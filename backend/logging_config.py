"""
Logging for containerised deployment: everything goes to stdout.

Outside development, each record is one JSON object per line (easy for
`docker compose logs` and any log shipper to parse). In development it is a
readable single-line format. Either way, context passed via `extra=` —
`job_id`, `stage`, `molecule_id`, `target_id` — is included, which is what
makes a failed run traceable from the logs alone.

Secrets: database URLs are never logged except through
`settings.safe_database_url`, which redacts the password.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .config import settings

# Fields lifted from `extra=` into the output when present.
CONTEXT_FIELDS = ("job_id", "stage", "molecule_id", "target_id", "request_id", "status")

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for field in CONTEXT_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        context = " ".join(
            f"{field}={getattr(record, field)}" for field in CONTEXT_FIELDS if hasattr(record, field)
        )
        if context:
            base = f"{base} [{context}]"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging() -> None:
    """Idempotent: safe to call from both the entrypoint and app import."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_mdp", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler._mdp = True  # type: ignore[attr-defined]
    handler.setFormatter(TextFormatter() if settings.is_development else JsonFormatter())
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Route uvicorn's loggers through the same handler/format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # SQLAlchemy engine logging can echo statement parameters; keep it quiet.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # Alembic announces every autogenerate plugin at INFO; the migration
    # lines ("Running upgrade ...") are the useful ones and stay at INFO.
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mdp.{name}")
