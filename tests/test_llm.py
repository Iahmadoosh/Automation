import requests

from engine.llm import GeminiLLM, extract_json, strip_reasoning

KEY = "AIza-secret"


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class Post:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def __call__(self, url, headers, json, timeout):
        self.calls.append((url, headers, json))
        if self.exc:
            raise self.exc
        return self.resp


def ok(*parts):
    return Resp(200, {"candidates": [{"content": {"parts": list(parts)}}]})


def test_gemini_request_shape_and_key_in_header_only():
    post = Post(ok({"text": "thinking...", "thought": True}, {"text": '{"a": 1}'}))
    reply = GeminiLLM(KEY, "gemini-2.5-flash", post=post).query("sys", "user")
    assert reply == '{"a": 1}'
    url, headers, body = post.calls[0]
    assert KEY not in url and headers["x-goog-api-key"] == KEY
    assert url.endswith("models/gemini-2.5-flash:generateContent")
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert body["contents"][0]["parts"][0]["text"] == "user"
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_errors_return_empty_and_explain(capsys):
    bad_key = Post(Resp(403, {"error": {"message": "API key not valid"}}))
    assert GeminiLLM(KEY, "m", post=bad_key).query("s", "u") == ""
    out = capsys.readouterr().out
    assert "API key not valid" in out and "GEMINI_API_KEY" in out and KEY not in out

    down = Post(exc=requests.ConnectionError(f"boom {KEY}"))
    assert GeminiLLM(KEY, "m", post=down).query("s", "u") == ""
    assert KEY not in capsys.readouterr().out

    blocked = Post(Resp(200, {"candidates": []}))
    assert GeminiLLM(KEY, "m", post=blocked).query("s", "u") == ""


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": {"b": "x}y"}} hope it helps') == {"a": {"b": "x}y"}}
    assert extract_json('<think>{not json}</think>{"ok": true}') == {"ok": True}
    assert extract_json("no json here") is None
    assert extract_json("[1, 2]") is None


def test_strip_reasoning():
    assert strip_reasoning("<think>\nplan\n</think>\nAnswer") == "Answer"
