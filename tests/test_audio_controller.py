import queue

import pytest
import time

import audio_controller
from types import SimpleNamespace

class FakeThread:
  def __init__(self, target, **kwargs):
    self.target = target
    self.kwargs = kwargs
    self.started = False
    self.joined = False
    self.join_calls = 0

  def start(self):
    self.started = True

  def join(self, timeout=None):
    self.joined = True
    self.join_calls += 1

  def is_alive(self):
    return self.started and not self.joined


class FakeAudioDiscovery:
  def __init__(self):
    self.listeners = {}
    self.started = False
    self.stopped = False
    self.stop_calls = 0
    self.set_default_device_calls = []
    self.set_default_device_errors = {}
    self.default_device_calls = []
    self.default_device_errors = {}
    self.default_devices = {
      flow: {
        "console": None,
        "multimedia": None,
        "communications": None,
      }
      for flow in ("output", "input")
    }

  def add_listener(self, event, callback):
    self.listeners.setdefault(event, [])

    if callback not in self.listeners[event]:
      self.listeners[event].append(callback)

  def remove_listener(self, event, callback):
    callbacks = self.listeners.get(event, [])

    if callback in callbacks:
      callbacks.remove(callback)

  def start(self):
    self.started = True

  def stop(self, timeout=5):
    self.stopped = True
    self.stop_calls += 1
    return True

  def get_default_devices(self, flow):
    self.default_device_calls.append(flow)

    if flow in self.default_device_errors:
      raise self.default_device_errors[flow]

    return self.default_devices[flow].copy()

  def set_default_devices(self, flow, devices):
    self.set_default_device_calls.append({
      "flow": flow,
      "devices": devices.copy(),
    })

    if flow in self.set_default_device_errors:
      raise self.set_default_device_errors[flow]

    return set(devices)

  def emit(self, event, **payload):
    for callback in tuple(self.listeners.get(event, [])):
      callback(self, **payload)

class FakeArctisDevice:
  def __init__(self, state=None):
    self.state = state or SimpleNamespace(
      headset_on=None,
      pnp_device=None,
    )
    self.listeners = []

  def add_listener(self, callback):
    if callback not in self.listeners:
      self.listeners.append(callback)

  def remove_listener(self, callback):
    if callback in self.listeners:
      self.listeners.remove(callback)

  def emit(self, event):
    for callback in tuple(self.listeners):
      callback(event)

def create_controller(discovery=None, device=None):
  if device is None:
    device = FakeArctisDevice()

  return audio_controller.AudioController(
    device,
    discovery,
  )

# ============================================================
# TestInitialization
# ============================================================

