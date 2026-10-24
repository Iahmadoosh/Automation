"""uOttawa co-op applier: IndEX job board -> posting -> application form, Ottawa first."""

import base64
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import BrowserContext, Dialog, Locator, Page
from playwright.sync_api import Error as PlaywrightError

from adapters.browser_utils import click_and_follow, first_visible, goto_with_backoff, settle
from adapters.coop_parsing import (
    DocumentPrefs,
    FieldAction,
    FieldPlan,
    FormField,
    JobCard,
    Posting,
    RadioGroup,
    group_by_location,
    group_radios,
    map_posting,
    plan_field,
)
from adapters.coop_selectors import CoopSelectors
from adapters.uottawa_index import IndexNavigator
from core.config import CoopSettings
from core.errors import IndexLoginError, LayoutChangedError, SessionExpiredError
from core.throttle import RateLimiter, Throttle
from engine.screening import JobContext, ScreeningAnswerer, ScreeningQuestion

MAX_FORM_STEPS = 8
MAX_FILL_PASSES = 3
MAX_ADVANCE_HOPS = 4
FETCH_BASE64_JS = r"""
async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const bytes = new Uint8Array(await r.arrayBuffer());
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
"""
_PDF_POSTING = re.compile(r"DisplayPDF|\.pdf($|\?)", re.IGNORECASE)
EXPAND_PROMPT = (
    "Finished applying to all Ottawa listings. Would you like to expand your search to "
    "listings outside Ottawa? (y/n): "
)

