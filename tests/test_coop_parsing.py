import re
from pathlib import Path

import pytest

from adapters.coop_parsing import (
    DocumentPrefs,
    FieldAction,
    FormField,
    Posting,
    enforce_limits,
    group_by_location,
    group_radios,
    map_posting,
    match_options,
    parse_card,
    parse_limits,
    plan_field,
)

NO_DOCS = DocumentPrefs(None, None, "", "")
PROFILE = {"first_name": "Jane", "email": "jdoe@uottawa.ca"}


def make_posting(**overrides) -> Posting:
    base = dict(
        job_id="123",
        title="Software Developer Intern",
        company="Acme",
        deadline="",
        documents=(),
        description="Build things.",
        level="",
        programs="Computer Science (CSI)",
        url="https://x/1",
    )
    return Posting(**(base | overrides))


def make_field(**overrides) -> FormField:
    base = dict(
        idx=0,
        tag="input",
        type="text",
        name="",
        label="",
        maxlength=None,
        required=False,
        value="",
        checked=False,
        options=(),
    )
    return FormField(**(base | overrides))


class TestMapPosting:
    def test_maps_labelled_pairs(self):
        pairs = [
            ["Job ID:", "98765"],
            ["Job Title", "Junior Developer"],
            ["Organization", "Shopify"],
            ["Application Deadline", "Nov 3, 2026"],
            ["Application Documents Required", "Resume, Transcript; Cover Letter"],
            ["Job Description", "Write Python."],
            ["Level", "2nd year, 3rd year"],
            ["Targeted Programs", "CSI, SEG"],
        ]
        p = map_posting(pairs, "https://coop.example/posting")
        assert (p.job_id, p.title, p.company) == ("98765", "Junior Developer", "Shopify")
        assert p.documents == ("Resume", "Transcript", "Cover Letter")
        assert p.description == "Write Python."
        assert p.level == "2nd year, 3rd year"

    def test_falls_back_to_url_id_heading_and_page_text(self):
        p = map_posting([], "https://coop.example/view?postingId=4411", "Data Intern", "Body text")
        assert (p.job_id, p.title, p.description) == ("4411", "Data Intern", "Body text")

    def test_stable_hash_id_when_no_id_anywhere(self):
        a = map_posting([], "https://coop.example/view/abc")
        b = map_posting([], "https://coop.example/view/abc")
        assert a.job_id == b.job_id and a.job_id.startswith("url-")


class TestLimits:
    @pytest.mark.parametrize(
        "label,maxlength,expected",
        [
            ("Why us? (max 250 words)", None, (250, None)),
            ("Describe a project (1000 characters)", 800, (None, 800)),
            ("Tell us about yourself", 1500, (None, 1500)),
            ("Motivation (200 mots)", None, (200, None)),
        ],
    )
    def test_parse_limits(self, label, maxlength, expected):
        assert parse_limits(label, maxlength) == expected

    def test_enforce_word_limit_ends_on_sentence(self):
        text = "One two three. Four five six. Seven eight nine ten."
        assert enforce_limits(text, 7, None) == "One two three. Four five six."

    def test_enforce_char_limit(self):
        assert len(enforce_limits("a" * 500, None, 100)) <= 100

    def test_untouched_when_within_limits(self):
        assert enforce_limits("  Short answer.  ", 50, 500) == "Short answer."


