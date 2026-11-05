from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RawBatch:
    kind: str
    payload: Any
    context: dict = field(default_factory=dict)


@dataclass
class Record:
    id: str
    native_id: str
    platform: str
    content_type: str
    title: str | None
    body: str
    author: str | None
    created_at: str | None
    source_url: str
    parent_id: str | None = None
    extra_metadata: dict = field(default_factory=dict)


class CollectorError(Exception):
    pass


class AccessBlocked(CollectorError):
    pass


class Cancelled(CollectorError):
    pass


class RateLimited(AccessBlocked):
    pass
