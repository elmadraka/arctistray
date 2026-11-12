import threading
import queue
import time
import pytest

import connection_manager
from device_profile import load_device_profile

class FakeNotifications:
  def __init__(self, events, **kwargs):
    self.started = False
    self.stopped = False

  def start(self):
    self.started = True

  def stop(self):
    self.stopped = True

@pytest.fixture
def config():
  return load_device_profile("arctis_7_plus.yml")

def make_manager(config):
  return connection_manager.ConnectionManager(
    config,
    queue.Queue()
  )

def make_hid_entry(config, interface_id, path, product_id=None):
  interface = config.interfaces[interface_id]

  return {
    "vendor_id": config.vendor_id,
    "product_id": (
      config.dongle_id if product_id is None else product_id
    ),
    "usage_page": interface.config["usage_page"],
    "usage": interface.config["usage"],
    "path": path,
  }

def make_discovery(config, paths, dongle_connected=True, headset_usb_connected=False):
  return connection_manager.DiscoveryResult(
    dongle_connected=dongle_connected,
    headset_usb_connected=headset_usb_connected,
    interface_paths=paths,
    unavailable_interfaces=frozenset(
      set(config.interfaces) - set(paths)
      if dongle_connected else ()
    ),
    ambiguous_interfaces=frozenset(),
  )

def make_notification(event_type):
  return connection_manager.DeviceNotification(event_type)


class TestInitialization:
  def test_constructor_has_no_side_effects(self, config, monkeypatch):
    def unexpected_enumeration(*args, **kwargs):
      pytest.fail("HID enumeration must not run during construction")

    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      unexpected_enumeration,
    )

    manager = connection_manager.ConnectionManager(
      config,
      queue.Queue()
    )

    assert manager._thread is None
    assert manager._notifications is None
    assert manager._status == "stopped"
    assert manager._started is False


  def test_initial_state(self, config):
    manager = make_manager(config)

    state = manager._state

    assert state.dongle_connected is False
    assert state.headset_usb_connected is False
    assert set(state.interfaces) == set(config.interfaces)

    for interface in state.interfaces.values():
      assert interface.status == "waiting"

  def test_initial_discovery_failure_keeps_coordinator_running(
    self,
    config,
    monkeypatch,
  ):
    discovery_failed = threading.Event()
    waiting_for_events = threading.Event()

    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )
    monkeypatch.setattr(
      connection_manager,
      "hid",
      type(
        "FakeHID",
        (),
        {"enumerate": staticmethod(lambda *args, **kwargs: [])},
      ),
    )

    manager = make_manager(config)

    def fail_discovery():
      discovery_failed.set()
      raise RuntimeError("simulated initial discovery failure")

    def get_timeout():
      waiting_for_events.set()
      return 0.05

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      fail_discovery,
    )
    monkeypatch.setattr(
      manager,
      "_get_next_retry_timeout",
      get_timeout,
    )

    manager.start()

    assert discovery_failed.wait(timeout=2)
    assert waiting_for_events.wait(timeout=2)

    assert manager._thread.is_alive()
    assert manager._status == "running"

    manager.stop()

    assert manager._status == "stopped"
    assert not manager._thread.is_alive()
    assert manager._notifications is None

  def test_initial_discovery_failure_schedules_retry(
    self,
    config,
    monkeypatch,
  ):
    discovery_failed = threading.Event()
    waiting_for_events = threading.Event()

    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)

    def fail_discovery():
      discovery_failed.set()
      raise RuntimeError("simulated initial discovery failure")

    def get_timeout():
      waiting_for_events.set()
      return 0.05

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      fail_discovery,
    )
    monkeypatch.setattr(
      manager,
      "_get_next_retry_timeout",
      get_timeout,
    )

    manager.start()

    assert discovery_failed.wait(timeout=2)
    assert waiting_for_events.wait(timeout=2)

    for runtime in manager._interfaces.values():
      assert runtime.next_retry_at == (
        100.0 + manager.RETRY_INTERVAL
      )

    manager.stop()

  def test_initial_discovery_failure_recovers_on_device_arrival(
    self,
    config,
    monkeypatch,
  ):
    discovery_failed = threading.Event()
    recovery_completed = threading.Event()

    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )
    monkeypatch.setattr(
      connection_manager,
      "hid",
      type(
        "FakeHID",
        (),
        {"enumerate": staticmethod(lambda *args, **kwargs: [])},
      ),
    )

    manager = make_manager(config)

    discovery_calls = 0
    started_interfaces = []

    def discover():
      nonlocal discovery_calls
      discovery_calls += 1

      if discovery_calls == 1:
        discovery_failed.set()
        raise RuntimeError("simulated initial discovery failure")

      return make_discovery(
        config,
        {
          interface_id: f"path-{interface_id}".encode()
          for interface_id in config.interfaces
        },
      )

    original_start_worker = manager._start_worker

    def track_worker_start(interface_id, device_path):
      original_start_worker(interface_id, device_path)
      started_interfaces.append(interface_id)

      if len(started_interfaces) == len(config.interfaces):
        recovery_completed.set()

    monkeypatch.setattr(manager, "_discover_devices", discover)
    monkeypatch.setattr(manager, "_start_worker", track_worker_start)

    manager.start()

    assert discovery_failed.wait(timeout=2)
    assert manager._thread.is_alive()

    manager._events.put(
      connection_manager.DeviceNotification(
        event_type=connection_manager.DeviceEventType.ARRIVAL,
      )
    )

    assert recovery_completed.wait(timeout=2)

    assert discovery_calls == 2
    assert set(started_interfaces) == set(config.interfaces)

    for interface_id in config.interfaces:
      runtime = manager._interfaces[interface_id]

      assert runtime.worker is not None
      assert runtime.worker.started is True
      assert runtime.path == f"path-{interface_id}".encode()

    manager.stop()

    assert manager._status == "stopped"
    assert not manager._thread.is_alive()

  def test_initial_discovery_schedules_retry_for_missing_and_ambiguous_interfaces(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)

    interface_ids = list(config.interfaces)
    available_id = interface_ids[0]
    ambiguous_id = interface_ids[1]

    discovery = connection_manager.DiscoveryResult(
      dongle_connected=True,
      headset_usb_connected=False,
      interface_paths={
        available_id: b"available-interface",
      },
      unavailable_interfaces=frozenset(
        set(interface_ids) - {available_id, ambiguous_id}
      ),
      ambiguous_interfaces=frozenset({ambiguous_id}),
    )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: discovery,
    )

    manager._events.put(
      connection_manager.ManagerEvent(
        event_type=connection_manager.ManagerEventType.STOP,
      )
    )

    published_states = []
    original_publish_state = manager._publish_state

    def capture_state():
      published_states.append(manager._build_state())
      original_publish_state()

    monkeypatch.setattr(manager, "_publish_state", capture_state)

    manager.start()
    manager._thread.join(timeout=2)

    assert len(published_states) >= 1

    initial_state = published_states[0]

    for interface_id, runtime_state in initial_state.interfaces.items():
      if interface_id == available_id:
        assert runtime_state.status == "waiting"
      else:
        assert runtime_state.status == "unavailable"

    for interface_id, runtime in manager._interfaces.items():
      if interface_id == available_id:
        assert runtime.next_retry_at is None
      else:
        assert runtime.next_retry_at == (
          100.0 + manager.RETRY_INTERVAL
        )


  def test_stop_during_initial_discovery_prevents_worker_start(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )

    manager = make_manager(config)

    started_interfaces = []

    def discover_devices():
      # Simulate the public stop() request during initial discovery.
      manager._stop_requested.set()
      manager._events.put(
        connection_manager.ManagerEvent(
          connection_manager.ManagerEventType.STOP
        )
      )

      return make_discovery(
        config,
        {
          interface_id: f"path-{interface_id}".encode()
          for interface_id in config.interfaces
        },
      )

    original_start_worker = manager._start_worker

    def track_worker_start(interface_id, device_path):
      started_interfaces.append(interface_id)
      original_start_worker(interface_id, device_path)

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      discover_devices,
    )
    monkeypatch.setattr(
      manager,
      "_start_worker",
      track_worker_start,
    )

    manager.start()
    manager._thread.join(timeout=2)

    assert not manager._thread.is_alive()
    assert started_interfaces == []

    for runtime in manager._interfaces.values():
      assert runtime.worker is None

    assert manager._status == "stopped"
    assert manager._notifications is None


  def test_stop_during_worker_start_stops_new_worker(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )

    manager = make_manager(config)

    started_workers = []

    def discover_devices():
      return make_discovery(
        config,
        {
          interface_id: f"path-{interface_id}".encode()
          for interface_id in config.interfaces
        },
      )

    original_start_worker = manager._start_worker

    def start_worker(interface_id, device_path):
      original_start_worker(interface_id, device_path)

      worker = manager._interfaces[interface_id].worker
      started_workers.append(worker)

      if len(started_workers) == 1:
        manager._stop_requested.set()
        manager._events.put(
          connection_manager.ManagerEvent(
            connection_manager.ManagerEventType.STOP
          )
        )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      discover_devices,
    )
    monkeypatch.setattr(
      manager,
      "_start_worker",
      start_worker,
    )

    manager.start()
    manager._thread.join(timeout=2)

    assert not manager._thread.is_alive()
    assert len(started_workers) == 1

    worker = started_workers[0]

    assert worker.started is True
    assert worker.stopped is True
    assert worker.joined is True

    for runtime in manager._interfaces.values():
      assert runtime.worker is None

    assert manager._status == "stopped"
    assert manager._notifications is None

# END TestInitialization

