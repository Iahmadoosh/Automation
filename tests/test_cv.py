import json
from datetime import date

import pytest

from core.errors import ConfigError
from engine.cv import (
    CvMaker,
    JobPosting,
    check_line,
    keyword_tailor,
    parse_master,
    render_html,
    shorten,
    tailor,
)

MASTER = parse_master(
    {
        "name": "Jane Doe",
        "contact": {"email": "jane@uottawa.ca", "github": "https://github.com/jane"},
        "summary": "CS co-op student who builds tools in Python.",
        "education": [{"school": "University of Ottawa", "degree": "BSc CS", "start": "2024", "end": "2028"}],
        "skills": {"Languages": ["Python", "Java", "SQL"], "Tools": ["Git", "Playwright"]},
        "experience": [
            {
                "id": "store",
                "title": "Sales Associate",
                "company": "Store",
                "bullets": ["Served 50+ customers per shift", "Trained 3 new hires"],
            }
        ],
        "projects": [
            {"id": "bot", "name": "Job Bot", "tech": ["Python", "Playwright"],
             "bullets": ["Automated forms with Playwright", "Wrote 60 tests with pytest"]},
            {"id": "sched", "name": "Scheduler", "tech": "Java",
             "bullets": ["Built a timetable tool in Java"]},
            {"id": "game", "name": "Game", "bullets": ["Made a game"]},
        ],
    }
)  # fmt: skip
JOB = JobPosting(
    "QA Automation Intern",
    "Acme",
    "Write Python test automation with Playwright and pytest. Kubernetes and AWS are assets.",
)


class FakeLLM:
    name = "fake"

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def query(self, system_prompt, user_input, timeout=0):
        self.prompts.append(user_input)
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def test_parse_master_basics():
    assert MASTER.contact == ("jane@uottawa.ca", "https://github.com/jane")
    assert [p.id for p in MASTER.projects] == ["bot", "sched", "game"]
    assert MASTER.projects[0].subheading == "Python, Playwright"


@pytest.mark.parametrize(
    "data, message",
    [
        ({}, "name"),
        ({"name": "J", "experience": [{"company": "X"}]}, "needs a title"),
        ({"name": "J", "projects": [{"name": "A"}, {"name": "A"}]}, "share the id"),
        ({"name": "J", "skills": "Python"}, "skills"),
        ({"name": "J", "projects": [{"name": "A", "bullets": "one line"}]}, "list of text lines"),
    ],
)
def test_parse_master_errors(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_master(data)


def test_check_line_blocks_new_numbers_and_copied_tools():
    original = MASTER.experience[0].text
    assert check_line("Served more than 50 customers each shift", original, MASTER, JOB) is None
    assert "new number" in check_line("Served 80 customers per shift", original, MASTER, JOB)
    assert "Kubernetes" in check_line("Deployed services on Kubernetes", original, MASTER, JOB)
    # The company and job title may be named.
    assert check_line("Eager to join Acme as a QA Automation Intern", original, MASTER, JOB) is None


def test_keyword_tailor_orders_by_relevance_and_keeps_wording():
    cv = keyword_tailor(MASTER, JOB, max_projects=2)
    assert [it.id for it, _ in cv.projects] == ["bot", "sched"]
    assert cv.projects[0][1][0] == "Automated forms with Playwright"
    assert cv.skills["Languages"][0] == "Python"
    assert cv.source == "keyword match"


def test_tailor_applies_valid_rewrites_and_rejects_invented_content():
    llm = FakeLLM(
        {
            "summary": "Student skilled in Python test automation with Playwright.",
            "skills": {"Languages": ["Python", "Go"], "Tools": ["Playwright"]},
            "experience": [
                {"id": "store", "bullets": ["Supported 50+ customers per shift", "Managed 12 staff"]}
            ],
            "projects": [
                {"id": "bot", "bullets": ["Automated web forms using Playwright", "Ran tests on AWS"]},
                {"id": "nope", "bullets": ["Invented project"]},
                {"id": "store", "bullets": ["experience id in projects is ignored"]},
            ],
        }
    )
    cv = tailor(MASTER, JOB, llm, max_projects=2)
    assert cv.source == "fake"
    assert cv.summary.startswith("Student skilled")
    assert cv.skills["Languages"] == ["Python", "Java", "SQL"]  # Go ignored, nothing you listed dropped
    assert cv.experience[0][1] == ["Supported 50+ customers per shift"]
    assert [it.id for it, _ in cv.projects] == ["bot"]
    assert cv.projects[0][1] == ["Automated web forms using Playwright"]
    joined = " ".join(cv.warnings)
    assert "Go" in joined and "12" in joined and "AWS" in joined
    assert "Kubernetes" not in llm.prompts[0].split("Master resume:")[1]


def test_tailor_falls_back_when_model_gives_garbage():
    cv = tailor(MASTER, JOB, FakeLLM("sorry, I can't help"))
    assert cv.source == "keyword match"
    assert "no usable answer" in cv.warnings[0]


def test_tailor_reads_json_after_reasoning_and_fences():
    reply = '<think>hmm {"x": 1}</think>\n```json\n{"summary": "Python developer.", "projects": []}\n```'
    cv = tailor(MASTER, JOB, FakeLLM(reply))
    assert cv.summary == "Python developer."
    assert cv.projects  # empty choice -> keyword fallback projects


def test_shorten_trims_projects_before_experience():
    cv = keyword_tailor(MASTER, JOB)
    steps = 0
    while shorten(cv):
        steps += 1
    assert steps > 0
    assert len(cv.projects) == 1 and len(cv.projects[0][1]) == 1
    assert len(cv.experience[0][1]) == 2  # experience keeps at least 2 bullets
    assert cv.summary == ""


def test_render_html_escapes_and_links():
    master = parse_master({"name": "<Jane>", "contact": {"github": "https://github.com/j"}})
    page = render_html(master, keyword_tailor(master, JOB))
    assert "&lt;Jane&gt;" in page and "<Jane>" not in page
    assert '<a href="https://github.com/j">github.com/j</a>' in page


def test_cv_maker_shrinks_to_one_page_and_reuses_output(tmp_path):
    rendered = []

    def fake_render(html, path):
        rendered.append(html)
        path.write_bytes(b"%PDF")
        return 2 if len(rendered) < 3 else 1

    maker = CvMaker(MASTER, None, tmp_path, fake_render)
    out = maker.make(JOB, date(2026, 11, 24))
    assert out.folder.name == "20261124-Acme-QA-Automation-Intern"
    assert out.pages == 1 and len(rendered) == 3
    assert {p.name for p in out.folder.iterdir()} == {"cv.pdf", "cv.html", "cv.txt", "job.txt", "report.json"}

    again = maker.make(JOB, date(2026, 11, 24))
    assert again.reused and len(rendered) == 3
    maker.make(JOB, date(2026, 11, 24), force=True)
    assert len(rendered) > 3


def test_cv_maker_warns_when_it_cannot_fit(tmp_path):
    def always_two(html, path):
        path.write_bytes(b"%PDF")
        return 2

    out = CvMaker(MASTER, None, tmp_path, always_two).make(JOB, date(2026, 11, 24))
    assert out.pages == 2
    assert "trim your master resume" in out.warnings[-1]


def test_template_is_valid():
    from pathlib import Path

    from engine.cv import load_master

    master = load_master(Path(__file__).resolve().parent.parent / "data" / "master_resume.template.yaml")
    assert master.name and master.experience and master.projects
