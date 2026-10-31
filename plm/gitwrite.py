"""The commit primitive — how plm writes into a bare repo.

Git-first means every design write ends up as **a commit on a draft branch**,
whoever asked for it: a `git push` from an agent with nothing but stock git, a
`set_attributes` through the MCP, a BOM edit in the browser. The surfaces stay;
their backend becomes this module, and then `plm/gitindex.py` brings the
database up to date from the commit that was written. One writer, one history,
and a DB that is derived again (derived data is rebuildable, never
precious).

PLUMBING, NOT A WORKING TREE — the same discipline `plm/split.py` uses. A bare
repo has no checkout, and creating one to change three lines of `plm.toml` would
mean writing every mesh of the revision to disk. Instead: a temp index seeded
from the parent tree (`read-tree`), `hash-object -w` for new blobs,
`update-index` for the deltas, `write-tree`, `commit-tree`, `update-ref`.

TWO THINGS THAT ARE NOT OPTIONAL:

  * **Compare-and-swap on the ref.** `update-ref <ref> <new> <old>` fails if the
    branch moved since we read it, so two concurrent writers cannot clobber each
    other — the loser is told to retry, rather than the loser's change silently
    disappearing. The per-repo flock on top of it makes that rare instead of
    routine, but the CAS is the part that is actually load-bearing, because the
    other writer may be a `git push` in a different container.
  * **The same validation the push path runs** (`plm/gitvalidate.check`). A
    commit plm makes itself is not more trustworthy than one an agent pushes; if
    it were exempt, the two paths would drift and the exempt one would be the
    one that breaks a clone.

A repo that has been written to is no longer disposable, so every commit drops a
marker (`plm-written`) that `plm/split.py` refuses to regenerate over.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import gitvalidate, manifest, safegit

GIT = os.environ.get("PLM_GIT", "git")
from . import config  # noqa: E402
REPOS = config.REPOS          # one home: plm/config.py

MODE_BLOB = "100644"
MODE_GITLINK = "160000"

BOT_EMAIL = config.BOT_EMAIL     # one home: plm/config.py

# The file that says "this repo holds work that was not generated from the DB".
# A marker file rather than a commit trailer, for one blunt reason: a PUSHED
# commit is the author's, byte for byte, and adding a trailer to it would change
# its SHA — which is the exact thing this whole stage exists not to do.
MARKER = "plm-written"


class WriteError(RuntimeError):
    pass


def repo_path(number: str) -> Path:
    """⛔ THE GATE (L2) — `safegit.safe_number` decides whether a caller's string
    may be joined to the repos root. This was one of the four helpers that built
    `REPOS / f"{n}.git"` with no regex at all."""
    try:
        p = safegit.repo_dir(REPOS, number)
    except safegit.SafeError as exc:
        raise WriteError(str(exc)) from None
    if not (p / "HEAD").exists():
        raise WriteError(f"no repository for {number} under {REPOS}")
    return p


def _git(repo: Path, *args: str, stdin: bytes | None = None,
         env: dict | None = None, check: bool = True,
         timeout: float | None = None) -> str:
    """One name over `safegit.git` — same timeout, same capture ceiling, same
    `GIT_CONFIG_NOSYSTEM` as every other git call in `plm/` (M5)."""
    try:
        return safegit.git(repo, *args, stdin=stdin, env=env, check=check,
                           timeout=timeout)
    except safegit.SafeError as exc:
        raise WriteError(str(exc)) from None


class lock:
    """One writer at a time per repo, across processes.

    An in-process lock would not do: the other writer is a `receive-pack` hook
    in the git container, and the only thing the two share is the filesystem.

    ⭐ NON-BLOCKING WITH A DEADLINE (M5). It used to be a bare `LOCK_EX`: one
    wedged git process held this forever and every subsequent write to the repo
    parked an HTTP worker until the pool was gone, with nothing logged. It is
    `safegit.flock` now — `LOCK_NB` in a bounded retry that raises a 409 naming
    the file and the pid that holds it.

    ⭐ RE-ENTRANT PER THREAD (stage 11b). `plm/gitkeep.py` writes a keep-ref
    under the owning repo's lock, and is reached from code that may already
    hold it (a read-mode sync re-splitting an item, a guard after a ref move).
    A second `flock` on a new descriptor in the same process blocks against
    the first — it would have waited out its deadline against itself. The
    thread that holds the lock just proceeds.
    """

    _held = threading.local()

    def __init__(self, repo: Path, wait: float | None = None):
        self._f = safegit.flock(Path(repo) / "plm-write.lock",
                                what=f"{Path(repo).name}'s write lock",
                                wait=wait)
        self.path = self._f.path
        self._nested = False

    def __enter__(self):
        held = getattr(lock._held, "paths", None)
        if held is None:
            held = lock._held.paths = {}
        key = str(self.path)
        if held.get(key):
            held[key] += 1
            self._nested = True
            return self
        self._f.__enter__()
        held[key] = 1
        return self

    def __exit__(self, *exc):
        held = lock._held.paths
        key = str(self.path)
        held[key] -= 1
        if self._nested:
            return False
        held.pop(key, None)
        return self._f.__exit__(*exc)


# -- the marker: this repo is no longer regenerable ---------------------------

def mark_written(repo: Path, entry: dict) -> None:
    """Append one line to the repo's marker file. It is a JSONL ledger, not a
    flag, because `split --force` deserves to print what it would destroy."""
    with (repo / MARKER).open("a") as fh:
        fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             **entry}) + "\n")


def written_log(number_or_repo) -> list[dict]:
    repo = (number_or_repo if isinstance(number_or_repo, Path)
            else REPOS / f"{number_or_repo}.git")
    p = repo / MARKER
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def is_live(number_or_repo) -> bool:
    """True once a repo holds work that `plm/split.py` did not generate."""
    repo = (number_or_repo if isinstance(number_or_repo, Path)
            else REPOS / f"{number_or_repo}.git")
    return (repo / MARKER).is_file()


def _lfs_changes(repo: Path, base: str, changes: dict[str, bytes | None]
                 ) -> None:
    """Stage 17: plm's own commits follow the push door's LFS policy, IN
    PLACE — a file `lfs.wants_lfs` names is stored in the LFS store and
    committed as its pointer, and `.gitattributes` is made to say so (written
    from the template when the tree has none; otherwise the missing lines are
    appended, never rewritten). One rule for every door: a mesh uploaded
    through the API is the same tree a git-lfs client would have pushed."""
    from . import lfs
    tracked: list[str] = []
    for path, data in sorted(changes.items()):
        if data is None or path == ".gitattributes":
            continue
        if lfs.parse_pointer(data):
            tracked.append(path)
        elif lfs.wants_lfs(path, len(data)):
            oid, n = lfs.put_bytes(data)
            changes[path] = lfs.pointer(oid, n)
            tracked.append(path)
    if not tracked:
        return
    attrs = changes.get(".gitattributes")
    if attrs is None and base:
        try:
            attrs = read_blob(repo, base, ".gitattributes")
        except WriteError:
            attrs = None
    if not attrs:
        changes[".gitattributes"] = lfs.gitattributes(
            p for p in tracked if not lfs.is_mesh(p))
        return
    need = sorted({(f"*{Path(p).suffix}" if lfs.is_mesh(p) else
                    "/" + lfs._attr_path(p))
                   for p in tracked if not lfs.tracks(attrs, p)})
    if need:
        changes[".gitattributes"] = (
            attrs.rstrip(b"\n") + b"\n\n# plm: Git LFS (docs/LARGE-FILES.md)\n"
            + "".join(f"{pat} {lfs._LFS_ATTRS}\n" for pat in need).encode())


# -- reading what is there ----------------------------------------------------

def read_blob(repo: Path, rev: str, path: str) -> bytes:
    try:
        return safegit.git(repo, "cat-file", "blob",
                           f"{safegit.safe_ref(rev)}:{safegit.safe_path(path)}",
                           binary=True)
    except safegit.SafeError as exc:
        raise WriteError(f"{path} is not readable in {rev} of {repo.name}: "
                         f"{exc}") from None


def read_manifest(repo: Path, rev: str) -> dict:
    """The parsed manifest at a ref, in `plm.manifest.build()`'s dict shape —
    ready to mutate and hand back to `commit(manifest=…)`."""
    return manifest.parse(read_blob(repo, rev, "plm.toml").decode())


def _manifest_or_empty(repo: Path, base: str) -> dict:
    try:
        return read_manifest(repo, base) if base else {}
    except WriteError:
        return {}


def tip(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--quiet", ref,
                check=False).strip()


# -- .gitmodules: the submodule's URL, beside the gitlink that pins it ---------
#
# A gitlink alone is half a submodule: `git clone --recurse-submodules` reads
# the URL out of `.gitmodules`, so a BOM child added without one is a child
# nobody can fetch. plm writes RELATIVE urls (§5.1) — `../KML-PRT-1013.git`
# resolves against whatever URL the parent was cloned from, which is what keeps
# the structure intact through a host move or a GitHub mirror.

def gitmodules_set(text: str, path: str, url: str = "") -> str:
    """Add, replace or (url='') remove one submodule stanza. Sorted by path, so
    two agents adding two children produce the same file either way round."""
    subs: dict[str, str] = {}
    cur = ""
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("[submodule "):
            cur = s[len("[submodule "):].rstrip("]").strip().strip('"')
            subs.setdefault(cur, "")
        elif cur and s.startswith("url"):
            subs[cur] = s.split("=", 1)[1].strip()
        elif cur and s.startswith("path"):
            # The stanza name is conventionally the path; trust `path =`.
            p = s.split("=", 1)[1].strip()
            if p != cur:
                subs[p] = subs.pop(cur, "")
                cur = p
    if url:
        subs[path] = url
    else:
        subs.pop(path, None)
    out = []
    for p in sorted(subs):
        out += [f'[submodule "{p}"]', f"\tpath = {p}", f"\turl = {subs[p]}"]
    return ("\n".join(out) + "\n") if out else ""


def _remove(repo: Path, path: str, env: dict) -> None:
    """Drop one path from the temp index.

    `update-index --force-remove` refuses in a bare repo ("this operation must
    be run in a work tree"), which is every repo here. `--index-info` with the
    null mode is the plumbing that does not care whether a checkout exists —
    the same reason the rest of this module is plumbing.
    """
    _git(repo, "update-index", "--index-info", env=env,
         stdin=f"000000 {'0' * 40}\t{path}\n".encode())


def submodule_url(path: str) -> str:
    """`parts/KML-PRT-1013` -> `../KML-PRT-1013.git`; `catalog` -> `../catalog.git`."""
    return f"../{path.rsplit('/', 1)[-1]}.git"


def draft_ref(topic: str) -> str:
    topic = topic.strip("/")
    return f"refs/heads/draft/{topic}" if not topic.startswith("draft/") \
        else f"refs/heads/{topic}"


def branch_for_commit(repo: Path, commit: str) -> str:
    """Which draft branch has this commit as its tip. That is how a git-origin
    revision finds its branch again at approval time without plm inventing a
    second home for a fact git already stores."""
    for ref, sha in gitvalidate.draft_branches(repo).items():
        if sha == commit:
            return ref
    return ""


def reset_ref(number: str, ref: str, *, back_to: str, only_if: str) -> dict:
    """Put a ref back where it was — the undo half of a two-store write.

    Called when the DATABASE half of an operation rolled back after the commit
    had already landed (`plm/db.py` `PlmConnection.on_rollback`). Two things
    make it safe rather than a second race:

      * it runs under the same per-repo `flock` the write took, so nothing can
        slip between the read and the reset;
      * `only_if` is the sha the write left behind, and `update-ref` is given it
        as the expected old value — a compare-and-swap. If somebody else has
        moved the ref since (a `git push` in the git container is a real
        possibility), the reset is DECLINED rather than clobbering their work.
        Git is the record; when it has moved on, the registry catches up at the
        next edit or at `--rebuild`, which is the existing healing path.

    `back_to` empty means the ref did not exist before the write, so the undo is
    to delete it.
    """
    try:
        repo = repo_path(number)
    except WriteError as exc:
        return {"reset": False, "why": str(exc)}
    with lock(repo):
        now = tip(repo, ref)
        if now != only_if:
            return {"reset": False, "ref": ref, "why":
                    f"{ref} is at {now[:10] or '(gone)'}, not the "
                    f"{only_if[:10]} this write left — somebody else moved it, "
                    "so it is theirs now."}
        args = ["update-ref", "-d", ref, only_if] if not back_to else \
            ["update-ref", ref, back_to, only_if]
        _git(repo, *args)
        mark_written(repo, {"commit": back_to, "ref": ref, "origin": "rollback",
                            "actor": "plm", "undid": only_if})
    return {"reset": True, "ref": ref, "from": only_if, "to": back_to}


def branch_containing(repo: Path, commit: str) -> str:
    """The one draft branch this commit is on, when it is not the tip.

    The registry can fall BEHIND the branch: a commit lands and the database
    transaction that would have recorded it rolls back, or somebody re-pushes
    while an edit is in flight. Git is the record, so the answer is to catch up
    with the branch rather than to refuse the next edit — but only when there is
    exactly ONE branch it could mean. Two would be a guess, and this module does
    not guess about where work goes.
    """
    on = [ref for ref, sha in gitvalidate.draft_branches(repo).items()
          if sha != commit and safegit.ok(repo, "merge-base", "--is-ancestor",
                                          commit, sha)]
    return on[0] if len(on) == 1 else ""


# -- the write ----------------------------------------------------------------

def commit(number: str, ref: str, *, changes: dict[str, bytes | None] | None = None,
           manifest_doc: dict | None = None, message: str, actor: str,
           origin: str, parent: str = "", validate: bool = True,
           email: str = "", links: dict[str, str | None] | None = None,
           new_branch: bool = False) -> dict:
    """Write one commit onto `ref` and return what happened.

    `changes` maps a repo path to its new bytes, or to None to delete it.
    `manifest_doc` is a mutated `plm.manifest` dict; it is re-emitted with the
    deterministic emitter, so changing one attribute changes one line.
    `links` maps a submodule path to the child commit it pins, or to None to
    remove it — the BOM verbs' half of §5.1, "the gitlink IS the pin". The
    `.gitmodules` stanza is written with it, because a gitlink without a URL is
    a child `git clone --recurse-submodules` cannot fetch.

    `parent` pins the commit the caller read (compare-and-swap). Omit it and the
    current tip of `ref` is used — which is safe under the lock but loses the
    "somebody else moved it" answer, so callers that read first should pass it.
    `new_branch` says the ref is expected NOT to exist (revise): `parent` is
    then the commit the new branch grows from rather than the ref's old tip.
    """
    repo = repo_path(number)
    changes = dict(changes or {})
    links = dict(links or {})

    with lock(repo):
        old = tip(repo, ref)
        if new_branch and old:
            raise WriteError(
                f"{ref[len('refs/heads/'):]} already exists in {number} "
                f"({old[:10]}) — pick another branch name, or push to the one "
                "that is there.")
        base = parent or old or tip(repo, gitvalidate.default_ref(repo))
        if parent and old and not new_branch and parent != old:
            raise WriteError(
                f"{ref} moved while you were editing ({parent[:10]} -> "
                f"{old[:10]}); re-read the manifest and try again.")
        if not base and not new_branch:
            raise WriteError(
                f"{number} has no history to build on — nothing has ever been "
                "checked in.")
        if links:
            text = ""
            try:
                text = read_blob(repo, base, ".gitmodules").decode() if base \
                    else ""
            except WriteError:
                pass
            for path, sha in links.items():
                text = gitmodules_set(text, path,
                                      submodule_url(path) if sha else "")
            changes[".gitmodules"] = text.encode() if text else None
        if manifest_doc is not None:
            changes["plm.toml"] = manifest.dumps(
                manifest.document(manifest_doc)).encode()
        _lfs_changes(repo, base, changes)
        if not changes and not links:
            raise WriteError(
                "nothing to commit — pass changes=, links= or manifest_doc=")

        idx = repo / f"write-index-{os.getpid()}"
        env = {"GIT_INDEX_FILE": str(idx)}
        try:
            if idx.exists():
                idx.unlink()
            if base:
                _git(repo, "read-tree", base, env=env)
            for path, data in sorted(changes.items()):
                if data is None:
                    _remove(repo, path, env)
                    continue
                sha = _git(repo, "hash-object", "-w", "--stdin",
                           stdin=data).strip()
                _git(repo, "update-index", "--add", "--cacheinfo",
                     f"{MODE_BLOB},{sha},{path}", env=env)
            for path, sha in sorted(links.items()):
                if sha is None:
                    _remove(repo, path, env)
                    continue
                _git(repo, "update-index", "--add", "--cacheinfo",
                     f"{MODE_GITLINK},{sha},{path}", env=env)
            tree = _git(repo, "write-tree", env=env).strip()
        finally:
            if idx.exists():
                idx.unlink()

        if base and tree == _git(repo, "rev-parse", f"{base}^{{tree}}").strip():
            return {"ok": True, "number": number, "ref": ref, "commit": base,
                    "created": False, "note": "no change — the tree is identical"}

        body = message.rstrip("\n") + "\n\n"
        # `catalog.git` has no `plm.toml` at its root — it is a repo of many
        # items, one TOML each (§5.4) — so a commit there carries no version.
        doc = manifest_doc if manifest_doc is not None else _manifest_or_empty(
            repo, base)
        body += (f"Plm-Item: {number}\n"
                 f"Plm-Version: {doc.get('version', '')}\n"
                 f"Plm-Origin: {origin}\n"
                 f"Plm-Actor: {actor}\n")
        when = f"{int(time.time())} +0000"
        ident = {"GIT_AUTHOR_NAME": actor, "GIT_AUTHOR_EMAIL": email or BOT_EMAIL,
                 "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_NAME": actor,
                 "GIT_COMMITTER_EMAIL": email or BOT_EMAIL,
                 "GIT_COMMITTER_DATE": when}
        sha = _git(repo, "commit-tree", tree, *(("-p", base) if base else ()),
                   stdin=body.encode(), env=ident).strip()

        if validate:
            # Exactly what a push would face. A commit plm made is not more
            # trustworthy than one an agent pushed; the object is already
            # written but no ref points at it, so a refusal leaves an
            # unreachable object that the next gc collects — the same cost a
            # rejected push pays.
            gitvalidate.check(repo, number, sha, ref=ref, old=old)

        _git(repo, "update-ref", ref, sha, old or ("0" * 40))
        mark_written(repo, {"commit": sha, "ref": ref, "origin": origin,
                            "actor": actor, "version": doc.get("version", "")})
    # PIN SITE (stage 11b): every child this commit pins stays reachable in the
    # CHILD's repo. Outside this repo's lock on purpose — the keep takes the
    # child's, and holding two repos' locks at once is how two writers deadlock.
    from . import gitkeep
    kept = gitkeep.protect(
        [(gitkeep.link_target(p), s, f"gitlink {number}@{sha[:10]}:{p}")
         for p, s in sorted(links.items()) if s],
        where=f"gitwrite {number}")
    return {"ok": True, "number": number, "ref": ref, "commit": sha,
            "parent": base, "created": True, "version": doc.get("version", ""),
            "paths": sorted(changes), **({"kept": kept} if kept else {})}
