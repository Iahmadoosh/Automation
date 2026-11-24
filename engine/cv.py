"""Tailored CVs: pick and reword items from your master resume for one job, then lay them out.

The model may only select, reorder and reword. Names, employers, titles, dates and links always
come from the master resume, and every reworded line is checked:
  - numbers (metrics, percentages, years) must already appear in that item in the master resume;
  - technical terms copied from the job posting must already appear somewhere in your resume;
  - skills must be ones listed in the master resume.
Anything that fails a check is replaced by your original wording and reported as a warning.
"""

import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.errors import ConfigError
from engine.llm import LLMClient, extract_json

MAX_BULLET_CHARS = 240
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# Tech-looking tokens: words with a capital letter, digit or symbol (React, AWS, C++, .NET, Node.js, S3).
_TERM = re.compile(r"(?<![\w.])(?:\.?[A-Za-z][\w+#.]*[\w+#]|[A-Z])(?![\w])")
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*")
_STOP_WORDS = (
    "a an and are as at be by for from has have in into is it of on or our the to we will with you your "
    "this that their they who what us all any can may must should able work team role job"
)
_STOP = frozenset(_STOP_WORDS.split())


# ----------------------------------------------------------------------------- master resume


@dataclass(frozen=True)
class Item:
    """One experience or project entry."""

    id: str
    heading: str  # job title or project name
    subheading: str  # company, or project tech stack
    location: str
    dates: str
    link: str
    bullets: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join((self.heading, self.subheading, *self.bullets))


@dataclass(frozen=True)
class MasterResume:
    name: str
    contact: tuple[str, ...]
    summary: str
    education: tuple[Mapping, ...]
    skills: Mapping[str, tuple[str, ...]]
    experience: tuple[Item, ...]
    projects: tuple[Item, ...]
    extras: Mapping[str, tuple[str, ...]]

    @property
    def all_items(self) -> tuple[Item, ...]:
        return self.experience + self.projects

    @property
    def all_skills(self) -> tuple[str, ...]:
        return tuple(s for group in self.skills.values() for s in group)

    @property
    def full_text(self) -> str:
        edu = " ".join(
            " ".join(str(v) for v in e.values() if not isinstance(v, list)) for e in self.education
        )
        details = " ".join(str(d) for e in self.education for d in e.get("details", []))
        extras = " ".join(" ".join(v) for v in self.extras.values())
        items = " ".join(i.text for i in self.all_items)
        return " ".join((self.summary, edu, details, " ".join(self.all_skills), items, extras))


def _dates(entry: Mapping) -> str:
    start, end = str(entry.get("start", "")).strip(), str(entry.get("end", "")).strip()
    return f"{start} - {end}" if start and end else start or end


def _strings(value, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str | int | float) for v in value):
        raise ConfigError(f"{where} must be a list of text lines.")
    return tuple(str(v).strip() for v in value if str(v).strip())


def _items(raw, section: str) -> tuple[Item, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"'{section}' must be a list.")
    items, seen = [], set()
    for n, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise ConfigError(f"{section} entry #{n} must be a set of fields (title:, bullets:, ...).")
        if section == "experience":
            heading, sub = entry.get("title", ""), entry.get("company", "")
        else:
            heading = entry.get("name", "")
            tech = entry.get("tech", "")
            sub = ", ".join(tech) if isinstance(tech, list) else str(tech or "")
        if not str(heading).strip():
            raise ConfigError(
                f"{section} entry #{n} needs a {'title' if section == 'experience' else 'name'}."
            )
        item_id = str(entry.get("id") or re.sub(r"\W+", "-", str(heading).lower()).strip("-"))
        if item_id in seen:
            raise ConfigError(f"Two {section} entries share the id '{item_id}'; give them unique ids.")
        seen.add(item_id)
        items.append(
            Item(
                id=item_id,
                heading=str(heading).strip(),
                subheading=str(sub).strip(),
                location=str(entry.get("location", "")).strip(),
                dates=_dates(entry),
                link=str(entry.get("link", "")).strip(),
                bullets=_strings(entry.get("bullets"), f"{section} '{heading}' bullets"),
            )
        )
    return tuple(items)


