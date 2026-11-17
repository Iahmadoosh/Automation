# Job Application Assistant

[![CI](https://github.com/iahmadoosh/automation/actions/workflows/ci.yml/badge.svg)](https://github.com/iahmadoosh/automation/actions/workflows/ci.yml)

A job application assistant built with Python, Playwright, and a local Qwen model (via LM Studio).
It has two independent tools that share one LLM client:

- **uOttawa co-op assistant (`coop.py`)**: the main tool. It signs in to the Experiential
  Learning portal and IndEX, filters co-op listings, and fills application forms for you,
  Ottawa listings first.
- **LinkedIn job screener (`main.py`)**: an earlier prototype. It searches LinkedIn Easy Apply
  listings, has Qwen score each one against your resume, and opens Easy Apply on matches. It
  does not fill in or submit applications.

## Features
- **IndEX filtering**: Applies the term, faculty, program, and "hide applied" filters (Winter 2027
  Computer Science by default; set in `.env`), then groups listings into Ottawa and everywhere else.
- **Form filling**: Fills contact and profile fields from `data/profile.json`, picks or uploads your
  resume and transcript, and leaves anything uncertain (GPA, dropdowns, attestations) for you.
- **Screening answers**: Your local Qwen model writes free-text answers within each question's
  word or character limit and picks multiple-choice answers, grounded in your resume. It answers
  UNSURE rather than guessing.
- **Safe by default**: Preview mode never clicks Apply, the default dry run stops before Submit,
  and live mode asks you to confirm every application.
- **LinkedIn screening**: Turns your target role into search queries and rates each listing as a
  match or reject, with a reason.

## Tech Stack
- **Language**: Python 3.12+
- **Automation**: Playwright
- **LLM**: Local Qwen via LM Studio's REST API
- **Environment**: Virtual Environment (`venv`), Git/GitHub

## Architecture

```
main.py                 LinkedIn entry point (prototype)
coop.py                 uOttawa co-op entry point: login | inspect | run
adapters/
  linkedin.py           LinkedIn page actions
  uottawa_index.py      IndexNavigator: SSO -> dashboard -> IndEX Jobs card -> IndEX login,
                        sidebar filters, job-card scraping
  uottawa_coop.py       UOttawaCoopApplier: Ottawa-first loop, intermediary pages, form filling,
                        submit gate, search-expansion prompt
  browser_utils.py      first_visible / click_and_follow (popup, tab or same page) / backoff goto
  coop_parsing.py       Pure rules: cards, location grouping, filter option matching, field plans
  coop_selectors.py     Optional selector overrides (tried before built-in fallbacks)
engine/
  evaluator.py          Shared Qwen client (JobEvaluator.query) + LinkedIn match scoring
  resume.py             resume.pdf/.txt -> cached text split into education/skills/projects/experience
  screening.py          Qwen prompts for free-text answers and multiple-choice (radio) questions
core/
  config.py             .env -> frozen settings
  throttle.py           Jittered pauses, per-application rate limit, exponential backoff
  errors.py             ConfigError / SessionExpiredError / IndexLoginError / LayoutChangedError
```

Each platform owns its adapter and entry point and they share the `engine/` Qwen client.
Nothing in the LinkedIn flow imports the co-op code.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
playwright install chromium
copy .env.example .env                       # then edit it
copy data\profile.template.json data\profile.json   # then edit it
```

Put your resume at `assets/resume.pdf` (`data/` and the project root also work, as do `.txt`
resumes). Start LM Studio's local server with your Qwen model loaded.

## uOttawa co-op (Experiential Learning -> IndEX)

> Check that automating applications is allowed by the co-op office and the platform's terms
> of use before running in live mode. You stay responsible for every application submitted.

### How a run works
1. Opens `/dashboard`. If Microsoft SSO is required, it fills in your email and waits while you
   complete MFA.
2. Clicks the **IndEX Jobs** card and follows it into a popup, a new tab, or the same tab.
3. IndEX login: sets **User Type = Student**, fills the username and password, and clicks
   **Login**. If the password is empty or a field isn't recognised, it waits for you to finish
   in the browser. A rejected login is never retried, to avoid locking your account.
4. Filters: Term `2027, Winter`, Faculty `Engineering`, Program `Computer Science`,
   **Hide Jobs Marked as Applied**, then **Filter**.
5. Scrapes the job cards (title, organization, location, mobility, duration, programs) and
   splits them into **Ottawa** and **everything else**.
6. For each Ottawa listing it opens the posting and clicks through intermediary pages (Apply,
   Continue to Application, employer redirect notice). It then fills the form: profile fields,
   documents, Qwen free-text answers within their word/character limits, and Qwen multiple-choice
   answers (it answers UNSURE when your resume doesn't establish the answer).
7. Prompts: *"Finished applying to all Ottawa listings. Would you like to expand your search to
   listings outside Ottawa? (y/n)"*, then prints a summary.

### Commands
```powershell
python coop.py login                 # sign in once (SSO/MFA + IndEX); saves data/coop_auth.json
python coop.py run --preview         # filters + cards + posting pages only; never clicks Apply
python coop.py run                   # dry run (default): fill forms, stop before Submit
python coop.py run --live            # submit; asks "Submit this application? [y/N]" each time
python coop.py run --expand no       # don't ask about non-Ottawa listings (or --expand yes)
python coop.py run --max 20          # raise the per-run cap (default 5)
python coop.py inspect               # snapshot any pages to calibrate selectors
```

Run `--preview` first, then a dry run, before going live. Every run writes
`logs/coop/run-*.json`, and each posting gets a screenshot. When a step can't find what it
needs, it saves a screenshot and the page HTML to `logs/coop/error-*` and names that file in
the error message.

Never filled automatically: attestation checkboxes, unknown dropdowns, and short factual
fields (GPA, availability). They're listed as "needs you". Portal `confirm()` dialogs are
always dismissed, so a dialog can't submit anything without you.

### Calibrating
Every step tries an optional override from `data/coop_selectors.json` first, then built-in
role, label, and text fallbacks. If a step fails, use `python coop.py inspect` on that page and
set only the key you need (see `data/coop_selectors.template.json`).

### Pacing
Random 0.8–2.5 s pauses between actions, at least 90 s (+ jitter) between applications, at most 5
applications per run, and exponential backoff on navigation timeouts and 5xx responses. All of
these are set in `.env`.

## Tests & lint
```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check core adapters/browser_utils.py adapters/coop_*.py adapters/uottawa_*.py engine/resume.py engine/screening.py coop.py tests
```
`tests/test_coop_flow.py` drives the real applier through a fake two-tier portal (dashboard -> IndEX
popup -> login -> filters -> cards -> intermediary page -> form). It checks Ottawa-first ordering,
that dry runs never POST, that live runs submit only after confirmation, and that a bad password
is tried exactly once.

GitHub Actions (`.github/workflows/ci.yml`) runs the same lint, format, and test commands on every
push and pull request, with Chromium installed so the end-to-end flow tests run too.
