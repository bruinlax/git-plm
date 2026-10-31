"""The git vault: where checked-in files actually live.

plm writes the same folder convention the lantern project established by hand
(`<class-dir>/<NUMBER>_<slug>/versions/vX.Y.Z/…`), commits each check-in, and
uses git for revision-to-revision diffs. Imported history predates per-revision
commits, so diff falls back to comparing version folders directly.
"""
from __future__ import annotations

import difflib
import random
import re
import subprocess
import threading
import time
from pathlib import Path

from . import config, safegit
from .storage import sha256_file

GIT_ID = ["-c", "user.name=plm", "-c", f"user.email={config.BOT_EMAIL}"]
TEXT_EXT = {".md", ".scad", ".csv", ".txt", ".json", ".ini", ".gcode", ".svg", ".sha256"}


class VaultError(Exception):
    pass


# -- THE SECOND LOCK ------------------------------------------------
# SQLite's writer lock is not the only one in a check-in's critical path. git has
# `.git/index.lock`, it has NO timeout and NO retry, and on this vault — a big
# repo on the network-synced volume — the operations that take it are slow:
# measured 2026-08-13 on production, `git add -f -- <item folder>` ~1 s,
# `git status --porcelain -- <item folder>` ~8 s (git refreshes the whole index
# regardless of the pathspec), `git rev-parse HEAD` ~2 s. Twelve concurrent
# check-ins therefore queue on index.lock, and each one that loses gets an
# immediate hard failure rather than a wait.
#
# Two guards, and neither of them tries to be clever about other people's locks:
#
#   _VAULT_LOCK  plm's own writers take a process lock first, so plm never
#                contends with itself. (plm and plm-mcp are separate processes
#                and still can, which is what the retry is for.)
#   retry        a bounded, backed-off retry when git says the index is locked,
#                with a refusal that names the file and the fix. A STALE lock —
#                left behind by a killed git — is never removed automatically:
#                deleting another process's lock file is how a repository gets
#                corrupted, so plm says where it is and lets a human decide.
_VAULT_LOCK = threading.RLock()
_LOCK_MARKERS = ("index.lock", "unable to create", "cannot lock ref",
                 "another git process")
GIT_RETRIES = 4


def _looks_locked(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _LOCK_MARKERS)


def _git(*args: str, check: bool = True) -> str:
    """Run one git command in the vault, waiting out git's own index lock."""
    delay = 0.25
    for attempt in range(GIT_RETRIES + 1):
        # ⛔ M5 — a `timeout=`. The vault is on a network-synced volume; a git
        # command there can stall on the filesystem, and this is the one git
        # call in `plm/` that is not a bare repo (so it is `-C`, not the shared
        # `--git-dir` helper). The ceiling is the same one.
        try:
            res = subprocess.run(
                ["git", "-C", str(config.DATA_ROOT), *GIT_ID, *args],
                capture_output=True, text=True,
                timeout=safegit.TIMEOUT_LONG_S)
        except subprocess.TimeoutExpired:
            raise VaultError(
                f"git {' '.join(args[:3])} in the vault did not finish within "
                f"{safegit.TIMEOUT_LONG_S:g}s and was killed.\n"
                "The vault lives on a synced volume; a stall there is usually "
                "the filesystem,\nnot git. PLM_GIT_TIMEOUT_LONG raises the "
                "ceiling once you know which.") from None
        if res.returncode == 0:
            return res.stdout
        err = (res.stderr or res.stdout).strip()
        if _looks_locked(err) and attempt < GIT_RETRIES:
            time.sleep(delay + random.uniform(0, delay))
            delay *= 2
            continue
        if not check:
            return res.stdout
        if _looks_locked(err):
            raise VaultError(
                f"{err[:800]}\n\nThe vault's git index has been locked for "
                f"{GIT_RETRIES + 1} attempts. Either another check-in is still "
                "running (retry in a moment — nothing was written), or a killed "
                "git left a stale .git/index.lock behind, in which case delete "
                "that ONE file by hand after confirming no git process is "
                "running. plm does not remove other processes' lock files.")
        raise VaultError(err[:2000])
    raise VaultError("git: retries exhausted")  # unreachable; keeps mypy honest


