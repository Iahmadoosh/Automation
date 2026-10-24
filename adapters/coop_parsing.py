"""Pure parsing and decision logic for the uOttawa co-op (IndEX) adapter.

Nothing here touches the browser, so every rule is unit-testable. The applier
feeds in raw DOM extractions and executes the plans returned from here.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Ordered most-specific first: the first alias contained in a label wins.
_FIELD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("job_id", ("job id", "posting id", "job number", "numéro de l'offre", "id de l'offre")),
    ("title", ("job title", "position title", "titre du poste", "titre")),
    ("company", ("organization", "organisation", "employer", "company", "employeur")),
    ("deadline", ("application deadline", "deadline", "date limite")),
    ("documents", ("documents required", "required documents", "application documents", "documents")),
    ("level", ("year of study", "level", "work term", "niveau", "année d'études")),
    ("programs", ("targeted program", "program", "discipline", "programme")),
    ("description", ("job description", "description", "responsibilities", "duties", "tâches")),
)

_ID_IN_URL = re.compile(r"(?:posting|job)[_-]?id=(\w+)", re.IGNORECASE)
_WORD_LIMIT = re.compile(r"(\d{2,4})\s*(?:words?|mots?)\b", re.IGNORECASE)
_CHAR_LIMIT = re.compile(r"(\d{2,5})\s*(?:characters?|chars?|caractères?)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")

_PROFILE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), key)
    for pattern, key in (
        (r"first\s*name|given\s*name|prénom", "first_name"),
        (r"last\s*name|surname|family\s*name|nom\s*de\s*famille", "last_name"),
        (r"e-?mail|courriel", "email"),
        (r"phone|téléphone|telephone|mobile", "phone"),
        (r"linkedin", "linkedin"),
        (r"github", "github"),
        (r"portfolio|personal\s*website|site\s*web", "portfolio"),
        (r"student\s*(number|id|no)|numéro\s*d.étudiant", "student_number"),
    )
)
_RESUME = re.compile(r"r[ée]sum[ée]|\bcv\b|curriculum", re.IGNORECASE)
_TRANSCRIPT = re.compile(r"transcript|relevé\s*de\s*notes", re.IGNORECASE)
_COVER_LETTER = re.compile(r"cover\s*letter|lettre\s*de\s*présentation", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"^\s*$|^-+|select|choose|choisir|sélectionner", re.IGNORECASE)
_TEXT_TYPES = frozenset({"text", "textarea", "email", "tel", "url", "number", "search"})


@dataclass(frozen=True)
class Posting:
    job_id: str
    title: str
    company: str
    deadline: str
    documents: tuple[str, ...]
    description: str
    level: str
    programs: str
    url: str


@dataclass(frozen=True)
class JobCard:
    """One listing on the filtered IndEX job board."""

    idx: int
    title: str
    organization: str
    location: str
    mobility: str
    duration: str
    programs: str
    href: str

    @property
    def key(self) -> str:
        return f"{self.title}|{self.organization}|{self.location}".lower()


@dataclass(frozen=True)
class FormField:
    idx: int
    tag: str
    type: str
    name: str
    label: str
    maxlength: int | None
    required: bool
    value: str
    checked: bool
    options: tuple[tuple[str, str], ...]
    group_label: str = ""

    @classmethod
    def from_dom(cls, raw: Mapping) -> "FormField":
        return cls(
            idx=int(raw["idx"]),
            tag=str(raw.get("tag", "")).lower(),
            type=str(raw.get("type", "")).lower(),
            name=str(raw.get("name", "")),
            label=str(raw.get("label", "")).strip(),
            maxlength=raw.get("maxlength") or None,
            required=bool(raw.get("required")),
            value=str(raw.get("value", "")),
            checked=bool(raw.get("checked")),
            options=tuple((str(o["value"]), str(o["text"])) for o in raw.get("options", [])),
            group_label=str(raw.get("group_label", "")).strip(),
        )

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity that survives re-enumeration when the form re-renders."""
        return (self.name, self.label, self.type)

    @property
    def display_name(self) -> str:
        return self.label or self.name or f"field #{self.idx}"


class FieldAction(StrEnum):
    SKIP = "skip"
    PROFILE = "profile"
    LLM = "llm"
    SELECT = "select"
    UPLOAD = "upload"
    CHOICE = "choice"  # radio option: answered per group by the model
    MANUAL = "manual"


@dataclass(frozen=True)
class DocumentPrefs:
    resume_path: Path | None
    transcript_path: Path | None
    resume_doc_name: str
    transcript_doc_name: str
    cover_letter_doc_name: str = ""


@dataclass(frozen=True)
class FieldPlan:
    field: FormField
    action: FieldAction
    value: str = ""
    note: str = ""
    max_words: int | None = None
    max_chars: int | None = None


