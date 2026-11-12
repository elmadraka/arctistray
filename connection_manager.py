from __future__ import annotations

import ctypes
import logging
import queue
import re
import threading
import hid
import time

from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum, auto
from types import MappingProxyType
from typing import Mapping

from hid_worker import (
  HIDWorker,
  WorkerMessage,
  WorkerMessageType,
)

from pnp_worker import (
  PnPWorker,
  PnPWorkerMessage,
  PnPWorkerMessageType,
)

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class InterfaceConnectionState:
  status: str


@dataclass(frozen=True, slots=True)
class ConnectionState:
  dongle_connected: bool
  headset_usb_connected: bool
  interfaces: Mapping[int, InterfaceConnectionState]


@dataclass
class InterfaceRuntime:
  status: str = "waiting"
  path: bytes | None = None
  worker: object | None = None
  next_retry_at: float | None = None
  worker_closing: bool = False

@dataclass
class PnPRuntime:
  status: str = "waiting"
  worker: PnPWorker | None = None
  session_id: int = 0
  hid_path: bytes | None = None
  worker_closing: bool = False
  resolved: bool = False
  next_retry_at: float | None = None

@dataclass(frozen=True, slots=True)
class DiscoveryResult:
  dongle_connected: bool
  headset_usb_connected: bool
  interface_paths: Mapping[int, bytes]
  unavailable_interfaces: frozenset[int]
  ambiguous_interfaces: frozenset[int]


class DeviceEventType(Enum):
  ARRIVAL = auto()
  REMOVAL = auto()


class ManagerEventType(Enum):
  STOP = auto()


@dataclass(frozen=True, slots=True)
class ManagerEvent:
  event_type: ManagerEventType


@dataclass(frozen=True, slots=True)
class DeviceNotification:
  event_type: DeviceEventType


class GUID(ctypes.Structure):
  _fields_ = [
    ("Data1", wintypes.DWORD),
    ("Data2", wintypes.WORD),
    ("Data3", wintypes.WORD),
    ("Data4", ctypes.c_ubyte * 8),
  ]


class CM_NOTIFY_DEVICE_INTERFACE(ctypes.Structure):
  _fields_ = [
    ("ClassGuid", GUID),
  ]


class CM_NOTIFY_EVENT_DATA_INTERFACE(ctypes.Structure):
  _fields_ = [
    ("FilterType", wintypes.DWORD),
    ("ClassGuid", GUID),
    ("SymbolicLink", wintypes.WCHAR * 1),
  ]


class CM_NOTIFY_DEVICE_INSTANCE(ctypes.Structure):
  _fields_ = [
    ("InstanceId", wintypes.WCHAR * 200),
  ]


class CM_NOTIFY_UNION(ctypes.Union):
  _fields_ = [
    ("DeviceInterface", CM_NOTIFY_DEVICE_INTERFACE),
    ("DeviceInstance", CM_NOTIFY_DEVICE_INSTANCE),
  ]


class CM_NOTIFY_FILTER(ctypes.Structure):
  _fields_ = [
    ("cbSize", wintypes.DWORD),
    ("Flags", wintypes.DWORD),
    ("FilterType", wintypes.DWORD),
    ("Reserved", wintypes.DWORD),
    ("u", CM_NOTIFY_UNION),
  ]


GUID_DEVINTERFACE_HID = (
  0x4D1E55B2,
  0xF16F,
  0x11CF,
  (0x88, 0xCB, 0x00, 0x11, 0x11, 0x00, 0x00, 0x30),
)

CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE = 0
CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL = 0
CM_NOTIFY_ACTION_DEVICEINTERFACEREMOVAL = 1
CR_SUCCESS = 0


def make_hid_guid():
  data1, data2, data3, data4 = GUID_DEVINTERFACE_HID

  return GUID(
    data1,
    data2,
    data3,
    (ctypes.c_ubyte * 8)(*data4),
  )