class TestDiscovery:
  def test_discover_devices_with_all_interfaces(self, config, monkeypatch):
    devices = [
      make_hid_entry(config, interface_id, f"path-{interface_id}".encode())
      for interface_id in config.interfaces
    ]

    devices.append(
      {
        "vendor_id": config.vendor_id,
        "product_id": config.headset_id,
        "usage_page": 0,
        "usage": 0,
        "path": b"headset-usb",
      }
    )

    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      lambda vendor_id, product_id: devices,
    )

    manager = make_manager(config)
    result = manager._discover_devices()

    assert result.dongle_connected is True
    assert result.headset_usb_connected is True
    assert dict(result.interface_paths) == {
      interface_id: f"path-{interface_id}".encode()
      for interface_id in config.interfaces
    }
    assert result.unavailable_interfaces == frozenset()
    assert result.ambiguous_interfaces == frozenset()


  def test_discover_devices_with_missing_interface(self, config, monkeypatch):
    interface_ids = list(config.interfaces)
    missing_id = interface_ids[-1]

    devices = [
      make_hid_entry(config, interface_id, f"path-{interface_id}".encode())
      for interface_id in interface_ids
      if interface_id != missing_id
    ]

    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      lambda vendor_id, product_id: devices,
    )

    manager = make_manager(config)
    result = manager._discover_devices()

    assert result.dongle_connected is True
    assert missing_id not in result.interface_paths
    assert result.unavailable_interfaces == frozenset({missing_id})
    assert result.ambiguous_interfaces == frozenset()


  def test_discover_devices_with_ambiguous_interface(self, config, monkeypatch):
    interface_ids = list(config.interfaces)
    ambiguous_id = interface_ids[0]

    devices = [
      make_hid_entry(config, interface_id, f"path-{interface_id}".encode())
      for interface_id in interface_ids
    ]

    devices.append(
      make_hid_entry(config, ambiguous_id, b"duplicate-path")
    )

    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      lambda vendor_id, product_id: devices,
    )

    manager = make_manager(config)
    result = manager._discover_devices()

    assert result.dongle_connected is True
    assert ambiguous_id not in result.interface_paths
    assert result.ambiguous_interfaces == frozenset({ambiguous_id})
    assert result.unavailable_interfaces == frozenset()


  def test_discover_devices_without_dongle(self, config, monkeypatch):
    devices = [
      {
        "vendor_id": config.vendor_id,
        "product_id": config.headset_id,
        "usage_page": 0,
        "usage": 0,
        "path": b"headset-usb",
      }
    ]

    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      lambda vendor_id, product_id: devices,
    )

    manager = make_manager(config)
    result = manager._discover_devices()

    assert result.dongle_connected is False
    assert result.headset_usb_connected is True
    assert result.interface_paths == {}
    assert result.unavailable_interfaces == frozenset()
    assert result.ambiguous_interfaces == frozenset()

# END TestDiscovery

class FakeWorker:
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
    self.started = False
    self.stopped = False
    self.joined = False
    self.alive = False
    self.join_error = None
    self.fast_polling_calls = []
    self.call_order = []

  def start(self):
    self.call_order.append("start")
    self.started = True
    self.alive = True

  def start_fast_polling(self, interval, duration):
    self.fast_polling_calls.append((interval, duration))
    self.call_order.append("start_fast_polling")

  def stop_fast_polling(self):
    self.call_order.append("stop_fast_polling")

  def join(self):
    self.joined = True

    if self.join_error is not None:
      raise self.join_error

    self.alive = False

  def is_alive(self):
    return self.alive

  def stop(self):
    self.stopped = True

class FakePnPWorker:
  def __init__(
    self,
    worker_id,
    session_id,
    hid_path,
    message_queue,
  ):
    self.worker_id = worker_id
    self.session_id = session_id
    self.hid_path = hid_path
    self.message_queue = message_queue

    self.started = False
    self.stopped = False
    self.joined = False
    self.alive = False

  def start(self):
    self.started = True
    self.alive = True

  def stop(self):
    self.stopped = True

  def join(self):
    self.joined = True
    self.alive = False

  def is_alive(self):
    return self.alive

class TestWorkerLifecycle:
  def test_start_worker(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    device_path = b"interface-3"

    manager._start_worker(3, device_path)

    runtime = manager._interfaces[3]
    worker = runtime.worker

    assert isinstance(worker, FakeWorker)
    assert worker.worker_id == 1
    assert worker.interface_config is config.interfaces[3]
    assert worker.device_path == device_path
    assert worker.message_queue is manager._events
    assert worker.started is True

    assert runtime.path == device_path
    assert runtime.worker_closing is False
    assert runtime.next_retry_at is None


  def test_start_worker_rejects_existing_worker(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)

    manager._start_worker(3, b"interface-3")
    original_worker = manager._interfaces[3].worker

    with pytest.raises(
      RuntimeError,
      match="already has a worker",
    ):
      manager._start_worker(3, b"another-interface-3")

    assert manager._interfaces[3].worker is original_worker
    assert original_worker.started is True

  def test_initial_discovery_starts_each_worker_only_once(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "PnPWorker",
      FakePnPWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )
    monkeypatch.setattr(
      connection_manager,
      "hid",
      type(
        "FakeHID",
        (),
        {"enumerate": staticmethod(lambda: [])},
      ),
    )

    manager = make_manager(config)

    discovery = make_discovery(
      config,
      {
        interface_id: f"path-{interface_id}".encode()
        for interface_id in config.interfaces
      },
    )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: discovery,
    )

    manager.start()

    try:
      assert manager._thread is not None

      deadline = time.monotonic() + 2

      while (
        manager._worker_id_counter < len(config.interfaces)
        and time.monotonic() < deadline
      ):
        threading.Event().wait(0.01)

      assert manager._worker_id_counter == len(config.interfaces)

      for interface_id in config.interfaces:
        runtime = manager._interfaces[interface_id]

        assert runtime.worker is not None
        assert runtime.worker.started is True
        assert runtime.worker.device_path == (
          f"path-{interface_id}".encode()
        )
    finally:
      manager.stop()

  def test_worker_start_failure_schedules_another_retry(
    self,
    config,
    monkeypatch,
  ):
    class FailingWorker(FakeWorker):
      def start(self):
        raise OSError("unable to open HID interface")

    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FailingWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    devices = [
      make_hid_entry(config, 3, b"interface-3"),
    ]

    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      lambda vendor_id, product_id: devices,
    )

    manager = make_manager(config)
    runtime = manager._interfaces[3]
    runtime.status = "waiting"
    runtime.next_retry_at = 100.0

    manager._process_due_retries()

    assert runtime.worker is None
    assert runtime.path == b"interface-3"
    assert runtime.status == "waiting"
    assert runtime.next_retry_at == (
      100.0 + manager.RETRY_INTERVAL
    )

  def test_worker_construction_failure_schedules_another_retry(
    self,
    config,
    monkeypatch,
  ):
    def fail_construction(**kwargs):
      raise RuntimeError("Worker construction failed")

    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      fail_construction,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)
    runtime = manager._interfaces[3]

    with pytest.raises(
      RuntimeError,
      match="Worker construction failed",
    ):
      manager._start_worker(3, b"interface-3")

    assert runtime.worker is None
    assert runtime.path == b"interface-3"
    assert runtime.worker_closing is False
    assert runtime.status == "unavailable"
    assert runtime.next_retry_at == 102.0

  def test_start_worker_does_not_set_usb_state(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._headset_usb_connected = True

    manager._start_worker(3, b"interface-3")

    worker = manager._interfaces[3].worker

    assert worker is not None
    assert not hasattr(worker, "usb_connected")
    assert worker.fast_polling_calls == []

  def test_update_usb_connection_starts_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._headset_usb_connected = False
    manager._dongle_connected = True
    manager._headset_on = "on"
    manager._start_worker(3, b"interface-3")

    worker = manager._interfaces[3].worker

    manager._set_headset_usb_connected(True)

    assert worker.fast_polling_calls == [(0.5, 30.0)]
    assert worker.call_order.count("start") == 1

  def test_usb_connection_after_headset_power_starts_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    manager, worker = TestHeadsetPowerPolling().make_manager_with_if3(
      config,
      monkeypatch,
    )

    manager._observe_headset_power({
      "status": {
        "headset": "on",
      },
    })

    assert worker.fast_polling_calls == []

    manager._set_headset_usb_connected(True)

    assert worker.fast_polling_calls == [(0.5, 30.0)]

  def test_headset_power_after_usb_connection_starts_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    manager, worker = TestHeadsetPowerPolling().make_manager_with_if3(
      config,
      monkeypatch,
    )

    manager._set_headset_usb_connected(True)

    assert worker.fast_polling_calls == []

    manager._observe_headset_power({
      "status": {
        "headset": "on",
      },
    })

    assert worker.fast_polling_calls == [(0.5, 30.0)]

  def test_usb_disconnect_stops_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    manager, worker = TestHeadsetPowerPolling().make_manager_with_if3(
      config,
      monkeypatch,
    )

    manager._headset_on = "on"
    manager._set_headset_usb_connected(True)
    manager._set_headset_usb_connected(False)

    assert worker.fast_polling_calls == [(0.5, 30.0)]
    assert worker.call_order.count("stop_fast_polling") == 1

  def test_headset_power_off_stops_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    manager, worker = TestHeadsetPowerPolling().make_manager_with_if3(
      config,
      monkeypatch,
    )

    manager._headset_usb_connected = True
    manager._observe_headset_power({
      "status": {
        "headset": "on",
      },
    })
    manager._observe_headset_power({
      "status": {
        "headset": "off",
      },
    })

    assert worker.fast_polling_calls == [(0.5, 30.0)]
    assert worker.call_order.count("stop_fast_polling") == 1

  def test_update_usb_connection_ignores_closing_worker(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._headset_usb_connected = False
    manager._dongle_connected = True
    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker

    runtime.worker_closing = True
    manager._set_headset_usb_connected(True)

    assert worker.fast_polling_calls == []

#END TestWorkerLifecycle

class TestWorkerMessages:
  def test_ready_message_marks_interface_available(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = connection_manager.ConnectionManager(
      config,
      queue.Queue()
    )

    manager._start_worker(3, b"interface-3")

    worker = manager._interfaces[3].worker

    message = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.READY,
    )

    manager._handle_worker_message(message)

    assert manager._interfaces[3].status == "available"
    assert manager._state.interfaces[3].status == "available"


  def test_ready_message_forwards_initial_payload(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    device_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      device_queue
    )

    manager._start_worker(3, b"interface-3")

    worker = manager._interfaces[3].worker
    payload = {
      "status": {
        "headset_powered_on": True,
      },
    }

    message = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.READY,
      payload=payload,
    )

    manager._handle_worker_message(message)

    assert device_queue.get_nowait() == payload
    assert manager._interfaces[3].status == "available"


  def test_message_forwards_payload_without_changing_status(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    device_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      device_queue
    )

    manager._start_worker(3, b"interface-3")

    worker = manager._interfaces[3].worker
    payload = {
      "status": {
        "battery_level": 3,
      },
    }

    message = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.MESSAGE,
      payload=payload,
    )

    manager._handle_worker_message(message)

    assert device_queue.get_nowait() == payload
    assert manager._interfaces[3].status == "waiting"

  def test_message_from_old_worker_is_ignored(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    device_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      device_queue
    )

    manager._start_worker(5, b"interface-5")

    current_worker = manager._interfaces[5].worker

    message = connection_manager.WorkerMessage(
      worker_id=current_worker.worker_id + 1,
      interface_id=5,
      message_type=connection_manager.WorkerMessageType.MESSAGE,
      payload={
        "status": {
          "headset": "on",
        },
      },
    )

    manager._handle_worker_message(message)

    assert device_queue.empty()
    assert manager._headset_on is None
    assert manager._interfaces[5].worker is current_worker


  def test_message_from_closing_worker_is_ignored(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    device_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      device_queue
    )

    manager._start_worker(5, b"interface-5")

    runtime = manager._interfaces[5]
    worker = runtime.worker
    runtime.worker_closing = True

    message = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=5,
      message_type=connection_manager.WorkerMessageType.MESSAGE,
      payload={
        "status": {
          "headset": "on",
        },
      },
    )

    manager._handle_worker_message(message)

    assert device_queue.empty()
    assert manager._headset_on is None
    assert runtime.worker is worker
    assert runtime.worker_closing is True

  def test_empty_message_payload_is_ignored(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    device_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      device_queue
    )

    manager._start_worker(3, b"interface-3")

    worker = manager._interfaces[3].worker

    message = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.MESSAGE,
      payload={},
    )

    manager._handle_worker_message(message)

    assert device_queue.empty()
    assert manager._interfaces[3].status == "waiting"


  def test_error_message_stops_worker_and_marks_closing(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    worker = manager._interfaces[3].worker

    message = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.ERROR,
      payload={"error": "read failed"},
    )

    manager._handle_worker_message(message)

    assert worker.stopped is True
    assert manager._interfaces[3].worker is worker
    assert manager._interfaces[3].worker_closing is True
    assert manager._interfaces[3].status == "restarting"


  def test_ready_message_is_ignored_while_worker_closing(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    device_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      device_queue
    )

    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker
    runtime.worker_closing = True

    message = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.READY,
      payload={"status": {"battery_level": 3}},
    )

    manager._handle_worker_message(message)

    assert runtime.status == "waiting"
    assert device_queue.empty()


  def test_control_message_from_old_worker_is_ignored(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    current_worker = manager._interfaces[3].worker

    message = connection_manager.WorkerMessage(
      worker_id=current_worker.worker_id + 1,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.ERROR,
      payload={"error": "old worker"},
    )

    manager._handle_worker_message(message)

    assert current_worker.stopped is False
    assert manager._interfaces[3].worker is current_worker
    assert manager._interfaces[3].worker_closing is False


  def test_worker_termination_completes_recovery(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker

    runtime.status = "available"

    error = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.ERROR,
      payload={"error": "read failed"},
    )

    manager._handle_worker_message(error)

    assert worker.stopped is True
    assert runtime.worker is worker
    assert runtime.worker_closing is True

    terminated = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.TERMINATED,
    )

    manager._handle_worker_message(terminated)

    assert worker.joined is True
    assert runtime.worker is None
    assert runtime.worker_closing is False
    assert runtime.status == "waiting"
    assert runtime.next_retry_at == (
      100.0 + manager.RETRY_INTERVAL
    )


  def test_termination_publishes_restarting_and_waiting_states(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    published_states = []
    manager = connection_manager.ConnectionManager(
      config,
      queue.Queue()
    )

    original_publish_state = manager._publish_state

    def capture_state():
      original_publish_state()
      published_states.append(manager._state)

    monkeypatch.setattr(
      manager,
      "_publish_state",
      capture_state,
    )

    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker
    runtime.status = "available"

    terminated = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.TERMINATED,
    )

    manager._handle_worker_message(terminated)

    interface_states = [
      state.interfaces[3].status
      for state in published_states
    ]

    assert interface_states == ["restarting", "waiting"]


  def test_termination_from_old_worker_is_ignored(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    current_worker = manager._interfaces[3].worker

    terminated = connection_manager.WorkerMessage(
      worker_id=current_worker.worker_id + 1,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.TERMINATED,
    )

    manager._handle_worker_message(terminated)

    runtime = manager._interfaces[3]

    assert runtime.worker is current_worker
    assert current_worker.joined is False
    assert runtime.status == "waiting"
    assert runtime.next_retry_at is None

  def test_worker_stop_failure_does_not_interrupt_error_handling(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)

    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker

    def fail_stop():
      raise RuntimeError("Simulated stop failure")

    worker.stop = fail_stop

    message = connection_manager.WorkerMessage(
      worker_id=worker.worker_id,
      interface_id=3,
      message_type=connection_manager.WorkerMessageType.ERROR,
      payload={"error": "read failed"},
    )

    manager._handle_worker_message(message)

    assert runtime.worker is worker
    assert runtime.worker_closing is True
    assert runtime.status == "restarting"

  def test_terminated_worker_join_failure_preserves_worker(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker

    runtime.worker_closing = True
    runtime.status = "restarting"

    worker.join_error = RuntimeError("Join failed")

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=worker.worker_id,
        interface_id=3,
        message_type=connection_manager.WorkerMessageType.TERMINATED,
      )
    )

    assert worker.joined is True
    assert worker.is_alive() is True
    assert runtime.worker is worker
    assert runtime.worker_closing is True
    assert runtime.status == "restarting"
    assert runtime.next_retry_at is None

  def test_terminated_worker_join_failure_cleans_up_dead_worker(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker

    runtime.worker_closing = True
    runtime.status = "restarting"

    worker.alive = False
    worker.join_error = RuntimeError("Join failed")

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=worker.worker_id,
        interface_id=3,
        message_type=connection_manager.WorkerMessageType.TERMINATED,
      )
    )

    assert worker.joined is True
    assert worker.is_alive() is False
    assert runtime.worker is None
    assert runtime.worker_closing is False
    assert runtime.status == "waiting"
    assert runtime.next_retry_at == 102.0

  def test_terminated_worker_join_and_alive_check_failure_preserves_worker(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker

    runtime.worker_closing = True
    runtime.status = "restarting"

    worker.join_error = RuntimeError("Join failed")

    def fail_is_alive():
      raise RuntimeError("Alive check failed")

    worker.is_alive = fail_is_alive

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=worker.worker_id,
        interface_id=3,
        message_type=connection_manager.WorkerMessageType.TERMINATED,
      )
    )

    assert runtime.worker is worker
    assert runtime.worker_closing is True
    assert runtime.status == "restarting"
    assert runtime.next_retry_at is None

