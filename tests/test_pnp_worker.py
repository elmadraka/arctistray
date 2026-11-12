import queue
import threading

import pytest

import pnp_worker


@pytest.fixture
def message_queue():
  return queue.Queue()


@pytest.fixture
def worker(message_queue):
  return pnp_worker.PnPWorker(
    worker_id=1,
    session_id=10,
    hid_path=b"interface-3",
    message_queue=message_queue,
  )


@pytest.fixture
def valid_result():
  return {
    "container_id": "{TEST-CONTAINER}",
    "device_instance_id": "USB\\VID_1038&PID_220E\\TEST",
    "audio_render_instance_id": "SWD\\RENDER",
    "audio_capture_instance_id": "SWD\\CAPTURE",
  }


class TestPnPWorkerMessages:
  def test_successful_resolution_sends_result_and_terminated(
    self,
    worker,
    message_queue,
    monkeypatch,
    valid_result,
  ):
    calls = []

    def resolve(hid_path):
      calls.append(hid_path)
      return valid_result

    monkeypatch.setattr(
      pnp_worker,
      "resolve_pnp_devices",
      resolve,
    )

    worker.run()

    result_message = message_queue.get_nowait()
    terminated_message = message_queue.get_nowait()

    assert calls == [b"interface-3"]

    assert result_message.worker_id == 1
    assert result_message.session_id == 10
    assert (
      result_message.message_type
      == pnp_worker.PnPWorkerMessageType.RESULT
    )
    assert result_message.payload == {
      "status": {
        "pnp_device": valid_result,
      },
    }

    assert terminated_message.worker_id == 1
    assert terminated_message.session_id == 10
    assert (
      terminated_message.message_type
      == pnp_worker.PnPWorkerMessageType.TERMINATED
    )

    assert message_queue.empty()

  def test_resolution_retries_until_both_endpoints_are_valid(
    self,
    worker,
    message_queue,
    monkeypatch,
    valid_result,
  ):
    results = [
      {
        **valid_result,
        "audio_render_instance_id": None,
      },
      {
        **valid_result,
        "audio_capture_instance_id": None,
      },
      valid_result,
    ]
    calls = []
    waits = []

    def resolve(hid_path):
      calls.append(hid_path)
      return results.pop(0)

    def wait(timeout):
      waits.append(timeout)
      return False

    monkeypatch.setattr(
      pnp_worker,
      "resolve_pnp_devices",
      resolve,
    )
    monkeypatch.setattr(
      worker._stop_requested,
      "wait",
      wait,
    )

    worker.run()

    result_message = message_queue.get_nowait()
    terminated_message = message_queue.get_nowait()

    assert calls == [
      b"interface-3",
      b"interface-3",
      b"interface-3",
    ]
    assert waits == [
      pnp_worker.PNP_RETRY_INTERVAL,
      pnp_worker.PNP_RETRY_INTERVAL,
    ]

    assert (
      result_message.message_type
      == pnp_worker.PnPWorkerMessageType.RESULT
    )
    assert result_message.payload == {
      "status": {
        "pnp_device": valid_result,
      },
    }

    assert (
      terminated_message.message_type
      == pnp_worker.PnPWorkerMessageType.TERMINATED
    )
    assert message_queue.empty()

  @pytest.mark.parametrize(
    "field",
    [
      "audio_render_instance_id",
      "audio_capture_instance_id",
    ],
  )
  @pytest.mark.parametrize("invalid_value", [None, ""])
  def test_empty_or_missing_endpoint_is_never_accepted(
    self,
    worker,
    message_queue,
    monkeypatch,
    valid_result,
    field,
    invalid_value,
  ):
    calls = []

    def resolve(hid_path):
      calls.append(hid_path)

      if len(calls) == 1:
        return {
          **valid_result,
          field: invalid_value,
        }

      return valid_result

    monkeypatch.setattr(
      pnp_worker,
      "resolve_pnp_devices",
      resolve,
    )
    monkeypatch.setattr(
      worker._stop_requested,
      "wait",
      lambda timeout: False,
    )

    worker.run()

    result_message = message_queue.get_nowait()

    assert len(calls) == 2
    assert (
      result_message.message_type
      == pnp_worker.PnPWorkerMessageType.RESULT
    )
    assert result_message.payload["status"]["pnp_device"] == valid_result

    assert (
      message_queue.get_nowait().message_type
      == pnp_worker.PnPWorkerMessageType.TERMINATED
    )
    assert message_queue.empty()

  def test_resolution_error_sends_error_and_terminated(
    self,
    worker,
    message_queue,
    monkeypatch,
  ):
    def resolve(hid_path):
      raise OSError("PnP resolution failed")

    monkeypatch.setattr(
      pnp_worker,
      "resolve_pnp_devices",
      resolve,
    )

    worker.run()

    error_message = message_queue.get_nowait()
    terminated_message = message_queue.get_nowait()

    assert error_message.worker_id == 1
    assert error_message.session_id == 10
    assert (
      error_message.message_type
      == pnp_worker.PnPWorkerMessageType.ERROR
    )
    assert error_message.payload == "PnP resolution failed"

    assert (
      terminated_message.message_type
      == pnp_worker.PnPWorkerMessageType.TERMINATED
    )

    assert message_queue.empty()

  def test_result_is_not_sent_when_stopped_before_resolution(
    self,
    worker,
    message_queue,
    monkeypatch,
  ):
    worker.stop()

    def resolve(hid_path):
      pytest.fail("Resolution must not run after stop")

    monkeypatch.setattr(
      pnp_worker,
      "resolve_pnp_devices",
      resolve,
    )

    worker.run()

    message = message_queue.get_nowait()

    assert (
      message.message_type
      == pnp_worker.PnPWorkerMessageType.TERMINATED
    )
    assert message_queue.empty()