class HidDeviceNotifications:
  def __init__(self, events, vendor_id, dongle_id, headset_id):
    self._events = events
    self._vendor_id = vendor_id
    self._device_ids = {dongle_id, headset_id}

    self._dll = None
    self._notification_handle = None
    self._callback_ref = None
    self._callback_type = None
    self._active = False

  def start(self):
    if self._notification_handle is not None:
      raise RuntimeError("HID notifications are already registered.")

    if not hasattr(ctypes, "WinDLL"):
      raise RuntimeError("HID device notifications require Windows.")

    self._dll = ctypes.WinDLL("CfgMgr32", use_last_error=True)

    self._callback_type = ctypes.WINFUNCTYPE(
      wintypes.DWORD,
      wintypes.HANDLE,
      wintypes.LPVOID,
      wintypes.DWORD,
      wintypes.LPVOID,
      wintypes.DWORD,
    )

    self._callback_ref = self._callback_type(self._callback)

    self._dll.CM_Register_Notification.argtypes = [
      ctypes.POINTER(CM_NOTIFY_FILTER),
      wintypes.LPVOID,
      self._callback_type,
      ctypes.POINTER(wintypes.HANDLE),
    ]
    self._dll.CM_Register_Notification.restype = wintypes.DWORD

    self._dll.CM_Unregister_Notification.argtypes = [
      wintypes.HANDLE,
    ]
    self._dll.CM_Unregister_Notification.restype = wintypes.DWORD

    device_filter = CM_NOTIFY_FILTER()
    device_filter.cbSize = ctypes.sizeof(CM_NOTIFY_FILTER)
    device_filter.Flags = 0
    device_filter.FilterType = CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE
    device_filter.Reserved = 0
    device_filter.u.DeviceInterface.ClassGuid = make_hid_guid()

    notification_handle = wintypes.HANDLE()

    result = self._dll.CM_Register_Notification(
      ctypes.byref(device_filter),
      None,
      self._callback_ref,
      ctypes.byref(notification_handle),
    )

    if result != CR_SUCCESS:
      self._callback_ref = None
      self._callback_type = None
      raise OSError(
        f"CM_Register_Notification failed: 0x{result:08X}"
      )

    self._notification_handle = notification_handle
    self._active = True

  def stop(self):
    if self._notification_handle is None:
      return

    self._active = False

    result = self._dll.CM_Unregister_Notification(
      self._notification_handle
    )

    if result != CR_SUCCESS:
      raise OSError(
        f"CM_Unregister_Notification failed: 0x{result:08X}"
      )

    self._notification_handle = None
    self._active = False
    self._callback_ref = None
    self._callback_type = None
    self._dll = None

  def _callback(
    self,
    notification,
    context,
    action,
    event_data,
    event_data_size,
  ):
    if not self._active:
      return CR_SUCCESS

    try:
      if action == CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL:
        event_type = DeviceEventType.ARRIVAL
      elif action == CM_NOTIFY_ACTION_DEVICEINTERFACEREMOVAL:
        event_type = DeviceEventType.REMOVAL
      else:
        return CR_SUCCESS

      if not event_data:
        return CR_SUCCESS

      symbolic_link_offset = (
        CM_NOTIFY_EVENT_DATA_INTERFACE.SymbolicLink.offset
      )

      if event_data_size < symbolic_link_offset + ctypes.sizeof(wintypes.WCHAR):
        return CR_SUCCESS

      event = ctypes.cast(
        event_data,
        ctypes.POINTER(CM_NOTIFY_EVENT_DATA_INTERFACE),
      ).contents

      symbolic_link_address = (
        ctypes.addressof(event)
        + CM_NOTIFY_EVENT_DATA_INTERFACE.SymbolicLink.offset
      )

      max_chars = (
        event_data_size - symbolic_link_offset
      ) // ctypes.sizeof(wintypes.WCHAR)

      symbolic_link = ctypes.wstring_at(
        symbolic_link_address,
        max_chars,
      ).split("\0", 1)[0]

      if self._matches_device(symbolic_link):
        self._events.put(DeviceNotification(event_type))

    except Exception:
      logger.exception("Error processing HID device notification.")

    return CR_SUCCESS

  def _matches_device(self, symbolic_link):
    vendor_match = re.search(
      r"VID_([0-9A-F]{4})",
      symbolic_link,
      re.IGNORECASE,
    )
    product_match = re.search(
      r"PID_([0-9A-F]{4})",
      symbolic_link,
      re.IGNORECASE,
    )

    if vendor_match is None or product_match is None:
      return False

    vendor_id = int(vendor_match.group(1), 16)
    product_id = int(product_match.group(1), 16)

    return (
      vendor_id == self._vendor_id
      and product_id in self._device_ids
    )


