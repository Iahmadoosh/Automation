"""LLM clients with one interface: query(system_prompt, user_input, timeout) -> str ("" on failure).

- LocalLLM: LM Studio's REST API (the same server the co-op screening answers use).
- GeminiLLM: Google's Gemini API (needs GEMINI_API_KEY from aistudio.google.com).
"""

import json
import re
from collections.abc import Callable
from typing import Protocol

import requests

from engine.evaluator import JobEvaluator

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMClient(Protocol):
    name: str

    def query(self, system_prompt: str, user_input: str, timeout: float = ...) -> str: ...


def strip_reasoning(text: str) -> str:
    """Drop <think>...</think> blocks that reasoning models (e.g. Qwen3) put before the answer."""
    return _THINK.sub("", text).strip()


def extract_json(text: str) -> dict | None:
    """First JSON object in a model reply, tolerating code fences and chatter around it."""
    text = strip_reasoning(text)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : i + 1])
                        return value if isinstance(value, dict) else None
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


class LocalLLM:
    """LM Studio server on your PC. Returns "" (and prints why) when it isn't reachable."""

    def __init__(self, endpoint: str, model: str) -> None:
        self.name = f"local:{model}"
        self._client = JobEvaluator({}, None, endpoint, model)

    def query(self, system_prompt: str, user_input: str, timeout: float = 180) -> str:
        return strip_reasoning(self._client.query(system_prompt, user_input, timeout=timeout))


class GeminiLLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        post: Callable[..., requests.Response] = requests.post,
        json_output: bool = True,
    ) -> None:
        self.name = f"gemini:{model}"
        self.model = model
        self._api_key = api_key
        self._post = post
        self.json_output = json_output

    def query(self, system_prompt: str, user_input: str, timeout: float = 180) -> str:
        body: dict = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_input}]}],
        }
        if self.json_output:
            body["generationConfig"] = {"responseMimeType": "application/json"}
        try:
            # The key goes in a header, never in the URL, so it can't leak into error messages.
            response = self._post(
                GEMINI_URL.format(model=self.model),
                headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                json=body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            print(f"[Gemini] Could not reach the API ({type(exc).__name__}).")
            return ""
        if response.status_code != 200:
            try:
                detail = response.json().get("error", {}).get("message", "")
            except ValueError:
                detail = ""
            hint = {
                400: " Check GEMINI_MODEL.",
                403: " Check GEMINI_API_KEY.",
                404: " Check GEMINI_MODEL.",
                429: " Rate limit or free-tier quota reached; try later.",
            }.get(response.status_code, "")
            print(f"[Gemini] HTTP {response.status_code}: {detail[:200]}{hint}")
            return ""
        try:
            data = response.json()
            parts = data["candidates"][0]["content"]["parts"]
        except (ValueError, KeyError, IndexError, TypeError):
            print("[Gemini] Empty or blocked response.")
            return ""
        return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