class TestPnPWorkerLifecycle:
  def test_stop_during_resolution_discards_result(
    self,
    worker,
    message_queue,
    monkeypatch,
    valid_result,
  ):
    resolution_started = threading.Event()
    allow_resolution_to_finish = threading.Event()

    def resolve(hid_path):
      resolution_started.set()

      if not allow_resolution_to_finish.wait(timeout=2):
        raise TimeoutError("Test resolution timed out")

      return valid_result

    monkeypatch.setattr(
      pnp_worker,
      "resolve_pnp_devices",
      resolve,
    )

    worker.start()

    try:
      assert resolution_started.wait(timeout=1)

      worker.stop()
      allow_resolution_to_finish.set()

      worker.join(timeout=2)

      assert not worker.is_alive()

      message = message_queue.get_nowait()

      assert (
        message.message_type
        == pnp_worker.PnPWorkerMessageType.TERMINATED
      )
      assert message_queue.empty()

    finally:
      allow_resolution_to_finish.set()
      worker.stop()
      worker.join(timeout=2)

    assert not worker.is_alive()

  def test_stop_during_retry_wait_does_not_send_result(
    self,
    worker,
    message_queue,
    monkeypatch,
    valid_result,
  ):
    calls = []

    def resolve(hid_path):
      calls.append(hid_path)
      return {
        **valid_result,
        "audio_render_instance_id": None,
      }

    def stop_during_wait(timeout):
      worker.stop()
      return True

    monkeypatch.setattr(
      pnp_worker,
      "resolve_pnp_devices",
      resolve,
    )
    monkeypatch.setattr(
      worker._stop_requested,
      "wait",
      stop_during_wait,
    )

    worker.run()

    message = message_queue.get_nowait()

    assert len(calls) == 1
    assert (
      message.message_type
      == pnp_worker.PnPWorkerMessageType.TERMINATED
    )
    assert message_queue.empty()

  def test_worker_preserves_worker_and_session_ids(
    self,
    message_queue,
    monkeypatch,
    valid_result,
  ):
    worker = pnp_worker.PnPWorker(
      worker_id=25,
      session_id=42,
      hid_path=b"interface-5",
      message_queue=message_queue,
    )

    monkeypatch.setattr(
      pnp_worker,
      "resolve_pnp_devices",
      lambda hid_path: valid_result,
    )

    worker.run()

    messages = [
      message_queue.get_nowait(),
      message_queue.get_nowait(),
    ]

    assert all(message.worker_id == 25 for message in messages)
    assert all(message.session_id == 42 for message in messages)

    assert (
      messages[0].message_type
      == pnp_worker.PnPWorkerMessageType.RESULT
    )
    assert (
      messages[1].message_type
      == pnp_worker.PnPWorkerMessageType.TERMINATED
    )
