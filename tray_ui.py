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
    get_refreshing_outputs,
    get_refresh_blink_gray,
    get_outputs,
    get_config,
    on_refresh_outputs,
    on_select_automatic,
    on_select_fixed_output,
    on_quit
  ):
    self.get_headset_state = get_headset_state
    self.get_receiver_connected = get_receiver_connected
    self.get_audio_connected = get_audio_connected
    self.get_output_overridden = get_output_overridden
    self.get_outputs_status = get_outputs_status
    self.get_refreshing_outputs = get_refreshing_outputs
    self.get_refresh_blink_gray = get_refresh_blink_gray
    self.get_outputs = get_outputs
    self.get_config = get_config
    self.on_refresh_outputs = on_refresh_outputs
    self.on_select_automatic = on_select_automatic
    self.on_select_fixed_output = on_select_fixed_output
    self.on_quit = on_quit

  def get_status(self):
    outputs_status = self.get_outputs_status()
    headset_state = self.get_headset_state()

    if outputs_status == "initializing":
      return "initializing"

    if not self.get_receiver_connected():
      return "disconnected"

    if headset_state != "ENCENDIDO":
      return "inactive"

    if self.get_output_overridden():
      return "manual"

    if self.get_audio_connected():
      return "active"

    return "inactive"

  def create_icon(self):
    status = self.get_status()

    if self.get_refreshing_outputs() and self.get_refresh_blink_gray():
      status = "processing"

    color = COLORS[status]

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
    refreshing_outputs = self.get_refreshing_outputs()
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
        self.get_config("output_mode") == "automatic"
      ),
      radio=True
    )

    if outputs_status == "ready" and not refreshing_outputs:
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
      "Actualizando dispositivos..." if refreshing_outputs
      else "Actualizar dispositivos",
      self.on_refresh_outputs,
      enabled=not refreshing_outputs
    )

    yield pystray.Menu.SEPARATOR

    yield pystray.MenuItem(
      lambda item: (
        f"Cambiar entrada: "
        f"{'Sí' if self.get_config('change_input') else 'No'}"
      ),
      self.do_nothing,
      enabled=False
    )

    yield pystray.MenuItem(
      lambda item: (
        f"Iniciar con Windows: "
        f"{'Sí' if self.get_config('startup') else 'No'}"
      ),
      self.do_nothing,
      enabled=False
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
        self.get_config("output_mode") == "fixed"
        and self.get_config("fixed_output") == device_id
      )

    return checked
