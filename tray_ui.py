from __future__ import annotations

import signal
import sys

from PySide6.QtCore import (
  Qt,
  QRectF,
  QPoint,
  QTimer,
  QEvent,
  Signal,
  QLocale,
)
from PySide6.QtGui import (
  QColor,
  QCursor,
  QIcon,
  QPainter,
  QPainterPath,
  QPen,
  QPixmap,
)
from PySide6.QtWidgets import (
  QApplication,
  QCheckBox,
  QComboBox,
  QFrame,
  QHBoxLayout,
  QLabel,
  QSlider,
  QSystemTrayIcon,
  QVBoxLayout,
  QWidget,
  QPushButton,
  QToolButton,
  QGridLayout,
  QStyle,
  QStyleOptionSlider,
  QStylePainter,
)


TRANSLATIONS = {
  "es": {
    "unknown": "Desconocido",
    "connected": "Conectado",
    "disconnected": "Desconectado",
    "charging": "Cargando",
    "charged": "Cargado",
    "discharging": "Descargando",
    "dongle": "Recept. USB",
    "usb_cable": "Cable USB",
    "output_restoration": "Restauración de salida",
    "automatic_output": "Automática (última salida)",
    "arctis_output": "SteelSeries Arctis 7+",
    "speakers": "Altavoces",
    "external_headphones": "Auriculares externos",
    "auto_input": "Cambiar entrada automáticamente",
    "arctis_microphone": "Micrófono Arctis 7+",
    "external_microphone": "Micrófono externo",
    "startup": "Iniciar con Windows",
    "exit": "Salir",
    "pin": "Fijar ventana",
    "unpin": "Desfijar ventana",
  },
  "en": {
    "unknown": "Unknown",
    "connected": "Connected",
    "disconnected": "Disconnected",
    "charging": "Charging",
    "charged": "Fully charged",
    "discharging": "Discharging",
    "dongle": "USB receiver",
    "usb_cable": "USB cable",
    "output_restoration": "Output restoration",
    "automatic_output": "Automatic (last output)",
    "arctis_output": "SteelSeries Arctis 7+",
    "speakers": "Speakers",
    "external_headphones": "External headphones",
    "auto_input": "Change input automatically",
    "arctis_microphone": "Arctis 7+ microphone",
    "external_microphone": "External microphone",
    "startup": "Start with Windows",
    "exit": "Exit",
    "pin": "Pin window",
    "unpin": "Unpin window",
  },
}


def _get_language():
  return (
    "es"
    if QLocale.system().name().lower().startswith("es")
    else "en"
  )


class MixDialSlider(QSlider):
  def paintEvent(self, event):
    painter = QStylePainter(self)

    option = QStyleOptionSlider()
    self.initStyleOption(option)

    # Draw the slider groove
    option.subControls = QStyle.SubControl.SC_SliderGroove
    painter.drawComplexControl(
      QStyle.ComplexControl.CC_Slider,
      option,
    )

    groove = self.style().subControlRect(
      QStyle.ComplexControl.CC_Slider,
      option,
      QStyle.SubControl.SC_SliderGroove,
      self,
    )

    # Draw the center marker
    painter.setPen(QPen(QColor("white"), 2))
    painter.drawLine(
      groove.center().x() + 1,
      groove.top() - 3,
      groove.center().x() + 1,
      groove.bottom() + 3,
    )

    # Keep the handle centered around 50% for values from 45 to 55
    value = self.value()

    if value >= 95:
      visual_value = 100
    elif value <= 5:
      visual_value = 0
    elif 45 <= value <= 55:
      visual_value = 50
    else:
      visual_value = value

    # Draw the handle using the native slider geometry
    option.sliderPosition = visual_value
    option.sliderValue = visual_value
    option.subControls = QStyle.SubControl.SC_SliderHandle

    painter.drawComplexControl(
      QStyle.ComplexControl.CC_Slider,
      option,
    )

    painter.end()


