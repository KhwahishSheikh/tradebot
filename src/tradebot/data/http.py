"""HTTP with rate limiting and retries, shared by every exchange/broker client.

Retries: network errors, 429, 5xx: exponential backoff + jitter, honours Retry-After.
Never retries 4xx other than 429 (a rejected order must not be resent blindly).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """Token bucket: at most `rate` calls per `per` seconds, thread-safe."""

    def __init__(self, rate: float, per: float = 1.0):
        self.capacity = rate
        self.tokens = rate
        self.fill_rate = rate / per
        self.last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.fill_rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.fill_rate
            time.sleep(wait)


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        self.status, self.body, self.url = status, body, url
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")

    @property
    def code(self) -> str | None:
        """Exchange error code if the body is {"error": {"code": ...}} (Delta's format)."""
        import json as _json
        try:
            err = _json.loads(self.body).get("error")
            return err.get("code") if isinstance(err, dict) else str(err)
        except (ValueError, AttributeError):
            return None


class ApiClient:
    def __init__(self, base_url: str, rate_per_sec: float, *, max_retries: int = 5,
                 timeout: float = 15.0, headers: dict[str, str] | None = None,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                 headers={"User-Agent": "tradebot/0.1", **(headers or {})})
        self.limiter = RateLimiter(rate_per_sec)
        self.max_retries = max_retries
        self._sleep = sleep

    def request(self, method: str, path: str, *, params: dict | None = None,
                json: Any = None, headers: dict[str, str] | None = None,
                content: str | None = None, retries: int | None = None) -> Any:
        """retries=0 for non-idempotent calls (order placement): the caller decides what is safe
        to resend, after checking whether the first attempt actually reached the exchange."""
        max_retries = self.max_retries if retries is None else retries
        for attempt in range(max_retries + 1):
            self.limiter.acquire()
            try:
                r = self.http.request(method, path, params=params, json=json,
                                      headers=headers, content=content)
            except httpx.TransportError as e:
                if attempt == max_retries:
                    raise
                self._backoff(attempt, None, f"{type(e).__name__}: {e}", path)
                continue
            if r.status_code in RETRY_STATUS and attempt < max_retries:
                wait = r.headers.get("Retry-After")
                if r.headers.get("X-RATE-LIMIT-RESET"):          # Delta: milliseconds
                    wait = str(float(r.headers["X-RATE-LIMIT-RESET"]) / 1000)
                self._backoff(attempt, wait, f"HTTP {r.status_code}", path)
                continue
            if r.status_code >= 400:
                raise HttpError(r.status_code, r.text, str(r.url))
            return r.json()
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int, retry_after: str | None, why: str, path: str) -> None:
        delay = min(60.0, 2 ** attempt) + random.uniform(0, 0.5)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        log.warning("%s on %s, retry %d in %.1fs", why, path, attempt + 1, delay)
        self._sleep(delay)

    def close(self) -> None:
        self.http.close()
