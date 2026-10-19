import re
import subprocess
import threading

import comtypes

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
  return (value or "").strip().strip("{}").lower()


def get_endpoint_guid(endpoint_id):
  match = re.search(r"\.\{([^}]+)\}$", endpoint_id or "")
  return normalize(match.group(1)) if match else None


class AudioDiscovery:
  SUPPORTED_KINDS = ("headset", "physical", "virtual")

  def __init__(self):
    self._condition = threading.Condition()
    self._devices = []
    self._status = "initializing"
    self._last_error = None
    self._refreshing = False

  def get_status(self):
    with self._condition:
      return {
        "status": self._status,
        "error": self._last_error
      }

  def get_pnp_data(self):
    result = subprocess.run(
      ["powershell.exe", "-NoProfile", "-Command", POWERSHELL_SCRIPT],
      capture_output=True,
      text=True,
      encoding="utf-8",
      errors="replace",
      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
      timeout=60
    )

    if result.returncode != 0:
      raise RuntimeError(
        result.stderr or "Error consultando los dispositivos PnP."
      )

    receivers = []
    interfaces = []
    endpoints = []

    for line in result.stdout.splitlines():
      parts = line.split("|", 5)

      if parts[0] == "RECEIVER" and len(parts) >= 3:
        receivers.append({
          "instance_id": parts[1],
          "container_id": normalize(parts[2])
        })

      elif parts[0] == "AUDIO" and len(parts) >= 4:
        interfaces.append({
          "instance_id": parts[1],
          "parent": parts[2],
          "container_id": normalize(parts[3])
        })

      elif parts[0] == "ENDPOINT" and len(parts) >= 6:
        endpoints.append({
          "name": parts[1],
          "instance_id": parts[2],
          "parent": parts[3],
          "container_id": normalize(parts[4]),
          "status": parts[5]
        })

    return receivers, interfaces, endpoints

  @staticmethod
  def _get_core_state(device):
    try:
      return device._dev.GetState()
    except (AttributeError, OSError, comtypes.COMError):
      return None

  def discover(self):
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

      # IMMDevice state: 1 = ACTIVE. Unknown states remain unknown.
      core_state = self._get_core_state(device)

      core_devices[guid] = {
        "id": endpoint_id,
        "name": device.FriendlyName or endpoint_id,
        "flow": flow,
        "available": core_state == 1 if core_state is not None else None
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

      # Never accept a stale Arctis endpoint without a current PnP match.
      if "arctis 7+" in name_lower and not is_headset:
        continue

      # Sonar can remain valid even when PnP reports Unknown.
      if is_sonar:
        kind = "virtual"
      elif is_headset:
        kind = "headset"
      elif endpoint and endpoint["status"] == "OK":
        kind = "physical"
      else:
        continue

      devices.append({
        **core,
        "kind": kind,
        "pnp_status": endpoint["status"] if endpoint else None,
        "pnp_instance_id": endpoint["instance_id"] if endpoint else None
      })

    return devices
    
  def _refresh(self):
    with self._condition:
      if self._refreshing:
        while self._refreshing:
          self._condition.wait()

        if self._status == "error":
          raise RuntimeError(
            f"No se pudo actualizar el catálogo: {self._last_error}"
          )

        return

      self._refreshing = True
      self._status = "processing"
      self._last_error = None

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

    if error is not None:
      raise error

  def _get_devices(self, devices, flow=None, kind=None, available=None):
    return [
      device.copy() for device in devices
      if (flow is None or device["flow"] == flow)
      and (
        kind is None
        or device["kind"] == kind
        or (
          isinstance(kind, (tuple, list, set, frozenset))
          and device["kind"] in kind
        )
      )
      and (
        available is None
        or device["available"] is available
      )
    ]

  def _get_supported_outputs(self, devices):
    return self._get_devices(
      devices,
      flow="output",
      kind=self.SUPPORTED_KINDS
    )

  def _get_supported_inputs(self, devices):
    return self._get_devices(
      devices,
      flow="input",
      kind=self.SUPPORTED_KINDS
    )

  def _get_headset_output(self, devices):
    headsets = self._get_devices(
      devices,
      flow="output",
      kind="headset"
    )
    return headsets[0] if headsets else None

  def _get_headset_input(self, devices):
    headsets = self._get_devices(
      devices,
      flow="input",
      kind="headset"
    )
    return headsets[0] if headsets else None

  def get_catalog(self, refresh=False):
    if refresh:
      self._refresh()
    else:
      with self._condition:
        needs_initialization = (
          self._status == "initializing"
          and not self._refreshing
        )

      if needs_initialization:
        self._refresh()

    with self._condition:
      while self._refreshing or self._status == "initializing":
        self._condition.wait()

      if self._status == "error":
        raise RuntimeError(
          f"No se dispone de un catálogo actualizado: {self._last_error}"
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
        item for item in supported_outputs
        if item["available"] is True
      ],
      "active_inputs": [
        item for item in supported_inputs
        if item["available"] is True
      ],
      "headset_output": self._get_headset_output(devices),
      "headset_input": self._get_headset_input(devices)
    }