#END TestWorkerMessages

class TestHeadsetPowerPolling:
  def make_manager_with_if3(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._dongle_connected = True
    manager._start_worker(3, b"interface-3")

    worker = manager._interfaces[3].worker

    return manager, worker

  def test_initial_headset_state_does_not_start_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    manager, worker = self.make_manager_with_if3(
      config,
      monkeypatch,
    )

    manager._observe_headset_power({
      "status": {
        "headset": "off",
      },
    })

    assert manager._headset_on == "off"
    assert worker.fast_polling_calls == []

  @pytest.mark.parametrize(
    ("initial_state", "new_state"),
    [
      ("off", "on"),
      ("on", "off"),
    ],
  )
  def test_headset_power_change_starts_fast_polling(
    self,
    config,
    monkeypatch,
    initial_state,
    new_state,
  ):
    manager, worker = self.make_manager_with_if3(
      config,
      monkeypatch,
    )

    manager._headset_usb_connected = True

    manager._observe_headset_power({
      "status": {
        "headset": initial_state,
      },
    })

    manager._observe_headset_power({
      "status": {
        "headset": new_state,
      },
    })

    assert manager._headset_on == new_state
    if new_state == "on":
      assert worker.fast_polling_calls == [(0.5, 30.0)]
      assert "stop_fast_polling" not in worker.call_order
    else:
      assert worker.fast_polling_calls == [(0.5, 30.0)]
      assert worker.call_order.count("stop_fast_polling") == 1

  def test_repeated_headset_state_does_not_start_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    manager, worker = self.make_manager_with_if3(
      config,
      monkeypatch,
    )

    payload = {
      "status": {
        "headset": "on",
      },
    }

    manager._observe_headset_power(payload)
    manager._observe_headset_power(payload)

    assert manager._headset_on == "on"
    assert worker.fast_polling_calls == []

  def test_invalid_headset_state_is_ignored(
    self,
    config,
    monkeypatch,
  ):
    manager, worker = self.make_manager_with_if3(
      config,
      monkeypatch,
    )

    manager._observe_headset_power(None)
    manager._observe_headset_power({})
    manager._observe_headset_power({
      "status": {},
    })
    manager._observe_headset_power({
      "status": {
        "headset": "unknown",
      },
    })

    assert manager._headset_on is None
    assert worker.fast_polling_calls == []

  def test_ready_headset_state_does_not_start_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    manager, worker = self.make_manager_with_if3(
      config,
      monkeypatch,
    )

    manager._observe_headset_power(
      {
        "status": {
          "headset": "off",
        },
      },
    )

    assert manager._headset_on == "off"
    assert worker.fast_polling_calls == []

# END TestHeadsetPowerPolling

class TestWorkerMessageHeadsetPower:
  def make_manager_with_workers(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._dongle_connected = True

    manager._start_worker(3, b"interface-3")
    manager._start_worker(5, b"interface-5")

    if3_worker = manager._interfaces[3].worker
    if5_worker = manager._interfaces[5].worker

    return manager, if3_worker, if5_worker

  def test_if5_message_headset_power_change_starts_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    device_queue = queue.Queue()

    manager, if3_worker, if5_worker = (
      self.make_manager_with_workers(config, monkeypatch)
    )
    manager._headset_usb_connected = True
    manager._device_message_queue = device_queue

    for headset_state in ("off", "on"):
      payload = {
        "status": {
          "headset": headset_state,
        },
      }

      manager._handle_worker_message(
        connection_manager.WorkerMessage(
          worker_id=if5_worker.worker_id,
          interface_id=5,
          message_type=connection_manager.WorkerMessageType.MESSAGE,
          payload=payload,
        )
      )

      assert device_queue.get_nowait() == payload

    assert manager._headset_on == "on"
    assert if3_worker.fast_polling_calls == [(0.5, 30.0)]

  def test_if5_ready_initializes_headset_power_without_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    device_queue = queue.Queue()

    manager, if3_worker, if5_worker = (
      self.make_manager_with_workers(config, monkeypatch)
    )
    manager._device_message_queue = device_queue

    payload = {
      "status": {
        "headset": "off",
      },
    }

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=if5_worker.worker_id,
        interface_id=5,
        message_type=connection_manager.WorkerMessageType.READY,
        payload=payload,
      )
    )

    assert manager._headset_on == "off"
    assert device_queue.get_nowait() == payload
    assert if3_worker.fast_polling_calls == []

  def test_if5_ready_state_allows_later_message_to_trigger_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    device_queue = queue.Queue()

    manager, if3_worker, if5_worker = (
      self.make_manager_with_workers(config, monkeypatch)
    )
    manager._headset_usb_connected = True
    manager._device_message_queue = device_queue

    initial_payload = {
      "status": {
        "headset": "off",
      },
    }

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=if5_worker.worker_id,
        interface_id=5,
        message_type=connection_manager.WorkerMessageType.READY,
        payload=initial_payload,
      )
    )

    changed_payload = {
      "status": {
        "headset": "on",
      },
    }

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=if5_worker.worker_id,
        interface_id=5,
        message_type=connection_manager.WorkerMessageType.MESSAGE,
        payload=changed_payload,
      )
    )

    assert manager._headset_on == "on"
    assert device_queue.get_nowait() == initial_payload
    assert device_queue.get_nowait() == changed_payload
    assert if3_worker.fast_polling_calls == [(0.5, 30.0)]

