import threading
import pystray

from audio_controller import AudioController
from config_manager import ConfigManager
from device_factory import create_device
from tray_ui import TrayUI
from startup_manager import (
  is_startup_enabled,
  set_startup_enabled
)

class ArctisTray:
  def __init__(self):
    self.config = ConfigManager()

    try:
      startup_enabled = is_startup_enabled()

      if self.config.get("startup_enabled") != startup_enabled:
        self.config.set_startup_enabled(startup_enabled)
    except Exception as exc:
      print(f"[STARTUP] Windows registry read error: {exc}")

    self.device = create_device()
    self.audio = AudioController(self.config)
    self.audio.bind_device(self.device)

    self.icon = None
    self.closing = False
    self.outputs_status = "initializing"

    self._shutdown_lock = threading.Lock()
    self._shutdown_started = False
    self._shutdown_thread = None

    self.device.add_listener("state_changed", self._on_state_changed)
    self.device.add_listener("error", self._on_device_error)
    self.device.add_listener("headset_dongle_connected", self._on_dongle_connected)
    self.device.add_listener("headset_dongle_disconnected", self._on_dongle_disconnected)

    self.ui = TrayUI(
      get_headset_state=self.get_headset_state,
      get_receiver_connected=lambda: self.device.headset_dongle_connected is True,
      get_audio_connected=lambda: self.device.headset_active is True,
      get_outputs_status=lambda: self.outputs_status,
      get_output_overridden=lambda: self.audio.output_overridden,
      get_outputs=self.audio.get_outputs,
      get_config=self.config.get,
      on_refresh_outputs=self.refresh_outputs,
      on_select_automatic=self.select_automatic,
      on_select_fixed_output=self.select_fixed_output,
      on_toggle_startup=self.toggle_startup,
      on_quit=self.quit_app
    )

  def get_headset_state(self):
    if self.device.headset_dongle_connected is not True:
      return "Desconocido"

    if self.device.headset_connected is True:
      return "ENCENDIDO"

    if self.device.headset_connected is False:
      return "APAGADO"

    return "Desconocido"

  def _on_state_changed(self, device, **data):
    self.update_tray()

  def _on_device_error(self, device, message=None, **data):
    print(f"[DEVICE] {message or data}")

  def _on_dongle_connected(self, device, **data):
    print("[USB] Dongle connected.")

    if self.outputs_status != "processing":
      self.refresh_outputs()

  def _on_dongle_disconnected(self, device, **data):
    print("[USB] Dongle disconnected.")
    self.update_tray()

  def _on_audio_refresh_started(self, discovery, **data):
    if self.closing:
      return

    self.outputs_status = "processing"
    self.update_tray()

  def _on_audio_refresh_finished(
    self,
    discovery,
    status="ready",
    error=None,
    **data,
  ):
    if self.closing:
      return

    self.outputs_status = "error" if status == "error" else "ready"

    if error:
      print(f"[AUDIO] Discovery error: {error}")

    self.update_tray()

  def _on_audio_discovery_error(self, discovery, error=None, **data):
    self.outputs_status = "error"
    print(f"[AUDIO] Discovery error: {error or data}")
    self.update_tray()

  def toggle_startup(self, icon, item):
    try:
      current_value = is_startup_enabled()
      new_value = not current_value

      set_startup_enabled(new_value)

      try:
        self.config.set_startup_enabled(new_value)
      except Exception:
        set_startup_enabled(current_value)
        raise

    except Exception as exc:
      print(f"[STARTUP] Error cambiando el inicio: {exc}")
      return

    if self.icon and not self.closing:
      self.icon.update_menu()

  def update_tray(self):
    if not self.icon or self.closing:
      return

    self.icon.icon = self.ui.create_icon()

    if self.outputs_status == "initializing":
      self.icon.title = "Arctis 7+: Inicializando..."
    elif self.outputs_status == "processing":
      self.icon.title = "Arctis 7+: Procesando dispositivos..."
    elif self.outputs_status == "error":
      self.icon.title = "Arctis 7+: Error al descubrir dispositivos"
    elif self.device.headset_dongle_connected is not True:
      self.icon.title = "Arctis 7+: Receptor desconectado"
    elif self.device.headset_connected is True:
      if self.audio.output_overridden:
        self.icon.title = "Arctis 7+: Cambio manual de audio"
      elif self.device.headset_active is True:
        self.icon.title = "Arctis 7+: Activo"
      else:
        self.icon.title = "Arctis 7+: Esperando confirmación de audio"
    else:
      self.icon.title = f"Arctis 7+: {self.get_headset_state()}"

    self.icon.update_menu()

  def refresh_outputs(self, icon=None, item=None):
    if self.closing:
      return

    try:
      self.audio.refresh_outputs()
    except Exception as exc:
      self.outputs_status = "error"
      print(f"[AUDIO] Error requesting device refresh: {exc}")
      self.update_tray()

  def change_output_configuration(self, mode, device_id=None):
    if mode not in ("automatic", "fixed"):
      raise ValueError(f"Invalid output mode: {mode}")

    if mode == "fixed" and not device_id:
      raise ValueError("A device ID is required for fixed output mode.")

    try:
      self.config.set_fixed_output(
        device_id if mode == "fixed" else None
      )
    except Exception as exc:
      print(f"[CONFIG] Error saving output configuration: {exc}")
      return

    if self.icon and not self.closing:
      self.icon.update_menu()

  def select_automatic(self, icon, item):
    self.change_output_configuration("automatic")

  def select_fixed_output(self, device_id):
    self.change_output_configuration("fixed", device_id)

  def quit_app(self, icon, item):
    with self._shutdown_lock:
      if self._shutdown_started:
        return

      self._shutdown_started = True
      self.closing = True

    self._shutdown_thread = threading.Thread(
      target=self._shutdown,
      name="ArctisTrayShutdown",
      daemon=False
    )
    self._shutdown_thread.start()

  def _shutdown(self):
    try:
      self.device.stop()

      if self.device.is_alive():
        self.device.join(timeout=3)

        if self.device.is_alive():
          print("[DEVICE] El monitor HID no terminó dentro del plazo.")

    except Exception as exc:
      print(f"[DEVICE] Error durante el cierre: {exc}")

    try:
      self.audio.remove_listener("discovery_error", self._on_audio_discovery_error)
      self.audio.remove_listener("refresh_started", self._on_audio_refresh_started)
      self.audio.remove_listener("refresh_finished", self._on_audio_refresh_finished)
      stopped = self.audio.stop(timeout=5)

      if not stopped:
        print("[AUDIO] El trabajador de audio sigue activo.")

    except Exception as exc:
      print(f"[AUDIO] Error durante el cierre: {exc}")

    finally:
      if self.icon:
        self.icon.stop()

  def run(self):
    self.icon = pystray.Icon(
      "ArctisTray",
      self.ui.create_icon(),
      "Arctis 7+: Inicializando...",
      menu=self.ui.get_menu()
    )

    self.audio.add_listener("discovery_error", self._on_audio_discovery_error)
    self.audio.add_listener("refresh_started", self._on_audio_refresh_started)
    self.audio.add_listener("refresh_finished", self._on_audio_refresh_finished)
    self.audio.start()
    self.device.start()

    try:
      self.icon.run()

    finally:
      with self._shutdown_lock:
        should_start_shutdown = not self._shutdown_started

        if should_start_shutdown:
          self._shutdown_started = True
          self.closing = True

      if should_start_shutdown:
        self._shutdown()

      if (
        self._shutdown_thread
        and self._shutdown_thread is not threading.current_thread()
      ):
        self._shutdown_thread.join()


if __name__ == "__main__":
  ArctisTray().run()
