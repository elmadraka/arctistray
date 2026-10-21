
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from arctis_device import ArctisDevice
from devices import arctis_7_plus as protocol


def create_device():
  device = ArctisDevice(protocol)
  device.set_headset_connected = lambda connected: setattr(
    device, "headset_connected", connected
  )
  device.headset_connected = False
  return device


def test_headset_on_report():
  device = create_device()
  report = bytes.fromhex(
    "b0 03 03 03 00 64"
  )

  device._process_report(report)

  assert device.headset_connected is True


def test_headset_off_report():
  device = create_device()
  device.headset_connected = True
  report = bytes.fromhex(
    "b0 01 03 00 ff ff"
  )

  device._process_report(report)

  assert device.headset_connected is False


def test_unrelated_report_is_ignored():
  device = create_device()
  report = bytes.fromhex(
    "b0 02 03 03 00 64"
  )

  device._process_report(report)

  assert device.headset_connected is False


def test_headset_on_report_with_alternate_prefix():
  device = create_device()
  report = bytes.fromhex(
    "b0 03 04 03 00 64"
  )

  device._process_report(report)

  assert device.headset_connected is True


def test_headset_off_report_with_alternate_prefix():
  device = create_device()
  device.headset_connected = True
  report = bytes.fromhex(
    "b0 01 04 00 ff ff"
  )

  device._process_report(report)

  assert device.headset_connected is False


def test_short_report_is_ignored():
  device = create_device()
  report = bytes.fromhex("b0 03")

  device._process_report(report)

  assert device.headset_connected is False


def test_on_report_with_wrong_value_is_ignored():
  device = create_device()
  report = bytes.fromhex(
    "b0 03 03 03 00 00"
  )

  device._process_report(report)

  assert device.headset_connected is False


def test_off_report_without_marker_is_ignored():
  device = create_device()
  device.headset_connected = True
  report = bytes.fromhex(
    "b0 01 03 00 00 00"
  )

  device._process_report(report)

  assert device.headset_connected is True


if __name__ == "__main__":
  tests = [
    test_headset_on_report,
    test_headset_off_report,
    test_unrelated_report_is_ignored,
    test_headset_on_report_with_alternate_prefix,
    test_headset_off_report_with_alternate_prefix,
    test_short_report_is_ignored,
    test_on_report_with_wrong_value_is_ignored,
    test_off_report_without_marker_is_ignored,
  ]

  for test in tests:
    test()
    print(f"[PASS] {test.__name__}")

  print("[PASS] All parser tests passed")
