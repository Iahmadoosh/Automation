"""Selector overrides for the uOttawa Experiential Learning / IndEX portals.

Every step tries the override here first (when non-empty), then built-in role/label/text
fallbacks, so most keys default to "". Calibrate with `python coop.py inspect` and set
keys in data/coop_selectors.json (see the template next to it).
"""

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path

from core.errors import ConfigError


@dataclass(frozen=True)
class CoopSelectors:
    # Experiential Learning dashboard
    index_card: str = ""
    # IndEX login view
    index_user_type: str = ""
    index_username: str = ""
    index_login_button: str = ""
    index_login_error_regex: str = r"invalid|incorrect|failed|not recognized|erreur"
    # IndEX job board
    board_ready_regex: str = r"filter\s*by"
    filter_button: str = ""
    hide_applied_checkbox: str = ""
    # Posting -> application form
    apply_button: str = (
        'button:text-matches("^(apply|continue to application|proceed to application)", "i"), '
        'a:text-matches("^(apply|continue to application|proceed to application)", "i"), '
        'input[type="submit"][value^="Apply" i], input[type="button"][value^="Apply" i]'
    )
    # Empty = auto-detect: the <form> holding the most inputs on the page.
    application_form: str = ""
    next_step_button: str = (
        'button:text-matches("^(next|continue)$", "i"), '
        'input[type="submit"][value^="Next" i], input[type="submit"][value^="Continue" i]'
    )
    submit_button: str = (
        'button:text-matches("^submit", "i"), input[type="submit"][value^="Submit" i], '
        'button:text-matches("^soumettre", "i")'
    )
    already_applied_regex: str = r"already applied|application (was )?submitted|vous avez déjà postulé"
    # Hosts that mean the session bounced back to SSO.
    login_hosts: tuple[str, ...] = ("login.microsoftonline.com", "login.live.com")


def load_selectors(path: Path) -> CoopSelectors:
    defaults = CoopSelectors()
    if not path.exists():
        return defaults
    with open(path, encoding="utf-8") as f:
        overrides = json.load(f)
    known = {f.name for f in fields(CoopSelectors)}
    unknown = set(overrides) - known - {"_comment"}
    if unknown:
        raise ConfigError(f"Unknown selector keys in {path}: {', '.join(sorted(unknown))}")
    overrides.pop("_comment", None)
    if "login_hosts" in overrides:
        overrides["login_hosts"] = tuple(overrides["login_hosts"])
    return replace(defaults, **overrides)
