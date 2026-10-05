"""Filesystem helpers.

Exports are written atomically: content goes to a sibling temporary file which is
then renamed over the destination. A crash or a full disk mid-write therefore
leaves the previous export intact instead of a half-written file that looks
valid.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from src.utils.errors import ConfigError


def ensure_directory(path: Path, *, what: str = "directory") -> Path:
    """Create ``path`` if it is missing, then confirm it is a directory.

    ``what`` names the path in the error message, so a caller can say "output
    directory" where a bare "directory" would leave the reader guessing which
    of several paths was at fault.

    Raises:
        ConfigError: if the directory cannot be created, or the path exists as
            something that is not a directory.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create {what} {path}: {exc}") from exc
    if not path.is_dir():
        raise ConfigError(f"{what} {path} exists but is not a directory")
    return path


@contextmanager
def atomic_write(
    path: Path, *, encoding: str = "utf-8", newline: str | None = None
) -> Iterator[IO[str]]:
    """Open ``path`` for writing, publishing it only if the block succeeds.

    The temporary file is created in the destination directory so the final
    rename stays on one filesystem (and is therefore atomic).

    Raises:
        OSError: if the directory is not writable or the rename fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Not opened as a context manager at creation: the handle has to stay open
    # across the ``yield`` and be closed by the inner ``with`` below, so that the
    # rename only happens once the caller's block has finished cleanly.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        encoding=encoding,
        newline=newline,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            yield handle
        # NamedTemporaryFile creates 0600. Exports are ordinary business files
        # that colleagues and downstream tools need to read, so widen to the
        # conventional 0644 rather than leaving them owner-only.
        temp_path.chmod(0o644)
        temp_path.replace(path)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        temp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> int:
    """Write ``text`` to ``path`` atomically. Returns the byte length written."""
    with atomic_write(path, encoding=encoding) as handle:
        handle.write(text)
    return len(text.encode(encoding))