# END TestWorkerMessageHeadsetPower

class TestRetries:
  def test_retry_does_not_run_before_deadline(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 98.0,
    )

    def unexpected_enumeration(*args, **kwargs):
      pytest.fail("HID enumeration must not run before retry deadline")

    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      unexpected_enumeration,
    )

    manager = make_manager(config)
    runtime = manager._interfaces[3]
    runtime.status = "waiting"
    runtime.next_retry_at = 100.0

    manager._process_due_retries()

    assert runtime.worker is None
    assert runtime.next_retry_at == 100.0


  def test_due_retry_starts_new_worker(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    devices = [
      make_hid_entry(config, 3, b"interface-3"),
    ]

    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      lambda vendor_id, product_id: devices,
    )

    manager = make_manager(config)
    runtime = manager._interfaces[3]
    runtime.status = "waiting"
    runtime.next_retry_at = 100.0

    manager._process_due_retries()

    assert isinstance(runtime.worker, FakeWorker)
    assert runtime.worker.worker_id == 1
    assert runtime.worker.started is True
    assert runtime.worker.device_path == b"interface-3"
    assert runtime.status == "waiting"
    assert runtime.next_retry_at is None


  def test_due_retry_does_not_start_worker_without_dongle(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )
    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      lambda vendor_id, product_id: [],
    )

    manager = make_manager(config)
    runtime = manager._interfaces[3]
    runtime.status = "waiting"
    runtime.path = b"old-interface-3"
    runtime.next_retry_at = 100.0

    manager._process_due_retries()

    assert runtime.worker is None
    assert runtime.path is None
    assert runtime.next_retry_at is None
    assert runtime.status == "waiting"
    assert manager._dongle_connected is False


  def test_due_retry_keeps_missing_interface_unavailable(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    devices = [
      make_hid_entry(config, 4, b"interface-4"),
    ]

    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      lambda vendor_id, product_id: devices,
    )

    manager = make_manager(config)
    runtime = manager._interfaces[3]
    runtime.status = "waiting"
    runtime.next_retry_at = 100.0

    manager._process_due_retries()

    assert manager._dongle_connected is True
    assert runtime.worker is None
    assert runtime.path is None
    assert runtime.status == "unavailable"
    assert runtime.next_retry_at == (
      100.0 + manager.RETRY_INTERVAL
    )

  def test_worker_stop_failure_does_not_interrupt_due_retry_without_dongle(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )
    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      lambda vendor_id, product_id: [],
    )

    manager = make_manager(config)
    manager._start_worker(3, b"old-path")

    runtime = manager._interfaces[3]
    old_worker = runtime.worker

    def fail_stop():
      raise RuntimeError("Stop failed")

    monkeypatch.setattr(old_worker, "stop", fail_stop)

    runtime.next_retry_at = 100.0

    manager._process_due_retries()

    assert runtime.worker is old_worker
    assert runtime.worker_closing is True
    assert runtime.path is None
    assert runtime.next_retry_at is None
    assert runtime.status == "waiting"
    assert manager._dongle_connected is False

  def test_due_retry_discovery_failure_preserves_retry_state(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)
    runtime = manager._interfaces[3]

    runtime.status = "unavailable"
    runtime.path = b"interface-3"
    runtime.next_retry_at = 100.0

    previous_state = manager._build_state()

    def fail_discovery():
      raise RuntimeError("Discovery failed")

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      fail_discovery,
    )

    manager._process_due_retries()

    assert manager._build_state() == previous_state
    assert runtime.worker is None
    assert runtime.path == b"interface-3"
    assert runtime.next_retry_at == (
      100.0 + manager.RETRY_INTERVAL
    )
    assert runtime.status == "unavailable"

  def test_interface_recovery_does_not_affect_other_interfaces(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    current_time = [100.0]

    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: current_time[0],
    )

    device_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      device_queue,
    )

    for interface_id in config.interfaces:
      manager._start_worker(
        interface_id,
        f"path-{interface_id}".encode(),
      )

      manager._handle_worker_message(
        connection_manager.WorkerMessage(
          worker_id=manager._interfaces[interface_id].worker.worker_id,
          interface_id=interface_id,
          message_type=connection_manager.WorkerMessageType.READY,
        )
      )

    failed_id = 3
    failed_runtime = manager._interfaces[failed_id]
    old_worker = failed_runtime.worker

    other_workers = {
      interface_id: runtime.worker
      for interface_id, runtime in manager._interfaces.items()
      if interface_id != failed_id
    }

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=old_worker.worker_id,
        interface_id=failed_id,
        message_type=connection_manager.WorkerMessageType.ERROR,
        payload={"error": "read failed"},
      )
    )

    assert old_worker.stopped is True
    assert failed_runtime.worker is old_worker
    assert failed_runtime.worker_closing is True
    assert failed_runtime.status == "restarting"

    for interface_id, worker in other_workers.items():
      runtime = manager._interfaces[interface_id]

      assert runtime.worker is worker
      assert worker.stopped is False
      assert runtime.status == "available"

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=old_worker.worker_id,
        interface_id=failed_id,
        message_type=connection_manager.WorkerMessageType.TERMINATED,
      )
    )

    assert failed_runtime.worker is None
    assert failed_runtime.status == "waiting"
    assert failed_runtime.next_retry_at == (
      100.0 + manager.RETRY_INTERVAL
    )

    devices = [
      make_hid_entry(
        config,
        failed_id,
        b"recovered-interface",
      ),
    ]

    monkeypatch.setattr(
      connection_manager.hid,
      "enumerate",
      lambda vendor_id, product_id: devices,
    )

    current_time[0] = 100.0 + manager.RETRY_INTERVAL
    manager._process_due_retries()

    new_worker = failed_runtime.worker

    assert isinstance(new_worker, FakeWorker)
    assert new_worker is not old_worker
    assert new_worker.started is True
    assert new_worker.device_path == b"recovered-interface"
    assert failed_runtime.status == "waiting"

    for interface_id, worker in other_workers.items():
      runtime = manager._interfaces[interface_id]

      assert runtime.worker is worker
      assert worker.stopped is False
      assert runtime.status == "available"

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=new_worker.worker_id,
        interface_id=failed_id,
        message_type=connection_manager.WorkerMessageType.READY,
      )
    )

    assert failed_runtime.status == "available"

    for interface_id, worker in other_workers.items():
      runtime = manager._interfaces[interface_id]

      assert runtime.worker is worker
      assert runtime.status == "available"

  def test_failed_worker_start_schedules_repeated_retries(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    current_time = [100.0]

    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: current_time[0],
    )

    manager = make_manager(config)
    failed_id = 3
    runtime = manager._interfaces[failed_id]

    runtime.next_retry_at = 100.0

    discovery = make_discovery(
      config,
      {failed_id: b"interface-3"},
    )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: discovery,
    )

    attempts = []

    def fail_start(interface_id, path):
      attempts.append((interface_id, path))
      raise RuntimeError("Simulated worker start failure")

    monkeypatch.setattr(
      manager,
      "_start_worker",
      fail_start,
    )

    # First failed attempt.
    manager._process_due_retries()

    assert attempts == [
      (failed_id, b"interface-3"),
    ]
    assert runtime.worker is None
    assert runtime.status == "waiting"
    assert runtime.next_retry_at == (
      100.0 + manager.RETRY_INTERVAL
    )

    # Second failed attempt.
    current_time[0] += manager.RETRY_INTERVAL

    manager._process_due_retries()

    assert attempts == [
      (failed_id, b"interface-3"),
      (failed_id, b"interface-3"),
    ]
    assert runtime.worker is None
    assert runtime.status == "waiting"
    assert runtime.next_retry_at == (
      100.0 + 2 * manager.RETRY_INTERVAL
    )

  def test_missing_interface_recovers_without_restarting_available_workers(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    current_time = [100.0]

    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: current_time[0],
    )

    manager = make_manager(config)

    missing_id = 3
    available_id = 4

    # Start an interface that must remain available.
    manager._start_worker(
      available_id,
      b"interface-4",
    )

    available_runtime = manager._interfaces[available_id]
    available_worker = available_runtime.worker

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=available_worker.worker_id,
        interface_id=available_id,
        message_type=connection_manager.WorkerMessageType.READY,
      )
    )

    # The missing interface is scheduled for its first retry.
    missing_runtime = manager._interfaces[missing_id]
    missing_runtime.status = "unavailable"
    missing_runtime.next_retry_at = 100.0

    discovery_results = [
      make_discovery(
        config,
        {
          available_id: b"interface-4",
        },
      ),
      make_discovery(
        config,
        {
          missing_id: b"recovered-interface-3",
          available_id: b"interface-4",
        },
      ),
    ]

    discovery_calls = []

    def discover_devices():
      result = discovery_results[len(discovery_calls)]
      discovery_calls.append(result)
      return result

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      discover_devices,
    )

    # First retry: interface 3 is still missing.
    manager._process_due_retries()

    assert missing_runtime.worker is None
    assert missing_runtime.path is None
    assert missing_runtime.status == "unavailable"
    assert missing_runtime.next_retry_at == (
      100.0 + manager.RETRY_INTERVAL
    )

    assert available_runtime.worker is available_worker
    assert available_worker.stopped is False
    assert available_runtime.status == "available"

    # Second retry: interface 3 has returned.
    current_time[0] += manager.RETRY_INTERVAL

    manager._process_due_retries()

    recovered_worker = missing_runtime.worker

    assert isinstance(recovered_worker, FakeWorker)
    assert recovered_worker is not available_worker
    assert recovered_worker.started is True
    assert recovered_worker.device_path == b"recovered-interface-3"
    assert missing_runtime.status == "waiting"
    assert missing_runtime.next_retry_at is None

    # Interface 4 must remain untouched.
    assert available_runtime.worker is available_worker
    assert available_worker.stopped is False
    assert available_runtime.status == "available"

    # The recovered interface becomes available only after READY.
    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=recovered_worker.worker_id,
        interface_id=missing_id,
        message_type=connection_manager.WorkerMessageType.READY,
      )
    )

    assert missing_runtime.status == "available"
    assert available_runtime.worker is available_worker
    assert available_runtime.status == "available"


  def test_stop_during_retry_discovery_prevents_worker_start(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)

    interface_id = 3
    runtime = manager._interfaces[interface_id]

    runtime.status = "unavailable"
    runtime.next_retry_at = 100.0

    def discover_devices():
      # Simulate the public stop() request during discovery.
      manager._stop_requested.set()
      manager._events.put(
        connection_manager.ManagerEvent(
          connection_manager.ManagerEventType.STOP
        )
      )

      return make_discovery(
        config,
        {
          interface_id: b"interface-3",
        },
      )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      discover_devices,
    )

    manager._process_due_retries()

    assert manager._stop_requested.is_set()
    assert runtime.worker is None
    assert runtime.status == "unavailable"
    assert runtime.next_retry_at == 100.0


  def test_stop_during_retry_worker_start_prevents_other_workers(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)

    interface_ids = list(config.interfaces)

    for interface_id in interface_ids:
      runtime = manager._interfaces[interface_id]
      runtime.status = "unavailable"
      runtime.next_retry_at = 100.0

    started_workers = []

    def discover_devices():
      return make_discovery(
        config,
        {
          interface_id: f"path-{interface_id}".encode()
          for interface_id in interface_ids
        },
      )

    original_start_worker = manager._start_worker

    def start_worker(interface_id, device_path):
      original_start_worker(interface_id, device_path)

      worker = manager._interfaces[interface_id].worker
      started_workers.append(worker)

      if len(started_workers) == 1:
        manager._stop_requested.set()
        manager._events.put(
          connection_manager.ManagerEvent(
            connection_manager.ManagerEventType.STOP
          )
        )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      discover_devices,
    )
    monkeypatch.setattr(
      manager,
      "_start_worker",
      start_worker,
    )

    manager._process_due_retries()

    assert len(started_workers) == 1

    worker = started_workers[0]

    assert worker.started is True
    assert worker.stopped is False

    for interface_id in interface_ids[1:]:
      assert manager._interfaces[interface_id].worker is None

  def test_retry_starts_fast_polling_without_restarting_if3(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._dongle_connected = True
    manager._headset_on = "on"
    manager._headset_usb_connected = False

    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    paths = {
      interface_id: f"path-{interface_id}".encode()
      for interface_id in config.interfaces
    }

    for interface_id, path in paths.items():
      manager._start_worker(interface_id, path)

    worker = manager._interfaces[3].worker
    worker_id = worker.worker_id
    manager._interfaces[3].next_retry_at = 100.0

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(
        config,
        paths,
        dongle_connected=True,
        headset_usb_connected=True,
      ),
    )

    manager._process_due_retries()

    assert manager._headset_usb_connected is True
    assert worker.fast_polling_calls == [(0.5, 30.0)]
    assert worker.worker_id == worker_id
    assert worker.started is True
    assert worker.stopped is False
    assert worker.call_order.count("start") == 1

