"""Daily job-hunt summary built from the run logs, for sending to your phone.

Reads:
  logs/coop/run-<mode>-YYYYMMDD-HHMMSS.json   written by `coop.py run`
  logs/outreach/drafts-YYYYMMDD.json          referral drafts (written by the outreach tool)

Draft file format: a JSON list of objects with name, company, profile_url, message
(and optionally headline / job_title).
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

_RUN_FILE = re.compile(r"^run-(?P<mode>[a-z]+)-(?P<day>\d{8})-\d{6}\.json$")
_DEADLINE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d")


@dataclass
class Draft:
    name: str
    company: str
    profile_url: str
    message: str
    headline: str = ""
    job_title: str = ""


@dataclass
class DailySummary:
    day: date
    runs: int = 0
    cvs_made: int = 0
    statuses: Counter = field(default_factory=Counter)
    submitted: list[str] = field(default_factory=list)
    needs_you: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    deadlines_soon: list[str] = field(default_factory=list)
    drafts: list[Draft] = field(default_factory=list)


def _parse_deadline(text: str) -> date | None:
    text = re.sub(r"\s+", " ", text).strip()
    candidates = [text, text.split(" at ")[0], text.split(" ")[0]]
    for candidate in candidates:
        for fmt in _DEADLINE_FORMATS:
            try:
                return datetime.strptime(candidate.strip(" ,"), fmt).date()
            except ValueError:
                continue
    return None


def _job(entry: dict) -> str:
    return f"{entry.get('title') or 'Unknown'} @ {entry.get('company') or '?'}"


def load_drafts(path: Path) -> list[Draft]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Draft(
            name=str(d.get("name", "")),
            company=str(d.get("company", "")),
            profile_url=str(d.get("profile_url", "")),
            message=str(d.get("message", "")),
            headline=str(d.get("headline", "")),
            job_title=str(d.get("job_title", "")),
        )
        for d in raw
        if d.get("message")
    ]


def build_summary(coop_log_dir: Path, outreach_dir: Path, day: date, deadline_days: int = 3) -> DailySummary:
    summary = DailySummary(day)
    stamp = day.strftime("%Y%m%d")
    seen_deadlines: set[str] = set()
    if coop_log_dir.exists():
        for path in sorted(coop_log_dir.glob(f"run-*-{stamp}-*.json")):
            match = _RUN_FILE.match(path.name)
            if not match:
                continue
            summary.runs += 1
            for entry in json.loads(path.read_text(encoding="utf-8")):
                status = str(entry.get("status", "unknown"))
                summary.statuses[status] += 1
                if entry.get("cv_path"):
                    summary.cvs_made += 1
                job = _job(entry)
                if status == "submitted":
                    summary.submitted.append(job)
                elif status == "error":
                    summary.errors.append(f"{job}: {entry.get('reason', '')}".strip(": "))
                elif status == "pdf_posting":
                    summary.needs_you.append(f"{job}: PDF posting, apply by its instructions")
                required = [m for m in entry.get("manual", []) if m.get("required")]
                if required and status in ("dry_run_complete", "declined_at_confirmation"):
                    fields = ", ".join(m.get("label", "?")[:40] for m in required[:3])
                    summary.needs_you.append(f"{job}: {fields}")
                deadline = _parse_deadline(str(entry.get("deadline", "")))
                if (
                    deadline
                    and status != "submitted"
                    and day <= deadline <= day + timedelta(days=deadline_days)
                    and job not in seen_deadlines
                ):
                    seen_deadlines.add(job)
                    summary.deadlines_soon.append(f"{job} (due {deadline:%b %d})")
    summary.drafts = load_drafts(outreach_dir / f"drafts-{stamp}.json")
    return summary


_STATUS_LABELS = (
    ("submitted", "Applied (submitted)"),
    ("dry_run_complete", "Filled, not submitted (dry run)"),
    ("declined_at_confirmation", "You declined at confirmation"),
    ("previewed", "Previewed"),
    ("already_applied", "Already applied"),
    ("no_application_form", "No application form"),
    ("pdf_posting", "PDF postings"),
    ("error", "Errors"),
)


def _bullets(items: list[str], limit: int = 8) -> list[str]:
    lines = [f"- {item}" for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"- ...and {len(items) - limit} more")
    return lines


def format_messages(summary: DailySummary) -> list[str]:
    """First message: the overview. Then one message per drafted referral note, ready to copy."""
    lines = [f"Job hunt summary - {summary.day:%a %b %d}", ""]
    if not summary.runs and not summary.drafts:
        lines.append("Nothing ran today.")
        return ["\n".join(lines)]

    lines.append(f"Applied: {summary.statuses['submitted']}")
    lines.append(f"Connections found: {len(summary.drafts)}")
    lines.append(f"Messages drafted: {len(summary.drafts)}")
    if summary.cvs_made:
        lines.append(f"Tailored CVs made: {summary.cvs_made}")
    if summary.runs:
        lines.append(f"Co-op runs: {summary.runs}")
        lines.extend(
            f"  {label}: {summary.statuses[key]}"
            for key, label in _STATUS_LABELS[1:]
            if summary.statuses[key]
        )
    for title, items in (
        ("Applied to", summary.submitted),
        ("Needs you", summary.needs_you),
        ("Deadlines soon", summary.deadlines_soon),
        ("Errors", summary.errors),
    ):
        if items:
            lines += ["", f"{title}:", *_bullets(items)]
    if summary.drafts:
        lines += ["", "Drafted messages follow, one per person. Copy, review, and send them yourself."]

    messages = ["\n".join(lines)]
    for i, draft in enumerate(summary.drafts, 1):
        head = f"Draft {i}/{len(summary.drafts)}: {draft.name}"
        if draft.headline:
            head += f" - {draft.headline}"
        about = f"{draft.company}" + (f" ({draft.job_title})" if draft.job_title else "")
        messages.append("\n".join(filter(None, [head, about, draft.profile_url, "", draft.message])))
    return messages
