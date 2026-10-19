import queue
import threading

import comtypes

from pycaw.callbacks import MMNotificationClient
from pycaw.constants import EDataFlow, ERole
from pycaw.pycaw import AudioUtilities

from audio_discovery import AudioDiscovery


OUTPUT_ROLES = {
  "console": ERole.eConsole,
  "communications": ERole.eCommunications
}

INPUT_ROLES = {
  "console": ERole.eConsole,
  "communications": ERole.eCommunications
}


class _AudioNotificationClient(MMNotificationClient):
  def __init__(self, controller):
    super().__init__()
    self.controller = controller

  def on_default_device_changed(
    self,
    flow,
    flow_id,
    role,
    role_id,
    default_device_id
  ):
    if flow in ("eRender", "eCapture"):
      self.controller._queue_default_change(flow)


class AudioController:
  def __init__(self, config=None):
    self.config = config
    self.discovery = AudioDiscovery()
    self.device = None

    self._catalog = {
      "outputs": [],
      "inputs": [],
      "headset_output": None,
      "headset_input": None
    }
    self.discovery_ready = False
    self.discovery_error = None

    self._tasks = queue.Queue()
    self._worker = None
    self._closing = False
    self._observer = None
    self._enumerator = None
    self._observer_ready = False

    self._output_previous = None
    self._output_changed_roles = set()
    self._output_owned = False
    self._output_override = False
    self._activated_output = None
    self._last_non_headset_output = None

    self._input_previous = None
    self._input_changed_roles = set()
    self._input_owned = False
    self._input_override = False
    self._activated_input = None

    self._headset_active = False
    self._last_log_messages = {}

  @property
  def output_overridden(self):
    return self._output_override

  @property
  def outputs(self):
    return self._catalog["outputs"]

  @property
  def inputs(self):
    return self._catalog["inputs"]

  @property
  def cached_headset_output(self):
    return self._catalog["headset_output"]

  @property
  def cached_headset_input(self):
    return self._catalog["headset_input"]

  def bind_device(self, device):
    if self.device is not None:
      raise RuntimeError("Ya hay un dispositivo vinculado.")

    self.device = device
    device.add_listener("headset_connected", self._on_headset_connected)
    device.add_listener("headset_disconnected", self._on_headset_disconnected)
    device.add_listener(
      "headset_dongle_disconnected",
      self._on_dongle_disconnected
    )

  def start(self):
    if self.device is None:
      raise RuntimeError("Primero hay que vincular un dispositivo.")

    if self._worker and self._worker.is_alive():
      return

    self._closing = False
    self._worker = threading.Thread(
      target=self._worker_loop,
      name="ArctisTrayAudioWorker",
      daemon=True
    )
    self._worker.start()

  def stop(self, timeout=5):
    self._closing = True
    self._tasks.put("stop")

    worker = self._worker
    if worker and worker.is_alive() and worker is not threading.current_thread():
      worker.join(timeout=max(0, timeout))

    stopped = worker is None or not worker.is_alive()

    if self.device is not None:
      self.device.remove_listener(
        "headset_connected",
        self._on_headset_connected
      )
      self.device.remove_listener(
        "headset_disconnected",
        self._on_headset_disconnected
      )
      self.device.remove_listener(
        "headset_dongle_disconnected",
        self._on_dongle_disconnected
      )

    if not stopped:
      print("[AUDIO] El trabajador no terminó dentro del plazo.")

    return stopped
  
  def refresh_outputs(self):
    """Actualiza el catálogo y publica una instantánea coherente."""
    try:
      catalog = self.discovery.get_catalog(refresh=True)

      outputs = catalog["active_outputs"]
      inputs = catalog["active_inputs"]

      headset_output = catalog["headset_output"]
      headset_input = catalog["headset_input"]
      
      self._catalog = {
        "outputs": outputs,
        "inputs": inputs,
        "headset_output": (
          headset_output["id"] if headset_output else None
        ),
        "headset_input": (
          headset_input["id"] if headset_input else None
        )
      }

      self.discovery_error = None
      self.discovery_ready = True

      print(
        f"[AUDIO] Catálogo actualizado: "
        f"{len(outputs)} salidas, {len(inputs)} entradas."
      )

    except Exception as exc:
      self.discovery_error = str(exc)
      print(f"[AUDIO] Error descubriendo dispositivos: {exc}")
      raise

    if self._headset_is_ready():
      self.request_activation()

  def get_outputs(self):
    return [item.copy() for item in self._catalog["outputs"]]

  def get_inputs(self):
    return [item.copy() for item in self._catalog["inputs"]]

  def get_discovery_status(self):
    return {
      "ready": self.discovery_ready,
      "error": self.discovery_error
    }

  def request_activation(self):
    if (
      not self._closing
      and self.discovery_ready
      and self.discovery_error is None
    ):
      self._tasks.put("activate")

  def _on_headset_connected(self, device, **data):
    self.request_activation()

  def _on_headset_disconnected(self, device, **data):
    self._tasks.put("deactivate")

  def _on_dongle_disconnected(self, device, **data):
    self._tasks.put("deactivate")

  def _queue_default_change(self, flow):
    if not self._closing:
      self._tasks.put(("default_changed", flow))

  def _worker_loop(self):
    initialized = False

    try:
      comtypes.CoInitialize()
      initialized = True
      self._register_notifications()

      while not self._closing:
        task = self._tasks.get()

        if task == "stop":
          break

        if task == "activate":
          self._activate_until_done()
        elif task == "deactivate":
          self._deactivate()
        elif isinstance(task, tuple) and task[0] == "default_changed":
          self._handle_default_change(task[1])

    except Exception as exc:
      print(f"[AUDIO] Error en el trabajador de audio: {exc}")

    finally:
      try:
        self._restore_on_shutdown()
      finally:
        self._unregister_notifications()
        if initialized:
          comtypes.CoUninitialize()

  def _register_notifications(self):
    self._enumerator = AudioUtilities.GetDeviceEnumerator()
    self._observer = _AudioNotificationClient(self)
    self._enumerator.RegisterEndpointNotificationCallback(self._observer)
    self._observer_ready = True

  def _unregister_notifications(self):
    if self._enumerator is not None and self._observer is not None:
      try:
        self._enumerator.UnregisterEndpointNotificationCallback(
          self._observer
        )
      except Exception as exc:
        print(f"[AUDIO] Error retirando las notificaciones: {exc}")

    self._observer = None
    self._enumerator = None
    self._observer_ready = False

  def _headset_is_ready(self):
    return (
      not self._closing
      and self.device is not None
      and self.device.headset_dongle_connected is True
      and self.device.headset_connected is True
    )

  def _get_default_devices(self, flow):
    if flow == "output":
      data_flow = EDataFlow.eRender
      roles = OUTPUT_ROLES
    else:
      data_flow = EDataFlow.eCapture
      roles = INPUT_ROLES

    enumerator = AudioUtilities.GetDeviceEnumerator()
    result = {}

    for key, role in roles.items():
      try:
        endpoint = enumerator.GetDefaultAudioEndpoint(
          data_flow.value,
          role.value
        )
        result[key] = endpoint.GetId()
      except Exception:
        result[key] = None

    if not any(result.values()):
      raise RuntimeError(f"Windows no tiene una entrada/salida {flow} válida.")

    return result

  def get_default_output(self):
    return self._get_default_devices("output")

  def get_default_input(self):
    return self._get_default_devices("input")

  def _set_default_devices(self, flow, devices, changed_roles=None):
    roles = OUTPUT_ROLES if flow == "output" else INPUT_ROLES

    for key, device_id in devices.items():
      if key not in roles or not device_id:
        continue

      current = self._get_default_devices(flow).get(key)

      if current and current.casefold() == device_id.casefold():
        continue

      # Console y Communications se cambian juntos por ahora.
      # Estos roles se pueden independizar en una futura configuración.
      AudioUtilities.SetDefaultDevice(device_id, roles=[roles[key]])

      if changed_roles is not None:
        changed_roles.add(key)

  def set_default_outputs(self, outputs, changed_roles=None):
    return self._set_default_devices(
      "output",
      outputs,
      changed_roles=changed_roles
    )

  def set_default_inputs(self, inputs, changed_roles=None):
    return self._set_default_devices(
      "input",
      inputs,
      changed_roles=changed_roles
    )

  def is_output_default(self, device_id):
    current = self.get_default_output()
    return all(
      value is not None and value.casefold() == device_id.casefold()
      for value in current.values()
    )

  def get_output_mode(self):
    return self.config.get("output_mode") or "automatic"

  def get_fixed_output(self):
    return self.config.get("fixed_output")

  def get_input_mode(self):
    return self.config.get("input_mode") or "automatic"

  def get_fixed_input(self):
    return self.config.get("fixed_input")

  def _capture_previous(self, flow, headset_id):
    current = self._get_default_devices(flow)

    # Si el Arctis ya era el predeterminado, no hay nada que restaurar.
    if all(
      value is not None and value.casefold() == headset_id.casefold()
      for value in current.values()
    ):
      return None

    return current

  def _activate_until_done(self):
    while not self._closing and self._headset_is_ready():
      if not self.discovery_ready or self.discovery_error is not None:
        return

      if not self._observer_ready:
        print("[AUDIO] No se puede activar sin observar los cambios de Windows.")
        return

      try:
        headset_output = self.cached_headset_output
        if not headset_output:
          print(
            "[AUDIO] Activación aplazada: la salida física del Arctis "
            "no está disponible en el catálogo."
          )
          return

        if self._output_override:
          self._set_active_state(False)
          return

        if not self._output_owned:
          self._output_previous = self._capture_previous(
            "output",
            headset_output
          )

        if not self.is_output_default(headset_output):
          self._activated_output = headset_output
          self.set_default_outputs(
            {
              "console": headset_output,
              "communications": headset_output
            },
            changed_roles=self._output_changed_roles
          )
          self._output_owned = bool(self._output_changed_roles)

        if not self._headset_is_ready():
          self._deactivate()
          return

        if not self.is_output_default(headset_output):
          raise RuntimeError("Windows no ha confirmado la salida Arctis.")

        self._set_active_state(True)
        self._last_log_messages.pop("activation_pending", None)
        self._last_log_messages.pop("arctis_output_unavailable", None)
        self._activate_input_if_enabled()
        self._log_audio_state("activación confirmada")
        return

      except Exception as exc:
        self._log("activation_pending", f"[AUDIO] Activación pendiente: {exc}")

      try:
        task = self._tasks.get(timeout=1)
      except queue.Empty:
        continue

      if task == "stop":
        return
      if task == "deactivate":
        self._deactivate()
        return
      if isinstance(task, tuple) and task[0] == "default_changed":
        self._handle_default_change(task[1])

  def _activate_input_if_enabled(self):
    if not self.config or not self.config.get("change_input"):
      return

    headset_input = self.cached_headset_input
    if not headset_input or self._input_override:
      return

    if not self._input_owned:
      self._input_previous = self._capture_previous(
        "input",
        headset_input
      )

    current = self.get_default_input()
    if not all(
      value is not None and value.casefold() == headset_input.casefold()
      for value in current.values()
    ):
      self._activated_input = headset_input
      self.set_default_inputs(
        {
          "console": headset_input,
          "communications": headset_input
        },
        changed_roles=self._input_changed_roles
      )
      self._input_owned = bool(self._input_changed_roles)

  def _handle_default_change(self, flow):
    if self._closing:
      return

    try:
      if flow == "eRender" and self._output_owned and self._activated_output:
        current = self.get_default_output()

        if all(
          value is not None
          and value.casefold() == self._activated_output.casefold()
          for value in current.values()
        ):
          self._log_audio_state("notificación propia de salida")
          return

        if not self._is_endpoint_available(self._activated_output):
          self._log(
            "arctis_output_unavailable",
            "[AUDIO] La salida Arctis ha desaparecido o no está "
            "disponible; no se considera un cambio manual."
          )
          self._log_audio_state("salida Arctis no disponible")
          return

        # Windows puede haber restaurado el Arctis mientras
        # comprobábamos la disponibilidad del endpoint.
        current = self.get_default_output()

        if all(
          value is not None
          and value.casefold() == self._activated_output.casefold()
          for value in current.values()
        ):
          self._last_log_messages.pop("arctis_output_unavailable", None)
          self._log_audio_state("salida Arctis recuperada")
          return

        self._output_override = True
        self._output_owned = False
        self._output_changed_roles.clear()
        self._output_previous = None
        self._activated_output = None
        self._set_active_state(False)
        print("[AUDIO] Cambio manual de salida; se respeta.")
        self._log_audio_state("manual out")

      elif flow == "eCapture" and self._input_owned and self._activated_input:
        current = self.get_default_input()

        if all(
          value is not None
          and value.casefold() == self._activated_input.casefold()
          for value in current.values()
        ):
          return

        if not self._is_endpoint_available(self._activated_input):
          print(
            "[AUDIO] La entrada Arctis ha desaparecido o no está "
            "disponible; no se considera un cambio manual."
          )
          return

        # Aplicar la misma comprobación para evitar falsos cambios manuales.
        current = self.get_default_input()

        if all(
          value is not None
          and value.casefold() == self._activated_input.casefold()
          for value in current.values()
        ):
          return

        self._input_override = True
        self._input_owned = False
        self._input_changed_roles.clear()
        self._input_previous = None
        self._activated_input = None
        print("[AUDIO] Cambio manual de entrada detectado; se respeta.")

    except Exception as exc:
      print(f"[AUDIO] No se pudo verificar el cambio predeterminado: {exc}")

  def _is_endpoint_available(self, device_id):
    if not device_id:
      return False

    try:
      for device in AudioUtilities.GetAllDevices():
        if device.id.casefold() != device_id.casefold():
          continue

        try:
          return device._dev.GetState() == 1
        except Exception:
          return False

      return False

    except Exception as exc:
      print(
        f"[AUDIO] No se pudo comprobar la disponibilidad "
        f"del endpoint: {exc}"
      )
      return False

  def _restore_flow(self, flow):
    if flow == "output":
      owned = self._output_owned
      overridden = self._output_override
      changed_roles = self._output_changed_roles
      headset_id = self._activated_output
      previous = self._output_previous
      mode = self.get_output_mode()
      fixed_id = self.get_fixed_output()
    else:
      owned = self._input_owned
      overridden = self._input_override
      changed_roles = self._input_changed_roles
      headset_id = self._activated_input
      previous = self._input_previous
      mode = self.get_input_mode()
      fixed_id = self.get_fixed_input()

    if not owned or overridden or not headset_id:
      return

    if mode == "fixed":
      if not fixed_id:
        raise RuntimeError(f"No hay una entrada/salida fija configurada para {flow}.")
      target = {
        "console": fixed_id,
        "communications": fixed_id
      }
    else:
      target = previous

    if not target:
      self._clear_flow_state(flow)
      return

    current = self._get_default_devices(flow)
    pending = {}

    for key in changed_roles:
      current_id = current.get(key)
      target_id = target.get(key)

      # Restaurar solo los roles que siguen bajo control de la aplicación.
      if (
        current_id
        and target_id
        and current_id.casefold() == headset_id.casefold()
        and current_id.casefold() != target_id.casefold()
      ):
        pending[key] = target_id

    if pending:
      self._set_default_devices(flow, pending)
      restored = self._get_default_devices(flow)

      for key, target_id in pending.items():
        actual = restored.get(key)
        if not actual or actual.casefold() != target_id.casefold():
          raise RuntimeError(f"Windows no confirmó la restauración de {flow}/{key}.")

    self._clear_flow_state(flow)

  def _clear_flow_state(self, flow):
    if flow == "output":
      self._output_previous = None
      self._output_changed_roles.clear()
      self._output_owned = False
      self._activated_output = None
      self._output_override = False
    else:
      self._input_previous = None
      self._input_changed_roles.clear()
      self._input_owned = False
      self._activated_input = None
      self._input_override = False

  def _deactivate(self):
    if self._headset_is_ready():
      self._activate_until_done()
      return

    for flow in ("input", "output"):
      try:
        if flow == "input" and (
          not self.config or not self.config.get("change_input")
        ):
          continue

        self._restore_flow(flow)
      except Exception as exc:
        print(f"[AUDIO] Error restaurando {flow}: {exc}")

    # El ciclo de conexión ha terminado. Si el usuario había tomado
    # el control, limpiamos el indicador sin cambiar su dispositivo actual.
    if not self._headset_is_ready():
      self._output_override = False
      self._input_override = False

    if self.device:
      if self.device.headset_dongle_connected is False:
        self.device.set_headset_active(None)
      elif self.device.headset_connected is False:
        self.device.set_headset_active(False)
      else:
        self.device.set_headset_active(None)

  def _restore_on_shutdown(self):
    for flow in ("input", "output"):
      for attempt in range(1, 4):
        try:
          if flow == "input" and (
            not self.config or not self.config.get("change_input")
          ):
            break

          self._restore_flow(flow)
          break

        except Exception as exc:
          print(
            f"[AUDIO] Error restaurando {flow} al cerrar "
            f"({attempt}/3): {exc}"
          )
          if attempt < 3:
            threading.Event().wait(0.25)

  def _set_active_state(self, active):
    self._headset_active = active
    if self.device:
      self.device.set_headset_active(active)
  
  def _log(self, key, message):
    if self._last_log_messages.get(key) == message:
      return

    self._last_log_messages[key] = message
    print(message)

  def _log_audio_state(self, reason):
    try:
      defaults = self.get_default_output()
      console = defaults.get("console")
      comms = defaults.get("communications")
    except Exception as exc:
      console = comms = f"ERR:{exc}"

    state = self.device.get_state() if self.device is not None else {}

    # USB|Casco|Activo|Own|Manual|Con&Com
    message = (
      f"[DBG] {reason} => "
      f"{int(bool(state.get('headset_dongle_connected')))}|"
      f"{int(bool(state.get('headset_connected')))}|"
      f"{int(bool(state.get('headset_active')))}|"
      f"{int(self._output_owned)}|"
      f"{int(self._output_override)}|"
      f"{console}&{comms}"
    )

    self._log(f"audio_debug:{reason}", message)