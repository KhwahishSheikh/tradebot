"""Telegram Bot API over plain HTTPS (long polling). No webhook, so no inbound port on the VPS.

Threading model: a background thread ONLY fetches updates into a queue. All state changes
(approve, reject, kill switch...) happen on the runtime's main thread, so databases are never
touched from two threads.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class TelegramAPI:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._base = f"https://api.telegram.org/bot{token}/"
        self.http = httpx.Client(timeout=40, transport=transport)

    def _call(self, method: str, **params: Any) -> Any:
        r = self.http.post(self._base + method, json={k: v for k, v in params.items() if v is not None})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return self._call("getUpdates", offset=offset, timeout=timeout,
                          allowed_updates=["message", "callback_query"])

    def send(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> int:
        markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                      for row in buttons]} if buttons else None
        return self._call("sendMessage", chat_id=chat_id, text=text[:4096],
                          reply_markup=markup, disable_web_page_preview=True)["message_id"]

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4096])
        except RuntimeError as e:          # "message is not modified" etc. are harmless
            log.debug("edit failed: %s", e)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])
        except RuntimeError as e:
            log.debug("answerCallbackQuery failed: %s", e)


class Poller(threading.Thread):
    """Fetches updates into a queue; survives network errors with backoff."""

    def __init__(self, api: TelegramAPI, out: queue.Queue):
        super().__init__(daemon=True, name="telegram-poller")
        self.api, self.out = api, out
        self.offset: int | None = None
        self.stop_event = threading.Event()
        self.last_ok = time.time()

    def run(self) -> None:
        backoff = 1
        while not self.stop_event.is_set():
            try:
                for u in self.api.get_updates(self.offset):
                    self.offset = u["update_id"] + 1
                    self.out.put(u)
                self.last_ok, backoff = time.time(), 1
            except Exception as e:  # noqa: BLE001 - keep polling forever
                log.warning("telegram poll error: %s", e)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)


class TelegramNotifier:
    """Sends to every whitelisted user. Keeps message ids so decided/expired proposals get edited
    in place (buttons removed) instead of leaving stale APPROVE buttons around."""

    def __init__(self, api: TelegramAPI, chat_ids: list[int]):
        self.api, self.chats = api, chat_ids
        self.sent: dict[str, list[tuple[int, int]]] = {}   # proposal/request id -> [(chat, msg)]

    def proposal(self, p, text: str) -> None:
        buttons = [[("✅ APPROVE", f"ap:{p.id}"), ("❌ REJECT", f"rj:{p.id}")],
                   [("✏️ MODIFY", f"md:{p.id}")]]
        self._send_tracked(p.id, text, buttons)

    def question(self, rid: str, text: str) -> None:
        self._send_tracked(rid, text, [[("HOLD", f"xh:{rid}"), ("CLOSE", f"xc:{rid}")]])

    def message(self, text: str) -> None:
        for c in self.chats:
            try:
                self.api.send(c, text)
            except Exception as e:  # noqa: BLE001 - notifications must never crash trading
                log.error("telegram send failed: %s", e)

    def finalize(self, key: str, text: str) -> None:
        for chat, mid in self.sent.pop(key, []):
            self.api.edit(chat, mid, text)

    def _send_tracked(self, key: str, text: str, buttons) -> None:
        for c in self.chats:
            try:
                self.sent.setdefault(key, []).append((c, self.api.send(c, text, buttons)))
            except Exception as e:  # noqa: BLE001
                log.error("telegram send failed: %s", e)
