import json
from datetime import date

from core.summary import build_summary, format_messages

DAY = date(2026, 11, 24)


def entry(title, status, **extra):
    return {"title": title, "company": "Acme", "status": status, "deadline": "", "manual": [], **extra}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_counts_only_that_days_runs(tmp_path):
    coop = tmp_path / "coop"
    write(coop / "run-live-20261124-090000.json", [entry("A", "submitted"), entry("B", "submitted")])
    write(
        coop / "run-dry-20261124-180000.json",
        [entry("C", "dry_run_complete"), entry("D", "error", reason="boom")],
    )
    write(coop / "run-live-20261123-090000.json", [entry("Old", "submitted")])
    write(coop / "inspect-20261124-090000.json", [entry("Ignored", "submitted")])

    s = build_summary(coop, tmp_path / "outreach", DAY)
    assert s.runs == 2
    assert s.statuses["submitted"] == 2
    assert s.submitted == ["A @ Acme", "B @ Acme"]
    assert s.errors == ["D @ Acme: boom"]


def test_needs_you_and_deadlines(tmp_path):
    coop = tmp_path / "coop"
    manual = [{"label": "GPA", "note": "fill", "required": True}, {"label": "Opt", "required": False}]
    write(
        coop / "run-dry-20261124-090000.json",
        [
            entry("Gpa job", "dry_run_complete", manual=manual, deadline="2026-11-26"),
            entry("Far job", "dry_run_complete", deadline="December 30, 2026"),
            entry("Done job", "submitted", deadline="2026-11-25"),
        ],
    )
    s = build_summary(coop, tmp_path / "outreach", DAY)
    assert s.needs_you == ["Gpa job @ Acme: GPA"]
    assert s.deadlines_soon == ["Gpa job @ Acme (due Nov 26)"]


def test_messages_include_counts_and_one_message_per_draft(tmp_path):
    coop = tmp_path / "coop"
    write(coop / "run-live-20261124-090000.json", [entry("A", "submitted", cv_path="output/cvs/a/cv.pdf")])
    drafts = [
        {
            "name": "Sam Lee",
            "company": "Shopify",
            "profile_url": "https://linkedin.com/in/sam",
            "message": "Hi Sam!",
        },
        {
            "name": "Kim",
            "company": "Ciena",
            "profile_url": "",
            "message": "Hi Kim!",
            "job_title": "SWE Intern",
        },
        {"name": "No message", "company": "X", "message": ""},
    ]
    write(tmp_path / "outreach" / "drafts-20261124.json", drafts)

    messages = format_messages(build_summary(coop, tmp_path / "outreach", DAY))
    assert len(messages) == 3
    assert "Applied: 1" in messages[0]
    assert "Connections found: 2" in messages[0]
    assert "Messages drafted: 2" in messages[0]
    assert "- A @ Acme" in messages[0]
    assert "Tailored CVs made: 1" in messages[0]
    assert messages[1].startswith("Draft 1/2: Sam Lee")
    assert messages[1].endswith("Hi Sam!")
    assert "Ciena (SWE Intern)" in messages[2]


def test_empty_day(tmp_path):
    messages = format_messages(build_summary(tmp_path / "coop", tmp_path / "outreach", DAY))
    assert messages == ["Job hunt summary - Tue Nov 24\n\nNothing ran today."]
