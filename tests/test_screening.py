from engine.screening import (
    SYSTEM_PROMPT,
    JobContext,
    ScreeningAnswerer,
    ScreeningQuestion,
    build_answer_request,
    clean_answer,
    parse_choice,
)

JOB = JobContext("Backend Intern", "Ciena", "Work on Go microservices.")
PROFILE = {"coursework": ["CSI 2110", "CSI 2132"], "projects": [{"name": "Bot", "summary": "Playwright"}]}


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str, float]] = []

    def query(self, system_prompt: str, user_input: str, timeout: float = 30) -> str:
        self.calls.append((system_prompt, user_input, timeout))
        return self.reply


def test_payload_contains_job_question_limits_and_profile():
    q = ScreeningQuestion("Why Ciena?", max_words=100)
    system, user = build_answer_request(JOB, q, "Resume text", PROFILE)
    assert system == SYSTEM_PROMPT and "first person" in system
    for expected in (
        "Backend Intern",
        "Ciena",
        "Go microservices",
        "Why Ciena?",
        "at most 100 words",
        "CSI 2110; CSI 2132",
        "Bot: Playwright",
        "Resume text",
    ):
        assert expected in user


def test_clean_answer_strips_preamble_and_enforces_limit():
    raw = 'Sure, here is my answer:\n"I built a bot. It scraped jobs. It used Playwright and Python daily."'
    assert clean_answer(raw, ScreeningQuestion("q", max_words=8)) == "I built a bot. It scraped jobs."


def test_parse_choice():
    options = ["Yes", "No"]
    assert parse_choice("2", options) == 1
    assert parse_choice("Option 1.", options) == 0
    assert parse_choice("yes", options) == 0
    assert parse_choice("UNSURE", options) is None
    assert parse_choice("7", options) is None
    assert parse_choice("", options) is None


def test_choose_sends_numbered_options():
    llm = FakeLLM("1")
    answerer = ScreeningAnswerer(llm, "resume", {})
    assert answerer.choose(JOB, "Legally eligible to work in Canada?", ["Yes", "No"]) == 0
    system, user, _ = llm.calls[0]
    assert "UNSURE" in system and "1. Yes\n2. No" in user


def test_answerer_returns_empty_when_model_down():
    llm = FakeLLM("")
    assert ScreeningAnswerer(llm, "", {}, timeout=5).answer(JOB, ScreeningQuestion("q")) == ""
    assert llm.calls[0][2] == 5
