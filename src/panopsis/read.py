"""The sessions needing you that you've already read, so the wall counts only the others.

A session needs you from when its turn ends, or it asks something, until you answer. Once you've
had it in front of you in that time, it has been read: it stops counting as needing you until it
next starts to. Which ones are kept in ~/.panopsis/read.json, so a restart doesn't bring back
everything you'd read.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from panopsis.preferences import home


def read_path() -> Path:
    return home() / "read.json"


class ReadSessions:
    """For each session read, the time it started needing you then: it's read until it starts again."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or read_path()
        self._since: dict[str, datetime] = {}
        try:
            for session_id, since in json.loads(self.path.read_text(encoding="utf-8")).items():
                self._since[session_id] = datetime.fromisoformat(since)
        except (OSError, ValueError, AttributeError, TypeError):
            pass  # none read yet, or a file from something else: every session that needs you does

    def has_read(self, session_id: str, since: datetime | None) -> bool:
        read = self._since.get(session_id)
        return read is not None and (since is None or since <= read)

    def mark(self, session_id: str, since: datetime | None) -> None:
        self._since[session_id] = since or datetime.now(timezone.utc)
        self._save()

    def keep_only(self, session_ids: Iterable[str]) -> None:
        """Forget the sessions that aren't open any more."""
        open_ids = set(session_ids)
        kept = {sid: since for sid, since in self._since.items() if sid in open_ids}
        if kept != self._since:
            self._since = kept
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({sid: since.isoformat() for sid, since in self._since.items()}, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass  # only so a restart remembers