def safe_path(rel: str) -> Path:
    p = (config.DATA_ROOT / rel).resolve()
    if not str(p).startswith(str(config.DATA_ROOT.resolve())):
        raise VaultError(f"path escapes the vault: {rel}")
    if ".git" in Path(rel).parts:
        raise VaultError("not served")
    return p


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:60] or "item"


def item_folder(project: str, kind: str, number: str, name: str) -> str:
    # assets/ keeps reference geometry — the camper you bolt to — visibly out of
    # parts/, where everything is something you make or buy (docs/PLM_RELATIONS.md).
    subdir = {"assembly": "assemblies", "coupon": "coupons",
              "asset": "assets"}.get(kind, "parts")
    return f"projects/{project}/{subdir}/{number}_{slugify(name)}"


def head() -> str:
    """The vault's current commit. Captured before a write so the write can be
    undone if the transaction it belongs to fails (ops.checkin)."""
    return _git("rev-parse", "HEAD", check=False).strip()


def version_dir_exists(folder: str, version: str) -> bool:
    """Did this version folder exist BEFORE we wrote to it? Rollback needs the
    answer: a folder plm created is removed outright, one that already had
    content is restored to what git last recorded."""
    try:
        return safe_path(f"{folder}/versions/v{version}").is_dir()
    except VaultError:
        return False


def write_checkin(folder: str, version: str,
                  files: list[dict]) -> list[dict]:
    """Write a revision's files into <folder>/versions/v<version>/ and return
    file records (path, sha256, size). `files` entries carry name + bytes."""
    vdir = safe_path(f"{folder}/versions/v{version}")
    with _VAULT_LOCK:
        vdir.mkdir(parents=True, exist_ok=True)
        out = []
        for f in files:
            name = Path(f["name"]).name  # no nested paths from clients
            dest = vdir / name
            dest.write_bytes(f["data"])
            sha, size = sha256_file(dest)
            out.append({
                "path": str(dest.relative_to(config.DATA_ROOT)),
                "sha256": sha,
                "size": size,
                "role": f.get("role", ""),
            })
        # Manifest covers everything in the version folder, not just this write —
        # working (DRAFT) uploads land file-by-file into an existing folder.
        entries = []
        for p in sorted(vdir.rglob("*")):
            if p.is_file() and p.name != "MANIFEST.sha256":
                entries.append(f"{sha256_file(p)[0]}  {p.relative_to(vdir)}")
        (vdir / "MANIFEST.sha256").write_text("\n".join(entries) + "\n")
    return out


def commit(folder: str, message: str) -> str:
    """Stage one item folder and commit. -f because artifact extensions (.stl)
    are gitignored as *generated* outputs; vault copies are controlled records.

    Serialized against plm's other vault writers (_VAULT_LOCK): these four git
    invocations are not individually atomic, and two check-ins interleaving
    inside them is how one commit ends up carrying the other's files."""
    with _VAULT_LOCK:
        _git("add", "-f", "--", folder)
        status = _git("status", "--porcelain", "--", folder)
        if not status.strip():
            return _git("rev-parse", "HEAD").strip()  # nothing new: reuse HEAD
        _git("commit", "-m", message, "--", folder)
        return _git("rev-parse", "HEAD").strip()


