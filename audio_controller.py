"""Coordinate Arctis audio endpoints and Windows audio devices."""

import queue
import threading
import time

from dataclasses import dataclass, replace

from audio_discovery import AudioDiscovery


_STOP_SENTINEL = object()


@dataclass(frozen=True, slots=True)
class ArctisAudioEndpoints:
  output_id: str | None = None
  input_id: str | None = None


@dataclass(frozen=True, slots=True)
class DefaultAudioDevices:
  console: str | None = None
  multimedia: str | None = None
  communications: str | None = None


@dataclass(frozen=True, slots=True)
class AudioControllerState:
  status: str = "initializing"
  error: str | None = None
  arctis_endpoints: ArctisAudioEndpoints = ArctisAudioEndpoints()
  default_output: DefaultAudioDevices = DefaultAudioDevices()
  default_input: DefaultAudioDevices = DefaultAudioDevices()
  manual_output_override: bool = False
  manual_input_override: bool = False


class AudioController:
  """Coordinate ArctisDevice and Windows audio discovery."""

  DISCOVERY_EVENTS = (
    "refresh_finished",
    "default_device_changed",
    "discovery_error",
  )

  PENDING_DEFAULT_DEVICE_CHANGE_TIMEOUT = 5.0

  def __init__(self, arctis_device, discovery=None):
    self._arctis_device = arctis_device
    self._discovery = (
      discovery if discovery is not None else AudioDiscovery()
    )

    self._state = AudioControllerState()
    self._arctis_pnp_ids = {
      "output": None,
      "input": None,
    }
    self._catalog = None
    self._default_devices_loaded = set()
    self._original_output_devices = None
    self._pending_default_device_changes = {}
    self._state_lock = threading.RLock()

    self._listeners = []
    self._listener_lock = threading.RLock()

    self._event_queue = queue.Queue()
    self._worker = None
    self._started = False
    self._stopped = False

    self._lifecycle_lock = threading.RLock()

    self._discovery_callbacks = {
      "refresh_finished": self._on_refresh_finished,
      "default_device_changed": self._on_default_device_changed,
      "discovery_error": self._on_discovery_error,
    }

    self._arctis_device.add_listener(self.on_device_event)

  @property
  def state(self):
    with self._state_lock:
      return self._state

  def get_status(self):
    return self.state

  def add_listener(self, callback):
    if not callable(callback):
      raise TypeError("callback must be callable")

    with self._listener_lock:
      if callback not in self._listeners:
        self._listeners.append(callback)

  def remove_listener(self, callback):
    with self._listener_lock:
      if callback in self._listeners:
        self._listeners.remove(callback)

  def _emit(self, event, **payload):
    with self._listener_lock:
      callbacks = tuple(self._listeners)

    for callback in callbacks:
      try:
        callback(self, event, **payload)
      except Exception as exc:
        print(
          f"[AudioController] Listener failed for {event}: {exc}"
        )

  def _set_state(self, **changes):
    with self._state_lock:
      new_state = replace(self._state, **changes)

      if new_state == self._state:
        return False

      self._state = new_state
      return True

  def _enqueue_event(self, event, payload):
    with self._lifecycle_lock:
      if not self._started or self._stopped:
        return

      self._event_queue.put((event, payload))

  def on_device_event(self, event):
    """Receive an ArctisDevice event without blocking its consumer."""
    self._enqueue_event("device_event", {"event": event})

  def _on_refresh_finished(self, discovery, **payload):
    self._enqueue_event("refresh_finished", payload)

  def _on_default_device_changed(self, discovery, **payload):
    self._enqueue_event("default_device_changed", payload)

  def _on_discovery_error(self, discovery, **payload):
    self._enqueue_event("discovery_error", payload)

  def _register_discovery_listeners(self):
    for event, callback in self._discovery_callbacks.items():
      self._discovery.add_listener(event, callback)

  def _unregister_discovery_listeners(self):
    for event, callback in self._discovery_callbacks.items():
      self._discovery.remove_listener(event, callback)

  def _restore_original_output_devices(self):
    """Restore the original output devices if there was no manual override."""
    original_devices = self._original_output_devices

    if original_devices is None:
      return

    try:
      if not self.state.manual_output_override:
        self._switch_output({
          "console": original_devices.console,
          "multimedia": original_devices.multimedia,
          "communications": original_devices.communications,
        })
    finally:
      self._original_output_devices = None

  def _save_original_output_devices(self):
    """Save the current default output devices before automatic switching."""
    if "output" not in self._default_devices_loaded:
      return

    if self._original_output_devices is not None:
      return

    self._original_output_devices = replace(
      self.state.default_output,
    )

  def start(self):
    """Start the controller worker and audio discovery."""
    with self._lifecycle_lock:
      if self._started:
        return

      if self._stopped:
        raise RuntimeError("AudioController cannot be restarted.")

      self._set_state(
        status="starting",
        error=None,
      )

      self._register_discovery_listeners()

      self._worker = threading.Thread(
        target=self._consume_events,
        name="ArctisTrayAudioController",
        daemon=True,
      )

      try:
        self._worker.start()
        self._started = True
        self._discovery.start()

      except Exception as exc:
        self._stopped = True
        self._arctis_device.remove_listener(self.on_device_event)
        self._unregister_discovery_listeners()

        if self._worker.is_alive():
          self._event_queue.put(_STOP_SENTINEL)
          self._worker.join(5)

        self._set_state(
          status="error",
          error=str(exc),
        )
        raise

  def stop(self, timeout=5):
    """Stop audio discovery and the controller worker."""
    with self._lifecycle_lock:
      if self._stopped:
        return True

      self._stopped = True
      self._arctis_device.remove_listener(self.on_device_event)

      if not self._started:
        self._set_state(status="stopped")
        return True

      self._unregister_discovery_listeners()

    self._restore_original_output_devices()

    discovery_stopped = self._discovery.stop(timeout)

    self._event_queue.put(_STOP_SENTINEL)

    worker = self._worker

    if worker is not threading.current_thread():
      worker.join(max(0, timeout))

    worker_stopped = not worker.is_alive()

    if discovery_stopped and worker_stopped:
      self._set_state(status="stopped")
      return True

    self._set_state(
      status="error",
      error="AudioController failed to stop cleanly.",
    )
    return False

  def _process_initial_device_state(self):
    """Process the current ArctisDevice state."""
    device_state = self._arctis_device.state

    if device_state.headset_on == "on":
      self._set_state(manual_output_override=False)
      self._save_original_output_devices()
      self._switch_output({
        "console": self.state.arctis_endpoints.output_id,
        "multimedia": self.state.arctis_endpoints.output_id,
        "communications": self.state.arctis_endpoints.output_id,
      })

  def _consume_events(self):
    while True:
      item = self._event_queue.get()

      if item is _STOP_SENTINEL:
        break

      event, payload = item
      self._process_event(event, payload)

  def _process_event(self, event, payload):
    if event == "refresh_finished":
      self._process_refresh_finished(payload)
    elif event == "default_device_changed":
      self._process_default_device_changed(payload)
    elif event == "discovery_error":
      self._process_discovery_error(payload)
    elif event == "device_event":
      self._process_device_event(payload["event"])

  def _process_refresh_finished(self, payload):
    status = payload.get("status")
    error = payload.get("error")

    if status == "ready":
      changed = self._set_state(
        status="ready",
        error=None,
      )

      catalog = payload.get("catalog")

      if catalog is not None:
        self._catalog = catalog

        device_state = self._arctis_device.state
        pnp_device = device_state.pnp_device

        if pnp_device is None:
          self._arctis_pnp_ids = {
            "output": None,
            "input": None,
          }
        else:
          self._arctis_pnp_ids = {
            "output": pnp_device.audio_render_instance_id,
            "input": pnp_device.audio_capture_instance_id,
          }

        self._resolve_arctis_endpoints()
        self._load_default_devices()
        self._process_initial_device_state()

        self._emit(
          "audio_devices_changed",
          catalog=catalog,
        )

      if changed:
        self._emit(
          "status_changed",
          status="ready",
          error=None,
        )

    elif status == "error":
      self._process_discovery_error({"error": error})

  def _load_default_devices(self):
    """Load the current Windows default audio devices."""
    defaults = {}

    for flow in ("output", "input"):
      if flow in self._default_devices_loaded:
        continue

      try:
        devices = self._discovery.get_default_devices(flow)
      except Exception as exc:
        self._emit(
          "error",
          error=f"Failed to load default {flow} devices: {exc}",
        )
        continue

      defaults[flow] = DefaultAudioDevices(
        console=devices.get("console"),
        multimedia=devices.get("multimedia"),
        communications=devices.get("communications"),
      )

      self._default_devices_loaded.add(flow)

    changes = {}

    if "output" in defaults:
      changes["default_output"] = defaults["output"]

    if "input" in defaults:
      changes["default_input"] = defaults["input"]

    if changes:
      self._set_state(**changes)

      self._emit(
        "default_devices_initialized",
        output=self.state.default_output,
        input=self.state.default_input,
      )

  def _remove_expired_pending_default_device_changes(self, now=None):
    """Remove pending changes whose notification timeout has expired."""
    if now is None:
      now = time.monotonic()

    expired = [
      change
      for change, deadline in self._pending_default_device_changes.items()
      if deadline <= now
    ]

    for change in expired:
      self._pending_default_device_changes.pop(change, None)

  def _process_default_device_changed(self, payload):
    flow = payload.get("flow")
    role = payload.get("role")
    endpoint_id = payload.get("endpoint_id")

    if flow not in ("output", "input"):
      return

    if role not in (
      "console",
      "multimedia",
      "communications",
    ):
      return

    self._remove_expired_pending_default_device_changes()

    change = (flow, role, endpoint_id)
    deadline = self._pending_default_device_changes.get(change)

    if deadline is None and flow == "output":
      self._set_state(manual_output_override=True)

    field = "default_output" if flow == "output" else "default_input"

    with self._state_lock:
      defaults = getattr(self._state, field)
      updated_defaults = replace(
        defaults,
        **{role: endpoint_id},
      )

      if updated_defaults == defaults:
        return

      self._state = replace(
        self._state,
        **{field: updated_defaults},
      )

    self._emit(
      "default_device_changed",
      flow=flow,
      role=role,
      endpoint_id=endpoint_id,
    )

  def _process_discovery_error(self, payload):
    error = payload.get("error", "Unknown audio discovery error.")

    changed = self._set_state(
      status="error",
      error=str(error),
    )

    if changed:
      self._emit(
        "status_changed",
        status="error",
        error=str(error),
      )

    self._emit("error", error=str(error))

  def _update_arctis_pnp_ids(self, event):
    """Update cached Arctis PnP instance IDs from a device snapshot."""
    state = getattr(event, "state", None)
    pnp_device = getattr(state, "pnp_device", None)

    if pnp_device is None:
      self._arctis_pnp_ids = {
        "output": None,
        "input": None,
      }
    else:
      self._arctis_pnp_ids = {
        "output": pnp_device.audio_render_instance_id,
        "input": pnp_device.audio_capture_instance_id,
      }

    self._resolve_arctis_endpoints()


  def _resolve_arctis_endpoints(self):
    """Resolve Arctis PnP IDs to available Windows audio endpoints."""
    resolved = {
      "output": None,
      "input": None,
    }

    if self._catalog is not None:
      endpoint_lists = {
        "output": self._catalog.get("outputs", []),
        "input": self._catalog.get("inputs", []),
      }

      for flow, devices in endpoint_lists.items():
        expected_id = self._arctis_pnp_ids[flow]

        if not expected_id:
          continue

        for device in devices:
          pnp_instance_id = device.get("pnp_instance_id")

          if not device.get("available"):
            continue

          if not pnp_instance_id:
            continue

          if pnp_instance_id.casefold() != expected_id.casefold():
            continue

          resolved[flow] = device.get("id")
          break

    endpoints = ArctisAudioEndpoints(
      output_id=resolved["output"],
      input_id=resolved["input"],
    )

    changed = self._set_state(arctis_endpoints=endpoints)

    if changed:
      self._emit(
        "arctis_endpoints_changed",
        endpoints=endpoints,
      )

  def _switch_output(self, devices):
    """Switch Windows output roles to the specified devices."""
    if not devices or self.state.manual_output_override:
      return

    pending = {
      ("output", role, endpoint_id)
      for role, endpoint_id in devices.items()
    }

    now = time.monotonic()
    deadline = now + self.PENDING_DEFAULT_DEVICE_CHANGE_TIMEOUT

    self._remove_expired_pending_default_device_changes(now)

    for change in pending:
      self._pending_default_device_changes[change] = deadline

    try:
      changed_roles = self._discovery.set_default_devices("output", devices)
    except Exception as exc:
      for change in pending:
        self._pending_default_device_changes.pop(change, None)
      self._emit("error", error=f"Failed to switch output devices: {exc}")
      return

    for role, endpoint_id in devices.items():
      if role not in changed_roles:
        self._pending_default_device_changes.pop(
          ("output", role, endpoint_id),
          None,
        )

  def _process_device_event(self, event):
    """Process an ArctisDevice event."""
    if event.event_type == "headset_power_changed":
      if event.state.headset_on == "on":
        if (
          "output" not in self._default_devices_loaded
          or not self.state.arctis_endpoints.output_id
        ):
          return

        self._set_state(manual_output_override=False)
        self._save_original_output_devices()
        self._switch_output({
          "console": self.state.arctis_endpoints.output_id,
          "multimedia": self.state.arctis_endpoints.output_id,
          "communications": self.state.arctis_endpoints.output_id,
        })

      elif event.state.headset_on == "off":
        self._restore_original_output_devices()

      return

    if event.event_type not in (
      "dongle_connection_changed",
      "pnp_device_connected",
      "pnp_device_disconnected",
      "pnp_device_changed",
    ):
      return

    if (
      event.event_type == "dongle_connection_changed"
      and not event.state.dongle_connected
    ):
      self._restore_original_output_devices()

    self._update_arctis_pnp_ids(event)