class TrayPanel(QWidget):
  resized = Signal()

  def __init__(self, debug=False):
    window_flags = (
      Qt.WindowType.Tool
      | Qt.WindowType.WindowStaysOnTopHint
      if debug
      else Qt.WindowType.Popup
    ) | Qt.WindowType.FramelessWindowHint

    super().__init__(None, window_flags)

    self.debug = debug
    self.language = _get_language()
    self.translations = TRANSLATIONS[self.language]
    self._pinned = debug

    self.setWindowTitle("ArctisTray")
    self.setMinimumWidth(400)

    self._build_ui()
    self.adjustSize()

  def _tr(self, key):
    return self.translations[key]

  def _quit_application(self):
    QApplication.quit()


  def update_device_state(self, device_name, state):
    self.device_name.setText(device_name)

    # Headset connection
    if state.headset_on == "on":
      self.connection_status.setText(self._tr("connected"))
    elif state.headset_on == "off":
      self.connection_status.setText(self._tr("disconnected"))
    else:
      self.connection_status.setText(self._tr("unknown"))

    # Dongle connection
    self.dongle_detail.setText(
      self._tr("connected")
      if state.dongle_connected
      else self._tr("disconnected")
    )

    # USB cable connection
    if state.usb_connected is None:
      self.usb_detail.setText(self._tr("unknown"))
    else:
      self.usb_detail.setText(
        self._tr("connected")
        if state.usb_connected
        else self._tr("disconnected")
      )

    # Battery level
    if state.battery_level in (2, 3, 4):
      percentage = round(state.battery_level / 4 * 100)
      self.battery_value.setText(f"{percentage}%")
    else:
      self.battery_value.setText("--")

    # Battery status
    battery_status = {
      "charging": self._tr("charging"),
      "fully_charged": self._tr("charged"),
      "discharging": self._tr("discharging"),
    }

    self.battery_status.setText(
      battery_status.get(state.battery_status, self._tr("unknown"))
    )

    # MixDial
    game = state.mixdial_game
    chat = state.mixdial_chat

    if game is not None and chat is not None:
      value = round(50 + (game - chat) / 2)
      self.mixdial_slider.setValue(
        max(0, min(100, value))
      )
    else:
      self.mixdial_slider.setValue(50)

  def resizeEvent(self, event):
    super().resizeEvent(event)
    self.resized.emit()

  def _set_pinned(self, pinned: bool):
    self._pinned = pinned
    self.pin_button.setToolTip(
      self._tr("unpin") if pinned else self._tr("pin")
    )

    position = self.pos()
    self.hide()
    self.setWindowFlags(
      (
        Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
        if pinned else Qt.WindowType.Popup
      ) | Qt.WindowType.FramelessWindowHint
    )
    self.move(position)
    self.show()
    self.raise_()
    self.activateWindow()

  def _build_ui(self):
    root = QVBoxLayout(self)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(12)
    root.setSizeConstraint(
      QVBoxLayout.SizeConstraint.SetFixedSize
    )

    # Top content
    top_container = QWidget()
    top_layout = QVBoxLayout(top_container)
    top_layout.setContentsMargins(18, 16, 18, 0)
    top_layout.setSpacing(12)

    # Device header
    header = QHBoxLayout()

    self.device_name = QLabel()
    self.device_name.setStyleSheet(
      "font-size: 16px; font-weight: 600;"
    )

    self.connection_status = QLabel("Desconocido")
    self.connection_status.setAlignment(
      Qt.AlignmentFlag.AlignRight
    )

    header.addWidget(self.device_name)
    header.addStretch()
    header.addWidget(self.connection_status)

    top_layout.addLayout(header)

    # Connection and battery status
    device_info = QWidget()
    device_info_layout = QGridLayout(device_info)
    device_info_layout.setContentsMargins(0, 18, 0, 18)
    device_info_layout.setHorizontalSpacing(0)
    device_info_layout.setVerticalSpacing(36)

    for column in range(6):
      device_info_layout.setColumnStretch(column, 1)

    device_info_layout.addWidget(
      self._status_column(
        "usb", self._tr("dongle"), self._tr("connected")
      ),
      0, 0, 1, 2,
    )

    device_info_layout.addWidget(
      self._status_column(
        "cable", self._tr("usb_cable"), self._tr("disconnected")
      ),
      0, 2, 1, 2,
    )

    device_info_layout.addWidget(
      self._status_column(
        "battery", "75%", self._tr("charging")
      ),
      0, 4, 1, 2,
    )

    chat_icon = QLabel()
    chat_icon.setPixmap(self._draw_icon("headset", 24))
    chat_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

    game_icon = QLabel()
    game_icon.setPixmap(self._draw_icon("game", 24))
    game_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

    self.mixdial_slider = MixDialSlider(
      Qt.Orientation.Horizontal
    )
    self.mixdial_slider.setRange(0, 100)

    total = 70 + 30
    self.mixdial_slider.setValue(
      round(70 / total * 100) if total > 0 else 50
    )
    self.mixdial_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    self.mixdial_slider.setAttribute(
      Qt.WidgetAttribute.WA_TransparentForMouseEvents,
      True,
    )

    device_info_layout.addWidget(chat_icon, 1, 0)
    device_info_layout.addWidget(self.mixdial_slider, 1, 1, 1, 4)
    device_info_layout.addWidget(game_icon, 1, 5)

    top_layout.addWidget(device_info)
    root.addWidget(top_container)

    # First full-width separator
    root.addWidget(self._separator(margin_y=6))

    # Audio content
    audio_container = QWidget()
    audio_layout = QVBoxLayout(audio_container)
    audio_layout.setContentsMargins(18, 0, 18, 0)
    audio_layout.setSpacing(12)


    audio_layout.addWidget(
      self._section_title(self._tr("output_restoration"))
    )

    self.output_container, self.output_combo = self._add_combo(
      audio_layout,
      None,
      [
        self._tr("automatic_output"),
        self._tr("arctis_output"),
        self._tr("speakers"),
        self._tr("external_headphones"),
      ],
    )

    self.auto_input = QCheckBox(
      self._tr("auto_input")
    )
    audio_layout.addWidget(self.auto_input)

    self.input_container, self.input_combo = self._add_combo(
      audio_layout,
      None,
      [
        self._tr("arctis_microphone"),
        self._tr("external_microphone"),
      ],
    )

    self.input_container.hide()
    self.auto_input.toggled.connect(
      self.input_container.setVisible
    )

    self.startup = QCheckBox(self._tr("startup"))
    self.startup.setChecked(False)
    audio_layout.addWidget(self.startup)

    root.addWidget(audio_container)

    # Second full-width separator
    root.addWidget(self._separator(margin_y=6))

    # Footer
    footer_container = QWidget()
    footer_layout = QVBoxLayout(footer_container)
    footer_layout.setContentsMargins(18, 0, 18, 18)
    footer_layout.setSpacing(0)

    footer_buttons = QHBoxLayout()
    footer_buttons.setSpacing(8)

    if self.debug:
      self.pin_button = QToolButton()
      self.pin_button.setText("📌")
      self.pin_button.setCheckable(True)
      self.pin_button.setChecked(True)
      self.pin_button.setToolTip(self._tr("unpin"))
      self.pin_button.toggled.connect(self._set_pinned)

      footer_buttons.addWidget(self.pin_button)

    exit_button = QPushButton(self._tr("exit"))
    exit_button.clicked.connect(self._quit_application)
    footer_buttons.addWidget(exit_button, 1)

    footer_layout.addLayout(footer_buttons)

    version = QLabel("v1.2")
    version.setStyleSheet(
      "font-size: 8px;"
      "color: #707070;"
    )

    version.setParent(footer_container)
    version.adjustSize()

    QTimer.singleShot(0, lambda: (
      version.move(
        footer_container.width() - version.width() - 18,
        footer_container.height() - version.height() - 6
      ),
      version.raise_()
    ))

    root.addWidget(footer_container)



  def _status_column(
    self,
    icon_name: str,
    title: str,
    detail: str,
  ) -> QWidget:
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

    icon = QLabel()
    icon.setPixmap(self._draw_icon(icon_name, 24))

    title_label = QLabel(title)
    title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    title_label.setStyleSheet("font-weight: 600;")

    detail_label = QLabel(detail)
    detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    detail_label.setStyleSheet("font-size: 10px;")

    if icon_name == "usb":
      self.dongle_detail = detail_label
    elif icon_name == "cable":
      self.usb_detail = detail_label
    elif icon_name == "battery":
      self.battery_value = title_label
      self.battery_status = detail_label

    layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title_label)
    layout.addWidget(detail_label)

    return container

  def _draw_icon(
    self,
    name: str,
    size: int,
  ) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(
      QPainter.RenderHint.Antialiasing
    )

    color = self.palette().color(
      self.palette().ColorRole.WindowText
    )

    pen = QPen(color, 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if name == "usb":
      painter.drawLine(12, 20, 12, 5)
      painter.drawLine(12, 5, 9, 8)
      painter.drawLine(12, 5, 15, 8)
      painter.drawLine(12, 12, 6, 12)
      painter.drawLine(6, 12, 6, 16)
      painter.drawRect(4, 16, 4, 3)
      painter.drawLine(12, 12, 18, 12)
      painter.drawLine(18, 12, 18, 16)
      painter.drawEllipse(16, 16, 4, 4)

    elif name == "cable":
      path = QPainterPath()
      path.moveTo(5, 5)
      path.lineTo(5, 10)
      path.cubicTo(5, 17, 19, 7, 19, 14)
      path.lineTo(19, 19)
      painter.drawPath(path)
      painter.drawLine(3, 5, 7, 5)
      painter.drawLine(3, 3, 3, 7)
      painter.drawLine(17, 19, 21, 19)
      painter.drawLine(17, 17, 17, 21)

    elif name == "battery":
      painter.drawRoundedRect(
        QRectF(3, 7, 17, 11),
        2,
        2,
      )
      painter.drawRect(20, 10, 2, 5)
      painter.drawLine(7, 10, 7, 15)
      painter.drawLine(10, 10, 10, 15)
      painter.drawLine(13, 10, 13, 15)

    elif name == "game":
      painter.drawRoundedRect(
        QRectF(2, 7, 20, 12),
        5,
        5,
      )
      painter.drawLine(7, 10, 7, 16)
      painter.drawLine(4, 13, 10, 13)
      painter.drawEllipse(15, 10, 2, 2)
      painter.drawEllipse(18, 14, 2, 2)

    elif name == "headset":
      path = QPainterPath()
      path.moveTo(3, 13)
      path.cubicTo(3, 1, 21, 1, 21, 13)
      painter.drawPath(path)
      painter.drawRoundedRect(
        QRectF(2, 11, 5, 9),
        2,
        2,
      )
      painter.drawRoundedRect(
        QRectF(17, 11, 5, 9),
        2,
        2,
      )
      painter.drawLine(17, 19, 14, 22)
      painter.drawLine(14, 22, 11, 22)

    painter.end()

    return pixmap

  def _separator(self, margin_y: int = 0):
    line = QFrame()
    line.setFixedHeight(2)
    line.setStyleSheet(
      "background-color: #505050;"
    )

    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, margin_y, 0, margin_y)
    layout.setSpacing(0)
    layout.addWidget(line)

    return container

  def _section_title(self, text: str):
    label = QLabel(text)
    label.setStyleSheet("font-weight: 600;")
    return label

  def _add_combo(
    self,
    layout: QVBoxLayout,
    name: str | None,
    options: list[str],
  ) -> tuple[QWidget, QComboBox]:
    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)

    combo = QComboBox()
    combo.addItems(options)

    if name is not None:
      row.addWidget(QLabel(name))

    row.addWidget(combo, 1)
    layout.addWidget(container)

    return container, combo

