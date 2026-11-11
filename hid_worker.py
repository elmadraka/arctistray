import time
import logging
import os
import traceback
from dataclasses import dataclass
from enum import Enum, auto
from queue import Queue
from threading import Condition, Event, Thread
from typing import Any, Mapping

from hid_parser import HIDParser


logger = logging.getLogger(__name__)

READ_TIMEOUT_MS = 50


class WorkerMessageType(Enum):
  READY = auto()
  MESSAGE = auto()
  ERROR = auto()
  TERMINATED = auto()


@dataclass(frozen=True, slots=True)
class WorkerMessage:
  worker_id: int
  interface_id: int
  message_type: WorkerMessageType
  payload: Mapping[str, Any] | None = None


class HIDWorker(Thread):
  def __init__(
    self,
    worker_id: int,
    interface_config,
    device_path: bytes,
    message_queue: Queue,
  ):
    super().__init__(
      name=f"HIDWorker-{interface_config.interface_id}-{worker_id}",
      daemon=True,
    )

    self.worker_id = worker_id
    self.interface_config = interface_config
    self.interface_id = interface_config.interface_id
    self.device_path = device_path
    self.message_queue = message_queue
    self._last_command_at = None

    self._stop_event = Event()
    self._polling_condition = Condition()

    self._fast_polling_interval = None
    self._fast_polling_until = None

    self._parser = None
    self._transport = None
    self._ready_sent = False

  def stop(self):
    self._stop_event.set()

    with self._polling_condition:
      self._polling_condition.notify_all()

  def start_fast_polling(
    self,
    interval: float,
    duration: float,
  ):
    if self.interface_config.mode != "polling":
      return

    if interval <= 0 or duration <= 0:
      raise ValueError(
        "Polling interval and duration must be positive"
      )

    with self._polling_condition:
      self._fast_polling_interval = interval
      self._fast_polling_until = (
        time.monotonic() + duration
      )

      self._polling_condition.notify_all()

  def stop_fast_polling(self):
    with self._polling_condition:
      self._fast_polling_until = 0.0
      self._fast_polling_interval = None

      self._polling_condition.notify_all()

  def _get_polling_interval(self):
    polling_config = self.interface_config.config["polling"]
    normal_interval = polling_config["refresh_interval"]

    min_interval = (
      self._get_polling_command().get(
        "min_command_interval_ms",
        0,
      ) / 1000
    )

    with self._polling_condition:
      interval = normal_interval

      if self._fast_polling_until is not None:
        if time.monotonic() < self._fast_polling_until:
          interval = self._fast_polling_interval
        else:
          self._fast_polling_interval = None
          self._fast_polling_until = None

    return max(interval, min_interval)

  def _send_message(
    self,
    message_type: WorkerMessageType,
    payload: Mapping[str, Any] | None = None,
  ):
    self.message_queue.put(
      WorkerMessage(
        worker_id=self.worker_id,
        interface_id=self.interface_id,
        message_type=message_type,
        payload=payload,
      )
    )

  def _get_polling_command(self):
    commands = self.interface_config.config["polling"]["commands"]

    try:
      return commands["status"]
    except KeyError as exc:
      raise ValueError(
        "Polling interface requires a 'status' command"
      ) from exc

  def _initialize_parser(self):
    response_config = None

    if self.interface_config.mode == "polling":
      command = self._get_polling_command()
      response_config = command["response"]

    self._parser = HIDParser(
      self.interface_config,
      response_config=response_config,
    )

  def _create_transport(self):
    if os.name != "nt":
      raise OSError(
        f"No HID transport available for platform: {os.name}"
      )

    from windows_hid_transport import WindowsHIDTransport

    output_config = None

    if self.interface_config.mode == "polling":
      command = self._get_polling_command()

      if command["transport"] != "ctrl_output":
        raise ValueError(
          f"Unsupported polling transport: {command['transport']}"
        )

      output_config = command["request"]

    return WindowsHIDTransport(
      device_path=self.device_path,
      input_report_size=self.interface_config.config["report_size"],
      output_config=output_config,
    )

  def _initialize_transport(self):
    self._transport = self._create_transport()
    self._transport.open()

  def _parse_report(self, report):
    return self._parser.parse(report)

  def _process_report(self, report):
    result = self._parse_report(report)

    if result:
      self._send_message(
        WorkerMessageType.MESSAGE,
        result,
      )

  def _send_ready(self, initial_payload=None):
    if self._ready_sent:
      raise RuntimeError("READY has already been sent")

    self._send_message(
      WorkerMessageType.READY,
      initial_payload,
    )

    self._ready_sent = True

  def _initialize_communication(self):
    if self.interface_config.mode == "polling":
      request = self._get_polling_command()["request"]

      self._transport.write(request["payload"])
      self._last_command_at = time.monotonic()

      report = self._transport.read(READ_TIMEOUT_MS)

      if report is None:
        raise TimeoutError("Initial polling command received no response")

      result = self._parse_report(report) or None

      return result

    report = self._transport.read(READ_TIMEOUT_MS)

    if report is None:
      return None

    return self._parse_report(report) or None

  def _run_polling(self):
    command = self._get_polling_command()
    request = command["request"]

    while not self._stop_event.is_set():
      with self._polling_condition:
        interval = self._get_polling_interval()

        elapsed = time.monotonic() - self._last_command_at
        remaining = max(0, interval - elapsed)

        if remaining > 0:
          self._polling_condition.wait(remaining)
          continue

      if self._stop_event.is_set():
        return

      self._transport.write(request["payload"])
      self._last_command_at = time.monotonic()

      report = self._transport.read(READ_TIMEOUT_MS)

      if report is None:
        raise TimeoutError("Polling command received no response")

      self._process_report(report)

  def _run_passive(self):
    while not self._stop_event.is_set():
      report = self._transport.read(READ_TIMEOUT_MS)

      if report is not None:
        self._process_report(report)

  def _run(self):
    if self.interface_config.mode not in {"polling", "passive"}:
      raise ValueError(
        f"Unsupported interface mode: {self.interface_config.mode}"
      )

    self._initialize_parser()
    self._initialize_transport()

    initial_payload = self._initialize_communication()
    self._send_ready(initial_payload)

    if self._stop_event.is_set():
      return

    if self.interface_config.mode == "polling":
      self._run_polling()
    else:
      self._run_passive()

  def run(self):
    try:
      self._run()

    except Exception as exc:
      logger.exception(
        "Unexpected error in HID worker %s (interface %s)",
        self.worker_id,
        self.interface_id,
      )

      self._send_message(
        WorkerMessageType.ERROR,
        {
          "error": str(exc),
          "exception_type": type(exc).__name__,
          "traceback": traceback.format_exc(),
        },
      )

    finally:
      try:
        if self._transport is not None:
          self._transport.close()

      except Exception as exc:
        logger.exception(
          "Failed to close transport for HID worker %s",
          self.worker_id,
        )

        self._send_message(
          WorkerMessageType.ERROR,
          {
            "error": str(exc),
            "exception_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
          },
        )

      finally:
        self._send_message(WorkerMessageType.TERMINATED)