#END TestRetries

class TestDeviceNotifications:
  def test_device_arrival_starts_available_workers(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    paths = {
      interface_id: f"path-{interface_id}".encode()
      for interface_id in config.interfaces
    }

    manager = make_manager(config)
    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(config, paths),
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.ARRIVAL)
    )

    assert manager._dongle_connected is True

    for interface_id, path in paths.items():
      runtime = manager._interfaces[interface_id]

      assert isinstance(runtime.worker, FakeWorker)
      assert runtime.worker.started is True
      assert runtime.path == path
      assert runtime.status == "waiting"


  def test_dongle_disconnect_stops_all_workers(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)

    for interface_id in config.interfaces:
      manager._start_worker(
        interface_id,
        f"path-{interface_id}".encode(),
      )

    workers = {
      interface_id: manager._interfaces[interface_id].worker
      for interface_id in config.interfaces
    }

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(
        config,
        {},
        dongle_connected=False,
        headset_usb_connected=True,
      ),
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.REMOVAL)
    )

    assert manager._dongle_connected is False
    assert manager._headset_usb_connected is True

    for interface_id, worker in workers.items():
      runtime = manager._interfaces[interface_id]

      assert worker.stopped is True
      assert runtime.worker is worker
      assert runtime.worker_closing is True
      assert runtime.path is None
      assert runtime.next_retry_at is None

    assert workers[3].fast_polling_calls == []

  def test_dongle_disconnect_publishes_restarting_until_termination(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)

    for interface_id in config.interfaces:
      manager._start_worker(
        interface_id,
        f"path-{interface_id}".encode(),
      )

      manager._interfaces[interface_id].status = "available"

    workers = {
      interface_id: runtime.worker
      for interface_id, runtime in manager._interfaces.items()
    }

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(
        config,
        {},
        dongle_connected=False,
      ),
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.REMOVAL)
    )

    for interface_id, worker in workers.items():
      runtime = manager._interfaces[interface_id]

      assert worker.stopped is True
      assert runtime.worker is worker
      assert runtime.worker_closing is True
      assert runtime.status == "restarting"
      assert manager._state.interfaces[interface_id].status == "restarting"

    for interface_id, worker in workers.items():
      manager._handle_worker_message(
        connection_manager.WorkerMessage(
          worker_id=worker.worker_id,
          interface_id=interface_id,
          message_type=connection_manager.WorkerMessageType.TERMINATED,
        )
      )

      runtime = manager._interfaces[interface_id]

      assert runtime.worker is None
      assert runtime.worker_closing is False
      assert runtime.status == "waiting"
      assert manager._state.interfaces[interface_id].status == "waiting"

  def test_missing_interface_stops_only_its_worker(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)

    for interface_id in config.interfaces:
      manager._start_worker(
        interface_id,
        f"path-{interface_id}".encode(),
      )

    missing_id = 3
    missing_worker = manager._interfaces[missing_id].worker
    other_workers = {
      interface_id: manager._interfaces[interface_id].worker
      for interface_id in config.interfaces
      if interface_id != missing_id
    }

    paths = {
      interface_id: f"path-{interface_id}".encode()
      for interface_id in config.interfaces
      if interface_id != missing_id
    }

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(config, paths),
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.REMOVAL)
    )

    runtime = manager._interfaces[missing_id]

    assert missing_worker.stopped is True
    assert runtime.worker is missing_worker
    assert runtime.worker_closing is True
    assert runtime.path is None

    for interface_id, worker in other_workers.items():
      runtime = manager._interfaces[interface_id]

      assert worker.stopped is False
      assert runtime.worker is worker
      assert runtime.worker_closing is False


  def test_interface_path_change_stops_old_worker(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"old-path")

    runtime = manager._interfaces[3]
    old_worker = runtime.worker

    paths = {
      3: b"new-path",
    }

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(config, paths),
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.ARRIVAL)
    )

    assert old_worker.stopped is True
    assert runtime.worker is old_worker
    assert runtime.worker_closing is True
    assert runtime.path == b"new-path"


  def test_repeated_arrival_does_not_create_duplicate_workers(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    paths = {
      interface_id: f"path-{interface_id}".encode()
      for interface_id in config.interfaces
    }

    manager = make_manager(config)
    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(config, paths),
    )

    event = make_notification(
      connection_manager.DeviceEventType.ARRIVAL
    )

    manager._handle_device_notification(event)

    workers = {
      interface_id: manager._interfaces[interface_id].worker
      for interface_id in config.interfaces
    }

    manager._handle_device_notification(event)

    for interface_id, worker in workers.items():
      runtime = manager._interfaces[interface_id]

      assert runtime.worker is worker
      assert worker.started is True
      assert worker.stopped is False


  def test_dongle_reconnection_starts_new_workers(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)

    old_workers = {}

    for interface_id in config.interfaces:
      manager._start_worker(
        interface_id,
        f"old-path-{interface_id}".encode(),
      )

      old_workers[interface_id] = (
        manager._interfaces[interface_id].worker
      )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(
        config,
        {},
        dongle_connected=False,
      ),
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.REMOVAL)
    )

    for interface_id, old_worker in old_workers.items():
      runtime = manager._interfaces[interface_id]

      assert runtime.worker is old_worker
      assert runtime.worker_closing is True
      assert old_worker.stopped is True

    for interface_id, old_worker in old_workers.items():
      manager._handle_worker_message(
        connection_manager.WorkerMessage(
          worker_id=old_worker.worker_id,
          interface_id=interface_id,
          message_type=connection_manager.WorkerMessageType.TERMINATED,
        )
      )

      runtime = manager._interfaces[interface_id]

      assert old_worker.joined is True
      assert runtime.worker is None
      assert runtime.status == "waiting"

    new_paths = {
      interface_id: f"new-path-{interface_id}".encode()
      for interface_id in config.interfaces
    }

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(config, new_paths),
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.ARRIVAL)
    )

    for interface_id, old_worker in old_workers.items():
      runtime = manager._interfaces[interface_id]
      new_worker = runtime.worker

      assert isinstance(new_worker, FakeWorker)
      assert new_worker is not old_worker
      assert new_worker.worker_id > old_worker.worker_id
      assert new_worker.started is True
      assert new_worker.device_path == new_paths[interface_id]
      assert old_worker.joined is True
      assert runtime.worker_closing is False
      assert runtime.next_retry_at is None
      assert runtime.status == "waiting"
      assert manager._state.interfaces[interface_id].status == "waiting"

      manager._handle_worker_message(
        connection_manager.WorkerMessage(
          worker_id=new_worker.worker_id,
          interface_id=interface_id,
          message_type=connection_manager.WorkerMessageType.READY,
        )
      )

      assert runtime.status == "available"
      assert manager._state.interfaces[interface_id].status == "available"

  def test_interface_path_change_restarts_only_after_termination(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"old-path")

    runtime = manager._interfaces[3]
    old_worker = runtime.worker

    paths = {
      3: b"new-path",
    }

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(config, paths),
    )

    event = make_notification(
      connection_manager.DeviceEventType.ARRIVAL
    )

    manager._handle_device_notification(event)

    assert old_worker.stopped is True
    assert old_worker.joined is False
    assert runtime.worker is old_worker
    assert runtime.worker_closing is True
    assert runtime.path == b"new-path"

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=old_worker.worker_id,
        interface_id=3,
        message_type=connection_manager.WorkerMessageType.TERMINATED,
      )
    )

    assert old_worker.joined is True
    assert runtime.worker is None
    assert runtime.status == "waiting"

    manager._handle_device_notification(event)

    new_worker = runtime.worker

    assert isinstance(new_worker, FakeWorker)
    assert new_worker is not old_worker
    assert new_worker.started is True
    assert new_worker.device_path == b"new-path"
    assert old_worker.joined is True
    assert runtime.worker_closing is False

  def test_worker_stop_failure_does_not_interrupt_dongle_disconnect(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)

    for interface_id in config.interfaces:
      manager._start_worker(
        interface_id,
        f"interface-{interface_id}".encode(),
      )

    workers = {
      interface_id: runtime.worker
      for interface_id, runtime in manager._interfaces.items()
    }

    failing_worker = workers[3]

    def fail_stop():
      raise RuntimeError("Simulated stop failure")

    failing_worker.stop = fail_stop

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(
        config,
        {},
        dongle_connected=False,
      ),
    )

    manager._handle_device_notification(
      connection_manager.DeviceNotification(
        event_type=connection_manager.DeviceEventType.REMOVAL,
      )
    )

    assert manager._dongle_connected is False

    assert manager._interfaces[3].worker is failing_worker
    assert manager._interfaces[3].worker_closing is True

    for interface_id, worker in workers.items():
      if interface_id == 3:
        continue

      assert worker.stopped is True
      assert manager._interfaces[interface_id].worker_closing is True


  def test_worker_stop_failure_does_not_interrupt_interface_path_change(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"old-path")

    runtime = manager._interfaces[3]
    old_worker = runtime.worker

    def fail_stop():
      raise RuntimeError("Stop failed")

    monkeypatch.setattr(old_worker, "stop", fail_stop)

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(config, {3: b"new-path"}),
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.ARRIVAL)
    )

    assert runtime.worker is old_worker
    assert runtime.worker_closing is True
    assert runtime.path == b"new-path"
    assert runtime.status == "waiting"

  def test_device_notification_discovery_failure_preserves_current_state(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._dongle_connected = True
    manager._headset_usb_connected = True
    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker

    previous_state = manager._build_state()

    def fail_discovery():
      raise RuntimeError("Discovery failed")

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      fail_discovery,
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.ARRIVAL)
    )

    assert manager._build_state() == previous_state
    assert runtime.worker is worker
    assert runtime.worker.stopped is False
    assert runtime.worker_closing is False
    assert runtime.path == b"interface-3"

  def test_notification_discovery_failure_schedules_retry(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)

    runtimes = manager._interfaces.values()

    for runtime in runtimes:
      runtime.status = "waiting"

    def fail_discovery():
      raise RuntimeError("Discovery failed")

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      fail_discovery,
    )

    previous_state = manager._build_state()

    manager._handle_device_notification(
      connection_manager.DeviceNotification(
        event_type=connection_manager.DeviceEventType.ARRIVAL,
      )
    )

    assert manager._build_state() == previous_state

    for runtime in runtimes:
      assert runtime.next_retry_at == (
        100.0 + manager.RETRY_INTERVAL
      )

  def test_missing_interface_schedules_retry_on_notification(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)

    available_interface = next(iter(config.interfaces))
    paths = {
      available_interface: b"available-interface",
    }

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(config, paths),
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.ARRIVAL)
    )

    for interface_id, runtime in manager._interfaces.items():
      if interface_id == available_interface:
        assert runtime.next_retry_at is None
      else:
        assert runtime.status == "unavailable"
        assert runtime.next_retry_at == (
          100.0 + manager.RETRY_INTERVAL
        )

  def test_usb_connection_change_starts_fast_polling_without_restarting_worker(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._headset_usb_connected = False
    manager._dongle_connected = True
    manager._headset_on = "on"

    paths = {
      interface_id: f"path-{interface_id}".encode()
      for interface_id in config.interfaces
    }

    for interface_id, path in paths.items():
      manager._start_worker(interface_id, path)

    worker = manager._interfaces[3].worker
    worker_id = worker.worker_id

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: make_discovery(
        config,
        paths,
        dongle_connected=True,
        headset_usb_connected=True,
      ),
    )

    manager._handle_device_notification(
      make_notification(connection_manager.DeviceEventType.ARRIVAL)
    )

    assert manager._headset_usb_connected is True
    assert worker.fast_polling_calls == [(0.5, 30.0)]
    assert worker.worker_id == worker_id
    assert worker.started is True
    assert worker.stopped is False
    assert worker.call_order.count("start") == 1

