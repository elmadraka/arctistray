import ctypes
import os
from ctypes import wintypes

import hid


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

HIDP_STATUS_SUCCESS = 0x00110000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class HIDPCaps(ctypes.Structure):
  _fields_ = [
    ("Usage", wintypes.USHORT),
    ("UsagePage", wintypes.USHORT),
    ("InputReportByteLength", wintypes.USHORT),
    ("OutputReportByteLength", wintypes.USHORT),
    ("FeatureReportByteLength", wintypes.USHORT),
    ("Reserved", wintypes.USHORT * 17),
    ("NumberLinkCollectionNodes", wintypes.USHORT),
    ("NumberInputButtonCaps", wintypes.USHORT),
    ("NumberInputValueCaps", wintypes.USHORT),
    ("NumberInputDataIndices", wintypes.USHORT),
    ("NumberOutputButtonCaps", wintypes.USHORT),
    ("NumberOutputValueCaps", wintypes.USHORT),
    ("NumberOutputDataIndices", wintypes.USHORT),
    ("NumberFeatureButtonCaps", wintypes.USHORT),
    ("NumberFeatureValueCaps", wintypes.USHORT),
    ("NumberFeatureDataIndices", wintypes.USHORT),
  ]


class WindowsHIDTransport:
  def __init__(
    self,
    device_path: bytes,
    input_report_size: int,
    output_config=None,
  ):
    self._device_path = os.fsdecode(device_path)
    self._input_report_length = input_report_size + 1
    self._output_config = output_config

    self._listener = None
    self._handle = None
    self._api = None

  def open(self):
    if os.name != "nt":
      raise OSError(
        "WindowsHIDTransport can only run on Windows"
      )

    if self._listener is not None or self._handle is not None:
      raise RuntimeError("Transport is already open")

    try:
      self._open_listener()

      if self._output_config is not None:
        self._open_output_handle()

    except Exception as original_error:
      try:
        self.close()
      except Exception as cleanup_error:
        raise ExceptionGroup(
          "Transport initialization and cleanup both failed",
          [original_error, cleanup_error],
        ) from None

      raise

  def _open_listener(self):
    listener = hid.device()
    listener.open_path(os.fsencode(self._device_path))
    self._listener = listener

  def _load_windows_api(self):
    kernel32 = ctypes.WinDLL(
      "kernel32",
      use_last_error=True,
    )

    hid_dll = ctypes.WinDLL(
      "hid",
      use_last_error=True,
    )

    kernel32.CreateFileW.argtypes = [
      wintypes.LPCWSTR,
      wintypes.DWORD,
      wintypes.DWORD,
      wintypes.LPVOID,
      wintypes.DWORD,
      wintypes.DWORD,
      wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    hid_dll.HidD_GetPreparsedData.argtypes = [
      wintypes.HANDLE,
      ctypes.POINTER(ctypes.c_void_p),
    ]
    hid_dll.HidD_GetPreparsedData.restype = wintypes.BOOL

    hid_dll.HidD_FreePreparsedData.argtypes = [
      ctypes.c_void_p,
    ]
    hid_dll.HidD_FreePreparsedData.restype = wintypes.BOOL

    hid_dll.HidD_SetOutputReport.argtypes = [
      wintypes.HANDLE,
      ctypes.POINTER(ctypes.c_ubyte),
      wintypes.ULONG,
    ]
    hid_dll.HidD_SetOutputReport.restype = wintypes.BOOL

    hid_dll.HidP_GetCaps.argtypes = [
      ctypes.c_void_p,
      ctypes.POINTER(HIDPCaps),
    ]
    hid_dll.HidP_GetCaps.restype = ctypes.c_long

    self._api = {
      "kernel32": kernel32,
      "hid": hid_dll,
    }

  def _open_output_handle(self):
    self._load_windows_api()

    handle = self._api["kernel32"].CreateFileW(
      self._device_path,
      GENERIC_READ | GENERIC_WRITE,
      FILE_SHARE_READ | FILE_SHARE_WRITE,
      None,
      OPEN_EXISTING,
      0,
      None,
    )

    if handle == INVALID_HANDLE_VALUE:
      raise ctypes.WinError(ctypes.get_last_error())

    self._handle = handle

    self._validate_output_report_length()

  def _validate_output_report_length(self):
    preparsed_data = ctypes.c_void_p()

    hid_dll = self._api["hid"]

    if not hid_dll.HidD_GetPreparsedData(
      self._handle,
      ctypes.byref(preparsed_data),
    ):
      raise ctypes.WinError(ctypes.get_last_error())

    try:
      caps = HIDPCaps()

      status = hid_dll.HidP_GetCaps(
        preparsed_data,
        ctypes.byref(caps),
      )

      if status != HIDP_STATUS_SUCCESS:
        raise OSError(
          f"HidP_GetCaps failed: 0x{status & 0xFFFFFFFF:08X}"
        )

      expected_length = (
        self._output_config["report_length"] + 1
      )

      actual_length = caps.OutputReportByteLength

      if actual_length != expected_length:
        raise ValueError(
          "Unexpected HID output report length: "
          f"expected={expected_length}, actual={actual_length}"
        )

    finally:
      hid_dll.HidD_FreePreparsedData(preparsed_data)

  def read(self, timeout):
    if self._listener is None:
      raise RuntimeError("Transport is not open")

    report = self._listener.read(
      self._input_report_length,
      max(0, int(timeout)),
    )

    return bytes(report) if report else None

  def write(self, data):
    if self._handle is None:
      raise RuntimeError(
        "Native output handle is not available"
      )

    payload = bytes(data)

    report_id = self._output_config["report_id"]
    report_length = self._output_config["report_length"]

    if len(payload) > report_length:
      raise ValueError(
        "Output payload exceeds configured report length"
      )

    report = (
      bytes([report_id])
      + payload
      + bytes(report_length - len(payload))
    )

    buffer = (
      ctypes.c_ubyte * len(report)
    ).from_buffer_copy(report)

    success = self._api["hid"].HidD_SetOutputReport(
      self._handle,
      buffer,
      len(report),
    )

    if not success:
      raise ctypes.WinError(ctypes.get_last_error())
  
  def close(self):
    errors = []

    if self._listener is not None:
      listener = self._listener
      self._listener = None

      try:
        listener.close()
      except Exception as exc:
        errors.append(exc)

    if self._handle is not None:
      handle = self._handle
      self._handle = None

      try:
        if not self._api["kernel32"].CloseHandle(handle):
          raise ctypes.WinError(ctypes.get_last_error())
      except Exception as exc:
        errors.append(exc)

    if errors:
      raise ExceptionGroup(
        "Failed to close HID transport resources",
        errors,
      )
