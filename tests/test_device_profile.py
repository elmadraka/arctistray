import pytest
import copy

import device_profile
from device_profile import load_device_profile

@pytest.fixture
def profile_data():
  return copy.deepcopy(
    device_profile._load_profile_data("arctis_7_plus.yml")
  )

def validate(profile_data):
  return device_profile._build_device_config(profile_data)

class TestProfileLoading:
  def test_load_valid_profile(self):
    config = load_device_profile("arctis_7_plus.yml")

    assert config.name == "SteelSeries Arctis 7+"
    assert config.vendor_id == 0x1038
    assert config.dongle_id == 0x220e
    assert config.headset_id == 0x220c
    assert set(config.interfaces) == {3, 4, 5}


  def test_load_profile_preserves_on_off_keys(self):
    config = load_device_profile("arctis_7_plus.yml")

    values = config.interfaces[3].definitions["headset"]["values"]

    assert values["on"] == 0x03
    assert values["off"] == 0x01


  def test_missing_profile(self):
    with pytest.raises(FileNotFoundError, match="Device profile not found"):
      load_device_profile("missing.yml")


  @pytest.mark.parametrize(
    "filename",
    [
      "profile.yaml",
      "profile.txt",
      "profile",
    ],
  )
  def test_reject_invalid_extension(self, filename):
    with pytest.raises(ValueError, match="must be a .yml filename"):
      load_device_profile(filename)


  @pytest.mark.parametrize(
    "filename",
    [
      "../arctis_7_plus.yml",
      "subfolder/profile.yml",
      "C:/profiles/profile.yml",
    ],
  )
  def test_reject_external_profile_path(self, filename):
    with pytest.raises(ValueError, match="devices folder"):
      load_device_profile(filename)


  def test_invalid_yaml(self, tmp_path, monkeypatch):
    module_file = tmp_path / "arctis_device.py"
    devices_path = tmp_path / "devices"
    devices_path.mkdir()

    (devices_path / "invalid.yml").write_text(
      "device: [invalid",
      encoding="utf-8",
    )

    monkeypatch.setattr(device_profile, "__file__", str(module_file))

    with pytest.raises(ValueError, match="Invalid YAML"):
      device_profile.load_device_profile("invalid.yml")

# END TestProfileLoading

class TestProfileValidation:
  def test_valid_profile_passes_validation(self, profile_data):
    config = validate(profile_data)

    assert set(config.interfaces) == {3, 4, 5}


  def test_missing_interface_mode(self, profile_data):
    del profile_data["device"]["interfaces"][3]["mode"]

    with pytest.raises(ValueError, match=r"interfaces\.3\.mode is required"):
      validate(profile_data)


  def test_invalid_interface_mode(self, profile_data):
    profile_data["device"]["interfaces"][3]["mode"] = "unknown"

    with pytest.raises(ValueError, match="mode must be"):
      validate(profile_data)


  def test_empty_interfaces(self, profile_data):
    profile_data["device"]["interfaces"] = {}

    with pytest.raises(ValueError, match="at least one interface"):
      validate(profile_data)


  def test_unknown_technical_config_key(self, profile_data):
    profile_data["device"]["interfaces"][3]["config"]["unexpected"] = True

    with pytest.raises(ValueError, match="unexpected is not supported"):
      validate(profile_data)


  def test_polling_config_required_for_polling_mode(self, profile_data):
    del profile_data["device"]["interfaces"][3]["config"]["polling"]

    with pytest.raises(ValueError, match="polling is required"):
      validate(profile_data)


  def test_polling_config_rejected_for_passive_mode(self, profile_data):
    profile_data["device"]["interfaces"][4]["config"]["polling"] = {}

    with pytest.raises(ValueError, match="polling is not allowed"):
      validate(profile_data)


  def test_offset_must_fit_report_size(self, profile_data):
    profile_data["device"]["interfaces"][3]["headset"]["offset"] = 64

    with pytest.raises(ValueError, match="exceeds the configured report size"):
      validate(profile_data)


  def test_prefix_must_not_be_empty(self, profile_data):
    profile_data["device"]["interfaces"][5]["battery"]["prefix"] = []

    with pytest.raises(ValueError, match="must not be empty"):
      validate(profile_data)


  def test_prefix_must_fit_minimum_report_length(self, profile_data):
    profile_data["device"]["interfaces"][5]["battery"]["prefix"] = [
      0xb7, 0x00, 0x00
    ]

    with pytest.raises(ValueError, match="exceeds the minimum report length"):
      validate(profile_data)


  def test_semantic_range_max_must_not_be_less_than_min(self, profile_data):
    level = profile_data["device"]["interfaces"][3]["battery"]["level"]
    level["min"] = 4
    level["max"] = 2

    with pytest.raises(ValueError, match=r"max must be >= .*min"):
      validate(profile_data)


  @pytest.mark.parametrize(
    ("missing_key", "present_key"),
    [
      ("max", "min"),
      ("min", "max"),
    ],
  )

  def test_semantic_range_requires_min_and_max(
    self, 
    profile_data,
    missing_key,
    present_key,
  ):
    level = profile_data["device"]["interfaces"][3]["battery"]["level"]
    del level[missing_key]

    with pytest.raises(ValueError, match=rf"{missing_key} is required"):
      validate(profile_data)


  def test_button_report_must_match_report_size(self, profile_data):
    button = profile_data["device"]["interfaces"][4]["button"]
    button["single"]["report"] = [0x00, 0x02, 0x00]

    with pytest.raises(ValueError, match="exactly 4 bytes"):
      validate(profile_data)


  def test_semantic_values_must_be_valid_bytes(self, profile_data):
    profile_data["device"]["interfaces"][3]["headset"]["values"]["on"] = 256

    with pytest.raises(ValueError, match=r"must be <= 255"):
      validate(profile_data)

  @pytest.mark.parametrize(
    "key",
    ["dongle_id", "headset_id"],
  )
  def test_missing_product_id(self, profile_data, key):
    del profile_data["device"][key]

    with pytest.raises(ValueError):
      validate(profile_data)


  @pytest.mark.parametrize(
    "key",
    ["dongle_id", "headset_id"],
  )
  @pytest.mark.parametrize(
    "value",
    [-1, 0x10000],
  )
  def test_product_id_out_of_range(self, profile_data, key, value):
    profile_data["device"][key] = value

    with pytest.raises(ValueError, match="must be"):
      validate(profile_data)

  def test_old_product_ids_key_is_rejected(self, profile_data):
    profile_data["device"]["product_ids"] = [0x220e]

    with pytest.raises(ValueError, match="product_ids is not supported"):
      validate(profile_data)


# END TestProfileValidation