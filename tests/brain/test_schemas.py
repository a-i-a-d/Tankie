"""Offline unit tests for the brain scaffolding (issue #72, Phase 1 T1).

Run from the repo root:  ``pytest tests/brain/ -q``

No tank, no Pi, no LocalAI, no network — pure data + config. These tests
lock the contract (schemas) and the single-config-source behaviour
(precedence + never-crash) that every later task depends on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml
from pydantic import ValidationError

from brain.config import BrainConfig, load_config
from brain.schemas import (
    Action,
    BridgeResult,
    DataLogRecord,
    Detection,
    ReflexResult,
    TankState,
    VLMResult,
)


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

def test_action_round_trip_json():
    a = Action(intent="go_to", cmd="drive", speed=30, steer=-15, reason="to the door")
    s = a.model_dump_json()
    b = Action.model_validate_json(s)
    assert b == a
    assert b.speed == 30 and b.steer == -15 and b.intent == "go_to"


def test_action_defaults():
    a = Action(intent="stop", cmd="stop")
    assert a.speed == 0 and a.steer == 0
    assert a.pan is None and a.tilt is None
    assert a.say == "" and a.reason == ""


@pytest.mark.parametrize("intent", ["fly", "goto", ""])
def test_action_rejects_bad_intent(intent):
    with pytest.raises(ValidationError):
        Action(intent=intent, cmd="stop")


@pytest.mark.parametrize("cmd", ["move", "driv", ""])
def test_action_rejects_bad_cmd(cmd):
    with pytest.raises(ValidationError):
        Action(intent="stop", cmd=cmd)


@pytest.mark.parametrize("speed", [-101, 101, 1000, -1000])
def test_action_rejects_out_of_range_speed(speed):
    with pytest.raises(ValidationError):
        Action(intent="go_to", cmd="drive", speed=speed)


@pytest.mark.parametrize("steer", [-101, 101])
def test_action_rejects_out_of_range_steer(steer):
    with pytest.raises(ValidationError):
        Action(intent="go_to", cmd="drive", steer=steer)


@pytest.mark.parametrize("pan", [-1, 181, 999])
def test_action_rejects_out_of_range_pan(pan):
    with pytest.raises(ValidationError):
        Action(intent="go_to", cmd="pan", pan=pan)


@pytest.mark.parametrize("tilt", [-1, 181])
def test_action_rejects_out_of_range_tilt(tilt):
    with pytest.raises(ValidationError):
        Action(intent="go_to", cmd="tilt", tilt=tilt)


def test_action_rejects_extra_key():
    with pytest.raises(ValidationError):
        Action(intent="stop", cmd="stop", bogus=1)


def test_action_clamp_bounds_to_auto_profile():
    # over-limit input: built without validation to model a raw LLM output
    # that the clamp must bring back into the AUTO_PROFILE range.
    a = Action.model_construct(intent="go_to", cmd="drive", speed=90, steer=-200)
    c = a.clamp(40, 120)
    assert c["speed"] == 40
    assert c["steer"] == -120
    # other fields preserved
    assert c["intent"] == "go_to" and c["cmd"] == "drive"


def test_action_clamp_negative_bound():
    a = Action.model_construct(intent="go_to", cmd="drive", speed=-90, steer=200)
    c = a.clamp(40, 120)
    assert c["speed"] == -40
    assert c["steer"] == 120


def test_action_clamp_within_limits_noop():
    a = Action(intent="go_to", cmd="drive", speed=10, steer=-5)
    c = a.clamp(40, 120)
    assert c["speed"] == 10 and c["steer"] == -5


def test_action_clamp_preserves_other_fields():
    a = Action(intent="go_to", cmd="drive", speed=90, steer=0, say="hi", reason="why")
    c = a.clamp(40, 120)
    assert c["say"] == "hi" and c["reason"] == "why" and c["intent"] == "go_to" and c["cmd"] == "drive"


# ---------------------------------------------------------------------------
# Detection / sub-models
# ---------------------------------------------------------------------------

def test_detection_valid():
    d = Detection(class_name="cat", x=10.0, y=20.0, w=30.0, h=40.0, confidence=0.9)
    assert d.class_name == "cat" and d.confidence == 0.9


def test_detection_rejects_bad_confidence():
    with pytest.raises(ValidationError):
        Detection(class_name="cat", x=0, y=0, w=1, h=1, confidence=1.5)


def test_vlm_result_shape():
    v = VLMResult(model="qwen3.8-4b-q4", raw="go", ok=True)
    assert v.action is None and v.error is None and v.ok is True


def test_reflex_result_shape():
    r = ReflexResult(cmd="drive", speed=20, steer=0, preempted=True, reason="obstacle")
    assert r.preempted is True and r.reason == "obstacle"


def test_bridge_result_shape():
    b = BridgeResult(cmd="drive", speed=20, steer=0, ok=True)
    assert b.ok is True and b.error is None


def test_tank_state_defaults():
    s = TankState()
    assert s.link_up is False and s.battery_v is None and s.watchdog_fired is False


# ---------------------------------------------------------------------------
# DataLogRecord — one full cycle serialises to one valid JSON line
# ---------------------------------------------------------------------------

def _full_cycle() -> DataLogRecord:
    return DataLogRecord(
        ts=datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc),
        frame_id="abc123",
        task="find the red ball",
        vlm=VLMResult(
            model="qwen3.8-4b-q4",
            raw="drive forward",
            action=Action(intent="go_to", cmd="drive", speed=30, steer=0),
            ok=True,
        ),
        detections=[Detection(class_name="ball", x=100, y=100, w=20, h=20, confidence=0.8)],
        reflex=ReflexResult(cmd="drive", speed=30, steer=0, preempted=False),
        bridge=BridgeResult(cmd="drive", speed=30, steer=0, ok=True),
        state=TankState(link_up=True, battery_v=7.4, net_mode="sta", net_ip="192.0.2.42"),
        watchdog=False,
    )


def test_data_log_record_full_cycle_serialises():
    rec = _full_cycle()
    line = rec.model_dump_json()
    # one valid JSON line
    assert line.count("\n") == 0
    parsed = DataLogRecord.model_validate_json(line)
    assert parsed == rec
    assert parsed.vlm.action.speed == 30
    assert parsed.detections[0].class_name == "ball"


def test_data_log_record_minimal():
    rec = DataLogRecord(
        ts=datetime(2026, 10, 10, tzinfo=timezone.utc),
        frame_id="x",
        task="explore",
        reflex=ReflexResult(cmd="stop"),
        bridge=BridgeResult(cmd="stop"),
        state=TankState(),
    )
    assert rec.vlm is None and rec.detections == [] and rec.depth is None


def test_data_log_record_rejects_extra_key():
    with pytest.raises(ValidationError):
        DataLogRecord(
            ts=datetime(2026, 10, 10, tzinfo=timezone.utc),
            frame_id="x",
            task="t",
            reflex=ReflexResult(cmd="stop"),
            bridge=BridgeResult(cmd="stop"),
            state=TankState(),
            bogus=1,
        )


# ---------------------------------------------------------------------------
# config — single source + precedence + never-crash
# ---------------------------------------------------------------------------

def test_load_config_missing_file_defaults():
    c = load_config(path="/nonexistent/brain.yaml", env={})
    assert c == BrainConfig()
    assert c.localai_base_url == "http://localai.local:8080/v1"
    assert c.vlm_model == "qwen3.8-4b-q4"


def test_load_config_env_override_wins(tmp_path):
    y = tmp_path / "brain.yaml"
    y.write_text("vlm_model: from_yaml\nlocalai_base_url: http://yaml:1/v1\n")
    c = load_config(
        path=y,
        env={"TANKIE_VLM_MODEL": "from_env", "TANKIE_LOCALAI_BASE_URL": "http://env:2/v1"},
    )
    assert c.vlm_model == "from_env"          # env beats yaml
    assert c.localai_base_url == "http://env:2/v1"


def test_load_config_yaml_beats_defaults(tmp_path):
    y = tmp_path / "brain.yaml"
    y.write_text("vlm_model: from_yaml\nreflex_hz: 20\n")
    c = load_config(path=y, env={})
    assert c.vlm_model == "from_yaml"          # yaml beats default
    assert c.reflex_hz == 20
    # untouched keys stay at defaults
    assert c.localai_base_url == "http://localai.local:8080/v1"


def test_load_config_corrupt_yaml_defaults(tmp_path):
    y = tmp_path / "brain.yaml"
    y.write_text("a: b: c: d\n")               # raises YAMLError on parse
    c = load_config(path=y, env={})
    assert c == BrainConfig()                  # no exception, defaults


def test_load_config_non_dict_yaml_defaults(tmp_path):
    y = tmp_path / "brain.yaml"
    y.write_text("- 1\n- 2\n- 3\n")            # parses, but is a list, not a dict
    c = load_config(path=y, env={})
    assert c == BrainConfig()


def test_load_config_nested_sub_dict(tmp_path):
    y = tmp_path / "brain.yaml"
    y.write_text("auto_profile:\n  enabled: false\n  max_speed: 10\ntimeouts:\n  chat_s: 9.5\n")
    c = load_config(path=y, env={})
    assert c.auto_profile.enabled is False
    assert c.auto_profile.max_speed == 10
    assert c.auto_profile.max_steer == 120     # untouched default
    assert c.timeouts.chat_s == 9.5


def test_load_config_ignores_unknown_scalar(tmp_path):
    y = tmp_path / "brain.yaml"
    y.write_text("vlm_model: m\nnot_a_real_key: 123\n")
    c = load_config(path=y, env={})
    assert c.vlm_model == "m"
    # the unknown key is simply not in the dataclass
    assert not hasattr(c, "not_a_real_key")


def test_brain_yaml_in_repo_loads():
    # the committed conf/brain.yaml must be loadable and sane
    c = load_config(env={})
    assert c.reflex_hz == 10
    assert c.deliberate_hz == 1
    assert c.auto_profile.max_speed == 40
    assert c.auto_profile.max_steer == 120
    assert c.detection_model == "TBD"
    # A4: the code default must be the dnsmasq hostname, not a literal IP
    assert BrainConfig().localai_base_url == "http://localai.local:8080/v1"


def test_brain_yaml_is_valid_yaml():
    import brain.config as cfg
    path = cfg.default_config_path()
    with open(path) as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict)
    assert "localai_base_url" in data
