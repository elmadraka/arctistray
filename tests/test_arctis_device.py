import queue
import pytest

import arctis_device
import connection_manager
import hid_worker

class FakeThread:
  def __init__(self, target, **kwargs):
    self.target = target
    self.kwargs = kwargs
    self.started = False
    self.joined = False
    self.join_calls = 0

  def start(self):
    self.started = True

  def join(self):
    self.joined = True
    self.join_calls += 1

  def is_alive(self):
    return self.started and not self.joined


class FakeConnectionManager:
  def __init__(self, config, message_queue):
    self.config = config
    self.message_queue = message_queue
    self.started = False
    self.stopped = False

  def start(self):
    self.started = True

  def stop(self):
    self.stopped = True

def flush_pending_mixdial(device):
  import time

  time.sleep(0.11)
  device._flush_pending_mixdial()

def create_device(state=None, on_event=None):
  device = arctis_device.ArctisDevice.__new__(
    arctis_device.ArctisDevice
  )
  device._state = (
    state if state is not None else arctis_device.DeviceState()
  )
  device._on_event = on_event
  device._listeners = []
  device._listeners_lock = arctis_device.threading.RLock()
  device._pending_mixdial = {}
  device._mixdial_debounce = 0.1
  device._reported_battery_status = None
  return device

# ============================================================
# TestInitialization
# ============================================================

class TestInitialization:
  def test_initialization_starts_consumer_before_connection_manager(
    self,
    monkeypatch,
  ):
    operations = []

    class TrackingThread(FakeThread):
      def start(self):
        super().start()
        operations.append("consumer_started")

    class TrackingConnectionManager(FakeConnectionManager):
      def start(self):
        super().start()
        operations.append("manager_started")

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      TrackingConnectionManager,
    )
    monkeypatch.setattr(
      arctis_device.threading,
      "Thread",
      TrackingThread,
    )

    callback = lambda event: None

    device = arctis_device.ArctisDevice(
      "arctis_7_plus.yml",
      callback,
    )

    assert operations == [
      "consumer_started",
      "manager_started",
    ]

    assert device._connection_manager.started is True
    assert device._connection_manager.message_queue is (
      device._message_queue
    )
    assert isinstance(device._message_queue, queue.Queue)
    assert device._on_event is callback

  def test_initialization_without_callback(self, monkeypatch):
    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      FakeConnectionManager,
    )
    monkeypatch.setattr(
      arctis_device.threading,
      "Thread",
      FakeThread,
    )

    device = arctis_device.ArctisDevice(
      "arctis_7_plus.yml",
    )

    assert device._on_event is None
    assert device._consumer_thread.started is True
    assert device._connection_manager.started is True

# END TestInitialization

# ============================================================
# TestShutdown
# ============================================================

class TestShutdown:
  def test_stop_stops_connection_manager_before_consumer(
    self,
    monkeypatch,
  ):
    operations = []

    class TrackingThread(FakeThread):
      def join(self):
        super().join()
        operations.append("consumer_joined")

    class TrackingConnectionManager(FakeConnectionManager):
      def stop(self):
        operations.append("manager_stopped")
        self.stopped = True

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      TrackingConnectionManager,
    )
    monkeypatch.setattr(
      arctis_device.threading,
      "Thread",
      TrackingThread,
    )

    device = arctis_device.ArctisDevice(
      "arctis_7_plus.yml",
    )

    device.stop()

    assert operations == [
      "manager_stopped",
      "consumer_joined",
    ]
    assert device._connection_manager.stopped is True
    assert device._consumer_thread.joined is True

  def test_stop_sends_consumer_sentinel_after_manager_stops(
    self,
    monkeypatch,
  ):
    operations = []

    class TrackingQueue(queue.Queue):
      def put(self, item, *args, **kwargs):
        operations.append(("queue_put", item))
        super().put(item, *args, **kwargs)

    class TrackingConnectionManager(FakeConnectionManager):
      def stop(self):
        operations.append(("manager_stopped", None))
        self.stopped = True

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      TrackingConnectionManager,
    )
    monkeypatch.setattr(
      arctis_device.threading,
      "Thread",
      FakeThread,
    )
    monkeypatch.setattr(
      arctis_device.queue,
      "Queue",
      TrackingQueue,
    )

    device = arctis_device.ArctisDevice(
      "arctis_7_plus.yml",
    )

    operations.clear()
    device.stop()

    assert [item[0] for item in operations] == [
      "manager_stopped",
      "queue_put",
    ]

  def test_stop_is_idempotent(self, monkeypatch):
    manager_instances = []

    class TrackingConnectionManager(FakeConnectionManager):
      def __init__(self, config, message_queue):
        super().__init__(config, message_queue)
        manager_instances.append(self)
        self.stop_calls = 0

      def stop(self):
        self.stop_calls += 1
        self.stopped = True

    class TrackingQueue(queue.Queue):
      def __init__(self):
        super().__init__()
        self.put_calls = 0

      def put(self, item, *args, **kwargs):
        self.put_calls += 1
        super().put(item, *args, **kwargs)

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      TrackingConnectionManager,
    )
    monkeypatch.setattr(
      arctis_device.threading,
      "Thread",
      FakeThread,
    )
    monkeypatch.setattr(
      arctis_device.queue,
      "Queue",
      TrackingQueue,
    )

    device = arctis_device.ArctisDevice(
      "arctis_7_plus.yml",
    )

    device.stop()
    device.stop()

    assert manager_instances[0].stop_calls == 1
    assert device._message_queue.put_calls == 1
    assert device._consumer_thread.join_calls == 1

  def test_consumer_processes_pending_messages_before_sentinel(
    self,
    monkeypatch,
  ):
    processed_messages = []

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      FakeConnectionManager,
    )
    monkeypatch.setattr(
      arctis_device.threading,
      "Thread",
      FakeThread,
    )

    device = arctis_device.ArctisDevice(
      "arctis_7_plus.yml",
    )

    device._process_message = processed_messages.append

    first_message = {"status": {"battery_level": 3}}
    second_message = {"actions": ["button_single"]}

    device._message_queue.put(first_message)
    device._message_queue.put(second_message)
    device._message_queue.put(arctis_device._STOP_SENTINEL)

    device._consume_messages()

    assert processed_messages == [
      first_message,
      second_message,
    ]

