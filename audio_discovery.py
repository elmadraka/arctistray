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

$endpoints = @(Get-PnpDevice -Class AudioEndpoint)

foreach ($device in $endpoints) {
  Write-Output "ENDPOINT|$($device.FriendlyName)|$($device.InstanceId)|$($device.Status)"
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

  FLOW_VALUES = {"output": EDataFlow.eRender, "input": EDataFlow.eCapture}
  ROLE_VALUES = {
    "console": ERole.eConsole,
    "multimedia": ERole.eMultimedia,
    "communications": ERole.eCommunications,
  }

  _ENDPOINT_EVENTS = {
    "endpoint_added",
    "endpoint_removed",
  }

  _REFRESH_DEBOUNCE_SECONDS = 0.1

  _ENDPOINT_STATE_REFRESH_STATES = {
    1,  # DEVICE_STATE_ACTIVE
    4,  # DEVICE_STATE_NOTPRESENT
    8,  # DEVICE_STATE_UNPLUGGED
  }

  def _event_requires_refresh(self, event, payload):
    if event in self._ENDPOINT_EVENTS:
      return True

    if event != "endpoint_state_changed":
      return False

    try:
      new_state = int(payload.get("state"))
    except (TypeError, ValueError):
      return False

    if new_state not in self._ENDPOINT_STATE_REFRESH_STATES:
      return False

    endpoint_id = payload.get("endpoint_id")
    device = next(
      (
        device for device in self._devices
        if str(device.get("id", "")).casefold()
        == str(endpoint_id or "").casefold()
      ),
      None,
    )

    if device is None:
      return new_state == 1

    was_active = device.get("available") is True
    is_active = new_state == 1

    return was_active != is_active

  def __init__(self):
    self._condition = threading.Condition()
    self._devices = []
    self._status = "initializing"
    self._last_error = None
    self._refreshing = False
    self._refresh_pending = False

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
    """Return Windows audio endpoint PnP records."""
    result = subprocess.run(
      [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        POWERSHELL_SCRIPT,
      ],
      capture_output=True,
      text=True,
      encoding="utf-8",
      errors="replace",
      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
      timeout=60,
    )

    if result.returncode != 0:
      raise RuntimeError(
        result.stderr or "Failed to query Windows audio endpoints."
      )

    endpoints = []

    for line in result.stdout.splitlines():
      parts = line.split("|", 3)

      if parts[0] != "ENDPOINT" or len(parts) != 4:
        continue

      endpoints.append(
        {
          "name": parts[1],
          "instance_id": parts[2],
          "status": parts[3],
        }
      )

    return endpoints

  @staticmethod
  def _get_core_state(device):
    try:
      return device._dev.GetState()
    except (AttributeError, OSError, comtypes.COMError):
      return None

  def discover(self):
    """Discover all Windows audio endpoints."""
    pnp_endpoints = self.get_pnp_data()

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

    pnp_by_guid = {
      get_endpoint_guid(endpoint["instance_id"]): endpoint
      for endpoint in pnp_endpoints
      if get_endpoint_guid(endpoint["instance_id"])
    }

    devices = []

    for guid, core in core_devices.items():
      endpoint = pnp_by_guid.get(guid)

      name_lower = core["name"].casefold()

      # Keep virtual-device detection isolated for future changes.
      kind = (
        "virtual"
        if "steelseries sonar" in name_lower
        else "physical"
      )

      devices.append(
        {
          **core,
          "kind": kind,
          "pnp_status": endpoint["status"] if endpoint else None,
          "pnp_instance_id": endpoint["instance_id"] if endpoint else None,
        }
      )

    return devices

  def request_refresh(self):
    """Request an asynchronous audio catalog refresh."""
    if self._stop_event.is_set() or not self._worker or not self._worker.is_alive():
      raise RuntimeError("Audio discovery is not running.")

    with self._condition:
      if self._refreshing:
        return

      if self._refresh_pending:
        return

      self._refresh_pending = True
      self._event_queue.put(("__refresh__", {}))
  
  def _refresh(self):
    """Refresh the cached catalog once, coordinating concurrent callers."""
    started_at = time.perf_counter()

    with self._condition:
      if self._refreshing:
        while self._refreshing:
          self._condition.wait()
        if self._status == "error":
          raise RuntimeError(f"Catalog refresh failed: {self._last_error}")
        return

      self._refreshing = True
      self._refresh_pending = False
      self._status = "processing"
      self._last_error = None
      old_devices = [device.copy() for device in self._devices]

    self._emit("refresh_started", {})
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

    catalog = (
      self._build_catalog(new_snapshot)
      if error is None
      else None
    )

    self._emit(
      "refresh_finished",
      {
        "status": "error" if error is not None else "ready",
        "error": str(error) if error is not None else None,
        "catalog": catalog,
      },
    )

    if error is not None:
      raise error

  def _get_devices(self, devices, flow=None, kind=None, available=None):
    return [
      device.copy()
      for device in devices
      if (flow is None or device["flow"] == flow)
      and (kind is None or device["kind"] == kind)
      and (
        available is None
        or device["available"] is available
      )
    ]

  def _build_catalog(self, devices):
    """Build the public audio endpoint catalog."""
    devices = [device.copy() for device in devices]

    return {
      "devices": devices,
      "outputs": self._get_devices(
        devices,
        flow="output",
        kind="physical",
        available=True,
      ),
      "inputs": self._get_devices(
        devices,
        flow="input",
        kind="physical",
        available=True,
      ),
    }

  def get_catalog(self, refresh=False):
    """Return the current audio endpoint catalog."""
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

    return self._build_catalog(devices)

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

      try:
        self._refresh()
      except Exception:
        pass

      while not self._stop_event.is_set():
        try:
          event, payload = self._event_queue.get(timeout=0.5)
        except queue.Empty:
          continue

        if event == "__stop__":
          break

        refresh_requested = event == "__refresh__"
        refresh_pending = (
          self._event_requires_refresh(event, payload)
          or refresh_requested
        )

        if not refresh_requested:
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

          if (
            self._event_requires_refresh(next_event, next_payload)
            or next_event == "__refresh__"
          ):
            refresh_pending = True

          if next_event != "__refresh__":
            self._process_native_event(next_event, next_payload, refresh=False)

        if stop_requested or self._stop_event.is_set():
          break

        if refresh_pending:
          try:
            self._refresh()
          except Exception as exc:
            print(f"MONITOR refresh failed: {exc}")

        if self._stop_event.is_set():
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

    if self._event_requires_refresh(event, payload):
      try:
        self._refresh()
      except Exception:
        pass

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
      enumerator = AudioUtilities.GetDeviceEnumerator()
      data_flow = self.FLOW_VALUES[flow]
      current_devices = {}

      for role_name, role in self.ROLE_VALUES.items():
        try:
          endpoint = enumerator.GetDefaultAudioEndpoint(
            data_flow.value, role.value
          )
          current_devices[role_name] = endpoint.GetId()
        except Exception:
          current_devices[role_name] = None

      changed = set()

      for role_name, endpoint_id in devices.items():
        if not endpoint_id:
          continue

        current = current_devices.get(role_name)
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