from __future__ import annotations

import json
import msvcrt
import queue
import time
import logging

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from arctis_device import ArctisDevice


REFRESH_INTERVAL = 0.05

RESULTS_DIR = Path(__file__).parent / "physical_test_results"


@dataclass(frozen=True, slots=True)
class TestStep:
  name: str
  instruction: str
  expected: str
  condition: object

def configure_logging(log_path):
  handler = logging.FileHandler(
    log_path,
    encoding="utf-8",
  )
  handler.setFormatter(
    logging.Formatter(
      "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
    )
  )

  logging.basicConfig(
    level=logging.DEBUG,
    handlers=[handler],
    force=True,
  )

def format_state(state):
  headset = state.headset_on or "UNKNOWN"
  battery = (
    f"{state.battery_level}/4"
    if state.battery_level is not None
    else "UNKNOWN"
  )
  charging = state.battery_status or "UNKNOWN"
  game = (
    str(state.mixdial_game)
    if state.mixdial_game is not None
    else "?"
  )
  chat = (
    str(state.mixdial_chat)
    if state.mixdial_chat is not None
    else "?"
  )

  return (
    f"[DEVICE] Dongle: {'ON' if state.dongle_connected else 'OFF'}"
    f" | Headset: {headset}"
    f" | USB: {'ON' if state.usb_connected else 'OFF'}"
    f" | Battery: {battery}"
    f" | Charging: {charging}"
    f" | Game: {game}"
    f" | Chat: {chat}"
  )


def write_log(file, message):
  timestamp = datetime.now().astimezone().isoformat(
    timespec="milliseconds"
  )
  file.write(f"[{timestamp}] {message}\n")
  file.flush()


def create_steps():
  return [
    TestStep(
      name="Connect dongle",
      instruction="Connect the wireless dongle to the computer.",
      expected="dongle_connected == True",
      condition=lambda device, baseline, events: (
        device.state.dongle_connected is True
      ),
    ),
    TestStep(
      name="Power on headset",
      instruction="Turn on the headset.",
      expected='headset_on == "on"',
      condition=lambda device, baseline, events: (
        device.state.headset_on == "on"
      ),
    ),
    TestStep(
      name="Change mix dial",
      instruction="Move the game/chat mix dial.",
      expected="At least one mixdial value changes.",
      condition=lambda device, baseline, events: (
        device.state.mixdial_game != baseline.mixdial_game
        or device.state.mixdial_chat != baseline.mixdial_chat
      ),
    ),
    TestStep(
      name="Press media button",
      instruction="Press one media button on the headset.",
      expected='A "button_pressed" event is received.',
      condition=lambda device, baseline, events: any(
        event.event_type == "button_pressed"
        for event in events
      ),
    ),
    TestStep(
      name="Connect USB-C",
      instruction="Connect the headset directly using USB-C.",
      expected="usb_connected == True",
      condition=lambda device, baseline, events: (
        device.state.usb_connected is True
      ),
    ),
    TestStep(
      name="Disconnect USB-C",
      instruction="Disconnect the USB-C cable.",
      expected="usb_connected == False",
      condition=lambda device, baseline, events: (
        device.state.usb_connected is False
      ),
    ),
    TestStep(
      name="Power off headset",
      instruction="Turn off the headset.",
      expected='headset_on == "off"',
      condition=lambda device, baseline, events: (
        device.state.headset_on == "off"
      ),
    ),
    TestStep(
      name="Disconnect dongle",
      instruction="Remove the wireless dongle from the computer.",
      expected="dongle_connected == False",
      condition=lambda device, baseline, events: (
        device.state.dongle_connected is False
      ),
    ),
    TestStep(
      name="Reconnect dongle on another USB port",
      instruction="Connect the dongle to a different USB port.",
      expected="dongle_connected == True",
      condition=lambda device, baseline, events: (
        device.state.dongle_connected is True
      ),
    ),
    TestStep(
      name="Power on headset again",
      instruction="Turn on the headset again.",
      expected='headset_on == "on"',
      condition=lambda device, baseline, events: (
        device.state.headset_on == "on"
      ),
    ),
    TestStep(
      name="Disconnect dongle again",
      instruction="Remove the dongle once more.",
      expected="dongle_connected == False",
      condition=lambda device, baseline, events: (
        device.state.dongle_connected is False
      ),
    ),
    TestStep(
      name="Reconnect dongle again",
      instruction="Reconnect the dongle to the computer.",
      expected="dongle_connected == True",
      condition=lambda device, baseline, events: (
        device.state.dongle_connected is True
      ),
    ),
  ]



def drain_events(event_queue, event_history, log_file):
  while True:
    try:
      event = event_queue.get_nowait()
    except queue.Empty:
      return

    event_history.append(event)

    message = (
      "EVENT "
      + json.dumps(
        {
          "event_type": event.event_type,
          "data": dict(event.data),
        },
        default=str,
        ensure_ascii=False,
      )
    )

    write_log(log_file, message)
    print(f"\n[{datetime.now().astimezone().strftime('%H:%M:%S')}] {message}", flush=True)



