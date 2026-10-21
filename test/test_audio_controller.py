import sys
import queue
import time
import unittest
import threading
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from audio_controller import AudioController


class FakeAudioDiscovery:
  def __init__(self):
    self.listeners = {}
    self.defaults = {
      "output": {
        "console": "speaker-console",
        "communications": "speaker-communications",
      },
      "input": {
        "console": "microphone-console",
        "communications": "microphone-communications",
      },
    }
    self.available_endpoints = set()
    self.started = False
    self.start_calls = 0
    self.stop_calls = 0
    self.set_default_calls = []

  def add_listener(self, event, callback):
    callbacks = self.listeners.setdefault(event, [])
    if callback not in callbacks:
      callbacks.append(callback)

  def remove_listener(self, event, callback):
    callbacks = self.listeners.get(event, [])
    if callback in callbacks:
      callbacks.remove(callback)

  def start(self):
    self.start_calls += 1
    self.started = True

  def stop(self, timeout=5):
    self.stop_calls += 1
    self.started = False
    return True

  def get_default_devices(self, flow):
    return self.defaults[flow].copy()

  def set_default_devices(self, flow, devices):
    self.set_default_calls.append((flow, devices.copy()))
    for role, device_id in devices.items():
      self.defaults[flow][role] = device_id

  def is_endpoint_available(self, endpoint_id):
    return endpoint_id in self.available_endpoints

  def get_catalog(self, refresh=False):
    return {
      "active_outputs": [],
      "active_inputs": [],
      "headset_output": None,
      "headset_input": None,
    }


class FakeDevice:
  def __init__(self):
    self.headset_connected = True
    self.headset_dongle_connected = True
    self.listeners = {}
    self.active_states = []

  def add_listener(self, event, callback):
    self.listeners.setdefault(event, []).append(callback)

  def remove_listener(self, event, callback):
    callbacks = self.listeners.get(event, [])
    if callback in callbacks:
      callbacks.remove(callback)

  def set_headset_active(self, active):
    self.active_states.append(active)

  def get_state(self):
    return {
      "headset_connected": self.headset_connected,
      "headset_dongle_connected": self.headset_dongle_connected,
      "headset_active": (
        self.active_states[-1] if self.active_states else None
      ),
    }


