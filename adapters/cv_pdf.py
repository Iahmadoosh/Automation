"""Print a CV's HTML to PDF with Playwright's Chromium (the browser the project already uses)."""

from pathlib import Path

from playwright.sync_api import Browser
from pypdf import PdfReader


def make_pdf_renderer(browser: Browser):
    """Return render(html, path) -> page count, reusing one tab of `browser`."""
    page = browser.new_page()

    def render(html: str, path: Path) -> int:
        page.set_content(html, wait_until="load")
        page.pdf(path=str(path), format="Letter", print_background=True, prefer_css_page_size=True)
        return len(PdfReader(str(path)).pages)

    return render
