"""Facts that arrive AFTER a revision was frozen — `refs/notes/plm`.

A release tag's tree is immutable, and that is the point: what was printed is
what was reviewed. But some facts about a released revision are simply not
knowable when it is released, and they are real facts, not opinions:

  * the mass the slicer measured the first time somebody actually printed it;
  * a revision-level tag — `unverified-datum`, `measure-this`;
  * one BOM line suppressed on a variant build (effectivity, not design);
  * the LIFECYCLE decision itself: promoted to RELEASED, put on hold, made
    OBSOLETE — §2.7's "release status", which plain git does not have.

Before this they went to the registry only, so a clone knew nothing about them
and `--rebuild` could not re-derive them. Git's answer to "annotate an object
you must not rewrite" is **notes**, and notes have history: every change is a
commit on the notes ref, with an author and a date, so the annotation is as
auditable as the thing it annotates.

THE SHAPE. One small TOML document per commit, exactly the same emitter the
manifest uses, so a note diffs as readably as `plm.toml`:

    schema = 1

    [attributes]
    mass_g = { value = "41.2", unit = "g", source = "slicer" }

    tags = ["unverified-datum"]

    [[suppress]]
    child = "KML-HDW-0012"
    find  = 12
    note  = "not fitted on the van build"

    [[installed]]                 # a note-home relation (stage 10)
    item    = "CCR-AST-0001"
    version = "1.0.0"
    commit  = "9f3e…"
    note    = "bolted on 2026-08-30"

    [[results]]                   # a RESULT that arrived after release (stage 12)
    type        = "coupon_for"    # …of a claim the TREE makes (revision home)
    item        = "GSI-PRT-0052"
    version     = "0.1.0"
    result      = "pass"
    result_note = "slides on, no rattle"
    measured    = { gap_mm = "0.25" }

    [status]
    state    = "RELEASED"
    maturity = "released"
    approver = "geoff"
    date     = "2026-09-18"
    release  = 7

WHAT A NOTE MAY NEVER DO. It does not alter a tree and it does not move a tag,
so release immutability holds by construction: `git cat-file -p v0.4.0` is the
same bytes before and after. The indexer OVERLAYS the note onto the revision it
derived from the tree, in that order, so the registry says what plm knows and
the tag still says what was approved.

FETCHING IT. The ref is an ordinary ref and is advertised like any other:

    git fetch origin refs/notes/plm:refs/notes/plm
    git log --notes=plm main
"""
from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

from . import manifest, safegit

GIT = os.environ.get("PLM_GIT", "git")

#: The one notes ref plm writes. `refs/notes/commits` is git's default and
#: belongs to whoever is using the repo; this namespace is plm's.
REF = "refs/notes/plm"
NAME = "plm"          # what `--notes=plm` and `--ref=plm` want

SCHEMA = 1

#: `[[results]]` — the outcome of a claim the revision's TREE makes, recorded
#: after the tree was frozen (stage 12, loose end 1: print the coupon, release
#: it, THEN measure — the common case). Keyed by (type, item, version) of that
#: claim; the indexer overlays it onto the claim's row after the tree, exactly
#: as `[attributes]` overlays the manifest's. The claim itself stays in the
#: tree — a note never makes one, it only says how one came out.
RESULTS = "results"


class NoteError(RuntimeError):
    pass


def _git(repo: Path, *args: str, stdin: bytes | None = None,
         env: dict | None = None, check: bool = True) -> str:
    """One name over `safegit.git` — the shared timeout, capture ceiling and
    clean environment (M5)."""
    try:
        return safegit.git(repo, *args, stdin=stdin, env=env, check=check)
    except safegit.SafeError as exc:
        raise NoteError(str(exc)) from None


# -- the document -------------------------------------------------------------

