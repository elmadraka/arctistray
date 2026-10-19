import json
import os
import tempfile

from copy import deepcopy
from pathlib import Path


APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ArctisTray"
CONFIG_FILE = APP_DIR / "config.json"

DEFAULT_CONFIG = {
  "output_mode": "automatic",
  "fixed_output": None,
  "last_output": None,
  "change_input": False,
  "input_mode": "automatic",
  "fixed_input": None,
  "startup": False
}


class ConfigManager:
  def __init__(self):
    self.data = deepcopy(DEFAULT_CONFIG)
    self.load()

  def load(self):
    try:
      with CONFIG_FILE.open("r", encoding="utf-8") as file:
        saved = json.load(file)

      if isinstance(saved, dict):
        self.data.update({
          key: value
          for key, value in saved.items()
          if key in DEFAULT_CONFIG
        })

      if self.data["output_mode"] not in ("automatic", "fixed"):
        self.data["output_mode"] = "automatic"

      if self.data["input_mode"] not in ("automatic", "fixed"):
        self.data["input_mode"] = "automatic"

      if not isinstance(self.data["change_input"], bool):
        self.data["change_input"] = False

      if not isinstance(self.data["startup"], bool):
        self.data["startup"] = False

    except FileNotFoundError:
      self.save()
    except (json.JSONDecodeError, OSError):
      self.data = deepcopy(DEFAULT_CONFIG)
      self.save()

  def save(self):
    APP_DIR.mkdir(parents=True, exist_ok=True)

    fd, filename = tempfile.mkstemp(
      dir=APP_DIR,
      suffix=".tmp"
    )

    try:
      with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(self.data, file, indent=2, ensure_ascii=False)

      os.replace(filename, CONFIG_FILE)
    finally:
      if os.path.exists(filename):
        os.remove(filename)

  def get(self, key):
    return self.data.get(key, DEFAULT_CONFIG.get(key))

  def set(self, key, value):
    if key not in DEFAULT_CONFIG:
      raise KeyError(f"Configuración desconocida: {key}")

    self.data[key] = value
    self.save()
