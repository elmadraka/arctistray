import pytest

import windows_pnp_api


CONTAINER_ID = "{F8704138-C7AF-11F1-AB57-C475AB3BACCD}"

HID_INSTANCE_ID = (
  r"HID\VID_1038&PID_220E&MI_03\7&123456&0&0000"
)

INTERFACE_INSTANCE_ID = (
  r"USB\VID_1038&PID_220E&MI_03\5&123456&0&0000"
)

PHYSICAL_INSTANCE_ID = (
  r"USB\VID_1038&PID_220E\5&123456&0&4"
)

RENDER_INSTANCE_ID = (
  r"SWD\MMDEVAPI\{0.0.0.00000000}.{RENDER}"
)

CAPTURE_INSTANCE_ID = (
  r"SWD\MMDEVAPI\{0.0.1.00000000}.{CAPTURE}"
)


def make_device(
  instance_id,
  parent=None,
  container_id=CONTAINER_ID,
  device_class=None,
):
  return {
    "instance_id": instance_id,
    "parent": parent,
    "container_id": container_id,
    "class": device_class,
  }


def make_device_tree():
  return {
    HID_INSTANCE_ID.upper(): make_device(
      HID_INSTANCE_ID,
      INTERFACE_INSTANCE_ID,
    ),
    INTERFACE_INSTANCE_ID.upper(): make_device(
      INTERFACE_INSTANCE_ID,
      PHYSICAL_INSTANCE_ID,
    ),
    PHYSICAL_INSTANCE_ID.upper(): make_device(
      PHYSICAL_INSTANCE_ID,
    ),
    RENDER_INSTANCE_ID.upper(): make_device(
      RENDER_INSTANCE_ID,
      container_id=CONTAINER_ID,
      device_class="AudioEndpoint",
    ),
    CAPTURE_INSTANCE_ID.upper(): make_device(
      CAPTURE_INSTANCE_ID,
      container_id=CONTAINER_ID,
      device_class="AudioEndpoint",
    ),
  }


class TestFindPhysicalDevice:
  def test_finds_physical_usb_device_in_parent_chain(self):
    devices = make_device_tree()
    hid_device = devices[HID_INSTANCE_ID.upper()]

    result = windows_pnp_api._find_physical_device(
      devices,
      hid_device,
    )

    assert result["instance_id"] == PHYSICAL_INSTANCE_ID

  def test_ignores_usb_interface_with_different_container_id(self):
    devices = make_device_tree()

    devices[INTERFACE_INSTANCE_ID.upper()]["container_id"] = (
      "{DIFFERENT-CONTAINER}"
    )

    result = windows_pnp_api._find_physical_device(
      devices,
      devices[HID_INSTANCE_ID.upper()],
    )

    assert result["instance_id"] == PHYSICAL_INSTANCE_ID

  def test_rejects_missing_container_id(self):
    devices = make_device_tree()
    hid_device = devices[HID_INSTANCE_ID.upper()]
    hid_device["container_id"] = None

    with pytest.raises(
      LookupError,
      match="no Container ID",
    ):
      windows_pnp_api._find_physical_device(
        devices,
        hid_device,
      )

  def test_rejects_missing_physical_device(self):
    devices = make_device_tree()

    devices.pop(PHYSICAL_INSTANCE_ID.upper())

    with pytest.raises(
      LookupError,
      match="Physical USB device was not found",
    ):
      windows_pnp_api._find_physical_device(
        devices,
        devices[HID_INSTANCE_ID.upper()],
      )


