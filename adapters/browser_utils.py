"""Small Playwright helpers shared by the IndEX navigator and the applier."""

import time
from collections.abc import Sequence

from playwright.sync_api import BrowserContext, Locator, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from core.throttle import with_backoff


def first_visible(candidates: Sequence[Locator | None], timeout_ms: int = 15000) -> Locator | None:
    """First candidate locator that becomes visible within the timeout (checked in order)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for loc in candidates:
            if loc is None:
                continue
            try:
                if loc.first.is_visible():
                    return loc.first
            except PlaywrightError:
                pass  # page mid-navigation
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.4)


def settle(page: Page, timeout_ms: int = 15000) -> None:
    """Wait for the DOM and, when it happens, network idle (long-polling pages never idle)."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


def goto_with_backoff(page: Page, url: str) -> None:
    """Navigate, retrying timeouts and 5xx responses with exponential backoff + jitter."""

    def navigate() -> None:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response is not None and response.status >= 500:
            raise PlaywrightError(f"HTTP {response.status} from {url}")

    with_backoff(
        navigate,
        retry_on=(PlaywrightError,),
        on_retry=lambda n, exc, d: print(
            f"  Navigation failed ({str(exc).splitlines()[0]}); retry {n} in {d:.1f}s"
        ),
    )
    settle(page)


def click_and_follow(context: BrowserContext, page: Page, target: Locator, wait_ms: int = 3000) -> Page:
    """Click, then return whichever page shows the result: a new tab/popup or the same page."""
    before = list(context.pages)
    url_before = page.url
    target.click()
    deadline = time.monotonic() + wait_ms / 1000
    while time.monotonic() < deadline:
        opened = [p for p in context.pages if p not in before]
        if opened:
            settle(opened[-1])
            return opened[-1]
        if page.url != url_before:
            break
        page.wait_for_timeout(200)
    settle(page)
    return page


def choose_select_values(select: Locator, values: Sequence[str], multiple: bool) -> None:
    """Select option values on a native <select>, even when a custom widget hides it."""
    chosen = list(values) if multiple else list(values)[:1]
    if select.is_visible():
        select.select_option(value=chosen)
        return
    select.evaluate(
        """(s, vals) => {
            for (const o of s.options) o.selected = vals.includes(o.value);
            s.dispatchEvent(new Event('input', {bubbles: true}));
            s.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        chosen,
    )
