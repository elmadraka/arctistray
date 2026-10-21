import os
import subprocess
import sys


APP_NAME = "ArctisTray"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _get_startup_command():
  if getattr(sys, "frozen", False):
    arguments = [sys.executable]
  else:
    arguments = [sys.executable, os.path.abspath(sys.argv[0])]

  return subprocess.list2cmdline(arguments)


def _read_startup_entry():
  if sys.platform != "win32":
    raise RuntimeError("Windows startup registration requires Windows.")

  import winreg

  try:
    with winreg.OpenKey(
      winreg.HKEY_CURRENT_USER,
      RUN_KEY,
      0,
      winreg.KEY_READ
    ) as key:
      try:
        value, _ = winreg.QueryValueEx(key, APP_NAME)
        return value
      except FileNotFoundError:
        return None
  except FileNotFoundError:
    return None


def is_startup_enabled():
  value = _read_startup_entry()
  exists = value is not None

  if exists:
    print("[STARTUP] Windows registry entry already exists.")
  else:
    print("[STARTUP] Windows registry entry does not exist.")

  return exists


def set_startup_enabled(enabled):
  if not isinstance(enabled, bool):
    raise ValueError("enabled must be a bool.")

  if sys.platform != "win32":
    raise RuntimeError("Windows startup registration requires Windows.")

  import winreg

  if enabled:
    with winreg.CreateKeyEx(
      winreg.HKEY_CURRENT_USER,
      RUN_KEY,
      0,
      winreg.KEY_SET_VALUE
    ) as key:
      winreg.SetValueEx(
        key,
        APP_NAME,
        0,
        winreg.REG_SZ,
        _get_startup_command()
      )

    if _read_startup_entry() is None:
      raise RuntimeError(
        "The startup entry could not be verified."
      )

    print("[STARTUP] Windows registry entry created successfully.")

  else:
    try:
      with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        RUN_KEY,
        0,
        winreg.KEY_SET_VALUE
      ) as key:
        try:
          winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
          pass
    except FileNotFoundError:
      pass

    if _read_startup_entry() is not None:
      raise RuntimeError(
        "The startup entry could not be removed."
      )

    print("[STARTUP] Windows registry entry removed successfully.")
