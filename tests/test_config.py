from pathlib import Path

import pytest
from pydantic import ValidationError

from app.limits import MAX_PAYLOAD_KB
from app.traffic_generator.config import Config, load_config

EXAMPLE = Path(__file__).parent.parent / "configs" / "example.yaml"


def make(**sections: dict) -> Config:
    return Config.model_validate({"target": {"base_url": "http://127.0.0.1:8000"}, **sections})


def test_example_config_is_valid() -> None:
    config = load_config(EXAMPLE)
    assert config.profile.type == "web_browsing"
    assert config.metadata.train_id == "train-poc-001"


def test_defaults_are_conservative() -> None:
    run = make().run
    assert run.concurrent_users <= 2
    assert run.max_requests_per_second <= 5
    assert run.duration_seconds <= 60


def test_target_is_required() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({})


@pytest.mark.parametrize(
    "base_url", ["ftp://host", "not a url", "http://", "http://host/?q=1", "http://host/#frag"]
)
def test_invalid_base_url(base_url: str) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"target": {"base_url": base_url}})


def test_base_url_trailing_slash_is_removed() -> None:
    config = Config.model_validate({"target": {"base_url": "http://host:8000/"}})
    assert config.target.base_url == "http://host:8000"


@pytest.mark.parametrize(
    "run",
    [
        {"concurrent_users": 0},
        {"concurrent_users": 51},
        {"max_requests_per_second": 0},
        {"max_requests_per_second": 1000},
        {"max_mbps": 5000},
        {"duration_seconds": 0},
        {"duration_seconds": 7200},
        {"request_timeout_seconds": 0},
    ],
)
def test_run_limits_are_bounded(run: dict) -> None:
    with pytest.raises(ValidationError):
        make(run=run)


def test_min_greater_than_max_is_rejected() -> None:
    with pytest.raises(ValidationError, match="payload_min_kb"):
        make(profile={"payload_min_kb": 500, "payload_max_kb": 50})
    with pytest.raises(ValidationError, match="think_time_min_ms"):
        make(profile={"think_time_min_ms": 2000, "think_time_max_ms": 100})


def test_payload_above_server_cap_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make(profile={"payload_max_kb": MAX_PAYLOAD_KB + 1})


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make(run={"concurrent_user": 3})


def test_unknown_profile_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make(profile={"type": "torrent"})


def test_cli_overrides_are_validated(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text('target:\n  base_url: "http://127.0.0.1:8000"\nrun:\n  duration_seconds: 30\n')
    assert load_config(path, {"run": {"duration_seconds": 5}}, environ={}).run.duration_seconds == 5
    with pytest.raises(ValidationError):
        load_config(path, {"run": {"concurrent_users": 1000}}, environ={})


# --- Schedule (D-27) ---

def test_runner_config_is_valid_and_scheduled() -> None:
    config = load_config(EXAMPLE.parent / "runner.yaml", environ={})
    assert config.schedule is not None
    assert config.schedule.interval_seconds == 900
    assert config.run.duration_seconds == 60
    assert config.publish is not None


def test_schedule_must_leave_room_for_the_run() -> None:
    # 60 s jitter + 50 s run + 10 s timeout = 120 s > 100 s interval
    with pytest.raises(ValidationError, match="never overlap"):
        make(run={"duration_seconds": 50}, schedule={"interval_seconds": 100, "jitter_seconds": 60})


def test_schedule_duty_cycle_is_capped() -> None:
    with pytest.raises(ValidationError, match="at most 50%"):
        make(run={"duration_seconds": 80}, schedule={"interval_seconds": 120, "jitter_seconds": 0})
    make(run={"duration_seconds": 60}, schedule={"interval_seconds": 120, "jitter_seconds": 0})


def test_schedule_interval_has_a_floor() -> None:
    with pytest.raises(ValidationError):
        make(schedule={"interval_seconds": 10})


# --- Environment overrides (D-28) ---

def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "c.yaml"
    path.write_text(text)
    return path


def test_environment_overrides_yaml_and_cli_overrides_environment(tmp_path: Path) -> None:
    path = write(tmp_path, 'target:\n  base_url: "http://127.0.0.1:8000"\nmetadata:\n  train_id: "yaml-train"\n')
    env = {
        "QOE_TRAIN_ID": "train-101",
        "QOE_DEVICE_ID": "router-101-a",
        "QOE_TARGET_URL": "http://endpoint.example:8000",
        "QOE_RUN_DURATION_SECONDS": "45",
    }
    config = load_config(path, {"run": {"duration_seconds": 20}}, environ=env)
    assert config.metadata.train_id == "train-101"
    assert config.metadata.device_id == "router-101-a"
    assert config.target.base_url == "http://endpoint.example:8000"
    assert config.run.duration_seconds == 20  # CLI wins over the environment


def test_environment_values_are_validated(tmp_path: Path) -> None:
    path = write(tmp_path, 'target:\n  base_url: "http://127.0.0.1:8000"\n')
    with pytest.raises(ValidationError):
        load_config(path, environ={"QOE_CONCURRENT_USERS": "500"})


def test_empty_environment_values_are_ignored(tmp_path: Path) -> None:
    path = write(tmp_path, 'target:\n  base_url: "http://127.0.0.1:8000"\nmetadata:\n  train_id: "yaml-train"\n')
    assert load_config(path, environ={"QOE_TRAIN_ID": "  "}).metadata.train_id == "yaml-train"


def test_schedule_interval_variable_enables_scheduling(tmp_path: Path) -> None:
    path = write(tmp_path, 'target:\n  base_url: "http://127.0.0.1:8000"\n')
    config = load_config(path, environ={"QOE_SCHEDULE_INTERVAL_SECONDS": "600"})
    assert config.schedule is not None and config.schedule.interval_seconds == 600


def test_rabbitmq_variable_never_enables_publishing_by_itself(tmp_path: Path) -> None:
    plain = write(tmp_path, 'target:\n  base_url: "http://127.0.0.1:8000"\n')
    assert load_config(plain, environ={"QOE_RABBITMQ_URL": "amqp://u:p@broker:5672/"}).publish is None
    publishing = write(tmp_path, 'target:\n  base_url: "http://127.0.0.1:8000"\npublish: {}\n')
    config = load_config(publishing, environ={"QOE_RABBITMQ_URL": "amqp://u:p@broker:5672/"})
    assert config.publish is not None and config.publish.rabbitmq_url == "amqp://u:p@broker:5672/"


def test_device_id_defaults_to_hostname(tmp_path: Path) -> None:
    import socket

    path = write(tmp_path, 'target:\n  base_url: "http://127.0.0.1:8000"\n')
    assert load_config(path, environ={}).metadata.device_id == socket.gethostname()
