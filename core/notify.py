"""Push notifications to your phone through a Telegram bot.

Set TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID in .env.
"""

import os
from collections.abc import Callable, Sequence

import requests

from core.errors import NotifyError

# Telegram rejects messages over 4096 characters; leave room for safety.
MAX_MESSAGE_CHARS = 4000
API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split long text into chunks under the limit, preferring line breaks."""
    chunks: list[str] = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        post: Callable[..., requests.Response] = requests.post,
        timeout: float = 20,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self._post = post
        self.timeout = timeout

    def _send_one(self, text: str) -> None:
        try:
            response = self._post(
                API_URL.format(token=self._token),
                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            # The request URL contains the token, so never echo the raw exception.
            raise NotifyError(f"Could not reach Telegram ({type(exc).__name__}).") from None
        if response.status_code != 200:
            try:
                detail = response.json().get("description", "")
            except ValueError:
                detail = ""
            hint = {
                401: " Check TELEGRAM_BOT_TOKEN.",
                404: " Check TELEGRAM_BOT_TOKEN.",
                400: " Check TELEGRAM_CHAT_ID, and that you pressed Start in the bot's chat.",
                403: " You blocked the bot or never pressed Start in its chat.",
            }.get(response.status_code, "")
            raise NotifyError(f"Telegram returned HTTP {response.status_code} {detail}.{hint}".strip())

    def send(self, messages: Sequence[str]) -> int:
        """Send each message (splitting long ones). Returns the number of Telegram messages sent."""
        sent = 0
        for message in messages:
            for chunk in split_message(message):
                self._send_one(chunk)
                sent += 1
        return sent


def load_notifier() -> TelegramNotifier | None:
    """Notifier from the environment, or None when Telegram isn't configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token and not chat_id:
        return None
    if not token or not chat_id:
        raise NotifyError("Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (or neither).")
    return TelegramNotifier(token, chat_id)
