import logging
from types import MappingProxyType

import pytest

from device_profile import InterfaceConfig, load_device_profile
from hid_parser import HIDParser


@pytest.fixture
def profile():
  return load_device_profile("arctis_7_plus.yml")


def make_parser(profile, interface_id):
  interface = profile.interfaces[interface_id]

  response_config = None

  if interface.mode == "polling":
    response_config = (
      interface.config["polling"]["commands"]["status"]["response"]
    )

  return HIDParser(interface, response_config)


def make_report(prefix, fields):
  report = bytearray(64)
  report[0] = prefix

  for offset, value in fields.items():
    report[offset] = value

  return bytes(report)


class TestCompilation:
  def test_compile_if3(self, profile):
    parser = make_parser(profile, 3)

    assert len(parser._rules) == 1

    rule = parser._rules[0]

    assert rule.prefix == (0xb0,)
    assert rule.report is None
    assert rule.min_length == 1

    assert {
      field.name
      for field in rule.fields
    } == {
      "headset",
      "battery_level",
      "battery_status",
      "mixdial_game",
      "mixdial_chat",
    }

  def test_compile_if4(self, profile):
    parser = make_parser(profile, 4)

    actions = {
      rule.report: tuple(action.name for action in rule.actions)
      for rule in parser._rules
      if rule.actions
    }

    assert actions == {
      (0x00, 0x02, 0x00, 0x00): ("button_single",),
      (0x00, 0x04, 0x00, 0x00): ("button_double",),
      (0x00, 0x01, 0x00, 0x00): ("button_triple",),
    }

  def test_compile_if4_release_report(self, profile):
    parser = make_parser(profile, 4)

    release_rules = [
      rule
      for rule in parser._rules
      if rule.report == (0x00, 0x00, 0x00, 0x00)
    ]

    assert len(release_rules) == 1
    assert release_rules[0].actions == ()
    assert release_rules[0].fields == ()

  def test_compile_if5(self, profile):
    parser = make_parser(profile, 5)

    rules = {
      rule.prefix: rule
      for rule in parser._rules
    }

    assert set(rules) == {
      (0x45,),
      (0xb7,),
      (0xb9,),
      (0xbb,),
    }

    assert {
      field.name
      for field in rules[(0x45,)].fields
    } == {
      "mixdial_game",
      "mixdial_chat",
    }

    assert {
      field.name
      for field in rules[(0xb7,)].fields
    } == {"battery_level"}

    assert {
      field.name
      for field in rules[(0xb9,)].fields
    } == {"headset"}

    assert {
      field.name
      for field in rules[(0xbb,)].fields
    } == {"usb"}

  def test_if3_requires_response_config(self, profile):
    interface = profile.interfaces[3]

    with pytest.raises(
      ValueError,
      match="Response configuration is required",
    ):
      HIDParser(interface)

  def test_if3_requires_response_prefix(self, profile):
    interface = profile.interfaces[3]

    with pytest.raises(
      ValueError,
      match="Response prefix is required",
    ):
      HIDParser(interface, {})

  # END TestCompilation


class TestReportMatching:
  def test_unknown_if3_report_returns_empty_result(self, profile):
    parser = make_parser(profile, 3)

    assert parser.parse(bytes([0xaa] + [0] * 63)) == {}

  def test_unknown_if5_report_returns_empty_result(self, profile):
    parser = make_parser(profile, 5)

    assert parser.parse(bytes([0xaa] + [0] * 63)) == {}

  def test_if5_rejects_report_shorter_than_minimum_length(self, profile):
    parser = make_parser(profile, 5)

    assert parser.parse(bytes([0x45, 50])) == {}

  def test_if4_requires_exact_report(self, profile):
    parser = make_parser(profile, 4)

    assert parser.parse(bytes([0x00, 0x02, 0x00])) == {}

    assert parser.parse(
      bytes([0x00, 0x02, 0x00, 0x00, 0x00])
    ) == {}

  # END TestReportMatching


class TestIF3StatusParsing:
  def test_if3_parses_multiple_status_fields(self, profile):
    parser = make_parser(profile, 3)

    report = make_report(
      0xb0,
      {
        1: 0x03,
        2: 0x04,
        3: 0x01,
        4: 60,
        5: 40,
      },
    )

    assert parser.parse(report) == {
      "status": {
        "headset": "on",
        "battery_level": 4,
        "battery_status": "charging",
        "mixdial_game": 60,
        "mixdial_chat": 40,
      }
    }

  @pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
      (0x03, "on"),
      (0x01, "off"),
    ],
  )
  def test_if3_parses_headset_values(
    self,
    profile,
    raw_value,
    expected,
  ):
    parser = make_parser(profile, 3)

    report = make_report(0xb0, {1: raw_value})

    assert parser.parse(report)["status"]["headset"] == expected

  @pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
      (0x01, "charging"),
      (0x02, "fully_charged"),
      (0x03, "not_charging"),
    ],
  )
  def test_if3_parses_battery_status_values(
    self,
    profile,
    raw_value,
    expected,
  ):
    parser = make_parser(profile, 3)

    report = make_report(0xb0, {3: raw_value})

    assert parser.parse(report)["status"]["battery_status"] == expected

  def test_if3_invalid_field_does_not_discard_valid_fields(
    self,
    profile,
    caplog,
  ):
    parser = make_parser(profile, 3)

    report = make_report(
      0xb0,
      {
        1: 0x03,
        2: 0x05,
        3: 0x01,
        4: 60,
        5: 40,
      },
    )

    with caplog.at_level(logging.WARNING):
      result = parser.parse(report)

    assert result == {
      "status": {
        "headset": "on",
        "battery_status": "charging",
        "mixdial_game": 60,
        "mixdial_chat": 40,
      }
    }

    assert "interface=3" in caplog.text
    assert "field=battery_level" in caplog.text
    assert "raw=0x05" in caplog.text
    assert "report_prefix=B0" in caplog.text

  def test_if3_unknown_semantic_value_is_ignored(self, profile, caplog):
    parser = make_parser(profile, 3)

    report = make_report(
      0xb0,
      {
        1: 0x03,
        3: 0xff,
      },
    )

    with caplog.at_level(logging.WARNING):
      result = parser.parse(report)

    assert result == {
      "status": {
        "headset": "on",
        "mixdial_game": 0,
        "mixdial_chat": 0,
      }
    }

    assert "field=battery_status" in caplog.text
    assert "raw=0xFF" in caplog.text

  def test_if3_report_with_only_invalid_fields_returns_mixdial(self, profile):
    parser = make_parser(profile, 3)

    report = make_report(
      0xb0,
      {
        1: 0xff,
        2: 0xff,
        3: 0xff,
      },
    )

    assert parser.parse(report) == {
      "status": {
        "mixdial_game": 0,
        "mixdial_chat": 0,
      }
    }

  # END TestIF3StatusParsing


