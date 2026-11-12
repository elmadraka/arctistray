
from __future__ import annotations

import ctypes
import sys

from ctypes import wintypes

import hid


VENDOR_ID = 0x1038
DONGLE_ID = 0x220E

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
DIGCF_ALLCLASSES = 0x00000004
ERROR_NO_MORE_ITEMS = 259

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


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
  _fields_ = [
    ("cbSize", wintypes.DWORD),
    ("InterfaceClassGuid", GUID),
    ("Flags", wintypes.DWORD),
    ("Reserved", ctypes.c_void_p),
  ]


class SP_DEVINFO_DATA(ctypes.Structure):
  _fields_ = [
    ("cbSize", wintypes.DWORD),
    ("ClassGuid", GUID),
    ("DevInst", wintypes.DWORD),
    ("Reserved", ctypes.c_void_p),
  ]


def make_guid(data1, data2, data3, data4):
  return GUID(
    data1,
    data2,
    data3,
    (ctypes.c_ubyte * 8)(*data4),
  )


def make_property_key(guid, pid):
  return DEVPROPKEY(guid, pid)


GUID_DEVINTERFACE_HID = make_guid(
  0x4D1E55B2,
  0xF16F,
  0x11CF,
  (0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30),
)

# DEVPKEY_Device_Parent
GUID_DEVPROP_DEVICE = make_guid(
  0x4340A6C5,
  0x93FA,
  0x4706,
  (0x97, 0x2C, 0x7B, 0x64, 0x80, 0x08, 0xA5, 0xA7),
)

# DEVPKEY_Device_ContainerId
GUID_DEVPROP_CONTAINER = make_guid(
  0x8C7ED206,
  0x3F8A,
  0x4827,
  (0xB3, 0xAB, 0xAE, 0x9E, 0x1F, 0xAE, 0xFC, 0x6C),
)

# DEVPKEY_Device_FriendlyName
# DEVPKEY_Device_DeviceDesc
GUID_DEVPROP_GENERAL = make_guid(
  0xA45C254E,
  0xDF1C,
  0x4EFD,
  (0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0),
)

# DEVPKEY_Device_Class
GUID_DEVPROP_CLASS = GUID_DEVPROP_GENERAL


def setupapi_functions():
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


def get_instance_id(setupapi, device_info_set, device_info):
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


def get_property(
  setupapi,
  device_info_set,
  device_info,
  key,
):
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


def enumerate_pnp_devices(setupapi):
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

      instance_id = get_instance_id(
        setupapi,
        device_info_set,
        device_info,
      )

      if instance_id:
        devices[instance_id.upper()] = {
          "instance_id": instance_id,
          "parent": get_property(
            setupapi,
            device_info_set,
            device_info,
            make_property_key(GUID_DEVPROP_DEVICE, 8),
          ),
          "container_id": get_property(
            setupapi,
            device_info_set,
            device_info,
            make_property_key(GUID_DEVPROP_CONTAINER, 2),
          ),
          "friendly_name": get_property(
            setupapi,
            device_info_set,
            device_info,
            make_property_key(GUID_DEVPROP_GENERAL, 14),
          ),
          "description": get_property(
            setupapi,
            device_info_set,
            device_info,
            make_property_key(GUID_DEVPROP_GENERAL, 2),
          ),
          "class": get_property(
            setupapi,
            device_info_set,
            device_info,
            make_property_key(GUID_DEVPROP_CLASS, 9),
          ),
        }

      index += 1

  finally:
    setupapi.SetupDiDestroyDeviceInfoList(device_info_set)

  return devices


def enumerate_hid_interfaces(setupapi, hid_devices):
  device_info_set = setupapi.SetupDiGetClassDevsW(
    ctypes.byref(GUID_DEVINTERFACE_HID),
    None,
    None,
    DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
  )

  if device_info_set == INVALID_HANDLE_VALUE:
    raise ctypes.WinError(ctypes.get_last_error())

  hid_paths = {}

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

      required = wintypes.DWORD()

      setupapi.SetupDiGetDeviceInterfaceDetailW(
        device_info_set,
        ctypes.byref(interface_data),
        None,
        0,
        ctypes.byref(required),
        None,
      )

      detail_buffer = ctypes.create_string_buffer(
        required.value
      )

      # SP_DEVICE_INTERFACE_DETAIL_DATA_W has a 4-byte
      # DevicePath offset on Windows.
      ctypes.cast(
        detail_buffer,
        ctypes.POINTER(wintypes.DWORD),
      ).contents.value = (
        8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
      )

      device_info = SP_DEVINFO_DATA()
      device_info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)

      if not setupapi.SetupDiGetDeviceInterfaceDetailW(
        device_info_set,
        ctypes.byref(interface_data),
        detail_buffer,
        required.value,
        ctypes.byref(required),
        ctypes.byref(device_info),
      ):
        raise ctypes.WinError(ctypes.get_last_error())

      device_path = ctypes.wstring_at(
        ctypes.addressof(detail_buffer) + 4
      )

      instance_id = get_instance_id(
        setupapi,
        device_info_set,
        device_info,
      )

      if instance_id:
        hid_paths[device_path.casefold()] = instance_id

      index += 1

  finally:
    setupapi.SetupDiDestroyDeviceInfoList(device_info_set)

  results = []

  for device in hid_devices:
    path = device["path"]

    if isinstance(path, bytes):
      path = path.decode(errors="replace")

    instance_id = hid_paths.get(path.casefold())

    results.append({
      "hid": device,
      "path": path,
      "instance_id": instance_id,
    })

  return results