# END TestShutdown

# ============================================================
# TestInitializationFailures
# ============================================================

class TestInitializationFailures:
  def test_connection_manager_is_not_started_if_consumer_start_fails(
    self,
    monkeypatch,
  ):
    class FailingThread(FakeThread):
      def start(self):
        raise RuntimeError("Consumer thread failed to start")

    manager_instances = []

    class TrackingConnectionManager(FakeConnectionManager):
      def __init__(self, config, message_queue):
        super().__init__(config, message_queue)
        manager_instances.append(self)

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      TrackingConnectionManager,
    )
    monkeypatch.setattr(
      arctis_device.threading,
      "Thread",
      FailingThread,
    )

    with pytest.raises(
      RuntimeError,
      match="Consumer thread failed to start",
    ):
      arctis_device.ArctisDevice("arctis_7_plus.yml")

    assert manager_instances == []

  def test_consumer_is_stopped_if_connection_manager_start_fails(
    self,
    monkeypatch,
  ):
    consumer_threads = []

    class TrackingThread(FakeThread):
      def __init__(self, target, **kwargs):
        super().__init__(target, **kwargs)
        consumer_threads.append(self)

    class FailingConnectionManager(FakeConnectionManager):
      def start(self):
        raise RuntimeError("ConnectionManager failed to start")

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      FailingConnectionManager,
    )
    monkeypatch.setattr(
      arctis_device.threading,
      "Thread",
      TrackingThread,
    )

    with pytest.raises(
      RuntimeError,
      match="ConnectionManager failed to start",
    ):
      arctis_device.ArctisDevice("arctis_7_plus.yml")

    assert len(consumer_threads) == 1
    assert consumer_threads[0].started is True
    assert consumer_threads[0].joined is True

  def test_profile_loading_failure_does_not_start_services(
    self,
    monkeypatch,
  ):
    operations = []

    def fail_profile_loading(profile):
      raise ValueError("Invalid device profile")

    class TrackingThread(FakeThread):
      def start(self):
        operations.append("consumer_started")
        super().start()

    class TrackingConnectionManager(FakeConnectionManager):
      def __init__(self, config, message_queue):
        operations.append("manager_created")
        super().__init__(config, message_queue)

    monkeypatch.setattr(
      arctis_device,
      "load_device_profile",
      fail_profile_loading,
    )
    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      TrackingConnectionManager,
    )
    monkeypatch.setattr(
      arctis_device.threading,
      "Thread",
      TrackingThread,
    )

    with pytest.raises(
      ValueError,
      match="Invalid device profile",
    ):
      arctis_device.ArctisDevice("arctis_7_plus.yml")

    assert operations == []

# END TestInitializationFailures

# ============================================================
# TestState
# ============================================================

