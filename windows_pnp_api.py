from __future__ import annotations

import ctypes
import sys

from ctypes import wintypes

DIGCF_PRESENT = 0x00000002
DIGCF_ALLCLASSES = 0x00000004
DIGCF_DEVICEINTERFACE = 0x00000010

ERROR_NO_MORE_ITEMS = 259
ERROR_INSUFFICIENT_BUFFER = 122

DEVPROP_TYPE_STRING = 0x00000012
DEVPROP_TYPE_GUID = 0x0000000D

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class GUID(ctypes.Structure):
  _fields_ = [
    ("Data1", wintypes.DWORD),
    ("Data2", wintypes.WORD),
    ("Data3", wintypes.WORD),
    ("Data4", ctypes.c_ubyte * 8),
  ]


class DEVPROPKEY(ctypes.Structure):
  _fields_ = [
    ("fmtid", GUID),
    ("pid", wintypes.DWORD),
  ]


class SP_DEVINFO_DATA(ctypes.Structure):
  _fields_ = [
    ("cbSize", wintypes.DWORD),
    ("ClassGuid", GUID),
    ("DevInst", wintypes.DWORD),
    ("Reserved", ctypes.c_void_p),
  ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
  _fields_ = [
    ("cbSize", wintypes.DWORD),
    ("InterfaceClassGuid", GUID),
    ("Flags", wintypes.DWORD),
    ("Reserved", ctypes.c_void_p),
  ]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
  _fields_ = [
    ("cbSize", wintypes.DWORD),
    ("DevicePath", wintypes.WCHAR),
  ]


def _make_guid(data1, data2, data3, data4):
  return GUID(
    data1,
    data2,
    data3,
    (ctypes.c_ubyte * 8)(*data4),
  )


def _make_property_key(guid, pid):
  return DEVPROPKEY(guid, pid)


GUID_DEVINTERFACE_HID = _make_guid(
  0x4D1E55B2,
  0xF16F,
  0x11CF,
  (0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30),
)

GUID_DEVPROP_DEVICE = _make_guid(
  0x4340A6C5,
  0x93FA,
  0x4706,
  (0x97, 0x2C, 0x7B, 0x64, 0x80, 0x08, 0xA5, 0xA7),
)

GUID_DEVPROP_CONTAINER = _make_guid(
  0x8C7ED206,
  0x3F8A,
  0x4827,
  (0xB3, 0xAB, 0xAE, 0x9E, 0x1F, 0xAE, 0xFC, 0x6C),
)

GUID_DEVPROP_GENERAL = _make_guid(
  0xA45C254E,
  0xDF1C,
  0x4EFD,
  (0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0),
)


def _setupapi_functions():
  setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

  setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID),
    wintypes.LPCWSTR,
    wintypes.HWND,
    wintypes.DWORD,
  ]
  setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p

  setupapi.SetupDiEnumDeviceInfo.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(SP_DEVINFO_DATA),
  ]
  setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL

  setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(SP_DEVINFO_DATA),
    ctypes.POINTER(GUID),
    wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
  ]
  setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL

  setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(SP_DEVINFO_DATA),
  ]
  setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL

  setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(SP_DEVINFO_DATA),
    wintypes.LPWSTR,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
  ]
  setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL

  setupapi.SetupDiGetDevicePropertyW.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(SP_DEVINFO_DATA),
    ctypes.POINTER(DEVPROPKEY),
    ctypes.POINTER(wintypes.ULONG),
    ctypes.POINTER(ctypes.c_ubyte),
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.DWORD,
  ]
  setupapi.SetupDiGetDevicePropertyW.restype = wintypes.BOOL

  setupapi.SetupDiDestroyDeviceInfoList.argtypes = [
    ctypes.c_void_p,
  ]
  setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

  return setupapi