class ConnectionManager:
  RETRY_INTERVAL = 2.0
  FAST_POLLING_INTERVAL = 0.5
  FAST_POLLING_DURATION = 30.0

  def __init__(self, config, message_queue):
    self._config = config
    self._device_message_queue = message_queue

    self._events = queue.Queue()

    self._thread = None
    self._status = "stopped"
    self._started = False
    self._lifecycle_lock = threading.Lock()
    self._notifications = None
    self._stop_requested = threading.Event()

    self._dongle_connected = False
    self._headset_usb_connected = False
    self._headset_on = None
    self._fast_polling_active = False
    self._last_published_device_status = None

    self._shutdown_error = None

    self._worker_id_counter = 0
    self._pnp_worker_id_counter = 0

    self._interfaces = {
      interface_id: InterfaceRuntime()
      for interface_id in config.interfaces
    }

    self._pnp = PnPRuntime()

    self._state = self._build_state()

  def start(self):
    with self._lifecycle_lock:
      if self._started:
        raise RuntimeError("ConnectionManager cannot be started twice.")

      self._started = True

      self._thread = threading.Thread(
        target=self._run,
        name="ConnectionManager",
        daemon=True,
      )

      try:
        self._thread.start()
      except Exception:
        self._thread = None
        self._started = False
        raise

  def stop(self):
    with self._lifecycle_lock:
      if not self._started:
        return

      if not self._stop_requested.is_set():
        self._stop_requested.set()
        self._events.put(
          ManagerEvent(ManagerEventType.STOP)
        )

      thread = self._thread

    if thread is not None:
      thread.join()

    if self._shutdown_error is not None:
      raise RuntimeError(
        f"ConnectionManager shutdown incomplete: "
        f"{self._shutdown_error}"
      ) from self._shutdown_error

  def _run(self):
    try:
      self._status = "running"

      if self._stop_requested.is_set():
        return

      self._notifications = HidDeviceNotifications(
        events=self._events,
        vendor_id=self._config.vendor_id,
        dongle_id=self._config.dongle_id,
        headset_id=self._config.headset_id,
      )

      self._notifications.start()

      if self._stop_requested.is_set():
        return

      try:
        discovery = self._discover_devices()
      except Exception:
        logger.exception("Initial device discovery failed.")
        discovery = None

        retry_at = time.monotonic() + self.RETRY_INTERVAL

        for runtime in self._interfaces.values():
          runtime.next_retry_at = retry_at

        self._pnp.next_retry_at = retry_at

      if self._stop_requested.is_set():
        return

      if discovery is not None:
        self._dongle_connected = discovery.dongle_connected
        self._headset_usb_connected = discovery.headset_usb_connected

        if discovery.dongle_connected:
          try:
            self._start_pnp_worker(discovery)
          except Exception:
            logger.exception("Failed to start PnP worker.")

        retry_interfaces = (
          discovery.unavailable_interfaces
          | discovery.ambiguous_interfaces
        )

        if discovery.dongle_connected and retry_interfaces:
          retry_at = time.monotonic() + self.RETRY_INTERVAL

          for interface_id in retry_interfaces:
            runtime = self._interfaces[interface_id]
            runtime.status = "unavailable"
            runtime.next_retry_at = retry_at

        if discovery.dongle_connected:
          for interface_id, path in discovery.interface_paths.items():
            if self._stop_requested.is_set():
              break

            try:
              self._start_worker(interface_id, path)
            except Exception:
              logger.exception(
                "Failed to start worker for interface %s",
                interface_id,
              )

      if discovery is not None:
        self._publish_device_status()

      self._publish_state()

      while True:
        try:
          event = self._events.get(timeout=self._get_next_retry_timeout())

        except queue.Empty:
          self._process_due_retries()
          continue

        if isinstance(event, ManagerEvent):
          if event.event_type == ManagerEventType.STOP:
            self._status = "stopping"
            break

        elif isinstance(event, DeviceNotification):
          if self._status != "stopping":
            self._handle_device_notification(event)

        elif isinstance(event, WorkerMessage):
          if self._status != "stopping":
            self._handle_worker_message(event)

        elif isinstance(event, PnPWorkerMessage):
          if self._status != "stopping":
            self._handle_pnp_worker_message(event)

        self._process_due_retries()

    except Exception:
      logger.exception("ConnectionManager coordinator failed.")

    finally:
      self._status = "stopping"

      if self._notifications is not None:
        notifications = self._notifications

        try:
          notifications.stop()

        except Exception:
          logger.exception(
            "Failed to stop device notifications."
          )

        else:
          self._notifications = None

      self._stop_pnp_worker()
      workers = []

      for runtime in self._interfaces.values():
        worker = runtime.worker

        if worker is None:
          continue

        workers.append((runtime, worker))

        if not runtime.worker_closing:
          runtime.worker_closing = True

          try:
            worker.stop()

          except Exception:
            logger.exception(
              "Failed to stop HID worker %s",
              worker.worker_id,
            )

      shutdown_error = None

      for runtime, worker in workers:
        try:
          worker.join()

        except Exception as error:
          logger.exception(
            "Failed to join HID worker %s",
            worker.worker_id,
          )

          try:
            if worker.is_alive():
              if shutdown_error is None:
                shutdown_error = error

              continue

          except Exception:
            logger.exception(
              "Failed to check HID worker %s state.",
              worker.worker_id,
            )

            if shutdown_error is None:
              shutdown_error = error

            continue

        runtime.worker = None
        runtime.worker_closing = False
        runtime.next_retry_at = None
        runtime.path = None
        runtime.status = "waiting"

      pnp_worker = self._pnp.worker

      if pnp_worker is not None:
        try:
          pnp_worker.join()

        except Exception as error:
          logger.exception(
            "Failed to join PnP worker %s.",
            pnp_worker.worker_id,
          )

          try:
            if pnp_worker.is_alive() and shutdown_error is None:
              shutdown_error = error
          except Exception:
            logger.exception(
              "Failed to check PnP worker %s state.",
              pnp_worker.worker_id,
            )

            if shutdown_error is None:
              shutdown_error = error

        else:
          self._pnp.worker = None
          self._pnp.worker_closing = False

      self._shutdown_error = shutdown_error
      self._status = (
        "error"
        if shutdown_error is not None
        else "stopped"
      )

      self._publish_state()

  def _discover_devices(self):
    devices = hid.enumerate(self._config.vendor_id, 0)

    dongles = [
      device for device in devices
      if device["vendor_id"] == self._config.vendor_id
      and device["product_id"] == self._config.dongle_id
    ]

    headsets = [
      device for device in devices
      if device["vendor_id"] == self._config.vendor_id
      and device["product_id"] == self._config.headset_id
    ]

    dongle_connected = bool(dongles)
    headset_usb_connected = bool(headsets)

    interface_paths = {}
    unavailable_interfaces = set()
    ambiguous_interfaces = set()

    if dongle_connected:
      for interface_id, interface_config in self._config.interfaces.items():
        usage_page = interface_config.config["usage_page"]
        usage = interface_config.config["usage"]

        matches = [
          device for device in dongles
          if device["usage_page"] == usage_page
          and device["usage"] == usage
        ]

        if len(matches) == 1:
          interface_paths[interface_id] = matches[0]["path"]

        elif len(matches) == 0:
          unavailable_interfaces.add(interface_id)
          logger.warning(
            "HID interface %s was not found.",
            interface_id,
          )

        else:
          ambiguous_interfaces.add(interface_id)
          logger.error(
            "HID interface %s has multiple matches.",
            interface_id,
          )

    return DiscoveryResult(
      dongle_connected=dongle_connected,
      headset_usb_connected=headset_usb_connected,
      interface_paths=MappingProxyType(interface_paths),
      unavailable_interfaces=frozenset(unavailable_interfaces),
      ambiguous_interfaces=frozenset(ambiguous_interfaces),
    )

  def _build_state(self):
    return ConnectionState(
      dongle_connected=self._dongle_connected,
      headset_usb_connected=self._headset_usb_connected,
      interfaces=MappingProxyType({
        interface_id: InterfaceConnectionState(
          status=runtime.status
        )
        for interface_id, runtime in self._interfaces.items()
      }),
    )

  def _publish_state(self):
    new_state = self._build_state()

    if new_state == self._state:
      return

    self._state = new_state

  def _start_worker(self, interface_id, device_path):
    runtime = self._interfaces[interface_id]

    if runtime.worker is not None:
      raise RuntimeError(
        f"Interface {interface_id} already has a worker"
      )

    if interface_id == 3:
      self._fast_polling_active = False

    self._worker_id_counter += 1

    runtime.path = device_path
    runtime.worker_closing = False
    runtime.next_retry_at = None

    try:
      worker = HIDWorker(
        worker_id=self._worker_id_counter,
        interface_config=self._config.interfaces[interface_id],
        device_path=device_path,
        message_queue=self._events,
      )

      runtime.worker = worker

      worker.start()

    except Exception:
      runtime.worker = None
      runtime.status = "unavailable"
      runtime.next_retry_at = time.monotonic() + self.RETRY_INTERVAL
      raise

    logger.debug(
      "Started HID worker %s for interface %s",
      worker.worker_id,
      interface_id,
    )

  def _start_pnp_worker(self, discovery):
    if not discovery.dongle_connected:
      return

    if (
      self._pnp.worker is not None
      or self._pnp.worker_closing
      or self._pnp.resolved
    ):
      return

    if not discovery.interface_paths:
      logger.warning(
        "Cannot start PnP worker: no HID interface is available."
      )

      self._pnp.status = "unavailable"
      self._pnp.next_retry_at = (
        time.monotonic() + self.RETRY_INTERVAL
      )
      return

    interface_id, hid_path = next(
      iter(discovery.interface_paths.items())
    )

    self._pnp_worker_id_counter += 1
    self._pnp.session_id += 1
    self._pnp.hid_path = hid_path
    self._pnp.next_retry_at = None
    self._pnp.status = "resolving"

    try:
      worker = PnPWorker(
        worker_id=self._pnp_worker_id_counter,
        session_id=self._pnp.session_id,
        hid_path=hid_path,
        message_queue=self._events,
      )

      self._pnp.worker = worker
      worker.start()

    except Exception:
      self._pnp.worker = None
      self._pnp.status = "unavailable"
      self._pnp.hid_path = None
      self._pnp.next_retry_at = (
        time.monotonic() + self.RETRY_INTERVAL
      )
      raise

    logger.info(
      "Started PnP worker %s using HID interface %s.",
      worker.worker_id,
      interface_id,
    )

  def _stop_pnp_worker(self):
    runtime = self._pnp

    # Invalidate the current session immediately.
    runtime.session_id += 1
    runtime.resolved = False
    runtime.hid_path = None
    runtime.next_retry_at = None
    runtime.status = "waiting"

    worker = runtime.worker

    if worker is None:
      runtime.worker_closing = False
      return

    if runtime.worker_closing:
      return

    runtime.worker_closing = True

    try:
      worker.stop()
    except Exception:
      logger.exception(
        "Failed to stop PnP worker %s.",
        worker.worker_id,
      )

  def _update_fast_polling(self):
    should_fast_poll = (
      self._dongle_connected
      and self._headset_on == "on"
      and self._headset_usb_connected is True
    )

    runtime = self._interfaces.get(3)

    if (
      runtime is None
      or runtime.worker is None
      or runtime.worker_closing
    ):
      self._fast_polling_active = False
      return

    if should_fast_poll == self._fast_polling_active:
      return

    if should_fast_poll:
      runtime.worker.start_fast_polling(
        interval=self.FAST_POLLING_INTERVAL,
        duration=self.FAST_POLLING_DURATION,
      )
    else:
      runtime.worker.stop_fast_polling()

    self._fast_polling_active = should_fast_poll

  def _set_headset_usb_connected(self, connected):
    if self._headset_usb_connected == connected:
      return

    self._headset_usb_connected = connected
    self._update_fast_polling()

  def _observe_headset_power(self, payload):
    if not isinstance(payload, Mapping):
      return

    status = payload.get("status")

    if not isinstance(status, Mapping):
      return

    headset_on = status.get("headset")

    if headset_on not in ("on", "off"):
      return

    self._headset_on = headset_on
    self._update_fast_polling()

  def _observe_headset_usb(self, payload):
    if not isinstance(payload, Mapping):
      return

    status = payload.get("status")

    if not isinstance(status, Mapping):
      return

    usb = status.get("usb")

    if usb == "connected":
      self._set_headset_usb_connected(True)
    elif usb == "disconnected":
      self._set_headset_usb_connected(False)

  def _publish_device_status(self):
    current_status = (
      self._dongle_connected,
      self._headset_usb_connected,
    )

    if current_status == self._last_published_device_status:
      return

    self._device_message_queue.put({
      "status": {
        "dongle_connected": self._dongle_connected,
        "headset_usb_connected": self._headset_usb_connected,
      },
    })

    self._last_published_device_status = current_status

  def _handle_device_notification(self, event: DeviceNotification):
    logger.debug(
      "Received device notification: %s",
      event.event_type.name,
    )

    try:
      discovery = self._discover_devices()
    except Exception:
      logger.exception(
        "Failed to discover devices while handling notification."
      )

      retry_at = time.monotonic() + self.RETRY_INTERVAL

      for runtime in self._interfaces.values():
        runtime.next_retry_at = retry_at

      self._pnp.next_retry_at = retry_at

      return
    
    self._dongle_connected = discovery.dongle_connected
    self._update_fast_polling()

    self._set_headset_usb_connected(
      discovery.headset_usb_connected
    )

    if not discovery.dongle_connected:
      self._stop_pnp_worker()

      for runtime in self._interfaces.values():
        runtime.path = None
        runtime.next_retry_at = None

        if runtime.worker is not None:
          runtime.status = "restarting"

          if not runtime.worker_closing:
            runtime.worker_closing = True
            try:
              runtime.worker.stop()

            except Exception:
              logger.exception(
                "Failed to stop HID worker %s",
                runtime.worker.worker_id,
              )
        else:
          runtime.status = "waiting"

      self._publish_device_status()
      self._publish_state()
      return

    try:
      self._start_pnp_worker(discovery)
    except Exception:
      logger.exception("Failed to start PnP worker after device notification.")

    for interface_id, runtime in self._interfaces.items():
      path = discovery.interface_paths.get(interface_id)

      if path is None:
        runtime.path = None

        if runtime.worker is not None:
          runtime.next_retry_at = None

          if not runtime.worker_closing:
            runtime.worker_closing = True
            try:
              runtime.worker.stop()

            except Exception:
              logger.exception(
                "Failed to stop HID worker %s",
                runtime.worker.worker_id,
              )
        else:
          runtime.status = "unavailable"
          runtime.next_retry_at = (
            time.monotonic() + self.RETRY_INTERVAL
          )

        continue

      if runtime.worker is not None:
        if runtime.path != path and not runtime.worker_closing:
          runtime.path = path
          runtime.worker_closing = True

          try:
            runtime.worker.stop()

          except Exception:
            logger.exception(
              "Failed to stop worker for interface %s after path change.",
              interface_id,
            )

        continue

      runtime.path = path
      runtime.next_retry_at = None
      runtime.status = "waiting"

      try:
        self._start_worker(interface_id, path)

      except Exception:
        logger.exception(
          "Failed to start worker for interface %s",
          interface_id,
        )

    self._publish_device_status()
    self._publish_state()

  def _queue_device_payload(
    self,
    interface_id: int,
    payload: dict,
  ) -> None:
    if interface_id != 3 or not self._headset_usb_connected:
      self._device_message_queue.put(payload)
      return

    status = payload.get("status")

    if not isinstance(status, dict):
      self._device_message_queue.put(payload)
      return

    filtered_status = dict(status)
    filtered_status.pop("mixdial_game", None)
    filtered_status.pop("mixdial_chat", None)

    if filtered_status == status:
      self._device_message_queue.put(payload)
      return

    filtered_payload = dict(payload)

    if filtered_status:
      filtered_payload["status"] = filtered_status
    else:
      filtered_payload.pop("status", None)

    if filtered_payload:
      self._device_message_queue.put(filtered_payload)

  def _handle_worker_message(self, message: WorkerMessage):
    runtime = self._interfaces.get(message.interface_id)

    if runtime is None:
      return

    if (
      runtime.worker is None
      or runtime.worker.worker_id != message.worker_id
    ):
      return

    if message.message_type == WorkerMessageType.MESSAGE:
      if runtime.worker_closing:
        return

      if message.interface_id == 5:
        self._observe_headset_power(message.payload)
        self._observe_headset_usb(message.payload)

      if message.payload:
        self._device_message_queue.put(message.payload)
      return

    if message.message_type == WorkerMessageType.READY:
      if runtime.worker_closing:
        return

      if message.payload:
        if message.interface_id == 5:
          self._observe_headset_power(message.payload)
          self._observe_headset_usb(message.payload)

        self._device_message_queue.put(message.payload)

      if runtime.status == "waiting":
        runtime.status = "available"
        self._publish_state()

      if message.interface_id == 3:
        self._update_fast_polling()

      return

    if message.message_type == WorkerMessageType.ERROR:
      if runtime.worker_closing:
        return

      runtime.status = "restarting"
      runtime.worker_closing = True

      try:
        runtime.worker.stop()

      except Exception:
        logger.exception(
          "Failed to stop HID worker %s after an error.",
          runtime.worker.worker_id,
        )

      logger.error(
        "HID worker %s failed on interface %s: %s",
        message.worker_id,
        message.interface_id,
        message.payload,
      )

      return

    if message.message_type == WorkerMessageType.TERMINATED:
      runtime.status = "restarting"
      self._publish_state()

      worker = runtime.worker

      try:
        worker.join()
      except Exception:
        logger.exception(
          "Failed to join worker %s for interface %s.",
          worker.worker_id,
          message.interface_id,
        )

        try:
          if worker.is_alive():
            return
        except Exception:
          logger.exception(
            "Failed to check worker %s state for interface %s.",
            worker.worker_id,
            message.interface_id,
          )
          return

      runtime.worker = None
      runtime.worker_closing = False
      runtime.status = "waiting"
      runtime.next_retry_at = (
        time.monotonic() + self.RETRY_INTERVAL
      )

      self._publish_state()

  def _handle_pnp_worker_message(
    self,
    message: PnPWorkerMessage,
  ):
    runtime = self._pnp
    worker = runtime.worker

    if worker is None or worker.worker_id != message.worker_id:
      return

    if (
      message.message_type != PnPWorkerMessageType.TERMINATED
      and runtime.session_id != message.session_id
    ):
      return

    if message.message_type == PnPWorkerMessageType.RESULT:
      if runtime.worker_closing:
        return

      if not isinstance(message.payload, Mapping):
        logger.error("PnP worker returned an invalid result.")
        runtime.status = "unavailable"
        return

      status = message.payload.get("status")

      if not isinstance(status, Mapping):
        logger.error("PnP worker result has no valid status.")
        runtime.status = "unavailable"
        return

      pnp_device = status.get("pnp_device")

      if not isinstance(pnp_device, Mapping):
        logger.error("PnP worker result has no valid pnp_device.")
        runtime.status = "unavailable"
        return

      runtime.resolved = True
      runtime.status = "available"

      self._device_message_queue.put(message.payload)

      logger.info("PnP device identity resolved successfully.")
      return

    if message.message_type == PnPWorkerMessageType.ERROR:
      if runtime.worker_closing:
        return

      runtime.status = "unavailable"

      logger.error(
        "PnP worker %s failed: %s",
        message.worker_id,
        message.payload,
      )
      return

    if message.message_type == PnPWorkerMessageType.TERMINATED:
      try:
        worker.join()
      except Exception:
        logger.exception(
          "Failed to join PnP worker %s.",
          message.worker_id,
        )

        try:
          if worker.is_alive():
            return
        except Exception:
          logger.exception(
            "Failed to check PnP worker %s state.",
            message.worker_id,
          )
          return

      runtime.worker = None
      runtime.worker_closing = False

      if runtime.resolved:
        runtime.status = "available"
        runtime.next_retry_at = None
      elif self._dongle_connected:
        runtime.status = "unavailable"
        runtime.next_retry_at = (
          time.monotonic() + self.RETRY_INTERVAL
        )
      else:
        runtime.status = "waiting"
        runtime.next_retry_at = None

      logger.debug("PnP worker %s terminated.", message.worker_id)

  def _get_next_retry_timeout(self):
    retry_times = [
      runtime.next_retry_at
      for runtime in self._interfaces.values()
      if runtime.next_retry_at is not None
    ]

    if self._pnp.next_retry_at is not None:
      retry_times.append(self._pnp.next_retry_at)

    if not retry_times:
      return None

    return max(0, min(retry_times) - time.monotonic())

  def _process_due_retries(self):
    now = time.monotonic()

    due_interfaces = [
      interface_id
      for interface_id, runtime in self._interfaces.items()
      if (
        runtime.next_retry_at is not None
        and runtime.next_retry_at <= now
      )
    ]

    pnp_retry_due = (
      self._pnp.next_retry_at is not None
      and self._pnp.next_retry_at <= now
    )

    if not due_interfaces and not pnp_retry_due:
      return

    try:
      discovery = self._discover_devices()
    except Exception:
      logger.exception(
        "Failed to discover devices while processing retries."
      )

      retry_at = time.monotonic() + self.RETRY_INTERVAL

      for interface_id in due_interfaces:
        runtime = self._interfaces[interface_id]
        runtime.next_retry_at = retry_at

      if pnp_retry_due:
        self._pnp.next_retry_at = (
          time.monotonic() + self.RETRY_INTERVAL
        )

      return

    if self._stop_requested.is_set():
      return

    self._dongle_connected = discovery.dongle_connected
    self._set_headset_usb_connected(
      discovery.headset_usb_connected
    )

    if not discovery.dongle_connected:
      self._stop_pnp_worker()

      for interface_id, runtime in self._interfaces.items():
        runtime.next_retry_at = None
        runtime.path = None
        runtime.status = "waiting"

        if (
          runtime.worker is not None
          and not runtime.worker_closing
        ):
          runtime.worker_closing = True
          try:
            runtime.worker.stop()
          except Exception:
            logger.exception(
              "Failed to stop worker for interface %s after dongle disconnection.",
              interface_id,
            )

      self._publish_device_status()
      self._publish_state()
      return

    if pnp_retry_due:
      self._pnp.next_retry_at = None

      try:
        self._start_pnp_worker(discovery)
      except Exception:
        logger.exception(
          "Failed to restart PnP worker during retry processing."
        )

    for interface_id in due_interfaces:
      if self._stop_requested.is_set():
        break

      runtime = self._interfaces[interface_id]
      runtime.next_retry_at = None

      if runtime.worker is not None:
        continue

      path = discovery.interface_paths.get(interface_id)

      if path is None:
        runtime.path = None
        runtime.status = "unavailable"
        runtime.next_retry_at = (
          time.monotonic() + self.RETRY_INTERVAL
        )
        continue

      runtime.path = path
      runtime.status = "waiting"

      try:
        self._start_worker(interface_id, path)

      except Exception:
        logger.exception(
          "Failed to restart worker for interface %s",
          interface_id,
        )

        runtime.status = "waiting"
        runtime.next_retry_at = (
          time.monotonic() + self.RETRY_INTERVAL
        )

    self._publish_device_status()
    self._publish_state()