def _clean_label(label: str) -> str:
    return re.sub(r"[\s:*]+$", "", re.sub(r"\s+", " ", label)).strip().lower()


def map_posting(pairs: Sequence[Sequence[str]], url: str, heading: str = "", page_text: str = "") -> Posting:
    """Map label/value pairs scraped from a posting page onto a Posting."""
    found: dict[str, str] = {}
    for raw_label, value in pairs:
        label = _clean_label(raw_label)
        if not label or not value.strip() or len(label) > 60:
            continue
        for field, aliases in _FIELD_ALIASES:
            if any(alias in label for alias in aliases):
                found.setdefault(field, value.strip())
                break

    job_id = found.get("job_id", "")
    if not job_id:
        match = _ID_IN_URL.search(url)
        job_id = (
            match.group(1)
            if match
            else "url-" + hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest()[:10]
        )

    documents = tuple(doc.strip() for doc in re.split(r"[,;\n]", found.get("documents", "")) if doc.strip())
    return Posting(
        job_id=job_id.strip(),
        title=found.get("title") or heading.strip() or "Unknown Title",
        company=found.get("company", "Unknown Company"),
        deadline=found.get("deadline", ""),
        documents=documents,
        description=found.get("description") or page_text.strip(),
        level=found.get("level", ""),
        programs=found.get("programs", ""),
        url=url,
    )


def parse_limits(label: str, maxlength: int | None = None) -> tuple[int | None, int | None]:
    """Extract (max_words, max_chars) from a question label and the field's maxlength."""
    words = _WORD_LIMIT.search(label)
    chars = _CHAR_LIMIT.search(label)
    max_words = int(words.group(1)) if words else None
    char_limits = [n for n in (int(chars.group(1)) if chars else None, maxlength) if n]
    return max_words, (min(char_limits) if char_limits else None)


def enforce_limits(text: str, max_words: int | None, max_chars: int | None) -> str:
    """Trim to the limits, preferring to end on a sentence boundary."""
    text = text.strip()
    original = text
    if max_words is not None:
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words])
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    if text != original:
        ends = [m.end() for m in _SENTENCE_END.finditer(text)]
        if ends and ends[-1] >= len(text) * 0.6:
            text = text[: ends[-1]]
    return text


def _looks_like_question(label: str) -> bool:
    return len(label) > 60 or (label.rstrip().endswith("?") and len(label.split()) >= 6)


def _document_kind(text: str) -> str | None:
    if _COVER_LETTER.search(text):
        return "cover letter"
    if _TRANSCRIPT.search(text):
        return "transcript"
    if _RESUME.search(text):
        return "resume"
    return None


def _pick_document_option(options: Sequence[tuple[str, str]], preferred_name: str) -> tuple[str, str] | None:
    real = [(value, text) for value, text in options if value and not _PLACEHOLDER.search(text)]
    if preferred_name:
        matches = [opt for opt in real if preferred_name.lower() in opt[1].lower()]
        return matches[0] if len(matches) == 1 else None
    return real[0] if len(real) == 1 else None


def plan_field(field: FormField, profile: Mapping, docs: DocumentPrefs) -> FieldPlan:
    """Decide how to fill one form field. Anything uncertain is left for the human."""
    if field.type == "radio":
        return FieldPlan(field, FieldAction.CHOICE)
    if field.type == "checkbox":
        if field.checked:
            return FieldPlan(field, FieldAction.SKIP)
        return FieldPlan(field, FieldAction.MANUAL, note="attestation checkbox - confirm yourself")

    if field.type == "file":
        kind = _document_kind(field.label)
        path = {"transcript": docs.transcript_path, "resume": docs.resume_path}.get(kind or "")
        if path and path.exists():
            return FieldPlan(field, FieldAction.UPLOAD, value=str(path), note=kind)
        return FieldPlan(field, FieldAction.MANUAL, note="file upload with no matching local document")

    if field.tag == "select":
        current = dict(field.options).get(field.value, "")
        kind = _document_kind(field.label) or _document_kind(" ".join(t for _, t in field.options))
        if kind:
            preferred = {
                "transcript": docs.transcript_doc_name,
                "cover letter": docs.cover_letter_doc_name,
            }.get(kind, docs.resume_doc_name)
            choice = _pick_document_option(field.options, preferred)
            if choice:
                if choice[0] == field.value:
                    return FieldPlan(field, FieldAction.SKIP)
                return FieldPlan(field, FieldAction.SELECT, value=choice[0], note=f"{kind}: {choice[1]}")
            return FieldPlan(field, FieldAction.MANUAL, note=f"pick the right {kind} version")
        if field.value and not _PLACEHOLDER.search(current):
            return FieldPlan(field, FieldAction.SKIP)
        return FieldPlan(field, FieldAction.MANUAL, note="dropdown needs a human choice")

    field_type = "textarea" if field.tag == "textarea" else field.type or "text"
    if field_type not in _TEXT_TYPES:
        return FieldPlan(field, FieldAction.MANUAL, note=f"unsupported input type '{field_type}'")
    if field.value.strip():
        return FieldPlan(field, FieldAction.SKIP)

    label = field.display_name
    if field_type != "textarea" and len(label.split()) <= 8:
        for pattern, key in _PROFILE_PATTERNS:
            if pattern.search(label):
                value = str(profile.get(key) or "")
                if value:
                    return FieldPlan(field, FieldAction.PROFILE, value=value, note=key)
                return FieldPlan(field, FieldAction.MANUAL, note=f"add '{key}' to profile.json")

    if field_type == "textarea" or _looks_like_question(label):
        max_words, max_chars = parse_limits(label, field.maxlength)
        return FieldPlan(field, FieldAction.LLM, max_words=max_words, max_chars=max_chars)

    # Short factual inputs (GPA, availability dates, ...) are never invented by the model.
    return FieldPlan(field, FieldAction.MANUAL, note="short factual field - fill yourself")