def document(note: dict) -> str:
    """The note dict as deterministic TOML.

    Same ordering discipline as `plm.toml`: sorted keys, one fact per line, so
    adding one attribute is a one-line diff in `git log --notes=plm -p` rather
    than a reflow of the annotation.
    """
    doc: list[tuple] = [("kv", "", {"schema": SCHEMA})]
    attrs = note.get("attributes") or {}
    if attrs:
        doc += [("blank", "", None),
                ("table", "attributes", dict(sorted(attrs.items())))]
    tags = sorted({str(t) for t in (note.get("tags") or [])})
    if tags:
        doc += [("blank", "", None), ("kv", "", {"tags": tags})]
    sup = note.get("suppress") or []
    if sup:
        doc += [("blank", "", None),
                ("aot", "suppress",
                 sorted(sup, key=lambda s: (str(s.get("child") or ""),
                                            str(s.get("find") or ""))))]
    for key in note_keys():
        rows = note.get(key) or []
        if rows:
            doc += [("blank", "", None),
                    ("aot", key, sorted(rows, key=lambda r: (
                        str(r.get("item") or ""), str(r.get("version") or ""))))]
    res = note.get(RESULTS) or []
    if res:
        doc += [("blank", "", None),
                ("aot", RESULTS, sorted(
                    ({k: (dict(sorted(v.items())) if isinstance(v, dict) else v)
                      for k, v in r.items()} for r in res),
                    key=lambda r: (str(r.get("type") or ""),
                                   str(r.get("item") or ""),
                                   str(r.get("version") or ""))))]
    status = note.get("status") or {}
    if status:
        doc += [("blank", "", None), ("table", "status", dict(status))]
    return manifest.dumps(doc)


def note_keys() -> list[str]:
    """The `[[<key>]]` arrays a note may carry for note-home relations — one per
    registry type with `home = "note"` (`installed_on` → `installed`). Asked of
    the registry so a new post-release fact is a dict entry, not code here."""
    from . import relations
    return sorted({relations.spec(t)["note_key"]
                   for t in relations.types_with_home("note")
                   if relations.spec(t)["note_key"]})


def type_for_key(key: str) -> str:
    from . import relations
    for t in relations.types_with_home("note"):
        if relations.spec(t)["note_key"] == key:
            return t
    return ""


def parse(text: str) -> dict:
    """The inverse of `document` — `{}` for an absent or unreadable note.

    A note that does not parse is NOT an error that stops a read: it is an
    annotation, and a revision whose annotation is corrupt still has a tree that
    says what the part is. It is reported by `--rebuild --check` instead.
    """
    if not (text or "").strip():
        return {}
    try:
        t = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {"_unparsable": True}
    out = {"schema": t.get("schema", 0),
           "attributes": {n: dict(a) if isinstance(a, dict) else {"value": a}
                          for n, a in (t.get("attributes") or {}).items()},
           "tags": [str(x) for x in (t.get("tags") or [])],
           "suppress": [dict(s) for s in (t.get("suppress") or [])],
           "status": dict(t.get("status") or {}),
           RESULTS: [dict(r) for r in (t.get(RESULTS) or [])]}
    for key in note_keys():
        out[key] = [dict(r) for r in (t.get(key) or [])]
    return out


def empty() -> dict:
    out = {"schema": SCHEMA, "attributes": {}, "tags": [], "suppress": [],
           "status": {}, RESULTS: []}
    for key in note_keys():
        out[key] = []
    return out


def is_empty(note: dict) -> bool:
    return not (note.get("attributes") or note.get("tags")
                or note.get("suppress") or note.get("status")
                or note.get(RESULTS)
                or any(note.get(k) for k in note_keys()))


# -- reading ------------------------------------------------------------------

def blob_path(commit: str) -> str:
    """Notes are addressed by the sha of the object they annotate.

    Flat, not fanned out: `git notes` reads either layout, and a flat tree keeps
    the note commits' diffs readable (`2f1a…/b3c4…` tells nobody anything). git
    itself only fans out past a few hundred notes per repo, which one part will
    not reach.
    """
    return commit


def read_text(repo: Path, commit: str) -> str:
    return _git(repo, "cat-file", "blob", f"{REF}:{blob_path(commit)}",
                check=False)


def read(repo: Path, commit: str) -> dict:
    return parse(read_text(repo, commit))


def notes_at(repo: Path) -> dict[str, str]:
    """commit -> note blob sha, for everything annotated in this repo."""
    out: dict[str, str] = {}
    if not _git(repo, "rev-parse", "--verify", "--quiet", REF,
                check=False).strip():
        return out
    for line in _git(repo, "ls-tree", "-r", REF).splitlines():
        meta, _, path = line.partition("\t")
        bits = meta.split()
        if len(bits) >= 3 and path:
            out[path.replace("/", "")] = bits[2]
    return out


# -- writing ------------------------------------------------------------------