#END TestDeviceNotifications

class TestStatePublication:
  def test_state_callback_failure_does_not_interrupt_state_publication(
    self,
    config,
    monkeypatch,
  ):
    manager = make_manager(config)

    def fail_callback(state):
      raise RuntimeError("State callback failed")

    manager._state_callback = fail_callback

    manager._dongle_connected = True

    manager._publish_state()

    assert manager._state.dongle_connected is True


#END TestStatePublication

class TestShutdown:
  def test_stop_stops_and_joins_all_workers(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)

    workers = {}

    for interface_id in config.interfaces:
      manager._start_worker(
        interface_id,
        f"path-{interface_id}".encode(),
      )
      workers[interface_id] = manager._interfaces[interface_id].worker

    manager._started = True
    manager._stop_requested = threading.Event()

    def fake_coordinator():
      manager._status = "stopping"

      for runtime in manager._interfaces.values():
        worker = runtime.worker

        if worker is None:
          continue

        worker.stop()
        worker.join()

        runtime.worker = None
        runtime.worker_closing = False
        runtime.next_retry_at = None
        runtime.path = None
        runtime.status = "waiting"

      manager._status = "stopped"

    class FakeThread:
      def join(self):
        fake_coordinator()

    manager._thread = FakeThread()

    manager.stop()

    for interface_id, worker in workers.items():
      runtime = manager._interfaces[interface_id]

      assert worker.stopped is True
      assert worker.joined is True
      assert runtime.worker is None
      assert runtime.worker_closing is False
      assert runtime.status == "waiting"
      assert runtime.path is None

    assert manager._status == "stopped"


  def test_stop_without_workers(self, config):
    manager = make_manager(config)

    manager._started = True
    manager._thread = None
    manager._stop_requested = threading.Event()

    manager.stop()

    assert manager._status == "stopped"

    for runtime in manager._interfaces.values():
      assert runtime.worker is None
      assert runtime.status == "waiting"


  def test_stop_is_idempotent(self, config):
    manager = make_manager(config)

    manager._started = True
    manager._thread = None
    manager._stop_requested = threading.Event()

    manager.stop()
    manager.stop()

    assert manager._status == "stopped"

  def test_real_coordinator_stops_all_workers(self, config, monkeypatch):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )
    monkeypatch.setattr(
      connection_manager,
      "hid",
      type(
        "FakeHID",
        (),
        {"enumerate": staticmethod(lambda: [])},
      ),
    )

    manager = make_manager(config)

    workers = {}

    for interface_id in config.interfaces:
      manager._start_worker(
        interface_id,
        f"path-{interface_id}".encode(),
      )
      workers[interface_id] = manager._interfaces[interface_id].worker

    manager.start()

    assert manager._thread is not None
    assert manager._thread.is_alive()

    manager.stop()

    assert not manager._thread.is_alive()
    assert manager._status == "stopped"

    for interface_id, worker in workers.items():
      runtime = manager._interfaces[interface_id]

      assert worker.stopped is True
      assert worker.joined is True
      assert runtime.worker is None
      assert runtime.worker_closing is False
      assert runtime.status == "waiting"
      assert runtime.path is None

    assert manager._notifications is None


  def test_shutdown_does_not_stop_closing_worker_twice(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker

    manager._handle_worker_message(
      connection_manager.WorkerMessage(
        worker_id=worker.worker_id,
        interface_id=3,
        message_type=connection_manager.WorkerMessageType.ERROR,
        payload={"error": "simulated error"},
      )
    )

    assert runtime.worker_closing is True
    assert worker.stopped is True

    stop_calls = 1
    original_stop = worker.stop

    def count_stop():
      nonlocal stop_calls
      stop_calls += 1
      original_stop()

    worker.stop = count_stop

    class FakeNotifications:
      def stop(self):
        manager._notifications = None

    manager._notifications = FakeNotifications()
    manager._started = True

    class FakeThread:
      def join(self):
        manager._run()

    manager._thread = FakeThread()
    manager._events.put(
      connection_manager.ManagerEvent(
        event_type=connection_manager.ManagerEventType.STOP,
      )
    )

    manager.stop()

    assert stop_calls == 1
    assert worker.joined is True
    assert runtime.worker is None
    assert manager._status == "stopped"


  def test_shutdown_continues_when_notifications_stop_fails(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )

    class FailingNotifications:
      def __init__(self, events, **kwargs):
        self.started = False
        self.stopped = False

      def start(self):
        self.started = True

      def stop(self):
        self.stopped = True
        raise RuntimeError("simulated notification shutdown failure")

    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FailingNotifications,
    )

    monkeypatch.setattr(
      connection_manager,
      "hid",
      type(
        "FakeHID",
        (),
        {"enumerate": staticmethod(lambda *args, **kwargs: [])},
      ),
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    worker = manager._interfaces[3].worker

    manager.start()
    notifications = manager._notifications

    assert notifications is not None
    assert notifications.started is True

    manager.stop()

    runtime = manager._interfaces[3]

    assert notifications.stopped is True
    assert worker.stopped is True
    assert worker.joined is True
    assert runtime.worker is None
    assert runtime.status == "waiting"
    assert manager._notifications is notifications
    assert manager._status == "stopped"
    assert not manager._thread.is_alive()


  def test_immediate_stop_skips_initialization(self, config, monkeypatch):
    notifications_started = threading.Event()
    run_entered = threading.Event()
    allow_run = threading.Event()
    discovery_called = threading.Event()

    class TrackingNotifications:
      def __init__(self, events, **kwargs):
        pass

      def start(self):
        notifications_started.set()

      def stop(self):
        pass

    manager = make_manager(config)

    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      TrackingNotifications,
    )

    original_run = manager._run

    def gated_run():
      run_entered.set()

      if not allow_run.wait(timeout=2):
        raise TimeoutError("Test synchronization timed out")

      original_run()

    monkeypatch.setattr(manager, "_run", gated_run)

    def discover():
      discovery_called.set()
      return make_discovery(config, {})

    monkeypatch.setattr(manager, "_discover_devices", discover)

    manager.start()

    assert run_entered.wait(timeout=2)

    stop_thread = threading.Thread(target=manager.stop)
    stop_thread.start()

    assert manager._stop_requested.wait(timeout=2)

    allow_run.set()

    stop_thread.join(timeout=2)

    assert not stop_thread.is_alive()
    assert not notifications_started.is_set()
    assert not discovery_called.is_set()
    assert manager._status == "stopped"
    assert not manager._thread.is_alive()


  def test_stop_during_notification_initialization(
    self,
    config,
    monkeypatch,
  ):
    notifications_entered = threading.Event()
    allow_notifications_start = threading.Event()
    discovery_called = threading.Event()

    class BlockingNotifications:
      def __init__(self, events, **kwargs):
        self.stopped = False

      def start(self):
        notifications_entered.set()

        if not allow_notifications_start.wait(timeout=2):
          raise TimeoutError("Test synchronization timed out")

      def stop(self):
        self.stopped = True

    manager = make_manager(config)

    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      BlockingNotifications,
    )

    def discover():
      discovery_called.set()
      return make_discovery(config, {})

    monkeypatch.setattr(manager, "_discover_devices", discover)

    manager.start()

    assert notifications_entered.wait(timeout=2)

    stop_thread = threading.Thread(target=manager.stop)
    stop_thread.start()

    assert manager._stop_requested.wait(timeout=2)

    allow_notifications_start.set()

    stop_thread.join(timeout=2)

    assert not stop_thread.is_alive()
    assert not discovery_called.is_set()
    assert manager._status == "stopped"
    assert manager._notifications is None


  def test_stop_during_device_discovery(
    self,
    config,
    monkeypatch,
  ):
    discovery_entered = threading.Event()
    allow_discovery = threading.Event()
    worker_starts = []

    class TrackingNotifications:
      def __init__(self, events, **kwargs):
        self.stopped = False

      def start(self):
        pass

      def stop(self):
        self.stopped = True

    manager = make_manager(config)

    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      TrackingNotifications,
    )

    def discover():
      discovery_entered.set()

      if not allow_discovery.wait(timeout=2):
        raise TimeoutError("Test synchronization timed out")

      return make_discovery(
        config,
        {
          interface_id: f"path-{interface_id}".encode()
          for interface_id in config.interfaces
        },
      )

    def track_worker_start(interface_id, device_path):
      worker_starts.append((interface_id, device_path))

    monkeypatch.setattr(manager, "_discover_devices", discover)
    monkeypatch.setattr(manager, "_start_worker", track_worker_start)

    manager.start()

    assert discovery_entered.wait(timeout=2)

    stop_thread = threading.Thread(target=manager.stop)
    stop_thread.start()

    assert manager._stop_requested.wait(timeout=2)

    allow_discovery.set()

    stop_thread.join(timeout=2)

    assert not stop_thread.is_alive()
    assert manager._status == "stopped"
    assert worker_starts == []
    assert manager._worker_id_counter == 0

    for runtime in manager._interfaces.values():
      assert runtime.worker is None

    assert manager._notifications is None


  def test_shutdown_reports_failure_when_worker_remains_alive(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )
    monkeypatch.setattr(
      connection_manager,
      "hid",
      type(
        "FakeHID",
        (),
        {"enumerate": staticmethod(lambda *args, **kwargs: [])},
      ),
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker
    join_error = RuntimeError("simulated join failure")
    worker.join_error = join_error

    manager.start()

    with pytest.raises(RuntimeError, match="simulated join failure"):
      manager.stop()

    assert worker.stopped is True
    assert worker.joined is True
    assert worker.is_alive() is True

    assert runtime.worker is worker
    assert runtime.worker_closing is True
    assert runtime.path == b"interface-3"

    assert manager._status == "error"
    assert not manager._thread.is_alive()


  def test_shutdown_cleans_up_dead_worker_when_join_fails(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )
    monkeypatch.setattr(
      connection_manager,
      "hid",
      type(
        "FakeHID",
        (),
        {"enumerate": staticmethod(lambda *args, **kwargs: [])},
      ),
    )

    manager = make_manager(config)
    manager._start_worker(3, b"interface-3")

    runtime = manager._interfaces[3]
    worker = runtime.worker

    worker.alive = False
    worker.join_error = RuntimeError("simulated join failure")

    manager.start()
    manager.stop()

    assert worker.stopped is True
    assert worker.joined is True
    assert worker.is_alive() is False

    assert runtime.worker is None
    assert runtime.worker_closing is False
    assert runtime.next_retry_at is None
    assert runtime.path is None
    assert runtime.status == "waiting"

    assert manager._status == "stopped"
    assert not manager._thread.is_alive()

  def test_stop_waits_until_thread_start_completes(
    self,
    config,
    monkeypatch,
  ):
    real_thread = threading.Thread

    thread_start_entered = threading.Event()
    allow_thread_start = threading.Event()
    stop_entered = threading.Event()
    stop_completed = threading.Event()
    errors = []

    class BlockingThread:
      def __init__(self, target, name, daemon):
        self._thread = real_thread(
          target=target,
          name=name,
          daemon=daemon,
        )

      def start(self):
        thread_start_entered.set()

        if not allow_thread_start.wait(timeout=2):
          raise TimeoutError("Test synchronization timed out")

        self._thread.start()

      def join(self):
        self._thread.join()

      def is_alive(self):
        return self._thread.is_alive()

    monkeypatch.setattr(
      connection_manager.threading,
      "Thread",
      BlockingThread,
    )

    manager = make_manager(config)

    def start_manager():
      try:
        manager.start()
      except Exception as error:
        errors.append(error)

    def stop_manager():
      stop_entered.set()

      try:
        manager.stop()
      except Exception as error:
        errors.append(error)
      finally:
        stop_completed.set()

    start_thread = real_thread(target=start_manager)
    start_thread.start()

    assert thread_start_entered.wait(timeout=2)

    stop_thread = real_thread(target=stop_manager)
    stop_thread.start()

    assert stop_entered.wait(timeout=2)

    assert not stop_completed.wait(timeout=0.05)

    allow_thread_start.set()

    start_thread.join(timeout=2)
    stop_thread.join(timeout=2)

    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert errors == []

    assert manager._started is True
    assert manager._stop_requested.is_set()
    assert manager._status == "stopped"
    assert not manager._thread.is_alive()

  def test_concurrent_start_only_starts_once(
    self,
    config,
    monkeypatch,
  ):
    run_entered = threading.Event()
    allow_run = threading.Event()

    manager = make_manager(config)

    def blocked_run():
      run_entered.set()

      if not allow_run.wait(timeout=2):
        raise TimeoutError("Test synchronization timed out")

    monkeypatch.setattr(manager, "_run", blocked_run)

    start_results = []
    start_barrier = threading.Barrier(3)

    def start_manager():
      start_barrier.wait()

      try:
        manager.start()
        start_results.append("started")
      except RuntimeError:
        start_results.append("rejected")

    threads = [
      threading.Thread(target=start_manager)
      for _ in range(2)
    ]

    for thread in threads:
      thread.start()

    start_barrier.wait()

    for thread in threads:
      thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(start_results) == ["rejected", "started"]
    assert run_entered.wait(timeout=2)

    allow_run.set()
    manager.stop()

    assert manager._status == "stopped"
    assert not manager._thread.is_alive()

  def test_worker_stop_failure_does_not_interrupt_shutdown(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HIDWorker",
      FakeWorker,
    )
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )

    manager = make_manager(config)

    workers = {}

    for interface_id in config.interfaces:
      manager._start_worker(
        interface_id,
        f"path-{interface_id}".encode(),
      )
      workers[interface_id] = manager._interfaces[interface_id].worker

    failed_id = next(iter(config.interfaces))

    def fail_stop():
      raise OSError("simulated worker stop failure")

    workers[failed_id].stop = fail_stop

    manager.start()
    manager.stop()

    assert manager._status == "stopped"
    assert not manager._thread.is_alive()

    for interface_id, worker in workers.items():
      assert worker.joined is True

      if interface_id == failed_id:
        assert worker.stopped is False
      else:
        assert worker.stopped is True

    for runtime in manager._interfaces.values():
      assert runtime.worker is None
      assert runtime.worker_closing is False
      assert runtime.status == "waiting"