def parse_master(data: Mapping) -> MasterResume:
    if not isinstance(data, Mapping) or not str(data.get("name", "")).strip():
        raise ConfigError("The master resume needs at least a 'name:' line.")
    contact_raw = data.get("contact") or {}
    if not isinstance(contact_raw, Mapping):
        raise ConfigError("'contact' must be a set of fields (email:, phone:, ...).")
    order = ("email", "phone", "location", "linkedin", "github", "portfolio")
    contact = tuple(
        str(contact_raw[k]).strip()
        for k in (*order, *(k for k in contact_raw if k not in order))
        if str(contact_raw.get(k) or "").strip()
    )
    skills_raw = data.get("skills") or {}
    if isinstance(skills_raw, list):
        skills_raw = {"Skills": skills_raw}
    if not isinstance(skills_raw, Mapping):
        raise ConfigError("'skills' must be groups like 'Languages: [Python, Java]'.")
    education = data.get("education") or []
    if not isinstance(education, list) or not all(isinstance(e, Mapping) for e in education):
        raise ConfigError("'education' must be a list of schools.")
    extras_raw = data.get("extras") or {}
    if not isinstance(extras_raw, Mapping):
        raise ConfigError("'extras' must be sections like 'Volunteering: [...]'.")
    return MasterResume(
        name=str(data["name"]).strip(),
        contact=contact,
        summary=str(data.get("summary") or "").strip(),
        education=tuple(education),
        skills={str(k): _strings(v, f"skills '{k}'") for k, v in skills_raw.items()},
        experience=_items(data.get("experience"), "experience"),
        projects=_items(data.get("projects"), "projects"),
        extras={str(k): _strings(v, f"extras '{k}'") for k, v in extras_raw.items()},
    )


def load_master(path: Path) -> MasterResume:
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy data/master_resume.template.yaml to {path.name} and fill it in."
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise ConfigError(f"{path.name} has a formatting mistake{where}: {exc}") from None
    return parse_master(data or {})


# ----------------------------------------------------------------------------- job + result


@dataclass(frozen=True)
class JobPosting:
    title: str
    company: str
    description: str


@dataclass
class TailoredCV:
    summary: str
    skills: dict[str, list[str]]
    experience: list[tuple[Item, list[str]]]
    projects: list[tuple[Item, list[str]]]
    source: str  # which model wrote it, or "keyword match"
    warnings: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- fact checks


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def _words(text: str) -> set[str]:
    return {w.strip(".") for w in _WORD.findall(text.lower())}


def _new_terms(line: str, allowed_text: str, job_text: str) -> list[str]:
    """Tech-looking terms in `line` that come from the job posting but not from your resume."""
    allowed, job = _words(allowed_text), _words(job_text)
    terms = []
    for i, match in enumerate(_TERM.finditer(line)):
        token = match.group(0)
        low = token.lower().strip(".")
        if token == token.lower() and not any(c.isdigit() or c in "+#." for c in token):
            continue  # plain lowercase words aren't tool names
        if i == 0 and token[:1].isupper() and token[1:].islower():
            continue  # sentence-initial capital ("Built", "Led")
        if low in _STOP or len(low) < 2:
            continue
        if low in job and low not in allowed:
            terms.append(token)
    return terms


def check_line(line: str, original_text: str, master: MasterResume, job: JobPosting) -> str | None:
    """Why a reworded line is not allowed, or None if it passes."""
    # The job's own title and company may be named (e.g. "Winter 2027 Developer Intern at Acme").
    named = f"{job.title} {job.company}"
    invented = _numbers(line) - _numbers(original_text) - _numbers(named)
    if invented:
        return f"new number(s) {', '.join(sorted(invented))}"
    copied = _new_terms(line, f"{master.full_text} {named}", job.description)
    if copied:
        return f"claims {', '.join(copied)} from the job posting, which isn't in your resume"
    return None


# ----------------------------------------------------------------------------- keyword fallback


def _relevance(text: str, job_words: set[str]) -> int:
    return len((_words(text) - _STOP) & job_words)


def keyword_tailor(master: MasterResume, job: JobPosting, max_projects: int = 3) -> TailoredCV:
    """No-AI tailoring: reorder by keyword overlap with the job, keeping your exact wording."""
    job_words = _words(f"{job.title} {job.description}") - _STOP

    def order(items: Sequence[Item]) -> list[Item]:
        return sorted(items, key=lambda it: -_relevance(it.text, job_words))

    def bullets(item: Item) -> list[str]:
        return sorted(item.bullets, key=lambda b: -_relevance(b, job_words))

    skills = {
        group: sorted(values, key=lambda s: (s.lower() not in job.description.lower(), values.index(s)))
        for group, values in master.skills.items()
    }
    return TailoredCV(
        summary=master.summary,
        skills=skills,
        experience=[(it, bullets(it)) for it in master.experience],  # keep chronological order
        projects=[(it, bullets(it)) for it in order(master.projects)[:max_projects]],
        source="keyword match",
    )


# ----------------------------------------------------------------------------- AI tailoring

