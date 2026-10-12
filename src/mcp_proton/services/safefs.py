"""Open caller-named files and directories inside owner-configured roots.

A path is checked against the roots by its resolved name, but a client that can
write inside a shared root could swap a directory for a symbolic link after that
check, so a later ``open(path)`` would land outside the root. Here the deepest
configured root that contains the path is opened as a directory, and every
component below it is opened relative to its parent with ``O_NOFOLLOW``: a
component that has become a symbolic link fails instead of being followed, and
reads, hashes and writes all go through the descriptors that were opened.

Platforms without ``dir_fd`` support (Windows) fall back to path-based access
after the same root check.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..domain.errors import ErrorCode, MailError
from ..domain.families import OperationFamily
from ..policy import engine
from .core import MailApp

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # opening a planted FIFO must not block

SUPPORTED = bool(_O_NOFOLLOW and _O_DIRECTORY and os.open in os.supports_dir_fd
                 and os.mkdir in os.supports_dir_fd and os.unlink in os.supports_dir_fd)

_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC


def _rejected(detail: str = "path is outside the configured directories") -> MailError:
    return MailError(ErrorCode.PATH_REJECTED, detail)


def root_sets(app: MailApp, client_id: str, family: OperationFamily) -> list[list[str]]:
    """The configured roots for ``family``, one list per applicable constraint set
    (global, then the client's); a path must be inside a root of every set."""
    attr = engine._PATH_DIRS[family]
    return [list(getattr(c, attr)) for c in engine._intersect_constraints(app.policy, client_id)]


def _anchor(path: str, sets: list[list[str]]) -> tuple[str, list[str]]:
    """(deepest containing root, components below it) for the resolved ``path``.

    Every set has a root that is an ancestor of the path, so the deepest of them is
    inside all the others: staying under it keeps the path inside every set."""
    try:
        real = os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError) as exc:
        raise _rejected() from exc
    best: str | None = None
    for roots in sets:
        found: str | None = None
        for root in roots:
            r = os.path.realpath(os.path.expanduser(root))
            if (real == r or real.startswith(r.rstrip(os.sep) + os.sep)) and (
                    found is None or len(r) > len(found)):
                found = r
        if found is None:
            raise _rejected()
        if best is None or len(found) > len(best):
            best = found
    if best is None:
        raise _rejected()
    rel = os.path.relpath(real, best)
    return best, ([] if rel == os.curdir else rel.split(os.sep))


def _walk(root: str, parts: list[str], *, create: bool) -> int:
    """A descriptor for ``root/parts...``, opening each component with no-follow."""
    fd = os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        for name in parts:
            try:
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=fd)
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _os_error(exc: OSError, what: str) -> MailError:
    if isinstance(exc, FileNotFoundError):
        return MailError(ErrorCode.NOT_FOUND, f"{what} does not exist")
    # ELOOP / ENOTDIR: a component is (now) a symbolic link or not a directory
    return _rejected(f"{what} is not inside the configured directories "
                     "(a path component is a symbolic link or changed)")


def open_file(app: MailApp, client_id: str, family: OperationFamily, path: str) -> BinaryIO:
    """Open an existing regular file under the roots for reading."""
    root, parts = _anchor(path, root_sets(app, client_id, family))
    if not parts:
        raise _rejected("path is a directory, not a file")
    try:
        if SUPPORTED:
            dfd = _walk(root, parts[:-1], create=False)
            try:
                fd = os.open(parts[-1], os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK,
                             dir_fd=dfd)
            finally:
                os.close(dfd)
        else:
            fd = os.open(os.path.join(root, *parts), os.O_RDONLY | _O_NOFOLLOW)
    except OSError as exc:
        raise _os_error(exc, "file") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise MailError(ErrorCode.NOT_FOUND, "path is not a regular file")
    return os.fdopen(fd, "rb")


@dataclass
class Directory:
    """An opened directory under the roots: ``path`` for reporting, ``fd`` (None on
    platforms without ``dir_fd``) for every operation inside it."""

    path: Path
    fd: int | None

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


@contextlib.contextmanager
def open_dir(app: MailApp, client_id: str, family: OperationFamily, path: str, *,
             create: bool = True) -> Iterator[Directory]:
    """Open (and with ``create``, make) a directory under the roots."""
    root, parts = _anchor(path, root_sets(app, client_id, family))
    real = Path(root, *parts)
    if SUPPORTED:
        try:
            fd: int | None = _walk(root, parts, create=create)
        except OSError as exc:
            raise _os_error(exc, "directory") from exc
    else:
        if create:
            real.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = None
    d = Directory(real, fd)
    try:
        yield d
    finally:
        d.close()


def create_new(directory: Directory, name: str) -> int:
    """Exclusively create ``name`` in ``directory`` (mode 0600, never following a
    link). Raises ``FileExistsError`` if it is taken."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
    if directory.fd is not None:
        return os.open(name, flags, 0o600, dir_fd=directory.fd)
    return os.open(directory.path / name, flags, 0o600)


def unlink(directory: Directory, name: str) -> None:
    if directory.fd is not None:
        os.unlink(name, dir_fd=directory.fd)
    else:
        (directory.path / name).unlink()


__all__ = ["SUPPORTED", "Directory", "create_new", "open_dir", "open_file", "root_sets",
           "unlink"]
