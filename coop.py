"""uOttawa co-op (IndEX) application assistant.

python coop.py login                  # SSO/MFA + IndEX login once, saves the session
python coop.py inspect [URL]          # snapshot pages to calibrate selectors
python coop.py run --preview          # filters + listings + posting pages only; no Apply clicks
python coop.py run                    # dry run (default): fill forms, stop before Submit
python coop.py run --live             # submit, with a confirmation per application
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from adapters.coop_selectors import load_selectors
from adapters.uottawa_coop import (
    ENUMERATE_FIELDS_JS,
    EXTRACT_POSTING_JS,
    ApplicationResult,
    UOttawaCoopApplier,
)
from core.config import CoopSettings, load_coop_settings, load_profile
from core.errors import ConfigError, IndexLoginError, LayoutChangedError, SessionExpiredError
from engine.evaluator import JobEvaluator
from engine.resume import load_resume
from engine.screening import ScreeningAnswerer


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="uOttawa co-op (IndEX) application assistant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="Sign in (SSO/MFA + IndEX) once and save the session")

    inspect = sub.add_parser("inspect", help="Capture page structure to calibrate selectors")
    inspect.add_argument("url", nargs="?", help="Start URL (defaults to the dashboard)")

    run = sub.add_parser("run", help="Filter IndEX listings and fill applications, Ottawa first")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Fill everything, stop before Submit (default)")
    mode.add_argument("--live", action="store_true", help="Actually submit applications")
    mode.add_argument("--preview", action="store_true", help="Open postings only; never click Apply")
    run.add_argument("--max", type=int, help="Max applications this run (default COOP_MAX_APPLICATIONS)")
    run.add_argument(
        "--expand",
        choices=("ask", "yes", "no"),
        default="ask",
        help="After Ottawa: ask (default), or always/never continue with listings elsewhere",
    )
    run.add_argument(
        "--yes",
        action="store_true",
        help="Live mode: skip the per-application prompt when no required field needs you",
    )
    return parser.parse_args(argv)


def _snapshot(page: Page, out_dir: Path, n: int) -> None:
    target = out_dir / f"{n:02d}"
    target.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(target / "page.png"), full_page=True)
    (target / "page.html").write_text(page.content(), encoding="utf-8")
    controls = page.evaluate(
        """() => Array.from(document.querySelectorAll('a, button, input[type=submit]'))
             .filter(e => e.offsetParent !== null)
             .map(e => ({tag: e.tagName.toLowerCase(),
                         text: (e.innerText || e.value || '').trim().slice(0, 80),
                         href: e.getAttribute('href') || '', id: e.id, cls: e.className}))
             .filter(e => e.text)"""
    )
    summary = {
        "url": page.url,
        "posting_pairs": page.evaluate(EXTRACT_POSTING_JS)["pairs"],
        "form_fields": page.evaluate(ENUMERATE_FIELDS_JS, ""),
        "controls": controls,
    }
    (target / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Saved {target} ({len(summary['form_fields'])} fields, {len(controls)} controls)")


def _inspect(page: Page, settings: CoopSettings, url: str | None) -> None:
    out_dir = settings.log_dir / f"inspect-{datetime.now():%Y%m%d-%H%M%S}"
    page.goto(url or settings.dashboard_url, wait_until="domcontentloaded")
    print("Navigate in the browser and press Enter to snapshot each page. Nothing is clicked")
    print("or submitted for you. Type q to quit.")
    n = 1
    while input(f"[{n}] Enter = snapshot, q = quit: ").strip().lower() != "q":
        _snapshot(page.context.pages[-1], out_dir, n)
        n += 1
    print(f"Snapshots in {out_dir}. They contain personal data; they're git-ignored.")


def _report(settings: CoopSettings, results: list[ApplicationResult], label: str) -> None:
    path = settings.log_dir / f"run-{label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(
        json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\n" + "=" * 60 + "\nSummary")
    for group in dict.fromkeys(r.group for r in results):
        counts: dict[str, int] = {}
        for r in results:
            if r.group == group:
                counts[r.status.value] = counts.get(r.status.value, 0) + 1
        print(f"  {group}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if not results:
        print("  No listings processed.")
    print(f"Report: {path}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = load_coop_settings()
        selectors = load_selectors(settings.selectors_path)
        profile = load_profile(settings.profile_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2

    live = getattr(args, "live", False)
    preview = getattr(args, "preview", False)
    label = "live" if live else "preview" if preview else "dry"
    if args.command == "run":
        print("=" * 60)
        print(
            {
                "live": "LIVE MODE: applications WILL be submitted (you confirm each one).",
                "preview": "PREVIEW: filters + listings + posting pages only. No Apply clicks.",
                "dry": "DRY RUN: forms are filled, nothing is submitted.",
            }[label]
        )
        print("=" * 60)

    resume = load_resume(settings.resume_candidates)  # parsed once, cached for every answer
    evaluator = JobEvaluator(profile, None, settings.llm_endpoint, settings.llm_model)
    answerer = ScreeningAnswerer(evaluator, resume.for_prompt(), profile, settings.llm_answer_timeout)

    with sync_playwright() as p:
        headless = settings.headless and args.command == "run"
        browser = p.chromium.launch(headless=headless, slow_mo=50)
        state = str(settings.auth_path) if settings.auth_path.exists() else None
        context = browser.new_context(storage_state=state, viewport={"width": 1366, "height": 900})
        page = context.new_page()
        bot = UOttawaCoopApplier(
            context, page, settings, selectors, profile, answerer, interactive=not headless
        )
        try:
            if args.command == "login":
                bot.login()
                return 0
            if args.command == "inspect":
                _inspect(page, settings, args.url)
                return 0
            results = bot.run(
                dry_run=not live,
                max_applications=args.max or settings.max_applications,
                auto_confirm=args.yes,
                expand=args.expand,
                preview=preview,
            )
            bot.save_session()  # refresh rolling SSO cookies
            _report(settings, results, label)
            return 0
        except SessionExpiredError as exc:
            print(f"\nSession expired: {exc}")
            return 3
        except IndexLoginError as exc:
            print(f"\nIndEX login failed: {exc}")
            return 3
        except LayoutChangedError as exc:
            print(f"\nPortal layout mismatch: {exc}")
            return 4
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
