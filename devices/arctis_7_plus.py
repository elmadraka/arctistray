# devices/arctis_7_plus.py

# USB identifiers for the SteelSeries Arctis 7+ wireless receiver.
USB_VENDOR_ID = 0x1038
USB_PRODUCT_ID = 0x220e

# HID communication settings.
HID_REPORT_SIZE = 64
RADIO_INTERFACE_NUMBER = 3
USB_RECONNECT_INTERVAL_SECONDS = 2
HID_COMMAND_INTERVAL_SECONDS = 0.05
HID_POLL_INTERVAL_SECONDS = 1

# HID commands sent periodically to query the receiver's status.
# Each entry specifies the command bytes and whether the HID report ID
# must be prepended to the command.
# Format: (command_bytes, include_report_id).
HID_POLL_COMMANDS = (
  ((0x00, 0x06, 0x18), False),
  ((0x06, 0x18), True),
  ((0x00, 0xB0), False),
  ((0x02, 0x01), True),
)

# Signature used to identify a report indicating that the headset is powered on.
HEADSET_ON_REPORT_PREFIX = (0xB0, 0x03, 0x04, 0x03)
HEADSET_ON_VALUE_BYTE_INDEX = 5
HEADSET_ON_VALUE = 0x64

# Signature and marker used to identify a report indicating that the headset is powered off.
HEADSET_OFF_REPORT_PREFIX = (0xB0, 0x01, 0x04, 0x00)
HEADSET_OFF_MARKER_INDEX = 4
HEADSET_OFF_MARKER = (0xFF, 0xFF)

# Battery status report parsing.
# The receiver reports the battery level as a discrete value that must
# be mapped to a percentage.
BATTERY_REPORT_PREFIX = (0xB0, 0x03)
BATTERY_VALUE_BYTE_INDEX = 2
BATTERY_VALUE_MAP = {
  0: 0,
  1: 25,
  2: 50,
  3: 75,
  4: 100,
}