class TestResolvePnPDevices:
  def setup_method(self):
    self.setupapi = object()
    self.hid_path = r"\\?\HID#VID_1038&PID_220E&MI_03#TEST"

  def prepare_resolution(
    self,
    monkeypatch,
    devices=None,
    hid_instance_id=HID_INSTANCE_ID,
  ):
    monkeypatch.setattr(
      windows_pnp_api,
      "_setupapi_functions",
      lambda: self.setupapi,
    )

    monkeypatch.setattr(
      windows_pnp_api,
      "_find_hid_device",
      lambda setupapi, hid_path: hid_instance_id,
    )

    monkeypatch.setattr(
      windows_pnp_api,
      "_enumerate_pnp_devices",
      lambda setupapi: (
        make_device_tree() if devices is None else devices
      ),
    )

  def test_rejects_non_windows_platform(self, monkeypatch):
    monkeypatch.setattr(
      windows_pnp_api.sys,
      "platform",
      "linux",
    )

    with pytest.raises(
      RuntimeError,
      match="only available on Windows",
    ):
      windows_pnp_api.resolve_pnp_devices(self.hid_path)

  @pytest.mark.parametrize(
    "hid_path",
    [
      None,
      "",
      123,
      b"",
    ],
  )
  def test_rejects_invalid_hid_path(self, monkeypatch, hid_path):
    monkeypatch.setattr(
      windows_pnp_api.sys,
      "platform",
      "win32",
    )

    with pytest.raises(
      ValueError,
      match="valid HID device path",
    ):
      windows_pnp_api.resolve_pnp_devices(hid_path)

  def test_resolves_bytes_hid_path(self, monkeypatch):
    monkeypatch.setattr(
      windows_pnp_api.sys,
      "platform",
      "win32",
    )

    self.prepare_resolution(monkeypatch)

    received_paths = []

    def find_hid_device(setupapi, hid_path):
      received_paths.append(hid_path)
      return HID_INSTANCE_ID

    monkeypatch.setattr(
      windows_pnp_api,
      "_find_hid_device",
      find_hid_device,
    )

    result = windows_pnp_api.resolve_pnp_devices(
      self.hid_path.encode()
    )

    assert received_paths == [self.hid_path]
    assert result["device_instance_id"] == PHYSICAL_INSTANCE_ID

  def test_resolves_physical_device_and_audio_endpoints(
    self,
    monkeypatch,
  ):
    monkeypatch.setattr(
      windows_pnp_api.sys,
      "platform",
      "win32",
    )

    self.prepare_resolution(monkeypatch)

    result = windows_pnp_api.resolve_pnp_devices(
      self.hid_path
    )

    assert result == {
      "container_id": CONTAINER_ID,
      "device_instance_id": PHYSICAL_INSTANCE_ID,
      "audio_render_instance_id": RENDER_INSTANCE_ID,
      "audio_capture_instance_id": CAPTURE_INSTANCE_ID,
    }

  def test_rejects_hid_path_not_found(self, monkeypatch):
    monkeypatch.setattr(
      windows_pnp_api.sys,
      "platform",
      "win32",
    )

    self.prepare_resolution(
      monkeypatch,
      hid_instance_id=None,
    )

    with pytest.raises(
      LookupError,
      match="Exact HID interface path was not found",
    ):
      windows_pnp_api.resolve_pnp_devices(self.hid_path)

  def test_rejects_hid_instance_missing_from_device_list(
    self,
    monkeypatch,
  ):
    monkeypatch.setattr(
      windows_pnp_api.sys,
      "platform",
      "win32",
    )

    self.prepare_resolution(
      monkeypatch,
      devices={},
    )

    with pytest.raises(
      LookupError,
      match="HID device instance was not found",
    ):
      windows_pnp_api.resolve_pnp_devices(self.hid_path)

  def test_rejects_hid_without_container_id(self, monkeypatch):
    monkeypatch.setattr(
      windows_pnp_api.sys,
      "platform",
      "win32",
    )

    devices = make_device_tree()
    devices[HID_INSTANCE_ID.upper()]["container_id"] = None

    self.prepare_resolution(
      monkeypatch,
      devices=devices,
    )

    with pytest.raises(
      LookupError,
      match="HID device has no Container ID",
    ):
      windows_pnp_api.resolve_pnp_devices(self.hid_path)

  def test_returns_none_when_audio_endpoints_are_missing(
    self,
    monkeypatch,
  ):
    monkeypatch.setattr(
      windows_pnp_api.sys,
      "platform",
      "win32",
    )

    devices = make_device_tree()

    devices.pop(RENDER_INSTANCE_ID.upper())
    devices.pop(CAPTURE_INSTANCE_ID.upper())

    self.prepare_resolution(
      monkeypatch,
      devices=devices,
    )

    result = windows_pnp_api.resolve_pnp_devices(
      self.hid_path
    )

    assert result["device_instance_id"] == PHYSICAL_INSTANCE_ID
    assert result["audio_render_instance_id"] is None
    assert result["audio_capture_instance_id"] is None

  def test_ignores_audio_endpoints_from_different_container(
    self,
    monkeypatch,
  ):
    monkeypatch.setattr(
      windows_pnp_api.sys,
      "platform",
      "win32",
    )

    devices = make_device_tree()

    devices[RENDER_INSTANCE_ID.upper()]["container_id"] = (
      "{DIFFERENT-CONTAINER}"
    )

    self.prepare_resolution(
      monkeypatch,
      devices=devices,
    )

    result = windows_pnp_api.resolve_pnp_devices(
      self.hid_path
    )

    assert result["audio_render_instance_id"] is None
    assert result["audio_capture_instance_id"] == (
      CAPTURE_INSTANCE_ID
    )