class TestState:
  def test_initial_state_contains_unknown_device_values(self):
    device = create_device()

    device._state = arctis_device.DeviceState()

    assert device.state == arctis_device.DeviceState(
      dongle_connected=False,
      headset_on=None,
      usb_connected=False,
      battery_level=None,
      battery_status=None,
      mixdial_game=None,
      mixdial_chat=None,
    )

  def test_status_message_updates_only_received_fields(self):
    device = create_device()
    device._state = arctis_device.DeviceState(
      dongle_connected=True,
      headset_on="on",
      usb_connected=False,
      battery_level=3,
      battery_status="charging",
      mixdial_game=50,
      mixdial_chat=50,
    )

    previous_state = device._state

    device._reported_battery_status = "charging"

    device._process_message({
      "status": {
        "mixdial_game": 75,
      },
    })
    flush_pending_mixdial(device)

    assert device.state == arctis_device.DeviceState(
      dongle_connected=True,
      headset_on="on",
      usb_connected=False,
      battery_level=3,
      battery_status="charging",
      mixdial_game=75,
      mixdial_chat=50,
    )
    assert device.state is not previous_state

  def test_status_message_preserves_fields_not_received(self):
    device = create_device()
    device._state = arctis_device.DeviceState(
      dongle_connected=True,
      headset_on="on",
      usb_connected=True,
      battery_level=4,
      battery_status="fully_charged",
      mixdial_game=80,
      mixdial_chat=20,
    )

    device._process_message({
      "status": {
        "battery_level": 3,
        "battery_status": "charging",
      },
    })

    assert device.state.battery_level == 3
    assert device.state.battery_status == "charging"
    assert device.state.dongle_connected is True
    assert device.state.headset_on == "on"
    assert device.state.usb_connected is True
    assert device.state.mixdial_game == 80
    assert device.state.mixdial_chat == 20

  def test_status_message_can_clear_existing_value(self):
    device = create_device()
    device._state = arctis_device.DeviceState(
      dongle_connected=True,
      battery_level=3,
    )

    device._process_message({
      "status": {
        "battery_level": None,
      },
    })

    assert device.state.battery_level is None
    assert device.state.dongle_connected is True

  def test_status_message_with_unchanged_values_preserves_state(self):
    device = create_device()
    device._state = arctis_device.DeviceState(
      dongle_connected=True,
      headset_on="on",
      battery_level=3,
    )

    previous_state = device.state

    device._process_message({
      "status": {
        "headset_on": "on",
        "battery_level": 3,
      },
    })

    assert device.state == arctis_device.DeviceState(
      dongle_connected=True,
      headset_on="on",
      usb_connected=False,
      battery_level=3,
      battery_status="discharging",
      mixdial_game=None,
      mixdial_chat=None,
    )
    assert device.state is not previous_state

  @pytest.mark.parametrize(
    ("physical_usb", "expected_usb"),
    [
      (True, True),
      (False, False),
    ],
  )
  def test_headset_usb_connection_is_translated(
    self,
    physical_usb,
    expected_usb,
  ):
    device = create_device()
    device._state = arctis_device.DeviceState()

    device._process_message({
      "status": {
        "headset_usb_connected": physical_usb,
      },
    })

    assert device.state.usb_connected == expected_usb


  @pytest.mark.parametrize(
    ("field", "value", "expected_field", "expected_value"),
    [
      ("headset", "on", "headset_on", "on"),
      ("headset", "off", "headset_on", "off"),
      ("usb", "connected", "usb_connected", True),
      ("usb", "disconnected", "usb_connected", False),
    ],
  )
  def test_profile_status_fields_are_translated(
    self,
    field,
    value,
    expected_field,
    expected_value,
  ):
    device = create_device()
    device._state = arctis_device.DeviceState()

    device._process_message({
      "status": {
        field: value,
      },
    })

    assert getattr(device.state, expected_field) == expected_value


  def test_pnp_device_starts_as_none(self):
    device = create_device()
    device._state = arctis_device.DeviceState()

    assert device.state.pnp_device is None

  def test_pnp_device_status_updates_state(self):
    device = create_device()
    device._state = arctis_device.DeviceState(
      dongle_connected=True,
    )

    device._process_message({
      "status": {
        "pnp_device": {
          "audio_render_instance_id": "SWD\\MMDEVAPI\\RENDER",
          "audio_capture_instance_id": "SWD\\MMDEVAPI\\CAPTURE",
        },
      },
    })

    assert isinstance(
      device.state.pnp_device,
      arctis_device.PnPDeviceState,
    )
    assert device.state.pnp_device == (
      arctis_device.PnPDeviceState(
        audio_render_instance_id="SWD\\MMDEVAPI\\RENDER",
        audio_capture_instance_id="SWD\\MMDEVAPI\\CAPTURE",
      )
    )

  def test_pnp_device_allows_missing_audio_endpoints(self):
    device = create_device()
    device._state = arctis_device.DeviceState(
      dongle_connected=True,
    )

    device._process_message({
      "status": {
        "pnp_device": {
          "audio_render_instance_id": None,
          "audio_capture_instance_id": None,
        },
      },
    })

    assert device.state.pnp_device is not None
    assert device.state.pnp_device.audio_render_instance_id is None
    assert device.state.pnp_device.audio_capture_instance_id is None

  def test_missing_pnp_device_field_preserves_existing_state(self):
    device = create_device()
    device._state = arctis_device.DeviceState(
      pnp_device=arctis_device.PnPDeviceState(
        audio_render_instance_id=None,
        audio_capture_instance_id=None,
      ),
    )

    previous_pnp_device = device.state.pnp_device

    device._process_message({
      "status": {
        "battery_level": 3,
      },
    })

    assert device.state.pnp_device is previous_pnp_device

  def test_invalid_pnp_device_value_preserves_existing_state(self):
    device = create_device()
    device._state = arctis_device.DeviceState(
      pnp_device=arctis_device.PnPDeviceState(
        audio_render_instance_id=None,
        audio_capture_instance_id=None,
      ),
    )

    previous_pnp_device = device.state.pnp_device

    device._process_message({
      "status": {
        "pnp_device": "invalid",
      },
    })

    assert device.state.pnp_device is previous_pnp_device


