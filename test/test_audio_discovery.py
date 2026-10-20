"""Unit tests for AudioDiscovery that do not require Windows or pycaw."""

import importlib.util
import pathlib
import sys
import threading
import time
import types
import unittest
from enum import IntEnum
from unittest.mock import Mock


ROOT = pathlib.Path(__file__).resolve().parent


# Provide import-time stand-ins for Windows-only dependencies.
comtypes = types.ModuleType("comtypes")
comtypes.CoInitialize = Mock()
comtypes.CoUninitialize = Mock()
comtypes.COMError = type("COMError", (Exception,), {})
sys.modules["comtypes"] = comtypes

pycaw = types.ModuleType("pycaw")
callbacks = types.ModuleType("pycaw.callbacks")
constants = types.ModuleType("pycaw.constants")
pycaw_core = types.ModuleType("pycaw.pycaw")


class EDataFlow(IntEnum):
    eRender = 0
    eCapture = 1


class ERole(IntEnum):
    eConsole = 0
    eMultimedia = 1
    eCommunications = 2


class MMNotificationClient:
    pass


class AudioUtilities:
    enumerator = None
    devices = []
    set_default_calls = []

    @classmethod
    def GetDeviceEnumerator(cls):
        return cls.enumerator

    @classmethod
    def GetAllDevices(cls):
        return cls.devices

    @classmethod
    def SetDefaultDevice(cls, endpoint_id, roles=None):
        cls.set_default_calls.append((endpoint_id, roles))


callbacks.MMNotificationClient = MMNotificationClient
constants.EDataFlow = EDataFlow
constants.ERole = ERole
pycaw_core.AudioUtilities = AudioUtilities
sys.modules["pycaw"] = pycaw
sys.modules["pycaw.callbacks"] = callbacks
sys.modules["pycaw.constants"] = constants
sys.modules["pycaw.pycaw"] = pycaw_core

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
MODULE_PATH = PROJECT_ROOT / "audio_discovery.py"

spec = importlib.util.spec_from_file_location(
    "audio_discovery",
    MODULE_PATH,
)

if spec is None or spec.loader is None:
    raise ImportError(f"Cannot load AudioDiscovery from {MODULE_PATH}")

audio_discovery = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audio_discovery
spec.loader.exec_module(audio_discovery)
AudioDiscovery = audio_discovery.AudioDiscovery


class FakeEndpoint:
    def __init__(self, endpoint_id):
        self.endpoint_id = endpoint_id

    def GetId(self):
        return self.endpoint_id


class FakeEnumerator:
    def __init__(self):
        self.defaults = {
            (EDataFlow.eRender.value, ERole.eConsole.value): "out-console",
            (EDataFlow.eRender.value, ERole.eMultimedia.value): "out-media",
            (EDataFlow.eRender.value, ERole.eCommunications.value): "out-comms",
            (EDataFlow.eCapture.value, ERole.eConsole.value): "in-console",
            (EDataFlow.eCapture.value, ERole.eMultimedia.value): "in-media",
            (EDataFlow.eCapture.value, ERole.eCommunications.value): "in-comms",
        }
        self.registered = []
        self.unregistered = []

    def GetDefaultAudioEndpoint(self, flow, role):
        endpoint_id = self.defaults.get((flow, role))
        if endpoint_id is None:
            raise RuntimeError("No endpoint for role")
        return FakeEndpoint(endpoint_id)

    def RegisterEndpointNotificationCallback(self, observer):
        self.registered.append(observer)

    def UnregisterEndpointNotificationCallback(self, observer):
        self.unregistered.append(observer)


class FakeCoreDevice:
    def __init__(self, endpoint_id, state=1, friendly_name=None):
        self.id = endpoint_id
        self.FriendlyName = friendly_name or endpoint_id
        self._dev = Mock()
        self._dev.GetState.return_value = state


class AudioDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.discovery = AudioDiscovery()
        AudioUtilities.enumerator = FakeEnumerator()
        AudioUtilities.devices = []
        AudioUtilities.set_default_calls = []
        comtypes.CoInitialize.reset_mock()
        comtypes.CoUninitialize.reset_mock()

    def tearDown(self):
        self.discovery.stop(timeout=1)

    def test_identifier_helpers(self):
        self.assertEqual(audio_discovery.normalize(" {ABC-DEF} "), "abc-def")
        self.assertEqual(
            audio_discovery.get_endpoint_guid("{0.0.0.00000000}.{ABC-DEF}"),
            "abc-def",
        )
        self.assertIsNone(audio_discovery.get_endpoint_guid("not-an-endpoint"))

    def test_catalog_keeps_existing_schema(self):
        self.discovery.discover = Mock(
            return_value=[
                {"id": "o1", "name": "Headset", "flow": "output", "kind": "headset", "available": True},
                {"id": "i1", "name": "Mic", "flow": "input", "kind": "headset", "available": True},
                {"id": "v1", "name": "SteelSeries Sonar", "flow": "output", "kind": "virtual", "available": False},
            ]
        )
        catalog = self.discovery.get_catalog()
        self.assertEqual(
            set(catalog),
            {"devices", "outputs", "inputs", "supported_outputs", "supported_inputs", "active_outputs", "active_inputs", "headset_output", "headset_input"},
        )
        self.assertEqual(catalog["headset_output"]["id"], "o1")
        self.assertEqual(catalog["headset_input"]["id"], "i1")
        self.assertEqual([item["id"] for item in catalog["active_outputs"]], ["o1"])
        self.assertEqual([item["id"] for item in catalog["outputs"]], ["o1", "v1"])

    def test_catalog_returns_copies(self):
        self.discovery.discover = Mock(
            return_value=[{"id": "o1", "name": "Headset", "flow": "output", "kind": "headset", "available": True}]
        )
        catalog = self.discovery.get_catalog()
        catalog["devices"][0]["name"] = "mutated"
        self.assertEqual(self.discovery.get_catalog()["devices"][0]["name"], "Headset")

    def test_get_pnp_data_parses_records(self):
        stdout = "\n".join([
            "RECEIVER|USB\\VID_1038&PID_220E\\ABC|container-1",
            "AUDIO|USB\\VID_1038&PID_220E&MI_00\\ABC|parent-1|container-1",
            "ENDPOINT|Arctis Output|SWD\\MMDEVAPI\\{0.0.0.00000000}.{ABC-DEF}|parent-1|container-1|OK",
        ])
        result = types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        with unittest.mock.patch.object(audio_discovery.subprocess, "run", return_value=result):
            receivers, interfaces, endpoints = self.discovery.get_pnp_data()
        self.assertEqual(receivers[0]["container_id"], "container-1")
        self.assertEqual(interfaces[0]["parent"], "parent-1")
        self.assertEqual(endpoints[0]["name"], "Arctis Output")
        self.assertEqual(endpoints[0]["status"], "OK")

    def test_get_pnp_data_raises_on_powershell_failure(self):
        result = types.SimpleNamespace(returncode=1, stdout="", stderr="PowerShell failed")
        with unittest.mock.patch.object(audio_discovery.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "PowerShell failed"):
                self.discovery.get_pnp_data()

    def test_discover_classifies_pnp_matched_headset_endpoint(self):
        guid = "abc-def"
        endpoint_id = "{0.0.0.00000000}.{ABC-DEF}"
        self.discovery.get_pnp_data = Mock(return_value=(
            [{"instance_id": "receiver", "container_id": "container-1"}],
            [{"instance_id": "interface", "parent": "receiver", "container_id": "container-1"}],
            [{"name": "Arctis Output", "instance_id": f"SWD\\MMDEVAPI\\{{0.0.0.00000000}}.{{{guid}}}", "parent": "interface", "container_id": "container-1", "status": "OK"}],
        ))
        AudioUtilities.devices = [FakeCoreDevice(endpoint_id, 1, "Arctis 7+ Game")]
        devices = self.discovery.discover()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["kind"], "headset")
        self.assertEqual(devices[0]["flow"], "output")
        self.assertTrue(devices[0]["available"])

    def test_endpoint_event_refreshes_catalog(self):
        self.discovery.discover = Mock(return_value=[
            {"id": "o1", "name": "Headset", "flow": "output", "kind": "headset", "available": True}
        ])
        callback = Mock()
        self.discovery.add_listener("endpoint_added", callback)
        self.discovery._process_native_event("endpoint_added", {"endpoint_id": "o1"})
        callback.assert_called_once_with(self.discovery, endpoint_id="o1")
        self.discovery.discover.assert_called_once()
        self.assertEqual(self.discovery.get_catalog()["devices"][0]["id"], "o1")

    def test_listener_registration_deduplicates_and_removes(self):
        callback = Mock()
        self.discovery.add_listener("sample", callback)
        self.discovery.add_listener("sample", callback)
        self.discovery._emit("sample", {"value": 5})
        callback.assert_called_once_with(self.discovery, value=5)
        self.discovery.remove_listener("sample", callback)
        self.discovery._emit("sample", {"value": 6})
        callback.assert_called_once()

    def test_listener_errors_do_not_break_other_listeners(self):
        good = Mock()
        self.discovery.add_listener("sample", Mock(side_effect=ValueError("listener")))
        self.discovery.add_listener("sample", good)
        self.discovery._emit("sample", {"value": 1})
        good.assert_called_once_with(self.discovery, value=1)

    def test_get_default_devices_returns_each_role(self):
        result = self.discovery.get_default_devices("output")
        self.assertEqual(result["console"], "out-console")
        self.assertEqual(result["multimedia"], "out-media")
        self.assertEqual(result["communications"], "out-comms")

    def test_get_default_devices_rejects_invalid_flow(self):
        with self.assertRaises(ValueError):
            self.discovery.get_default_devices("render")

    def test_set_default_devices_changes_only_requested_roles(self):
        changed = self.discovery.set_default_devices("output", {"console": "new-out"})
        self.assertEqual(changed, {"console"})
        self.assertEqual(
            AudioUtilities.set_default_calls,
            [("new-out", [ERole.eConsole])],
        )

    def test_set_default_devices_skips_already_default_endpoint(self):
        changed = self.discovery.set_default_devices("output", {"console": "out-console"})
        self.assertEqual(changed, set())
        self.assertEqual(AudioUtilities.set_default_calls, [])

    def test_set_default_devices_validates_roles(self):
        with self.assertRaises(ValueError):
            self.discovery.set_default_devices("output", {"unknown": "out1"})
        with self.assertRaises(TypeError):
            self.discovery.set_default_devices("output", [("console", "out1")])

    def test_endpoint_availability(self):
        AudioUtilities.devices = [FakeCoreDevice("active", 1), FakeCoreDevice("inactive", 2)]
        self.assertTrue(self.discovery.is_endpoint_available("active"))
        self.assertFalse(self.discovery.is_endpoint_available("inactive"))
        self.assertFalse(self.discovery.is_endpoint_available("missing"))
        self.assertFalse(self.discovery.is_endpoint_available(None))

    def test_native_default_change_emits_structured_event(self):
        callback = Mock()
        self.discovery.add_listener("default_device_changed", callback)
        self.discovery._process_native_event(
            "default_device_changed",
            {"flow": "output", "role": "console", "endpoint_id": "out-new"},
        )
        callback.assert_called_once_with(
            self.discovery, flow="output", role="console", endpoint_id="out-new"
        )
        # A default-device change alone must not trigger a full discovery.
        self.discovery.discover = Mock()
        self.discovery._process_native_event(
            "default_device_changed",
            {"flow": "input", "role": "communications", "endpoint_id": "in-new"},
        )
        self.discovery.discover.assert_not_called()

    def test_stop_then_start_restarts_monitor(self):
        started = threading.Event()
        started_count = []
        processing = threading.Event()
        release_processing = threading.Event()

        def on_started(discovery):
            started_count.append(time.monotonic())
            if len(started_count) >= 2:
                started.set()

        self.discovery.add_listener("monitoring_started", on_started)
        self.discovery.start()
        deadline = time.monotonic() + 2
        while not started_count and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(started_count, "monitor did not start")

        def blocked_process(event, payload, refresh=True):
            processing.set()
            release_processing.wait(2)

        self.discovery._process_native_event = blocked_process
        self.discovery._enqueue_native_event("endpoint_added", {"endpoint_id": "x"})
        self.assertTrue(processing.wait(2), "worker did not process queued event")

        # Force stop() to time out while the worker is handling an event.
        self.assertFalse(self.discovery.stop(timeout=0.01))
        release_processing.set()
        worker = self.discovery._worker
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "worker did not stop after release")

        # Restart without an extra stop() call; queued sentinels must not leak.
        self.discovery.start()
        started.wait(2)
        self.assertGreaterEqual(len(started_count), 2, "monitor did not restart")
        time.sleep(0.05)
        self.assertTrue(self.discovery._monitoring, "monitor exited due to a stale stop event")


if __name__ == "__main__":
    unittest.main(verbosity=2)
