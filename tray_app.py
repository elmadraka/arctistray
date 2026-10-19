import threading
import pystray

from audio_controller import AudioController
from config_manager import ConfigManager
from device_factory import create_device
from tray_ui import TrayUI


class ArctisTray:
  def __init__(self):
    self.config = ConfigManager()
    self.device = create_device()
    self.audio = AudioController(self.config)
    self.audio.bind_device(self.device)

    self.icon = None
    self.closing = False
    self.refreshing_outputs = False
    self.refresh_blink_gray = False
    self.outputs_status = "initializing"

    self._refresh_blink_stop = threading.Event()
    self._refresh_blink_thread = None

    self._refresh_lock = threading.Lock()
    self._refresh_thread = None
    self._refresh_pending = False
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
      get_refreshing_outputs=lambda: self.refreshing_outputs,
      get_refresh_blink_gray=lambda: self.refresh_blink_gray,
      get_outputs=self.audio.get_outputs,
      get_config=self.config.get,
      on_refresh_outputs=self.refresh_outputs,
      on_select_automatic=self.select_automatic,
      on_select_fixed_output=self.select_fixed_output,
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
    self._start_output_refresh(force=True)

  def _start_output_refresh(self, force):
    if self.closing:
      return

    if not force and self.outputs_status == "ready":
      return

    if not self._refresh_lock.acquire(blocking=False):
      self._refresh_pending = True
      return

    self.refreshing_outputs = True
    self.refresh_blink_gray = False
    self._refresh_blink_stop.clear()
    self.outputs_status = "processing"
    self.update_tray()

    self._refresh_blink_thread = threading.Thread(
      target=self._refresh_blink_worker,
      name="ArctisTrayRefreshBlink",
      daemon=True
    )
    self._refresh_blink_thread.start()

    self._refresh_thread = threading.Thread(
      target=self._refresh_outputs_worker,
      name="ArctisTrayOutputRefresh",
      daemon=True
    )
    self._refresh_thread.start()

  def _refresh_blink_worker(self):
    while not self._refresh_blink_stop.wait(0.45):
      if not self.refreshing_outputs or self.closing:
        break

      self.refresh_blink_gray = not self.refresh_blink_gray
      self.update_tray()

  def _refresh_outputs_worker(self):
    try:
      self.audio.refresh_outputs()

      if not self.closing:
        self.outputs_status = "ready"

    except Exception as exc:
      if not self.closing:
        self.outputs_status = "error"
        print(f"[AUDIO] Error actualizando los dispositivos: {exc}")

    finally:
      self.refreshing_outputs = False
      self.refresh_blink_gray = False
      self._refresh_blink_stop.set()
      self._refresh_lock.release()
      self.update_tray()

      if self._refresh_pending and not self.closing:
        self._refresh_pending = False
        self._start_output_refresh(force=True)

  def change_output_configuration(self, mode, device_id=None):
    self.config.set_fixed_output(device_id)

    except Exception as exc:
      print(f"[CONFIG] Error guardando la configuración: {exc}")
      return

    # Cambiar la preferencia de restauración no altera la salida actual.
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
      refresh_thread = self._refresh_thread

      if (
        refresh_thread
        and refresh_thread.is_alive()
        and refresh_thread is not threading.current_thread()
      ):
        refresh_thread.join(timeout=5)

        if refresh_thread.is_alive():
          print("[AUDIO] El descubrimiento sigue activo durante el cierre.")

    finally:
      try:
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

    self.audio.start()
    self.device.start()
    self._start_output_refresh(force=False)

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
