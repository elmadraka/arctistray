import time
import threading

import pytest

import hid_worker
from device_profile import load_device_profile


@pytest.fixture
def config():
  return load_device_profile("arctis_7_plus.yml")


@pytest.fixture
def worker(config):
  return hid_worker.HIDWorker(
    worker_id=1,
    interface_config=config.interfaces[3],
    device_path=b"interface-3",
    message_queue=hid_worker.Queue(),
  )


class TestAdaptivePolling:
  def test_normal_polling_interval(self, worker):
    assert worker._get_polling_interval() == 300

  def test_start_fast_polling(self, worker, monkeypatch):
    monkeypatch.setattr(
      hid_worker.time,
      "monotonic",
      lambda: 100.0,
    )

    worker.start_fast_polling(
      interval=0.5,
      duration=30,
    )

    assert worker._fast_polling_interval == 0.5
    assert worker._fast_polling_until == 130.0
    assert worker._get_polling_interval() == 0.5

  def test_stop_fast_polling(self, worker, monkeypatch):
    monkeypatch.setattr(
      hid_worker.time,
      "monotonic",
      lambda: 100.0,
    )

    worker.start_fast_polling(
      interval=0.5,
      duration=30,
    )

    worker.stop_fast_polling()

    assert worker._fast_polling_interval is None
    assert worker._fast_polling_until == 0.0
    assert worker._get_polling_interval() == 300

  def test_stop_fast_polling_wakes_polling_condition(
    self,
    worker,
  ):
    waiting = threading.Event()
    awakened = threading.Event()

    worker.start_fast_polling(
      interval=0.5,
      duration=30,
    )

    def wait_for_change():
      with worker._polling_condition:
        waiting.set()
        worker._polling_condition.wait(timeout=2)
        awakened.set()

    thread = threading.Thread(target=wait_for_change)
    thread.start()

    try:
      assert waiting.wait(timeout=1)

      worker.stop_fast_polling()

      assert awakened.wait(timeout=1)
    finally:
      worker.stop()
      thread.join(timeout=1)

    assert not thread.is_alive()

  def test_fast_polling_expires_after_duration(
    self,
    worker,
    monkeypatch,
  ):
    current_time = [100.0]

    monkeypatch.setattr(
      hid_worker.time,
      "monotonic",
      lambda: current_time[0],
    )

    worker.start_fast_polling(
      interval=0.5,
      duration=30,
    )

    current_time[0] = 129.9

    assert worker._get_polling_interval() == 0.5

    current_time[0] = 130.0

    assert worker._get_polling_interval() == 300
    assert worker._fast_polling_interval is None
    assert worker._fast_polling_until is None

  def test_repeated_command_restarts_fast_polling(
    self,
    worker,
    monkeypatch,
  ):
    current_time = [100.0]

    monkeypatch.setattr(
      hid_worker.time,
      "monotonic",
      lambda: current_time[0],
    )

    worker.start_fast_polling(
      interval=0.5,
      duration=30,
    )

    assert worker._fast_polling_until == 130.0

    current_time[0] = 110.0

    worker.start_fast_polling(
      interval=0.5,
      duration=30,
    )

    assert worker._fast_polling_until == 140.0
    assert worker._get_polling_interval() == 0.5

    current_time[0] = 139.9

    assert worker._get_polling_interval() == 0.5

    current_time[0] = 140.0

    assert worker._get_polling_interval() == 300

  @pytest.mark.parametrize(
    ("interval", "duration"),
    [
      (0, 30),
      (-1, 30),
      (0.5, 0),
      (0.5, -1),
    ],
  )
  def test_invalid_fast_polling_parameters_are_rejected(
    self,
    worker,
    interval,
    duration,
  ):
    with pytest.raises(ValueError):
      worker.start_fast_polling(
        interval=interval,
        duration=duration,
      )

  def test_fast_polling_wakes_polling_condition(self, worker):
    waiting = threading.Event()
    awakened = threading.Event()

    def wait_for_change():
      with worker._polling_condition:
        waiting.set()
        worker._polling_condition.wait(timeout=2)
        awakened.set()

    thread = threading.Thread(target=wait_for_change)
    thread.start()

    try:
      assert waiting.wait(timeout=1)

      worker.start_fast_polling(
        interval=0.5,
        duration=30,
      )

      assert awakened.wait(timeout=1)
    finally:
      worker.stop()
      thread.join(timeout=1)

    assert not thread.is_alive()

  def test_fast_polling_does_not_apply_to_passive_interface(
    self,
    config,
  ):
    interface_config = next(
      interface
      for interface in config.interfaces.values()
      if interface.mode != "polling"
    )

    worker = hid_worker.HIDWorker(
      worker_id=2,
      interface_config=interface_config,
      device_path=b"passive-interface",
      message_queue=hid_worker.Queue(),
    )

    worker.start_fast_polling(
      interval=0.5,
      duration=30,
    )

    assert worker._fast_polling_interval is None
    assert worker._fast_polling_until is None


# END TestAdaptivePolling

class TestPollingLifecycle:
  def test_stop_interrupts_polling_wait(self, worker):
    polling_started = threading.Event()
    write_calls = []

    class FakeTransport:
      def write(self, payload):
        write_calls.append(payload)
        polling_started.set()

      def read(self, timeout):
        return b"\x00"

    worker._transport = FakeTransport()
    worker._last_command_at = time.monotonic() - 300

    worker._process_report = lambda report: None

    thread = threading.Thread(
      target=worker._run_polling,
    )

    thread.start()

    try:
      assert polling_started.wait(timeout=1)

      worker.stop()
      thread.join(timeout=1)

      assert not thread.is_alive()
      assert len(write_calls) == 1

    finally:
      worker.stop()
      thread.join(timeout=1)

  def test_new_worker_does_not_inherit_fast_polling(
    self,
    config,
    monkeypatch,
  ):
    current_time = [100.0]

    monkeypatch.setattr(
      hid_worker.time,
      "monotonic",
      lambda: current_time[0],
    )

    previous_worker = hid_worker.HIDWorker(
      worker_id=1,
      interface_config=config.interfaces[3],
      device_path=b"interface-3",
      message_queue=hid_worker.Queue(),
    )

    previous_worker.start_fast_polling(
      interval=0.5,
      duration=30,
    )

    assert previous_worker._get_polling_interval() == 0.5

    new_worker = hid_worker.HIDWorker(
      worker_id=2,
      interface_config=config.interfaces[3],
      device_path=b"interface-3",
      message_queue=hid_worker.Queue(),
    )

    assert new_worker._fast_polling_interval is None
    assert new_worker._fast_polling_until is None
    assert new_worker._get_polling_interval() == 300

# END TestPollingLifecycle