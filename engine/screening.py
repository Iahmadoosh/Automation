"""Builds Qwen prompts for employer screening questions and post-processes answers."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from adapters.coop_parsing import enforce_limits

SYSTEM_PROMPT = (
    "You write answers to co-op job application questions on behalf of a 2nd-year Computer "
    "Science (CSI) student at the University of Ottawa. Write in the first person, in a "
    "professional and direct tone. Ground every claim in the candidate's resume and profile; "
    "never invent employers, grades, certifications, dates, or experience. Highlight the "
    "2nd-year coursework, technical skills, and hands-on projects most relevant to this job. "
    "Stay within the stated length limit. Output only the answer text: no preamble, no "
    "headings, no quotation marks, no markdown."
)

_PREAMBLE = re.compile(r"^(sure|certainly|here(?:'s| is) (?:my|an|the) answer)[^\n]*:\s*", re.IGNORECASE)


class LLMClient(Protocol):
    def query(self, system_prompt: str, user_input: str, timeout: float = ...) -> str: ...


@dataclass(frozen=True)
class JobContext:
    title: str
    company: str
    description: str


@dataclass(frozen=True)
class ScreeningQuestion:
    prompt: str
    max_words: int | None = None
    max_chars: int | None = None


def _profile_highlights(profile: Mapping) -> str:
    lines = []
    for key, label in (
        ("program", "Program"),
        ("year", "Year of study"),
        ("coursework", "Relevant coursework"),
        ("skills", "Technical skills"),
        ("projects", "Projects"),
    ):
        value = profile.get(key)
        if not value:
            continue
        if isinstance(value, list):
            value = "; ".join(
                f"{v.get('name', '')}: {v.get('summary', '')}" if isinstance(v, dict) else str(v)
                for v in value
            )
        lines.append(f"- {label}: {value}")
    return "\n".join(lines) or "- (no extra profile highlights provided)"


def _length_instruction(question: ScreeningQuestion) -> str:
    parts = []
    if question.max_words:
        parts.append(f"at most {question.max_words} words")
    if question.max_chars:
        parts.append(f"at most {question.max_chars} characters including spaces")
    if not parts:
        return "Keep it to roughly 120-180 words."
    return "Hard limit: " + " and ".join(parts) + ". Aim for about 85% of the limit."


def build_answer_request(
    job: JobContext, question: ScreeningQuestion, resume_text: str, profile: Mapping
) -> tuple[str, str]:
    """Return (system_prompt, user_input) for one screening question."""
    user_input = f"""Job:
- Title: {job.title}
- Company: {job.company}
- Description:
{job.description[:3500]}

Candidate resume:
{resume_text[:3000] or "(resume unavailable)"}

Candidate profile highlights:
{_profile_highlights(profile)}

Application question:
{question.prompt}

{_length_instruction(question)}
Answer the question directly, connecting the candidate's real experience to this job."""
    return SYSTEM_PROMPT, user_input


def clean_answer(raw: str, question: ScreeningQuestion) -> str:
    text = _PREAMBLE.sub("", raw.strip())
    text = re.sub(r"^#+\s.*\n", "", text)
    text = text.replace("**", "").strip().strip('"').strip()
    return enforce_limits(text, question.max_words, question.max_chars)


CHOICE_SYSTEM_PROMPT = (
    "You answer multiple-choice co-op application questions for a 2nd-year Computer Science "
    "student at the University of Ottawa. Choose the option that is truthful according to the "
    "candidate's resume and profile. If the resume does not establish the answer, reply UNSURE. "
    "Reply with only the option number or UNSURE."
)


def build_choice_request(
    job: JobContext, question: str, options: Sequence[str], resume_text: str, profile: Mapping
) -> tuple[str, str]:
    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(options, 1))
    user_input = f"""Job: {job.title} at {job.company}

Candidate resume:
{resume_text[:3000] or "(resume unavailable)"}

Candidate profile highlights:
{_profile_highlights(profile)}

Question:
{question}

Options:
{numbered}

Reply with the option number, or UNSURE."""
    return CHOICE_SYSTEM_PROMPT, user_input


def parse_choice(raw: str, options: Sequence[str]) -> int | None:
    """0-based index of the chosen option, or None when unsure/unparseable."""
    text = raw.strip()
    if not text or "unsure" in text.lower():
        return None
    number = re.match(r"^\D{0,10}?(\d+)", text)
    if number and 1 <= int(number.group(1)) <= len(options):
        return int(number.group(1)) - 1
    lowered = text.lower().strip(" .")
    exact = [i for i, opt in enumerate(options) if opt.lower().strip() == lowered]
    return exact[0] if len(exact) == 1 else None


class ScreeningAnswerer:
    def __init__(self, llm: LLMClient, resume_text: str, profile: Mapping, timeout: float = 120) -> None:
        self.llm = llm
        self.resume_text = resume_text
        self.profile = profile
        self.timeout = timeout

    def answer(self, job: JobContext, question: ScreeningQuestion) -> str:
        """Generated answer, or "" when the model is unavailable (caller flags it for review)."""
        system_prompt, user_input = build_answer_request(job, question, self.resume_text, self.profile)
        raw = self.llm.query(system_prompt, user_input, timeout=self.timeout)
        return clean_answer(raw, question) if raw else ""

    def choose(self, job: JobContext, question: str, options: Sequence[str]) -> int | None:
        """Index of the truthful option, or None to leave the question for the human."""
        system_prompt, user_input = build_choice_request(
            job, question, options, self.resume_text, self.profile
        )
        return parse_choice(self.llm.query(system_prompt, user_input, timeout=self.timeout), options)
