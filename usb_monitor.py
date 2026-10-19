import queue
import threading

import hid


# Identificación USB del receptor SteelSeries Arctis 7+.
USB_VENDOR_ID = 0x1038
USB_PRODUCT_ID = 0x220e

# Configuración de comunicación HID.
HID_REPORT_SIZE = 64
RADIO_INTERFACE_NUMBER = 3
USB_RECONNECT_INTERVAL_SECONDS = 2

# Comandos HID enviados al receptor para consultar su estado.
# Cada tupla contiene los bytes iniciales del comando.
HID_POLL_COMMANDS = (
  (0x00, 0x06, 0x18),
  (0x06, 0x18),
  (0x00, 0xB0),
  (0x02, 0x01),
)

# Patrón del informe que indica que los auriculares están encendidos.
HEADSET_ON_REPORT_PREFIX = (0xB0, 0x03, 0x04, 0x03)
HEADSET_ON_VALUE_BYTE_INDEX = 5
HEADSET_ON_VALUE = 0x64

# Patrón del informe que indica que los auriculares están apagados.
HEADSET_OFF_REPORT_PREFIX = (0xB0, 0x01, 0x04, 0x00)
HEADSET_OFF_MARKER = (0xFF, 0xFF)


class USBMonitor(threading.Thread):
  def __init__(self):
    super().__init__(name="ArctisUSBMonitor", daemon=True)
    self.events = queue.Queue()
    self.stop_event = threading.Event()
    self.state = "Desconocido"
    self.connected = False
    self.battery = None
    self.last_raw_report_hex = None

  def emit(self, event, value):
    self.events.put((event, value))

  def set_connection(self, connected):
    if self.connected == connected:
      return

    self.connected = connected
    self.emit("connection", connected)

    if not connected:
      self.battery = None
      self.set_state("Desconocido")

  def set_state(self, state):
    if self.state != state:
      self.state = state
      self.emit("headset", state)

  def set_battery(self, level):
    if level not in range(5):
      return

    battery_percentage = level * 25

    if self.battery != battery_percentage:
      self.battery = battery_percentage
      self.emit("battery", battery_percentage)

  def open_devices(self):
    devices = []

    device_infos = [
      device_info for device_info in hid.enumerate()
      if device_info["vendor_id"] == USB_VENDOR_ID
      and device_info["product_id"] == USB_PRODUCT_ID
    ]

    for device_info in device_infos:
      try:
        device = hid.device()
        device.open_path(device_info["path"])
        device.set_nonblocking(1)
        devices.append((device, device_info["interface_number"]))
      except Exception:
        try:
          device.close()
        except Exception:
          pass

    return devices

  def close_devices(self, devices):
    for device, _ in devices:
      try:
        device.close()
      except Exception:
        pass

  def poll_devices(self, devices):
    for device, interface_number in devices:
      if self.stop_event.is_set():
        return False

      for command_bytes in HID_POLL_COMMANDS:
        if self.stop_event.is_set():
          return False

        command_packet = list(command_bytes) + [0x00] * (
          HID_REPORT_SIZE - len(command_bytes)
        )

        try:
          if command_bytes[0] == 0x00:
            device.write(command_packet)
          else:
            device.write([0x00] + command_packet[:HID_REPORT_SIZE - 1])
        except Exception:
          return False

      if self.stop_event.wait(0.05):
        return False

      try:
        report_data = device.read(HID_REPORT_SIZE)
      except Exception:
        return False

      if (
        report_data
        and interface_number == RADIO_INTERFACE_NUMBER
        and len(report_data) > 1
      ):
        raw_report_hex = bytes(report_data).hex(" ")

        if raw_report_hex != self.last_raw_report_hex:
          self.last_raw_report_hex = raw_report_hex

        if (
          len(report_data) > HEADSET_ON_VALUE_BYTE_INDEX
          and tuple(report_data[:len(HEADSET_ON_REPORT_PREFIX)])
          == HEADSET_ON_REPORT_PREFIX
          and report_data[HEADSET_ON_VALUE_BYTE_INDEX] == HEADSET_ON_VALUE
        ):
          self.set_state("ENCENDIDO")
        elif (
          len(report_data) >= 6
          and tuple(report_data[:len(HEADSET_OFF_REPORT_PREFIX)])
          == HEADSET_OFF_REPORT_PREFIX
          and tuple(report_data[4:6]) == HEADSET_OFF_MARKER
        ):
          self.set_state("APAGADO")

        # El tercer byte del informe indica el nivel de batería, de 0 a 4.
        if (
          len(report_data) >= 4
          and report_data[0] == 0xB0
          and report_data[1] == 0x03
          and report_data[2] in range(5)
        ):
          self.set_battery(report_data[2])

    return True

  def run(self):
    try:
      while not self.stop_event.is_set():
        devices = []

        try:
          devices = self.open_devices()
          radio_connected = any(
            interface_number == RADIO_INTERFACE_NUMBER
            for _, interface_number in devices
          )

          self.set_connection(radio_connected)

          if not radio_connected:
            self.set_state("Desconocido")
            continue

          while devices and not self.stop_event.is_set():
            if not self.poll_devices(devices):
              break

            if self.stop_event.wait(1):
              break

        except Exception as exc:
          self.emit("error", f"Error en el monitor USB: {exc}")

        finally:
          self.close_devices(devices)

        if self.stop_event.is_set():
          break

        self.set_connection(False)
        self.set_state("Desconocido")
        self.stop_event.wait(USB_RECONNECT_INTERVAL_SECONDS)

    finally:
      self.set_connection(False)
      self.set_state("Desconocido")
      self.emit("stopped", True)

  def stop(self):
    self.stop_event.set()
