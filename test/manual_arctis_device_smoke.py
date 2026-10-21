
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import hid

from arctis_device import ArctisDevice

# Replace this import with the same protocol import used by tray_app.py.
from devices import arctis_7_plus as protocol


def print_event(event_name):
  def callback(device, **data):
    print(f"\n[EVENT] {event_name}: {data}")
  return callback


def run_parser_tests():
  """Test headset state detection using known HID reports."""
  test_device = ArctisDevice(protocol)
  detected_states = []

  def record_headset_state(is_connected):
    detected_states.append(is_connected)

  test_device.set_headset_connected = record_headset_state

  # Observed HID report while the headset is powered on.
  headset_on_report = bytes(
    (0xB0, 0x03, 0x03, 0x03, 0x00, 0x64)
  ) + bytes(58)

  test_device._process_report(headset_on_report)

  assert detected_states == [True], (
    f"Expected headset connected, got {detected_states}"
  )
  print("[PASS] Headset-on report detected")

  detected_states.clear()

  # Observed HID report while the headset is powered off.
  headset_off_report = bytes(
    (0xB0, 0x01, 0x03, 0x00, 0xFF, 0xFF)
  ) + bytes(58)

  test_device._process_report(headset_off_report)

  assert detected_states == [False], (
    f"Expected headset disconnected, got {detected_states}"
  )
  print("[PASS] Headset-off report detected")

  detected_states.clear()

  # An unrelated report must not change the headset state.
  unrelated_report = bytes(
    (0xB0, 0x02, 0x03, 0x00, 0x00, 0x00)
  ) + bytes(58)

  test_device._process_report(unrelated_report)

  assert detected_states == [], (
    f"Unexpected state change: {detected_states}"
  )
  print("[PASS] Unrelated report ignored")

  print("[PASS] All parser tests passed")



def main():
  run_parser_tests()

  device = ArctisDevice(protocol)

  # Keep the rest of your existing main() here.


  for event_name in (
    "headset_dongle_connected",
    "headset_dongle_disconnected",
    "headset_connected",
    "headset_disconnected",
    "state_changed",
    "error",
  ):
    device.add_listener(event_name, print_event(event_name))

  original_process_report = device._process_report

  def trace_report(report_data):
    if report_data:
      print(f"[HID RAW] {bytes(report_data).hex(' ')}")
    original_process_report(report_data)

  device._process_report = trace_report

  try:
    print("[START] ArctisDevice HID monitor")
    device.start()

    print("[ACTION] Keep the USB receiver connected.")
    print("[ACTION] Turn the headset off and on several times.")
    print("[MONITOR] Press Ctrl+C to stop.")

    while True:
      time.sleep(0.5)

  except KeyboardInterrupt:
    print("\n[STOP] Interrupted by user.")

  finally:
    device.stop()
    device.join(timeout=5)
    print("[DONE] HID test finished.")


if __name__ == "__main__":
  main()
