import threading
import hid


class ArctisDevice(threading.Thread):
  def __init__(self, protocol):
    super().__init__(daemon=True)

    self.protocol = protocol
    self._state_lock = threading.RLock()
    self._listeners = {}
    self._last_raw_report_hex = None
    self._stop_event = threading.Event()

    self.headset_dongle_connected = None
    self.headset_connected = None
    self.headset_active = None
    self.battery = None

    self._last_radio_status = None

  # ------------------------------------------------------------------
  # Event handling
  # ------------------------------------------------------------------

  def add_listener(self, event, callback):
    with self._state_lock:
      listeners = self._listeners.setdefault(event, [])

      if callback not in listeners:
        listeners.append(callback)

  def remove_listener(self, event, callback):
    with self._state_lock:
      listeners = self._listeners.get(event, [])

      if callback in listeners:
        listeners.remove(callback)

  def _emit(self, event, **data):
    with self._state_lock:
      listeners = tuple(self._listeners.get(event, []))

    for callback in listeners:
      try:
        callback(self, **data)
      except Exception as exc:
        print(f"[DEVICE] Error in '{event}' listener: {exc}")

  def _state_changed(self, **changes):
    self._emit("state_changed", changes=changes)

  # ------------------------------------------------------------------
  # Public device state
  # ------------------------------------------------------------------

  def get_state(self):
    """Return a consistent snapshot of the current device state."""
    with self._state_lock:
      return {
        "headset_dongle_connected": self.headset_dongle_connected,
        "headset_connected": self.headset_connected,
        "headset_active": self.headset_active,
        "battery": self.battery,
      }

  def set_dongle_connected(self, connected):
    if connected not in (True, False, None):
      raise ValueError("Invalid dongle connection state.")

    with self._state_lock:
      if self.headset_dongle_connected is connected:
        return

      previous_dongle_state = self.headset_dongle_connected
      self.headset_dongle_connected = connected
      events = []

      if connected is True:
        self.headset_connected = False
        self.headset_active = None
        self.battery = None

        if previous_dongle_state is not True:
          events.append("headset_dongle_connected")

      else:
        self.headset_connected = None
        self.headset_active = None
        self.battery = None

        if connected is False:
          events.append("headset_dongle_disconnected")

      changes = {
        "headset_dongle_connected": self.headset_dongle_connected,
        "headset_connected": self.headset_connected,
        "headset_active": self.headset_active,
        "battery": self.battery,
      }

    for event in events:
      self._emit(event)

    self._state_changed(**changes)

  def set_headset_connected(self, connected):
    if connected not in (True, False):
      raise ValueError("Headset connection state must be True or False.")

    with self._state_lock:
      if self.headset_dongle_connected is not True:
        return

      if self.headset_connected is connected:
        return

      previous_connected = self.headset_connected
      self.headset_connected = connected
      self.headset_active = None
      events = []

      if connected:
        events.append("headset_connected")
        message = "[HEADSET] Headset connected."

      else:
        if previous_connected is True:
          events.append("headset_disconnected")
        message = "[HEADSET] Headset disconnected."

      changes = {
        "headset_connected": self.headset_connected,
        "headset_active": self.headset_active,
      }
    
    print(message)

    for event in events:
      self._emit(event)

    self._state_changed(**changes)

  def set_headset_active(self, active):
    if active not in (True, False, None):
      raise ValueError("Invalid headset active state.")

    with self._state_lock:
      if (
        self.headset_dongle_connected is not True
        or self.headset_connected is not True
      ):
        active = None

      if self.headset_active is active:
        return

      previous_active = self.headset_active
      self.headset_active = active

    if active is True:
      self._emit("headset_activated")
    elif active is False and previous_active is True:
      self._emit("headset_deactivated")

    self._state_changed(headset_active=active)

  def set_battery(self, percentage):
    if percentage not in (0, 25, 50, 75, 100):
      return

    with self._state_lock:
      if self.headset_dongle_connected is not True:
        return

      if self.battery == percentage:
        return

      self.battery = percentage

    self._state_changed(battery=percentage)

  # ------------------------------------------------------------------
  # Shared HID interface management
  # ------------------------------------------------------------------

  def _open_devices(self):
    protocol = self.protocol
    devices = []

    device_infos = [
      info for info in hid.enumerate()
      if info["vendor_id"] == protocol.USB_VENDOR_ID
      and info["product_id"] == protocol.USB_PRODUCT_ID
      and info["interface_number"] == protocol.RADIO_INTERFACE_NUMBER
    ]

    for info in device_infos:
      if self._stop_event.is_set():
        break

      device = None

      try:
        device = hid.device()
        device.open_path(info["path"])
        device.set_nonblocking(1)
        devices.append(device)

      except Exception as exc:
        if device is not None:
          try:
            device.close()
          except Exception:
            pass

        self._close_devices(devices)
        raise RuntimeError(
          f"Failed to open radio HID interface: {exc}"
        ) from exc

    return devices

  def _close_devices(self, devices):
    for device in devices:
      try:
        device.close()
      except Exception:
        pass

  # ------------------------------------------------------------------
  # Shared HID polling
  # ------------------------------------------------------------------

  def _send_poll_commands(self, device):
    protocol = self.protocol

    for command_bytes, prepend_report_id in protocol.HID_POLL_COMMANDS:
      if self._stop_event.is_set():
        return False

      if len(command_bytes) > protocol.HID_REPORT_SIZE:
        self._emit(
          "error",
          message="HID command exceeds the configured report size.",
        )
        return False

      packet = list(command_bytes)
      packet.extend(
        [0x00] * (protocol.HID_REPORT_SIZE - len(packet))
      )

      if prepend_report_id:
        packet = [0x00] + packet[:protocol.HID_REPORT_SIZE - 1]

      try:
        device.write(packet)
      except Exception as exc:
        if self._stop_event.is_set():
          return False

        self._emit(
          "error",
          message=f"Failed to send HID command: {exc}",
        )
        return False

    return True

  def _process_report(self, report_data):
    protocol = self.protocol

    if not report_data or len(report_data) <= 1:
      return

    raw_report_hex = bytes(report_data).hex(" ")

    if raw_report_hex != self._last_raw_report_hex:
      self._last_raw_report_hex = raw_report_hex

    on_prefix = protocol.HEADSET_ON_REPORT_PREFIX
    on_index = protocol.HEADSET_ON_VALUE_BYTE_INDEX

    if (
      len(report_data) > on_index
      and tuple(report_data[:len(on_prefix)]) == on_prefix
      and report_data[on_index] == protocol.HEADSET_ON_VALUE
    ):
      self.set_headset_connected(True)

    off_prefix = protocol.HEADSET_OFF_REPORT_PREFIX
    marker_index = protocol.HEADSET_OFF_MARKER_INDEX
    marker = protocol.HEADSET_OFF_MARKER

    if (
      len(report_data) >= marker_index + len(marker)
      and tuple(report_data[:len(off_prefix)]) == off_prefix
      and tuple(
        report_data[marker_index:marker_index + len(marker)]
      ) == marker
    ):
      self.set_headset_connected(False)

    # Battery parsing is temporarily disabled until the report format
    # has been verified. Keep this code for future reactivation.
    #
    # battery_prefix = protocol.BATTERY_REPORT_PREFIX
    # battery_index = protocol.BATTERY_VALUE_BYTE_INDEX
    #
    # if (
    #   len(report_data) > battery_index
    #   and tuple(report_data[:len(battery_prefix)]) == battery_prefix
    # ):
    #   battery_value = protocol.BATTERY_VALUE_MAP.get(
    #     report_data[battery_index]
    #   )
    #
    #   if battery_value is not None:
    #     self.set_battery(battery_value)

  def _poll_devices(self, devices):
    protocol = self.protocol

    for device in devices:
      if self._stop_event.is_set():
        return False

      if not self._send_poll_commands(device):
        return False

      if self._stop_event.wait(
        protocol.HID_COMMAND_INTERVAL_SECONDS
      ):
        return False

      if self._stop_event.is_set():
        return False

      try:
        report_data = device.read(protocol.HID_REPORT_SIZE)
      except Exception as exc:
        if self._stop_event.is_set():
          return False

        self._emit(
          "error",
          message=f"Failed to read HID report: {exc}",
        )
        return False

      if self._stop_event.is_set():
        return False

      self._process_report(report_data)

    return True

  # ------------------------------------------------------------------
  # Thread lifecycle and reconnection
  # ------------------------------------------------------------------

  def stop(self):
    """Request a graceful stop without changing the reported device state."""
    self._stop_event.set()

  def run(self):
    protocol = self.protocol

    try:
      while not self._stop_event.is_set():
        devices = []

        try:
          devices = self._open_devices()

          if self._stop_event.is_set():
            break

          radio_connected = bool(devices)
          radio_status = (len(devices), radio_connected)

          if radio_status != self._last_radio_status:
            if radio_connected:
              print(
                f"[HID] Interfaces abiertas: {len(devices)}; "
                "receptor detectado: True"
              )
            else:
              print(
                "[HID] Buscando interfaces de radio... "
                "Interfaces abiertas: 0; receptor detectado: False"
              )

            self._last_radio_status = radio_status

          self.set_dongle_connected(radio_connected)

          if radio_connected:
            while not self._stop_event.is_set():
              if not self._poll_devices(devices):
                break

              if self._stop_event.wait(
                protocol.HID_POLL_INTERVAL_SECONDS
              ):
                break

        except Exception as exc:
          if not self._stop_event.is_set():
            self._emit(
              "error",
              message=f"Error in HID monitor: {exc}",
            )

        finally:
          self._close_devices(devices)

        if self._stop_event.wait(
          protocol.USB_RECONNECT_INTERVAL_SECONDS
        ):
          break

    finally:
      self._emit("stopped")