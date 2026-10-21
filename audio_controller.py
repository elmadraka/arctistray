import queue
import threading

from audio_discovery import AudioDiscovery


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
    self._discovery_listeners_registered = False
    self._discovery_monitor_ready = False

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

  def _register_discovery_listeners(self):
    if self._discovery_listeners_registered:
      return

    self.discovery.add_listener("monitoring_started", self._on_discovery_monitoring_started)
    self.discovery.add_listener("monitoring_stopped", self._on_discovery_monitoring_stopped)
    self.discovery.add_listener("default_device_changed", self._on_default_device_changed)
    self.discovery.add_listener("catalog_changed", self._on_catalog_changed)
    self.discovery.add_listener("discovery_error", self._on_discovery_error)
    self._discovery_listeners_registered = True

  def _unregister_discovery_listeners(self):
    if not self._discovery_listeners_registered:
      return

    self.discovery.remove_listener("monitoring_started", self._on_discovery_monitoring_started)
    self.discovery.remove_listener("monitoring_stopped", self._on_discovery_monitoring_stopped)
    self.discovery.remove_listener("default_device_changed", self._on_default_device_changed)
    self.discovery.remove_listener("catalog_changed", self._on_catalog_changed)
    self.discovery.remove_listener("discovery_error", self._on_discovery_error)
    self._discovery_listeners_registered = False

  def _on_discovery_monitoring_started(self, discovery, **payload):
    self._discovery_monitor_ready = True

    if self._headset_is_ready() and self.discovery_ready:
      self.request_activation()

  def _on_discovery_monitoring_stopped(self, discovery, **payload):
    self._discovery_monitor_ready = False

  def _on_default_device_changed(
    self,
    discovery,
    flow,
    role,
    endpoint_id,
    **payload,
  ):
    if flow in ("output", "input"):
      self._queue_default_change(flow)

  def _on_catalog_changed(self, discovery, devices, **payload):
    try:
      catalog = discovery.get_catalog()
      self._update_catalog(catalog)
    except Exception as exc:
      self.discovery_error = str(exc)
      print(f"[AUDIO] Error updating the audio catalog: {exc}")
      return

    if self._headset_is_ready():
      self.request_activation()

  def _on_discovery_error(self, discovery, error, **payload):
    self.discovery_error = error
    print(f"[AUDIO] Audio discovery error: {error}")

  def bind_device(self, device):
    if self.device is not None:
      raise RuntimeError("A device is already bound.")

    self.device = device
    device.add_listener("headset_connected", self._on_headset_connected)
    device.add_listener("headset_disconnected", self._on_headset_disconnected)
    device.add_listener("headset_dongle_disconnected", self._on_dongle_disconnected)

  def start(self):
    if self.device is None:
      raise RuntimeError("A device must be bound before starting.")

    if self._worker and self._worker.is_alive():
      return

    # Remove stale tasks left by a previous worker lifecycle.
    while True:
      try:
        self._tasks.get_nowait()
      except queue.Empty:
        break

    self._closing = False
    self._register_discovery_listeners()

    self._worker = threading.Thread(
      target=self._worker_loop,
      name="ArctisTrayAudioWorker",
      daemon=True,
    )
    self._worker.start()

    try:
      self.discovery.start()
    except Exception:
      self._closing = True
      self._tasks.put("stop")
      self._worker.join(timeout=5)
      self._unregister_discovery_listeners()
      raise

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

    worker_stopped = (
      worker is None
      or not worker.is_alive()
      or worker is threading.current_thread()
    )

    # Restore devices only after the worker can no longer change them.
    if worker_stopped:
      for flow in ("input", "output"):
        if flow == "input" and not self._input_change_enabled():
          continue

        try:
          self._restore_flow(flow)
        except Exception as exc:
          print(f"[AUDIO] Error restoring {flow} during shutdown: {exc}")

    discovery_stopped = self.discovery.stop(timeout=timeout)
    stopped = discovery_stopped and worker_stopped

    self._unregister_discovery_listeners()
    self._discovery_monitor_ready = False

    if self.device is not None:
      self.device.remove_listener("headset_connected", self._on_headset_connected)
      self.device.remove_listener("headset_disconnected", self._on_headset_disconnected)
      self.device.remove_listener("headset_dongle_disconnected", self._on_dongle_disconnected)
      self.device = None

    if not stopped:
      print("[AUDIO] A worker did not stop within the timeout.")

    return stopped

  def _update_catalog(self, catalog):
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
      f"[AUDIO] Catalog updated: "
      f"{len(self._catalog['outputs'])} outputs, "
      f"{len(self._catalog['inputs'])} inputs."
    )

  def refresh_outputs(self):
    """Refresh the catalog through AudioDiscovery."""
    try:
      catalog = self.discovery.get_catalog(refresh=True)
      self._update_catalog(catalog)
    except Exception as exc:
      self.discovery_error = str(exc)
      self.discovery_ready = False
      print(f"[AUDIO] Error discovering audio devices: {exc}")
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
    # A new physical connection cycle allows the controller to take control.
    with self._state_lock:
      self._output_override = False
      self._input_override = False

    self.request_activation()

  def _on_headset_disconnected(self, device, **data):
    if not self._closing:
      self._tasks.put("deactivate")

  def _on_dongle_disconnected(self, device, **data):
    if not self._closing:
      self._tasks.put("deactivate")

  def _queue_default_change(self, flow):
    if not self._closing:
      self._tasks.put(("default_changed", flow))

  def _worker_loop(self):
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
        print(f"[AUDIO] Error processing task {task}: {exc}")

  def _headset_is_ready(self):
    return bool(
      not self._closing
      and self.device is not None
      and self.device.headset_dongle_connected is True
      and self.device.headset_connected is True
    )

  # ------------------------------------------------------------------
  # Windows default devices
  # ------------------------------------------------------------------

  def _get_default_devices(self, flow):
    devices = self.discovery.get_default_devices(flow)
    return {
      role: devices.get(role)
      for role in ("console", "communications")
    }

  def get_default_output(self):
    return self._get_default_devices("output")

  def get_default_input(self):
    return self._get_default_devices("input")

  def _set_default_devices(self, flow, devices, changed_roles=None):
    requested = {
      role: device_id
      for role, device_id in devices.items()
      if role in ("console", "communications") and device_id
    }
    if not requested:
      return

    before = self._get_default_devices(flow)
    self.discovery.set_default_devices(flow, requested)
    after = self._get_default_devices(flow)

    if changed_roles is not None:
      for role, device_id in requested.items():
        previous_id = before.get(role)
        actual_id = after.get(role)

        if (
          actual_id
          and actual_id.casefold() == device_id.casefold()
          and (
            not previous_id
            or previous_id.casefold() != actual_id.casefold()
          )
        ):
          changed_roles.add(role)

  def set_default_outputs(self, outputs, changed_roles=None):
    self._set_default_devices("output", outputs, changed_roles=changed_roles)

  def set_default_inputs(self, inputs, changed_roles=None):
    self._set_default_devices("input", inputs, changed_roles=changed_roles)

  def is_output_default(self, device_id):
    if not device_id:
      return False

    current = self.get_default_output()
    return all(
      value is not None and value.casefold() == device_id.casefold()
      for value in current.values()
    )

  # ------------------------------------------------------------------
  # Configuration
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
    return bool(self.config and self.config.get("input_change_enabled"))

  # ------------------------------------------------------------------
  # Activation
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

    if not self._discovery_monitor_ready:
      print("[AUDIO] Cannot activate without Windows audio monitoring.")
      return

    headset_output = self.cached_headset_output
    if not headset_output:
      print("[AUDIO] The Arctis output endpoint is unavailable.")
      return

    with self._state_lock:
      if self._output_override:
        self._set_active_state(False)
        return

      if not self._output_owned:
        self._output_previous = self._capture_previous("output", headset_output)

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
      print("[AUDIO] Windows did not confirm the Arctis output.")
      return

    self._set_active_state(True)
    self._activate_input_if_enabled()
    self._log_audio_state("activation confirmed")

  def _activate_input_if_enabled(self):
    if not self._input_change_enabled() or self._input_override:
      return

    headset_input = self.cached_headset_input
    if not headset_input:
      return

    if not self._input_owned:
      self._input_previous = self._capture_previous("input", headset_input)

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
  # Manual default-device changes
  # ------------------------------------------------------------------

  def _handle_default_change(self, flow):
    if self._closing:
      return

    try:
      if flow == "output":
        self._handle_output_change()
      elif flow == "input":
        self._handle_input_change()
    except Exception as exc:
      print(f"[AUDIO] Failed to verify the audio change: {exc}")

  def _handle_output_change(self):
    current = self.get_default_output()
    headset_id = self.cached_headset_output

    if not self._headset_is_ready():
      return

    # If the user selects Arctis again, clear the manual override.
    if headset_id and all(
      value is not None and value.casefold() == headset_id.casefold()
      for value in current.values()
    ):
      with self._state_lock:
        if self._output_override:
          self._output_override = False
          self._output_changed_roles.clear()
          self._output_owned = True
          self._activated_output = headset_id

      self._set_active_state(True)
      self._activate_input_if_enabled()
      self._log_audio_state("Arctis selected manually")
      return

    # Ignore notifications when the controller does not own the output.
    if not self._output_owned or not self._activated_output:
      return

    if not self._is_endpoint_available(self._activated_output):
      # Endpoint disappearance is not treated as a manual selection.
      return

    # Respect a change to another available output device.
    with self._state_lock:
      self._output_override = True
      self._output_owned = False
      self._output_previous = current
      self._output_changed_roles.clear()
      self._activated_output = None

    self._set_active_state(False)
    print("[AUDIO] Manual output change detected; respecting user choice.")
    self._log_audio_state("manual output override")

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

    print("[AUDIO] Manual input change detected; respecting user choice.")

  def _is_endpoint_available(self, device_id):
    if not device_id:
      return False

    try:
      return self.discovery.is_endpoint_available(device_id)
    except Exception as exc:
      print(f"[AUDIO] Failed to check endpoint availability: {exc}")
      return False

  # ------------------------------------------------------------------
  # Restore default devices on shutdown or disconnection
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

    # A configured fixed device takes priority if it is in the current catalog.
    target = None
    if fixed_id and self._catalog_contains(flow, fixed_id):
      target = { "console": fixed_id, "communications": fixed_id }

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

      # Do not overwrite an external change made after activation.
      if (
        current_id
        and target_id
        and self._catalog_contains(flow, target_id)
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
          raise RuntimeError(f"Windows did not confirm restoration of {flow}/{role}.")

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
    # If the headset is still ready, keep it active.
    if self._headset_is_ready():
      self._activate()
      return

    for flow in ("input", "output"):
      if flow == "input" and not self._input_change_enabled():
        continue

      try:
        self._restore_flow(flow)
      except Exception as exc:
        print(f"[AUDIO] Error restoring {flow}: {exc}")

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