#END TestShutdown

# ============================================================
# TestDeviceStatusPublication
# ============================================================
class TestDeviceStatusPublication:
  def test_initial_status_is_published_even_when_disconnected(
    self,
    config,
  ):
    message_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      message_queue,
    )

    manager._publish_device_status()

    assert message_queue.get_nowait() == {
      "status": {
        "dongle_connected": False,
        "headset_usb_connected": False,
      },
    }

  def test_unchanged_status_is_not_published_twice(
    self,
    config,
  ):
    message_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      message_queue,
    )

    manager._publish_device_status()
    manager._publish_device_status()

    assert message_queue.qsize() == 1

  def test_changed_status_is_published(
    self,
    config,
  ):
    message_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      message_queue,
    )

    manager._publish_device_status()

    manager._dongle_connected = True
    manager._publish_device_status()

    manager._headset_usb_connected = True
    manager._publish_device_status()

    assert message_queue.get_nowait() == {
      "status": {
        "dongle_connected": False,
        "headset_usb_connected": False,
      },
    }

    assert message_queue.get_nowait() == {
      "status": {
        "dongle_connected": True,
        "headset_usb_connected": False,
      },
    }

    assert message_queue.get_nowait() == {
      "status": {
        "dongle_connected": True,
        "headset_usb_connected": True,
      },
    }

    assert message_queue.empty()

# END TestDeviceStatusPublication