# END TestState

# ============================================================
# TestEvents
# ============================================================
class TestEvents:
  def test_dongle_connection_change_emits_event(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "dongle_connected": True,
      },
    })

    assert len(events) == 1
    assert events[0].event_type == "dongle_connection_changed"
    assert events[0].data == {
      "dongle_connected": True,
    }

  def test_headset_power_change_emits_event(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "headset_on": "on",
      },
    })

    assert [event.event_type for event in events] == [
      "headset_power_changed",
      "battery_status_changed",
    ]
    assert events[0].data["headset_on"] == "on"
    assert events[1].data["battery_status"] == "discharging"
    assert events[0].event_type == "headset_power_changed"
    assert events[0].data == {
      "headset_on": "on",
    }

  def test_usb_connection_change_emits_event(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "usb_connected": True,
      },
    })

    assert len(events) == 1
    assert events[0].event_type == "usb_connection_changed"
    assert events[0].data == {
      "usb_connected": True,
    }

  def test_battery_level_change_emits_event(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "battery_level": 3,
      },
    })

    assert len(events) == 1
    assert events[0].event_type == "battery_changed"
    assert events[0].data == {
      "battery_level": 3,
    }

  def test_battery_status_change_emits_event(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(
        headset_on="on",
      ),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "battery_status": "charging",
      },
    })

    assert len(events) == 1
    assert events[0].event_type == "battery_status_changed"
    assert events[0].data == {
      "battery_status": "charging",
    }

  def test_usb_disconnect_changes_battery_status_to_discharging(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(
        headset_on="on",
        usb_connected=True,
        battery_status="charging",
      ),
      on_event=events.append,
    )
    device._reported_battery_status = "charging"

    device._process_message({
      "status": {
        "usb_connected": False,
      },
    })

    assert device._state.usb_connected is False
    assert device._state.battery_status == "discharging"

    battery_events = [
      event
      for event in events
      if event.event_type == "battery_status_changed"
    ]

    assert len(battery_events) == 1
    assert battery_events[0].data == {
      "battery_status": "discharging",
    }

  def test_game_mix_change_emits_both_mix_values(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(
        mixdial_game=50,
        mixdial_chat=50,
      ),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "mixdial_game": 75,
      },
    })
    flush_pending_mixdial(device)

    assert len(events) == 1
    assert events[0].event_type == "mixdial_changed"
    assert events[0].data == {
      "mixdial_game": 75,
      "mixdial_chat": 50,
    }

  def test_chat_mix_change_emits_both_mix_values(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(
        mixdial_game=50,
        mixdial_chat=50,
      ),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "mixdial_chat": 25,
      },
    })
    flush_pending_mixdial(device)

    assert len(events) == 1
    assert events[0].event_type == "mixdial_changed"
    assert events[0].data == {
      "mixdial_game": 50,
      "mixdial_chat": 25,
    }

  def test_mixdial_change_includes_unknown_value(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "mixdial_game": 75,
      },
    })
    flush_pending_mixdial(device)

    assert len(events) == 1
    assert events[0].data == {
      "mixdial_game": 75,
      "mixdial_chat": None,
    }

  def test_button_action_emits_button_pressed_event(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(),
      on_event=events.append,
    )

    device._process_message({
      "actions": ["button_single"],
    })

    assert len(events) == 1
    assert events[0].event_type == "button_pressed"
    assert events[0].data == {
      "button": "button_single",
    }

  def test_button_double_action_emits_button_pressed_event(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(),
      on_event=events.append,
    )

    device._process_message({
      "actions": ["button_double"],
    })

    assert len(events) == 1
    assert events[0].event_type == "button_pressed"
    assert events[0].data == {
      "button": "button_double",
    }

  def test_button_triple_action_emits_button_pressed_event(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(),
      on_event=events.append,
    )

    device._process_message({
      "actions": ["button_triple"],
    })

    assert len(events) == 1
    assert events[0].event_type == "button_pressed"
    assert events[0].data == {
      "button": "button_triple",
    }

  def test_unchanged_status_values_do_not_emit_events(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(
        dongle_connected=True,
        headset_on="on",
        battery_level=3,
      ),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "dongle_connected": True,
        "headset_on": "on",
        "battery_level": 3,
      },
    })

    assert len(events) == 1
    assert events[0].event_type == "battery_status_changed"
    assert events[0].data["battery_status"] == "discharging"

  def test_event_data_is_immutable(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "battery_level": 3,
      },
    })

    with pytest.raises(TypeError):
      events[0].data["battery_level"] = 4

  def test_multiple_status_changes_emit_separate_events(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(
        dongle_connected=False,
        headset_on="off",
        usb_connected=False,
        battery_level=2,
        battery_status=None,
        mixdial_game=50,
        mixdial_chat=50,
      ),
      on_event=events.append,
    )
    device._reported_battery_status = "not_charging"

    device._process_message({
      "status": {
        "dongle_connected": True,
        "headset_on": "on",
        "battery_level": 3,
        "mixdial_game": 75,
      },
    })
    flush_pending_mixdial(device)

    assert device.state == arctis_device.DeviceState(
      dongle_connected=True,
      headset_on="on",
      usb_connected=False,
      battery_level=3,
      battery_status="discharging",
      mixdial_game=75,
      mixdial_chat=50,
    )

    assert [event.event_type for event in events] == [
      "dongle_connection_changed",
      "headset_power_changed",
      "battery_changed",
      "battery_status_changed",
      "mixdial_changed",
    ]

    battery_status_event = next(
      event
      for event in events
      if event.event_type == "battery_status_changed"
    )
    assert battery_status_event.data["battery_status"] == "discharging"

    assert events[-1].data == {
      "mixdial_game": 75,
      "mixdial_chat": 50,
    }

  def test_all_events_are_emitted_after_state_update(self):
    observed_states = []
    device = create_device()
    device._state = arctis_device.DeviceState(
      headset_on="on",
      battery_level=2,
      battery_status="discharging",
    )
    device._reported_battery_status = "not_charging"

    def on_event(event):
      observed_states.append(device.state)

    device._on_event = on_event

    device._process_message({
      "status": {
        "battery_level": 3,
        "battery_status": "charging",
      },
    })

    assert len(observed_states) == 2
    assert all(
      state.battery_level == 3
      and state.battery_status == "charging"
      for state in observed_states
    )

  def test_state_updates_without_event_callback(self):
    device = create_device()
    device._state = arctis_device.DeviceState()
    device._on_event = None

    device._process_message({
      "status": {
        "battery_level": 3,
      },
    })

    assert device.state.battery_level == 3

  def test_physical_usb_connection_emits_semantic_event(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "headset_usb_connected": True,
      },
    })

    assert len(events) == 1
    assert events[0].event_type == "usb_connection_changed"
    assert events[0].data == {
      "usb_connected": True,
    }


  @pytest.mark.parametrize(
    ("field", "value", "event_type", "event_data"),
    [
      (
        "headset",
        "on",
        "headset_power_changed",
        {"headset_on": "on"},
      ),
      (
        "headset",
        "off",
        "headset_power_changed",
        {"headset_on": "off"},
      ),
      (
        "usb",
        "connected",
        "usb_connection_changed",
        {"usb_connected": True},
      ),
      (
        "usb",
        "disconnected",
        "usb_connection_changed",
        {"usb_connected": False},
      ),
    ],
  )
  def test_profile_status_fields_emit_semantic_events(
    self,
    field,
    value,
    event_type,
    event_data,
  ):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(
        usb_connected=(
          field == "usb" and value == "disconnected"
        )
      ),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        field: value,
      },
    })

    expected_events = [event_type]

    if field == "headset" and value == "on":
      expected_events.append("battery_status_changed")

    assert [event.event_type for event in events] == expected_events

    assert events[0].data == event_data

    if "battery_status_changed" in expected_events:
      assert events[1].data["battery_status"] == "discharging"
    assert events[0].event_type == event_type
    assert dict(events[0].data) == event_data


  def test_pnp_device_lifecycle_emits_semantic_events(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(
        dongle_connected=True,
      ),
      on_event=events.append,
    )

    first_device = {
      "audio_render_instance_id": "SWD\\RENDER",
      "audio_capture_instance_id": "SWD\\CAPTURE",
    }

    changed_device = {
      "audio_render_instance_id": "SWD\\RENDER-NEW",
      "audio_capture_instance_id": "SWD\\CAPTURE",
    }

    device._process_message({
      "status": {
        "pnp_device": first_device,
      },
    })

    device._process_message({
      "status": {
        "pnp_device": first_device,
      },
    })

    device._process_message({
      "status": {
        "pnp_device": changed_device,
      },
    })

    device._process_message({
      "status": {
        "pnp_device": None,
      },
    })

    assert [
      event.event_type
      for event in events
    ] == [
      "pnp_device_connected",
      "pnp_device_changed",
      "pnp_device_disconnected",
    ]

    assert events[0].data["pnp_device"] == (
      arctis_device.PnPDeviceState(**first_device)
    )
    assert events[1].data["pnp_device"] == (
      arctis_device.PnPDeviceState(**changed_device)
    )
    assert events[2].data["pnp_device"] is None

  def test_dongle_disconnect_clears_pnp_device(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(
        dongle_connected=True,
        pnp_device=arctis_device.PnPDeviceState(
          audio_render_instance_id="SWD\\RENDER",
          audio_capture_instance_id="SWD\\CAPTURE",
        ),
      ),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "dongle_connected": False,
      },
    })

    assert device.state.pnp_device is None

    pnp_events = [
      event
      for event in events
      if event.event_type == "pnp_device_disconnected"
    ]

    assert len(pnp_events) == 1
    assert pnp_events[0].data["pnp_device"] is None
    assert pnp_events[0].state.pnp_device is None

  def test_event_contains_immutable_state_snapshot(self):
    events = []
    device = create_device(
      state=arctis_device.DeviceState(
        dongle_connected=True,
      ),
      on_event=events.append,
    )

    device._process_message({
      "status": {
        "pnp_device": {
          "audio_render_instance_id": None,
          "audio_capture_instance_id": None,
        },
      },
    })

    assert len(events) == 1

    first_event = events[0]
    first_snapshot = first_event.state

    device._process_message({
      "status": {
        "pnp_device": {
          "audio_render_instance_id": None,
          "audio_capture_instance_id": None,
        },
      },
    })

    assert len(events) == 1
    assert first_event.state is first_snapshot
    assert device.state is first_snapshot
    assert first_snapshot.pnp_device is not None

    with pytest.raises(AttributeError):
      first_snapshot.pnp_device.audio_render_instance_id = (
        "MODIFIED"
      )

  def test_add_listener_receives_events(self):
    events = []
    device = create_device()

    device.add_listener(events.append)

    device._process_message({
      "status": {
        "dongle_connected": True,
      },
    })

    assert len(events) == 1
    assert events[0].event_type == "dongle_connection_changed"
    assert events[0].data == {
      "dongle_connected": True,
    }

  def test_remove_listener_stops_receiving_events(self):
    events = []
    device = create_device()

    device.add_listener(events.append)
    device.remove_listener(events.append)

    device._process_message({
      "status": {
        "dongle_connected": True,
      },
    })

    assert events == []

  def test_add_listener_does_not_register_duplicate(self):
    events = []
    device = create_device()

    device.add_listener(events.append)
    device.add_listener(events.append)

    device._process_message({
      "status": {
        "dongle_connected": True,
      },
    })

    assert len(events) == 1

