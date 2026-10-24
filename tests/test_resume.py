from engine.resume import ResumeProfile, find_resume, load_resume, split_sections

RESUME = """Jane Doe
jdoe@uottawa.ca
EDUCATION
BSc Computer Science, University of Ottawa (2025-2029)
Technical Skills:
Python, Java, SQL
Projects
Job bot - Playwright + local LLM
Work Experience
Cashier, Loblaws
"""


def test_split_sections():
    sections = split_sections(RESUME)
    assert sections["other"] == "Jane Doe\njdoe@uottawa.ca"
    assert sections["education"].startswith("BSc Computer Science")
    assert sections["skills"] == "Python, Java, SQL"
    assert sections["projects"] == "Job bot - Playwright + local LLM"
    assert sections["experience"] == "Cashier, Loblaws"


def test_prompt_orders_education_skills_projects_first():
    text = ResumeProfile(None, RESUME, split_sections(RESUME)).for_prompt()
    assert text.index("EDUCATION") < text.index("SKILLS") < text.index("PROJECTS") < text.index("EXPERIENCE")


def test_find_and_load_txt_resume(tmp_path):
    txt = tmp_path / "resume.txt"
    txt.write_text(RESUME, encoding="utf-8")
    assert find_resume([tmp_path / "resume.pdf", txt]) == txt
    profile = load_resume([tmp_path / "missing.pdf", txt])
    assert profile.path == txt and "skills" in profile.sections


def test_missing_resume_is_empty_profile(tmp_path):
    profile = load_resume([tmp_path / "nope.pdf"])
    assert profile.path is None and profile.for_prompt() == ""
