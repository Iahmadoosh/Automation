"""Tailored CV generator.

python cv.py check                         # check data/master_resume.yaml and render an untailored CV
python cv.py test-ai                       # check that the AI (LM Studio or Gemini) answers
python cv.py make --title "Software Developer Intern" --company "Acme" --job job.txt
python cv.py make --title "..." --company "..."       # paste the job description instead
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

from adapters.cv_pdf import make_pdf_renderer
from core.config import CV_PROVIDERS, CvSettings, load_cv_settings
from core.errors import ConfigError
from engine.cv import CvMaker, CvOutput, JobPosting, load_master
from engine.llm import GeminiLLM, LLMClient, LocalLLM


def make_llm(settings: CvSettings, provider: str | None = None) -> LLMClient | None:
    provider = provider or settings.provider
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise ConfigError("Set GEMINI_API_KEY in .env to use Gemini (free key at aistudio.google.com).")
        return GeminiLLM(settings.gemini_api_key, settings.gemini_model)
    if provider == "local":
        return LocalLLM(settings.llm_endpoint, settings.writing_model)
    return None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tailored CV generator")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Validate your master resume and render an untailored CV")

    test = sub.add_parser("test-ai", help="Check that the AI answers")
    test.add_argument("--provider", choices=CV_PROVIDERS[:2], help="Override CV_PROVIDER")

    make = sub.add_parser("make", help="Make a CV tailored to one job")
    make.add_argument("--title", required=True, help="Job title")
    make.add_argument("--company", required=True, help="Company name")
    make.add_argument("--job", type=Path, help="Text file with the job description (else paste it)")
    make.add_argument("--provider", choices=CV_PROVIDERS, help="Override CV_PROVIDER for this CV")
    make.add_argument(
        "--force", action="store_true", help="Regenerate even if today's CV for this job exists"
    )
    return parser.parse_args(argv)


def _read_pasted_description() -> str:
    print("Paste the job description, then type END on its own line and press Enter:")
    lines = []
    for line in sys.stdin:
        if line.strip() == "END":
            break
        lines.append(line.rstrip("\n"))
    return "\n".join(lines).strip()


def print_result(result: CvOutput) -> None:
    if result.reused:
        print(f"Already made today (use --force to redo): {result.folder}")
        return
    print(f"Written by: {result.source}")
    if result.pdf:
        print(f"PDF ({result.pages} page{'s' if result.pages != 1 else ''}): {result.pdf}")
    print(f"Folder: {result.folder}  (cv.html, cv.txt, report.json)")
    if result.warnings:
        print("Check these:")
        for warning in result.warnings:
            print(f"  - {warning}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = load_cv_settings()
        if args.command == "test-ai":
            llm = make_llm(settings, args.provider)
            print(f"Asking {llm.name} ...")
            reply = llm.query("Reply with one short sentence.", "Say hello to a co-op student.", timeout=120)
            print(f"Reply: {reply}" if reply else "No reply. See the error above.")
            return 0 if reply else 1

        master = load_master(settings.master_path)
        if args.command == "check":
            llm, job = None, JobPosting("Master resume", "untailored", "")
        else:
            description = args.job.read_text(encoding="utf-8") if args.job else _read_pasted_description()
            if not description:
                print("No job description given.")
                return 2
            llm, job = make_llm(settings, args.provider), JobPosting(args.title, args.company, description)
    except (ConfigError, OSError) as exc:
        print(f"Error: {exc}")
        return 2

    print(
        f"Resume: {master.name} - {len(master.experience)} experience, {len(master.projects)} projects, "
        f"{len(master.all_skills)} skills"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            maker = CvMaker(master, llm, settings.out_dir, make_pdf_renderer(browser), settings.max_projects)
            if llm:
                print(f"Tailoring with {llm.name} (a local model can take a minute or two) ...")
            print_result(maker.make(job, date.today(), force=args.command == "check" or args.force))
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
