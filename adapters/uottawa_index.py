"""Navigation for the two-tier portal: Experiential Learning dashboard -> IndEX job board."""

import re
import time
from collections.abc import Callable
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Locator, Page
from playwright.sync_api import Error as PlaywrightError

from adapters.browser_utils import (
    choose_select_values,
    click_and_follow,
    first_visible,
    goto_with_backoff,
    settle,
)
from adapters.coop_parsing import CARD_LABELS, JobCard, match_options, parse_card
from adapters.coop_selectors import CoopSelectors
from core.config import CoopSettings
from core.errors import IndexLoginError, LayoutChangedError, SessionExpiredError
from core.throttle import Throttle

INDEX_CARD_TEXT = re.compile(r"ind\s*ex\s*jobs", re.IGNORECASE)
SSO_TIMEOUT_S = 600
MANUAL_TIMEOUT_S = 300

# Selects whose label (or name/id) matches the regex; tags them with data-coop-filter.
FIND_SELECTS_JS = r"""
(labelRe) => {
  const r = new RegExp(labelRe, 'i');
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    for (let n = el, d = 0; n && d < 4; n = n.parentElement, d++) {
      let s = n.previousElementSibling;
      for (let k = 0; s && k < 3; k++, s = s.previousElementSibling) {
        const t = clean(s.innerText);
        if (t && !s.querySelector('select')) return t.slice(0, 80);
      }
    }
    return '';
  };
  const out = [];
  document.querySelectorAll('select').forEach((s, i) => {
    const label = labelOf(s);
    if (!r.test(`${label} ${s.name || ''} ${s.id || ''}`)) return;
    s.setAttribute('data-coop-filter', String(i));
    out.push({idx: i, label, multiple: s.multiple,
              options: Array.from(s.options).map(o => ({value: o.value, text: clean(o.text)}))});
  });
  return out;
}
"""

# Smallest visible elements with a link and >= 3 card labels (the filter sidebar has selects,
# so it is excluded); tags each card's title link with data-coop-card.
SCRAPE_CARDS_JS = r"""
(labels) => {
  const tests = labels.map(l => new RegExp(`(^|\\n)\\s*${l}\\s*(:|\\n|$)`, 'i'));
  const matches = Array.from(document.querySelectorAll('div, li, tr, article, section'))
    .filter(e => e.offsetParent !== null && e.querySelector('a') && !e.querySelector('select'))
    .filter(e => { const t = e.innerText || ''; return tests.filter(rx => rx.test(t)).length >= 3; });
  const cards = matches.filter(e => !matches.some(o => o !== e && e.contains(o)));
  document.querySelectorAll('[data-coop-card]').forEach(e => e.removeAttribute('data-coop-card'));
  return cards.map((e, i) => {
    const links = Array.from(e.querySelectorAll('a'));
    const link = links.find(a => (a.innerText || '').trim()) || links[0];
    link.setAttribute('data-coop-card', String(i));
    return {idx: i, text: e.innerText, linkText: (link.innerText || '').trim(),
            href: link.getAttribute('href') || ''};
  });
}
"""


def _css(page: Page, selector: str) -> Locator | None:
    return page.locator(selector) if selector else None