class TestInitialization:
  def test_initial_state(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    assert controller.state == (
      audio_controller.AudioControllerState()
    )

  def test_discovery_is_injected(self):
    discovery = FakeAudioDiscovery()

    controller = create_controller(discovery)

    assert controller._discovery is discovery

  def test_default_discovery_is_created(self, monkeypatch):
    discovery = FakeAudioDiscovery()

    monkeypatch.setattr(
      audio_controller,
      "AudioDiscovery",
      lambda: discovery,
    )

    controller = create_controller()

    assert controller._discovery is discovery

  def test_initialization_starts_worker_before_discovery(
    self,
    monkeypatch,
  ):
    operations = []
    discovery = FakeAudioDiscovery()

    class TrackingThread(FakeThread):
      def start(self):
        super().start()
        operations.append("worker_started")

    class TrackingDiscovery(FakeAudioDiscovery):
      def start(self):
        super().start()
        operations.append("discovery_started")

    discovery = TrackingDiscovery()

    monkeypatch.setattr(
      audio_controller.threading,
      "Thread",
      TrackingThread,
    )

    controller = create_controller(discovery)
    controller.start()

    assert operations == [
      "worker_started",
      "discovery_started",
    ]

    assert controller._worker.started
    assert discovery.started

  def test_start_is_idempotent(self, monkeypatch):
    discovery = FakeAudioDiscovery()
    threads = []

    class TrackingThread(FakeThread):
      def __init__(self, target, **kwargs):
        super().__init__(target, **kwargs)
        threads.append(self)

    monkeypatch.setattr(
      audio_controller.threading,
      "Thread",
      TrackingThread,
    )

    controller = create_controller(discovery)

    controller.start()
    controller.start()

    assert len(threads) == 1
    assert discovery.started

  def test_discovery_listeners_are_registered(self, monkeypatch):
    monkeypatch.setattr(
      audio_controller.threading,
      "Thread",
      FakeThread,
    )

    discovery = FakeAudioDiscovery()
    controller = create_controller(discovery)

    controller.start()

    assert set(discovery.listeners) == set(
      audio_controller.AudioController.DISCOVERY_EVENTS
    )

    assert all(
      len(callbacks) == 1
      for callbacks in discovery.listeners.values()
    )

  def test_loads_default_devices_during_initialization(self):
    discovery = FakeAudioDiscovery()

    discovery.default_devices = {
      "output": {
        "console": "output-console",
        "multimedia": "output-multimedia",
        "communications": "output-communications",
      },
      "input": {
        "console": "input-console",
        "multimedia": "input-multimedia",
        "communications": "input-communications",
      },
    }

    controller = create_controller(discovery)

    controller._process_refresh_finished({
      "status": "ready",
      "catalog": {
        "devices": [],
        "outputs": [],
        "inputs": [],
      },
    })

    assert discovery.default_device_calls == [
      "output",
      "input",
    ]

    assert controller.state.default_output == (
      audio_controller.DefaultAudioDevices(
        console="output-console",
        multimedia="output-multimedia",
        communications="output-communications",
      )
    )

    assert controller.state.default_input == (
      audio_controller.DefaultAudioDevices(
        console="input-console",
        multimedia="input-multimedia",
        communications="input-communications",
      )
    )

  def test_default_device_loading_failures_are_independent(self):
    discovery = FakeAudioDiscovery()

    discovery.default_devices["output"] = {
      "console": "output-console",
      "multimedia": "output-multimedia",
      "communications": "output-communications",
    }

    discovery.default_device_errors["input"] = RuntimeError(
      "Input discovery failed."
    )

    controller = create_controller(discovery)

    controller._process_refresh_finished({
      "status": "ready",
      "catalog": {
        "devices": [],
        "outputs": [],
        "inputs": [],
      },
    })

    assert controller.state.default_output == (
      audio_controller.DefaultAudioDevices(
        console="output-console",
        multimedia="output-multimedia",
        communications="output-communications",
      )
    )

    assert controller.state.default_input == (
      audio_controller.DefaultAudioDevices()
    )

    assert discovery.default_device_calls == [
      "output",
      "input",
    ]

  def test_default_devices_are_not_reloaded_on_catalog_refresh(self):
    discovery = FakeAudioDiscovery()
    controller = create_controller(discovery)

    payload = {
      "status": "ready",
      "catalog": {
        "devices": [],
        "outputs": [],
        "inputs": [],
      },
    }

    controller._process_refresh_finished(payload)
    controller._process_refresh_finished(payload)

    assert discovery.default_device_calls == [
      "output",
      "input",
    ]

  def test_registers_arctis_device_listener_once(self):
    device = FakeArctisDevice()
    controller = audio_controller.AudioController(
      device,
      discovery=FakeAudioDiscovery(),
    )

    assert device.listeners == [controller.on_device_event]


  def test_removes_arctis_device_listener_on_stop(self):
    device = FakeArctisDevice()
    controller = audio_controller.AudioController(
      device,
      discovery=FakeAudioDiscovery(),
    )

    assert controller.on_device_event in device.listeners

    controller.stop()

    assert controller.on_device_event not in device.listeners

  def test_removes_arctis_device_listener_when_start_fails(
    self,
    monkeypatch,
  ):
    monkeypatch.setattr(
      audio_controller.threading,
      "Thread",
      FakeThread,
    )

    class FailingDiscovery(FakeAudioDiscovery):
      def start(self):
        raise RuntimeError("Discovery failed.")

    device = FakeArctisDevice()
    discovery = FailingDiscovery()

    controller = create_controller(discovery, device)

    with pytest.raises(RuntimeError, match="Discovery failed."):
      controller.start()

    assert controller.on_device_event not in device.listeners

    assert all(
      not callbacks
      for callbacks in discovery.listeners.values()
    )

# END TestInitialization


# ============================================================
# TestEvents
# ============================================================

class TestEvents:
  def test_device_event_is_enqueued(self, monkeypatch):
    monkeypatch.setattr(
      audio_controller.threading,
      "Thread",
      FakeThread,
    )

    controller = create_controller(
      FakeAudioDiscovery()
    )
    controller.start()

    event = object()
    controller.on_device_event(event)

    queued_event, payload = controller._event_queue.get_nowait()

    assert queued_event == "device_event"
    assert payload == {"event": event}

  def test_discovery_event_is_enqueued(self, monkeypatch):
    monkeypatch.setattr(
      audio_controller.threading,
      "Thread",
      FakeThread,
    )

    discovery = FakeAudioDiscovery()
    controller = create_controller(discovery)
    controller.start()

    discovery.emit(
      "default_device_changed",
      flow="output",
      role="multimedia",
      endpoint_id="endpoint-1",
    )

    event, payload = controller._event_queue.get_nowait()

    assert event == "default_device_changed"
    assert payload == {
      "flow": "output",
      "role": "multimedia",
      "endpoint_id": "endpoint-1",
    }

  def test_events_are_ignored_before_start(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    controller.on_device_event(object())

    assert controller._event_queue.empty()

  def test_events_are_ignored_after_stop(self, monkeypatch):
    monkeypatch.setattr(
      audio_controller.threading,
      "Thread",
      FakeThread,
    )

    discovery = FakeAudioDiscovery()
    controller = create_controller(discovery)

    controller.start()
    controller.stop()

    # Simulate the worker consuming its stop sentinel.
    assert (
      controller._event_queue.get_nowait()
      is audio_controller._STOP_SENTINEL
    )

    controller.on_device_event(object())

    assert controller._event_queue.empty()


# END TestEvents


# ============================================================
# TestListeners
# ============================================================

class TestListeners:
  def test_add_listener(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    callback = lambda *args, **kwargs: None

    controller.add_listener(callback)

    assert controller._listeners == [callback]

  def test_add_listener_does_not_duplicate(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    callback = lambda *args, **kwargs: None

    controller.add_listener(callback)
    controller.add_listener(callback)

    assert controller._listeners == [callback]

  def test_remove_listener(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    callback = lambda *args, **kwargs: None

    controller.add_listener(callback)
    controller.remove_listener(callback)

    assert controller._listeners == []

  def test_invalid_listener_raises(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    with pytest.raises(TypeError):
      controller.add_listener(None)


# END TestListeners


# ============================================================
# TestShutdown
# ============================================================

class TestShutdown:
  def test_stop_stops_discovery_before_worker(self, monkeypatch):
    operations = []

    class TrackingThread(FakeThread):
      def join(self, timeout=None):
        super().join(timeout)
        operations.append("worker_joined")

    class TrackingDiscovery(FakeAudioDiscovery):
      def stop(self, timeout=5):
        super().stop(timeout)
        operations.append("discovery_stopped")
        return True

    monkeypatch.setattr(
      audio_controller.threading,
      "Thread",
      TrackingThread,
    )

    discovery = TrackingDiscovery()
    controller = create_controller(discovery)

    controller.start()
    controller.stop()

    assert operations == [
      "discovery_stopped",
      "worker_joined",
    ]

  def test_stop_is_idempotent(self, monkeypatch):
    monkeypatch.setattr(
      audio_controller.threading,
      "Thread",
      FakeThread,
    )

    discovery = FakeAudioDiscovery()
    controller = create_controller(discovery)

    controller.start()

    assert controller.stop() is True
    assert controller.stop() is True

    assert discovery.stop_calls == 1
    assert controller._worker.join_calls == 1

  def test_stop_sends_worker_sentinel(self, monkeypatch):
    monkeypatch.setattr(
      audio_controller.threading,
      "Thread",
      FakeThread,
    )

    controller = create_controller(
      FakeAudioDiscovery()
    )
    controller.start()

    controller.stop()

    assert controller._event_queue.get_nowait() is (
      audio_controller._STOP_SENTINEL
    )

  def test_stop_before_start(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    assert controller.stop() is True
    assert controller.state.status == "stopped"


# END TestShutdown

# ============================================================
# TestEndpointResolution
# ============================================================

class TestEndpointResolution:
  def test_resolves_arctis_endpoints_by_pnp_instance_id(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    controller._catalog = {
      "devices": [],
      "outputs": [
        {
          "id": "windows-output-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\OUTPUT-1",
          "flow": "output",
          "available": True,
        },
      ],
      "inputs": [
        {
          "id": "windows-input-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\INPUT-1",
          "flow": "input",
          "available": True,
        },
      ],
    }

    event = SimpleNamespace(
      event_type="pnp_device_connected",
      state=SimpleNamespace(
        dongle_connected=True,
        pnp_device=SimpleNamespace(
          audio_render_instance_id=r"swd\mmdevapi\output-1",
          audio_capture_instance_id=r"swd\mmdevapi\input-1",
        ),
      ),
    )

    controller._process_device_event(event)

    assert controller.state.arctis_endpoints == (
      audio_controller.ArctisAudioEndpoints(
        output_id="windows-output-1",
        input_id="windows-input-1",
      )
    )

  def test_ignores_unavailable_arctis_endpoints(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    controller._catalog = {
      "devices": [
        {
          "id": "windows-output-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\OUTPUT-1",
          "flow": "output",
          "available": False,
        },
        {
          "id": "windows-input-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\INPUT-1",
          "flow": "input",
          "available": False,
        },
      ],
      "outputs": [],
      "inputs": [],
    }

    event = SimpleNamespace(
      event_type="pnp_device_connected",
      state=SimpleNamespace(
        dongle_connected=True,
        pnp_device=SimpleNamespace(
          audio_render_instance_id=r"SWD\MMDEVAPI\OUTPUT-1",
          audio_capture_instance_id=r"SWD\MMDEVAPI\INPUT-1",
        ),
      ),
    )

    controller._process_device_event(event)

    assert controller.state.arctis_endpoints == (
      audio_controller.ArctisAudioEndpoints()
    )

  def test_does_not_match_arctis_endpoints_by_name(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    controller._catalog = {
      "devices": [
        {
          "id": "windows-output-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\OTHER-OUTPUT",
          "flow": "output",
          "name": "Auriculares (3- Arctis 7+)",
          "available": True,
        },
        {
          "id": "windows-input-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\OTHER-INPUT",
          "flow": "input",
          "name": "Micrófono (3- Arctis 7+)",
          "available": True,
        },
      ],
      "outputs": [],
      "inputs": [],
    }

    event = SimpleNamespace(
      event_type="pnp_device_connected",
      state=SimpleNamespace(
        dongle_connected=True,
        pnp_device=SimpleNamespace(
          audio_render_instance_id=r"SWD\MMDEVAPI\OUTPUT-1",
          audio_capture_instance_id=r"SWD\MMDEVAPI\INPUT-1",
        ),
      ),
    )

    controller._process_device_event(event)

    assert controller.state.arctis_endpoints == (
      audio_controller.ArctisAudioEndpoints()
    )

  def test_resolves_endpoints_when_catalog_arrives_after_pnp_event(self):
    device = FakeArctisDevice()

    controller = create_controller(
      FakeAudioDiscovery(),
      device,
    )

    event = SimpleNamespace(
      event_type="pnp_device_connected",
      state=SimpleNamespace(
        dongle_connected=True,
        headset_on=None,
        pnp_device=SimpleNamespace(
          audio_render_instance_id=r"SWD\MMDEVAPI\OUTPUT-1",
          audio_capture_instance_id=r"SWD\MMDEVAPI\INPUT-1",
        ),
      ),
    )

    device.state = event.state
    controller._process_device_event(event)

    assert controller.state.arctis_endpoints == (
      audio_controller.ArctisAudioEndpoints()
    )

    catalog = {
      "devices": [],
      "outputs": [
        {
          "id": "windows-output-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\OUTPUT-1",
          "flow": "output",
          "available": True,
        },
      ],
      "inputs": [
        {
          "id": "windows-input-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\INPUT-1",
          "flow": "input",
          "available": True,
        },
      ],
    }

    controller._process_refresh_finished({
      "status": "ready",
      "error": None,
      "catalog": catalog,
    })

    assert controller.state.arctis_endpoints == (
      audio_controller.ArctisAudioEndpoints(
        output_id="windows-output-1",
        input_id="windows-input-1",
      )
    )

  def test_resolves_endpoints_when_pnp_event_arrives_after_catalog(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    catalog = {
      "devices": [],
      "outputs": [
        {
          "id": "windows-output-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\OUTPUT-1",
          "flow": "output",
          "available": True,
        },
      ],
      "inputs": [
        {
          "id": "windows-input-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\INPUT-1",
          "flow": "input",
          "available": True,
        },
      ],
    }

    controller._process_refresh_finished({
      "status": "ready",
      "error": None,
      "catalog": catalog,
    })

    assert controller.state.arctis_endpoints == (
      audio_controller.ArctisAudioEndpoints()
    )

    event = SimpleNamespace(
      event_type="pnp_device_connected",
      state=SimpleNamespace(
        dongle_connected=True,
        pnp_device=SimpleNamespace(
          audio_render_instance_id=r"SWD\MMDEVAPI\OUTPUT-1",
          audio_capture_instance_id=r"SWD\MMDEVAPI\INPUT-1",
        ),
      ),
    )

    controller._process_device_event(event)

    assert controller.state.arctis_endpoints == (
      audio_controller.ArctisAudioEndpoints(
        output_id="windows-output-1",
        input_id="windows-input-1",
      )
    )

  def test_clears_arctis_endpoints_when_dongle_disconnects(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    controller._catalog = {
      "devices": [],
      "outputs": [
        {
          "id": "windows-output-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\OUTPUT-1",
          "flow": "output",
          "available": True,
        },
      ],
      "inputs": [
        {
          "id": "windows-input-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\INPUT-1",
          "flow": "input",
          "available": True,
        },
      ],
    }

    connected_event = SimpleNamespace(
      event_type="pnp_device_connected",
      state=SimpleNamespace(
        dongle_connected=True,
        pnp_device=SimpleNamespace(
          audio_render_instance_id=r"SWD\MMDEVAPI\OUTPUT-1",
          audio_capture_instance_id=r"SWD\MMDEVAPI\INPUT-1",
        ),
      ),
    )

    controller._process_device_event(connected_event)

    assert controller.state.arctis_endpoints.output_id == (
      "windows-output-1"
    )
    assert controller.state.arctis_endpoints.input_id == (
      "windows-input-1"
    )

    disconnected_event = SimpleNamespace(
      event_type="dongle_connection_changed",
      state=SimpleNamespace(
        dongle_connected=False,
        pnp_device=None,
      ),
    )

    controller._process_device_event(disconnected_event)

    assert controller.state.arctis_endpoints == (
      audio_controller.ArctisAudioEndpoints()
    )

  def test_emits_event_when_arctis_endpoints_change(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    events = []

    controller.add_listener(
      lambda source, event, **payload: events.append(
        (event, payload)
      )
    )

    controller._catalog = {
      "devices": [],
      "outputs": [
        {
          "id": "windows-output-1",
          "pnp_instance_id": r"SWD\MMDEVAPI\OUTPUT-1",
          "flow": "output",
          "available": True,
        },
      ],
      "inputs": [],
    }

    device_event = SimpleNamespace(
      event_type="pnp_device_connected",
      state=SimpleNamespace(
        dongle_connected=True,
        pnp_device=SimpleNamespace(
          audio_render_instance_id=r"SWD\MMDEVAPI\OUTPUT-1",
          audio_capture_instance_id=None,
        ),
      ),
    )

    controller._process_device_event(device_event)

    assert len(events) == 1
    assert events[0][0] == "arctis_endpoints_changed"
    assert events[0][1]["endpoints"] == (
      audio_controller.ArctisAudioEndpoints(
        output_id="windows-output-1",
      )
    )

    controller._process_device_event(device_event)

    assert len(events) == 1

# END TestEndpointResolution

# ============================================================
# TestAutomaticDeviceSwitching
# ============================================================

class TestAutomaticDeviceSwitching:
  def test_switches_output_roles_to_arctis_when_headset_powers_on(self):
    discovery = FakeAudioDiscovery()

    discovery.default_devices["output"] = {
      "console": "speakers-console",
      "multimedia": "speakers-multimedia",
      "communications": "speakers-communications",
    }

    controller = create_controller(discovery)

    controller._process_refresh_finished({
      "status": "ready",
      "catalog": {
        "devices": [],
        "outputs": [],
        "inputs": [],
      },
    })

    controller._set_state(
      arctis_endpoints=audio_controller.ArctisAudioEndpoints(
        output_id="arctis-output",
      )
    )

    event = SimpleNamespace(
      event_type="headset_power_changed",
      state=SimpleNamespace(
        headset_on="on",
        pnp_device=None,
      ),
    )

    controller._process_device_event(event)

    assert discovery.set_default_device_calls == [
      {
        "flow": "output",
        "devices": {
          "console": "arctis-output",
          "multimedia": "arctis-output",
          "communications": "arctis-output",
        },
      },
    ]

    assert controller.state.default_output == (
      audio_controller.DefaultAudioDevices(
        console="speakers-console",
        multimedia="speakers-multimedia",
        communications="speakers-communications",
      )
    )

  def test_does_not_switch_output_without_arctis_endpoint(self):
    discovery = FakeAudioDiscovery()
    controller = create_controller(discovery)

    event = SimpleNamespace(
      event_type="headset_powered_on",
      state=SimpleNamespace(
        powered_on=True,
        pnp_device=None,
      ),
    )

    controller._process_device_event(event)

    assert discovery.set_default_device_calls == []

  def test_ignores_own_default_device_change_notification(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    controller._pending_default_device_changes = {
      ("output", "console", "arctis-output"): (
        time.monotonic() + 5
      ),
    }

    notification = {
      "flow": "output",
      "role": "console",
      "endpoint_id": "arctis-output",
    }

    controller._process_default_device_changed(notification)
    controller._process_default_device_changed(notification)

    assert controller.state.manual_output_override is False

  def test_detects_external_default_device_change_notification(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    controller._pending_default_device_changes = {
      ("output", "console", "arctis-output"): (
        time.monotonic() + 5
      ),
    }

    controller._process_default_device_changed({
      "flow": "output",
      "role": "console",
      "endpoint_id": "speakers",
    })

    assert controller.state.manual_output_override is True

  def test_removes_expired_pending_default_device_changes(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    controller._pending_default_device_changes = {
      ("output", "console", "arctis-output"): 100,
      ("output", "multimedia", "arctis-output"): 200,
    }

    controller._remove_expired_pending_default_device_changes(
      now=150
    )

    assert controller._pending_default_device_changes == {
      ("output", "multimedia", "arctis-output"): 200,
    }

  def test_expired_own_notification_is_detected_as_external(self):
    controller = create_controller(
      FakeAudioDiscovery()
    )

    controller._pending_default_device_changes = {
      ("output", "console", "arctis-output"): 100,
    }

    controller._process_default_device_changed({
      "flow": "output",
      "role": "console",
      "endpoint_id": "arctis-output",
    })

    assert controller.state.manual_output_override is True
    assert controller._pending_default_device_changes == {}

  def test_restores_original_output_devices_when_headset_powers_off(self):
    discovery = FakeAudioDiscovery()

    original_devices = {
      "console": "speakers-console",
      "multimedia": "speakers-multimedia",
      "communications": "speakers-communications",
    }

    discovery.default_devices["output"] = original_devices.copy()

    controller = create_controller(discovery)

    controller._process_refresh_finished({
      "status": "ready",
      "catalog": {
        "devices": [],
        "outputs": [],
        "inputs": [],
      },
    })

    controller._set_state(
      arctis_endpoints=audio_controller.ArctisAudioEndpoints(
        output_id="arctis-output",
      )
    )

    controller._process_device_event(
      SimpleNamespace(
        event_type="headset_power_changed",
        state=SimpleNamespace(
          headset_on="on",
          pnp_device=None,
        ),
      )
    )

    controller._process_device_event(
      SimpleNamespace(
        event_type="headset_power_changed",
        state=SimpleNamespace(
          headset_on="off",
          pnp_device=None,
        ),
      )
    )

    assert discovery.set_default_device_calls == [
      {
        "flow": "output",
        "devices": {
          "console": "arctis-output",
          "multimedia": "arctis-output",
          "communications": "arctis-output",
        },
      },
      {
        "flow": "output",
        "devices": original_devices,
      },
    ]

    assert controller._original_output_devices is None

  def test_does_not_restore_original_output_devices_after_manual_change(self):
    discovery = FakeAudioDiscovery()

    discovery.default_devices["output"] = {
      "console": "speakers-console",
      "multimedia": "speakers-multimedia",
      "communications": "speakers-communications",
    }

    controller = create_controller(discovery)

    controller._process_refresh_finished({
      "status": "ready",
      "catalog": {
        "devices": [],
        "outputs": [],
        "inputs": [],
      },
    })

    controller._set_state(
      arctis_endpoints=audio_controller.ArctisAudioEndpoints(
        output_id="arctis-output",
      )
    )

    controller._process_device_event(
      SimpleNamespace(
        event_type="headset_power_changed",
        state=SimpleNamespace(
          headset_on="on",
          pnp_device=None,
        ),
      )
    )

    controller._set_state(manual_output_override=True)

    controller._process_device_event(
      SimpleNamespace(
        event_type="headset_power_changed",
        state=SimpleNamespace(
          headset_on="off",
          pnp_device=None,
        ),
      )
    )

    assert len(discovery.set_default_device_calls) == 1
    assert controller._original_output_devices is None

  def test_restores_original_output_devices_when_dongle_disconnects(self):
    discovery = FakeAudioDiscovery()

    original_devices = {
      "console": "speakers-console",
      "multimedia": "speakers-multimedia",
      "communications": "speakers-communications",
    }

    discovery.default_devices["output"] = original_devices.copy()

    controller = create_controller(discovery)

    controller._process_refresh_finished({
      "status": "ready",
      "catalog": {
        "devices": [],
        "outputs": [],
        "inputs": [],
      },
    })

    controller._set_state(
      arctis_endpoints=audio_controller.ArctisAudioEndpoints(
        output_id="arctis-output",
      )
    )

    controller._process_device_event(
      SimpleNamespace(
        event_type="headset_power_changed",
        state=SimpleNamespace(
          headset_on="on",
          pnp_device=None,
        ),
      )
    )

    controller._process_device_event(
      SimpleNamespace(
        event_type="dongle_connection_changed",
        state=SimpleNamespace(
          dongle_connected=False,
          headset_on=None,
          pnp_device=None,
        ),
      )
    )

    assert discovery.set_default_device_calls == [
      {
        "flow": "output",
        "devices": {
          "console": "arctis-output",
          "multimedia": "arctis-output",
          "communications": "arctis-output",
        },
      },
      {
        "flow": "output",
        "devices": original_devices,
      },
    ]

    assert controller._original_output_devices is None

  def test_does_not_restore_original_devices_after_external_output_change(self):
    discovery = FakeAudioDiscovery()

    original_devices = {
      "console": "speakers-console",
      "multimedia": "speakers-multimedia",
      "communications": "speakers-communications",
    }

    discovery.default_devices["output"] = original_devices.copy()

    controller = create_controller(discovery)

    controller._process_refresh_finished({
      "status": "ready",
      "catalog": {
        "devices": [],
        "outputs": [],
        "inputs": [],
      },
    })

    controller._set_state(
      arctis_endpoints=audio_controller.ArctisAudioEndpoints(
        output_id="arctis-output",
      )
    )

    controller._process_device_event(
      SimpleNamespace(
        event_type="headset_power_changed",
        state=SimpleNamespace(
          headset_on="on",
          pnp_device=None,
        ),
      )
    )

    controller._process_default_device_changed({
      "flow": "output",
      "role": "console",
      "endpoint_id": "manual-output",
    })

    assert controller.state.manual_output_override is True

    controller._process_device_event(
      SimpleNamespace(
        event_type="dongle_connection_changed",
        state=SimpleNamespace(
          dongle_connected=False,
          headset_on=None,
          pnp_device=None,
        ),
      )
    )

    assert len(discovery.set_default_device_calls) == 1
    assert controller._original_output_devices is None

  def test_restores_original_output_devices_when_app_starts_with_headset_on(self):
    discovery = FakeAudioDiscovery()

    original_devices = {
      "console": "speakers-console",
      "multimedia": "speakers-multimedia",
      "communications": "speakers-communications",
    }

    discovery.default_devices["output"] = original_devices.copy()

    pnp_instance_id = "USB\\ARCTIS_RENDER"

    device = FakeArctisDevice(
      SimpleNamespace(
        headset_on="on",
        pnp_device=SimpleNamespace(
          audio_render_instance_id=pnp_instance_id,
          audio_capture_instance_id=None,
        ),
      )
    )

    controller = create_controller(discovery, device)

    controller._process_refresh_finished({
      "status": "ready",
      "catalog": {
        "devices": [],
        "outputs": [
          {
            "id": "arctis-output",
            "pnp_instance_id": pnp_instance_id,
            "available": True,
          },
        ],
        "inputs": [],
      },
    })

    controller._process_device_event(
      SimpleNamespace(
        event_type="headset_power_changed",
        state=SimpleNamespace(
          headset_on="off",
          pnp_device=device.state.pnp_device,
        ),
      )
    )

    assert discovery.set_default_device_calls == [
      {
        "flow": "output",
        "devices": {
          "console": "arctis-output",
          "multimedia": "arctis-output",
          "communications": "arctis-output",
        },
      },
      {
        "flow": "output",
        "devices": original_devices,
      },
    ]

    assert controller._original_output_devices is None

  def test_cleans_pending_changes_when_output_switch_fails(self):
    discovery = FakeAudioDiscovery()
    discovery.set_default_device_errors["output"] = RuntimeError(
      "Windows rejected the output change"
    )

    device = FakeArctisDevice()
    controller = create_controller(discovery, device)

    events = []
    controller.add_listener(
      lambda controller, event, **payload: events.append((event, payload))
    )

    controller._switch_output({
      "console": "arctis-output",
      "multimedia": "arctis-output",
      "communications": "arctis-output",
    })

    assert len(discovery.set_default_device_calls) == 1

    assert controller._pending_default_device_changes == {}

    error_events = [
      payload for event, payload in events
      if event == "error"
    ]

    assert len(error_events) == 1
    assert (
      error_events[0]["error"]
      == "Failed to switch output devices: "
      "Windows rejected the output change"
    )

  def test_does_not_restore_original_devices_when_initial_output_switch_fails(self):
    discovery = FakeAudioDiscovery()

    original_devices = {
      "console": "speakers-console",
      "multimedia": "speakers-multimedia",
      "communications": "speakers-communications",
    }

    discovery.default_devices["output"] = original_devices.copy()
    discovery.set_default_device_errors["output"] = RuntimeError(
      "Windows rejected the output change"
    )
    discovery.set_default_device_errors.pop("output")

    device = FakeArctisDevice()

    controller = create_controller(discovery, device)

    controller._process_refresh_finished({
      "status": "ready",
      "catalog": {
        "devices": [],
        "outputs": [],
        "inputs": [],
      },
    })

    controller._set_state(
      arctis_endpoints=audio_controller.ArctisAudioEndpoints(
        output_id="arctis-output",
      )
    )

    controller._process_device_event(
      SimpleNamespace(
        event_type="headset_power_changed",
        state=SimpleNamespace(headset_on="on"),
      )
    )

    controller._process_device_event(
      SimpleNamespace(
        event_type="headset_power_changed",
        state=SimpleNamespace(headset_on="off"),
      )
    )

    assert discovery.set_default_device_calls == [
      {
        "flow": "output",
        "devices": {
          "console": "arctis-output",
          "multimedia": "arctis-output",
          "communications": "arctis-output",
        },
      },
      {
        "flow": "output",
        "devices": original_devices,
      },
    ]

    assert controller._original_output_devices is None

# END TestAutomaticDeviceSwitching