def _get_instance_id(setupapi, device_info_set, device_info):
  buffer = ctypes.create_unicode_buffer(1024)
  required = wintypes.DWORD()

  if not setupapi.SetupDiGetDeviceInstanceIdW(
    device_info_set,
    ctypes.byref(device_info),
    buffer,
    len(buffer),
    ctypes.byref(required),
  ):
    return None

  return buffer.value


def _get_property(setupapi, device_info_set, device_info, key):
  buffer = (ctypes.c_ubyte * 1024)()
  property_type = wintypes.ULONG()
  required = wintypes.DWORD()

  success = setupapi.SetupDiGetDevicePropertyW(
    device_info_set,
    ctypes.byref(device_info),
    ctypes.byref(key),
    ctypes.byref(property_type),
    buffer,
    ctypes.sizeof(buffer),
    ctypes.byref(required),
    0,
  )

  if not success:
    return None

  if property_type.value == DEVPROP_TYPE_STRING:
    return ctypes.wstring_at(ctypes.addressof(buffer))

  if property_type.value == DEVPROP_TYPE_GUID:
    guid = ctypes.cast(
      buffer,
      ctypes.POINTER(GUID),
    ).contents

    data4 = bytes(guid.Data4)

    return (
      f"{{{guid.Data1:08X}-{guid.Data2:04X}-"
      f"{guid.Data3:04X}-{data4[0]:02X}{data4[1]:02X}-"
      f"{data4[2:].hex().upper()}}}"
    )

  return None


def _find_hid_device(setupapi, hid_path):
  device_info_set = setupapi.SetupDiGetClassDevsW(
    ctypes.byref(GUID_DEVINTERFACE_HID),
    None,
    None,
    DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
  )

  if device_info_set == INVALID_HANDLE_VALUE:
    raise ctypes.WinError(ctypes.get_last_error())

  target_path = hid_path.casefold()

  try:
    index = 0

    while True:
      interface_data = SP_DEVICE_INTERFACE_DATA()
      interface_data.cbSize = ctypes.sizeof(
        SP_DEVICE_INTERFACE_DATA
      )

      if not setupapi.SetupDiEnumDeviceInterfaces(
        device_info_set,
        None,
        ctypes.byref(GUID_DEVINTERFACE_HID),
        index,
        ctypes.byref(interface_data),
      ):
        error = ctypes.get_last_error()

        if error == ERROR_NO_MORE_ITEMS:
          break

        raise ctypes.WinError(error)

      device_info = SP_DEVINFO_DATA()
      device_info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)

      required = wintypes.DWORD()

      setupapi.SetupDiGetDeviceInterfaceDetailW(
        device_info_set,
        ctypes.byref(interface_data),
        None,
        0,
        ctypes.byref(required),
        ctypes.byref(device_info),
      )

      error = ctypes.get_last_error()

      if error != ERROR_INSUFFICIENT_BUFFER:
        raise ctypes.WinError(error)

      detail_buffer = ctypes.create_string_buffer(
        required.value
      )

      detail = ctypes.cast(
        detail_buffer,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W),
      )

      detail.contents.cbSize = (
        8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
      )

      if not setupapi.SetupDiGetDeviceInterfaceDetailW(
        device_info_set,
        ctypes.byref(interface_data),
        detail_buffer,
        required.value,
        ctypes.byref(required),
        ctypes.byref(device_info),
      ):
        raise ctypes.WinError(ctypes.get_last_error())

      interface_path = ctypes.wstring_at(
        ctypes.addressof(detail_buffer)
        + SP_DEVICE_INTERFACE_DETAIL_DATA_W.DevicePath.offset
      )

      if interface_path.casefold() == target_path:
        return _get_instance_id(
          setupapi,
          device_info_set,
          device_info,
        )

      index += 1

  finally:
    setupapi.SetupDiDestroyDeviceInfoList(device_info_set)

  return None


