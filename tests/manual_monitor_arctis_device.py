
from __future__ import annotations

import ctypes
import logging
import threading
import time

from collections import deque
from ctypes import wintypes

from arctis_device import (
  ArctisDevice,
  DeviceEvent,
)


REFRESH_INTERVAL = 0.05
MAX_LOG_LINES = 2000
MAX_EVENTS = 20
MAX_WIDTH = 100

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class COORD(ctypes.Structure):
  _fields_ = [
    ("X", wintypes.SHORT),
    ("Y", wintypes.SHORT),
  ]


class SMALL_RECT(ctypes.Structure):
  _fields_ = [
    ("Left", wintypes.SHORT),
    ("Top", wintypes.SHORT),
    ("Right", wintypes.SHORT),
    ("Bottom", wintypes.SHORT),
  ]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
  _fields_ = [
    ("dwSize", COORD),
    ("dwCursorPosition", COORD),
    ("wAttributes", wintypes.WORD),
    ("srWindow", SMALL_RECT),
    ("dwMaximumWindowSize", COORD),
  ]


kernel32.GetStdHandle.restype = wintypes.HANDLE

kernel32.GetConsoleScreenBufferInfo.argtypes = [
  wintypes.HANDLE,
  ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO),
]
kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL

kernel32.WriteConsoleOutputCharacterW.argtypes = [
  wintypes.HANDLE,
  wintypes.LPCWSTR,
  wintypes.DWORD,
  COORD,
  ctypes.POINTER(wintypes.DWORD),
]
kernel32.WriteConsoleOutputCharacterW.restype = wintypes.BOOL

kernel32.SetConsoleCursorPosition.argtypes = [
  wintypes.HANDLE,
  COORD,
]
kernel32.SetConsoleCursorPosition.restype = wintypes.BOOL


class MonitorLogHandler(logging.Handler):
  def __init__(self, monitor):
    super().__init__()
    self.monitor = monitor

  def emit(self, record):
    try:
      message = self.format(record)

      for line in message.splitlines():
        self.monitor.add_log(line)

    except Exception:
      self.handleError(record)


class ArctisDeviceMonitor:
  def __init__(self):
    self._lock = threading.Lock()
    self._handle = kernel32.GetStdHandle(-11)

    self._events = deque(maxlen=MAX_EVENTS)
    self._logs = deque(maxlen=MAX_LOG_LINES)

  @staticmethod
  def _timestamp():
    return time.strftime("%H:%M:%S")

  def _get_console_info(self):
    info = CONSOLE_SCREEN_BUFFER_INFO()

    if not kernel32.GetConsoleScreenBufferInfo(
      self._handle,
      ctypes.byref(info),
    ):
      raise ctypes.WinError(ctypes.get_last_error())

    return info

  def add_log(self, message):
    with self._lock:
      self._logs.append(
        f"[{self._timestamp()}] {message}"
      )

  def on_event(self, event: DeviceEvent):
    formatted_event = (
      f"[{self._timestamp()}] "
      f"{event.event_type} {dict(event.data)}"
    )

    with self._lock:
      self._events.append(formatted_event)

  def _write_line(self, x, y, width, text):
    line = str(text)[:width].ljust(width)
    written = wintypes.DWORD()

    if not kernel32.WriteConsoleOutputCharacterW(
      self._handle,
      line,
      len(line),
      COORD(x, y),
      ctypes.byref(written),
    ):
      raise ctypes.WinError(ctypes.get_last_error())

  @staticmethod
  def _format_state(state):
    values = [
      ("D", int(state.dongle_connected)),
      ("USB", int(state.usb_connected)),
      (
        "H",
        "on" if state.headset_on == "on"
        else "off" if state.headset_on == "off"
        else "?",
      ),
      ("BAT_L", state.battery_level),
      ("BAT_S", state.battery_status),
      ("MD_G", state.mixdial_game),
      ("MD_C", state.mixdial_chat),
    ]

    return "  ".join(
      f"{name}: {value}"
      for name, value in values
    )

  def _build_state_box(self, state, width):
    content = self._format_state(state)

    if width < 4:
      return [content[:width]]

    inner_width = width - 2

    return [
      "┌" + "─" * inner_width + "┐",
      "│" + " STATE".ljust(inner_width) + "│",
      "│" + f" {content}"[:inner_width].ljust(inner_width) + "│",
      "└" + "─" * inner_width + "┘",
    ]

  def initialize(self):
    info = self._get_console_info()

    width = min(
      info.srWindow.Right - info.srWindow.Left + 1,
      MAX_WIDTH,
    )

    for index in range(
      info.srWindow.Bottom - info.srWindow.Top + 1
    ):
      self._write_line(
        info.srWindow.Left,
        info.srWindow.Top + index,
        width,
        "",
      )

  def render(self, state):
    info = self._get_console_info()

    left = info.srWindow.Left
    top = info.srWindow.Top

    width = min(
      info.srWindow.Right - left + 1,
      MAX_WIDTH,
    )

    height = info.srWindow.Bottom - top + 1

    with self._lock:
      events = list(self._events)
      logs = list(self._logs)

    lines = [
      "ARCTIS DEVICE MONITOR",
      *self._build_state_box(state, width),
      "",
      f"EVENTS (last {MAX_EVENTS})",
    ]

    available_event_lines = max(
      0,
      height - len(lines) - 2,
    )

    visible_events = events[-available_event_lines:]

    lines.extend(visible_events)

    lines.append("-" * width)

    remaining_lines = max(
      0,
      height - len(lines),
    )

    lines.extend(logs[-remaining_lines:])

    for index in range(height):
      line = lines[index] if index < len(lines) else ""

      self._write_line(
        left,
        top + index,
        width,
        line,
      )

    kernel32.SetConsoleCursorPosition(
      self._handle,
      COORD(left, top + height - 1),
    )


def configure_logging(monitor):
  root_logger = logging.getLogger()

  for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)
    handler.close()

  handler = MonitorLogHandler(monitor)
  handler.setFormatter(
    logging.Formatter(
      "%(levelname)s %(name)s: %(message)s"
    )
  )

  root_logger.addHandler(handler)
  root_logger.setLevel(logging.DEBUG)


def main():
  monitor = ArctisDeviceMonitor()
  configure_logging(monitor)

  device = ArctisDevice(
    "arctis_7_plus.yml",
    on_event=monitor.on_event,
  )

  try:
    monitor.initialize()

    while True:
      monitor.render(device.state)
      time.sleep(REFRESH_INTERVAL)

  except KeyboardInterrupt:
    pass

  finally:
    device.stop()
    print("\nArctisDevice stopped.")


if __name__ == "__main__":
  main()
