from pathlib import Path

import pytest

from adapters.coop_selectors import CoopSelectors, load_selectors
from core.config import load_coop_settings
from core.errors import ConfigError

ENV_VARS = (
    "COOP_BASE_URL",
    "COOP_DASHBOARD_URL",
    "UOTTAWA_EMAIL",
    "UOTTAWA_PASSWORD",
    "INDEX_USERNAME",
    "COOP_TERM",
    "COOP_FACULTY",
    "COOP_PROGRAM",
    "COOP_MIN_DELAY",
    "COOP_MAX_DELAY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def write_env(tmp_path: Path, body: str) -> Path:
    env = tmp_path / ".env"
    env.write_text(body, encoding="utf-8")
    return env


def test_missing_base_url_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="COOP_BASE_URL"):
        load_coop_settings(write_env(tmp_path, ""))


def test_rejects_non_https(tmp_path):
    with pytest.raises(ConfigError, match="https"):
        load_coop_settings(write_env(tmp_path, "COOP_BASE_URL=http://coop.example\n"))


def test_rejects_non_uottawa_email(tmp_path):
    env = write_env(tmp_path, "COOP_BASE_URL=https://coop.example\nUOTTAWA_EMAIL=me@gmail.com\n")
    with pytest.raises(ConfigError, match="uottawa"):
        load_coop_settings(env)


def test_loads_settings_from_env(tmp_path):
    env = write_env(tmp_path, "COOP_BASE_URL=https://coop.example/\nUOTTAWA_EMAIL=jdoe@uottawa.ca\n")
    settings = load_coop_settings(env)
    assert settings.base_url == "https://coop.example"
    assert settings.dashboard_url == "https://coop.example/dashboard"
    assert settings.email == settings.index_username == "jdoe@uottawa.ca"
    assert (settings.term, settings.faculty, settings.program) == (
        "2027, Winter",
        "Engineering",
        "Computer Science",
    )
    assert settings.local_regex.search("Ottawa, ON")


def test_password_never_in_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("UOTTAWA_PASSWORD", "hunter2-secret")
    settings = load_coop_settings(write_env(tmp_path, "COOP_BASE_URL=https://coop.example\n"))
    assert settings.password == "hunter2-secret"
    assert "hunter2-secret" not in repr(settings)


def test_selector_overrides(tmp_path):
    path = tmp_path / "sel.json"
    path.write_text('{"_comment": "x", "index_card": "a.job", "login_hosts": ["sso.example"]}')
    sel = load_selectors(path)
    assert sel.index_card == "a.job" and sel.login_hosts == ("sso.example",)
    assert sel.submit_button == CoopSelectors().submit_button


def test_unknown_selector_key_rejected(tmp_path):
    path = tmp_path / "sel.json"
    path.write_text('{"index_crad": "a"}')
    with pytest.raises(ConfigError, match="index_crad"):
        load_selectors(path)
