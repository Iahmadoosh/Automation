"""End-to-end flow against a fake two-tier portal served through Playwright routing (no network).

dashboard --(IndEX Jobs card, popup)--> IndEX login (User Type=Student) --> job board
--> filters --> cards (Ottawa first) --> posting --> intermediary page --> application form
"""

import re
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest

from adapters.coop_selectors import CoopSelectors
from adapters.uottawa_coop import ApplicationStatus, UOttawaCoopApplier
from core.config import CoopSettings
from core.errors import IndexLoginError
from engine.screening import ScreeningAnswerer

sync_api = pytest.importorskip("playwright.sync_api")

DASHBOARD = """<html><body><h1>Experiential Learning</h1>
  <div class="card" onclick="window.open('https://index.test/login', '_blank')"><h3>IndEX Jobs</h3></div>
</body></html>"""

LOGIN = """<html><body><h2>IndEX Login</h2>{error}
<form method="post" action="/board">
  <label for="ut">User Type</label>
  <select id="ut" name="UserType"><option value="">Select</option>
    <option value="employer">Employer</option><option value="student">Student</option></select>
  <label for="un">Username</label><input id="un" name="username">
  <label for="pw">Password</label><input id="pw" type="password" name="password">
  <button type="submit">Login</button>
</form></body></html>"""

CARD = """<div class="job-card"><a href="/job/{id}">{title}</a>
  <div>Organization: {org}</div><div>Location: {loc}</div><div>Mobility: Hybrid</div>
  <div>Duration: 4 months</div><div>Programs: Computer Science</div></div>"""
CARDS = [
    {"id": 3, "title": "Cloud Intern", "org": "Shopify", "loc": "Toronto, ON"},
    {"id": 1, "title": "Software Developer Intern", "org": "Acme", "loc": "Ottawa, ON"},
    {"id": 2, "title": "QA Intern", "org": "Closed Co", "loc": "Ottawa"},
]
PDF_CARD = CARD.replace("/job/{id}", "/IndEX/Home/DisplayPDF/{id}").format(
    id=46750, title="Policy Intern", org="Gov", loc="Ottawa, ON"
)


def board(filtered: bool) -> str:
    cards = "".join(CARD.format(**c) for c in CARDS) + PDF_CARD if filtered else "<p>Use the filters.</p>"
    return f"""<html><body><aside><h3>Filter By</h3><form method="get" action="/board">
      <label for="term">Term</label><select id="term" name="Term" multiple>
        <option value="f26">2026, Fall</option><option value="w27">2027, Winter</option></select>
      <label for="fac">Faculties</label><select id="fac" name="Faculty">
        <option value="">All</option><option value="sci">Science</option>
        <option value="eng">Engineering</option></select>
      <label for="prog">Programs</label><select id="prog" name="Program">
        <option value="">All</option><option value="ceg">Computer Engineering</option>
        <option value="csi">Computer Science</option></select>
      <label><input type="checkbox" name="hide" value="on"> Hide Jobs Marked as Applied</label>
      <button type="submit"><span class="glyphicon">&#xe003;</span> Filter</button></form></aside>
      <main>{cards}</main></body></html>"""


def posting(job_id: int, with_apply: bool) -> str:
    apply = f'<a href="/job/{job_id}/intro">Apply</a>' if with_apply else "<p>Posting closed.</p>"
    return f"""<html><body><h1>Posting</h1><table>
      <tr><th>Job ID</th><td>J-{job_id}</td></tr>
      <tr><th>Job Description</th><td>Build Python tools.</td></tr></table>{apply}</body></html>"""


INTRO = """<html><body><p>You are leaving IndEX for the employer's application.</p>
  <button onclick="location.href='{next}'">Continue to Application</button></body></html>"""

FORM = """<html><body><form method="post" action="/submitted">
  <label for="fn">First Name</label><input id="fn" name="fn">
  <label for="q1">Why do you want this role? (100 words)</label><textarea id="q1" name="q1"></textarea>
  <fieldset><legend>Are you legally eligible to work in Canada?</legend>
    <label><input type="radio" name="elig" value="yes"> Yes</label>
    <label><input type="radio" name="elig" value="no"> No</label></fieldset>
  <label for="cv">Resume</label><select id="cv" name="cv">
    <option value="">-- Select --</option><option value="7">Resume - Software</option></select>
  <button type="submit">Submit Application</button></form></body></html>"""


class FakeLLM:
    def query(self, system_prompt: str, user_input: str, timeout: float = 30) -> str:
        if "UNSURE" in system_prompt:
            return "1"
        return "I am a 2nd-year CSI student who builds Python automation tools."