# END TestEvents



# ============================================================
# TestIntegration
# ============================================================

class TestIntegration:
  def test_connection_manager_messages_update_device_state_and_emit_events(
    self,
    monkeypatch,
  ):
    import threading
    import time

    manager_instances = []
    events = []
    callback_threads = []

    class TrackingConnectionManager(FakeConnectionManager):
      def __init__(self, config, message_queue):
        super().__init__(config, message_queue)
        manager_instances.append(self)

      def publish(self, message):
        self.message_queue.put(message)

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      TrackingConnectionManager,
    )

    device = arctis_device.ArctisDevice(
      "arctis_7_plus.yml",
      lambda event: (
        events.append(event),
        callback_threads.append(threading.current_thread()),
      ),
    )

    try:
      manager = manager_instances[0]

      manager.publish({
        "status": {
          "dongle_connected": True,
          "headset_on": "on",
          "battery_level": 3,
          "battery_status": "charging",
          "mixdial_game": 75,
          "mixdial_chat": 25,
        },
      })

      deadline = time.monotonic() + 2

      while (
        (
          device.state.battery_level != 3
          or device.state.mixdial_game != 75
          or device.state.mixdial_chat != 25
        )
        and time.monotonic() < deadline
      ):
        time.sleep(0.01)

      assert device.state == arctis_device.DeviceState(
        dongle_connected=True,
        headset_on="on",
        usb_connected=False,
        battery_level=3,
        battery_status="charging",
        mixdial_game=75,
        mixdial_chat=25,
      )

      assert [
        event.event_type
        for event in events
      ] == [
        "dongle_connection_changed",
        "headset_power_changed",
        "battery_changed",
        "battery_status_changed",
        "mixdial_changed",
      ]

      assert all(
        thread is device._consumer_thread
        for thread in callback_threads
      )

    finally:
      device.stop()

  def test_real_connection_manager_delivers_discovery_and_worker_messages(
    self,
    monkeypatch,
  ):
    import threading
    import time

    import hid_worker

    events = []
    callback_threads = []
    notifications = []

    class IntegrationNotifications:
      def __init__(self, events, **kwargs):
        self.events = events
        self.started = False
        self.stopped = False
        notifications.append(self)

      def start(self):
        self.started = True

      def stop(self):
        self.stopped = True

    class IntegrationWorker:
      def __init__(
        self,
        worker_id,
        interface_config,
        device_path,
        message_queue,
      ):
        self.worker_id = worker_id
        self.interface_config = interface_config
        self.device_path = device_path
        self.message_queue = message_queue
        self.alive = False
        self.stopped = False
        self.joined = False

      def set_usb_connected(self, connected):
        self.usb_connected = connected

      def start(self):
        self.alive = True

        if self.interface_config.interface_id == 3:
          self.message_queue.put(
            hid_worker.WorkerMessage(
              worker_id=self.worker_id,
              interface_id=self.interface_config.interface_id,
              message_type=hid_worker.WorkerMessageType.READY,
              payload={
                "status": {
                  "headset_on": "on",
                },
              },
            )
          )

          self.message_queue.put(
            hid_worker.WorkerMessage(
              worker_id=self.worker_id,
              interface_id=self.interface_config.interface_id,
              message_type=hid_worker.WorkerMessageType.MESSAGE,
              payload={
                "status": {
                  "battery_level": 3,
                  "battery_status": "charging",
                },
              },
            )
          )

      def stop(self):
        self.stopped = True

      def join(self):
        self.joined = True
        self.alive = False

      def is_alive(self):
        return self.alive

    class IntegrationConnectionManager(
      connection_manager.ConnectionManager
    ):
      def __init__(self, config, message_queue):
        super().__init__(config, message_queue)

        paths = {
          interface_id: f"path-{interface_id}".encode()
          for interface_id in config.interfaces
        }

        self._discover_devices = lambda: (
          connection_manager.DiscoveryResult(
            dongle_connected=True,
            headset_usb_connected=True,
            interface_paths=paths,
            unavailable_interfaces=frozenset(),
            ambiguous_interfaces=frozenset(),
          )
        )

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      IntegrationConnectionManager,
    )
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      IntegrationWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      IntegrationNotifications,
    )

    device = arctis_device.ArctisDevice(
      "arctis_7_plus.yml",
      lambda event: (
        events.append(event),
        callback_threads.append(threading.current_thread()),
      ),
    )

    try:
      deadline = time.monotonic() + 2

      while (
        device.state.battery_level != 3
        and time.monotonic() < deadline
      ):
        time.sleep(0.01)

      assert device.state == arctis_device.DeviceState(
        dongle_connected=True,
        headset_on="on",
        usb_connected=True,
        battery_level=3,
        battery_status="charging",
        mixdial_game=None,
        mixdial_chat=None,
      )

      assert [
        event.event_type
        for event in events
      ] == [
        "dongle_connection_changed",
        "usb_connection_changed",
        "headset_power_changed",
        "battery_status_changed",
        "battery_changed",
        "battery_status_changed",
      ]

      assert all(
        thread is device._consumer_thread
        for thread in callback_threads
      )

      assert len(notifications) == 1
      assert notifications[0].started

    finally:
      device.stop()

    assert notifications[0].stopped
    assert not device._consumer_thread.is_alive()


  def test_disconnect_and_reconnection_restarts_workers_without_duplicate_events(
    self,
    monkeypatch,
  ):
    import time

    import connection_manager
    import hid_worker

    events = []
    notifications = []
    workers = []
    managers = []

    class IntegrationNotifications:
      def __init__(self, events, **kwargs):
        self.events = events
        self.started = False
        self.stopped = False
        notifications.append(self)

      def start(self):
        self.started = True

      def stop(self):
        self.stopped = True

    class IntegrationWorker:
      def __init__(
        self,
        worker_id,
        interface_config,
        device_path,
        message_queue,
      ):
        self.worker_id = worker_id
        self.interface_config = interface_config
        self.device_path = device_path
        self.message_queue = message_queue
        self.alive = False
        self.stopped = False
        self.joined = False
        workers.append(self)

      def set_usb_connected(self, connected):
        self.usb_connected = connected

      def start(self):
        self.alive = True

        if self.interface_config.interface_id == 3:
          self.message_queue.put(
            hid_worker.WorkerMessage(
              worker_id=self.worker_id,
              interface_id=3,
              message_type=hid_worker.WorkerMessageType.READY,
              payload={
                "status": {
                  "headset_on": "on",
                },
              },
            )
          )

          self.message_queue.put(
            hid_worker.WorkerMessage(
              worker_id=self.worker_id,
              interface_id=3,
              message_type=hid_worker.WorkerMessageType.MESSAGE,
              payload={
                "status": {
                  "battery_level": 3,
                },
              },
            )
          )

      def stop(self):
        if self.stopped:
          return

        self.stopped = True
        self.alive = False

        self.message_queue.put(
          hid_worker.WorkerMessage(
            worker_id=self.worker_id,
            interface_id=self.interface_config.interface_id,
            message_type=hid_worker.WorkerMessageType.TERMINATED,
          )
        )

      def join(self):
        self.joined = True
        self.alive = False

      def is_alive(self):
        return self.alive

    initial_paths = {
      interface_id: f"path-{interface_id}".encode()
      for interface_id in range(3, 6)
    }

    reconnected_paths = {
      interface_id: f"new-path-{interface_id}".encode()
      for interface_id in range(3, 6)
    }

    def discovery(dongle_connected, paths=None):
      return connection_manager.DiscoveryResult(
        dongle_connected=dongle_connected,
        headset_usb_connected=False,
        interface_paths=(
          paths if dongle_connected and paths is not None else {}
        ),
        unavailable_interfaces=frozenset(),
        ambiguous_interfaces=frozenset(),
      )

    discoveries = [
      discovery(True, initial_paths),
      discovery(False),
      discovery(True, reconnected_paths),
    ]

    class IntegrationConnectionManager(
      connection_manager.ConnectionManager
    ):
      RETRY_INTERVAL = 10.0

      def __init__(self, config, message_queue):
        super().__init__(config, message_queue)
        managers.append(self)

      def _discover_devices(self):
        return discoveries.pop(0)

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      IntegrationConnectionManager,
    )
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      IntegrationWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      IntegrationNotifications,
    )

    device = arctis_device.ArctisDevice(
      "arctis_7_plus.yml",
      events.append,
    )

    manager = managers[0]

    def wait_until(predicate, timeout=3):
      deadline = time.monotonic() + timeout

      while time.monotonic() < deadline:
        if predicate():
          return True

        time.sleep(0.01)

      return predicate()

    try:
      # Initial discovery and worker startup.
      assert wait_until(
        lambda: (
          device.state.dongle_connected
          and device.state.battery_level == 3
        )
      )

      initial_workers = list(workers)

      assert len(initial_workers) == len(initial_paths)

      assert {
        worker.interface_config.interface_id: worker.device_path
        for worker in initial_workers
      } == initial_paths
      assert all(worker.alive for worker in initial_workers)

      # Dongle removal.
      notifications[0].events.put(
        connection_manager.DeviceNotification(
          connection_manager.DeviceEventType.REMOVAL
        )
      )

      assert wait_until(
        lambda: not device.state.dongle_connected
      )

      assert wait_until(
        lambda: all(
          runtime.worker is None
          for runtime in manager._interfaces.values()
        )
      )

      assert all(
        worker.stopped and worker.joined
        for worker in initial_workers
      )

      # Dongle arrival.
      notifications[0].events.put(
        connection_manager.DeviceNotification(
          connection_manager.DeviceEventType.ARRIVAL
        )
      )

      assert wait_until(
        lambda: (
          device.state.dongle_connected
          and len(workers) == (
            len(initial_paths) + len(reconnected_paths)
          )
          and manager._interfaces[3].status == "available"
        )
      )

      new_workers = workers[len(initial_workers):]

      assert len(new_workers) == len(reconnected_paths)
      assert all(worker.alive for worker in new_workers)

      assert {
        worker.interface_config.interface_id: worker.device_path
        for worker in new_workers
      } == reconnected_paths

      assert all(
        old.worker_id != new.worker_id
        for old in initial_workers
        for new in new_workers
      )

      # Physical connection events must not be duplicated.
      dongle_events = [
        event.data["dongle_connected"]
        for event in events
        if event.event_type == "dongle_connection_changed"
      ]

      assert dongle_events == [True, False, True]

      assert [
        event.data["headset_on"]
        for event in events
        if event.event_type == "headset_power_changed"
      ] == ["on", None, "on"]

      assert [
        event.data["battery_level"]
        for event in events
        if event.event_type == "battery_changed"
      ] == [3, None, 3]

    finally:
      device.stop()

    assert not device._consumer_thread.is_alive()


  def test_physical_connection_states_are_independent(
    self,
    monkeypatch,
  ):
    import time

    import connection_manager

    events = []
    notifications = []

    class IntegrationNotifications:
      def __init__(self, events, **kwargs):
        self.events = events
        notifications.append(self)

      def start(self):
        pass

      def stop(self):
        pass

    states = [
      (False, False),
      (True, False),
      (False, True),
      (True, True),
      (False, False),
    ]

    discoveries = [
      connection_manager.DiscoveryResult(
        dongle_connected=dongle_connected,
        headset_usb_connected=headset_usb_connected,
        interface_paths=(
          {
            interface_id: f"path-{interface_id}".encode()
            for interface_id in range(3, 6)
          }
          if dongle_connected else {}
        ),
        unavailable_interfaces=frozenset(),
        ambiguous_interfaces=frozenset(),
      )
      for dongle_connected, headset_usb_connected in states
    ]

    class IntegrationConnectionManager(
      connection_manager.ConnectionManager
    ):
      def _discover_devices(self):
        return discoveries.pop(0)

      def _start_worker(self, interface_id, device_path):
        pass

    monkeypatch.setattr(
      arctis_device,
      "ConnectionManager",
      IntegrationConnectionManager,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      IntegrationNotifications,
    )

    device = arctis_device.ArctisDevice(
      "arctis_7_plus.yml",
      events.append,
    )

    def wait_until(predicate, timeout=3):
      deadline = time.monotonic() + timeout

      while time.monotonic() < deadline:
        if predicate():
          return True

        time.sleep(0.01)

      return predicate()

    expected_states = [
      (True, False),
      (False, True),
      (True, True),
      (False, False),
    ]

    try:
      assert wait_until(
        lambda: device.state == arctis_device.DeviceState(
          dongle_connected=False,
          usb_connected=False,
        )
      )

      for expected_dongle, expected_usb in expected_states:
        event_type = (
          connection_manager.DeviceEventType.ARRIVAL
          if expected_dongle or expected_usb
          else connection_manager.DeviceEventType.REMOVAL
        )

        notifications[0].events.put(
          connection_manager.DeviceNotification(event_type)
        )

        assert wait_until(
          lambda: (
            device.state.dongle_connected == expected_dongle
            and device.state.usb_connected == expected_usb
          )
        )

      assert [
        (
          event.event_type,
          dict(event.data),
        )
        for event in events
      ] == [
        (
          "dongle_connection_changed",
          {"dongle_connected": True},
        ),
        (
          "dongle_connection_changed",
          {"dongle_connected": False},
        ),
        (
          "usb_connection_changed",
          {"usb_connected": True},
        ),
        (
          "dongle_connection_changed",
          {"dongle_connected": True},
        ),
        (
          "dongle_connection_changed",
          {"dongle_connected": False},
        ),
        (
          "usb_connection_changed",
          {"usb_connected": False},
        ),
      ]

    finally:
      device.stop()

# END TestIntegration