# ============================================================
# TestDeviceStatusIntegration
# ============================================================
class TestDeviceStatusIntegration:
  def test_initial_discovery_publishes_device_status(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "HidDeviceNotifications",
      FakeNotifications,
    )

    message_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      message_queue,
    )

    discovery = connection_manager.DiscoveryResult(
      dongle_connected=True,
      headset_usb_connected=True,
      interface_paths={},
      unavailable_interfaces=frozenset(),
      ambiguous_interfaces=frozenset(),
    )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: discovery,
    )

    monkeypatch.setattr(
      manager._events,
      "get",
      lambda timeout=None: connection_manager.ManagerEvent(
        event_type=connection_manager.ManagerEventType.STOP,
      ),
    )

    manager._run()

    assert message_queue.get_nowait() == {
      "status": {
        "dongle_connected": True,
        "headset_usb_connected": True,
      },
    }

    assert message_queue.empty()
    assert manager._status == "stopped"
    assert manager._notifications is None

  def test_device_notification_publishes_changed_status(
    self,
    config,
    monkeypatch,
  ):
    message_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      message_queue,
    )

    discoveries = iter([
      connection_manager.DiscoveryResult(
        dongle_connected=True,
        headset_usb_connected=False,
        interface_paths={},
        unavailable_interfaces=frozenset(),
        ambiguous_interfaces=frozenset(),
      ),
      connection_manager.DiscoveryResult(
        dongle_connected=False,
        headset_usb_connected=True,
        interface_paths={},
        unavailable_interfaces=frozenset(),
        ambiguous_interfaces=frozenset(),
      ),
    ])

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: next(discoveries),
    )

    event = connection_manager.DeviceNotification(
      event_type=connection_manager.DeviceEventType.ARRIVAL
    )

    manager._handle_device_notification(event)
    manager._handle_device_notification(event)

    assert message_queue.get_nowait() == {
      "status": {
        "dongle_connected": True,
        "headset_usb_connected": False,
      },
    }

    assert message_queue.get_nowait() == {
      "status": {
        "dongle_connected": False,
        "headset_usb_connected": True,
      },
    }

    assert message_queue.empty()

  def test_unchanged_device_notification_does_not_publish_status(
    self,
    config,
    monkeypatch,
  ):
    message_queue = queue.Queue()
    manager = connection_manager.ConnectionManager(
      config,
      message_queue,
    )

    discovery = connection_manager.DiscoveryResult(
      dongle_connected=True,
      headset_usb_connected=False,
      interface_paths={},
      unavailable_interfaces=frozenset(),
      ambiguous_interfaces=frozenset(),
    )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: discovery,
    )

    event = connection_manager.DeviceNotification(
      event_type=connection_manager.DeviceEventType.ARRIVAL
    )

    manager._handle_device_notification(event)
    manager._handle_device_notification(event)

    assert message_queue.qsize() == 1

  def test_retry_publishes_status_when_dongle_is_disconnected(
    self,
    config,
    monkeypatch,
  ):
    manager = make_manager(config)
    message_queue = manager._device_message_queue

    runtime = manager._interfaces[3]
    runtime.next_retry_at = 100.0

    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    discovery = connection_manager.DiscoveryResult(
      dongle_connected=False,
      headset_usb_connected=True,
      interface_paths={},
      unavailable_interfaces=frozenset(),
      ambiguous_interfaces=frozenset(),
    )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: discovery,
    )

    manager._process_due_retries()

    assert message_queue.get_nowait() == {
      "status": {
        "dongle_connected": False,
        "headset_usb_connected": True,
      },
    }

    assert message_queue.empty()

  def test_retry_publishes_status_when_dongle_appears(
    self,
    config,
    monkeypatch,
  ):
    manager = make_manager(config)
    message_queue = manager._device_message_queue

    runtime = manager._interfaces[3]
    runtime.next_retry_at = 100.0

    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    discovery = connection_manager.DiscoveryResult(
      dongle_connected=True,
      headset_usb_connected=False,
      interface_paths={},
      unavailable_interfaces=frozenset(),
      ambiguous_interfaces=frozenset(),
    )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: discovery,
    )

    manager._process_due_retries()

    assert message_queue.get_nowait() == {
      "status": {
        "dongle_connected": True,
        "headset_usb_connected": False,
      },
    }

    assert message_queue.empty()

  def test_notification_discovery_failure_does_not_publish_status(
    self,
    config,
    monkeypatch,
  ):
    manager = make_manager(config)

    def fail_discovery():
      raise OSError("Simulated discovery failure")

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      fail_discovery,
    )

    event = connection_manager.DeviceNotification(
      event_type=connection_manager.DeviceEventType.ARRIVAL,
    )

    manager._handle_device_notification(event)

    assert manager._device_message_queue.empty()

  def test_retry_discovery_failure_does_not_publish_status(
    self,
    config,
    monkeypatch,
  ):
    manager = make_manager(config)
    runtime = manager._interfaces[3]
    runtime.next_retry_at = 100.0

    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    def fail_discovery():
      raise OSError("Simulated discovery failure")

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      fail_discovery,
    )

    manager._process_due_retries()

    assert manager._device_message_queue.empty()
    assert runtime.next_retry_at == 100.0 + manager.RETRY_INTERVAL

# END TestDeviceStatusIntegration

class TestConnectionManagerPnP:
  def test_start_pnp_worker_uses_first_available_hid_interface(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "PnPWorker",
      FakePnPWorker,
    )

    paths = {
      5: b"interface-5",
      4: b"interface-4",
      3: b"interface-3",
    }

    manager = make_manager(config)
    discovery = make_discovery(config, paths)

    manager._start_pnp_worker(discovery)

    runtime = manager._pnp
    worker = runtime.worker

    assert isinstance(worker, FakePnPWorker)
    assert worker.started is True
    assert worker.hid_path == b"interface-5"
    assert worker.session_id == runtime.session_id
    assert worker.message_queue is manager._events

    assert runtime.hid_path == b"interface-5"
    assert runtime.status == "resolving"
    assert runtime.next_retry_at is None
    assert runtime.resolved is False

  def test_start_pnp_worker_does_not_create_duplicates(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "PnPWorker",
      FakePnPWorker,
    )

    manager = make_manager(config)
    discovery = make_discovery(
      config,
      {3: b"interface-3"},
    )

    manager._start_pnp_worker(discovery)

    worker = manager._pnp.worker
    session_id = manager._pnp.session_id

    manager._start_pnp_worker(discovery)

    assert manager._pnp.worker is worker
    assert manager._pnp.session_id == session_id
    assert worker.started is True

  def test_start_pnp_worker_without_available_interfaces_schedules_retry(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)
    discovery = make_discovery(config, {})

    manager._start_pnp_worker(discovery)

    assert manager._pnp.worker is None
    assert manager._pnp.status == "unavailable"
    assert manager._pnp.hid_path is None
    assert manager._pnp.next_retry_at == (
      100.0 + manager.RETRY_INTERVAL
    )

  def test_pnp_result_is_stored_and_published(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "PnPWorker",
      FakePnPWorker,
    )

    manager = make_manager(config)
    discovery = make_discovery(
      config,
      {3: b"interface-3"},
    )

    manager._dongle_connected = True
    manager._start_pnp_worker(discovery)

    worker = manager._pnp.worker

    payload = {
      "status": {
        "pnp_device": {
          "container_id": "{TEST-CONTAINER}",
          "device_instance_id": "USB\\VID_1038&PID_220E",
          "audio_render_instance_id": "render-instance",
          "audio_capture_instance_id": "capture-instance",
        },
      },
    }

    manager._handle_pnp_worker_message(
      connection_manager.PnPWorkerMessage(
        worker_id=worker.worker_id,
        session_id=worker.session_id,
        message_type=connection_manager.PnPWorkerMessageType.RESULT,
        payload=payload,
      )
    )

    assert manager._pnp.resolved is True
    assert manager._pnp.status == "available"
    assert manager._device_message_queue.get_nowait() == payload
    assert manager._device_message_queue.empty()

  def test_pnp_worker_ignores_result_from_old_session(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "PnPWorker",
      FakePnPWorker,
    )

    manager = make_manager(config)
    discovery = make_discovery(
      config,
      {3: b"interface-3"},
    )

    manager._start_pnp_worker(discovery)

    worker = manager._pnp.worker
    manager._pnp.session_id += 1

    payload = {
      "status": {
        "pnp_device": {
          "container_id": "{OLD-CONTAINER}",
        },
      },
    }

    manager._handle_pnp_worker_message(
      connection_manager.PnPWorkerMessage(
        worker_id=worker.worker_id,
        session_id=worker.session_id,
        message_type=connection_manager.PnPWorkerMessageType.RESULT,
        payload=payload,
      )
    )

    assert manager._pnp.resolved is False
    assert manager._pnp.status == "resolving"
    assert manager._device_message_queue.empty()

  def test_pnp_worker_error_schedules_retry_after_termination(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "PnPWorker",
      FakePnPWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)
    manager._dongle_connected = True

    manager._start_pnp_worker(
      make_discovery(config, {3: b"interface-3"})
    )

    worker = manager._pnp.worker

    manager._handle_pnp_worker_message(
      connection_manager.PnPWorkerMessage(
        worker_id=worker.worker_id,
        session_id=worker.session_id,
        message_type=connection_manager.PnPWorkerMessageType.ERROR,
        payload="Simulated PnP failure",
      )
    )

    assert manager._pnp.status == "unavailable"
    assert manager._pnp.worker is worker

    manager._handle_pnp_worker_message(
      connection_manager.PnPWorkerMessage(
        worker_id=worker.worker_id,
        session_id=worker.session_id,
        message_type=connection_manager.PnPWorkerMessageType.TERMINATED,
      )
    )

    assert worker.joined is True
    assert manager._pnp.worker is None
    assert manager._pnp.status == "unavailable"
    assert manager._pnp.next_retry_at == (
      100.0 + manager.RETRY_INTERVAL
    )

  def test_pnp_disconnect_invalidates_session_and_stops_worker(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "PnPWorker",
      FakePnPWorker,
    )

    manager = make_manager(config)
    manager._dongle_connected = True

    manager._start_pnp_worker(
      make_discovery(config, {3: b"interface-3"})
    )

    runtime = manager._pnp
    worker = runtime.worker
    old_session_id = runtime.session_id

    manager._dongle_connected = False
    manager._stop_pnp_worker()

    assert worker.stopped is True
    assert runtime.worker is worker
    assert runtime.worker_closing is True
    assert runtime.session_id > old_session_id
    assert runtime.resolved is False
    assert runtime.hid_path is None
    assert runtime.next_retry_at is None
    assert runtime.status == "waiting"

    manager._handle_pnp_worker_message(
      connection_manager.PnPWorkerMessage(
        worker_id=worker.worker_id,
        session_id=old_session_id,
        message_type=connection_manager.PnPWorkerMessageType.RESULT,
        payload={
          "pnp_devices": {
            "arctis_7_plus": {
              "container_id": "{STALE-CONTAINER}",
            },
          },
        },
      )
    )

    assert runtime.resolved is False
    assert manager._device_message_queue.empty()

    manager._handle_pnp_worker_message(
      connection_manager.PnPWorkerMessage(
        worker_id=worker.worker_id,
        session_id=old_session_id,
        message_type=connection_manager.PnPWorkerMessageType.TERMINATED,
      )
    )

    assert worker.joined is True
    assert runtime.worker is None
    assert runtime.worker_closing is False
    assert runtime.status == "waiting"

  def test_pnp_retry_starts_worker_again(
    self,
    config,
    monkeypatch,
  ):
    monkeypatch.setattr(
      connection_manager,
      "PnPWorker",
      FakePnPWorker,
    )
    monkeypatch.setattr(
      connection_manager.time,
      "monotonic",
      lambda: 100.0,
    )

    manager = make_manager(config)
    manager._dongle_connected = True
    manager._pnp.next_retry_at = 100.0

    discovery = make_discovery(
      config,
      {5: b"interface-5"},
    )

    monkeypatch.setattr(
      manager,
      "_discover_devices",
      lambda: discovery,
    )

    manager._process_due_retries()

    runtime = manager._pnp
    worker = runtime.worker

    assert isinstance(worker, FakePnPWorker)
    assert worker.started is True
    assert worker.hid_path == b"interface-5"
    assert runtime.status == "resolving"
    assert runtime.next_retry_at is None

# END TestConnectionManagerPnP