class Portal:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.logins: list[dict] = []
        self.filters: list[dict] = []

    def handle(self, route) -> None:
        req = route.request
        url = urlparse(req.url)
        html = lambda body: route.fulfill(body=body, content_type="text/html")  # noqa: E731
        if url.hostname == "exp.test":
            return html(DASHBOARD)
        if url.path == "/login":
            return html(LOGIN.format(error=""))
        if url.path == "/board" and req.method == "POST":
            form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
            self.logins.append(form)
            ok = form.get("UserType") == "student" and form.get("password") == "pw-123"
            return html(board(False) if ok else LOGIN.format(error="<p>Invalid username or password</p>"))
        if url.path == "/board":
            query = parse_qs(url.query)
            self.filters.append(query)
            want = {"Term": ["w27"], "Faculty": ["eng"], "Program": ["csi"], "hide": ["on"]}
            return html(board(all(query.get(k) == v for k, v in want.items())))
        if match := re.fullmatch(r"/job/(\d)", url.path):
            return html(posting(int(match[1]), with_apply=match[1] != "2"))
        if match := re.fullmatch(r"/job/(\d)/intro", url.path):
            return html(INTRO.format(next=f"/job/{match[1]}/apply"))
        if re.fullmatch(r"/job/\d/apply", url.path):
            return html(FORM)
        if url.path.startswith("/IndEX/Home/DisplayPDF/"):
            return route.fulfill(body=b"%PDF-1.4 fake", content_type="application/pdf")
        if url.path == "/submitted":
            self.submitted.append(req.post_data or "")
            return html("<html><body>Application submitted</body></html>")
        return route.fulfill(status=404, body="missing")


@pytest.fixture
def portal(tmp_path):
    fake = Portal()
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except sync_api.Error as exc:
            pytest.skip(f"Chromium not installed: {exc}")
        context = browser.new_context()
        context.route(re.compile(r"^https://(exp|index)\.test/.*"), fake.handle)
        settings = CoopSettings(
            base_url="https://exp.test", dashboard_url="https://exp.test/dashboard",
            email="jdoe@uottawa.ca", index_username="jdoe@uottawa.ca", password="pw-123",
            auth_path=tmp_path / "auth.json", profile_path=tmp_path / "p.json", resume_candidates=(),
            resume_path=None, transcript_path=None, resume_doc_name="Software", transcript_doc_name="",
            cover_letter_doc_name="", selectors_path=tmp_path / "s.json", log_dir=tmp_path / "logs",
            applied_log_path=tmp_path / "applied.json", term="2027, Winter", faculty="Engineering",
            program="Computer Science", hide_applied=True, local_regex=re.compile(r"\bottawa\b", re.I),
            max_applications=10, min_delay=0, max_delay=0, seconds_between_applications=0,
            headless=True, llm_endpoint="", llm_model="", llm_answer_timeout=1,
        )  # fmt: skip
        bot = UOttawaCoopApplier(
            context, context.new_page(), settings, CoopSelectors(), {"first_name": "Jane"},
            ScreeningAnswerer(FakeLLM(), "resume", {}), confirm=lambda _: True, interactive=False,
        )  # fmt: skip
        yield bot, fake
        browser.close()


def test_dry_run_ottawa_only_fills_forms_and_never_submits(portal):
    bot, fake = portal
    results = {r.title: r for r in bot.run(dry_run=True, max_applications=10, expand="no")}

    assert fake.logins[0] == {"UserType": "student", "username": "jdoe@uottawa.ca", "password": "pw-123"}
    assert set(results) == {"Software Developer Intern", "QA Intern", "Policy Intern"}  # no Toronto
    pdf = results["Policy Intern"]
    assert pdf.status is ApplicationStatus.PDF_POSTING and pdf.reason.endswith(".pdf")
    dry = results["Software Developer Intern"]
    assert dry.status is ApplicationStatus.DRY_RUN, dry.reason
    assert dry.group == "Ottawa" and dry.job_id == "J-1"
    assert dry.answers["Are you legally eligible to work in Canada?"] == "Yes"
    assert "Why do you want this role? (100 words)" in dry.answers
    assert dry.manual == []
    assert results["QA Intern"].status is ApplicationStatus.NO_APPLY_BUTTON
    assert fake.submitted == []


def test_live_run_processes_ottawa_then_expands_and_submits(portal):
    bot, fake = portal
    results = bot.run(dry_run=False, max_applications=10, expand="yes")

    assert [(r.group, r.title, r.status) for r in results] == [
        ("Ottawa", "Software Developer Intern", ApplicationStatus.SUBMITTED),
        ("Ottawa", "QA Intern", ApplicationStatus.NO_APPLY_BUTTON),
        ("Ottawa", "Policy Intern", ApplicationStatus.PDF_POSTING),
        ("Outside Ottawa", "Cloud Intern", ApplicationStatus.SUBMITTED),
    ]
    assert len(fake.submitted) == 2
    assert all("fn=Jane" in s and "cv=7" in s and "elig=yes" in s for s in fake.submitted)
    assert len(bot._load_applied()) == 2


def test_bad_password_fails_once_without_retry(portal):
    bot, fake = portal
    bot.navigator.settings = replace(bot.settings, password="wrong")

    with pytest.raises(IndexLoginError):
        bot.run(dry_run=True, max_applications=1, expand="no")
    assert len(fake.logins) == 1