SYSTEM_PROMPT = (
    "You tailor a student's resume to one job posting. You may only SELECT, REORDER and REWORD "
    "content that is already in the master resume. Never add employers, titles, dates, numbers, "
    "metrics, tools, languages, certifications or achievements that are not in the master resume. "
    "If the job asks for something the candidate doesn't have, leave it out. Use strong action "
    "verbs and the job's vocabulary only where it truthfully describes what the candidate did. "
    "Reply with only a JSON object."
)


def _master_for_prompt(master: MasterResume) -> str:
    lines = [f"SUMMARY: {master.summary or '(none)'}", "SKILLS:"]
    lines += [f"  {group}: {', '.join(values)}" for group, values in master.skills.items()]
    for section, items in (("EXPERIENCE", master.experience), ("PROJECTS", master.projects)):
        lines.append(f"{section}:")
        for it in items:
            lines.append(f"  [id={it.id}] {it.heading} | {it.subheading} | {it.dates}")
            lines += [f"    - {b}" for b in it.bullets]
    return "\n".join(lines)


def build_cv_request(master: MasterResume, job: JobPosting, max_projects: int = 3) -> tuple[str, str]:
    user = f"""Job posting:
Title: {job.title}
Company: {job.company}
Description:
{job.description[:6000]}

Master resume:
{_master_for_prompt(master)}

Return JSON exactly like:
{{
  "summary": "2 sentences, first person implied (no 'I'), tailored to this job, using only facts above",
  "skills": {{"<existing group name>": ["<skills from that group, most relevant first>"]}},
  "experience": [{{"id": "<id>", "bullets": ["<reworded bullet>", "..."]}}],
  "projects": [{{"id": "<id>", "bullets": ["<reworded bullet>", "..."]}}]
}}
Rules: include every experience id (most relevant bullets first, at most 4 bullets each);
choose the {max_projects} most relevant projects (at most 3 bullets each); every bullet under
{MAX_BULLET_CHARS} characters; keep every number exactly as written in the master resume."""
    return SYSTEM_PROMPT, user


def _apply_bullets(
    item: Item, proposed, master: MasterResume, job: JobPosting, warnings: list[str]
) -> list[str]:
    if not isinstance(proposed, list) or not proposed:
        return list(item.bullets)
    original = " ".join((item.text, master.summary))
    kept = []
    for line in proposed:
        if not isinstance(line, str) or not line.strip():
            continue
        line = re.sub(r"^[-*•]\s*", "", line.strip())[:MAX_BULLET_CHARS]
        problem = check_line(line, original, master, job)
        if problem:
            warnings.append(f"{item.heading}: dropped a reworded bullet ({problem}): {line!r}")
            continue
        kept.append(line)
    if not kept:
        warnings.append(f"{item.heading}: used your original bullets (no reworded bullet passed checks)")
        return list(item.bullets)
    return kept


def tailor(master: MasterResume, job: JobPosting, llm: LLMClient | None, max_projects: int = 3) -> TailoredCV:
    """Tailor with the model, falling back to keyword matching if it's unavailable or unusable."""
    fallback = keyword_tailor(master, job, max_projects)
    if llm is None:
        return fallback
    system, user = build_cv_request(master, job, max_projects)
    data = extract_json(llm.query(system, user, timeout=300))
    if not data:
        fallback.warnings.append(f"{llm.name} gave no usable answer; used keyword matching instead.")
        return fallback

    warnings: list[str] = []
    by_id = {it.id: it for it in master.all_items}

    summary = str(data.get("summary") or "").strip()
    if summary:
        problem = check_line(summary, master.full_text, master, job)
        if problem:
            warnings.append(f"Summary replaced with yours ({problem}).")
            summary = master.summary

    skills: dict[str, list[str]] = {}
    proposed_skills = data.get("skills") if isinstance(data.get("skills"), Mapping) else {}
    for group, values in master.skills.items():
        lookup = {v.lower(): v for v in values}
        picked = [
            lookup[str(s).lower()] for s in proposed_skills.get(group, []) or [] if str(s).lower() in lookup
        ]
        invented = [str(s) for s in proposed_skills.get(group, []) or [] if str(s).lower() not in lookup]
        if invented:
            warnings.append(f"Skills: ignored {', '.join(invented)} (not in your '{group}' list).")
        picked = list(dict.fromkeys(picked))
        skills[group] = picked + [v for v in values if v not in picked]  # keep everything you listed

    def section(
        key: str, allowed: Sequence[Item], keep_all: bool, limit: int | None
    ) -> list[tuple[Item, list[str]]]:
        chosen: list[tuple[Item, list[str]]] = []
        for entry in data.get(key) or []:
            if not isinstance(entry, Mapping):
                continue
            item = by_id.get(str(entry.get("id", "")))
            if item is None or item not in allowed or any(c[0] is item for c in chosen):
                continue
            chosen.append((item, _apply_bullets(item, entry.get("bullets"), master, job, warnings)))
        if keep_all:
            chosen += [(it, list(it.bullets)) for it in allowed if not any(c[0] is it for c in chosen)]
        if not chosen:
            return [pair for pair in (fallback.projects if key == "projects" else fallback.experience)]
        return chosen[:limit] if limit else chosen

    return TailoredCV(
        summary=summary or master.summary,
        skills=skills,
        experience=section("experience", master.experience, keep_all=True, limit=None),
        projects=section("projects", master.projects, keep_all=False, limit=max_projects),
        source=llm.name,
        warnings=warnings,
    )


