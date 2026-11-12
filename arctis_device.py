from __future__ import annotations

import logging
import queue
import threading
import time

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Mapping

from connection_manager import ConnectionManager
from device_profile import load_device_profile


logger = logging.getLogger(__name__)

_STOP_SENTINEL = object()


@dataclass(frozen=True, slots=True)
class DeviceState:
  dongle_connected: bool = False
  headset_on: str | None = None
  usb_connected: bool = False
  battery_level: int | None = None
  battery_status: str | None = None
  mixdial_game: int | None = None
  mixdial_chat: int | None = None
  pnp_device: PnPDeviceState | None = None

@dataclass(frozen=True, slots=True)
class PnPDeviceState:
  audio_render_instance_id: str | None = None
  audio_capture_instance_id: str | None = None

@dataclass(frozen=True, slots=True)
class DeviceEvent:
  event_type: str
  data: Mapping[str, Any]
  state: DeviceState

class ArctisDevice:
  def __init__(
    self,
    profile: str,
    on_event: Callable[[DeviceEvent], None] | None = None,
  ):
    self._config = load_device_profile(profile)
    self._message_queue = queue.Queue()
    self._on_event = on_event
    self._listeners = []
    self._listeners_lock = threading.RLock()
    self._state = DeviceState()
    self._mixdial_debounce = 0.1
    self._pending_mixdial: dict[str, tuple[int, float]] = {}
    self._reported_battery_status = None
    self._stop_lock = threading.Lock()
    self._stopped = False

    self._consumer_thread = threading.Thread(
      target=self._consume_messages,
      name="ArctisDeviceConsumer",
      daemon=True,
    )

    self._consumer_thread.start()

    try:
      self._connection_manager = ConnectionManager(
        self._config,
        self._message_queue,
      )
      self._connection_manager.start()
    except Exception:
      self._message_queue.put(_STOP_SENTINEL)
      self._consumer_thread.join()
      raise

  @property
  def state(self) -> DeviceState:
    return self._state

  def stop(self) -> None:
    with self._stop_lock:
      if self._stopped:
        return

      self._stopped = True

      try:
        self._connection_manager.stop()
      finally:
        self._message_queue.put(_STOP_SENTINEL)
        self._consumer_thread.join()

  def add_listener(self, callback: Callable[[DeviceEvent], None]) -> None:
    if not callable(callback):
      raise TypeError("callback must be callable")

    with self._listeners_lock:
      if callback not in self._listeners:
        self._listeners.append(callback)


  def remove_listener(self, callback: Callable[[DeviceEvent], None]) -> None:
    with self._listeners_lock:
      if callback in self._listeners:
        self._listeners.remove(callback)

  def _consume_messages(self) -> None:
    while True:
      timeout = self._next_mixdial_timeout()

      try:
        message = self._message_queue.get(timeout=timeout)
      except queue.Empty:
        self._flush_pending_mixdial()
        continue

      if message is _STOP_SENTINEL:
        return

      try:
        self._process_message(message)
      except Exception:
        logger.exception(
          "Failed to process Arctis device message"
        )

  def _next_mixdial_timeout(self) -> float | None:
    if not self._pending_mixdial:
      return None

    next_deadline = min(
      deadline
      for _, deadline in self._pending_mixdial.values()
    )

    return max(0.0, next_deadline - time.monotonic())

  def _flush_pending_mixdial(self) -> None:
    now = time.monotonic()

    updates = {
      field: value
      for field, (value, deadline) in self._pending_mixdial.items()
      if deadline <= now
    }

    if not updates:
      return

    for field in updates:
      self._pending_mixdial.pop(field)

    previous_state = self._state
    new_state = replace(previous_state, **updates)

    if new_state != previous_state:
      self._state = new_state
      self._emit_status_events(
        previous_state,
        new_state,
      )

  def _process_message(
    self,
    message: Mapping[str, Any],
  ) -> None:
    status = message.get("status", {})
    actions = message.get("actions", [])

    if not hasattr(self, "_pending_mixdial"):
      self._pending_mixdial = {}

    if not hasattr(self, "_mixdial_debounce"):
      self._mixdial_debounce = 0.1

    if not isinstance(status, Mapping):
      logger.warning("Ignoring invalid device status payload")
      status = {}

    if not isinstance(actions, (list, tuple)):
      logger.warning("Ignoring invalid device actions payload")
      actions = []

    normalized_status = dict(status)

    pnp_device_present = "pnp_device" in normalized_status
    pnp_device_value = normalized_status.pop(
      "pnp_device",
      None,
    )

    pnp_device_update = None

    if pnp_device_present:
      if pnp_device_value is None:
        pnp_device_update = None

      elif isinstance(pnp_device_value, PnPDeviceState):
        pnp_device_update = pnp_device_value

      elif isinstance(pnp_device_value, Mapping):
        fields = (
          "audio_render_instance_id",
          "audio_capture_instance_id",
        )

        values = {
          field: pnp_device_value.get(field)
          for field in fields
        }

        if all(
          value is None or isinstance(value, str)
          for value in values.values()
        ):
          pnp_device_update = PnPDeviceState(**values)
        else:
          logger.warning(
            "Ignoring invalid PnP device values: %r",
            pnp_device_value,
          )

      else:
        logger.warning(
          "Ignoring invalid PnP device value: %r",
          pnp_device_value,
        )

    if "headset_usb_connected" in normalized_status:
      physical_usb = normalized_status.pop(
        "headset_usb_connected"
      )

      if isinstance(physical_usb, bool):
        normalized_status["usb_connected"] = physical_usb
      else:
        logger.warning(
          "Ignoring invalid headset_usb_connected value: %r",
          physical_usb,
        )

    if "headset" in normalized_status:
      headset = normalized_status.pop("headset")

      if headset in ("on", "off"):
        normalized_status["headset_on"] = headset
      else:
        logger.warning(
          "Ignoring invalid headset value: %r",
          headset,
        )

    if "usb" in normalized_status:
      usb = normalized_status.pop("usb")

      if usb == "connected":
        normalized_status["usb_connected"] = True
      elif usb == "disconnected":
        normalized_status["usb_connected"] = False
      else:
        logger.warning(
          "Ignoring invalid usb value: %r",
          usb,
        )

    valid_fields = set(DeviceState.__dataclass_fields__)

    unknown_fields = normalized_status.keys() - valid_fields

    if unknown_fields:
      logger.warning(
        "Ignoring unknown device status fields: %s",
        sorted(unknown_fields),
      )

    updates = {
      field: value
      for field, value in normalized_status.items()
      if field in valid_fields
    }

    mixdial_updates = {}

    for field in ("mixdial_game", "mixdial_chat"):
      if field in updates:
        mixdial_updates[field] = updates.pop(field)

    previous_state = self._state

    usb_connected = updates.get(
      "usb_connected",
      previous_state.usb_connected,
    )

    dongle_connected = updates.get(
      "dongle_connected",
      previous_state.dongle_connected,
    )

    if pnp_device_present:
      if pnp_device_value is None:
        updates["pnp_device"] = None

      elif (
        pnp_device_update is not None
        and dongle_connected
      ):
        updates["pnp_device"] = pnp_device_update

    if usb_connected is True:
      mixdial_updates.clear()
      self._pending_mixdial.clear()


    new_state = replace(previous_state, **updates)

    # Headset information is unavailable without the wireless dongle.
    # USB-C connection remains independent and can still be detected.
    dongle_disconnected = (
      previous_state.dongle_connected
      and not new_state.dongle_connected
    )

    if dongle_disconnected:
      new_state = replace(
        new_state,
        headset_on=None,
        battery_level=None,
        battery_status=None,
        mixdial_game=None,
        mixdial_chat=None,
        pnp_device=None,
      )

    if dongle_disconnected:
      self._pending_mixdial.clear()
    else:
      now = time.monotonic()

      for field, value in mixdial_updates.items():
        if value == getattr(previous_state, field):
          self._pending_mixdial.pop(field, None)
        else:
          self._pending_mixdial[field] = (
            value,
            now + self._mixdial_debounce,
          )

    if "battery_status" in updates:
      self._reported_battery_status = updates["battery_status"]

    if updates.get("usb_connected") is False:
      self._reported_battery_status = None

    if dongle_disconnected:
      self._reported_battery_status = None

    new_state = replace(
      new_state,
      battery_status=self._resolve_battery_status(new_state),
    )

    if new_state != previous_state:
      self._state = new_state
      self._emit_status_events(
        previous_state,
        new_state,
      )

    for action in actions:
      if isinstance(action, str):
        self._emit_event(
          "button_pressed",
          {"button": action},
        )
      else:
        logger.warning(
          "Ignoring invalid device action: %r",
          action,
        )

  def _resolve_battery_status(
    self,
    state: DeviceState,
  ) -> str | None:
    reported_status = getattr(
      self,
      "_reported_battery_status",
      None,
    )

    if state.headset_on != "on":
      return None

    if reported_status == "charging":
      return "charging"

    if reported_status == "fully_charged":
      return "fully_charged"

    return "discharging"

  def _emit_status_events(
    self,
    previous: DeviceState,
    current: DeviceState,
  ) -> None:
    event_fields = (
      ("dongle_connected", "dongle_connection_changed"),
      ("headset_on", "headset_power_changed"),
      ("usb_connected", "usb_connection_changed"),
      ("battery_level", "battery_changed"),
      ("battery_status", "battery_status_changed"),
    )

    for field, event_type in event_fields:
      if getattr(previous, field) != getattr(current, field):
        self._emit_event(
          event_type,
          {field: getattr(current, field)},
        )

    if (
      previous.mixdial_game != current.mixdial_game
      or previous.mixdial_chat != current.mixdial_chat
    ):
      self._emit_event(
        "mixdial_changed",
        {
          "mixdial_game": current.mixdial_game,
          "mixdial_chat": current.mixdial_chat,
        },
      )

    previous_pnp = previous.pnp_device
    current_pnp = current.pnp_device

    if previous_pnp is None and current_pnp is not None:
      self._emit_event(
        "pnp_device_connected",
        {"pnp_device": current_pnp},
      )

    elif previous_pnp is not None and current_pnp is None:
      self._emit_event(
        "pnp_device_disconnected",
        {"pnp_device": None},
      )

    elif (
      previous_pnp is not None
      and current_pnp is not None
      and previous_pnp != current_pnp
    ):
      self._emit_event(
        "pnp_device_changed",
        {"pnp_device": current_pnp},
      )

  def _emit_event(
    self,
    event_type: str,
    data: Mapping[str, Any],
  ) -> None:
    event = DeviceEvent(
      event_type=event_type,
      data=MappingProxyType(dict(data)),
      state=self._state,
    )

    callbacks = []

    if self._on_event is not None:
      callbacks.append(self._on_event)

    with self._listeners_lock:
      callbacks.extend(self._listeners)

    for callback in callbacks:
      try:
        callback(event)
      except Exception:
        logger.exception(
          "Arctis device event callback failed: %s",
          event_type,
        )