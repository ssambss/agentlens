"""panopsis's own record of past sessions, in ~/.panopsis/history (PANOPSIS_HOME overrides).

Claude Code deletes a session's transcript 30 days after it was last active, unless you set
cleanupPeriodDays, and the transcript format is internal and can change in any release. So
each session panopsis reads is kept here as the summary its reader made of it: times,
counts, titles, file and folder paths, never the conversation. History and the report then
reach back further than Claude Code keeps transcripts, a session read before a format
change still reads the same after it, and one read once opens again without its transcript.

One gzipped JSON file per session, laid out like Claude Code's own folders. Each file's
modification time is when its session was last active, so the ones in a window can be
found without opening them.
"""

from __future__ import annotations

import dataclasses
import functools
import gzip
import hashlib
import json
import os
import threading
import types
import typing
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from panopsis import model
from panopsis.adapters import claude_transcripts
from panopsis.model import Session
from panopsis.preferences import home

FORMAT = 1  # of the files here


def reader_version() -> str:
    """Changes whenever the code that turns transcripts into summaries does. A transcript that's
    still there is then read again, so its summary says what this panopsis would say."""
    digest = hashlib.sha256()
    for module in (claude_transcripts, model):
        digest.update(Path(module.__file__).read_bytes())
    return digest.hexdigest()[:16]


class SessionStore:
    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder or home() / "history"
        self.reader = reader_version()
        # Summaries found that another version of the reader made, so their transcripts are read
        # again: after an update, all of them, which is why that read takes longer.
        self.outdated = 0

    def _file(self, transcript: Path) -> Path:
        return self.folder / transcript.parent.name / f"{transcript.stem}.json.gz"

    def load(self, transcript: Path, stamp: list) -> Session | None:
        """The summary kept for this transcript, if this reader made it from the transcript as it is now."""
        data = _read(self._file(transcript))
        if data is not None and data.get("reader") != self.reader:
            self.outdated += 1
            return None
        if data is None or data.get("stamp") != stamp:
            return None
        return _session(data)

    def save(self, transcript: Path, stamp: list, session: Session) -> None:
        file = self._file(transcript)
        data = {"format": FORMAT, "reader": self.reader, "transcript": str(transcript), "stamp": stamp,
                "session": to_plain(session)}
        temporary = file.with_name(f".{file.name}.{threading.get_ident()}.tmp")
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(gzip.compress(json.dumps(data).encode("utf-8"), compresslevel=6))
            last_active = session.end.timestamp()
            os.utime(temporary, (last_active, last_active))
            os.replace(temporary, file)
        except OSError:
            temporary.unlink(missing_ok=True)  # the next read keeps it instead

    def gone(self, root: Path, since: datetime | None = None) -> Iterator[Session]:
        """The sessions kept here whose transcripts Claude Code has deleted, active since `since`."""
        for file, _ in self._files(since):
            if not _transcript(root, file).exists() and (data := _read(file)) is not None and _under(root, data):
                session = _session(data)
                if session is not None:
                    yield session

    def forget(self, root: Path, keep_days: int, now: datetime | None = None) -> int:
        """Forget the sessions whose transcripts are gone and that were last active over keep_days ago
        (0 keeps them all). One whose transcript is still there is kept as long as it is."""
        if keep_days <= 0:
            return 0
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
        forgotten = 0
        for file, last_active in self._files():
            if last_active < cutoff and not _transcript(root, file).exists() and _under(root, _read(file)):
                file.unlink(missing_ok=True)
                forgotten += 1
        return forgotten

    def _files(self, since: datetime | None = None) -> Iterator[tuple[Path, datetime]]:
        for file in self.folder.glob("*/*.json.gz"):
            try:
                last_active = datetime.fromtimestamp(file.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if since is None or last_active >= since:
                yield file, last_active


def _transcript(root: Path, file: Path) -> Path:
    return root / file.parent.name / (file.name.removesuffix(".json.gz") + ".jsonl")


def _under(root: Path, data: dict | None) -> bool:
    """Whether it was read from this projects folder: reading another (--root) mustn't make
    every session kept from the usual one look deleted, or forget them."""
    return data is not None and Path(str(data.get("transcript", ""))).parent.parent == root


def _read(file: Path) -> dict | None:
    try:
        data = json.loads(gzip.decompress(file.read_bytes()))
    except (OSError, ValueError, EOFError):
        return None
    return data if isinstance(data, dict) and data.get("format") == FORMAT else None


def _session(data: dict) -> Session | None:
    try:
        return from_plain(Session, data["session"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None  # damaged, or from an panopsis whose sessions had a field this one needs


# Sessions to JSON and back -------------------------------------------------------------------


def to_plain(value: object) -> object:
    """A dataclass and what's in it as JSON values: times as ISO text, sets and tuples as lists."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):  # a Counter too: dataclasses.asdict would make its pairs the keys
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((to_plain(v) for v in value), key=str)
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    return value


def from_plain(kind: object, data: object) -> object:
    """Build a `kind` (a type hint) from what to_plain made of one. A field the data hasn't got
    gets its default, and one this panopsis doesn't know is left out."""
    return _converter(kind)(data)


@functools.cache
def _converter(kind: object) -> Callable[[object], object]:
    """What turns JSON into a `kind`, worked out once per type: a long session has thousands of spans."""
    if dataclasses.is_dataclass(kind) and isinstance(kind, type):
        hints = typing.get_type_hints(kind)
        fields = [(f.name, _converter(hints[f.name])) for f in dataclasses.fields(kind) if f.init]
        return lambda data: kind(**{name: convert(data[name]) for name, convert in fields if name in data})
    origin, args = typing.get_origin(kind), typing.get_args(kind)
    if origin in (typing.Union, types.UnionType):  # X | None
        inner = _converter(next(a for a in args if a is not type(None)))
        return lambda data: None if data is None else inner(data)
    if kind is datetime:
        return datetime.fromisoformat
    if kind is Counter or origin is Counter:
        return Counter
    if origin in (dict, set, list, tuple):
        items = [_converter(arg) for arg in args]
        if origin is dict:
            return lambda data: {key: items[1](value) for key, value in data.items()}
        if origin is tuple:
            return lambda data: tuple(convert(value) for convert, value in zip(items, data))
        return lambda data: origin(items[0](value) for value in data)
    return _as_is


def _as_is(data: object) -> object:
    return data