def _enumerate_pnp_devices(setupapi):
  device_info_set = setupapi.SetupDiGetClassDevsW(
    None,
    None,
    None,
    DIGCF_PRESENT | DIGCF_ALLCLASSES,
  )

  if device_info_set == INVALID_HANDLE_VALUE:
    raise ctypes.WinError(ctypes.get_last_error())

  devices = {}

  try:
    index = 0

    while True:
      device_info = SP_DEVINFO_DATA()
      device_info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)

      if not setupapi.SetupDiEnumDeviceInfo(
        device_info_set,
        index,
        ctypes.byref(device_info),
      ):
        error = ctypes.get_last_error()

        if error == ERROR_NO_MORE_ITEMS:
          break

        raise ctypes.WinError(error)

      instance_id = _get_instance_id(
        setupapi,
        device_info_set,
        device_info,
      )

      if instance_id:
        devices[instance_id.upper()] = {
          "instance_id": instance_id,
          "parent": _get_property(
            setupapi,
            device_info_set,
            device_info,
            _make_property_key(GUID_DEVPROP_DEVICE, 8),
          ),
          "container_id": _get_property(
            setupapi,
            device_info_set,
            device_info,
            _make_property_key(GUID_DEVPROP_CONTAINER, 2),
          ),
          "class": _get_property(
            setupapi,
            device_info_set,
            device_info,
            _make_property_key(GUID_DEVPROP_GENERAL, 9),
          ),
        }

      index += 1

  finally:
    setupapi.SetupDiDestroyDeviceInfoList(device_info_set)

  return devices


def _find_physical_device(devices, hid_device):
  container_id = hid_device["container_id"]

  if not container_id:
    raise LookupError("HID device has no Container ID.")

  current = hid_device

  while current.get("parent"):
    parent = devices.get(current["parent"].upper())

    if parent is None:
      break

    instance_id = parent["instance_id"].upper()

    if (
      instance_id.startswith("USB\\")
      and "&MI_" not in instance_id
      and parent["container_id"]
      and parent["container_id"].upper() == container_id.upper()
    ):
      return parent

    current = parent

  raise LookupError(
    "Physical USB device was not found in the HID parent chain."
  )


def resolve_pnp_devices(hid_path: bytes | str) -> dict:
  if sys.platform != "win32":
    raise RuntimeError("PnP resolution is only available on Windows.")

  if isinstance(hid_path, bytes):
    hid_path = hid_path.decode(errors="replace")

  if not isinstance(hid_path, str) or not hid_path:
    raise ValueError("A valid HID device path is required.")

  setupapi = _setupapi_functions()

  hid_instance_id = _find_hid_device(
    setupapi,
    hid_path,
  )

  if hid_instance_id is None:
    raise LookupError(
      "Exact HID interface path was not found in the PnP device list."
    )

  devices = _enumerate_pnp_devices(setupapi)

  hid_device = devices.get(hid_instance_id.upper())

  if hid_device is None:
    raise LookupError(
      "HID device instance was not found in the PnP device list."
    )

  container_id = hid_device["container_id"]

  if not container_id:
    raise LookupError("HID device has no Container ID.")

  physical_device = _find_physical_device(
    devices,
    hid_device,
  )

  endpoints = [
    device
    for device in devices.values()
    if (
      device["class"] == "AudioEndpoint"
      and device["instance_id"].upper().startswith("SWD\\MMDEVAPI\\")
      and device["container_id"]
      and device["container_id"].upper() == container_id.upper()
    )
  ]

  render_endpoint = next(
    (
      device
      for device in endpoints
      if device["instance_id"].upper().startswith(
        "SWD\\MMDEVAPI\\{0.0.0."
      )
    ),
    None,
  )

  capture_endpoint = next(
    (
      device
      for device in endpoints
      if device["instance_id"].upper().startswith(
        "SWD\\MMDEVAPI\\{0.0.1."
      )
    ),
    None,
  )

  return {
    "container_id": container_id,
    "device_instance_id": physical_device["instance_id"],
    "audio_render_instance_id": (
      render_endpoint["instance_id"]
      if render_endpoint else None
    ),
    "audio_capture_instance_id": (
      capture_endpoint["instance_id"]
      if capture_endpoint else None
    ),
  }