class TestIF4ActionParsing:
  @pytest.mark.parametrize(
    ("report", "action"),
    [
      ([0x00, 0x02, 0x00, 0x00], "button_single"),
      ([0x00, 0x04, 0x00, 0x00], "button_double"),
      ([0x00, 0x01, 0x00, 0x00], "button_triple"),
    ],
  )
  def test_if4_parses_button_actions(
    self,
    profile,
    report,
    action,
  ):
    parser = make_parser(profile, 4)

    assert parser.parse(bytes(report)) == {
      "actions": [action]
    }

  def test_if4_release_report_generates_no_action(self, profile):
    parser = make_parser(profile, 4)

    assert parser.parse(
      bytes([0x00, 0x00, 0x00, 0x00])
    ) == {}

  def test_if4_unknown_report_generates_no_action(self, profile):
    parser = make_parser(profile, 4)

    assert parser.parse(
      bytes([0x00, 0x08, 0x00, 0x00])
    ) == {}

  # END TestIF4ActionParsing


class TestIF5StatusParsing:
  def test_if5_parses_mixdial_fields(self, profile):
    parser = make_parser(profile, 5)

    report = make_report(
      0x45,
      {
        1: 75,
        2: 25,
      },
    )

    assert parser.parse(report) == {
      "status": {
        "mixdial_game": 75,
        "mixdial_chat": 25,
      }
    }

  def test_if5_parses_battery_level(self, profile):
    parser = make_parser(profile, 5)

    report = make_report(
      0xb7,
      {
        1: 0x04,
      },
    )

    assert parser.parse(report) == {
      "status": {
        "battery_level": 4,
      }
    }

  @pytest.mark.parametrize(
    ("prefix", "raw_value", "expected"),
    [
      (0xb9, 0x01, "off"),
      (0xb9, 0x03, "on"),
      (0xbb, 0x01, "connected"),
      (0xbb, 0x03, "disconnected"),
    ],
  )
  def test_if5_parses_semantic_values(
    self,
    profile,
    prefix,
    raw_value,
    expected,
  ):
    parser = make_parser(profile, 5)

    report = make_report(
      prefix,
      {
        1: raw_value,
      },
    )

    field_name = "headset" if prefix == 0xb9 else "usb"

    assert parser.parse(report) == {
      "status": {
        field_name: expected,
      }
    }

  def test_if5_invalid_field_preserves_other_fields(self, profile, caplog):
    parser = make_parser(profile, 5)

    report = make_report(
      0x45,
      {
        1: 101,
        2: 25,
      },
    )

    with caplog.at_level(logging.WARNING):
      result = parser.parse(report)

    assert result == {
      "status": {
        "mixdial_chat": 25,
      }
    }

    assert "field=mixdial_game" in caplog.text
    assert "raw=0x65" in caplog.text

  # END TestIF5StatusParsing


class TestAmbiguousActions:
  def test_ambiguous_actions_are_discarded(self, caplog):
    interface = InterfaceConfig(
      interface_id=4,
      mode="passive",
      config=MappingProxyType({}),
      definitions=MappingProxyType({
        "button": MappingProxyType({
          "first": MappingProxyType({
            "report": (0x00, 0x02, 0x00, 0x00),
            "action": "single",
          }),
          "second": MappingProxyType({
            "report": (0x00, 0x02, 0x00, 0x00),
            "action": "double",
          }),
        }),
      }),
    )

    parser = HIDParser(interface)

    with caplog.at_level(logging.WARNING):
      result = parser.parse(
        bytes([0x00, 0x02, 0x00, 0x00])
      )

    assert result == {}
    assert "Ambiguous actions" in caplog.text
    assert "button_first" in caplog.text
    assert "button_second" in caplog.text

  # END TestAmbiguousActions


class TestImmutability:
  def test_compiled_rules_are_immutable(self, profile):
    parser = make_parser(profile, 3)

    assert isinstance(parser._rules, tuple)

    with pytest.raises(AttributeError):
      parser._rules[0].prefix = (0xaa,)

  def test_compiled_field_values_are_immutable(self, profile):
    parser = make_parser(profile, 3)

    field = next(
      field
      for field in parser._rules[0].fields
      if field.name == "headset"
    )

    with pytest.raises(TypeError):
      field.values["unknown"] = 0xff

  # END TestImmutability
