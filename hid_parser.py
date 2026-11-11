import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)

_INVALID_VALUE = object()


@dataclass(frozen=True, slots=True)
class FieldRule:
  name: str
  offset: int
  min_value: int | None = None
  max_value: int | None = None
  values: Mapping[Any, int] | None = None


@dataclass(frozen=True, slots=True)
class ActionRule:
  name: str


@dataclass(frozen=True, slots=True)
class ReportRule:
  prefix: tuple[int, ...] | None = None
  report: tuple[int, ...] | None = None
  min_length: int = 0
  fields: tuple[FieldRule, ...] = ()
  actions: tuple[ActionRule, ...] = ()


@dataclass(slots=True)
class _RuleBuilder:
  prefix: tuple[int, ...] | None = None
  report: tuple[int, ...] | None = None
  min_length: int = 0
  fields: list[FieldRule] = field(default_factory=list)
  actions: list[ActionRule] = field(default_factory=list)

  def build(self):
    return ReportRule(
      prefix=self.prefix,
      report=self.report,
      min_length=self.min_length,
      fields=tuple(self.fields),
      actions=tuple(self.actions),
    )


class HIDParser:
  def __init__(self, interface_config, response_config=None):
    self._interface_config = interface_config
    self._response_config = response_config
    self._rules = self._compile_rules()

  def _compile_rules(self):
    rules = []
    default_rule = _RuleBuilder()

    for name, definition in self._interface_config.definitions.items():
      self._compile_node(
        name,
        definition,
        (),
        default_rule,
        rules,
      )

    if default_rule.fields or default_rule.actions:
      if self._response_config is None:
        raise ValueError(
          "Response configuration is required for unscoped definitions"
        )

      prefix = self._response_config.get("prefix")

      if prefix is None:
        raise ValueError(
          "Response prefix is required for unscoped definitions"
        )

      default_rule.prefix = tuple(prefix)
      default_rule.min_length = len(prefix)
      rules.append(default_rule.build())

    return tuple(rules)

  def _compile_node(
    self,
    name,
    node,
    parent_path,
    default_rule,
    rules,
    parent_rule=None,
  ):
    path = (*parent_path, name)

    if isinstance(node, Mapping):
      has_selector = "prefix" in node or "report" in node

      if has_selector:
        target_rule = _RuleBuilder(
          prefix=(
            tuple(node["prefix"])
            if "prefix" in node
            else None
          ),
          report=(
            tuple(node["report"])
            if "report" in node
            else None
          ),
          min_length=node.get(
            "min_length",
            max(
              len(node.get("prefix", ())),
              len(node.get("report", ())),
            ),
          ),
        )
      else:
        target_rule = (
          parent_rule
          if parent_rule is not None
          else default_rule
        )

      if "offset" in node:
        target_rule.fields.append(
          self._build_field(path, node)
        )

      if "action" in node:
        target_rule.actions.append(
          ActionRule(name="_".join(path))
        )

      for child_name, child_node in node.items():
        if child_name in {
          "prefix",
          "report",
          "min_length",
          "offset",
          "min",
          "max",
          "values",
          "action",
        }:
          continue

        self._compile_node(
          child_name,
          child_node,
          path,
          default_rule,
          rules,
          parent_rule=target_rule,
        )

      if has_selector:
        rules.append(target_rule.build())

      return

    if isinstance(node, (list, tuple)):
      rules.append(
        ReportRule(
          report=tuple(node),
          min_length=len(node),
        )
      )

  @staticmethod
  def _build_field(path, definition):
    return FieldRule(
      name="_".join(path),
      offset=definition["offset"],
      min_value=definition.get("min"),
      max_value=definition.get("max"),
      values=definition.get("values"),
    )

  def parse(self, report):
    report = bytes(report)

    matching_rules = [
      rule
      for rule in self._rules
      if self._matches_rule(rule, report)
    ]

    if not matching_rules:
      return {}

    status = {}
    actions = []

    for rule in matching_rules:
      for field in rule.fields:
        value = self._parse_field(field, report)

        if value is not _INVALID_VALUE:
          status[field.name] = value

      actions.extend(
        action.name
        for action in rule.actions
      )

    if len(actions) > 1:
      logger.warning(
        "Ambiguous actions: interface=%s report_prefix=%s actions=%s",
        self._interface_config.interface_id,
        self._format_prefix(report),
        actions,
      )
      actions = []

    result = {}

    if status:
      result["status"] = status

    if actions:
      result["actions"] = actions

    return result

  def _parse_field(self, field, report):
    if field.offset >= len(report):
      raw_value = None
      valid = False
    else:
      raw_value = report[field.offset]
      valid = True

    if valid and field.values is not None:
      value = next(
        (
          semantic_value
          for semantic_value, hid_value in field.values.items()
          if hid_value == raw_value
        ),
        _INVALID_VALUE,
      )

      valid = value is not _INVALID_VALUE

    elif valid:
      value = raw_value

      if field.min_value is not None:
        valid = valid and raw_value >= field.min_value

      if field.max_value is not None:
        valid = valid and raw_value <= field.max_value

    if not valid:
      raw_hex = (
        f"0x{raw_value:02X}"
        if raw_value is not None
        else "<missing>"
      )

      logger.warning(
        "Invalid field value: interface=%s field=%s "
        "raw=%s report_prefix=%s",
        self._interface_config.interface_id,
        field.name,
        raw_hex,
        self._format_prefix(report),
      )

      return _INVALID_VALUE

    return value

  @staticmethod
  def _matches_rule(rule, report):
    if len(report) < rule.min_length:
      return False

    if rule.prefix is not None:
      if not report.startswith(bytes(rule.prefix)):
        return False

    if rule.report is not None:
      if report != bytes(rule.report):
        return False

    return True

  @staticmethod
  def _format_prefix(report):
    return " ".join(
      f"{byte:02X}"
      for byte in report[:4]
    )