def shorten(cv: TailoredCV) -> bool:
    """Remove the least important line to help the CV fit one page. False when nothing is left to cut."""
    for _, bullets in reversed(cv.projects):
        if len(bullets) > 1:
            bullets.pop()
            return True
    if len(cv.projects) > 1:
        cv.projects.pop()
        return True
    for _, bullets in reversed(cv.experience):
        if len(bullets) > 2:
            bullets.pop()
            return True
    if cv.summary:
        cv.summary = ""
        return True
    return False


# ----------------------------------------------------------------------------- HTML layout

CSS = """
@page { size: Letter; margin: 0.5in 0.55in; }
* { box-sizing: border-box; }
body { font-family: Calibri, Carlito, "Segoe UI", Arial, sans-serif; font-size: 10.5pt; color: #111;
       line-height: 1.25; margin: 0; }
h1 { font-size: 20pt; margin: 0; text-align: center; letter-spacing: .5px; }
.contact { text-align: center; font-size: 9.5pt; margin: 2px 0 6px; }
.contact a { color: #111; text-decoration: none; }
h2 { font-size: 11pt; text-transform: uppercase; letter-spacing: .8px; border-bottom: 1px solid #333;
     margin: 9px 0 4px; padding-bottom: 1px; }
.row { display: flex; justify-content: space-between; gap: 12px; }
.row .left { font-weight: bold; }
.row .right { white-space: nowrap; }
.sub { display: flex; justify-content: space-between; font-style: italic; }
.entry { margin-bottom: 5px; break-inside: avoid; }
ul { margin: 2px 0 0 0; padding-left: 16px; }
li { margin: 1px 0; }
p { margin: 0; }
.skills p { margin: 1px 0; }
"""


def _e(text: str) -> str:
    return html.escape(str(text), quote=True)


def _contact_html(contact: Sequence[str]) -> str:
    parts = []
    for c in contact:
        if c.startswith(("http://", "https://")):
            label = re.sub(r"^https?://(www\.)?", "", c).rstrip("/")
            parts.append(f'<a href="{_e(c)}">{_e(label)}</a>')
        elif "@" in c and " " not in c:
            parts.append(f'<a href="mailto:{_e(c)}">{_e(c)}</a>')
        else:
            parts.append(_e(c))
    return " &nbsp;|&nbsp; ".join(parts)


def _entry_html(item: Item, bullets: Sequence[str], project: bool) -> str:
    heading = _e(item.heading)
    if project and item.link:
        heading = f'<a href="{_e(item.link)}" style="color:#111">{heading}</a>'
    if project and item.subheading:
        heading += f' <span style="font-weight:normal">| <i>{_e(item.subheading)}</i></span>'
    out = [
        f'<div class="entry"><div class="row"><span class="left">{heading}</span>'
        f'<span class="right">{_e(item.dates)}</span></div>'
    ]
    if not project and (item.subheading or item.location):
        out.append(
            f'<div class="sub"><span>{_e(item.subheading)}</span><span>{_e(item.location)}</span></div>'
        )
    if bullets:
        out.append("<ul>" + "".join(f"<li>{_e(b)}</li>" for b in bullets) + "</ul>")
    out.append("</div>")
    return "".join(out)


