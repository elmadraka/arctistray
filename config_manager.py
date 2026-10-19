import json
import os
from copy import deepcopy
from pathlib import Path


APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ArctisTray"
CONFIG_FILE = APP_DIR / "config.json"

DEFAULT_CONFIG = {
  "output_fixed": None,
  "input_change_enabled": False,
  "input_fixed": None,
  "startup_enabled": False
}


class ConfigManager:
  def __init__(self):
    self.data = deepcopy(DEFAULT_CONFIG)
    self.load()

  def _validate_value(self, key, value):
    if key not in DEFAULT_CONFIG:
      raise KeyError(f"Unknown key: {key}")

    if key in ("input_change_enabled", "startup_enabled"):
      if not isinstance(value, bool):
        raise ValueError(f"Invalid {key} value.")

    elif key in ("output_fixed", "input_fixed"):
      if value is not None and not isinstance(value, str):
        raise ValueError(f"Invalid {key} value.")

    return value

  def _normalize(self, data):
    normalized = deepcopy(DEFAULT_CONFIG)

    for key, value in data.items():
      if key not in DEFAULT_CONFIG:
        continue

      try:
        normalized[key] = self._validate_value(key, value)
      except ValueError:
        pass

    return normalized

  def load(self):
    try:
      with CONFIG_FILE.open("r", encoding="utf-8") as file:
        saved = json.load(file)

      if not isinstance(saved, dict):
        return

      self.data = self._normalize(saved)

    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
      pass

  def save(self):
    APP_DIR.mkdir(parents=True, exist_ok=True)

    with CONFIG_FILE.open("w", encoding="utf-8") as file:
      json.dump(self.data, file, indent=2, ensure_ascii=False)

  def get(self, key):
    if key not in DEFAULT_CONFIG:
      raise KeyError(f"Unknown key: {key}")

    return self.data[key]
  
  def _set(self, key, value):
    self._validate_value(key, value)

    if self.data[key] == value:
      return

    self.data[key] = value
    self.save()

  def set_fixed_output(self, device_id):
    self._set("output_fixed", device_id)

  def set_fixed_input(self, device_id):
    self._set("input_fixed", device_id)

  def set_input_change_enabled(self, enabled):
    self._set("input_change_enabled", enabled)

  def set_startup_enabled(self, enabled):
    self._set("startup_enabled", enabled)
