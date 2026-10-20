"""Discover Windows audio endpoints and publish audio-system events."""

import queue
import re
import subprocess
import threading
import time
from collections import defaultdict

import comtypes
from pycaw.callbacks import MMNotificationClient
from pycaw.constants import EDataFlow, ERole
from pycaw.pycaw import AudioUtilities


POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'

$receivers = @(Get-PnpDevice -Class USB | Where-Object {
  $_.InstanceId -match '^USB\\VID_1038&PID_220E\\' -and
  $_.Status -eq 'OK'
})

$interfaces = @(Get-PnpDevice -Class MEDIA | Where-Object {
  $_.InstanceId -match '^USB\\VID_1038&PID_220E&MI_00\\' -and
  $_.Status -eq 'OK'
})

$endpoints = @(Get-PnpDevice -Class AudioEndpoint)
$receiverProps = @{}
if ($receivers.Count -gt 0) {
  Get-PnpDeviceProperty `
    -InstanceId @($receivers | ForEach-Object InstanceId) `
    -KeyName 'DEVPKEY_Device_ContainerId' | ForEach-Object {
      $receiverProps[$_.InstanceId] = $_.Data
    }
}
$interfaceProps = @{}
if ($interfaces.Count -gt 0) {
  Get-PnpDeviceProperty `
    -InstanceId @($interfaces | ForEach-Object InstanceId) `
    -KeyName 'DEVPKEY_Device_Parent', 'DEVPKEY_Device_ContainerId' |
    ForEach-Object {
      if (-not $interfaceProps.ContainsKey($_.InstanceId)) {
        $interfaceProps[$_.InstanceId] = @{}
      }
      $interfaceProps[$_.InstanceId][$_.KeyName] = $_.Data
    }
}
$endpointProps = @{}
if ($endpoints.Count -gt 0) {
  Get-PnpDeviceProperty `
    -InstanceId @($endpoints | ForEach-Object InstanceId) `
    -KeyName 'DEVPKEY_Device_Parent', 'DEVPKEY_Device_ContainerId' |
    ForEach-Object {
      if (-not $endpointProps.ContainsKey($_.InstanceId)) {
        $endpointProps[$_.InstanceId] = @{}
      }
      $endpointProps[$_.InstanceId][$_.KeyName] = $_.Data
    }
}
foreach ($device in $receivers) {
  Write-Output "RECEIVER|$($device.InstanceId)|$($receiverProps[$device.InstanceId])"
}