class IndexNavigator:
    def __init__(
        self,
        context: BrowserContext,
        settings: CoopSettings,
        selectors: CoopSelectors,
        throttle: Throttle,
        interactive: bool,
        snapshot: Callable[[Page, str], str],
    ) -> None:
        self.context = context
        self.settings = settings
        self.selectors = selectors
        self.throttle = throttle
        self.interactive = interactive
        self.snapshot = snapshot
        self._board_ready = re.compile(selectors.board_ready_regex, re.IGNORECASE)

    # ------------------------------------------------------------ tier 1: SSO + dashboard

    def _on_sso(self, page: Page) -> bool:
        host = urlparse(page.url).hostname or ""
        return any(host.endswith(h) for h in self.selectors.login_hosts)

    def check_session(self, page: Page) -> None:
        if self._on_sso(page):
            raise SessionExpiredError("Redirected to Microsoft sign-in; run `python coop.py login`.")

    def _prefill_microsoft(self, page: Page) -> None:
        box = page.locator('input[name="loginfmt"]')
        if self.settings.email and box.is_visible() and not box.input_value():
            box.fill(self.settings.email)
            self.throttle.pause(0.5)
            page.locator("#idSIButton9").click()

    def _wait_for_sso(self, page: Page) -> None:
        if not self.interactive:
            raise SessionExpiredError("Microsoft sign-in required; run `python coop.py login`.")
        print("Microsoft sign-in required: complete it and Duo/Authenticator in the browser window.")
        print("Choose 'Yes' on 'Stay signed in?' so later runs skip MFA.")
        deadline = time.monotonic() + SSO_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if not self._on_sso(page):
                    settle(page)
                    if not self._on_sso(page):
                        print("  SSO complete.")
                        return
                else:
                    self._prefill_microsoft(page)
            except PlaywrightError:
                pass  # mid-redirect
            page.wait_for_timeout(1000)
        raise SessionExpiredError(f"Microsoft sign-in not completed within {SSO_TIMEOUT_S // 60} minutes.")

    def open_dashboard(self, page: Page) -> None:
        goto_with_backoff(page, self.settings.dashboard_url)
        settle(page)
        if self._on_sso(page):
            self._wait_for_sso(page)
            if "/dashboard" not in page.url:
                goto_with_backoff(page, self.settings.dashboard_url)
                settle(page)
        self.check_session(page)

    def open_index(self, page: Page) -> Page:
        """Click the 'IndEX Jobs' card and follow it into a popup, new tab, or same tab."""
        card = first_visible(
            [
                _css(page, self.selectors.index_card),
                page.get_by_role("link", name=INDEX_CARD_TEXT),
                page.get_by_role("button", name=INDEX_CARD_TEXT),
                page.locator("a, button, [role=link], [role=button], [onclick]").filter(
                    has_text=INDEX_CARD_TEXT
                ),
                page.get_by_text(INDEX_CARD_TEXT),
            ],
            timeout_ms=20000,
        )
        if card is None:
            shot = self.snapshot(page, "dashboard-no-index-card")
            raise LayoutChangedError(f"'IndEX Jobs' card not found on the dashboard. Evidence: {shot}")
        print("Opening IndEX Jobs...")
        target = click_and_follow(self.context, page, card, wait_ms=6000)
        print(f"  IndEX opened {'in a new tab/popup' if target is not page else 'in the same tab'}.")
        self.throttle.pause()
        return target

    # ------------------------------------------------------------ tier 2: IndEX login

    def board_ready(self, page: Page) -> bool:
        try:
            return page.get_by_text(self._board_ready).first.is_visible()
        except PlaywrightError:
            return False

    def _wait_for_board(self, page: Page, timeout_s: int) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.board_ready(page):
                return True
            page.wait_for_timeout(1000)
        return False

    def _manual_login(self, page: Page, reason: str) -> None:
        if not self.interactive:
            shot = self.snapshot(page, "index-login")
            raise LayoutChangedError(f"IndEX login: {reason}. Evidence: {shot}")
        print(f"  IndEX login: {reason}. Please finish logging in in the browser window...")
        if not self._wait_for_board(page, MANUAL_TIMEOUT_S):
            shot = self.snapshot(page, "index-login-timeout")
            raise LayoutChangedError(f"IndEX job board did not appear. Evidence: {shot}")

    def _select_user_type(self, page: Page) -> bool:
        student = re.compile(r"^\s*(student|étudiant\(e\)|étudiant)\s*$", re.IGNORECASE)
        select = first_visible(
            [
                _css(page, self.selectors.index_user_type),
                page.get_by_label(re.compile(r"user\s*type|type\s*d.utilisateur", re.IGNORECASE)),
            ],
            timeout_ms=3000,
        )
        if select is None:  # any <select> offering a "Student" option
            tagged = page.evaluate(
                """(re) => { const r = new RegExp(re, 'i');
                   for (const s of document.querySelectorAll('select'))
                     if (Array.from(s.options).some(o => r.test(o.text.trim()))) {
                       s.setAttribute('data-coop-usertype', '1'); return true; }
                   return false; }""",
                student.pattern,
            )
            if not tagged:
                return False
            select = page.locator('[data-coop-usertype="1"]')
        options = select.evaluate("s => Array.from(s.options).map(o => [o.value, o.text.trim()])")
        values = [v for v, text in options if student.match(text)]
        if not values:
            return False
        choose_select_values(select, values, multiple=False)
        settle(page)  # user type may trigger a postback that re-renders the form
        return True

    def login_index(self, page: Page) -> None:
        password_box = page.locator('input[type="password"]')
        state = first_visible([password_box, page.get_by_text(self._board_ready)], timeout_ms=20000)
        if self.board_ready(page):
            print("  Already logged in to IndEX.")
            return
        if state is None:
            self._manual_login(page, "login form not recognised")
            return

        print("Logging in to IndEX...")
        if not self._select_user_type(page):
            self._manual_login(page, "could not set User Type to 'Student'")
            return
        self.throttle.pause(0.5)

        username = first_visible(
            [
                _css(page, self.selectors.index_username),
                page.get_by_label(re.compile(r"user\s*name|username|e-?mail|user\s*id", re.IGNORECASE)),
                page.locator('input[name*="user" i]:not([type="hidden"]), input[type="email"]'),
            ],
            timeout_ms=5000,
        )
        if username is None or not self.settings.index_username:
            self._manual_login(page, "username field not found or INDEX_USERNAME/UOTTAWA_EMAIL unset")
            return
        username.fill(self.settings.index_username)
        self.throttle.pause(0.5)

        if not self.settings.password:
            self._manual_login(page, "UOTTAWA_PASSWORD is empty - type your password and click Login")
            return
        password_box.first.fill(self.settings.password)
        self.throttle.pause(0.5)

        button = first_visible(
            [
                _css(page, self.selectors.index_login_button),
                page.get_by_role("button", name=re.compile(r"^\s*(log\s*in|sign\s*in|connexion)\s*$", re.I)),
                page.locator('input[type="submit"][value*="log" i], input[type="submit"][value*="sign" i]'),
            ],
            timeout_ms=5000,
        )
        if button is None:
            self._manual_login(page, "Login button not found - click it yourself")
            return
        button.click()
        settle(page, timeout_ms=30000)
        if self._wait_for_board(page, 30):
            print("  IndEX login successful.")
            return
        # Never retry automatically: repeated bad passwords can lock the account.
        error = page.get_by_text(re.compile(self.selectors.index_login_error_regex, re.IGNORECASE))
        shot = self.snapshot(page, "index-login-failed")
        if error.first.is_visible():
            raise IndexLoginError(f"IndEX rejected the login (check credentials). Evidence: {shot}")
        raise LayoutChangedError(f"Job board ('Filter By') not visible after login. Evidence: {shot}")

    def open_job_board(self, page: Page) -> Page:
        self.open_dashboard(page)
        board = self.open_index(page)
        self.check_session(board)
        self.login_index(board)
        return board

    # ------------------------------------------------------------ filters + cards

    def _select_filter(self, page: Page, name: str, label_regex: str, wanted: str) -> None:
        candidates = page.evaluate(FIND_SELECTS_JS, label_regex)
        candidates.sort(key=lambda c: not re.match(label_regex, c["label"], re.IGNORECASE))
        for cand in candidates:
            values = match_options([(o["value"], o["text"]) for o in cand["options"]], wanted)
            if values:
                select = page.locator(f'[data-coop-filter="{cand["idx"]}"]')
                choose_select_values(select, values, cand["multiple"])
                texts = [o["text"] for o in cand["options"] if o["value"] in values]
                print(f"  {name}: {', '.join(texts)}")
                settle(page)  # cascading filters (faculty -> programs) may post back
                self.throttle.pause(0.6)
                return
        shot = self.snapshot(page, f"filter-{name}")
        seen = "; ".join(
            f"{c['label'] or '?'}: " + ", ".join(o["text"] for o in c["options"][:8]) for c in candidates
        )
        raise LayoutChangedError(
            f"No '{wanted}' option in a {name} filter (candidates: {seen or 'none'}). Evidence: {shot}"
        )

    def apply_filters(self, board: Page) -> None:
        print("Applying filters...")
        self._select_filter(board, "Term", r"term|session|trimestre", self.settings.term)
        self._select_filter(board, "Faculty", r"facult", self.settings.faculty)
        self._select_filter(board, "Program", r"program", self.settings.program)

        if self.settings.hide_applied:
            hide = first_visible(
                [
                    _css(board, self.selectors.hide_applied_checkbox),
                    board.get_by_label(re.compile(r"hide\s+jobs\s+marked\s+as\s+applied", re.IGNORECASE)),
                    board.locator(
                        # Not plain *="applied": every job card has an appliedJob_<id> checkbox.
                        'input[type="checkbox"][id*="hideapplied" i], '
                        'input[type="checkbox"][name*="hideapplied" i]'
                    ),
                ],
                timeout_ms=3000,
            )
            if hide is None:
                print("  'Hide Jobs Marked as Applied' not found - continuing without it.")
            elif not hide.is_checked():
                hide.check()
                print("  Hide Jobs Marked as Applied: on")
                settle(board)

        button = first_visible(
            [
                _css(board, self.selectors.filter_button),
                # IndEX: <button type="submit"><span class="glyphicon ..."></span> Filter</button>;
                # the icon-font glyph ends up in the accessible name, so don't anchor at ^.
                board.locator('form button[type="submit"]').filter(has_text=re.compile(r"\bfilter\b", re.I)),
                board.get_by_role("button", name=re.compile(r"(^|\W)(apply\s+)?filters?\s*$", re.IGNORECASE)),
                board.locator(
                    'input[type="submit"][value="Filter" i], input[type="button"][value="Filter" i]'
                ),
                board.get_by_role("link", name=re.compile(r"^\s*filter\s*$", re.IGNORECASE)),
            ],
            timeout_ms=5000,
        )
        if button is None:
            shot = self.snapshot(board, "filter-button")
            raise LayoutChangedError(f"'Filter' button not found. Evidence: {shot}")
        button.click()
        settle(board, timeout_ms=30000)
        self.check_session(board)
        self.throttle.pause()

    def scrape_cards(self, board: Page) -> list[JobCard]:
        raw = board.evaluate(SCRAPE_CARDS_JS, list(CARD_LABELS))
        cards, seen = [], set()
        for item in raw:
            card = parse_card(item)
            if card.key not in seen:
                seen.add(card.key)
                cards.append(card)
        return cards
