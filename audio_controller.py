import queue
import threading

import comtypes
from pycaw.callbacks import MMNotificationClient
from pycaw.constants import EDataFlow, ERole
from pycaw.pycaw import AudioUtilities

from audio_discovery import AudioDiscovery


OUTPUT_ROLES = {
    "console": ERole.eConsole,
    "communications": ERole.eCommunications,
}

INPUT_ROLES = {
    "console": ERole.eConsole,
    "communications": ERole.eCommunications,
}


class _AudioNotificationClient(MMNotificationClient):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    def on_default_device_changed(
        self, flow, flow_id, role, role_id, default_device_id
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
            "headset_input": None,
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

        self._input_previous = None
        self._input_changed_roles = set()
        self._input_owned = False
        self._input_override = False
        self._activated_input = None

        self._headset_active = False
        self._last_log_messages = {}
        self._state_lock = threading.RLock()

    @property
    def output_overridden(self):
        with self._state_lock:
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

    # ------------------------------------------------------------------
    # Ciclo de vida y eventos del dispositivo físico
    # ------------------------------------------------------------------

    def bind_device(self, device):
        if self.device is not None:
            raise RuntimeError("Ya hay un dispositivo vinculado.")

        self.device = device
        device.add_listener("headset_connected", self._on_headset_connected)
        device.add_listener(
            "headset_disconnected", self._on_headset_disconnected
        )
        device.add_listener(
            "headset_dongle_disconnected", self._on_dongle_disconnected
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
            daemon=True,
        )
        self._worker.start()

        # Si el auricular ya estaba encendido al iniciar la app,
        # activarlo cuando el catálogo de audio esté disponible.
        if self.discovery_ready and self._headset_is_ready():
            self.request_activation()

    def stop(self, timeout=5):
        self._closing = True
        self._tasks.put("stop")

        worker = self._worker
        if (
            worker
            and worker.is_alive()
            and worker is not threading.current_thread()
        ):
            worker.join(timeout=max(0, timeout))

        stopped = worker is None or not worker.is_alive()

        if self.device is not None:
            self.device.remove_listener(
                "headset_connected", self._on_headset_connected
            )
            self.device.remove_listener(
                "headset_disconnected", self._on_headset_disconnected
            )
            self.device.remove_listener(
                "headset_dongle_disconnected", self._on_dongle_disconnected
            )
            self.device = None

        if not stopped:
            print("[AUDIO] El trabajador no terminó dentro del plazo.")

        return stopped

    def refresh_outputs(self):
        """Actualiza el catálogo usando AudioDiscovery."""
        try:
            catalog = self.discovery.get_catalog(refresh=True)

            self._catalog = {
                "outputs": catalog["active_outputs"],
                "inputs": catalog["active_inputs"],
                "headset_output": (
                    catalog["headset_output"]["id"]
                    if catalog.get("headset_output")
                    else None
                ),
                "headset_input": (
                    catalog["headset_input"]["id"]
                    if catalog.get("headset_input")
                    else None
                ),
            }
            self.discovery_error = None
            self.discovery_ready = True

            print(
                f"[AUDIO] Catálogo actualizado: "
                f"{len(self._catalog['outputs'])} salidas, "
                f"{len(self._catalog['inputs'])} entradas."
            )

        except Exception as exc:
            self.discovery_error = str(exc)
            self.discovery_ready = False
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
            "error": self.discovery_error,
        }

    def request_activation(self):
        if (
            not self._closing
            and self.discovery_ready
            and self.discovery_error is None
        ):
            self._tasks.put("activate")

    def _on_headset_connected(self, device, **data):
        # Un nuevo ciclo físico permite volver a tomar el control.
        with self._state_lock:
            self._output_override = False
            self._input_override = False
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

                try:
                    if task == "activate":
                        self._activate()
                    elif task == "deactivate":
                        self._deactivate()
                    elif (
                        isinstance(task, tuple)
                        and task[0] == "default_changed"
                    ):
                        self._handle_default_change(task[1])
                except Exception as exc:
                    # Un error de un evento no debe matar el worker.
                    print(f"[AUDIO] Error procesando tarea {task}: {exc}")

        except Exception as exc:
            print(f"[AUDIO] Error iniciando el trabajador: {exc}")
        finally:
            # No restauramos automáticamente al cerrar la app:
            # cerrar la aplicación no debe pisar una elección manual.
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
                print(f"[AUDIO] Error retirando notificaciones: {exc}")

        self._observer = None
        self._enumerator = None
        self._observer_ready = False

    def _headset_is_ready(self):
        return bool(
            not self._closing
            and self.device is not None
            and self.device.headset_dongle_connected is True
            and self.device.headset_connected is True
        )

    # ------------------------------------------------------------------
    # Dispositivos predeterminados de Windows
    # ------------------------------------------------------------------

    def _get_default_devices(self, flow):
        if flow == "output":
            data_flow = EDataFlow.eRender
            roles = OUTPUT_ROLES
        else:
            data_flow = EDataFlow.eCapture
            roles = INPUT_ROLES

        enumerator = AudioUtilities.GetDeviceEnumerator()
        result = {}

        for name, role in roles.items():
            try:
                endpoint = enumerator.GetDefaultAudioEndpoint(
                    data_flow.value, role.value
                )
                result[name] = endpoint.GetId()
            except Exception:
                result[name] = None

        if not any(result.values()):
            raise RuntimeError(
                f"Windows no tiene un dispositivo {flow} predeterminado."
            )

        return result

    def get_default_output(self):
        return self._get_default_devices("output")

    def get_default_input(self):
        return self._get_default_devices("input")

    def _set_default_devices(self, flow, devices, changed_roles=None):
        roles = OUTPUT_ROLES if flow == "output" else INPUT_ROLES

        for name, device_id in devices.items():
            if name not in roles or not device_id:
                continue

            current = self._get_default_devices(flow).get(name)
            if current and current.casefold() == device_id.casefold():
                continue

            AudioUtilities.SetDefaultDevice(
                device_id, roles=[roles[name]]
            )
            if changed_roles is not None:
                changed_roles.add(name)

    def set_default_outputs(self, outputs, changed_roles=None):
        self._set_default_devices(
            "output", outputs, changed_roles=changed_roles
        )

    def set_default_inputs(self, inputs, changed_roles=None):
        self._set_default_devices(
            "input", inputs, changed_roles=changed_roles
        )

    def is_output_default(self, device_id):
        current = self.get_default_output()
        return all(
            value is not None and value.casefold() == device_id.casefold()
            for value in current.values()
        )

    # ------------------------------------------------------------------
    # Configuración: usar exclusivamente las claves de ConfigManager
    # ------------------------------------------------------------------

    def get_output_mode(self):
        return "fixed" if self.get_fixed_output() else "automatic"

    def get_fixed_output(self):
        return self.config.get("output_fixed") if self.config else None

    def get_input_mode(self):
        return "fixed" if self.get_fixed_input() else "automatic"

    def get_fixed_input(self):
        return self.config.get("input_fixed") if self.config else None

    def _input_change_enabled(self):
        return bool(
            self.config and self.config.get("input_change_enabled")
        )

    # ------------------------------------------------------------------
    # Activación
    # ------------------------------------------------------------------

    def _capture_previous(self, flow, headset_id):
        current = self._get_default_devices(flow)

        if all(
            value is not None and value.casefold() == headset_id.casefold()
            for value in current.values()
        ):
            return None

        return current

    def _activate(self):
        if not self._headset_is_ready():
            return
        if not self.discovery_ready or self.discovery_error is not None:
            return
        if not self._observer_ready:
            print("[AUDIO] No se puede activar sin observar cambios de Windows.")
            return

        headset_output = self.cached_headset_output
        if not headset_output:
            print("[AUDIO] La salida física de Arctis no está disponible.")
            return

        with self._state_lock:
            if self._output_override:
                self._set_active_state(False)
                return

            if not self._output_owned:
                self._output_previous = self._capture_previous(
                    "output", headset_output
                )

            self._activated_output = headset_output
            self.set_default_outputs(
                {
                    "console": headset_output,
                    "communications": headset_output,
                },
                changed_roles=self._output_changed_roles,
            )
            self._output_owned = bool(self._output_changed_roles)

        if not self._headset_is_ready():
            self._deactivate()
            return

        if not self.is_output_default(headset_output):
            print("[AUDIO] Windows no confirmó la salida Arctis.")
            return

        self._set_active_state(True)
        self._activate_input_if_enabled()
        self._log_audio_state("activación confirmada")

    def _activate_input_if_enabled(self):
        if not self._input_change_enabled() or self._input_override:
            return

        headset_input = self.cached_headset_input
        if not headset_input:
            return

        if not self._input_owned:
            self._input_previous = self._capture_previous(
                "input", headset_input
            )

        self._activated_input = headset_input
        self.set_default_inputs(
            {
                "console": headset_input,
                "communications": headset_input,
            },
            changed_roles=self._input_changed_roles,
        )
        self._input_owned = bool(self._input_changed_roles)

    # ------------------------------------------------------------------
    # Detección de cambios manuales
    # ------------------------------------------------------------------

    def _handle_default_change(self, flow):
        if self._closing:
            return

        try:
            if flow == "eRender":
                self._handle_output_change()
            elif flow == "eCapture":
                self._handle_input_change()
        except Exception as exc:
            print(f"[AUDIO] No se pudo verificar el cambio de audio: {exc}")

    def _handle_output_change(self):
        current = self.get_default_output()
        headset_id = self.cached_headset_output

        if not self._headset_is_ready():
            return

        # Si el usuario vuelve a elegir Arctis, termina la anulación.
        if headset_id and all(
            value is not None and value.casefold() == headset_id.casefold()
            for value in current.values()
        ):
            with self._state_lock:
                if self._output_override:
                    self._output_override = False
                    self._output_previous = self._output_previous or None
                    self._output_changed_roles.clear()
                    self._output_owned = True
                    self._activated_output = headset_id
            self._set_active_state(True)
            self._activate_input_if_enabled()
            self._log_audio_state("Arctis seleccionada manualmente")
            return

        # Ignorar notificaciones si no estamos gestionando la salida.
        if not self._output_owned or not self._activated_output:
            return

        if not self._is_endpoint_available(self._activated_output):
            # La desaparición del endpoint no es una elección manual.
            return

        # La salida cambió a otro dispositivo disponible: respetarlo.
        with self._state_lock:
            self._output_override = True
            self._output_owned = False
            self._output_previous = current
            self._output_changed_roles.clear()
            self._activated_output = None

        self._set_active_state(False)
        print("[AUDIO] Cambio manual de salida; se respeta.")
        self._log_audio_state("anulación manual de salida")

    def _handle_input_change(self):
        if not self._input_change_enabled():
            return

        current = self.get_default_input()
        headset_id = self.cached_headset_input

        if not self._headset_is_ready() or not headset_id:
            return

        if all(
            value is not None and value.casefold() == headset_id.casefold()
            for value in current.values()
        ):
            with self._state_lock:
                if self._input_override:
                    self._input_override = False
                    self._input_changed_roles.clear()
                    self._input_owned = True
                    self._activated_input = headset_id
            return

        if not self._input_owned or not self._activated_input:
            return

        if not self._is_endpoint_available(self._activated_input):
            return

        with self._state_lock:
            self._input_override = True
            self._input_owned = False
            self._input_previous = current
            self._input_changed_roles.clear()
            self._activated_input = None

        print("[AUDIO] Cambio manual de entrada; se respeta.")

    def _is_endpoint_available(self, device_id):
        if not device_id:
            return False

        try:
            for device in AudioUtilities.GetAllDevices():
                if device.id.casefold() == device_id.casefold():
                    return device._dev.GetState() == 1
            return False
        except Exception as exc:
            print(f"[AUDIO] No se pudo comprobar el endpoint: {exc}")
            return False

    # ------------------------------------------------------------------
    # Restauración al apagar o desconectar
    # ------------------------------------------------------------------

    def _restore_flow(self, flow):
        if flow == "output":
            owned = self._output_owned
            overridden = self._output_override
            changed_roles = self._output_changed_roles
            headset_id = self._activated_output
            previous = self._output_previous
            fixed_id = self.get_fixed_output()
        else:
            owned = self._input_owned
            overridden = self._input_override
            changed_roles = self._input_changed_roles
            headset_id = self._activated_input
            previous = self._input_previous
            fixed_id = self.get_fixed_input()

        if not owned or overridden or not headset_id:
            return

        # Una salida fija configurada tiene prioridad, pero solo si
        # aparece como endpoint disponible en el catálogo actual.
        target = None
        if fixed_id and self._catalog_contains(flow, fixed_id):
            target = {
                "console": fixed_id,
                "communications": fixed_id,
            }

        if target is None:
            target = previous

        if not target:
            self._clear_flow_state(flow)
            return

        current = self._get_default_devices(flow)
        pending = {}

        for role in tuple(changed_roles):
            current_id = current.get(role)
            target_id = target.get(role)

            # No pisar un cambio externo posterior a nuestra activación.
            if (
                current_id
                and target_id
                and current_id.casefold() == headset_id.casefold()
                and current_id.casefold() != target_id.casefold()
            ):
                pending[role] = target_id

        if pending:
            self._set_default_devices(flow, pending)
            restored = self._get_default_devices(flow)

            for role, target_id in pending.items():
                actual = restored.get(role)
                if not actual or actual.casefold() != target_id.casefold():
                    raise RuntimeError(
                        f"Windows no confirmó la restauración {flow}/{role}."
                    )

        self._clear_flow_state(flow)

    def _catalog_contains(self, flow, device_id):
        entries = self._catalog["outputs" if flow == "output" else "inputs"]
        return any(
            item.get("id")
            and item["id"].casefold() == device_id.casefold()
            for item in entries
        )

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
        # Una desconexión real cierra el ciclo. Si hubo anulación manual,
        # no restauramos nada: dejamos intacta la elección del usuario.
        if self._headset_is_ready():
            self._activate()
            return

        for flow in ("input", "output"):
            if flow == "input" and not self._input_change_enabled():
                continue
            try:
                self._restore_flow(flow)
            except Exception as exc:
                print(f"[AUDIO] Error restaurando {flow}: {exc}")

        with self._state_lock:
            self._output_override = False
            self._input_override = False

        if self.device:
            if self.device.headset_dongle_connected is False:
                self.device.set_headset_active(None)
            elif self.device.headset_connected is False:
                self.device.set_headset_active(False)
            else:
                self.device.set_headset_active(None)

    def _set_active_state(self, active):
        self._headset_active = active
        if self.device:
            self.device.set_headset_active(active)

    def _log_audio_state(self, reason):
        try:
            defaults = self.get_default_output()
            console = defaults.get("console")
            comms = defaults.get("communications")
        except Exception as exc:
            console = comms = f"ERR:{exc}"

        state = self.device.get_state() if self.device else {}
        message = (
            f"[DBG] {reason} => "
            f"{int(bool(state.get('headset_dongle_connected')))}|"
            f"{int(bool(state.get('headset_connected')))}|"
            f"{int(bool(state.get('headset_active')))}|"
            f"{int(self._output_owned)}|"
            f"{int(self._output_override)}|{console}&{comms}"
        )

        if self._last_log_messages.get(reason) != message:
            self._last_log_messages[reason] = message
            print(message)
