
import argparse
import queue
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from arctis_device import ArctisDevice
from device_profile import load_device_profile
from tray_ui import TrayController


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument(
    "--debug",
    action="store_true",
    help="Enable debug interface options",
  )
  args, qt_args = parser.parse_known_args()

  app = QApplication([sys.argv[0], *qt_args])
  app.setApplicationName("ArctisTray")
  app.setQuitOnLastWindowClosed(False)

  if not QSystemTrayIcon.isSystemTrayAvailable():
    print("System tray is not available.")
    return 1

  profile = load_device_profile("arctis_7_plus.yml")

  controller = TrayController(
    app,
    debug=args.debug,
  )

  pending_events = queue.Queue()

  def on_device_event(event):
    pending_events.put(event)

  device = ArctisDevice(
    "arctis_7_plus.yml",
    on_event=on_device_event,
  )

  controller.panel.update_device_state(
    profile.name,
    device.state,
  )

  def refresh_device_state():
    changed = False

    while True:
      try:
        pending_events.get_nowait()
        changed = True
      except queue.Empty:
        break

    if changed:
      controller.panel.update_device_state(
        profile.name,
        device.state,
      )

  update_timer = QTimer(app)
  update_timer.timeout.connect(refresh_device_state)
  update_timer.start(50)

  app.aboutToQuit.connect(device.stop)

  interrupt_requested = [False]

  def handle_sigint(_signum, _frame):
    interrupt_requested[0] = True

  previous_handler = signal.signal(
    signal.SIGINT,
    handle_sigint,
  )

  interrupt_timer = QTimer()
  interrupt_timer.timeout.connect(
    lambda: app.quit()
    if interrupt_requested[0]
    else None
  )
  interrupt_timer.start(200)

  try:
    return app.exec()
  finally:
    interrupt_timer.stop()
    signal.signal(signal.SIGINT, previous_handler)


if __name__ == "__main__":
  sys.exit(main())
