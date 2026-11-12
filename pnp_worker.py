from __future__ import annotations

import logging
import threading

from dataclasses import dataclass
from enum import Enum, auto

from windows_pnp_api import resolve_pnp_devices


logger = logging.getLogger(__name__)
PNP_RETRY_INTERVAL = 0.25

class PnPWorkerMessageType(Enum):
  RESULT = auto()
  ERROR = auto()
  TERMINATED = auto()


@dataclass(frozen=True, slots=True)
class PnPWorkerMessage:
  worker_id: int
  session_id: int
  message_type: PnPWorkerMessageType
  payload: object = None


class PnPWorker(threading.Thread):
  def __init__(
    self,
    worker_id: int,
    session_id: int,
    hid_path: bytes | str,
    message_queue,
  ):
    super().__init__(
      name=f"PnPWorker-{worker_id}",
      daemon=True,
    )

    self.worker_id = worker_id
    self.session_id = session_id
    self._hid_path = hid_path
    self._message_queue = message_queue
    self._stop_requested = threading.Event()

  def stop(self):
    self._stop_requested.set()

  def run(self):
    try:
      while not self._stop_requested.is_set():
        result = resolve_pnp_devices(self._hid_path)

        render_id = result.get("audio_render_instance_id")
        capture_id = result.get("audio_capture_instance_id")

        if (
          isinstance(render_id, str)
          and render_id.strip()
          and isinstance(capture_id, str)
          and capture_id.strip()
        ):
          if not self._stop_requested.is_set():
            self._message_queue.put(
              PnPWorkerMessage(
                worker_id=self.worker_id,
                session_id=self.session_id,
                message_type=PnPWorkerMessageType.RESULT,
                payload={
                  "status": {
                    "pnp_device": result,
                  },
                },
              )
            )

          break

        logger.debug(
          "PnP endpoints are not ready yet. "
          "Retrying resolution."
        )

        self._stop_requested.wait(PNP_RETRY_INTERVAL)

    except Exception as error:
      if not self._stop_requested.is_set():
        logger.exception("PnP worker failed.")

        self._message_queue.put(
          PnPWorkerMessage(
            worker_id=self.worker_id,
            session_id=self.session_id,
            message_type=PnPWorkerMessageType.ERROR,
            payload=str(error),
          )
        )

    finally:
      self._message_queue.put(
        PnPWorkerMessage(
          worker_id=self.worker_id,
          session_id=self.session_id,
          message_type=PnPWorkerMessageType.TERMINATED,
        )
      )