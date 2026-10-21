import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audio_discovery import AudioDiscovery


EVENT_NAMES = (
  "monitoring_started",
  "monitoring_stopped",
  "refresh_started",
  "refresh_finished",
  "default_device_changed",
  "endpoint_added",
  "endpoint_removed",
)


def make_event_printer(event_name):
  def print_event(*args, **kwargs):
    print(f"\n[EVENT] {event_name}")

    if event_name == "refresh_started":
      print("  Audio catalog refresh started.")

    elif event_name == "refresh_finished":
      status = kwargs.get("status", "unknown")
      error = kwargs.get("error")
      catalog = kwargs.get("catalog")

      print(f"  Status: {status}")

      if error:
        print(f"  Error: {error}")

      if catalog is not None:
        devices = catalog.get("devices", [])
        print(f"  Device count: {len(devices)}")

      headsets = [
          device for device in devices
          if device.get("kind") == "headset"
      ]

      for device in headsets:
          print(
              "[HEADSET] "
              f"name={device['name']!r}, "
              f"flow={device['flow']}, "
              f"available={device['available']}, "
              f"id={device['id']}"
          )

    elif event_name == "default_device_changed":
      print(f"  Flow: {kwargs.get('flow')}")
      print(f"  Role: {kwargs.get('role')}")
      print(f"  Endpoint: {kwargs.get('endpoint_id')}")

    elif event_name in {
      "endpoint_added",
      "endpoint_removed",
      "endpoint_state_changed",
      "endpoint_property_changed",
    }:
      print(f"  Endpoint: {kwargs.get('endpoint_id')}")
      if "state" in kwargs:
        print(f"  State: {kwargs['state']}")

    elif event_name == "discovery_error":
      print(f"  Error: {kwargs.get('error')}")

  return print_event


def main():
  discovery = AudioDiscovery()
  listeners = {
    event_name: make_event_printer(event_name)
    for event_name in EVENT_NAMES
  }

  for event_name, callback in listeners.items():
    discovery.add_listener(event_name, callback)

  try:
    print("[START] Starting Windows Audio monitor...")
    discovery.start()

    catalog = discovery.get_catalog(refresh=True)
    print(f"[CATALOG] Initial device count: {len(catalog['devices'])}")

    print(
      "[ACTION] Change default input/output devices and reconnect "
      "the headset during the next 90 seconds."
    )
    print("[MONITOR] Waiting for events...")

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
      time.sleep(0.5)

    catalog = discovery.get_catalog(refresh=True)
    devices = catalog["devices"]

    print(f"[INFO] Initial device count: {len(devices)}")

    headsets = [device for device in devices if device["kind"] == "headset"]
    if headsets:
      for device in headsets:
        print(
          "[HEADSET] "
          f"name={device['name']!r}, "
          f"flow={device['flow']}, "
          f"available={device['available']}, "
          f"id={device['id']}"
        )
    else:
      print("[HEADSET] No endpoints currently classified as headset.")

  except KeyboardInterrupt:
    print("\n[STOP] Interrupted by user.")

  except Exception as exc:
    print(f"\n[ERROR] Smoke test failed: {exc}")

  finally:
    print("[STOP] Stopping Windows Audio monitor...")
    discovery.stop()

    for event_name, callback in listeners.items():
      discovery.remove_listener(event_name, callback)

    print("[DONE] Manual smoke test finished.")


if __name__ == "__main__":
  main()
