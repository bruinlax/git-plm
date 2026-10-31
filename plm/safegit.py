"""The gate every user string passes through on its way to `git` or the disk.

⭐ ONE GATE, NOT FIVE. Before stage 7 the rules lived in `plm/gitrepo.py` and
were applied only there: `gitindex.repo_of`, `gitindex.parse_key`,
`gitwrite.repo_path` and `ops._rename_repo_dir` each built `REPOS / f"{n}.git"`
from an unvalidated string, and `parse_key` handed `{commit}:{path}` to
`git cat-file` with no `--`. The 2026-09-17 security review found four of them
(L2) and one real read primitive behind them (H1). `plm/gitrepo.py`'s regexes
were the right ones — they are here now, once, and gitrepo re-exports them so
the module the review called "the model the rest should copy" stays the model.

This module imports NOTHING from `plm`. That is deliberate: `gitrepo`,
`gitindex`, `gitwrite`, `gitvalidate`, `gitnotes`, `storage`, `split`, `ops` and
`itemref` all sit above it, and a shared gate with an import cycle is a gate
somebody will route around.

WHAT IT GIVES YOU

  safe_number(n)   an item number that may become a directory name
  safe_ref(r)      a ref that cannot be read as an option or a range
  safe_path(p)     a path inside a tree that cannot climb out of it
  safe_sha(s)      40/64 hex, for the places that must have a resolved object
  git(...)         ONE subprocess helper: argv list, `timeout=`, an output
                   ceiling, a clean environment, and `--end-of-options`
  stream(...)      the same, for bytes too big to hold — copied from the shape
                   `storage._git_stream` already had, with a byte ceiling
  flock(...)       LOCK_NB with a bounded retry, so a wedged holder is a 409
                   naming it rather than a parked worker thread

⚠️ `--end-of-options` vs `--`. They stop different parsers. `--end-of-options`
(git 2.24+) ends OPTION parsing, so a ref beginning `-` is a ref; `--` ends
REVISION parsing, so a path that looks like a ref is a path. `git()` inserts
neither by guessing — callers pass `end_of_options=True` for a command that
takes refs, and put `--` in the argv themselves where the command takes paths,
because only the caller knows which argument is which. `safe_ref` already
refuses a leading `-`; this is the second wall.
"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from pathlib import Path

GIT = os.environ.get("PLM_GIT", "git")

#: Seconds any one git subprocess may take. Long enough for a `merge-tree` or a
#: `clone --bare` of the biggest repo in the registry (85 MB blob, measured);
#: short enough that a wedged process — a corrupt pack, an NFS stall, an
#: adversarial object — surfaces as an error with a cause instead of holding a
#: repo lock and an HTTP worker forever. The review found ZERO `timeout=` in the
#: whole git layer (M5); this is the default that ends that.
TIMEOUT_S = float(os.environ.get("PLM_GIT_TIMEOUT", "120"))
#: A long one, for the handful of commands that legitimately walk everything.
TIMEOUT_LONG_S = float(os.environ.get("PLM_GIT_TIMEOUT_LONG", "900"))

#: Ceiling on the bytes a captured git command may return. `git log` over a
#: whole history or `cat-file blob` on an 85 MB mesh both want more memory than
#: an unauthenticated request should be able to ask for (H5). Anything bigger
#: than this has to stream.
MAX_CAPTURE = int(os.environ.get("PLM_GIT_MAX_CAPTURE", str(64 * 1024 * 1024)))

#: Ceiling on a STREAMED response, applied by `stream()`. The largest blob in
#: the POC's repos is 85 MB; the default is generous enough to serve it and
#: finite, which is the whole point.
MAX_STREAM = int(os.environ.get("PLM_GIT_MAX_STREAM", str(256 * 1024 * 1024)))

#: An item number is a directory name under the repos root. Same regex
#: `gitrepo.repo_path` has always had — anchored, no dots at the start, no
#: slashes, no `..`, bounded length.
_NUMBER_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,64}$")
#: A ref may be a branch, a tag, a SHA or `main`. Anything that could be read as
#: an option, a path traversal or a revision-range operator is refused rather
#: than sanitised, because a sanitiser that is wrong once is an injection.
_REF_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@^~-]{0,200}$")
_SHA_OK = re.compile(r"^[0-9a-fA-F]{7,64}$")


class SafeError(Exception):
    """Refused at the gate. Carries an HTTP status so every door answers the
    same way — `gitrepo.RepoError` and `gitindex.IndexError_` both subclass the
    idea, and this is what they share."""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


class GitTimeout(SafeError):
    def __init__(self, msg: str):
        super().__init__(msg, 504)


class GitTooBig(SafeError):
    def __init__(self, msg: str):
        super().__init__(msg, 413)


# -- the three gates ----------------------------------------------------------

def safe_number(number: str) -> str:
    """An item number that may be joined to the repos root. ⛔ Every
    `REPOS / f"{n}.git"` in this codebase goes through here."""
    n = (number or "").strip()
    if not _NUMBER_OK.match(n) or ".." in n:
        raise SafeError(f"not an item number: {number!r}", 400)
    return n


def safe_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if not ref:
        raise SafeError("empty ref")
    if not _REF_OK.match(ref) or ".." in ref:
        raise SafeError(f"not a usable ref: {ref!r}")
    return ref


def safe_path(path: str) -> str:
    path = (path or "").strip().strip("/")
    if path.startswith("-") or ".." in Path(path).parts:
        raise SafeError(f"not a usable path: {path!r}")
    return path


def safe_sha(sha: str) -> str:
    s = (sha or "").strip()
    if not _SHA_OK.match(s):
        raise SafeError(f"not an object name: {sha!r}")
    return s


def repo_dir(root: Path, number: str) -> Path:
    """`<root>/<NUMBER>.git`, gated. Returns the path whether or not it exists —
    "does this repo exist" is a different question with a different status, and
    the caller knows which 404 it wants to write."""
    return Path(root) / f"{safe_number(number)}.git"


# -- the one subprocess helper ------------------------------------------------

def clean_env(extra: dict | None = None) -> dict:
    """The environment a git child gets.

    `GIT_CONFIG_NOSYSTEM=1` is the load-bearing line: without it git reads
    `/etc/gitconfig`, which in a container is whatever the image or a future
    base-image bump put there. `GIT_CONFIG_*` from the process environment is
    kept, because that is how the operator configures the served repos
    (`uploadpack.allowFilter`, `receive.fsckObjects`) without writing a line
    into every one of them — see the compose file.
    """
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0",
           "HOME": os.environ.get("HOME", "/tmp")}
    env.update(extra or {})
    return env


def git(repo: Path | str | None, *args: str, timeout: float | None = None,
        binary: bool = False, check: bool = True, stdin: bytes | None = None,
        env: dict | None = None, end_of_options: bool = False,
        max_bytes: int | None = None, cwd: str | None = None):
    """Run one git command. The ONLY place `subprocess` meets `git` in `plm/`.

    `repo` is a bare repo's path (`--git-dir`), or None for a command that has
    no repository (`git init`). Returns stdout as `str` (or `bytes` with
    `binary=True`), or raises `SafeError` with a status.

    Everything the review asked for is here and nowhere else: a `timeout=`, a
    capture ceiling, `GIT_CONFIG_NOSYSTEM`, and an optional
    `--end-of-options` for commands whose first argument came off the wire.
    """
    argv = [GIT]
    if repo is not None:
        argv += ["--git-dir", str(repo)]
    if end_of_options and args:
        argv += [args[0], "--end-of-options", *args[1:]]
    else:
        argv += list(args)
    t = TIMEOUT_S if timeout is None else timeout
    try:
        res = subprocess.run(argv, input=stdin, capture_output=True,
                             env=clean_env(env), timeout=t, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        # NAME THE COMMAND AND THE LIMIT. A 504 that says "git timed out" sends
        # somebody to read the code; this one says which command, in which repo,
        # after how long, and which knob moves it.
        raise GitTimeout(
            f"git {' '.join(args[:3])} in "
            f"{Path(str(repo)).name if repo else '(no repo)'} did not finish "
            f"within {t:g}s and was killed. Nothing it had not already written "
            "was written.\n"
            "  A git process that hangs here is usually a corrupt pack, a "
            "stalled filesystem, or\n  an object built to be slow. Raise "
            "PLM_GIT_TIMEOUT only once you know which.") from exc
    cap = MAX_CAPTURE if max_bytes is None else max_bytes
    if len(res.stdout) > cap:
        raise GitTooBig(
            f"git {' '.join(args[:3])} returned {len(res.stdout):,} bytes, over "
            f"the {cap:,}-byte ceiling for a captured command. Something this "
            "large has to be\n  streamed (plm/safegit.py stream()), not held in "
            "memory.")
    if check and res.returncode != 0:
        raise SafeError(
            f"git {' '.join(args[:3])}"
            + (f" in {Path(str(repo)).name}" if repo else "")
            + ": " + (res.stderr or res.stdout).decode(
                errors="replace").strip()[:400], 400)
    return res.stdout if binary else res.stdout.decode(errors="replace")


def ok(repo: Path | str, *args: str, timeout: float | None = None) -> bool:
    """True when the command EXITS ZERO. For git's predicates — `merge-base
    --is-ancestor`, `cat-file -e` — which answer with a status, not with
    output, and which every caller was spelling as a bare `subprocess.run`
    because `git()` raises on a non-zero exit."""
    argv = [GIT, "--git-dir", str(repo), *args]
    t = TIMEOUT_S if timeout is None else timeout
    try:
        return subprocess.run(argv, capture_output=True, env=clean_env(),
                              timeout=t).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def stream(repo: Path | str, *args: str, timeout: float | None = None,
           max_bytes: int | None = None, chunk: int = 1 << 16):
    """Yield a git command's stdout in chunks, with a byte ceiling and a
    deadline. The shape `storage._git_stream` already had, made general.

    ⚠️ IT IS A GENERATOR, so the deadline is wall-clock across the whole read
    rather than a `subprocess.run(timeout=)`: the point is a client that reads
    one byte a second, not a child that never exits.
    """
    cap = MAX_STREAM if max_bytes is None else max_bytes
    deadline = time.monotonic() + (TIMEOUT_LONG_S if timeout is None else timeout)
    proc = subprocess.Popen([GIT, "--git-dir", str(repo), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=clean_env())
    sent = 0
    try:
        while True:
            buf = proc.stdout.read(chunk)
            if not buf:
                break
            sent += len(buf)
            if sent > cap:
                proc.kill()
                raise GitTooBig(
                    f"{' '.join(args[:3])} exceeded the {cap:,}-byte ceiling "
                    "for a single response.")
            if time.monotonic() > deadline:
                proc.kill()
                raise GitTimeout(
                    f"{' '.join(args[:3])} was still streaming after "
                    f"{TIMEOUT_LONG_S:g}s and was killed.")
            yield buf
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            err = proc.stderr.read()
        except Exception:                                   # pragma: no cover
            err = b""
        rc = proc.wait()
        if rc != 0 and sent == 0:
            raise SafeError(
                f"git {' '.join(args[:3])}: "
                f"{err.decode(errors='replace').strip()[:200]}", 400)


# -- locks that give up ------------------------------------------------------

LOCK_WAIT_S = float(os.environ.get("PLM_LOCK_WAIT", "20"))
LOCK_POLL_S = 0.1


class Busy(SafeError):
    def __init__(self, msg: str):
        super().__init__(msg, 409)


class flock:
    """An advisory `flock`, taken NON-BLOCKING in a bounded retry.

    The review's M5: both of plm's flocks took `LOCK_EX` with no `LOCK_NB` and
    no deadline, so one git process that wedged held a repo's write lock forever
    and every subsequent write to it parked an HTTP worker until the pool was
    gone — with nothing logged and no request ever returning. This waits
    `PLM_LOCK_WAIT` seconds and then refuses, 409, naming the lock file and the
    pid that holds it, because "who has it" is the only useful half of that
    answer.
    """

    def __init__(self, path: Path, what: str = "", wait: float | None = None):
        self.path = Path(path)
        self.what = what or self.path.name
        self.wait = LOCK_WAIT_S if wait is None else wait
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a+")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.fh.seek(0)
                    holder = (self.fh.read(120) or "").strip() or "(unnamed)"
                    self.fh.close()
                    self.fh = None
                    raise Busy(
                        f"{self.what} is locked by another writer and did not "
                        f"free up within {self.wait:g}s.\n"
                        f"  lock file: {self.path}\n"
                        f"  last holder recorded: {holder}\n"
                        "\n  This is normally a concurrent push or approval and "
                        "clears by itself — try again.\n  If it does not, a git "
                        "process is wedged: look for one in D state against "
                        "this repo.") from None
                time.sleep(LOCK_POLL_S)
        # Who holds it, for the next caller's refusal message. Best-effort: a
        # failure to write the courtesy line must never fail the lock.
        try:
            self.fh.seek(0)
            self.fh.truncate()
            self.fh.write(f"pid {os.getpid()} since "
                          f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
            self.fh.flush()
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None
