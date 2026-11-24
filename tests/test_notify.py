import pytest
import requests

from core.errors import NotifyError
from core.notify import TelegramNotifier, load_notifier, split_message

TOKEN = "123:secret-token"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"ok": status_code == 200}

    def json(self):
        return self._body


class FakePost:
    def __init__(self, response=None, exc=None):
        self.calls = []
        self.response = response or FakeResponse()
        self.exc = exc

    def __call__(self, url, json, timeout):
        self.calls.append((url, json))
        if self.exc:
            raise self.exc
        return self.response


def test_split_short_message_unchanged():
    assert split_message("hello") == ["hello"]


def test_split_prefers_line_breaks():
    text = "\n".join(f"line {i:03d}" for i in range(100))
    chunks = split_message(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "\n".join(chunks) == text


def test_split_hard_cuts_one_long_line():
    chunks = split_message("x" * 250, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_send_posts_each_chunk_to_chat():
    post = FakePost()
    sent = TelegramNotifier(TOKEN, "42", post=post).send(["hi", "there"])
    assert sent == 2
    assert [c[1]["text"] for c in post.calls] == ["hi", "there"]
    assert all(c[1]["chat_id"] == "42" for c in post.calls)
    assert TOKEN in post.calls[0][0]


def test_http_error_explains_without_leaking_token():
    post = FakePost(FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"}))
    with pytest.raises(NotifyError) as err:
        TelegramNotifier(TOKEN, "42", post=post).send(["hi"])
    assert "chat not found" in str(err.value)
    assert "TELEGRAM_CHAT_ID" in str(err.value)
    assert TOKEN not in str(err.value)


def test_network_error_hides_token():
    exc = requests.ConnectionError(f"failed https://api.telegram.org/bot{TOKEN}/sendMessage")
    with pytest.raises(NotifyError) as err:
        TelegramNotifier(TOKEN, "42", post=FakePost(exc=exc)).send(["hi"])
    assert TOKEN not in str(err.value)
    assert err.value.__cause__ is None


def test_load_notifier(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert load_notifier() is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    with pytest.raises(NotifyError, match="both"):
        load_notifier()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert load_notifier().chat_id == "42"