# ---------------------------------------------------------------- radio groups


@dataclass(frozen=True)
class RadioGroup:
    name: str
    question: str
    options: tuple[FormField, ...]
    required: bool

    @property
    def answered(self) -> bool:
        return any(option.checked for option in self.options)


def group_radios(fields: Sequence[FormField]) -> list[RadioGroup]:
    """Collect radio inputs into named groups, preserving page order."""
    groups: dict[str, list[FormField]] = {}
    for f in fields:
        if f.type == "radio":
            groups.setdefault(f.name or f"__radio{f.idx}", []).append(f)
    return [
        RadioGroup(
            name=name,
            question=next((o.group_label for o in options if o.group_label), "") or name,
            options=tuple(options),
            required=any(o.required for o in options),
        )
        for name, options in groups.items()
    ]


# ---------------------------------------------------------------- job board

_CARD_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("organization", ("organization", "organisation", "employer", "company", "employeur")),
    ("location", ("location", "lieu", "city")),
    ("mobility", ("mobility", "mobilité", "work mode", "work arrangement")),
    ("duration", ("duration", "durée", "length")),
    ("programs", ("programs", "programmes", "program", "programme")),
)
CARD_LABELS: tuple[str, ...] = tuple(alias for _, aliases in _CARD_FIELDS for alias in aliases)
_INLINE_LABEL = re.compile(r"^([^\W\d][\w ]{1,30}?)\s*:\s*(.*)$")


def _card_field(label: str) -> str | None:
    label = _clean_label(label)
    for field, aliases in _CARD_FIELDS:
        if label in aliases:
            return field
    return None


def parse_card(raw: Mapping) -> JobCard:
    """Parse one job card's text ("Label: value" or a label line followed by its value line)."""
    lines = [line.strip() for line in str(raw.get("text", "")).splitlines() if line.strip()]
    found: dict[str, str] = {}
    for i, line in enumerate(lines):
        inline = _INLINE_LABEL.match(line)
        field = _card_field(inline.group(1)) if inline else _card_field(line)
        if not field or field in found:
            continue
        value = inline.group(2).strip() if inline else ""
        if not value and i + 1 < len(lines) and not _card_field(lines[i + 1].split(":")[0]):
            value = lines[i + 1]
        if value:
            found[field] = value
    title = str(raw.get("linkText", "")).strip() or (lines[0] if lines else "Unknown Title")
    return JobCard(
        idx=int(raw.get("idx", 0)),
        title=title,
        organization=found.get("organization", ""),
        location=found.get("location", ""),
        mobility=found.get("mobility", ""),
        duration=found.get("duration", ""),
        programs=found.get("programs", ""),
        href=str(raw.get("href", "")),
    )


def group_by_location(
    cards: Sequence[JobCard], local_regex: re.Pattern[str]
) -> tuple[list[JobCard], list[JobCard]]:
    """Split into (local, everything else); remote and unknown locations count as 'else'."""
    local = [c for c in cards if local_regex.search(c.location)]
    return local, [c for c in cards if c not in local]


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def match_options(options: Sequence[tuple[str, str]], wanted: str) -> list[str]:
    """Option values for a filter choice: exact text match first, else options containing every word."""
    target = " ".join(_tokens(wanted))
    exact = [value for value, text in options if " ".join(_tokens(text)) == target]
    if exact:
        return exact
    words = set(_tokens(wanted))
    return [value for value, text in options if words and words <= set(_tokens(text))]
