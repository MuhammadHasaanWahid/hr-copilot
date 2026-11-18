"""Structured JSON logging with PII scrubbing.

Logs carry request IDs, employee IDs (pseudonymous), roles, categories and
timings - never message text, names, emails or salaries. A scrubbing filter is
a second line of defence in case a message slips through.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar("request_id", default="-")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
KEY_RE = re.compile(r"sk-ant-[\w-]+")
STD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage(), "request_id": request_id.get()}
        out.update({k: v for k, v in vars(record).items() if k not in STD})
        text = json.dumps(out, default=str)
        return KEY_RE.sub("[REDACTED_KEY]", EMAIL_RE.sub("[REDACTED_EMAIL]", text))


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