class AudioControllerTests(unittest.TestCase):
  def setUp(self):
    self.discovery = FakeAudioDiscovery()
    self.discovery_patch = patch(
      "audio_controller.AudioDiscovery",
      return_value=self.discovery,
    )
    self.discovery_patch.start()
    self.addCleanup(self.discovery_patch.stop)

    self.controller = AudioController()
    self.device = FakeDevice()

  def test_discovery_listeners_register_and_unregister(self):
    self.controller._register_discovery_listeners()
    self.controller._register_discovery_listeners()

    self.assertTrue(self.controller._discovery_listeners_registered)

    expected_events = {
      "monitoring_started",
      "monitoring_stopped",
      "default_device_changed",
      "catalog_changed",
      "discovery_error",
    }
    self.assertEqual(set(self.discovery.listeners), expected_events)

    for callbacks in self.discovery.listeners.values():
      self.assertEqual(len(callbacks), 1)

    self.controller._unregister_discovery_listeners()

    self.assertFalse(self.controller._discovery_listeners_registered)
    self.assertTrue(
      all(not callbacks for callbacks in self.discovery.listeners.values())
    )

  def test_start_and_stop_manage_workers_and_device_listeners(self):
    self.controller.bind_device(self.device)

    self.controller.start()

    self.assertTrue(self.controller._worker.is_alive())
    self.assertEqual(self.discovery.start_calls, 1)
    self.assertTrue(self.controller._discovery_listeners_registered)
    self.assertIn(
      self.controller._on_headset_connected,
      self.device.listeners["headset_connected"],
    )

    stopped = self.controller.stop(timeout=2)

    self.assertTrue(stopped)
    self.assertEqual(self.discovery.stop_calls, 1)
    self.assertIsNone(self.controller.device)
    self.assertFalse(self.controller._worker.is_alive())
    self.assertFalse(self.controller._discovery_listeners_registered)

    for callbacks in self.device.listeners.values():
      self.assertEqual(callbacks, [])

  def test_start_discards_stale_tasks_from_previous_lifecycle(self):
    self.controller.bind_device(self.device)
    self.controller._tasks.put("stop")
    self.controller._tasks.put(("default_changed", "output"))

    self.controller.start()

    self.assertTrue(self.controller._worker.is_alive())
    self.assertTrue(self.controller._tasks.empty())

    self.assertTrue(self.controller.stop(timeout=2))

  def test_manual_output_change_is_respected(self):
    self.controller.bind_device(self.device)
    self.controller.discovery_ready = True
    self.controller._discovery_monitor_ready = True
    self.controller._output_owned = True
    self.controller._activated_output = "arctis-output"
    self.discovery.available_endpoints.add("arctis-output")

    self.discovery.defaults["output"] = {
      "console": "user-speaker",
      "communications": "user-headset",
    }

    self.controller._handle_output_change()

    self.assertTrue(self.controller.output_overridden)
    self.assertFalse(self.controller._output_owned)
    self.assertIsNone(self.controller._activated_output)
    self.assertEqual(
      self.controller._output_previous,
      {
        "console": "user-speaker",
        "communications": "user-headset",
      },
    )
    self.assertIn(False, self.device.active_states)

    self.assertEqual(self.discovery.set_default_calls, [])

  def test_restoration_changes_only_roles_owned_by_controller(self):
    self.controller.config = {}
    self.controller._output_owned = True
    self.controller._activated_output = "arctis-output"
    self.controller._output_previous = {
      "console": "previous-console",
      "communications": "previous-communications",
    }
    self.controller._output_changed_roles = {"console"}

    self.discovery.defaults["output"] = {
      "console": "arctis-output",
      "communications": "arctis-output",
    }

    self.controller._catalog["outputs"] = [
      {"id": "previous-console"},
      {"id": "previous-communications"},
      {"id": "arctis-output"},
    ]

    self.controller._restore_flow("output")

    self.assertEqual(
      self.discovery.defaults["output"],
      {
        "console": "previous-console",
        "communications": "arctis-output",
      },
    )
    self.assertEqual(
      self.discovery.set_default_calls,
      [
        ("output", {"console": "previous-console"}),
      ],
    )
    self.assertFalse(self.controller._output_owned)
    self.assertIsNone(self.controller._activated_output)
    self.assertEqual(self.controller._output_changed_roles, set())

  def test_disconnect_restores_owned_output(self):
    self.controller.bind_device(self.device)
    self.controller._output_owned = True
    self.controller._activated_output = "arctis-output"
    self.controller._output_previous = {
      "console": "previous-console",
      "communications": "previous-communications",
    }
    self.controller._output_changed_roles = {
      "console",
      "communications",
    }

    self.discovery.defaults["output"] = {
      "console": "arctis-output",
      "communications": "arctis-output",
    }
    self.controller._catalog["outputs"] = [
      {"id": "previous-console"},
      {"id": "previous-communications"},
      {"id": "arctis-output"},
    ]

    self.device.headset_connected = False
    self.controller._deactivate()

    self.assertEqual(
      self.discovery.defaults["output"],
      {
        "console": "previous-console",
        "communications": "previous-communications",
      },
    )
    self.assertFalse(self.controller._output_owned)
    self.assertIsNone(self.controller._activated_output)
    self.assertIn(False, self.device.active_states)

  def test_reconnection_clears_manual_overrides(self):
    self.controller.bind_device(self.device)
    self.controller.discovery_ready = True
    self.controller._output_override = True
    self.controller._input_override = True

    self.controller._on_headset_connected(self.device)

    self.assertFalse(self.controller._output_override)
    self.assertFalse(self.controller._input_override)

    task = self.controller._tasks.get_nowait()
    self.assertEqual(task, "activate")

  def test_activation_request_is_ignored_when_discovery_has_failed(self):
    self.controller.discovery_ready = True
    self.controller.discovery_error = "Discovery failed"

    self.controller.request_activation()

    self.assertTrue(self.controller._tasks.empty())

  def test_unavailable_endpoint_is_not_treated_as_manual_change(self):
    self.controller.bind_device(self.device)
    self.controller.discovery_ready = True
    self.controller._discovery_monitor_ready = True
    self.controller._output_owned = True
    self.controller._output_override = False
    self.controller._activated_output = "arctis-output"

    self.discovery.available_endpoints.clear()
    self.discovery.defaults["console"] = "other-output"
    self.discovery.defaults["communications"] = "other-output"

    self.controller._handle_output_change()

    self.assertTrue(self.controller._output_owned)
    self.assertFalse(self.controller._output_override)

  def test_output_default_returns_false_for_missing_endpoint_id(self):
    self.assertFalse(self.controller.is_output_default(None))
    self.assertFalse(self.controller.is_output_default(""))

  def test_worker_survives_task_errors(self):
    processed = threading.Event()

    def fail_activation():
      processed.set()
      raise RuntimeError("Simulated activation failure")

    self.controller._activate = fail_activation
    self.controller._closing = False
    self.controller._tasks.put("activate")
    self.controller._tasks.put("stop")

    self.controller._worker_loop()

    self.assertTrue(processed.is_set())
    self.assertTrue(self.controller._tasks.empty())


  def test_restore_input_restores_roles_changed_by_controller(self):
    self.controller.config = {"input_change_enabled": True}
    self.controller._input_owned = True
    self.controller._activated_input = "arctis-input"
    self.controller._input_previous = {
      "console": "previous-console-input",
      "communications": "previous-communications-input",
    }
    self.controller._input_changed_roles = {
      "console",
      "communications",
    }
    self.controller._catalog["inputs"] = [
      {"id": "previous-console-input"},
      {"id": "previous-communications-input"},
      {"id": "arctis-input"},
    ]
    self.discovery.defaults["input"] = {
      "console": "arctis-input",
      "communications": "arctis-input",
    }

    self.controller._restore_flow("input")

    self.assertEqual(
      self.discovery.defaults["input"],
      {
        "console": "previous-console-input",
        "communications": "previous-communications-input",
      },
    )
    self.assertEqual(
      self.discovery.set_default_calls,
      [
        (
          "input",
          {
            "console": "previous-console-input",
            "communications": "previous-communications-input",
          },
        ),
      ],
    )
    self.assertFalse(self.controller._input_owned)
    self.assertIsNone(self.controller._activated_input)

  def test_restore_input_changes_only_roles_owned_by_controller(self):
    self.controller.config = {"input_change_enabled": True}
    self.controller._input_owned = True
    self.controller._activated_input = "arctis-input"
    self.controller._input_previous = {
      "console": "previous-console-input",
      "communications": "previous-communications-input",
    }
    self.controller._input_changed_roles = {"console"}
    self.controller._catalog["inputs"] = [
      {"id": "previous-console-input"},
      {"id": "previous-communications-input"},
      {"id": "arctis-input"},
    ]
    self.discovery.defaults["input"] = {
      "console": "arctis-input",
      "communications": "arctis-input",
    }

    self.controller._restore_flow("input")

    self.assertEqual(
      self.discovery.defaults["input"],
      {
        "console": "previous-console-input",
        "communications": "arctis-input",
      },
    )
    self.assertEqual(
      self.discovery.set_default_calls,
      [("input", {"console": "previous-console-input"})],
    )

  def test_restore_input_does_not_overwrite_later_manual_change(self):
    self.controller.config = {"input_change_enabled": True}
    self.controller._input_owned = True
    self.controller._activated_input = "arctis-input"
    self.controller._input_previous = {
      "console": "previous-console-input",
      "communications": "previous-communications-input",
    }
    self.controller._input_changed_roles = {
      "console",
      "communications",
    }
    self.controller._catalog["inputs"] = [
      {"id": "previous-console-input"},
      {"id": "previous-communications-input"},
      {"id": "arctis-input"},
      {"id": "user-selected-input"},
    ]
    self.discovery.defaults["input"] = {
      "console": "user-selected-input",
      "communications": "arctis-input",
    }

    self.controller._restore_flow("input")

    self.assertEqual(
      self.discovery.defaults["input"],
      {
        "console": "user-selected-input",
        "communications": "previous-communications-input",
      },
    )
    self.assertEqual(
      self.discovery.set_default_calls,
      [
        (
          "input",
          {"communications": "previous-communications-input"},
        ),
      ],
    )

  def test_restore_input_skips_previous_endpoint_missing_from_catalog(self):
    self.controller.config = {"input_change_enabled": True}
    self.controller._input_owned = True
    self.controller._activated_input = "arctis-input"
    self.controller._input_previous = {
      "console": "disconnected-previous-input",
      "communications": "previous-communications-input",
    }
    self.controller._input_changed_roles = {"console"}
    self.controller._catalog["inputs"] = [
      {"id": "previous-communications-input"},
      {"id": "arctis-input"},
    ]
    self.discovery.defaults["input"] = {
      "console": "arctis-input",
      "communications": "arctis-input",
    }

    self.controller._restore_flow("input")

    self.assertEqual(self.discovery.set_default_calls, [])
    self.assertEqual(
      self.discovery.defaults["input"]["console"],
      "arctis-input",
    )

  def test_disconnect_restores_owned_input_when_input_changes_are_enabled(self):
    self.controller.config = {"input_change_enabled": True}
    self.controller.bind_device(self.device)
    self.controller._input_owned = True
    self.controller._activated_input = "arctis-input"
    self.controller._input_previous = {
      "console": "previous-console-input",
      "communications": "previous-communications-input",
    }
    self.controller._input_changed_roles = {
      "console",
      "communications",
    }
    self.controller._catalog["inputs"] = [
      {"id": "previous-console-input"},
      {"id": "previous-communications-input"},
      {"id": "arctis-input"},
    ]
    self.discovery.defaults["input"] = {
      "console": "arctis-input",
      "communications": "arctis-input",
    }
    self.device.headset_connected = False

    self.controller._deactivate()

    self.assertEqual(
      self.discovery.defaults["input"],
      {
        "console": "previous-console-input",
        "communications": "previous-communications-input",
      },
    )
    self.assertFalse(self.controller._input_owned)
    self.assertIsNone(self.controller._activated_input)
    self.assertIn(False, self.device.active_states)


  def test_stop_restores_owned_output_when_headset_is_connected(self):
    self.controller.bind_device(self.device)
    self.controller.config = {}
    self.controller._output_owned = True
    self.controller._activated_output = "arctis-output"
    self.controller._output_previous = {
      "console": "previous-console",
      "communications": "previous-communications",
    }
    self.controller._output_changed_roles = {
      "console",
      "communications",
    }
    self.controller._catalog["outputs"] = [
      {"id": "previous-console"},
      {"id": "previous-communications"},
      {"id": "arctis-output"},
    ]
    self.discovery.defaults["output"] = {
      "console": "arctis-output",
      "communications": "arctis-output",
    }

    self.controller.stop(timeout=2)

    self.assertEqual(
      self.discovery.defaults["output"],
      {
        "console": "previous-console",
        "communications": "previous-communications",
      },
    )

  def test_stop_restores_owned_input_when_input_changes_are_enabled(self):
    self.controller.bind_device(self.device)
    self.controller.config = {"input_change_enabled": True}
    self.controller._input_owned = True
    self.controller._activated_input = "arctis-input"
    self.controller._input_previous = {
      "console": "previous-console-input",
      "communications": "previous-communications-input",
    }
    self.controller._input_changed_roles = {
      "console",
      "communications",
    }
    self.controller._catalog["inputs"] = [
      {"id": "previous-console-input"},
      {"id": "previous-communications-input"},
      {"id": "arctis-input"},
    ]
    self.discovery.defaults["input"] = {
      "console": "arctis-input",
      "communications": "arctis-input",
    }

    self.controller.stop(timeout=2)

    self.assertEqual(
      self.discovery.defaults["input"],
      {
        "console": "previous-console-input",
        "communications": "previous-communications-input",
      },
    )

  def test_stop_does_not_require_headset_disconnect_to_restore_output(self):
    self.controller.bind_device(self.device)
    self.device.headset_connected = True
    self.controller._output_owned = True
    self.controller._activated_output = "arctis-output"
    self.controller._output_previous = {
      "console": "previous-console",
      "communications": "previous-communications",
    }
    self.controller._output_changed_roles = {
      "console",
      "communications",
    }
    self.controller._catalog["outputs"] = [
      {"id": "previous-console"},
      {"id": "previous-communications"},
      {"id": "arctis-output"},
    ]
    self.discovery.defaults["output"] = {
      "console": "arctis-output",
      "communications": "arctis-output",
    }

    self.controller.stop(timeout=2)

    self.assertEqual(
      self.discovery.defaults["output"],
      {
        "console": "previous-console",
        "communications": "previous-communications",
      },
    )
    self.assertTrue(self.device.headset_connected)



if __name__ == "__main__":
  unittest.main()