def render_html(master: MasterResume, cv: TailoredCV) -> str:
    body = [f"<h1>{_e(master.name)}</h1>", f'<div class="contact">{_contact_html(master.contact)}</div>']
    if cv.summary:
        body += ["<h2>Summary</h2>", f"<p>{_e(cv.summary)}</p>"]
    if master.education:
        body.append("<h2>Education</h2>")
        for edu in master.education:
            body.append(
                f'<div class="entry"><div class="row"><span class="left">{_e(edu.get("school", ""))}</span>'
                f'<span class="right">{_e(edu.get("location", ""))}</span></div>'
                f'<div class="sub"><span>{_e(edu.get("degree", ""))}</span>'
                f"<span>{_e(_dates(edu))}</span></div>"
            )
            details = [str(d) for d in edu.get("details", []) or []]
            if details:
                body.append("<ul>" + "".join(f"<li>{_e(d)}</li>" for d in details) + "</ul>")
            body.append("</div>")
    if any(cv.skills.values()):
        body.append('<h2>Technical Skills</h2><div class="skills">')
        body += [f"<p><b>{_e(g)}:</b> {_e(', '.join(v))}</p>" for g, v in cv.skills.items() if v]
        body.append("</div>")
    if cv.experience:
        body.append("<h2>Experience</h2>")
        body += [_entry_html(it, b, project=False) for it, b in cv.experience]
    if cv.projects:
        body.append("<h2>Projects</h2>")
        body += [_entry_html(it, b, project=True) for it, b in cv.projects]
    for section, lines in master.extras.items():
        if lines:
            body.append(f"<h2>{_e(section)}</h2><ul>" + "".join(f"<li>{_e(x)}</li>" for x in lines) + "</ul>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{_e(master.name)} - Resume</title><style>{CSS}</style></head>"
        f"<body>{''.join(body)}</body></html>"
    )


def cv_text(master: MasterResume, cv: TailoredCV) -> str:
    """Plain-text version, handy for pasting into application text boxes."""
    lines = [master.name, " | ".join(master.contact), ""]
    if cv.summary:
        lines += ["SUMMARY", cv.summary, ""]
    lines.append("SKILLS")
    lines += [f"{g}: {', '.join(v)}" for g, v in cv.skills.items() if v]
    for title, entries in (("EXPERIENCE", cv.experience), ("PROJECTS", cv.projects)):
        if entries:
            lines += ["", title]
            for it, bullets in entries:
                head = " - ".join(filter(None, (it.heading, it.subheading)))
                lines.append(f"{head} ({it.dates})" if it.dates else head)
                lines += [f"  - {b}" for b in bullets]
    return "\n".join(lines).strip() + "\n"


# ----------------------------------------------------------------------------- output files

MAX_SHORTEN_STEPS = 15


@dataclass
class CvOutput:
    folder: Path
    pdf: Path | None
    pages: int
    source: str
    warnings: list[str]
    reused: bool = False


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:limit] or "job"


class CvMaker:
    """Tailors, lays out and saves one CV per job under out_dir/<date>-<company>-<title>/."""

    def __init__(
        self,
        master: MasterResume,
        llm: LLMClient | None,
        out_dir: Path,
        render_pdf=None,  # Callable[[str, Path], int]: html -> pdf file, returns page count
        max_projects: int = 3,
    ) -> None:
        self.master = master
        self.llm = llm
        self.out_dir = out_dir
        self.render_pdf = render_pdf
        self.max_projects = max_projects

    def folder_for(self, job: JobPosting, day) -> Path:
        return self.out_dir / f"{day:%Y%m%d}-{_slug(job.company)}-{_slug(job.title)}"

    def make(self, job: JobPosting, day, force: bool = False) -> CvOutput:
        folder = self.folder_for(job, day)
        report_path = folder / "report.json"
        if report_path.exists() and not force:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            pdf = folder / "cv.pdf"
            return CvOutput(
                folder, pdf if pdf.exists() else None, report.get("pages", 0), report.get("source", ""),
                report.get("warnings", []), reused=True,
            )  # fmt: skip

        folder.mkdir(parents=True, exist_ok=True)
        cv = tailor(self.master, job, self.llm, self.max_projects)
        pdf_path = folder / "cv.pdf" if self.render_pdf else None
        pages = 0
        for _ in range(MAX_SHORTEN_STEPS + 1):
            page_html = render_html(self.master, cv)
            if pdf_path is None:
                break
            pages = self.render_pdf(page_html, pdf_path)
            if pages <= 1 or not shorten(cv):
                break
        if pages > 1:
            cv.warnings.append(f"The CV is {pages} pages; trim your master resume to fit one page.")

        (folder / "cv.html").write_text(page_html, encoding="utf-8")
        (folder / "cv.txt").write_text(cv_text(self.master, cv), encoding="utf-8")
        (folder / "job.txt").write_text(
            f"{job.title}\n{job.company}\n\n{job.description}\n", encoding="utf-8"
        )
        report = {"job": job.title, "company": job.company, "source": cv.source, "pages": pages,
                  "warnings": cv.warnings}  # fmt: skip
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return CvOutput(folder, pdf_path, pages, cv.source, cv.warnings)
