"""Environment-driven settings. Secrets and personal URLs resolve from .env only."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from core.errors import ConfigError

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_path(name: str, default: Path | None) -> Path | None:
    raw = _env_str(name)
    if not raw:
        return default
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _env_number(name: str, default: float, cast: type = float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


def _env_regex(name: str, default: str) -> re.Pattern[str]:
    raw = _env_str(name) or default
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error as exc:
        raise ConfigError(f"{name} is not a valid regular expression: {exc}") from exc


def _https(name: str, url: str) -> str:
    if not url.startswith("https://"):
        raise ConfigError(f"{name} must be an https:// URL, got {url[:40]!r}.")
    return url.rstrip("/")


@dataclass(frozen=True)
class CoopSettings:
    base_url: str
    dashboard_url: str
    email: str
    index_username: str
    password: str = field(repr=False)
    auth_path: Path
    profile_path: Path
    resume_candidates: tuple[Path, ...]
    resume_path: Path | None
    transcript_path: Path | None
    resume_doc_name: str
    transcript_doc_name: str
    cover_letter_doc_name: str
    selectors_path: Path
    log_dir: Path
    applied_log_path: Path
    term: str
    faculty: str
    program: str
    hide_applied: bool
    local_regex: re.Pattern[str]
    max_applications: int
    min_delay: float
    max_delay: float
    seconds_between_applications: float
    headless: bool
    llm_endpoint: str
    llm_model: str
    llm_answer_timeout: float


def load_coop_settings(env_file: Path | None = None) -> CoopSettings:
    load_dotenv(env_file or BASE_DIR / ".env")

    raw_base = _env_str("COOP_BASE_URL")
    if not raw_base:
        raise ConfigError("COOP_BASE_URL is not set. Copy .env.example to .env and fill it in.")
    base_url = _https("COOP_BASE_URL", raw_base)
    dashboard_url = _https("COOP_DASHBOARD_URL", _env_str("COOP_DASHBOARD_URL") or f"{base_url}/dashboard")

    email = _env_str("UOTTAWA_EMAIL")
    if email and not email.lower().endswith("@uottawa.ca"):
        raise ConfigError("UOTTAWA_EMAIL must be your @uottawa.ca address.")

    min_delay = _env_number("COOP_MIN_DELAY", 0.8)
    max_delay = _env_number("COOP_MAX_DELAY", 2.5)
    if min_delay < 0 or max_delay < min_delay:
        raise ConfigError("Require 0 <= COOP_MIN_DELAY <= COOP_MAX_DELAY.")

    resume_override = _env_path("COOP_RESUME_PATH", None)
    resume_candidates = tuple(
        p
        for p in (
            resume_override,
            BASE_DIR / "assets" / "resume.pdf",
            BASE_DIR / "data" / "resume.pdf",
            BASE_DIR / "resume.pdf",
            BASE_DIR / "assets" / "resume.txt",
            BASE_DIR / "data" / "resume.txt",
            BASE_DIR / "resume.txt",
        )
        if p
    )
    resume_path = next((p for p in resume_candidates if p.is_file() and p.suffix == ".pdf"), None)

    return CoopSettings(
        base_url=base_url,
        dashboard_url=dashboard_url,
        email=email,
        index_username=_env_str("INDEX_USERNAME") or email,
        password=os.getenv("UOTTAWA_PASSWORD", ""),
        auth_path=_env_path("COOP_AUTH_STATE", BASE_DIR / "data" / "coop_auth.json"),
        profile_path=_env_path("PROFILE_PATH", BASE_DIR / "data" / "profile.json"),
        resume_candidates=resume_candidates,
        resume_path=resume_path,
        transcript_path=_env_path("COOP_TRANSCRIPT_PATH", None),
        resume_doc_name=_env_str("COOP_RESUME_DOC_NAME"),
        transcript_doc_name=_env_str("COOP_TRANSCRIPT_DOC_NAME"),
        cover_letter_doc_name=_env_str("COOP_COVER_LETTER_DOC_NAME"),
        selectors_path=_env_path("COOP_SELECTORS_PATH", BASE_DIR / "data" / "coop_selectors.json"),
        log_dir=_env_path("COOP_LOG_DIR", BASE_DIR / "logs" / "coop"),
        applied_log_path=_env_path("COOP_APPLIED_LOG", BASE_DIR / "data" / "coop_applied.json"),
        term=_env_str("COOP_TERM", "2027, Winter"),
        faculty=_env_str("COOP_FACULTY", "Engineering"),
        program=_env_str("COOP_PROGRAM", "Computer Science"),
        hide_applied=_env_bool("COOP_HIDE_APPLIED", True),
        local_regex=_env_regex("COOP_LOCAL_REGEX", r"\bottawa\b"),
        max_applications=int(_env_number("COOP_MAX_APPLICATIONS", 5, int)),
        min_delay=min_delay,
        max_delay=max_delay,
        seconds_between_applications=_env_number("COOP_SECONDS_BETWEEN_APPLICATIONS", 90),
        headless=_env_bool("COOP_HEADLESS", False),
        llm_endpoint=_env_str("LLM_ENDPOINT", "http://localhost:1234/api/v1/chat"),
        llm_model=_env_str("LLM_MODEL", "qwen2.5-coder-7b-instruct"),
        llm_answer_timeout=_env_number("LLM_ANSWER_TIMEOUT", 120),
    )


def load_profile(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"{path} not found. Copy data/profile.template.json to data/profile.json.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)