def read_enter():
  if not msvcrt.kbhit():
    return False

  key = msvcrt.getwch()

  if key == "\x03":
    raise KeyboardInterrupt

  return key in ("\r", "\n")




def run_step(
  index,
  total,
  step,
  device,
  event_queue,
  event_history,
  log_file,
):
  baseline = device.state
  event_start = len(event_history)

  started_at = time.monotonic()
  last_state = None

  print()
  print(f"[TEST {index:02}/{total}] {step.name}")
  print(f"Action: {step.instruction}")
  print(f"Expected: {step.expected}")
  print("Waiting for the expected condition. Press ENTER to skip.")

  write_log(
    log_file,
    f"TEST START {index:02}/{total}: {step.name}",
  )
  write_log(log_file, f"EXPECTED: {step.expected}")
  write_log(log_file, f"BASELINE: {baseline!r}")

  while True:
    drain_events(event_queue, event_history, log_file)

    current_state = device.state

    if current_state != last_state:
      last_state = current_state
      write_log(log_file, f"STATE: {current_state!r}")

    elapsed = time.monotonic() - started_at

    line = (
      f"D:{int(current_state.dongle_connected)}"
      f" H:{current_state.headset_on or '?'}"
      f" U:{int(current_state.usb_connected)}"
      f" B:{current_state.battery_level if current_state.battery_level is not None else '?'}"
      f" C:{current_state.battery_status or '?'}"
      f" G:{current_state.mixdial_game if current_state.mixdial_game is not None else '?'}"
      f" Ch:{current_state.mixdial_chat if current_state.mixdial_chat is not None else '?'}"
      f" | TEST {index:02}/{total}"
      f" | {elapsed:.0f}s"
    )

    print("\x1b[2K\r" + line, end="", flush=True)

    if step.condition(
      device,
      baseline,
      event_history[event_start:],
    ):
      print()
      print(f"[PASS] {step.name} ({elapsed:.2f}s)")

      write_log(
        log_file,
        f"TEST PASS {index:02}/{total}: {step.name} "
        f"({elapsed:.2f}s)",
      )
      write_log(log_file, f"FINAL STATE: {device.state!r}")

      return "PASS"

    if read_enter():
      print()
      print(f"[MANUAL] Continuing without confirmation: {step.name}")

      write_log(
        log_file,
        f"TEST MANUAL {index:02}/{total}: {step.name}",
      )
      write_log(log_file, f"FINAL STATE: {device.state!r}")

      return "MANUAL"

    time.sleep(REFRESH_INTERVAL)


def main():
  RESULTS_DIR.mkdir(parents=True, exist_ok=True)

  timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
  log_path = RESULTS_DIR / f"arctis_test_{timestamp}.log"

  event_queue = queue.Queue()
  event_history = []
  results = []

  log_file = log_path.open(
    "w",
    encoding="utf-8",
    buffering=1,
  )

  configure_logging(log_path)

  device = None

  try:
    print("ArctisDevice physical test suite")
    print("=" * 40)
    print("Connect the dongle only when instructed.")
    print("Press ENTER to skip a test manually.")
    print("Press CTRL+C to stop the suite.")
    print()
    print(f"Diagnostic log: {log_path}")
    print()

    write_log(log_file, "PHYSICAL TEST SUITE STARTED")

    device = ArctisDevice(
      "arctis_7_plus.yml",
      on_event=event_queue.put,
    )

    write_log(log_file, f"INITIAL STATE: {device.state!r}")

    steps = create_steps()

    for index, step in enumerate(steps, start=1):
      result = run_step(
        index,
        len(steps),
        step,
        device,
        event_queue,
        event_history,
        log_file,
      )
      results.append((step.name, result))

    print()
    print("=" * 40)
    print("TEST SUMMARY")
    print("=" * 40)

    for name, result in results:
      print(f"[{result}] {name}")

    passed = sum(result == "PASS" for _, result in results)
    manual = sum(result == "MANUAL" for _, result in results)
    failed = sum(result == "FAIL" for _, result in results)

    print()
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Manual: {manual}")
    print(f"Total:  {len(results)}")

    write_log(log_file, "TEST SUMMARY")
    write_log(log_file, f"PASSED: {passed}")
    write_log(log_file, f"FAILED: {failed}")
    write_log(log_file, f"MANUAL: {manual}")
    write_log(log_file, f"TOTAL: {len(results)}")

  except KeyboardInterrupt:
    print()
    print("\nTest suite interrupted by user.")
    write_log(log_file, "TEST SUITE INTERRUPTED")

  except Exception as error:
    print()
    print(f"\nFATAL ERROR: {error!r}")
    write_log(log_file, f"FATAL ERROR: {error!r}")
    raise

  finally:
    if device is not None:
      try:
        device.stop()
        write_log(log_file, "DEVICE STOPPED")
      except Exception as error:
        write_log(log_file, f"DEVICE STOP ERROR: {error!r}")
        print(f"Error stopping device: {error!r}")

    write_log(log_file, "PHYSICAL TEST SUITE FINISHED")
    logging.shutdown()
    log_file.close()

    print()
    print(f"Diagnostic log saved to:\n{log_path}")


if __name__ == "__main__":
  main()
