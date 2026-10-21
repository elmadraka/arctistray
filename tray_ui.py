import threading
from PIL import Image, ImageDraw
import pystray

COLORS = {
  "initializing": (150, 150, 150, 255),
  "processing": (150, 150, 150, 255),
  "disconnected": (220, 65, 65, 255),
  "inactive": (240, 160, 40, 255),
  "active": (40, 190, 80, 255),
  "manual": (160, 90, 210, 255)
}


class TrayUI:
  def __init__(
    self,
    get_headset_state,
    get_receiver_connected,
    get_audio_connected,
    get_output_overridden,
    get_outputs_status,
    get_outputs,
    get_config,
    on_refresh_outputs,
    on_select_automatic,
    on_select_fixed_output,
    on_toggle_startup,
    on_quit
  ):
    self.get_headset_state = get_headset_state
    self.get_receiver_connected = get_receiver_connected
    self.get_audio_connected = get_audio_connected
    self.get_output_overridden = get_output_overridden
    self.get_outputs_status = get_outputs_status
    self.get_outputs = get_outputs
    self.get_config = get_config
    self.on_refresh_outputs = on_refresh_outputs
    self.on_select_automatic = on_select_automatic
    self.on_select_fixed_output = on_select_fixed_output
    self.on_toggle_startup = on_toggle_startup
    self.on_quit = on_quit

    self._blink_timer = None
    self._blink_gray = False
    self._blinking = False
    self._update_icon_callback = None

  def start_blinking(self, update_icon_callback):
    self._update_icon_callback = update_icon_callback
    self._blinking = True
    self._blink_gray = True
    self._update_icon_callback()
    self._schedule_blink()

  def stop_blinking(self):
    self._blinking = False

    if self._blink_timer:
      self._blink_timer.cancel()
      self._blink_timer = None

    self._blink_gray = False
    self._update_icon_callback = None

  def _schedule_blink(self):
    if not self._blinking:
      return

    self._blink_timer = threading.Timer(0.4, self._toggle_blink)
    self._blink_timer.daemon = True
    self._blink_timer.start()

  def _toggle_blink(self):
    if not self._blinking:
      return

    self._blink_gray = not self._blink_gray

    if self._update_icon_callback:
      self._update_icon_callback()

    self._schedule_blink()

  def get_status(self):
    if self.get_outputs_status() == "initializing":
      return "initializing"

    if not self.get_receiver_connected():
      return "disconnected"

    if self.get_headset_state() != "ENCENDIDO":
      return "inactive"

    if self.get_output_overridden():
      return "manual"

    if self.get_audio_connected():
      return "active"

    return "inactive"

  def create_current_icon(self):
    return self.create_icon(gray=self._blinking and self._blink_gray)

  def create_icon(self, gray=False):
    status = self.get_status()
    color = COLORS["processing"] if gray else COLORS[status]

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.ellipse((5, 5, 59, 59), fill=color)
    draw.arc(
      (17, 15, 47, 49),
      200,
      340,
      fill=(255, 255, 255, 255),
      width=4
    )
    draw.arc(
      (17, 15, 47, 49),
      20,
      160,
      fill=(255, 255, 255, 255),
      width=4
    )
    draw.line(
      (17, 31, 17, 42),
      fill=(255, 255, 255, 255),
      width=4
    )
    draw.line(
      (47, 31, 47, 42),
      fill=(255, 255, 255, 255),
      width=4
    )
    draw.rounded_rectangle(
      (39, 37, 49, 47),
      radius=3,
      fill=(255, 255, 255, 255)
    )

    return image

  def get_menu(self):
    return pystray.Menu(self.build_menu_items)

  def build_menu_items(self):
    outputs_status = self.get_outputs_status()
    headset_state = self.get_headset_state()
    receiver_connected = self.get_receiver_connected()
    audio_connected = self.get_audio_connected()
    output_overridden = self.get_output_overridden()

    if outputs_status == "initializing":
      yield pystray.MenuItem(
        "Inicializando aplicación...",
        self.do_nothing,
        enabled=False
      )
    elif outputs_status == "processing":
      yield pystray.MenuItem(
        "Procesando dispositivos...",
        self.do_nothing,
        enabled=False
      )
    elif outputs_status == "error":
      yield pystray.MenuItem(
        "Error al consultar los dispositivos",
        self.do_nothing,
        enabled=False
      )

    yield pystray.MenuItem(
      lambda item: f"Auriculares: {self.get_headset_state()}",
      self.do_nothing,
      enabled=False
    )

    yield pystray.MenuItem(
      lambda item: (
        "Receptor USB: Conectado"
        if self.get_receiver_connected()
        else "Receptor USB: Desconectado"
      ),
      self.do_nothing,
      enabled=False
    )

    if headset_state == "ENCENDIDO" and output_overridden:
      audio_label = "Audio: Cambio manual detectado"
    elif audio_connected:
      audio_label = "Audio: Arctis activo"
    elif headset_state == "ENCENDIDO":
      audio_label = "Audio: Esperando confirmación"
    else:
      audio_label = "Audio: Inactivo"

    yield pystray.MenuItem(
      audio_label,
      self.do_nothing,
      enabled=False
    )

    yield pystray.Menu.SEPARATOR

    yield pystray.MenuItem(
      "Modo de restauración de salida",
      self.do_nothing,
      enabled=False
    )

    yield pystray.MenuItem(
      "Automática (última salida)",
      self.on_select_automatic,
      checked=lambda item: (
        self.get_config("output_fixed") is None
      ),
      radio=True
    )

    if outputs_status == "ready":
      try:
        for output in self.get_outputs():
          if output["kind"] == "headset":
            continue

          yield pystray.MenuItem(
            output["name"],
            self.make_output_callback(output["id"]),
            checked=self.make_output_checked(output["id"]),
            radio=True
          )

        yield pystray.Menu.SEPARATOR

      except Exception as exc:
        print(f"[UI] Error enumerando salidas: {exc}")
        yield pystray.MenuItem(
          "Error al consultar las salidas",
          self.do_nothing,
          enabled=False
        )

    yield pystray.MenuItem(
      "Actualizar dispositivos",
      self.on_refresh_outputs,
      enabled=outputs_status != "processing"
    )

    yield pystray.Menu.SEPARATOR

    yield pystray.MenuItem(
      lambda item: (
        f"Cambiar entrada: "
        f"{'Sí' if self.get_config('input_change_enabled') else 'No'}"
      ),
      self.do_nothing,
      enabled=False
    )

    yield pystray.MenuItem(
      lambda item: (
        "Iniciar con Windows: "
        f"{'Sí' if self.get_config('startup_enabled') else 'No'}"
      ),
      self.on_toggle_startup,
      checked=lambda item: bool(
        self.get_config("startup_enabled")
      )
    )

    yield pystray.Menu.SEPARATOR
    yield pystray.MenuItem("Salir", self.on_quit)

  def do_nothing(self, icon, item):
    pass

  def make_output_callback(self, device_id):
    def callback(icon, item):
      self.on_select_fixed_output(device_id)

    return callback

  def make_output_checked(self, device_id):
    def checked(item):
      return (
        self.get_config("output_fixed") is not None
        and self.get_config("output_fixed") == device_id
      )

    return checked
