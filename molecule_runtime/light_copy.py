"""The Light run copy step: ``molecule-runtime-light-copy`` (contract C2).

A Light run is a short-lived copy of a parked agent in its own pod. The control
plane runs this command in the run pod's init container::

    molecule-runtime-light-copy --src /mnt/agent --dst /mnt/run \
        --token-env MOLECULE_WORKSPACE_TOKEN

``/mnt/agent`` is the agent's identity volume, mounted read-only; ``/mnt/run`` is
the pod's private emptyDir, which the main container mounts with the same
subPaths the agent pod uses for the volume. The copy is the only thing that ever
touches the agent's volume; the task itself sees just the copy.

What is copied: regular files (streamed, permission bits kept), directories and
symlinks (as links: never followed, target text kept verbatim, dangling allowed).
Sockets, FIFOs and devices are skipped (a hermes volume holds live gateway
sockets). The metadata of ``--dst`` itself is never changed (the emptyDir root is
root-owned). Nothing is overwritten or written through an existing link.

What is left out, as glob patterns relative to the volume root: the built-ins
(``configs/.auth_token``, ``configs/.platform_inbound_secret``, ``lost+found/``)
and the engine's list in the file named by ``MOLECULE_LIGHT_COPY_EXCLUDE_FILE``
(one pattern per line, ``#`` comments). A pattern is matched with
``fnmatch.fnmatchcase`` against the whole relative path (``*`` may cross ``/``);
a trailing ``/`` matches directories only; a matched directory is left out with
everything under it.

The run's token (the env var named by ``--token-env``) is written to
``<dst>/configs/.auth_token`` with mode 0600 and never printed.

On success one JSON line goes to stdout -- ``{"files":N,"bytes":N,"excluded":N,
"seconds":F}`` -- and the exit code is 0. Any error (an unreadable file or
directory, an exclude file that is missing, not UTF-8 or holds an absolute
pattern, a token that is missing or not UTF-8) prints one line naming the path
to stderr, ``light-copy: FAILED <path>: <reason>``, and exits 1 without the JSON
line: a run must never start on a partial copy. The inputs are checked before
anything is copied. Bad arguments exit 2 (argparse).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import stat
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

BUILTIN_EXCLUDES: tuple[str, ...] = (
    "configs/.auth_token",
    "configs/.platform_inbound_secret",
    # A root-owned lost+found on block-volume storage classes would otherwise
    # fail every copy as unreadable.
    "lost+found/",
)
EXCLUDE_FILE_ENV = "MOLECULE_LIGHT_COPY_EXCLUDE_FILE"
TOKEN_RELPATH = "configs/.auth_token"

_CHUNK = 1024 * 1024
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


@dataclass
class CopyStats:
    """``files`` = regular files + symlinks written (the token file not counted);
    ``bytes`` = regular-file bytes; ``excluded`` = entries left out by a pattern
    (a skipped directory counts once) + skipped sockets, FIFOs and devices."""

    files: int = 0
    bytes: int = 0
    excluded: int = 0


class CopyError(Exception):
    """A copy failure, naming the path (relative to the volume root) and why."""

    def __init__(self, rel: str, reason: str) -> None:
        super().__init__(f"{rel}: {reason}")
        self.rel = rel
        self.reason = reason


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def load_patterns(exclude_file: str | None) -> list[str]:
    """The built-in excludes plus the patterns in ``exclude_file`` (None = none).

    Lines are trimmed; blank lines and lines starting with ``#`` are ignored. A
    pattern starting with ``/`` is refused: patterns are volume paths, and a
    container path such as ``/workspace/x`` lives at ``rtstate/workspace/x``, so
    accepting it would silently copy what it was meant to leave out.
    """
    patterns = list(BUILTIN_EXCLUDES)
    if exclude_file is None:
        return patterns
    try:
        text = Path(exclude_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise CopyError(
            exclude_file, f"cannot read the exclude file named by {EXCLUDE_FILE_ENV}: {_reason(exc)}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise CopyError(
            exclude_file,
            f"the exclude file named by {EXCLUDE_FILE_ENV} is not UTF-8 text "
            f"(byte {exc.object[exc.start]:#04x} at offset {exc.start})",
        ) from exc
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("/"):
            raise CopyError(
                exclude_file,
                f"line {lineno}: pattern {line!r} is absolute; patterns are relative "
                "to the volume root (configs/..., rtstate/...)",
            )
        patterns.append(line)
    return patterns


def is_excluded(rel: str, is_dir: bool, patterns: Sequence[str]) -> bool:
    """True when ``rel`` (a volume-relative POSIX path) matches a pattern."""
    for pattern in patterns:
        if pattern.endswith("/"):
            if is_dir and fnmatch.fnmatchcase(rel, pattern.rstrip("/")):
                return True
        elif fnmatch.fnmatchcase(rel, pattern):
            return True
    return False


def _open_source(path: str) -> int:
    """Open a source file for reading, never through a symlink.

    A seam: the unreadable-file tests patch it, because CI may run as root."""
    return os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _copy_file(src_path: str, dst_path: str, mode: int) -> int:
    fd_in = _open_source(src_path)
    try:
        fd_out = os.open(dst_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
        try:
            copied = 0
            while True:
                chunk = os.read(fd_in, _CHUNK)
                if not chunk:
                    break
                _write_all(fd_out, chunk)
                copied += len(chunk)
            os.fchmod(fd_out, mode)
        finally:
            os.close(fd_out)
    finally:
        os.close(fd_in)
    return copied


def _kind(mode: int) -> str:
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "special file"


def copy_tree(src: Path, dst: Path, patterns: Sequence[str]) -> CopyStats:
    """Copy ``src`` into the existing directory ``dst`` (see the module docstring).

    Directories are created 0700 and get their own permission bits only after
    everything under them is written, so read-only directories copy too. Raises
    :class:`CopyError` on the first failure."""
    stats = CopyStats()
    dir_modes: list[tuple[str, str, int]] = []  # (rel, dst path, mode), parents first
    pending = [""]
    while pending:
        rel_dir = pending.pop()
        src_dir = os.path.join(src, rel_dir) if rel_dir else str(src)
        try:
            with os.scandir(src_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise CopyError(rel_dir or ".", _reason(exc)) from exc
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise CopyError(rel, _reason(exc)) from exc
            kind = st.st_mode
            if is_excluded(rel, stat.S_ISDIR(kind), patterns):
                stats.excluded += 1
                continue
            target = os.path.join(dst, rel)
            try:
                if stat.S_ISLNK(kind):
                    os.symlink(os.readlink(entry.path), target)
                    stats.files += 1
                elif stat.S_ISDIR(kind):
                    os.mkdir(target, 0o700)
                    dir_modes.append((rel, target, stat.S_IMODE(kind) & 0o777))
                    pending.append(rel)
                elif stat.S_ISREG(kind):
                    stats.bytes += _copy_file(entry.path, target, stat.S_IMODE(kind) & 0o777)
                    stats.files += 1
                else:
                    print(f"light-copy: skipped {_kind(kind)} {rel}", file=sys.stderr)
                    stats.excluded += 1
            except OSError as exc:
                raise CopyError(rel, _reason(exc)) from exc
    for rel, path, dir_mode in reversed(dir_modes):  # children before parents
        try:
            os.chmod(path, dir_mode)
        except OSError as exc:
            raise CopyError(rel, _reason(exc)) from exc
    return stats


def write_token(dst: Path, token: str) -> None:
    """Write ``token`` to ``<dst>/configs/.auth_token`` with mode 0600.

    ``configs/`` is created 0700 when the volume had none. A read-only
    ``configs/`` (copied with its own bits) is opened up just long enough to
    write the file. Never writes through a symlink."""
    configs = os.path.join(dst, "configs")
    try:
        try:
            st = os.lstat(configs)
        except FileNotFoundError:
            os.mkdir(configs, 0o700)
            st = os.lstat(configs)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise CopyError("configs", "is not a directory; refusing to write the token through it")
        configs_mode = stat.S_IMODE(st.st_mode)
        if configs_mode & 0o300 != 0o300:
            os.chmod(configs, configs_mode | 0o700)
        try:
            fd = os.open(
                os.path.join(configs, ".auth_token"),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                0o600,
            )
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, token.encode("utf-8"))
            finally:
                os.close(fd)
        finally:
            if configs_mode & 0o300 != 0o300:
                os.chmod(configs, configs_mode)
    except OSError as exc:
        raise CopyError(TOKEN_RELPATH, _reason(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    """Entry point of ``molecule-runtime-light-copy``. Returns the exit code."""
    parser = argparse.ArgumentParser(
        prog="molecule-runtime-light-copy",
        description="Copy a parked agent's volume into a Light run's private directory.",
    )
    parser.add_argument("--src", required=True, help="the agent's volume root (read-only)")
    parser.add_argument("--dst", required=True, help="the run's empty private directory")
    parser.add_argument("--token-env", required=True, help="env var holding the run token")
    args = parser.parse_args(argv)

    started = time.monotonic()
    src, dst = Path(args.src), Path(args.dst)
    try:
        if not src.is_dir():
            raise CopyError(".", f"--src {src} is not a directory")
        if dst.is_symlink() or not dst.is_dir():
            raise CopyError(".", f"--dst {dst} is not a directory")
        token = os.environ.get(args.token_env, "").strip()
        if not token:
            raise CopyError(TOKEN_RELPATH, f"env {args.token_env} is empty or unset; a run needs its token")
        try:
            token.encode("utf-8")  # a byte that is not UTF-8 arrives as a lone surrogate
        except UnicodeEncodeError:
            raise CopyError(TOKEN_RELPATH, f"env {args.token_env} is not UTF-8 text") from None
        patterns = load_patterns(os.environ.get(EXCLUDE_FILE_ENV, "").strip() or None)
        stats = copy_tree(src, dst, patterns)
        write_token(dst, token)
    except CopyError as exc:
        print(f"light-copy: FAILED {exc.rel}: {exc.reason}", file=sys.stderr, flush=True)
        return 1

    summary = {
        "files": stats.files,
        "bytes": stats.bytes,
        "excluded": stats.excluded,
        "seconds": round(time.monotonic() - started, 3),
    }
    print(json.dumps(summary, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
