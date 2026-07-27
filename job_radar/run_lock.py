"""Cross-platform single-instance protection for Job Radar."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


class AlreadyRunningError(RuntimeError):
    """Raised when another Job Radar process owns the run lock."""


def _lock(file_handle: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        file_handle.seek(0)
        try:
            msvcrt.locking(file_handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise AlreadyRunningError(
                "another Job Radar run is already active"
            ) from exc
    else:
        import fcntl

        try:
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunningError(
                "another Job Radar run is already active"
            ) from exc


def _unlock(file_handle: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        file_handle.seek(0)
        msvcrt.locking(file_handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def single_instance(
    path: Path | None = None,
) -> Iterator[None]:
    """Own an advisory lock until the current run exits."""
    path = path or Path(
        os.environ.get("JOB_RADAR_LOCK_PATH", "data/job-radar.lock")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as file_handle:
        file_handle.seek(0, os.SEEK_END)
        if file_handle.tell() == 0:
            file_handle.write("0")
            file_handle.flush()
        _lock(file_handle)
        try:
            file_handle.seek(0)
            file_handle.truncate()
            file_handle.write(str(os.getpid()))
            file_handle.flush()
            yield
        finally:
            _unlock(file_handle)