foreach ($device in $interfaces) {
  $props = $interfaceProps[$device.InstanceId]
  Write-Output "AUDIO|$($device.InstanceId)|$($props['DEVPKEY_Device_Parent'])|$($props['DEVPKEY_Device_ContainerId'])"
}
foreach ($device in $endpoints) {
  $props = $endpointProps[$device.InstanceId]
  Write-Output "ENDPOINT|$($device.FriendlyName)|$($device.InstanceId)|$($props['DEVPKEY_Device_Parent'])|$($props['DEVPKEY_Device_ContainerId'])|$($device.Status)"
}
"""


def normalize(value):
    """Normalize a device identifier or GUID for comparisons."""
    return (value or "").strip().strip("{}").lower()


def get_endpoint_guid(endpoint_id):
    """Extract the endpoint GUID embedded in a Windows endpoint ID."""
    match = re.search(r"\.\{([^}]+)\}$", endpoint_id or "")
    return normalize(match.group(1)) if match else None


class _AudioNotificationClient(MMNotificationClient):
    """Forward native Windows audio notifications to AudioDiscovery."""

    def __init__(self, discovery):
        super().__init__()
        self._discovery = discovery
    
    def on_device_state_changed(self, device_id, new_state, new_state_id=None):
        self._discovery._enqueue_native_event(
            "endpoint_state_changed",
            {
                "endpoint_id": device_id,
                "state": new_state,
                "state_id": new_state_id,
            },
        )

    def on_device_added(self, device_id):
        self._discovery._enqueue_native_event(
            "endpoint_added", {"endpoint_id": device_id}
        )

    def on_device_removed(self, device_id):
        self._discovery._enqueue_native_event(
            "endpoint_removed", {"endpoint_id": device_id}
        )

    def on_default_device_changed(
        self, flow, flow_id, role, role_id, default_device_id
    ):
        flow_name = {
            "eRender": "output",
            "eCapture": "input",
        }.get(flow)

        if flow_name is None:
            flow_name = {
                EDataFlow.eRender.value: "output",
                EDataFlow.eCapture.value: "input",
            }.get(flow_id)

        role_name = {
            "eConsole": "console",
            "eMultimedia": "multimedia",
            "eCommunications": "communications",
        }.get(role)

        if role_name is None:
            role_name = {
                ERole.eConsole.value: "console",
                ERole.eMultimedia.value: "multimedia",
                ERole.eCommunications.value: "communications",
            }.get(role_id)

        if flow_name is None or role_name is None:
            self._discovery._enqueue_native_event(
                "discovery_error",
                {
                    "error": (
                        "Unrecognized native default-device notification: "
                        f"flow={flow!r}, flow_id={flow_id!r}, "
                        f"role={role!r}, role_id={role_id!r}"
                    )
                },
            )
            return

        self._discovery._enqueue_native_event(
            "default_device_changed",
            {
                "flow": flow_name,
                "role": role_name,
                "endpoint_id": default_device_id,
            },
        )

    def on_property_value_changed(
        self, device_id, property_struct, fmtid=None, pid=None
    ):
        self._discovery._enqueue_native_event(
            "endpoint_property_changed",
            {
                "endpoint_id": device_id,
                "property_key": (
                    f"{fmtid} {pid}"
                    if fmtid is not None and pid is not None
                    else str(property_struct)
                ),
            },
        )


class AudioDiscovery:
    """Discover Windows audio endpoints and publish system-level events."""

    SUPPORTED_KINDS = ("headset", "physical", "virtual")
    FLOW_VALUES = {"output": EDataFlow.eRender, "input": EDataFlow.eCapture}
    ROLE_VALUES = {
        "console": ERole.eConsole,
        "multimedia": ERole.eMultimedia,
        "communications": ERole.eCommunications,
    }

    _ENDPOINT_EVENTS = {
        "endpoint_added",
        "endpoint_removed",
        "endpoint_state_changed",
        "endpoint_property_changed",
    }

    _REFRESH_DEBOUNCE_SECONDS = 0.2

    def __init__(self):
        self._condition = threading.Condition()
        self._devices = []
        self._status = "initializing"
        self._last_error = None
        self._refreshing = False

        self._listeners = defaultdict(list)
        self._listener_lock = threading.RLock()
        self._event_queue = queue.Queue()
        self._worker = None
        self._stop_event = threading.Event()
        self._observer = None
        self._enumerator = None
        self._monitoring = False
        self._lifecycle_lock = threading.RLock()

    def get_status(self):
        """Return the current discovery status and last error."""
        with self._condition:
            return {"status": self._status, "error": self._last_error}

    def get_pnp_data(self):
        """Return receiver, audio-interface, and endpoint PnP records."""
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", POWERSHELL_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=60,
        )

        if result.returncode != 0:
            raise RuntimeError(
                result.stderr or "Failed to query Windows PnP devices."
            )

        receivers = []
        interfaces = []
        endpoints = []
        for line in result.stdout.splitlines():
            parts = line.split("|", 5)
            if parts[0] == "RECEIVER" and len(parts) >= 3:
                receivers.append(
                    {
                        "instance_id": parts[1],
                        "container_id": normalize(parts[2]),
                    }
                )
            elif parts[0] == "AUDIO" and len(parts) >= 4:
                interfaces.append(
                    {
                        "instance_id": parts[1],
                        "parent": parts[2],
                        "container_id": normalize(parts[3]),
                    }
                )
            elif parts[0] == "ENDPOINT" and len(parts) >= 6:
                endpoints.append(
                    {
                        "name": parts[1],
                        "instance_id": parts[2],
                        "parent": parts[3],
                        "container_id": normalize(parts[4]),
                        "status": parts[5],
                    }
                )

        return receivers, interfaces, endpoints

    @staticmethod
    def _get_core_state(device):
        try:
            return device._dev.GetState()
        except (AttributeError, OSError, comtypes.COMError):
            return None

    def discover(self):
        """Perform a full discovery and return normalized endpoint records."""
        receivers, interfaces, pnp_endpoints = self.get_pnp_data()
        core_devices = {}

        for device in AudioUtilities.GetAllDevices():
            endpoint_id = getattr(device, "id", None)
            if not endpoint_id:
                continue

            guid = get_endpoint_guid(endpoint_id)
            if not guid:
                continue

            endpoint_id_lower = endpoint_id.lower()
            if endpoint_id_lower.startswith("{0.0.0."):
                flow = "output"
            elif endpoint_id_lower.startswith("{0.0.1."):
                flow = "input"
            else:
                continue

            core_state = self._get_core_state(device)
            core_devices[guid] = {
                "id": endpoint_id,
                "name": device.FriendlyName or endpoint_id,
                "flow": flow,
                "available": core_state == 1 if core_state is not None else None,
            }

        active_interfaces = []
        for receiver in receivers:
            for interface in interfaces:
                if (
                    interface["parent"].lower() == receiver["instance_id"].lower()
                    and interface["container_id"] == receiver["container_id"]
                ):
                    active_interfaces.append(interface)

        pnp_by_guid = {
            get_endpoint_guid(endpoint["instance_id"]): endpoint
            for endpoint in pnp_endpoints
            if get_endpoint_guid(endpoint["instance_id"])
        }

        devices = []
        for guid, core in core_devices.items():
            endpoint = pnp_by_guid.get(guid)
            name_lower = core["name"].lower()
            is_sonar = "steelseries sonar" in name_lower
            is_headset = bool(
                endpoint
                and endpoint["status"] in ("OK", "Unknown")
                and any(
                    (endpoint["parent"] or "").lower()
                    == interface["instance_id"].lower()
                    and endpoint["container_id"] == interface["container_id"]
                    for interface in active_interfaces
                )
            )

            if "arctis 7+" in name_lower and not is_headset:
                continue

            if is_sonar:
                kind = "virtual"
            elif is_headset:
                kind = "headset"
            elif endpoint and endpoint["status"] == "OK":
                kind = "physical"
            else:
                continue

            devices.append(
                {
                    **core,
                    "kind": kind,
                    "pnp_status": endpoint["status"] if endpoint else None,
                    "pnp_instance_id": endpoint["instance_id"] if endpoint else None,
                }
            )

        return devices

    def _refresh(self):
        """Refresh the cached catalog once, coordinating concurrent callers."""
        with self._condition:
            if self._refreshing:
                while self._refreshing:
                    self._condition.wait()
                if self._status == "error":
                    raise RuntimeError(
                        f"Catalog refresh failed: {self._last_error}"
                    )
                return

            self._refreshing = True
            self._status = "processing"
            self._last_error = None
            old_devices = [device.copy() for device in self._devices]

        new_devices = None
        error = None
        com_initialized = False
        try:
            comtypes.CoInitialize()
            com_initialized = True
            new_devices = self.discover()
        except Exception as exc:
            error = exc
        finally:
            if com_initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception as exc:
                    if error is None:
                        error = exc

        with self._condition:
            if error is None:
                self._devices = new_devices
                self._status = "ready"
                self._last_error = None
            else:
                self._status = "error"
                self._last_error = str(error)
            self._refreshing = False
            self._condition.notify_all()
            new_snapshot = [device.copy() for device in self._devices]

        if error is not None:
            raise error

        if old_devices != new_snapshot:
            self._emit(
                "catalog_changed",
                {"devices": [device.copy() for device in new_snapshot]},
            )

    def _get_devices(self, devices, flow=None, kind=None, available=None):
        return [
            device.copy()
            for device in devices
            if (flow is None or device["flow"] == flow)
            and (
                kind is None
                or device["kind"] == kind
                or (
                    isinstance(kind, (tuple, list, set, frozenset))
                    and device["kind"] in kind
                )
            )
            and (available is None or device["available"] is available)
        ]

    def _get_supported_outputs(self, devices):
        return self._get_devices(devices, flow="output", kind=self.SUPPORTED_KINDS)

    def _get_supported_inputs(self, devices):
        return self._get_devices(devices, flow="input", kind=self.SUPPORTED_KINDS)

    def _get_headset_output(self, devices):
        headsets = self._get_devices(devices, flow="output", kind="headset")
        return headsets[0] if headsets else None

    def _get_headset_input(self, devices):
        headsets = self._get_devices(devices, flow="input", kind="headset")
        return headsets[0] if headsets else None

    def get_catalog(self, refresh=False):
        """Return the catalog using the existing public schema."""
        if refresh:
            self._refresh()
        else:
            with self._condition:
                needs_initialization = (
                    self._status == "initializing" and not self._refreshing
                )
            if needs_initialization:
                self._refresh()

        with self._condition:
            while self._refreshing or self._status == "initializing":
                self._condition.wait()
            if self._status == "error":
                raise RuntimeError(
                    f"No current device catalog is available: {self._last_error}"
                )
            devices = [device.copy() for device in self._devices]

        outputs = self._get_devices(devices, flow="output")
        inputs = self._get_devices(devices, flow="input")
        supported_outputs = self._get_supported_outputs(devices)
        supported_inputs = self._get_supported_inputs(devices)
        return {
            "devices": devices,
            "outputs": outputs,
            "inputs": inputs,
            "supported_outputs": supported_outputs,
            "supported_inputs": supported_inputs,
            "active_outputs": [
                item for item in supported_outputs if item["available"] is True
            ],
            "active_inputs": [
                item for item in supported_inputs if item["available"] is True
            ],
            "headset_output": self._get_headset_output(devices),
            "headset_input": self._get_headset_input(devices),
        }

    def add_listener(self, event, callback):
        """Register a callback for an AudioDiscovery event."""
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._listener_lock:
            if callback not in self._listeners[event]:
                self._listeners[event].append(callback)

    def remove_listener(self, event, callback):
        """Remove a previously registered event callback."""
        with self._listener_lock:
            callbacks = self._listeners.get(event, [])
            if callback in callbacks:
                callbacks.remove(callback)
            if not callbacks and event in self._listeners:
                self._listeners.pop(event, None)

    def _emit(self, event, payload):
        with self._listener_lock:
            callbacks = tuple(self._listeners.get(event, ()))
        for callback in callbacks:
            try:
                callback(self, **payload)
            except Exception as exc:
                print(f"[AudioDiscovery] Listener failed for {event}: {exc}")

    def _enqueue_native_event(self, event, payload):
      if not self._stop_event.is_set():
        self._event_queue.put((event, payload))

    def start(self):
        """Start native endpoint monitoring; repeated calls are harmless."""
        with self._lifecycle_lock:
            if self._worker and self._worker.is_alive():
                return

            # Discard events left behind by a previous worker shutdown.
            # In particular, stop() may enqueue a sentinel after the worker
            # has already passed its final queue read.
            while True:
                try:
                    self._event_queue.get_nowait()
                except queue.Empty:
                    break

            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._monitor_loop,
                name="ArctisTrayAudioDiscovery",
                daemon=True,
            )
            self._worker.start()

    def stop(self, timeout=5):
        """Stop monitoring and release the native notification callback."""
        with self._lifecycle_lock:
            worker = self._worker
            if worker is None:
                return True
            self._stop_event.set()
            self._event_queue.put(("__stop__", {}))

        if worker is not threading.current_thread():
            worker.join(max(0, timeout))
        stopped = not worker.is_alive()
        if stopped:
            with self._lifecycle_lock:
                if self._worker is worker:
                    self._worker = None
        return stopped

    def _monitor_loop(self):
        com_initialized = False
        try:
            comtypes.CoInitialize()
            com_initialized = True
            self._enumerator = AudioUtilities.GetDeviceEnumerator()
            self._observer = _AudioNotificationClient(self)
            self._enumerator.RegisterEndpointNotificationCallback(self._observer)
            self._monitoring = True
            self._emit("monitoring_started", {})

            while not self._stop_event.is_set():
              try:
                  event, payload = self._event_queue.get(timeout=0.5)
              except queue.Empty:
                  continue

              if event == "__stop__":
                  break

              refresh_pending = event in self._ENDPOINT_EVENTS
              self._process_native_event(event, payload, refresh=False)

              if not refresh_pending:
                  continue

              deadline = time.monotonic() + self._REFRESH_DEBOUNCE_SECONDS
              stop_requested = False

              while not self._stop_event.is_set():
                  remaining = deadline - time.monotonic()
                  if remaining <= 0:
                      break

                  try:
                      next_event, next_payload = self._event_queue.get(
                          timeout=remaining
                      )
                  except queue.Empty:
                      break

                  if next_event == "__stop__":
                      stop_requested = True
                      break

                  if next_event in self._ENDPOINT_EVENTS:
                      refresh_pending = True

                  self._process_native_event(
                      next_event,
                      next_payload,
                      refresh=False,
                  )

              if refresh_pending:
                  try:
                      self._refresh()
                  except Exception as exc:
                      self._emit("discovery_error", {"error": str(exc)})

              if stop_requested:
                  break
        except Exception as exc:
            with self._condition:
                self._last_error = str(exc)
                if self._status != "ready":
                    self._status = "error"
                self._condition.notify_all()
            self._emit("discovery_error", {"error": str(exc)})
        finally:
            if self._enumerator is not None and self._observer is not None:
                try:
                    self._enumerator.UnregisterEndpointNotificationCallback(
                        self._observer
                    )
                except Exception as exc:
                    print(f"[AudioDiscovery] Failed to unregister notifications: {exc}")
            self._observer = None
            self._enumerator = None
            self._monitoring = False
            if com_initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception as exc:
                    print(f"[AudioDiscovery] COM cleanup failed: {exc}")
            self._emit("monitoring_stopped", {})

    def _process_native_event(self, event, payload, refresh=True):
      self._emit(event, payload)

      if not refresh or event == "default_device_changed":
          return

      if event in self._ENDPOINT_EVENTS:
          try:
              self._refresh()
          except Exception as exc:
              self._emit("discovery_error", {"error": str(exc)})

    def get_default_devices(self, flow):
        """Return default endpoint IDs by Windows role for output or input."""
        if flow not in self.FLOW_VALUES:
            raise ValueError("flow must be 'output' or 'input'")

        comtypes.CoInitialize()
        try:
            enumerator = AudioUtilities.GetDeviceEnumerator()
            data_flow = self.FLOW_VALUES[flow]
            result = {}
            for role_name, role in self.ROLE_VALUES.items():
                try:
                    endpoint = enumerator.GetDefaultAudioEndpoint(
                        data_flow.value, role.value
                    )
                    result[role_name] = endpoint.GetId()
                except Exception:
                    result[role_name] = None
            if not any(result.values()):
                raise RuntimeError(
                    f"Windows has no valid default {flow} endpoint."
                )
            return result
        finally:
            comtypes.CoUninitialize()

    def set_default_devices(self, flow, devices):
        """Set default endpoint IDs for one or more Windows roles."""
        if flow not in self.FLOW_VALUES:
            raise ValueError("flow must be 'output' or 'input'")
        if not isinstance(devices, dict):
            raise TypeError("devices must be a mapping of role names to endpoint IDs")

        unknown_roles = set(devices) - set(self.ROLE_VALUES)
        if unknown_roles:
            raise ValueError(f"Unsupported Windows audio roles: {sorted(unknown_roles)}")

        comtypes.CoInitialize()
        try:
            changed = set()
            for role_name, endpoint_id in devices.items():
                if not endpoint_id:
                    continue
                current = self.get_default_devices(flow).get(role_name)
                if current and current.casefold() == endpoint_id.casefold():
                    continue
                AudioUtilities.SetDefaultDevice(
                    endpoint_id, roles=[self.ROLE_VALUES[role_name]]
                )
                changed.add(role_name)
            return changed
        finally:
            comtypes.CoUninitialize()

    def is_endpoint_available(self, endpoint_id):
        """Return True only when the endpoint is currently active."""
        if not endpoint_id:
            return False
        comtypes.CoInitialize()
        try:
            for device in AudioUtilities.GetAllDevices():
                current_id = getattr(device, "id", None)
                if not current_id or current_id.casefold() != endpoint_id.casefold():
                    continue
                try:
                    return device._dev.GetState() == 1
                except Exception:
                    return False
            return False
        finally:
            comtypes.CoUninitialize()