def rollback_checkin(folder: str, version: str, *, head_before: str,
                     existed: bool, message: str) -> dict:
    """Undo the VAULT half of a check-in whose database half failed.

    The registry's atomicity story has two stores in it: SQLite rows, which roll
    back with the transaction, and the git vault, which does not. Before this,
    a check-in that raised after `commit()` left a committed version folder that
    no revision row pointed at — bytes in the vault claiming to be a revision
    that does not exist, and a re-import would have registered them.

    Deliberately narrow, because a compensating action that overreaches is worse
    than the leak it fixes. A check-in writes exactly ONE directory,
    `<folder>/versions/v<version>/`, so that is all this touches:

      - the directory is removed;
      - if it existed before, its previous content is restored from
        `head_before` (so a working upload onto an existing DRAFT folder is put
        back, not deleted);
      - the undo is COMMITTED, because this is a git vault and the honest record
        of "we wrote this and then took it back" is a commit, not a silent
        `reset --hard` that would also discard whatever else is in flight.

    Nothing outside that one directory is ever staged, reset or cleaned.
    """
    import shutil
    out = {"folder": folder, "version": version, "head_before": head_before,
           "removed": False, "restored": False, "git_commit": ""}
    rel = f"{folder}/versions/v{version}"
    with _VAULT_LOCK:
        try:
            vdir = safe_path(rel)
        except VaultError:
            return out
        if vdir.is_dir():
            shutil.rmtree(vdir)
            out["removed"] = True
        if existed and head_before:
            # Restores the tracked content this folder had at head_before.
            _git("checkout", head_before, "--", rel, check=False)
            out["restored"] = safe_path(rel).is_dir()
        if _git("status", "--porcelain", "--", folder, check=False).strip():
            _git("add", "-f", "-A", "--", folder, check=False)
            _git("commit", "-m", message, "--", folder, check=False)
        out["git_commit"] = head()
    return out


def move_folder(old: str, new: str, message: str) -> str:
    """Move an item's folder inside the vault and commit the move.

    Used by ops.renumber_item: the folder name carries the item number, so a
    renumber that left `GL-ELEC-001_…/` on disk would put the vault and the
    registry into permanent disagreement. shutil.move + `git add -A` rather than
    `git mv` because some tracked bytes here were force-added past .gitignore
    (artifact extensions) and git mv is fussy about those; `add -A` stages the
    deletion and the addition, which is the same commit git would have made —
    and git still records it as a rename in the log, since the content matches.
    """
    import shutil
    with _VAULT_LOCK:
        src, dst = safe_path(old), safe_path(new)
        if not src.is_dir():
            raise VaultError(f"no such vault folder: {old}")
        if dst.exists():
            raise VaultError(f"target folder already exists: {new}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        _git("add", "-f", "-A", "--", old, new)
        if _git("status", "--porcelain", "--", old, new).strip():
            _git("commit", "-m", message, "--", old, new)
        return _git("rev-parse", "HEAD").strip()


def diff_commits(a: str, b: str, folder: str) -> str:
    return _git("diff", f"{a}..{b}", "--stat", "--patch", "--", folder, check=False)


def diff_folders(folder: str, va: str, vb: str) -> str:
    """Filesystem diff between two version folders, for imported history that
    has no per-revision commits. Text files get unified diffs; binaries are
    reported by size/hash change."""
    da, db_ = safe_path(f"{folder}/versions/v{va}"), safe_path(f"{folder}/versions/v{vb}")
    if not da.is_dir() or not db_.is_dir():
        return f"(no version folders to compare under {folder})"
    names = sorted({p.name for p in da.iterdir() if p.is_file()} |
                   {p.name for p in db_.iterdir() if p.is_file()})
    chunks = []
    for n in names:
        fa, fb = da / n, db_ / n
        if not fa.exists():
            chunks.append(f"+ added   {n} ({fb.stat().st_size:,} B)")
        elif not fb.exists():
            chunks.append(f"- removed {n} ({fa.stat().st_size:,} B)")
        elif Path(n).suffix.lower() in TEXT_EXT:
            d = list(difflib.unified_diff(
                fa.read_text(errors="replace").splitlines(keepends=True),
                fb.read_text(errors="replace").splitlines(keepends=True),
                fromfile=f"v{va}/{n}", tofile=f"v{vb}/{n}"))
            if d:
                chunks.append("".join(d))
        else:
            ha, hb = sha256_file(fa)[0], sha256_file(fb)[0]
            if ha != hb:
                chunks.append(f"~ changed {n}  {fa.stat().st_size:,} B -> {fb.stat().st_size:,} B"
                              f"  ({ha[:12]} -> {hb[:12]})")
    return "\n".join(chunks) or "(no file-level differences)"
