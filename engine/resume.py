"""Resume ingestion: locate resume.pdf / resume.txt, extract text once, split into sections."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

_SECTION_HEADINGS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rf"^\s*({pattern})\s*:?\s*$", re.IGNORECASE))
    for name, pattern in (
        ("education", r"education|academic background|formation"),
        ("skills", r"(technical\s+)?skills|technologies|tech stack|compétences"),
        ("experience", r"(work\s+|professional\s+|relevant\s+)?experience|employment|work history"),
        ("projects", r"(personal\s+|academic\s+|technical\s+)?projects|projets"),
    )
)
# Prompt order: most persuasive for a 2nd-year CS student first.
_PROMPT_ORDER = ("education", "skills", "projects", "experience", "other")


@dataclass(frozen=True)
class ResumeProfile:
    path: Path | None
    text: str
    sections: Mapping[str, str] = field(default_factory=dict)

    def for_prompt(self, max_chars: int = 3500) -> str:
        if not self.sections:
            return self.text[:max_chars]
        parts = [
            f"{name.upper()}:\n{self.sections[name]}" for name in _PROMPT_ORDER if self.sections.get(name)
        ]
        return "\n\n".join(parts)[:max_chars]


def find_resume(candidates: Sequence[Path]) -> Path | None:
    for path in candidates:
        if path and path.is_file():
            return path
    return None


def extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    return path.read_text(encoding="utf-8", errors="replace").strip()


def split_sections(text: str) -> dict[str, str]:
    """Group lines under the last recognised heading; text before any heading is 'other'."""
    sections: dict[str, list[str]] = {}
    current = "other"
    for line in text.splitlines():
        heading = next((name for name, rx in _SECTION_HEADINGS if rx.match(line)), None)
        if heading:
            current = heading
            continue
        if line.strip():
            sections.setdefault(current, []).append(line.strip())
    return {name: "\n".join(lines) for name, lines in sections.items()}


def load_resume(candidates: Sequence[Path]) -> ResumeProfile:
    path = find_resume(candidates)
    if path is None:
        print("[Notice] No resume.pdf / resume.txt found; answers will use profile.json only.")
        return ResumeProfile(None, "")
    text = extract_text(path)
    sections = split_sections(text)
    found = ", ".join(name for name in _PROMPT_ORDER if name in sections) or "none"
    print(f"Loaded resume {path.name} ({len(text)} chars; sections: {found}).")
    return ResumeProfile(path, text, sections)
