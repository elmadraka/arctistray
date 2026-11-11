import re
from pathlib import Path
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
import yaml

def _freeze_value(value):
  if isinstance(value, Mapping):
    return MappingProxyType({
      key: _freeze_value(item)
      for key, item in value.items()
    })

  if isinstance(value, (list, tuple)):
    return tuple(_freeze_value(item) for item in value)

  if isinstance(value, (set, frozenset)):
    return frozenset(_freeze_value(item) for item in value)

  return value

@dataclass(frozen=True, slots=True)
class HIDConfig:
  read_timeout_ms: int
  reconnect_interval: float
  retry_interval_ms: int
  retry_window: float


@dataclass(frozen=True, slots=True)
class InterfaceConfig:
  interface_id: int
  mode: str
  config: Mapping[str, Any]
  definitions: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DeviceConfig:
  name: str
  vendor_id: int
  dongle_id: int
  headset_id: int
  hid: HIDConfig
  interfaces: Mapping[int, InterfaceConfig]

class YAML12SafeLoader(yaml.SafeLoader):
  yaml_implicit_resolvers = {
    key: [
      (tag, regexp)
      for tag, regexp in resolvers
      if tag != "tag:yaml.org,2002:bool"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
  }


YAML12SafeLoader.add_implicit_resolver(
  "tag:yaml.org,2002:bool",
  re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
  list("tTfF"),
)

def _load_profile_data(filename):
  if not isinstance(filename, str) or not filename.endswith(".yml"):
    raise ValueError("Device profile must be a .yml filename")

  if Path(filename).name != filename:
    raise ValueError("Device profiles must be under the devices folder")

  base_path = Path(__file__).resolve().parent

  devices_path = (base_path / "devices").resolve()
  profile_path = (devices_path / filename).resolve()

  if profile_path.parent != devices_path:
    raise ValueError("Device profiles must be under the devices folder")

  if not profile_path.is_file():
    raise FileNotFoundError(
      f"Device profile not found: {filename}"
    )

  try:
    with profile_path.open("r", encoding="utf-8") as file:
      data = yaml.load(file, Loader=YAML12SafeLoader)
  except yaml.YAMLError as exc:
    raise ValueError(
      f"Invalid YAML in device profile '{filename}': {exc}"
    ) from exc

  if not isinstance(data, Mapping):
    raise ValueError(
      f"Device profile '{filename}' must contain a YAML mapping"
    )

  return data

def _require_mapping(value, path):
  if not isinstance(value, Mapping):
    raise ValueError(f"{path} must be a mapping")

  return value


def _validate_keys(data, path, required, optional=()):
  allowed = set(required) | set(optional)

  missing = set(required) - set(data)
  unknown = set(data) - allowed

  if missing:
    key = sorted(missing)[0]
    raise ValueError(f"{path}.{key} is required")

  if unknown:
    key = sorted(unknown)[0]
    raise ValueError(f"{path}.{key} is not supported")

def _require_integer(value, path, minimum=None, maximum=None):
  if isinstance(value, bool) or not isinstance(value, int):
    raise ValueError(f"{path} must be an integer")

  if minimum is not None and value < minimum:
    raise ValueError(f"{path} must be >= {minimum}")

  if maximum is not None and value > maximum:
    raise ValueError(f"{path} must be <= {maximum}")

  return value


def _require_number(value, path, minimum=None):
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise ValueError(f"{path} must be a number")

  if minimum is not None and value < minimum:
    raise ValueError(f"{path} must be >= {minimum}")

  return value

def _validate_device_identity(device):
  path = "device"

  _validate_keys(
    device,
    path,
    required={
      "name",
      "vendor_id",
      "dongle_id",
      "headset_id",
      "hid",
      "interfaces",
    },
  )

  name = device["name"]

  if not isinstance(name, str) or not name.strip():
    raise ValueError("device.name must be a non-empty string")

  vendor_id = _require_integer(
    device["vendor_id"],
    "device.vendor_id",
    minimum=0,
    maximum=0xffff,
  )

  dongle_id = _require_integer(
    device["dongle_id"],
    "device.dongle_id",
    minimum=0,
    maximum=0xffff,
  )

  headset_id = _require_integer(
    device["headset_id"],
    "device.headset_id",
    minimum=0,
    maximum=0xffff,
  )

  return name, vendor_id, dongle_id, headset_id

def _validate_hid_config(data):
  path = "device.hid"

  data = _require_mapping(data, path)

  _validate_keys(
    data,
    path,
    required={
      "read_timeout_ms",
      "reconnect_interval",
      "retry_interval_ms",
      "retry_window",
    },
  )

  return HIDConfig(
    read_timeout_ms=_require_integer(
      data["read_timeout_ms"],
      f"{path}.read_timeout_ms",
      minimum=1,
    ),
    reconnect_interval=_require_number(
      data["reconnect_interval"],
      f"{path}.reconnect_interval",
      minimum=0.001,
    ),
    retry_interval_ms=_require_integer(
      data["retry_interval_ms"],
      f"{path}.retry_interval_ms",
      minimum=1,
    ),
    retry_window=_require_number(
      data["retry_window"],
      f"{path}.retry_window",
      minimum=0.001,
    ),
  )

def _validate_interfaces(data):
  path = "device.interfaces"

  data = _require_mapping(data, path)

  if not data:
    raise ValueError(f"{path} must contain at least one interface")

  interfaces = {}

  for raw_id, raw_interface in data.items():
    interface_id = _require_integer(
      raw_id,
      f"{path} interface ID",
      minimum=0,
    )

    if interface_id in interfaces:
      raise ValueError(f"{path}.{interface_id} is duplicated")

    interface_path = f"{path}.{interface_id}"
    interface = _require_mapping(raw_interface, interface_path)

    for required_key in ("mode", "config"):
      if required_key not in interface:
        raise ValueError(
          f"{interface_path}.{required_key} is required"
        )

    mode = interface["mode"]

    if mode not in {"polling", "passive"}:
      raise ValueError(
        f"{interface_path}.mode must be 'polling' or 'passive'"
      )

    config = _validate_interface_config(
      interface["config"],
      f"{interface_path}.config",
    )

    has_polling = "polling" in config

    if mode == "polling" and not has_polling:
      raise ValueError(
        f"{interface_path}.config.polling is required for polling mode"
      )

    if mode == "passive" and has_polling:
      raise ValueError(
        f"{interface_path}.config.polling is not allowed for passive mode"
      )

    if has_polling:
      config["polling"] = _validate_polling_config(
        config["polling"],
        f"{interface_path}.config.polling",
      )

    report_size = config["report_size"]

    definitions = {}

    for key, value in interface.items():
      if key in {"mode", "config"}:
        continue

      definition_path = f"{interface_path}.{key}"

      definition = _validate_semantic_node(
        value,
        definition_path,
        report_size,
      )

      _validate_semantic_lengths(
        definition,
        definition_path,
        report_size,
      )

      definitions[key] = definition

    interfaces[interface_id] = InterfaceConfig(
      interface_id=interface_id,
      mode=mode,
      config=_freeze_value(config),
      definitions=_freeze_value(definitions),
    )

  return MappingProxyType(interfaces)

def _validate_interface_config(data, path):
  data = _require_mapping(data, path)

  _validate_keys(
    data,
    path,
    required={
      "usage_page",
      "usage",
      "report_size",
      "endpoint_in",
    },
    optional={"polling"},
  )

  usage_page = _require_integer(
    data["usage_page"],
    f"{path}.usage_page",
    minimum=0,
    maximum=0xffff,
  )

  usage = _require_integer(
    data["usage"],
    f"{path}.usage",
    minimum=0,
    maximum=0xffff,
  )

  report_size = _require_integer(
    data["report_size"],
    f"{path}.report_size",
    minimum=1,
  )

  endpoint_in = _require_integer(
    data["endpoint_in"],
    f"{path}.endpoint_in",
    minimum=0x80,
    maximum=0xff,
  )

  return {
    "usage_page": usage_page,
    "usage": usage,
    "report_size": report_size,
    "endpoint_in": endpoint_in,
    **(
      {"polling": data["polling"]}
      if "polling" in data
      else {}
    ),
  }

def _validate_polling_config(data, path):
  data = _require_mapping(data, path)

  _validate_keys(
    data,
    path,
    required={"refresh_interval", "commands"},
    optional={
      "transition_interval",
      "transition_timeout",
    },
  )

  refresh_interval = _require_number(
    data["refresh_interval"],
    f"{path}.refresh_interval",
    minimum=0.001,
  )

  transition_interval = None
  transition_timeout = None

  if "transition_interval" in data:
    transition_interval = _require_number(
      data["transition_interval"],
      f"{path}.transition_interval",
      minimum=0.001,
    )

  if "transition_timeout" in data:
    transition_timeout = _require_number(
      data["transition_timeout"],
      f"{path}.transition_timeout",
      minimum=0.001,
    )

  commands_data = _require_mapping(
    data["commands"],
    f"{path}.commands",
  )

  if not commands_data:
    raise ValueError(f"{path}.commands must not be empty")

  commands = {}

  for name, raw_command in commands_data.items():
    if not isinstance(name, str) or not name.strip():
      raise ValueError(f"{path}.commands contains an invalid name")

    command_path = f"{path}.commands.{name}"
    command = _require_mapping(raw_command, command_path)

    _validate_keys(
      command,
      command_path,
      required={
        "transport",
        "min_command_interval_ms",
        "request",
        "response",
      },
    )

    transport = command["transport"]

    if not isinstance(transport, str) or not transport.strip():
      raise ValueError(
        f"{command_path}.transport must be a non-empty string"
      )

    min_interval = _require_integer(
      command["min_command_interval_ms"],
      f"{command_path}.min_command_interval_ms",
      minimum=0,
    )

    commands[name] = {
      "transport": transport,
      "min_command_interval_ms": min_interval,
      "request": _validate_request(
        command["request"],
        f"{command_path}.request",
      ),
      "response": _validate_response(
        command["response"],
        f"{command_path}.response",
      ),
    }

  polling_config = {
    "refresh_interval": refresh_interval,
    "commands": commands,
  }

  if transition_interval is not None:
    polling_config["transition_interval"] = transition_interval

  if transition_timeout is not None:
    polling_config["transition_timeout"] = transition_timeout

  return _freeze_value(polling_config)

def _validate_byte_list(data, path, allow_empty=False):
  if not isinstance(data, list):
    raise ValueError(f"{path} must be a list of bytes")

  if not data and not allow_empty:
    raise ValueError(f"{path} must not be empty")

  return tuple(
    _require_integer(
      value,
      f"{path}[{index}]",
      minimum=0,
      maximum=0xff,
    )
    for index, value in enumerate(data)
  )


def _validate_request(data, path):
  data = _require_mapping(data, path)

  _validate_keys(
    data,
    path,
    required={"report_id", "report_length", "payload"},
  )

  report_id = _require_integer(
    data["report_id"],
    f"{path}.report_id",
    minimum=0,
    maximum=0xff,
  )

  report_length = _require_integer(
    data["report_length"],
    f"{path}.report_length",
    minimum=1,
  )

  payload = _validate_byte_list(
    data["payload"],
    f"{path}.payload",
    allow_empty=True,
  )

  if len(payload) > report_length:
    raise ValueError(
      f"{path}.payload exceeds report_length"
    )

  return {
    "report_id": report_id,
    "report_length": report_length,
    "payload": payload,
  }


def _validate_response(data, path):
  data = _require_mapping(data, path)

  _validate_keys(
    data,
    path,
    required={"prefix"},
  )

  return {
    "prefix": _validate_byte_list(
      data["prefix"],
      f"{path}.prefix",
    ),
  }

def _validate_offset(data, path, report_size):
  offset = _require_integer(
    data,
    path,
    minimum=0,
  )

  if offset >= report_size:
    raise ValueError(
      f"{path} exceeds the configured report size"
    )

  return offset


def _validate_min_length(data, path, report_size):
  min_length = _require_integer(
    data,
    path,
    minimum=1,
  )

  if min_length > report_size:
    raise ValueError(
      f"{path} exceeds the configured report size"
    )

  return min_length


def _validate_range(data, path):
  data = _require_mapping(data, path)

  _validate_keys(
    data,
    path,
    required={"min", "max"},
  )

  minimum = _require_number(data["min"], f"{path}.min")
  maximum = _require_number(data["max"], f"{path}.max")

  if maximum < minimum:
    raise ValueError(f"{path}.max must be >= {path}.min")

  return {
    "min": minimum,
    "max": maximum,
  }


def _validate_values(data, path):
  data = _require_mapping(data, path)

  if not data:
    raise ValueError(f"{path} must not be empty")

  values = {}

  for name, value in data.items():
    if not isinstance(name, str) or not name.strip():
      raise ValueError(f"{path} contains an invalid name")

    values[name] = _require_integer(
      value,
      f"{path}.{name}",
      minimum=0,
      maximum=0xff,
    )

  if len(set(values.values())) != len(values):
    raise ValueError(f"{path} must contain unique values")

  return values

def _validate_semantic_node(data, path, report_size):
  if isinstance(data, Mapping):
    result = {}

    for key, value in data.items():
      item_path = f"{path}.{key}"

      if key == "offset":
        result[key] = _validate_offset(
          value, item_path, report_size
        )

      elif key == "min_length":
        result[key] = _validate_min_length(
          value, item_path, report_size
        )

      elif key in {"prefix", "report"}:
        result[key] = _validate_byte_list(
          value, item_path
        )

      elif key == "values":
        result[key] = _validate_values(
          value, item_path
        )

      elif key == "action":
        if not isinstance(value, str) or not value.strip():
          raise ValueError(
            f"{item_path} must be a non-empty string"
          )
        result[key] = value

      elif key in {"min", "max"}:
        result[key] = _require_number(value, item_path)

      else:
        result[key] = _validate_semantic_node(
          value, item_path, report_size
        )

    if "min" in result or "max" in result:
      if "min" not in result:
        raise ValueError(f"{path}.min is required")

      if "max" not in result:
        raise ValueError(f"{path}.max is required")

      if result["max"] < result["min"]:
        raise ValueError(
          f"{path}.max must be >= {path}.min"
        )

    return result

  if isinstance(data, list):
    return [
      _validate_semantic_node(
        value, f"{path}[{index}]", report_size
      )
      for index, value in enumerate(data)
    ]

  return data

def _validate_semantic_lengths(data, path, report_size, effective_length=None):
  if not isinstance(data, Mapping):
    return

  if "min_length" in data:
    effective_length = data["min_length"]

  if "offset" in data:
    required_length = data["offset"] + 1

    if effective_length is not None and required_length > effective_length:
      raise ValueError(
        f"{path}.offset exceeds the effective report length"
      )

  if "prefix" in data and effective_length is not None:
    if len(data["prefix"]) > effective_length:
      raise ValueError(
        f"{path}.prefix exceeds the minimum report length"
      )

  if "report" in data:
    if len(data["report"]) != report_size:
      raise ValueError(
        f"{path}.report must contain exactly {report_size} bytes"
      )

  for key, value in data.items():
    if isinstance(value, Mapping):
      _validate_semantic_lengths(
        value,
        f"{path}.{key}",
        report_size,
        effective_length,
      )

def _build_device_config(data):
  data = _require_mapping(data, "profile")

  _validate_keys(
    data,
    "profile",
    required={"device"},
  )

  device = _require_mapping(data["device"], "device")

  name, vendor_id, dongle_id, headset_id = (
    _validate_device_identity(device)
  )

  hid = _validate_hid_config(device["hid"])
  interfaces = _validate_interfaces(device["interfaces"])

  return DeviceConfig(
    name=name,
    vendor_id=vendor_id,
    dongle_id=dongle_id,
    headset_id=headset_id,
    hid=hid,
    interfaces=interfaces,
  )

def load_device_profile(device_name):
  profile_data = _load_profile_data(device_name)
  return _build_device_config(profile_data)