def write(repo: Path, commit: str, note: dict, *, message: str,
          actor: str = "plm", email: str = "") -> dict:
    """Put (or remove) the note on `commit` and return what happened.

    Plumbing rather than `git notes add`, for the same reason the rest of the
    write path is plumbing: a bare repo has no working tree and no index, and
    `git notes` wants both. An empty note REMOVES the entry — an annotation that
    says nothing should not exist, and leaving one behind would make
    `--rebuild --check` compare an empty document against no document.

    Returns `{"ref", "commit", "parent", "created"}`. The caller registers the
    undo (`plm/gitwrite.reset_ref`) exactly as it does for a design commit: a
    note is a commit, and two stores still need one outcome.
    """
    from . import gitwrite
    path = blob_path(commit)
    text = "" if is_empty(note) else document(note)
    with gitwrite.lock(repo):
        old = _git(repo, "rev-parse", "--verify", "--quiet", REF,
                   check=False).strip()
        idx = repo / f"notes-index-{os.getpid()}"
        env = {"GIT_INDEX_FILE": str(idx)}
        try:
            if idx.exists():
                idx.unlink()
            if old:
                _git(repo, "read-tree", old, env=env)
            if text:
                sha = _git(repo, "hash-object", "-w", "--stdin",
                           stdin=text.encode()).strip()
                _git(repo, "update-index", "--add", "--cacheinfo",
                     f"100644,{sha},{path}", env=env)
            else:
                _git(repo, "update-index", "--index-info", env=env,
                     stdin=f"000000 {'0' * 40}\t{path}\n".encode())
            tree = _git(repo, "write-tree", env=env).strip()
        finally:
            if idx.exists():
                idx.unlink()
        if old and tree == _git(repo, "rev-parse", f"{old}^{{tree}}").strip():
            return {"ref": REF, "commit": old, "parent": old, "created": False,
                    "note": "no change — the note is already what it says"}
        when = f"{int(time.time())} +0000"
        ident = {"GIT_AUTHOR_NAME": actor,
                 "GIT_AUTHOR_EMAIL": email or gitwrite.BOT_EMAIL,
                 "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_NAME": actor,
                 "GIT_COMMITTER_EMAIL": email or gitwrite.BOT_EMAIL,
                 "GIT_COMMITTER_DATE": when}
        body = (message.rstrip("\n") + "\n\n"
                + f"Plm-Note: {commit}\nPlm-Actor: {actor}\n")
        sha = _git(repo, "commit-tree", tree, *(("-p", old) if old else ()),
                   stdin=body.encode(), env=ident).strip()
        _git(repo, "update-ref", REF, sha, old or ("0" * 40))
        gitwrite.mark_written(repo, {"commit": sha, "ref": REF, "origin": "note",
                                     "actor": actor, "annotates": commit})
    return {"ref": REF, "commit": sha, "parent": old, "created": True,
            "annotates": commit}


def amend(repo: Path, commit: str, mutate, *, message: str,
          actor: str = "plm") -> tuple[dict, dict]:
    """Read the note, let `mutate(note)` change it, write it back.

    The one read-modify-write path, so no caller has to remember to merge with
    what is already there. Returns (the new note, the write result).
    """
    note = read(repo, commit)
    if note.get("_unparsable"):
        raise NoteError(
            f"the existing plm note on {commit[:10]} does not parse as TOML, so "
            "plm will not\noverwrite it — somebody wrote it by hand. Inspect it "
            f"with:\n  git notes --ref={NAME} show {commit[:10]}")
    note = {**empty(), **note}
    mutate(note)
    return note, write(repo, commit, note, message=message, actor=actor)


# -- the status half (§2.7): what a released revision IS now ------------------

def status_of(repo: Path, commit: str) -> dict:
    """`[status]` for one commit, or {}. The lifecycle answer a clone can read."""
    return (read(repo, commit) or {}).get("status") or {}


def released(repo: Path, commit: str) -> bool:
    """Is the revision at this commit RELEASED *by status*?

    The release gate (§5.1) asks this of every child, and it deliberately asks
    about STATUS rather than "is there a tag here": a PROTOTYPE tag is still a
    tag, and a parent that shipped because its child happened to be tagged would
    make "you cannot release above your least-mature child" a rule about
    bookkeeping rather than about maturity.
    """
    st = status_of(repo, commit)
    return str(st.get("maturity") or "").lower() == "released" \
        or str(st.get("state") or "").upper() == "RELEASED"
