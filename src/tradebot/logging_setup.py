"""Operational logging: human-readable console + JSON-lines file, with secret redaction.

Operational logs are for debugging. The audit trail (audit.py) is the record of truth.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path

from tradebot.config import AppConfig, Secrets
from tradebot.timeutil import IST


class RedactSecrets(logging.Filter):
    """Replaces any known secret value in a log record with ***."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if len(s) >= 4]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            msg = record.getMessage()
            for s in self._secrets:
                msg = msg.replace(s, "***")
            record.msg, record.args = msg, None
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, mode: str):
        super().__init__()
        self.mode = mode

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC)
        out = {
            "ts_utc": ts.isoformat(),
            "ts_ist": ts.astimezone(IST).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "mode": self.mode,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def configure_logging(cfg: AppConfig, secrets: Secrets) -> None:
    log_dir = Path(cfg.logging.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    redactor = RedactSecrets(secrets.values())

    console = logging.StreamHandler()
    console_fmt = logging.Formatter(
        f"%(asctime)s IST [{cfg.mode.value.upper()}] %(levelname)s %(name)s: %(message)s")
    console_fmt.converter = lambda secs: datetime.fromtimestamp(secs, IST).timetuple()
    console.setFormatter(console_fmt)

    file = logging.handlers.TimedRotatingFileHandler(
        log_dir / "app.log", when="midnight", backupCount=60, encoding="utf-8")
    file.setFormatter(JsonFormatter(cfg.mode.value))

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(cfg.logging.level)
    for h in (console, file):
        h.addFilter(redactor)
        root.addHandler(h)
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)   # one line per request is noise