# Label/value pairs from tables (th|td + td) and definition lists, plus page text fallbacks.
EXTRACT_POSTING_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const pairs = [];
  document.querySelectorAll('tr').forEach(tr => {
    const cells = tr.querySelectorAll(':scope > th, :scope > td');
    if (cells.length === 2) pairs.push([clean(cells[0].innerText), cells[1].innerText.trim()]);
  });
  document.querySelectorAll('dt').forEach(dt => {
    const dd = dt.nextElementSibling;
    if (dd && dd.tagName === 'DD') pairs.push([clean(dt.innerText), dd.innerText.trim()]);
  });
  const heading = document.querySelector('h1, h2');
  const main = document.querySelector('main, #main, .main, #content') || document.body;
  return {pairs, heading: heading ? clean(heading.innerText) : '', text: main.innerText.slice(0, 20000)};
}
"""

# Tags every visible field with data-coop-idx and describes it, scoped to the form.
ENUMERATE_FIELDS_JS = r"""
(formSelector) => {
  document.querySelectorAll('[data-coop-idx]').forEach(e => e.removeAttribute('data-coop-idx'));
  let root = formSelector ? document.querySelector(formSelector) : null;
  if (!root) {
    const forms = Array.from(document.forms);
    const score = f => f.querySelectorAll('input, textarea, select').length;
    root = forms.sort((a, b) => score(b) - score(a))[0] || document;
  }
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const groupLabel = (el, own) => {
    const fs = el.closest('fieldset');
    const legend = fs && fs.querySelector('legend');
    const box = el.closest('table, ul, .radio-group, [role=radiogroup]');
    const prev = box && box.previousElementSibling;
    const row = el.closest('.form-group, .question, .control-group');
    const head = row && row.querySelector('.control-label, .question-text, label:not([for])');
    for (const c of [legend, prev, head, box && box.getAttribute('aria-label')]) {
      const t = clean(typeof c === 'string' ? c : c && c.innerText);
      if (t && t !== own) return t.slice(0, 400);
    }
    return '';
  };
  const out = [];
  let idx = 0;
  for (const el of root.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || (el.tagName === 'INPUT' ? 'text' : el.tagName)).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    if (el.disabled || (el.offsetParent === null && type !== 'file')) continue;
    let label = el.labels && el.labels.length ? Array.from(el.labels).map(l => l.innerText).join(' ') : '';
    if (!label) label = el.getAttribute('aria-label') || '';
    if (!label && el.getAttribute('aria-labelledby')) {
      label = el.getAttribute('aria-labelledby').split(/\s+/)
        .map(id => (document.getElementById(id) || {}).innerText || '').join(' ');
    }
    if (!label) {
      const row = el.closest('tr, .form-group, .control-group, fieldset, li');
      const head = row && row.querySelector('th, label, legend, .control-label');
      if (head) label = head.innerText;
    }
    if (!label) label = el.getAttribute('placeholder') || '';
    label = clean(label);
    el.setAttribute('data-coop-idx', String(idx));
    out.push({
      idx, tag: el.tagName.toLowerCase(), type, name: el.name || '', label,
      maxlength: el.maxLength > 0 ? el.maxLength : null,
      required: el.required || el.getAttribute('aria-required') === 'true',
      value: el.value || '', checked: !!el.checked,
      options: el.tagName === 'SELECT'
        ? Array.from(el.options).map(o => ({value: o.value, text: clean(o.text)})) : [],
      group_label: (type === 'radio' || type === 'checkbox') ? groupLabel(el, label) : '',
    });
    idx++;
  }
  return out;
}
"""


class ApplicationStatus(StrEnum):
    PREVIEW = "previewed"
    DRY_RUN = "dry_run_complete"
    SUBMITTED = "submitted"
    DECLINED = "declined_at_confirmation"
    ALREADY_APPLIED = "already_applied"
    NO_APPLY_BUTTON = "no_application_form"
    PDF_POSTING = "pdf_posting"
    ERROR = "error"


@dataclass(frozen=True)
class ManualItem:
    label: str
    note: str
    required: bool


@dataclass
class ApplicationResult:
    group: str
    job_id: str
    title: str
    company: str
    location: str
    status: ApplicationStatus
    deadline: str = ""
    reason: str = ""
    answers: dict[str, str] = field(default_factory=dict)
    manual: list[ManualItem] = field(default_factory=list)
    screenshot: str = ""

    @classmethod
    def for_card(cls, group: str, card: JobCard, status: ApplicationStatus, **kwargs) -> "ApplicationResult":
        return cls(group, "", card.title, card.organization, card.location, status, **kwargs)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _RunState:
    applied: set[str]
    max_applications: int
    attempted: int = 0

    @property
    def capped(self) -> bool:
        return self.attempted >= self.max_applications


def _is_navigable(href: str) -> bool:
    return bool(href) and not href.startswith(("javascript:", "#")) and href != "about:blank"


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text)[:60] or "posting"


def _ask_yes_no(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:  # no interactive terminal: always the safe answer
        print("(no interactive input available - treating as 'n')")
        return False


class UOttawaCoopApplier:
    def __init__(
        self,
        context: BrowserContext,
        page: Page,
        settings: CoopSettings,
        selectors: CoopSelectors,
        profile: dict,
        answerer: ScreeningAnswerer,
        confirm: Callable[[str], bool] | None = None,
        interactive: bool = True,
    ) -> None:
        self.context = context
        self.page = page
        self.settings = settings
        self.selectors = selectors
        self.profile = profile
        self.answerer = answerer
        self.confirm = confirm or _ask_yes_no
        self.throttle = Throttle(settings.min_delay, settings.max_delay)
        self.rate_limiter = RateLimiter(settings.seconds_between_applications)
        self.documents = DocumentPrefs(
            settings.resume_path,
            settings.transcript_path,
            settings.resume_doc_name,
            settings.transcript_doc_name,
            settings.cover_letter_doc_name,
        )
        self.navigator = IndexNavigator(
            context, settings, selectors, self.throttle, interactive, self.capture_failure
        )
        self._board: Page | None = None
        self._board_url = ""
        self._dialogs: list[str] = []
        # Never let a portal confirm() submit anything on our behalf.
        context.on("page", lambda p: p.on("dialog", self._dismiss_dialog))
        page.on("dialog", self._dismiss_dialog)
        settings.log_dir.mkdir(parents=True, exist_ok=True)

    def _dismiss_dialog(self, dialog: Dialog) -> None:
        self._dialogs.append(dialog.message[:200])
        print(f"  Portal dialog dismissed: {dialog.message[:120]!r}")
        dialog.dismiss()

    # ------------------------------------------------------------------ session + board

    def save_session(self) -> None:
        self.settings.auth_path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(self.settings.auth_path))

    def login(self) -> Page:
        """SSO (+MFA if needed) -> dashboard -> IndEX Jobs -> IndEX login. Returns the job board."""
        board = self.navigator.open_job_board(self.page)
        self.save_session()
        print(f"Job board ready: {board.url}")
        return board

    def _open_filtered_board(self) -> None:
        self._board = self.login()
        self.navigator.apply_filters(self._board)
        self._board_url = self._board.url

    def _restore_board(self) -> None:
        board = self._board
        try:
            goto_with_backoff(board, self._board_url)
            settle(board)
            if not self.navigator.board_ready(board):
                self.navigator.login_index(board)
            self.navigator.apply_filters(board)
        except (PlaywrightError, LayoutChangedError) as exc:
            print(f"  Board restore failed ({str(exc).splitlines()[0]}); reopening from the dashboard.")
            self._open_filtered_board()

    def _return_to_board(self, work: Page | None) -> None:
        keep = {id(self.page), id(self._board)}
        for p in list(self.context.pages):
            if id(p) not in keep:
                p.close()
        if work is self._board or not self.navigator.board_ready(self._board):
            self._restore_board()

    def _open_card(self, card: JobCard) -> Page:
        board = self._board
        # Re-scrape so data-coop-card tags are fresh even after the board re-rendered.
        current = next((c for c in self.navigator.scrape_cards(board) if c.key == card.key), None)
        if current is None:
            raise LayoutChangedError("Listing is no longer on the filtered board.")
        if _is_navigable(current.href):
            work = self.context.new_page()
            goto_with_backoff(work, urljoin(board.url, current.href))
            settle(work)
        else:
            work = click_and_follow(self.context, board, board.locator(f'[data-coop-card="{current.idx}"]'))
        self.navigator.check_session(work)
        self.throttle.pause()
        return work

    def read_posting(self, page: Page, card: JobCard) -> Posting:
        raw = page.evaluate(EXTRACT_POSTING_JS)
        posting = map_posting(raw["pairs"], page.url, raw["heading"], raw["text"])
        return replace(
            posting,
            title=card.title or posting.title,
            company=card.organization or posting.company,
            programs=posting.programs or card.programs,
        )

    # ------------------------------------------------------------------ form filling

    def _fields(self, page: Page) -> list[FormField]:
        raw = page.evaluate(ENUMERATE_FIELDS_JS, self.selectors.application_form)
        return [FormField.from_dom(r) for r in raw]

    def _has_form(self, page: Page) -> bool:
        fields = self._fields(page)
        rich = any(f.tag in ("textarea", "select") or f.type in ("file", "radio") for f in fields)
        return rich or sum(f.type in ("text", "email", "tel") for f in fields) >= 2

    def _advance_to_form(self, page: Page) -> Page | None:
        """Click through intermediary pages (Apply / Continue to Application / redirect notice)."""
        for _ in range(MAX_ADVANCE_HOPS):
            if self._has_form(page):
                return page
            button = first_visible([page.locator(self.selectors.apply_button)], timeout_ms=4000)
            if button is None:
                return None
            text = (button.inner_text() or button.get_attribute("value") or "").strip()
            print(f"  Clicking '{text[:50]}'")
            page = click_and_follow(self.context, page, button)
            self.navigator.check_session(page)
            self.throttle.pause()
        return page if self._has_form(page) else None

    def _type_into(self, locator: Locator, value: str) -> bool:
        locator.scroll_into_view_if_needed()
        locator.fill(value)
        if locator.input_value() != value:
            locator.fill("")
            locator.press_sequentially(value, delay=15)
        return locator.input_value() == value

    def _execute(self, page: Page, plan: FieldPlan, job: JobContext, result: ApplicationResult) -> None:
        field_ = plan.field
        name = field_.display_name
        locator = page.locator(f'[data-coop-idx="{field_.idx}"]')

        if plan.action in (FieldAction.SKIP, FieldAction.CHOICE):
            return
        if plan.action is FieldAction.MANUAL:
            result.manual.append(ManualItem(name, plan.note, field_.required))
            return
        if plan.action is FieldAction.SELECT:
            locator.select_option(value=plan.value)
            print(f"    [select] {name} -> {plan.note}")
        elif plan.action is FieldAction.UPLOAD:
            locator.set_input_files(plan.value)
            print(f"    [upload] {name} -> {Path(plan.value).name}")
        elif plan.action is FieldAction.PROFILE:
            if not self._type_into(locator, plan.value):
                result.manual.append(ManualItem(name, "value did not stick", field_.required))
            print(f"    [profile] {name}")
        elif plan.action is FieldAction.LLM:
            question = ScreeningQuestion(name, plan.max_words, plan.max_chars)
            print(f"    [qwen] {name[:70]}")
            answer = self.answerer.answer(job, question)
            if not answer:
                result.manual.append(ManualItem(name, "model returned no answer", field_.required))
                return
            if not self._type_into(locator, answer):
                result.manual.append(ManualItem(name, "rich-text field rejected input", field_.required))
            result.answers[name] = answer
        self.throttle.pause()

    def _answer_radio(
        self, page: Page, group: RadioGroup, job: JobContext, result: ApplicationResult
    ) -> None:
        if group.answered:
            return
        options = [o.label or o.value for o in group.options]
        print(f"    [qwen-choice] {group.question[:70]}")
        choice = self.answerer.choose(job, group.question, options)
        if choice is None:
            result.manual.append(ManualItem(group.question, "model unsure - choose yourself", group.required))
            return
        page.locator(f'[data-coop-idx="{group.options[choice].idx}"]').check()
        result.answers[group.question] = options[choice]
        self.throttle.pause()

    def _fill_current_step(
        self, page: Page, job: JobContext, result: ApplicationResult, handled: set
    ) -> None:
        # Re-enumerate after each pass: selects/uploads can reveal dependent fields.
        for _ in range(MAX_FILL_PASSES):
            fields = self._fields(page)
            pending = [f for f in fields if f.key not in handled]
            if not pending:
                return
            for form_field in pending:
                handled.add(form_field.key)
                self._execute(page, plan_field(form_field, self.profile, self.documents), job, result)
            for group in group_radios(pending):
                self._answer_radio(page, group, job, result)

    def _screenshot(self, page: Page, posting: Posting, stage: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.settings.log_dir / f"{_safe_name(posting.job_id)}-{stage}-{stamp}.png"
        page.screenshot(path=str(path), full_page=True)
        return str(path)

    def capture_failure(self, page: Page, label: str) -> str:
        """Screenshot + HTML for diagnosis. Typed passwords are DOM properties, not serialised."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = self.settings.log_dir / f"error-{_safe_name(label)}-{stamp}"
        try:
            page.screenshot(path=f"{base}.png", full_page=True)
            Path(f"{base}.html").write_text(page.content(), encoding="utf-8")
        except PlaywrightError:
            return ""
        return f"{base}.png"

    def _confirm_submit(self, result: ApplicationResult, auto_confirm: bool) -> bool:
        print(f"\n  Ready to submit: {result.title} @ {result.company} (deadline {result.deadline or 'n/a'})")
        for label, answer in result.answers.items():
            print(f"    Q: {label[:90]}\n    A: {answer[:300]}{'...' if len(answer) > 300 else ''}")
        for item in result.manual:
            flag = "REQUIRED" if item.required else "optional"
            print(f"    [needs you, {flag}] {item.label[:80]} - {item.note}")
        if auto_confirm and not any(item.required for item in result.manual):
            return True
        if result.manual:
            print("  Fix the fields above in the browser window before confirming.")
        return self.confirm("  Submit this application? [y/N]: ")

    def apply(
        self, page: Page, posting: Posting, result: ApplicationResult, *, dry_run: bool, auto_confirm: bool
    ) -> ApplicationResult:
        if re.search(self.selectors.already_applied_regex, page.inner_text("body"), re.IGNORECASE):
            result.status = ApplicationStatus.ALREADY_APPLIED
            return result
        self._dialogs.clear()
        form_page = self._advance_to_form(page)
        if form_page is None:
            result.status = ApplicationStatus.NO_APPLY_BUTTON
            result.reason = "no Apply button or form found (closed, external, or selectors need calibration)"
            result.screenshot = self._screenshot(page, posting, "no-form")
            return result

        job = JobContext(posting.title, posting.company, posting.description)
        handled: set = set()
        for _ in range(MAX_FORM_STEPS):
            self._fill_current_step(form_page, job, result, handled)
            submit = form_page.locator(self.selectors.submit_button).first
            if submit.is_visible():
                break
            next_step = form_page.locator(self.selectors.next_step_button).first
            if not next_step.is_visible():
                raise LayoutChangedError("Application form shows neither a Next nor a Submit button.")
            next_step.click()
            settle(form_page)
            self.navigator.check_session(form_page)
            self.throttle.pause()
        else:
            raise LayoutChangedError(f"Application form exceeded {MAX_FORM_STEPS} steps.")
        if self._dialogs:
            result.reason = f"portal dialog(s) dismissed: {'; '.join(self._dialogs)}"

        if dry_run:
            result.screenshot = self._screenshot(form_page, posting, "dry-run")
            result.reason = result.reason or "stopped before Submit (dry run)"
            return result

        result.screenshot = self._screenshot(form_page, posting, "pre-submit")
        if not self._confirm_submit(result, auto_confirm):
            result.status = ApplicationStatus.DECLINED
            return result
        submit.click()
        settle(form_page)
        self.throttle.pause(2)
        result.status = ApplicationStatus.SUBMITTED
        result.screenshot = self._screenshot(form_page, posting, "submitted")
        return result

    # ------------------------------------------------------------------ orchestration

    def _load_applied(self) -> set[str]:
        path = self.settings.applied_log_path
        if not path.exists():
            return set()
        return set(json.loads(path.read_text(encoding="utf-8")))

    def _save_applied(self, applied: set[str]) -> None:
        path = self.settings.applied_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(applied), indent=2), encoding="utf-8")

    def _save_pdf_posting(self, card: JobCard, result: ApplicationResult) -> ApplicationResult:
        """IndEX-hosted PDF postings carry their own instructions; save them for the human."""
        result.status = ApplicationStatus.PDF_POSTING
        url = urljoin(self._board.url, card.href)
        try:
            data = self._board.evaluate(FETCH_BASE64_JS, url)  # same-origin fetch with the session
            path = self.settings.log_dir / f"posting-{_safe_name(card.title)}.pdf"
            path.write_bytes(base64.b64decode(data))
            result.reason = f"PDF posting - apply per its instructions: {path}"
        except PlaywrightError as exc:
            result.reason = f"PDF posting at {url} (download failed: {str(exc).splitlines()[0]})"
        return result

    def _process_card(
        self, group: str, card: JobCard, state: _RunState, *, dry_run: bool, auto_confirm: bool, preview: bool
    ) -> ApplicationResult:
        if card.key in state.applied:
            return ApplicationResult.for_card(group, card, ApplicationStatus.ALREADY_APPLIED)
        result = ApplicationResult.for_card(group, card, ApplicationStatus.DRY_RUN)
        if _PDF_POSTING.search(card.href):
            return self._save_pdf_posting(card, result)
        work = None
        try:
            work = self._open_card(card)
            posting = self.read_posting(work, card)
            result.job_id, result.deadline = posting.job_id, posting.deadline
            if preview:
                result.status = ApplicationStatus.PREVIEW
                result.screenshot = self._screenshot(work, posting, "posting")
                return result
            self.rate_limiter.wait()
            state.attempted += 1
            self.apply(work, posting, result, dry_run=dry_run, auto_confirm=auto_confirm)
            if result.status is ApplicationStatus.SUBMITTED:
                state.applied.add(card.key)
                self._save_applied(state.applied)
        except (SessionExpiredError, IndexLoginError):
            raise
        except (PlaywrightError, LayoutChangedError) as exc:
            result.status = ApplicationStatus.ERROR
            result.reason = str(exc).splitlines()[0]
            result.screenshot = self.capture_failure(work or self._board, card.title)
        finally:
            self._return_to_board(work)
        return result

    def _process_group(
        self, group: str, cards: list[JobCard], state: _RunState, results: list, **mode
    ) -> None:
        print(f"\n=== {group}: {len(cards)} listing(s) ===")
        for card in cards:
            if state.capped:
                print(f"Reached the cap of {state.max_applications} applications for this run (--max).")
                return
            print(
                f"\n[{group}] {card.title} @ {card.organization or '?'} ({card.location or 'location n/a'})"
            )
            result = self._process_card(group, card, state, **mode)
            print(f"  -> {result.status.value}{': ' + result.reason if result.reason else ''}")
            results.append(result)

    def run(
        self,
        *,
        dry_run: bool,
        max_applications: int,
        auto_confirm: bool = False,
        expand: str = "ask",
        preview: bool = False,
    ) -> list[ApplicationResult]:
        self._open_filtered_board()
        cards = self.navigator.scrape_cards(self._board)
        if not cards:
            shot = self.capture_failure(self._board, "board-no-cards")
            print(f"No job cards found on the filtered board. Evidence: {shot}")
            return []
        ottawa, elsewhere = group_by_location(cards, self.settings.local_regex)
        print(f"\nFound {len(cards)} listing(s): {len(ottawa)} in Ottawa, {len(elsewhere)} elsewhere.")

        state = _RunState(self._load_applied(), max_applications)
        results: list[ApplicationResult] = []
        mode = {"dry_run": dry_run, "auto_confirm": auto_confirm, "preview": preview}
        self._process_group("Ottawa", ottawa, state, results, **mode)

        if not elsewhere or state.capped:
            return results
        wants_more = {"yes": True, "no": False}.get(expand)
        if wants_more is None:
            wants_more = self.confirm(EXPAND_PROMPT)
        if wants_more:
            self._process_group("Outside Ottawa", elsewhere, state, results, **mode)
        else:
            print(f"Skipping {len(elsewhere)} listing(s) outside Ottawa.")
        return results