class TrayController:
  def __init__(self, app: QApplication, debug=False):
    self.app = app
    self.panel = TrayPanel(debug=debug)
    self.panel.resized.connect(self._on_panel_resized)

    self.tray = QSystemTrayIcon()
    self.tray.setIcon(self._create_icon())
    self.tray.setToolTip("ArctisTray")

    self.tray.activated.connect(
      self._on_tray_activated
    )

    self.tray.show()
    self._show_panel()

    if hasattr(app, "paletteChanged"):
      app.paletteChanged.connect(self._update_icon)

  def _on_panel_resized(self):
    if self.panel.isVisible():
      self._position_panel()

  def _create_icon(self) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(
      QPainter.RenderHint.Antialiasing
    )

    color = self.app.palette().color(
      self.app.palette().ColorRole.WindowText
    )

    pen = QPen(color, 5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

    painter.setPen(pen)

    path = QPainterPath()
    path.moveTo(12, 35)
    path.cubicTo(12, 5, 52, 5, 52, 35)

    painter.drawPath(path)

    painter.drawRoundedRect(
      QRectF(8, 31, 11, 21),
      4,
      4,
    )

    painter.drawRoundedRect(
      QRectF(45, 31, 11, 21),
      4,
      4,
    )

    painter.end()

    return QIcon(pixmap)

  def _update_icon(self, *_args):
    self.tray.setIcon(self._create_icon())

  def _on_tray_activated(self, reason):
    if reason in (
      QSystemTrayIcon.ActivationReason.Trigger,
      QSystemTrayIcon.ActivationReason.Context,
    ):
      self.toggle_panel()

  def toggle_panel(self):
    if self.panel.isVisible():
      self.panel.hide()
    else:
      self._show_panel()

  def _show_panel(self):
    self.panel.adjustSize()
    self._position_panel()
    self.panel.show()
    self.panel.activateWindow()

  def _position_panel(self):
    tray_rect = self.tray.geometry()

    if tray_rect.isValid() and not tray_rect.isEmpty():
      anchor = tray_rect.center()
    else:
      anchor = QCursor.pos()

    screen = self.app.screenAt(anchor)

    if screen is None:
      screen = self.app.primaryScreen()

    if screen is None:
      return

    available = screen.availableGeometry()

    x = anchor.x() - self.panel.width() // 2

    if anchor.y() > available.center().y():
      y = anchor.y() - self.panel.height() - 8
    else:
      y = anchor.y() + 8

    x = max(
      available.left(),
      min(x, available.right() - self.panel.width() + 1),
    )

    y = max(
      available.top(),
      min(y, available.bottom() - self.panel.height() + 1),
    )

    self.panel.move(x, y)


def main():
  app = QApplication(sys.argv)
  app.setApplicationName("ArctisTray")
  app.setQuitOnLastWindowClosed(False)

  if not QSystemTrayIcon.isSystemTrayAvailable():
    print("System tray is not available.")
    return 1

  controller = TrayController(app)

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