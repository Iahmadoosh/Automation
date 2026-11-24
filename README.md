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
- **Tailored CVs**: Builds a one-page PDF CV for each job from your master resume, using your local
  model or Gemini. It can only select, reorder and reword what you wrote, and it checks every line.
- **Daily summary on your phone**: Sends the day's results to a Telegram bot.
- **LinkedIn screening**: Turns your target role into search queries and rates each listing as a
  match or reject, with a reason.

## Tech Stack
- **Language**: Python 3.12+
- **Automation**: Playwright
- **LLM**: Local Qwen via LM Studio's REST API, optional Google Gemini API for CVs
- **Environment**: Virtual Environment (`venv`), Git/GitHub

## Architecture

```
main.py                 LinkedIn entry point (prototype)
coop.py                 uOttawa co-op entry point: login | inspect | run | summary | notify-test
cv.py                   Tailored CV generator: check | test-ai | make
adapters/
  linkedin.py           LinkedIn page actions
  uottawa_index.py      IndexNavigator: SSO -> dashboard -> IndEX Jobs card -> IndEX login,
                        sidebar filters, job-card scraping
  uottawa_coop.py       UOttawaCoopApplier: Ottawa-first loop, intermediary pages, form filling,
                        submit gate, search-expansion prompt
  browser_utils.py      first_visible / click_and_follow (popup, tab or same page) / backoff goto
  coop_parsing.py       Pure rules: cards, location grouping, filter option matching, field plans
  coop_selectors.py     Optional selector overrides (tried before built-in fallbacks)
  cv_pdf.py             CV HTML -> PDF with headless Chromium
engine/
  evaluator.py          Shared Qwen client (JobEvaluator.query) + LinkedIn match scoring
  resume.py             resume.pdf/.txt -> cached text split into education/skills/projects/experience
  screening.py          Qwen prompts for free-text answers and multiple-choice (radio) questions
  llm.py                LM Studio and Gemini clients behind one query() interface
  cv.py                 Master resume -> tailored CV (selection, rewording, fact checks, layout)
core/
  config.py             .env -> frozen settings
  throttle.py           Jittered pauses, per-application rate limit, exponential backoff
  notify.py             Telegram bot messages to your phone
  summary.py            Daily summary built from the run logs
  errors.py             ConfigError / SessionExpiredError / IndexLoginError / LayoutChangedError / NotifyError
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
copy data\master_resume.template.yaml data\master_resume.yaml   # then fill in (for CVs)
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
python coop.py run --cvs             # also make a tailored CV for each posting (see below)
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

## Tailored CVs

`cv.py` makes a one-page PDF CV for a specific job from `data/master_resume.yaml`, your master
resume with everything you could ever list. For each job it picks the most relevant experience,
projects and skills, puts them in the best order, rewords bullets toward the job, and writes a
short summary.

**It never makes things up.** Names, employers, titles, dates and links always come from your
master resume. Every reworded line is checked, and is replaced by your original wording (with a
warning) if it:
- contains a number that isn't in that item in your master resume (no invented metrics);
- names a tool or technology from the job posting that isn't anywhere in your resume;
- lists a skill you didn't list.

If the AI isn't running or gives an unusable answer, the CV is still made by keyword matching,
using your exact wording.

### Setup
1. `copy data\master_resume.template.yaml data\master_resume.yaml` and fill it in. Put in more than
   fits on one page; the generator chooses. It's git-ignored.
2. Check it, which also renders an untailored CV so you can see the layout:
   ```powershell
   python cv.py check
   ```
3. Choose who writes the CVs in `.env`:
   - `CV_PROVIDER=local`: your LM Studio model (`LLM_WRITING_MODEL`, or `LLM_MODEL` if empty).
     Bigger models write better; on an 8 GB GPU with 64 GB RAM, `qwen3-30b-a3b` is a good choice.
   - `CV_PROVIDER=gemini`: needs `GEMINI_API_KEY`, free from aistudio.google.com (a Gemini app
     subscription doesn't include API access). On the free tier Google may use prompts to improve
     its products.
   - `CV_PROVIDER=none`: keyword matching only, no AI.
4. Check the AI answers: `python cv.py test-ai` (or `python cv.py test-ai --provider gemini`).

### Commands
```powershell
python cv.py make --title "Software Developer Intern" --company "Acme" --job job.txt
python cv.py make --title "..." --company "..."        # paste the description, then type END
python cv.py make ... --provider gemini                # override CV_PROVIDER for one CV
python cv.py make ... --force                          # redo today's CV for this job
python coop.py run --cvs                               # one CV per IndEX posting the run opens
```

Each CV goes in `output/cvs/<date>-<company>-<title>/`: `cv.pdf`, `cv.html`, `cv.txt` (plain text
for pasting into forms), `job.txt` and `report.json` (which model wrote it, and any warnings). If it
runs over one page, the least relevant lines are removed until it fits. **Read every CV before you
send it.** IndEX attaches documents you've uploaded to the portal, so upload the tailored PDF there
when you want to use it. The daily summary counts how many CVs were made.

## Daily summary on your phone (Telegram)

`python coop.py summary` collects the day's run logs and sends a summary to your phone through a
Telegram bot: how many applications were submitted, what needs you (fields left for you, PDF
postings), deadlines in the next 3 days, errors, and how many referral contacts were found. Each
drafted referral message arrives as its own Telegram message, ready to copy into LinkedIn.

### Setup
1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts. It gives you a token.
2. Open your bot's chat (`t.me/<your_bot_username>`), tap **Start**, and send it any message.
3. Get your chat ID from PowerShell (paste as one line, replacing `<TOKEN>`):
   ```powershell
   $token = "<TOKEN>"; $u = Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates"; "TELEGRAM_CHAT_ID=" + $u.result[-1].message.chat.id
   ```
4. Add both values to `.env`, then check the setup:
   ```powershell
   python coop.py notify-test
   ```

### Commands
```powershell
python coop.py summary                    # send today's summary
python coop.py summary --print            # show it in the terminal instead of sending
python coop.py summary --date 2026-11-24  # summarise another day
```

### Send it automatically every evening
Windows Task Scheduler (run once in PowerShell; adjust the project path and the time):
```powershell
$dir = "$HOME\Automation"
$action = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\python.exe" -Argument "coop.py summary" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 9pm
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName "Job hunt summary" -Action $action -Trigger $trigger -Settings $settings
```
`-StartWhenAvailable` sends a missed summary as soon as the PC is back on.

Referral drafts are read from `logs/outreach/drafts-YYYYMMDD.json` (a list of objects with `name`,
`company`, `profile_url` and `message`). The outreach tool that writes them is still to come, so
until then the summary reports 0 connections.

## Tests & lint
```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check core adapters/browser_utils.py adapters/coop_*.py adapters/uottawa_*.py adapters/cv_pdf.py engine/resume.py engine/screening.py engine/cv.py engine/llm.py coop.py cv.py tests
```
`tests/test_coop_flow.py` drives the real applier through a fake two-tier portal (dashboard -> IndEX
popup -> login -> filters -> cards -> intermediary page -> form). It checks Ottawa-first ordering,
that dry runs never POST, that live runs submit only after confirmation, and that a bad password
is tried exactly once.

GitHub Actions (`.github/workflows/ci.yml`) runs the same lint, format, and test commands on every
push and pull request, with Chromium installed so the end-to-end flow tests run too.