class TestPlanField:
    def test_profile_field(self):
        plan = plan_field(make_field(label="First Name"), PROFILE, NO_DOCS)
        assert plan.action is FieldAction.PROFILE and plan.value == "Jane"

    def test_missing_profile_value_is_manual(self):
        plan = plan_field(make_field(label="Phone"), PROFILE, NO_DOCS)
        assert plan.action is FieldAction.MANUAL

    def test_prefilled_is_skipped(self):
        assert plan_field(make_field(label="Email", value="x@y"), PROFILE, NO_DOCS).action is FieldAction.SKIP

    def test_textarea_goes_to_llm_with_limits(self):
        f = make_field(tag="textarea", type="textarea", label="Why this role? (150 words)", maxlength=1200)
        plan = plan_field(f, PROFILE, NO_DOCS)
        assert (plan.action, plan.max_words, plan.max_chars) == (FieldAction.LLM, 150, 1200)

    def test_textarea_about_mobile_is_not_treated_as_phone(self):
        f = make_field(tag="textarea", type="textarea", label="Describe your mobile development work")
        assert plan_field(f, PROFILE, NO_DOCS).action is FieldAction.LLM

    def test_short_factual_field_never_generated(self):
        assert plan_field(make_field(label="GPA"), PROFILE, NO_DOCS).action is FieldAction.MANUAL

    def test_radio_is_answered_as_a_group(self):
        assert (
            plan_field(make_field(type="radio", label="Yes"), PROFILE, NO_DOCS).action is FieldAction.CHOICE
        )

    def test_cover_letter_select_not_mistaken_for_resume(self):
        f = make_field(
            tag="select",
            type="select-one",
            label="Cover Letter",
            options=(("", "Select"), ("1", "Cover Letter - General"), ("2", "Resume - Software")),
        )
        plan = plan_field(f, PROFILE, DocumentPrefs(None, None, "Software", "", "General"))
        assert plan.action is FieldAction.SELECT and plan.value == "1"

    def test_checkbox_is_left_for_human(self):
        f = make_field(type="checkbox", label="I confirm I meet the requirements")
        assert plan_field(f, PROFILE, NO_DOCS).action is FieldAction.MANUAL

    def test_resume_select_uses_preferred_name(self):
        f = make_field(
            tag="select",
            type="select-one",
            label="Resume",
            options=(("", "-- Select --"), ("1", "Resume - General"), ("2", "Resume - Software")),
        )
        docs = DocumentPrefs(None, None, "Software", "")
        plan = plan_field(f, PROFILE, docs)
        assert plan.action is FieldAction.SELECT and plan.value == "2"

    def test_ambiguous_resume_select_is_manual(self):
        f = make_field(
            tag="select",
            type="select-one",
            label="Resume",
            options=(("", "Select"), ("1", "Resume A"), ("2", "Resume B")),
        )
        assert plan_field(f, PROFILE, NO_DOCS).action is FieldAction.MANUAL

    def test_transcript_file_upload(self, tmp_path: Path):
        transcript = tmp_path / "t.pdf"
        transcript.write_bytes(b"%PDF")
        f = make_field(type="file", label="Upload unofficial transcript")
        plan = plan_field(f, PROFILE, DocumentPrefs(None, transcript, "", ""))
        assert plan.action is FieldAction.UPLOAD and plan.value == str(transcript)


class TestJobBoard:
    def test_parse_card_label_lines_and_inline(self):
        card = parse_card(
            {
                "idx": 3,
                "linkText": "Junior Software Developer",
                "href": "/jobs/55",
                "text": "Junior Software Developer\nOrganization\nCiena\nLocation: Ottawa, ON\n"
                "Mobility\nHybrid\nDuration: 4 months\nPrograms\nComputer Science, SEG",
            }
        )
        assert (card.title, card.organization, card.location) == (
            "Junior Software Developer",
            "Ciena",
            "Ottawa, ON",
        )
        assert (card.mobility, card.duration, card.programs) == (
            "Hybrid",
            "4 months",
            "Computer Science, SEG",
        )
        assert card.href == "/jobs/55" and card.idx == 3

    def test_empty_label_does_not_swallow_next_label(self):
        card = parse_card({"text": "Dev\nLocation\nDuration\n8 months"})
        assert card.location == "" and card.duration == "8 months"

    def test_group_by_location_ottawa_first(self):
        cards = [
            parse_card({"idx": i, "linkText": t, "text": f"{t}\nLocation: {loc}"})
            for i, (t, loc) in enumerate(
                [("A", "Toronto"), ("B", "Ottawa, ON"), ("C", "Remote"), ("D", "OTTAWA")]
            )
        ]
        local, other = group_by_location(cards, re.compile(r"\bottawa\b", re.I))
        assert [c.title for c in local] == ["B", "D"]
        assert [c.title for c in other] == ["A", "C"]

    @pytest.mark.parametrize(
        "options,wanted,expected",
        [
            ([("1", "2026, Fall"), ("2", "2027, Winter"), ("3", "2027, Summer")], "2027, Winter", ["2"]),
            ([("e", "Faculty of Engineering"), ("s", "Faculty of Science")], "Engineering", ["e"]),
            (
                [
                    ("cs", "Computer Science"),
                    ("cse", "Computer Science and Mathematics"),
                    ("ce", "Computer Engineering"),
                ],
                "Computer Science",
                ["cs"],
            ),
            ([("x", "Winter 2027")], "2027, Winter", ["x"]),
            ([("x", "Fall 2027")], "2027, Winter", []),
        ],
    )
    def test_match_options(self, options, wanted, expected):
        assert match_options(options, wanted) == expected


def test_group_radios_uses_group_label_and_tracks_answered():
    fields = [
        make_field(
            idx=0, type="radio", name="q1", label="Yes", group_label="Are you eligible to work in Canada?"
        ),
        make_field(
            idx=1, type="radio", name="q1", label="No", group_label="Are you eligible to work in Canada?"
        ),
        make_field(idx=2, type="radio", name="q2", label="A", checked=True),
        make_field(idx=3, type="text", name="t"),
    ]
    groups = group_radios(fields)
    assert [g.name for g in groups] == ["q1", "q2"]
    assert groups[0].question == "Are you eligible to work in Canada?" and not groups[0].answered
    assert groups[1].question == "q2" and groups[1].answered