def print_device_tree(instance_id, devices):
  current_id = instance_id
  visited = set()
  depth = 0

  while current_id:
    key = current_id.upper()

    if key in visited:
      print("  " * depth + "[Parent cycle detected]")
      break

    visited.add(key)

    device = devices.get(key)

    if device is None:
      print("  " * depth + current_id + " [not in present-device list]")
      break

    print("  " * depth + device["instance_id"])
    print("  " * depth + f"    Name: {device['friendly_name'] or device['description']}")
    print("  " * depth + f"    Class: {device['class']}")
    print("  " * depth + f"    Container ID: {device['container_id']}")

    current_id = device["parent"]
    depth += 1


def main():
  if sys.platform != "win32":
    raise RuntimeError("This diagnostic must be run on Windows.")

  print("Enumerating HID interfaces...")
  hid_devices = hid.enumerate(VENDOR_ID, DONGLE_ID)

  if not hid_devices:
    print("No matching HID interfaces found.")
    return

  setupapi = setupapi_functions()

  print("Enumerating present PnP devices...")
  devices = enumerate_pnp_devices(setupapi)

  print("Resolving HID interface paths...")
  interfaces = enumerate_hid_interfaces(
    setupapi,
    hid_devices,
  )

  hid_container_ids = set()

  print("\n" + "=" * 72)
  print("HID INTERFACES")
  print("=" * 72)

  for index, interface in enumerate(interfaces, 1):
    hid_info = interface["hid"]
    instance_id = interface["instance_id"]

    print(f"\nInterface {index}")
    print(f"  Product ID: 0x{hid_info['product_id']:04X}")
    print(f"  Interface number: {hid_info.get('interface_number')}")
    print(f"  Usage page: {hid_info.get('usage_page')}")
    print(f"  Usage: {hid_info.get('usage')}")
    print(f"  HID path: {interface['path']}")
    print(f"  PnP instance ID: {instance_id}")

    if instance_id:
      print("\n  PnP parent chain:")
      print_device_tree(instance_id, devices)

      node = devices.get(instance_id.upper())

      if node and node["container_id"]:
        hid_container_ids.add(node["container_id"].upper())

  print("\n" + "=" * 72)
  print("HID CONTAINER IDS")
  print("=" * 72)

  if not hid_container_ids:
    print("No Container IDs could be read from the HID nodes.")
  else:
    for container_id in sorted(hid_container_ids):
      print(f"  {container_id}")

  print("\n" + "=" * 72)
  print("AUDIO ENDPOINT PNP DEVICES")
  print("=" * 72)

  endpoints = [
    device
    for device in devices.values()
    if (
      device["instance_id"].upper().startswith("SWD\\MMDEVAPI\\")
      and device["class"] == "AudioEndpoint"
    )
  ]

  if not endpoints:
    print("No present SWD\\MMDEVAPI AudioEndpoint devices found.")

  for endpoint in endpoints:
    container_id = endpoint["container_id"]
    instance_id = endpoint["instance_id"]

    if container_id:
      matches = container_id.upper() in hid_container_ids
    else:
      matches = False

    print(f"\nName: {endpoint['friendly_name'] or endpoint['description']}")
    print(f"  Instance ID: {instance_id}")
    print(f"  Container ID: {container_id}")
    print(f"  Matches HID Container ID: {matches}")

  print("\n" + "=" * 72)
  print("SUMMARY")
  print("=" * 72)
  print(f"HID interfaces found: {len(interfaces)}")
  print(f"Distinct HID Container IDs: {len(hid_container_ids)}")
  print(f"Audio endpoints found: {len(endpoints)}")

  matching_endpoints = [
    endpoint
    for endpoint in endpoints
    if (
      endpoint["container_id"]
      and endpoint["container_id"].upper() in hid_container_ids
    )
  ]

  print(f"Audio endpoints matching HID Container ID: {len(matching_endpoints)}")


if __name__ == "__main__":
  main()
