"""The indexer: a commit becomes the revision. And the merge: approval becomes
the release.

**Index, don't translate.** plm-git's August write path turned a push into
`revise` / `put_file` / `submit` API calls and plm re-authored the bytes into its
own layout — the DB-primary model wearing a git costume, which in this layout
would hand the pusher back different SHAs than they pushed. Here the direction
reverses: the server **keeps the commit**, and this module reads `plm.toml` and
the tree AT that commit and brings the database up to date.

It is `plm/manifest.py` run backwards. `manifest.build()` projects a revision
into a document; `index_commit()` takes the document and writes the rows. Stage
1's `--roundtrip` already proved the parse side is lossless, so the proof that
this is faithful is one line: index a pushed commit, export it with
`python -m plm.manifest --item <N>`, and diff against the pushed `plm.toml`.

WHY THIS IS THE ONLY WRITER OF DESIGN FACTS FOR A GIT-ORIGIN REVISION. If a
second code path could write `files`/`attributes`/`bom` rows for a revision that
lives in a repo, the repo and the index would disagree the first time one of
them was used and the other was not — which is exactly the failure the git-first
plan exists to remove. Everything else (`plm/gitwrite.py`, the API, the MCP)
writes a COMMIT and then calls this. `--rebuild` is the proof: drop every design
row of an item and re-derive it from the refs alone.

WHAT NEVER COMES FROM GIT. Approvals, context, events, users, sessions and
checkouts are the conversation *about* the part (§2's table). They stay in the
database, and the one that is a decision of record — an approval — is mirrored
INTO git as the annotated tag's message, so a clone is self-evidencing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from . import (attributes as attrs, context as contextmod, db, fab,
               gitvalidate, gitwrite, itemref, lfs, manifest, roles, safegit,
               tags as tagsmod)

GIT = os.environ.get("PLM_GIT", "git")
REPOS = gitwrite.REPOS

MODE_GITLINK = "160000"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

# The state a pushed draft is indexed at. Not negotiable and not taken from the
# manifest: `git push` means PROPOSED (§5.5), and a tree that could declare
# itself RELEASED would be the release gate answering to the pusher.
DRAFT_STATE = "DRAFT"


class IndexError_(RuntimeError):
    """Something in the commit cannot become registry rows. Carries plm's own
    sentence so the push output shows the real reason from the real authority."""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def _git(repo: Path, *args: str, check: bool = True, binary: bool = False,
         stdin: bytes | None = None, env: dict | None = None,
         timeout: float | None = None):
    """One name over `safegit.git` — the shared timeout, capture ceiling and
    clean environment, so this module has no subprocess of its own (M5)."""
    try:
        return safegit.git(repo, *args, check=check, binary=binary,
                           stdin=stdin, env=env, timeout=timeout)
    except safegit.SafeError as exc:
        raise IndexError_(str(exc), exc.code) from None


def repo_of(number: str) -> Path:
    """⛔ THE GATE (L2). `REPOS / f"{number}.git"` is a filesystem join with a
    caller's string in it; `safegit.safe_number` is the one regex that decides
    whether it may happen, and this was one of the four places that did it
    without asking."""
    try:
        p = safegit.repo_dir(REPOS, number)
    except safegit.SafeError as exc:
        raise IndexError_(str(exc), exc.code) from None
    if not (p / "HEAD").exists():
        raise IndexError_(f"no repository for {number} under {REPOS}", 404)
    return p


# -- pins: what version is a gitlink pointing at ------------------------------

def _tags_at(repo: Path) -> dict[str, list[str]]:
    """commit -> the release versions tagged on it."""
    out: dict[str, list[str]] = {}
    for v, sha in gitvalidate.releases(repo).items():
        out.setdefault(sha, []).append(v)
    return out


def pseudo_version(repo: Path, commit: str) -> str:
    """Go's display form for a pin that is not a release (§5.1).

    `0.3.2-0.20260917-a1b2c3d4e5f6` reads as "after 0.3.1, not a release", and
    sorts after every 0.3.1 and before 0.3.2 — which is exactly what a reviewer
    needs to know about a child their parent is pinning. Inventing a version
    number for it would be a lie; showing a bare SHA would be unreadable.
    """
    base = "0.0.0"
    rels_ = gitvalidate.releases(repo)
    anc = [(tuple(int(x) for x in v.split(".")), v) for v, sha in rels_.items()
           if safegit.ok(repo, "merge-base", "--is-ancestor", sha, commit)]
    if anc:
        a, b, c = max(anc)[0]
        base = f"{a}.{b}.{c + 1}"
    when = _git(repo, "show", "-s", "--format=%cd", "--date=format:%Y%m%d%H%M%S",
                commit).strip()
    return f"{base}-0.{when}-{commit[:12]}"


def pin_label(child_number: str, commit: str) -> dict:
    """{version, released, commit} for one gitlink.

    `released` here means "sits on a release TAG" — the §5.1 proposal rule. What
    that tag's MATURITY is, which is the stricter question a promotion asks, is
    `revision_status` below.
    """
    repo = REPOS / f"{child_number}.git"
    if not (repo / "HEAD").exists():
        return {"version": "", "released": False, "commit": commit}
    at = _tags_at(repo).get(commit) or []
    if at:
        return {"version": max(at, key=lambda v: tuple(int(x) for x in v.split("."))),
                "released": True, "commit": commit}
    return {"version": pseudo_version(repo, commit), "released": False,
            "commit": commit}


# -- what a frozen revision IS *now* (§2.7) -----------------------------------
#
# Three homes, asked in this order, and the order is the whole design:
#
#   1. `refs/notes/plm` `[status]` — a LATER decision about an immutable thing:
#      promoted to RELEASED, put on hold, made OBSOLETE. Notes are the only one
#      of the three that can change after the tag exists, so they win.
#   2. the annotated tag's `State:` / `Maturity:` lines — what was granted when
#      the revision was approved. Every tag `plm/split.py` replayed has these,
#      which is what keeps the 619 vault-era revisions answering correctly with
#      no backfill at all.
#   3. `plm.toml` in the tree — the release commit writes the granted state
#      into it, so this is the same answer as (2) for anything plm tagged.
#
# A clone can evaluate all three without asking plm, which is the property the
# release gate needs: "is my child released?" must be answerable offline.

_TAG_FIELD = re.compile(r"^(State|Maturity):\s*(.+?)\s*$", re.M)


def _tag_status(repo: Path, commit: str) -> dict:
    for v, sha in gitvalidate.releases(repo).items():
        if sha != commit:
            continue
        ann = _git(repo, "cat-file", "-p", f"refs/tags/v{v}", check=False)
        got = {k.lower(): val for k, val in _TAG_FIELD.findall(ann)}
        if got:
            return {"state": got.get("state", ""),
                    "maturity": got.get("maturity", "").replace("(none)", ""),
                    "version": v}
    return {}


def revision_status(repo: Path, commit: str) -> dict:
    """`{state, maturity, source}` for the revision at a commit."""
    from . import gitnotes
    st = gitnotes.status_of(repo, commit)
    if st.get("state") or st.get("maturity"):
        return {"state": str(st.get("state") or ""),
                "maturity": str(st.get("maturity") or ""), "source": "note"}
    tag = _tag_status(repo, commit)
    if tag:
        return {**tag, "source": "tag"}
    doc, _ = gitvalidate._manifest_of(repo, commit)
    state = str((doc or {}).get("state") or "")
    return {"state": state, "maturity": str((doc or {}).get("maturity") or ""),
            "source": "tree"}


def is_released(repo: Path, commit: str) -> bool:
    st = revision_status(repo, commit)
    return st["state"].upper() == "RELEASED" or st["maturity"].lower() == "released"


def pin_status(child_number: str, commit: str) -> dict:
    """`pin_label` plus the child's lifecycle — what a promotion has to know."""
    label = pin_label(child_number, commit)
    repo = REPOS / f"{child_number}.git"
    if not (repo / "HEAD").exists():
        return {**label, "state": "", "maturity": "", "status_source": "",
                "is_released": False}
    st = revision_status(repo, commit)
    return {**label, "state": st["state"], "maturity": st["maturity"],
            "status_source": st["source"],
            "is_released": st["state"].upper() == "RELEASED"
            or st["maturity"].lower() == "released"}


# -- reading the tree ---------------------------------------------------------

def _entries(repo: Path, commit: str) -> dict:
    return gitvalidate.tree_entries(repo, commit)


def _blob_head(repo: Path, sha: str, n: int = 512) -> bytes:
    """The first `n` bytes of a blob, WITHOUT reading the rest.

    ⚠️ This was `safegit.git(…, max_bytes=MAX_CAPTURE)[:512]` — it captured the
    whole object and then took 512 bytes of it, so the 85 MB mesh in
    KML-PRT-1011 hit the 64 MB capture ceiling and the H5 guard refused, which
    meant two RELEASED revisions of a real part could not be indexed from their
    own repo at all. Found in the stage-8 rehearsal (`--rebuild` over every
    item, refs `v0.2.0` and `v0.3.1`). The ceiling was right; reading 85 MB to
    look at 512 bytes was not. `_blob_sha256` two lines down has always streamed
    — this is the same shape.
    """
    out = bytearray()
    try:
        for chunk in safegit.stream(repo, "cat-file", "blob",
                                    safegit.safe_sha(sha), chunk=n):
            out += chunk
            if len(out) >= n:
                break
    except safegit.SafeError:
        return b""
    return bytes(out[:n])


def _blob_sha256(repo: Path, sha: str) -> str:
    h = hashlib.sha256()
    # Streamed with a ceiling: the largest blob in this registry is 85 MB and
    # hashing it must not mean holding it (H5).
    for chunk in safegit.stream(repo, "cat-file", "blob", safegit.safe_sha(sha),
                                chunk=1 << 20):
        h.update(chunk)
    return h.hexdigest()


def storage_key(number: str, commit: str, path: str) -> str:
    """The `files.key` for a git-backed row. One string that names the repo, the
    immutable commit and the path — so the bytes it points at can never change
    under it, which is what a revision's file has to promise."""
    return f"{number}@{commit}:{path}"


def parse_key(key: str) -> tuple[str, str, str]:
    """`<NUMBER>@<commit>:<path>` -> its three parts, EACH GATED.

    ⛔ THIS IS H1'S SECOND PRIMITIVE. It used to be a bare `partition("@")` /
    `partition(":")` with no validation at all, and `storage.resolve` handed its
    output straight to `REPOS / f"{number}.git"` and `git cat-file blob
    {commit}:{path}` — so a `[[files.external]]` row in a PUSHED manifest saying
    `backend = "git"`, `key = "../../etc@HEAD:passwd"` chose the repo AND the
    object. `_index_files` no longer accepts a caller's `key` at all (see
    there), and this is the second wall: a number that is not a number, a commit
    that is not an object name and a path that climbs are all refused here.
    """
    number, _, rest = key.partition("@")
    commit, _, path = rest.partition(":")
    if not (number and commit and path):
        raise ValueError(f"not a git storage key: {key!r}")
    try:
        return (safegit.safe_number(number), safegit.safe_sha(commit),
                safegit.safe_path(path))
    except safegit.SafeError as exc:
        raise ValueError(f"not a git storage key: {key!r} — {exc}") from None


# -- the index write ----------------------------------------------------------

def _clear_design_rows(conn, rev_id: int, item_id: int, *,
                       keep_addressable: bool = False) -> None:
    """Everything this module owns for one revision, removed before it is
    re-derived. Idempotency is not a nicety here: re-pushing a draft branch is
    the normal way to iterate, and an indexer that appended would double the BOM
    on the second push.

    `keep_addressable` leaves the `files` and `bom` rows in place for
    `_index_files` / `_index_bom` to RECONCILE instead (stage 3c — the design notes
    stage 3b known bug 2). Those two tables are the ones the outside world holds
    ids of: `PATCH /api/bom/{id}`, `DELETE /api/files/{id}`, the UI's file menu,
    an agent that read a BOM a moment ago. Delete-and-reinsert handed every one
    of them a 404 after the next unrelated edit, which is a data model leaking
    its rebuild strategy. Everything else here — attributes, routing,
    revision-scoped tags — is addressed by NAME, so re-deriving it costs nobody
    a handle.
    """
    if not keep_addressable:
        conn.execute("DELETE FROM files WHERE revision_id=?", (rev_id,))
        conn.execute("DELETE FROM bom WHERE parent_rev_id=?", (rev_id,))
    conn.execute("DELETE FROM attributes WHERE revision_id=?", (rev_id,))
    conn.execute("DELETE FROM operations WHERE revision_id=?", (rev_id,))
    conn.execute("DELETE FROM tags WHERE item_id=? AND revision_id=?",
                 (item_id, rev_id))
    conn.execute("UPDATE revisions SET material='', mass_g=NULL WHERE id=?",
                 (rev_id,))


# -- reconciling rather than rewriting: ids the outside world may hold --------
#
# `_upsert_by` is the whole mechanism, and it is deliberately one function: a
# second copy of "match, update, insert, delete the rest" would eventually
# disagree with this one about what identity means.

def _upsert_by(conn, table: str, scope: dict, key_cols: tuple,
               rows: list[dict]) -> list[int]:
    """Make `table` hold exactly `rows` within `scope`, keeping ids that match.

    `key_cols` is what "the same row" means — `(path,)` for a file,
    `(child_item_id, find_number)` for a BOM line. Duplicates under one key are
    matched in the order they appear on both sides, which is the only honest
    answer when a revision genuinely has two lines of the same child with no
    find number: they are interchangeable, so the ids stay with the positions.
    """
    where = " AND ".join(f"{c}=?" for c in scope)
    existing: dict[tuple, list[int]] = {}
    order: list[int] = []
    for r in conn.execute(
            f"SELECT * FROM {table} WHERE {where} ORDER BY id", tuple(scope.values())):
        existing.setdefault(tuple(r[c] for c in key_cols), []).append(r["id"])
        order.append(r["id"])
    kept: list[int] = []
    for row in rows:
        k = tuple(row.get(c) for c in key_cols)
        pool = existing.get(k) or []
        if pool:
            rid = pool.pop(0)
            sets = ", ".join(f"{c}=?" for c in row)
            conn.execute(f"UPDATE {table} SET {sets} WHERE id=?",
                         (*row.values(), rid))
        else:
            cols = {**scope, **row}
            rid = conn.execute(
                f"INSERT INTO {table} ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                tuple(cols.values())).lastrowid
        kept.append(rid)
    stale = [i for i in order if i not in set(kept)]
    for rid in stale:
        conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
    return kept


def external_location(number: str, doc: dict, ex: dict) -> tuple[str, str]:
    """`(backend, key)` for one `[[files.external]]` row — H1's fix.

    The manifest no longer chooses either one: `plm/storage.py::external_key`
    allowlists the backend and confines the key to this item's own namespace.
    The push validator asks the same function (`gitvalidate._check_files`), so a
    manifest that would be rewritten here is refused at the door instead, with
    the reason.
    """
    from . import storage
    return storage.external_key(number, str(doc.get("version") or ""),
                                ex.get("name") or "",
                                backend=ex.get("backend") or "",
                                declared=ex.get("key") or "")


def _index_files(conn, repo: Path, number: str, commit: str, rev_id: int,
                 doc: dict, entries: dict) -> int:
    # `doc` came through manifest.parse, which has already lifted `primary` and
    # `external` out of the `[files]` table — so what is left is role -> paths.
    files_tbl = doc.get("files") or {}
    rows: list[dict] = []
    for role, paths in sorted(files_tbl.items()):
        for path in ([paths] if isinstance(paths, str) else list(paths or [])):
            e = entries.get(path)
            if not e:
                continue  # declared external; handled below
            # `media` is MEASURED, never guessed from the name. 512
            # bytes is all `roles.sniff_media` needs, and reading a whole 20 MB
            # mesh to learn it is an STL would make every push quadratic.
            sniff = _blob_head(repo, e.sha)
            # stage 17: an LFS POINTER is indexed as the OBJECT it names —
            # backend `lfs`, key = sha256, the real size, media sniffed from
            # the object when the store has it. The push door refuses a
            # pointer whose object is missing; a rebuild of an older tree
            # whose object is gone still gets a row, with the size it claims.
            ptr = lfs.parse_pointer(sniff) if e.size <= lfs.POINTER_MAX else None
            if ptr:
                oid, osize = ptr
                head = b""
                if lfs.has(oid):
                    with lfs.object_path(oid).open("rb") as fh:
                        head = fh.read(512)
                c = roles.classify(Path(path).name, role, head or None, osize,
                                   "")
                rows.append({"path": path, "role": c["role"], "sha256": oid,
                             "size": osize, "backend": "lfs", "key": oid,
                             "media": c["media"],
                             "role_source": c["role_source"],
                             "media_source": c["media_source"]})
                continue
            c = roles.classify(Path(path).name, role, sniff, e.size, "")
            rows.append({"path": path, "role": c["role"],
                         "sha256": _blob_sha256(repo, e.sha), "size": e.size,
                         "backend": "git",
                         "key": storage_key(number, commit, path),
                         "media": c["media"], "role_source": c["role_source"],
                         "media_source": c["media_source"]})
    for ex in doc.get("files_external") or []:
        name = ex.get("name") or ""
        if name in entries:
            # stage 16b: the committed file SUPERSEDES an external row of the
            # same name (it used to overwrite the tree file's row with the old
            # external copy); the push door lints the stale line
            continue
        c = roles.classify(name, ex.get("role") or "", None,
                           int(ex.get("size") or 0), "")
        backend, key = external_location(number, doc, ex)
        rows.append({"path": name, "role": c["role"],
                     "sha256": ex.get("sha256") or "",
                     "size": int(ex.get("size") or 0),
                     "backend": backend, "key": key,
                     "media": c["media"], "role_source": c["role_source"],
                     "media_source": c["media_source"]})
    # A file is identified by its PATH within the revision — which is what the
    # tree says too, so the row id survives every edit that does not move it.
    _upsert_by(conn, "files", {"revision_id": rev_id}, ("path",), rows)
    return len(rows)


def _index_bom(conn, repo: Path, rev_id: int, doc: dict, entries: dict,
               number: str) -> list[dict]:
    """`[[bom]]` + the gitlinks -> `bom` rows.

    THE PIN IS THE GITLINK (§5.1), so `bom.child_version` is derived here and
    lives in the index only. `track = "release"` means the line was imprecise —
    "follow the child's releases" — and that IS the empty `child_version` the
    registry has always used, so it goes back as empty and round-trips.
    """
    from . import requirements
    links = {p: e.sha for p, e in entries.items() if e.mode == MODE_GITLINK}
    cat_pin = links.get("catalog", "")
    out, rows = [], []
    for line in (doc.get("bom") or []):
        if requirements.is_requirement(line):
            continue          # stage 19: a purchase requirement — not a row
        path = str(line.get("path") or "")
        if path.startswith("parts/"):
            child_number = path.split("/", 1)[1]
            pin = links.get(path, "")
            label = pin_label(child_number, pin) if pin else {"version": "",
                                                              "released": False}
        elif path.startswith("catalog/"):
            child_number = Path(path).stem
            label = {"version": _catalog_version(cat_pin, path), "released": True}
        else:
            raise IndexError_(
                f"`[[bom]] path = {path!r}` is neither `parts/<NUMBER>` nor "
                "`catalog/<NUMBER>.toml`.")
        child = db.resolve_item(conn, child_number)
        if not child:
            raise IndexError_(
                f"{number}'s BOM names {child_number}, which is not an item in "
                "plm. plm owns\nidentity — register it first (`create_item`), "
                "then reference it.", 404)
        # An imprecise line carries no resolved version: that is what
        # `track = "release"` means, and it is the fact the manifest keeps.
        child_version = "" if line.get("track") == "release" else label["version"]
        find = line.get("find")
        rows.append({"child_item_id": child["id"], "find_number": find,
                     "child_version": child_version,
                     "qty": float(line.get("qty") or 1),
                     "unit": line.get("unit") or "EA",
                     "alt_group": line.get("alt_group") or "",
                     "notes": line.get("notes") or "",
                     "suppressed": 1 if line.get("suppressed") else 0})
        out.append({"path": path, "child": child_number,
                    # `resolved` is what the gitlink POINTS AT; `version` is
                    # what the registry recorded. They differ on a `track =
                    # "release"` line, where the registry deliberately records
                    # nothing (§5.1: the gitlink is the pin) — and saying both
                    # is how the pusher sees that rather than guessing.
                    "resolved": label.get("version", ""),
                    "version": child_version,
                    "track": line.get("track", ""),
                    "released": label.get("released", False),
                    "commit": label.get("commit", "")})
    # A BOM line is identified by (child, find number) — the two facts that
    # say WHICH occurrence this is. Everything else about the line (qty, unit,
    # notes, suppression) is an attribute of it, so editing one keeps its id and
    # `PATCH /api/bom/{id}` survives the next unrelated push.
    ids = _upsert_by(conn, "bom", {"parent_rev_id": rev_id},
                     ("child_item_id", "find_number"), rows)
    for line, rid in zip(out, ids):
        line["id"] = rid
    return out


def _catalog_version(cat_pin: str, path: str) -> str:
    if not cat_pin:
        return ""
    cat = REPOS / "catalog.git"
    raw = _git(cat, "cat-file", "blob",
               f"{safegit.safe_sha(cat_pin)}:"
               f"{safegit.safe_path(path.split('/', 1)[1])}",
               binary=True, check=False)
    if not raw:
        return ""
    try:
        return str(manifest.parse(raw.decode(errors="replace"),
                                  catalog=True).get("version") or "")
    except Exception:  # noqa: BLE001 — a catalog TOML we cannot read is not a
        return ""      # reason to refuse an assembly; the line still resolves


def _index_relations(conn, item_id: int, rev_id: int, version: str,
                     doc: dict, repo: Path, sha: str) -> int:
    """This revision's claims, from where each one lives (stage 10).

    By the registry's `home` (relations.HOMES), never by a field in the file:

      revision  the `[[relations]]` lines of THIS tree's plm.toml.
      note      the `[[<note_key>]]` entries of THIS commit's refs/notes/plm
                note — plus, tolerantly, a pre-stage-10 tree's line for one.
      item      not here: item.toml on refs/plm/item is the home, applied by
                `itemref.apply`. A pre-stage-10 tree's snapshot copy is skipped
                when the item ref exists and UPSERTED (never deleted) when it
                does not — the old behaviour, kept for a repo with no item ref.

    TOLERANT OF OLD TREES. A line with `scope = "item"` is the pre-stage-10
    shape; it is read the way it was written (an item-level row, upserted), so
    a frozen tag that can never be rewritten still indexes to what it said.

    RECONCILED, NOT REWRITTEN. A relation is addressable — comments hang off
    its id (plm/context.py TARGETS) — so the rows this revision owns are matched
    by (type, target, target version) and updated in place; only a claim the
    tree and note no longer make is deleted. "The rows this revision owns" are
    every non-item row whose claimant (relations.claimant — the inference
    included) is this revision, so indexing a tree that declares an inferred
    claim adopts the legacy row instead of adding a second one.

    The target commit is the line's `commit`, else resolved from its version
    now (`relations.pin_commit`) — a pre-stage-10 line has no commit.
    """
    import json as _json
    from . import gitnotes, relations as relmod
    has_item_ref = bool(itemref.head(repo))
    note = gitnotes.read(repo, sha) if sha else {}
    if note.get("_unparsable"):
        note = {}
    want: list[dict] = []
    n = 0

    def target_of(r: dict):
        t = db.resolve_item(conn, str(r.get("item") or ""))
        if not t:
            raise IndexError_(
                f"`[[relations]] item = {r.get('item')!r}` is not an item in plm.",
                404)
        return t

    def entry(type_: str, r: dict) -> dict:
        t = target_of(r)
        tv = str(r.get("version") or "")
        m = r.get("measured")
        return {"relation": type_, "target_item_id": t["id"],
                "target_version": tv,
                "target_commit": str(r.get("commit") or "")
                or (relmod.pin_commit(t["number"], tv) if tv else ""),
                "note": str(r.get("note") or ""),
                "result": str(r.get("result") or ""),
                "result_note": str(r.get("result_note") or ""),
                "measured": _json.dumps(m, sort_keys=True)
                if isinstance(m, dict) and m else "",
                "_target": t["number"]}

    for r in (doc.get("relations") or []):
        type_ = str(r.get("type") or "")
        h = relmod.home(type_)
        if h == "item" or str(r.get("scope") or "") == "item":
            if h == "item" and has_item_ref:
                continue          # item.toml is its home; this is a snapshot
            t = target_of(r)
            tv = str(r.get("version") or "")
            if conn.execute(
                    "SELECT 1 FROM relations WHERE item_id=? AND relation=? AND "
                    "target_item_id=? AND target_version=?",
                    (item_id, type_, t["id"], tv)).fetchone():
                continue          # already recorded, wherever it was claimed
            conn.execute(
                """INSERT INTO relations (item_id, relation, target_item_id,
                                          target_version, note, from_version,
                                          created_at, actor)
                   VALUES (?,?,?,?,?,'',?,?)""",
                (item_id, type_, t["id"], tv, str(r.get("note") or ""),
                 db.now(), "gitindex"))
            n += 1
            continue
        want.append(entry(type_, r))
    for key in gitnotes.note_keys():
        type_ = gitnotes.type_for_key(key)
        for r in note.get(key) or []:
            e = entry(type_, r)
            if not any(w["relation"] == e["relation"]
                       and w["target_item_id"] == e["target_item_id"]
                       and w["target_version"] == e["target_version"]
                       for w in want):
                want.append(e)

    # A RESULT that arrived after the tree froze (stage 12): the note's
    # `[[results]]` lays over the claim the tree makes — after the tree, as
    # `_apply_note` does for attributes, because the note is the later word.
    # A result for a claim this tree does not make is ignored: a note records
    # how a claim came out, it never makes one.
    for (type_, number_, version_), r in relmod.result_overlay(note).items():
        for w in want:
            if (w["relation"], w["_target"], w["target_version"]) == \
                    (type_, number_, version_):
                w["result"], w["result_note"] = r["result"], r["result_note"]
                w["measured"] = (_json.dumps(r["measured"], sort_keys=True)
                                 if r["measured"] else "")

    have = [dict(r) for r in conn.execute(
        "SELECT * FROM relations WHERE item_id=? ORDER BY id", (item_id,))
        if relmod.home(r["relation"]) != "item"
        and relmod.claimant(conn, r)["revision_id"] == rev_id]
    used: set[int] = set()
    for w in want:
        match = next((h for h in have if h["id"] not in used
                      and h["relation"] == w["relation"]
                      and h["target_item_id"] == w["target_item_id"]
                      and h["target_version"] == w["target_version"]), None)
        vals = (rev_id, version, w["target_commit"], w["note"], w["result"],
                w["result_note"], w["measured"])
        if match:
            used.add(match["id"])
            conn.execute(
                "UPDATE relations SET revision_id=?, from_version=?, "
                "target_commit=?, note=?, result=?, result_note=?, measured=? "
                "WHERE id=?", (*vals, match["id"]))
        else:
            conn.execute(
                """INSERT INTO relations (item_id, relation, target_item_id,
                       target_version, created_at, actor, revision_id,
                       from_version, target_commit, note, result, result_note,
                       measured) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, w["relation"], w["target_item_id"],
                 w["target_version"], db.now(), "gitindex", *vals))
        n += 1
    for h in have:
        if h["id"] not in used:
            conn.execute("DELETE FROM relations WHERE id=?", (h["id"],))
    # A claim that carries a RESULT is evidence about its target's commit, and
    # that commit's `verified-by-coupon` row was measured before the claim
    # existed. Ask for it again — queue-only, never raises (plm/checks.py).
    from . import checks as _checks
    for w in want:
        if relmod.spec(w["relation"])["results"] and w["target_commit"]:
            _checks.enqueue(w["_target"], w["target_commit"],
                            names=["verified-by-coupon"], conn=conn)
    return n


def _index_tags(conn, item_id: int, rev_id: int, doc: dict) -> int:
    """Same shape as relations: item-level tags stay where they are, the rest
    become revision-scoped on this revision."""
    item_level = {r["tag"] for r in conn.execute(
        "SELECT tag FROM tags WHERE item_id=? AND revision_id IS NULL",
        (item_id,))}
    n = 0
    for t in (doc.get("tags") or []):
        if tagsmod.normalize(t) in item_level:
            continue
        tagsmod.set_tag(conn, item_id, t, revision_id=rev_id, actor="gitindex")
        n += 1
    return n


def _had_source_pins(conn, rev_id: int) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM source_pins WHERE revision_id=? "
                                 "LIMIT 1", (rev_id,)).fetchone())
    except Exception:                                      # noqa: BLE001
        return False                # no table: there never was one


def _authored(repo: Path, sha: str, doc: dict, subject: str,
              actor: str) -> tuple[str, str, str]:
    """(summary, author, date) of a draft plm has no row for: the newest commit
    of THIS draft (not reachable from a release tag) that a person or an agent
    pushed — plm's own commits carry a `Plm-Item:` trailer — else what plm
    wrote into the manifest (a `revise` / `create_item` draft)."""
    out = _git(repo, "log", "--first-parent", "-n", "50",
               "--format=%H%x1f%an%x1f%cd%x1f%s%x1f%b%x1e",
               "--date=format:%Y-%m-%d", sha, "--not", "--tags", check=False)
    for rec in out.split("\x1e"):
        f = rec.strip("\n").split("\x1f")
        if len(f) < 5:
            continue
        if "Plm-Item:" not in f[4]:
            return f[3], f[1], f[2]
    return (str(doc.get("summary") or "") or subject,
            str(doc.get("author") or "") or actor, str(doc.get("date") or ""))


def index_commit(conn, number: str, commit: str, *, ref: str = "",
                 actor: str = "", origin: str = "push",
                 state: str = "", old: str = "") -> dict:
    """One commit -> one revision's rows. Idempotent; re-pushing updates it.

    `old` is the ref's previous tip when the commit REPLACED one (a draft
    force-pushed, a `refs/for/main` re-proposal): whatever that left with no
    ref is kept if something still pins it (`plm/gitkeep.guard`, stage 11b).

    `state` is forced by the caller that knows the lifecycle: the push path
    passes DRAFT and nothing else (a push is a proposal, §5.5); `--rebuild`
    passes nothing and takes the manifest's own `state`, which by then is the
    state the approver granted and the release commit recorded.
    """
    repo = repo_of(number)
    item = db.resolve_item(conn, number)
    if not item:
        raise IndexError_(f"{number} is not an item in plm", 404)
    sha = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    doc = manifest.parse(
        _git(repo, "cat-file", "blob", f"{sha}:plm.toml", binary=True).decode())
    version = str(doc.get("version") or "")
    if not version:
        raise IndexError_("plm.toml at that commit carries no `version`")
    # A DRAFT BRANCH IS A DRAFT, whatever its plm.toml says (stage 15): the
    # file's lifecycle fields are plm's, and a pushed tree branched off a tag
    # still carries that tag's `state`. The push and the ops draft writers say
    # so explicitly; a REBUILD of a `draft/*` ref has to know it on its own.
    if not state and origin == "rebuild" and ref.startswith("refs/heads/"):
        state = DRAFT_STATE
    want_state = state or str(doc.get("state") or DRAFT_STATE)

    existing = conn.execute(
        "SELECT * FROM revisions WHERE item_id=? AND version=?",
        (item["id"], version)).fetchone()
    if existing and existing["state"] not in ("DRAFT", "IN_REVIEW") \
            and want_state == DRAFT_STATE:
        raise IndexError_(
            f"{number} v{version} is {existing['state']} — a released revision "
            "is a record and\ncannot be re-indexed as a draft. Bump the version "
            "in plm.toml.", 409)
    if existing and existing["state"] == "IN_REVIEW" and want_state == DRAFT_STATE \
            and origin == "rebuild":
        # A REBUILD is not a push. The refusal below exists to stop somebody
        # moving a revision that is in front of an approver right now; re-deriving
        # that same revision from the same commit moves nothing, and refusing it
        # meant `--rebuild --check` wiped an IN_REVIEW revision's rows and could
        # not put them back — then reported IDENTICAL over the hole on the next
        # run, which is the exact failure stage 3b fixed for deleted branches and
        # missed for this one. Keep the state it is in.
        want_state = "IN_REVIEW"
    elif existing and existing["state"] == "IN_REVIEW" and want_state == DRAFT_STATE:
        raise IndexError_(
            f"{number} v{version} is IN_REVIEW: it is in front of an approver "
            "right now and\nis frozen until they decide.\n"
            "\n  withdraw the submission first "
            "(POST /api/approvals/<id>/withdraw), then push again.", 409)

    subject = _git(repo, "log", "-1", "--format=%s", sha).strip()
    body = _git(repo, "log", "-1", "--format=%b", sha).strip()

    # WHO OWNS `summary` / `author` / `date` — THEY ARE PLM'S (stage 15).
    #
    # ONE HOME: the revision summary is the COMMIT SUBJECT of the pushed
    # commit; `plm.toml`'s `summary` (with `author`, `date`, `state`,
    # `maturity`) is written by plm in the RELEASE commit and ignored on a
    # draft. So:
    #   · a PUSH re-states them from the commit — the subject is what the
    #     author wrote to explain the change and what the approver reads;
    #   · any other index of a revision plm already has a row for (a commit plm
    #     made — "Set mass_g on …" is a fine commit subject and a terrible
    #     summary —, a release, a note, a rebuild) KEEPS plm's record;
    #   · a revision plm has no row for (a rebuild into an empty registry):
    #     on a draft ref, the newest commit plm did not write (no `Plm-Item:`
    #     trailer); on a tag, the manifest — plm wrote it at release.
    # That is what retired the stage-12 "summary two-homes" rebuild
    # differences: the tree's stale copy is never read where plm has the fact.
    pushed = origin == "push"
    if pushed:
        summary = subject
        author = actor or _git(repo, "log", "-1", "--format=%an", sha).strip()
        when = _git(repo, "log", "-1", "--format=%cd",
                    "--date=format:%Y-%m-%d", sha).strip()
    elif existing:
        summary = existing["summary"] or subject
        author = existing["author"] or actor
        when = existing["date"] or ""
    elif want_state in (DRAFT_STATE, "IN_REVIEW"):
        summary, author, when = _authored(repo, sha, doc, subject, actor)
    else:
        summary = str(doc.get("summary") or "") or subject
        author = str(doc.get("author") or "") or actor
        when = str(doc.get("date") or "")

    rev_id = db.upsert_revision(
        conn, item["id"], version, state=want_state, summary=summary,
        author=author, date=when, git_commit=sha,
        source=(doc.get("source") or {}).get("file", ""),
        selector=(doc.get("source") or {}).get("selector", ""),
        ecn=doc.get("ecn") or "", verification=doc.get("verification") or "",
        process=doc.get("process") or "")
    # upsert_revision only writes non-empty fields (carrying a value forward is
    # the right default for a partial update), so clearing one is explicit.
    conn.execute(
        "UPDATE revisions SET state=?, summary=?, author=?, date=?, source=?, "
        "selector=?, ecn=?, verification=?, process=?, git_commit=? WHERE id=?",
        (want_state, summary, author, when,
         (doc.get("source") or {}).get("file", ""),
         (doc.get("source") or {}).get("selector", ""), doc.get("ecn") or "",
         doc.get("verification") or "", doc.get("process") or "", sha, rev_id))

    _clear_design_rows(conn, rev_id, item["id"], keep_addressable=True)
    entries = _entries(repo, sha)
    nfiles = _index_files(conn, repo, number, sha, rev_id, doc, entries)
    bom = _index_bom(conn, repo, rev_id, doc, entries, number)
    # stage 19: purchase REQUIREMENT lines (no path) — their own table, only
    # when a tree has one (or had one), like the source pins below
    from . import requirements as _req
    reqs: list = []
    if _req.lines(doc) or _req.had(conn, rev_id):
        reqs = _req.index(conn, rev_id, doc)
    # stage 15: `lib/<N>` gitlinks are SOURCE pins (plm/sources.py) — indexed,
    # never BOM lines. Only when a tree has one (or had one), so a registry
    # with no shared sources never grows the table.
    srcpins: list = []
    if any(p.startswith("lib/") for p in entries) or _had_source_pins(conn, rev_id):
        from . import sources as _sources
        srcpins = _sources.index(conn, rev_id, item["id"], entries)
    nrel = _index_relations(conn, item["id"], rev_id, version, doc, repo, sha)
    ntag = _index_tags(conn, item["id"], rev_id, doc)
    if doc.get("attributes"):
        attrs.set_many(conn, rev_id,
                       {n: dict(a) for n, a in doc["attributes"].items()},
                       actor="gitindex")
    if doc.get("routing"):
        try:
            fab.set_routing(conn, rev_id, [dict(o) for o in doc["routing"]],
                            actor="gitindex", replace=True)
        except fab.FabError as exc:
            raise IndexError_(
                f"the `[[routing]]` in plm.toml is not work plm can route: {exc}",
                400) from None
    # Item-level master data. `refs/plm/item` is its home (§3) and wins when it
    # exists; the copy in `plm.toml` is a denormalised snapshot so a zip of a tag
    # still knows what it is, and a rebuild that took it from the OLDEST tag
    # would re-assert a name the item has not had for a year.
    item_doc = itemref.read(repo)
    if item_doc:
        itemref.apply(conn, item["id"], item_doc)
    else:
        conn.execute("UPDATE items SET name=?, project=?, product=?, updated_at=? "
                     "WHERE id=?",
                     (doc.get("name") or item["name"], doc.get("project") or "",
                      1 if doc.get("product") else 0, db.now(), item["id"]))
    # …and then the facts that arrived AFTER the tree was frozen (§5). Last, on
    # purpose: a note is an overlay onto the revision the tree describes, never
    # a replacement for it.
    note = _apply_note(conn, repo, sha, rev_id, item["id"])
    db.refresh_latest(conn, item["id"])

    if body:
        contextmod.add(
            conn,
            f"**Arrived as a commit.** `{sha[:10]}` on `{ref or '(no ref)'}` "
            f"by `{author}` ({origin}).\n\n{body}",
            target_type="", target=number, version=version, actor=author,
            event=False)
    # The repo is no longer regenerable: it holds a commit `plm/split.py` did
    # not write. The marker is what makes `split --all` skip it and say so.
    #
    # ⚠️ NOT for `origin="rebuild"` (stage 9). A rebuild re-derives rows FROM
    # commits that are already there — it adds nothing to the repo, so it
    # cannot make a generated repo hold work that exists nowhere else. Marking
    # it anyway turned every repo the stage-8 verification loop touched into a
    # "live" one, which `split` (and so the `read`-mode sync) then refuses to
    # regenerate: a projection that silently stops following the registry.
    if origin != "rebuild":
        gitwrite.mark_written(repo, {"commit": sha, "ref": ref,
                                     "origin": origin, "actor": author,
                                     "version": version, "indexed": True})
    db.add_event(conn, "git.index", item_id=item["id"], revision_id=rev_id,
                 actor=author, data={"commit": sha, "ref": ref,
                                     "origin": origin, "version": version,
                                     "state": want_state, "files": nfiles,
                                     "bom": len(bom)})
    from . import checks as _checks; _checks.enqueue(number, sha, item_id=item["id"], conn=conn)  # noqa: E702  STAGE 6 HOOK: CI for physical things (plm/checks.py). Never raises, never blocks this transaction; in a relay process (the MCP) the request rides in it — stage 12.
    # PIN SITE (stage 11b) — the push door's post-receive half, and every
    # commit plm writes itself: each child a gitlink pins, each commit a claim
    # pins (tree, note, or resolved from its version), and the revision's own
    # commit stay reachable in the repo that OWNS them. Not on a rebuild: that
    # re-derives rows from commits already pinned, and `--rebuild --check
    # --dry-run` must not write a ref.
    keep_out: dict = {}
    if origin != "rebuild":
        from . import gitkeep
        pins = [(t, p, f"{k} {number}@{sha[:10]}")
                for t, p, k in gitkeep.tree_pins(repo, sha)]
        pins.append((number, sha, f"revision {number} v{version}"))
        pins += [(r[0], r[1], f"relation {number} v{version} -> {r[0]}")
                 for r in conn.execute(
                     "SELECT t.number, x.target_commit FROM relations x JOIN "
                     "items t ON t.id=x.target_item_id WHERE x.revision_id=? "
                     "AND x.target_commit!=''", (rev_id,))]
        kept = gitkeep.protect(pins, where=f"index {number}")
        if kept:
            keep_out["kept"] = kept
        if old and old != sha:
            g = gitkeep.guard(conn, number, [old],
                              why=f"{ref or 'a ref'} moved {old[:10]} -> "
                                  f"{sha[:10]}")
            if g:
                keep_out["orphans"] = g
    out = {"ok": True, "number": number, "version": version,
           "state": want_state, "revision_id": rev_id, "commit": sha, "ref": ref,
           "summary": summary,
           "summary_from": "commit" if pushed else ("registry" if existing else "derived"),
           "files": nfiles, "bom": bom, "relations": nrel, "tags": ntag,
           **({"sources": srcpins} if srcpins else {}),
           **({"requirements": reqs} if reqs else {}),
           "attributes": len(doc.get("attributes") or {}),
           "routing": len(doc.get("routing") or [])}
    if note:
        out["note"] = note
    if keep_out:
        out["keep"] = keep_out
    return out


def _apply_note(conn, repo: Path, sha: str, rev_id: int, item_id: int) -> dict:
    """Overlay `refs/notes/plm` onto the rows just derived from the tree (§5).

    Applied in this order and never the other way round: the tree is what was
    approved, the note is what was learned since. An attribute in the note wins
    over the same attribute in the manifest, because the note is by construction
    the later statement.
    """
    from . import gitnotes
    note = gitnotes.read(repo, sha)
    if not note or note.get("_unparsable"):
        return {"unparsable": True} if note else {}
    applied: dict = {}
    if note.get("attributes"):
        attrs.set_many(conn, rev_id,
                       {n: dict(a) for n, a in note["attributes"].items()},
                       actor="gitnotes")
        applied["attributes"] = sorted(note["attributes"])
    for t in note.get("tags") or []:
        tagsmod.set_tag(conn, item_id, t, revision_id=rev_id, actor="gitnotes")
    if note.get("tags"):
        applied["tags"] = list(note["tags"])
    for s in note.get("suppress") or []:
        child = db.resolve_item(conn, str(s.get("child") or ""))
        if not child:
            continue
        sql = ("UPDATE bom SET suppressed=1 WHERE parent_rev_id=? "
               "AND child_item_id=?")
        args: tuple = (rev_id, child["id"])
        if s.get("find") not in (None, ""):
            sql += " AND find_number=?"
            args += (s["find"],)
        conn.execute(sql, args)
        applied.setdefault("suppress", []).append(str(s.get("child")))
    st = note.get("status") or {}
    if st.get("state"):
        conn.execute("UPDATE revisions SET state=? WHERE id=?",
                     (str(st["state"]), rev_id))
        applied["state"] = str(st["state"])
    return applied


def withdraw_draft(conn, number: str, ref: str, commit: str = "") -> dict:
    """A deleted draft branch retires its DRAFT.

    Deleting the branch you pushed is how you say "never mind", and the registry
    should not keep a revision nobody can fetch. A RELEASED revision is a record
    and is never removed this way — the refusal says so.
    """
    item = db.resolve_item(conn, number)
    if not item:
        raise IndexError_(f"{number} is not an item in plm", 404)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM revisions WHERE item_id=? AND git_commit=?",
        (item["id"], commit))] if commit else []
    if not rows:
        out = {"ok": True, "number": number, "removed": [],
               "note": "no revision was indexed from that branch"}
        g = _guard_withdrawn(conn, number, ref, commit)
        return {**out, "keep": g} if g else out
    removed = []
    for rev in rows:
        if rev["state"] not in ("DRAFT", "IN_REVIEW"):
            raise IndexError_(
                f"{number} v{rev['version']} is {rev['state']} — deleting a "
                "branch does not\nretire a released revision. Lifecycle is a "
                "plm decision (set_state / OBSOLETE).", 409)
        pending = conn.execute(
            "SELECT id FROM approvals WHERE revision_id=? AND decision=''",
            (rev["id"],)).fetchall()
        for a in pending:
            conn.execute("UPDATE approvals SET decision='withdrawn', "
                         "approver='gitindex', decided_at=?, decision_note=? "
                         "WHERE id=?",
                         (db.now(), f"draft branch {ref} was deleted", a["id"]))
        # …and then the rows themselves go, because `approvals.revision_id` is a
        # NOT NULL foreign key and the revision is about to be deleted. Without
        # this the whole withdraw failed with `FOREIGN KEY constraint failed`
        # the moment a draft had ever been submitted — i.e. for every proposal
        # that was reviewed and rejected, which is exactly the population this
        # verb exists for. The RECORD is not lost: `events` is the append-only
        # history (submit / release.rejected / git.withdraw, with the approval
        # ids below), while `approvals` is the queue.
        decided = [dict(r) for r in conn.execute(
            "SELECT id, decision, approver FROM approvals WHERE revision_id=?",
            (rev["id"],))]
        conn.execute("DELETE FROM approvals WHERE revision_id=?", (rev["id"],))
        _clear_design_rows(conn, rev["id"], item["id"])
        conn.execute("DELETE FROM relations WHERE item_id=? AND "
                     "(from_version=? OR revision_id=?)",
                     (item["id"], rev["version"], rev["id"]))
        conn.execute("DELETE FROM context WHERE revision_id=?", (rev["id"],))
        conn.execute("DELETE FROM revisions WHERE id=?", (rev["id"],))
        removed.append(rev["version"])
        db.add_event(conn, "git.withdraw", item_id=item["id"], actor="gitindex",
                     data={"ref": ref, "commit": commit,
                           "version": rev["version"], "approvals": decided,
                           "note": (
                               f"{number} v{rev['version']} was withdrawn: the "
                               f"branch it lived on ({ref}) was deleted. Its "
                               f"{len(decided)} approval row(s) went with it — "
                               "this event and the submit/decide events before "
                               "it are the record.")})
    db.refresh_latest(conn, item["id"])
    out = {"ok": True, "number": number, "removed": removed}
    g = _guard_withdrawn(conn, number, ref, commit)
    return {**out, "keep": g} if g else out


def _guard_withdrawn(conn, number: str, ref: str, commit: str) -> dict:
    """ORPHAN SITE (stage 11b): the branch is gone (git deleted it before the
    hook ran) and the revision rows that pinned its tip were just deleted in
    this transaction. What the branch held is kept only if something ELSE
    still pins it — a parent's gitlink, a claim, an evidence file
    (`plm/gitkeep.guard`); a draft nobody pinned is let go, for gc."""
    from . import gitkeep
    return gitkeep.guard(conn, number, [commit],
                         why=f"draft branch {ref} deleted (withdrawn)")


# -- approval IS the merge ----------------------------------------------------

def release_gate(repo: Path, commit: str) -> list[dict]:
    """§5.1: you cannot release above your least-mature child.

    Returns the offending children — every `parts/*` gitlink that is not sitting
    on a release tag. At PROTOTYPE and below this is a warning; above it, the
    approval is refused, because shipping a release that depends on unreleased
    work is the one rule code forges and hardware agree on completely.
    """
    bad = []
    for path, e in sorted(_entries(repo, commit).items()):
        if e.mode != MODE_GITLINK or not path.startswith("parts/"):
            continue
        child = path.split("/", 1)[1]
        label = pin_label(child, e.sha)
        if not label["released"]:
            bad.append({"path": path, "child": child,
                        "pin": e.sha[:10], "version": label["version"]})
    return bad


def _merge_tree(repo: Path, base: str, head: str) -> tuple[str, str]:
    """`git merge-tree --write-tree` (git 2.43) — a real three-way merge with no
    working tree. Returns (tree, conflict report). Exit 1 means conflicts."""
    # `merge-tree` answers with an exit code AND output (0 = merged, 1 =
    # conflicts, >1 = it could not try), so it is the one command that wants the
    # raw result rather than `git()`'s raise-on-non-zero.
    import subprocess as _sp
    try:
        res = _sp.run([GIT, "--git-dir", str(repo), "merge-tree",
                       "--write-tree", "--name-only", base, head],
                      capture_output=True, env=safegit.clean_env(),
                      timeout=safegit.TIMEOUT_S)
    except _sp.TimeoutExpired:
        raise IndexError_(
            f"git merge-tree in {repo.name} did not finish within "
            f"{safegit.TIMEOUT_S:g}s and was killed. Nothing was merged.") from None
    out = res.stdout.decode(errors="replace")
    if res.returncode == 0:
        return out.split("\n", 1)[0].strip(), ""
    if res.returncode == 1:
        lines = out.splitlines()
        return "", "\n".join(f"    {ln}" for ln in lines[1:] if ln.strip())
    raise IndexError_(
        "git could not merge this draft: "
        + (res.stderr.decode(errors="replace") or out)[:300])


def merge_preflight(number: str, commit: str, version: str) -> dict:
    """Would `approve_commit` merge this, without writing a ref? (stage 14)

    The CHANGE's pre-flight (`plm/changes.preflight`) asks this of every member
    before the first merge, so a conflict on the third repo is found while the
    first two are still untouched. Same three cases as the merge itself, and
    the tag-exists refusal; `merge-tree --write-tree` writes tree objects only
    (no ref moves), exactly as the merge would."""
    repo = REPOS / f"{number}.git"
    if not (repo / "HEAD").exists() or not commit:
        return {"ok": True, "how": "no-git"}
    if not safegit.ok(repo, "cat-file", "-e", f"{commit}^{{commit}}"):
        return {"ok": False, "why": f"{number}: commit {commit[:10]} is not in "
                                    "its repository", "fix": "push it again"}
    if gitwrite.tip(repo, f"refs/tags/v{version}"):
        return {"ok": False,
                "why": f"{number} already has a tag v{version} — another "
                       "proposal released that version first",
                "fix": "rebase onto main, bump `version` in plm.toml to the "
                       "next free one, and re-push:\n"
                       "    git fetch origin && git rebase origin/main"}
    main_sha = gitwrite.tip(repo, "refs/heads/main")
    if not main_sha:
        return {"ok": True, "how": "first-release"}
    if safegit.ok(repo, "merge-base", "--is-ancestor", main_sha, commit):
        return {"ok": True, "how": "fast-forward"}
    tree, conflicts = _merge_tree(repo, main_sha, commit)
    if not tree:
        return {"ok": False,
                "why": f"{number} v{version} does not merge into `main` "
                       "cleanly — " + (conflicts.strip().replace("\n", "; ")
                                       or "git reported a conflict"),
                "fix": "the author rebases onto main and re-pushes the same "
                       "draft branch:\n"
                       "    git fetch origin && git rebase origin/main && "
                       "git push -f"}
    return {"ok": True, "how": "merge"}


def approve_commit(conn, *, number: str, revision: dict, target_state: str,
                   approver: str, approval_id: int, note: str = "",
                   approver_kind: str = "", repin: dict | None = None,
                   change: str = "") -> dict:
    """Move `main` onto the approved draft and write the annotated tag.

    Three things happen, in this order, under the repo's write lock:

    1. **The merge.** Fast-forward when the draft is ahead of `main`; otherwise
       a real server-side merge with `git merge-tree --write-tree`. A conflict
       is refused outright — a half-merge that plm resolved by guessing would be
       a released revision nobody reviewed.
    2. **The release commit.** `plm.toml` is re-emitted with the state and
       maturity the approver granted, so the tree at the tag says what it IS.
       The pusher's commit is untouched and stays in the history byte for byte;
       this is a child of it, not a rewrite of it.
    3. **The tag.** `v<version>`, annotated with approver, target state,
       maturity, approval id, decision note and date — §2's "forge decisions are
       mirrored into git", which is what makes a clone self-evidencing.

    Then the draft branch is deleted: it has arrived. Returns {} for a revision
    with no git origin, so approving a DB-era revision behaves exactly as it
    always did.

    ⭐ RE-PIN (stage 14, a CHANGE). `repin = {path: {sha, child, tag, from}}`
    names gitlinks that point at another member's DRAFT head; the child merged
    first, in the same decision, so its release commit and tag now exist. The
    re-pin is written into the RELEASE COMMIT (step 2) — the commit plm already
    makes on the parent's behalf — never into the pushed commit, which stays
    byte for byte what was reviewed. The commit message, a `Plm-Repin:`
    trailer per path and the tag annotation all say what moved, from where to
    where, and why. The superseded pin (the child's draft head) stays reachable
    as the parent of the child's release commit on the child's `main`, and
    under `refs/plm/proposals/<n>/head`; the new pin is a tag commit by
    construction, and is asserted to be one below.
    """
    repo = REPOS / f"{number}.git"
    commit = (revision.get("git_commit") or "").strip()
    if not (repo / "HEAD").exists() or not commit:
        return {}
    if not safegit.ok(repo, "cat-file", "-e", f"{commit}^{{commit}}"):
        # `revisions.git_commit` is a VAULT sha for everything `split` replayed.
        # That is not a git origin; there is nothing here to merge.
        return {}
    branch = gitwrite.branch_for_commit(repo, commit)
    version = revision["version"]

    # ⭐ "warn at prototype, refuse above it" has ONE definition, and it is
    # `checks.pins_must_be_released()` — the same predicate `checks.required_for`
    # uses to decide whether `pins-released` is a required check, and the same
    # one `ops.release_gate_check` asks of a promotion (stage 7).
    from . import checks as _checks
    repin = {p: v for p, v in (repin or {}).items() if v.get("sha")}
    for p, v in repin.items():
        crepo = REPOS / f"{v['child']}.git"
        at = [t for t, sha in gitvalidate.releases(crepo).items()
              if sha == v["sha"]] if (crepo / "HEAD").exists() else []
        if not at:
            raise _ops_error(
                f"{number}: cannot re-pin {p} to {v['sha'][:10]} — it is not on "
                f"a release tag of {v['child']}. Nothing was merged.", 409)
        v["tag"] = v.get("tag") or f"v{max(at, key=lambda x: tuple(int(y) for y in x.split('.')))}"
    unreleased = [u for u in release_gate(repo, commit) if u["path"] not in repin]
    if unreleased and _checks.pins_must_be_released(
            manifest.MATURITY_BY_STATE.get(target_state, "")):
        raise _ops_error(
            f"{number} v{version} cannot be released: you cannot release above "
            "your least-mature child (docs/PLM_GIT_FIRST.md §5.1). These BOM "
            "children are pinned at commits that are not releases:\n"
            + "".join(f"  · {u['path']} @ {u['pin']} ({u['version']})\n"
                      for u in unreleased)
            + "\nRelease the child first, then bump this parent's gitlink to "
              "the child's tag:\n"
              f"  git -C {unreleased[0]['path']} checkout v<x.y.z> && "
              f"git add {unreleased[0]['path']}\n"
            + "\nApproving it at PROTOTYPE is fine — a prototype may depend on "
              "prototypes.", 409)

    with gitwrite.lock(repo):
        main_ref = "refs/heads/main"
        main_sha = gitwrite.tip(repo, main_ref)
        if not main_sha:
            merged, how = commit, "first-release"
        elif safegit.ok(repo, "merge-base", "--is-ancestor", main_sha,
                        commit):
            merged, how = commit, "fast-forward"
        else:
            tree, conflicts = _merge_tree(repo, main_sha, commit)
            if not tree:
                raise _ops_error(
                    f"{number} v{version} does not merge into `main` cleanly:\n"
                    + (conflicts or "    (git reported a conflict)") + "\n\n"
                    "Nothing was merged and the approval is still pending. A "
                    "half-merge that plm\nresolved by guessing would be a "
                    "released revision nobody reviewed.\n\n"
                    "  the author rebases and re-pushes, then you approve the "
                    "result:\n"
                    "    git fetch origin && git rebase origin/main\n"
                    f"    git push -f origin HEAD:{branch[len('refs/heads/'):] if branch else 'draft/<topic>'}",
                    409)
            msg = (f"Merge {number} v{version} into main\n\n"
                   f"Approved by {approver} (approval {approval_id}).\n\n"
                   f"Plm-Item: {number}\nPlm-Version: {version}\n"
                   f"Plm-Origin: release\nPlm-Actor: {approver}\n")
            merged = _commit_tree(repo, tree, [main_sha, commit], msg, approver)
            how = "merge"

        # 2. the release commit: the tree has to say what it now IS.
        doc = gitwrite.read_manifest(repo, merged)
        maturity = manifest.MATURITY_BY_STATE.get(target_state,
                                                  target_state.lower())
        # …and the LIFECYCLE FIELDS ARE PLM'S (stage 15): the pushed tree may
        # carry its parent tag's state, maturity, author, date and summary
        # (ignored on the draft); the release commit is where plm writes the
        # truth, so the tag's plm.toml says what this revision IS and a rebuild
        # from the tag reads it back. The summary is the registry's — the
        # pushed commit's subject — never the file's stale copy.
        life = {"state": target_state, "maturity": maturity,
                "summary": str(revision.get("summary") or ""),
                "author": str(revision.get("author") or ""),
                "date": str(revision.get("date") or "")}
        # stage 16 (finding 6): the item's NAME is plm's too — its home is
        # `refs/plm/item`; the file's copy is restated, never believed
        life["name"] = gitvalidate.item_name(repo)
        life = {k: v for k, v in life.items() if v or k == "maturity"}
        # …and a README plm GENERATED (split's marker) restates this
        # revision's summary, version and state: regenerated here, so it never
        # says the previous release's (run 3's "0.35 bumps"). A README without
        # the marker is the author's and is never touched.
        readme = _released_readme(repo, merged, {**doc, **life}, number)
        if any(str(doc.get(k) or "") != v for k, v in life.items()) or repin \
                or readme:
            doc.update(life)
            tree = _write_one_file(repo, merged, "plm.toml",
                                   manifest.dumps(manifest.document(doc)).encode(),
                                   gitlinks={p: v["sha"] for p, v in repin.items()},
                                   extra={"README.md": readme} if readme else None)
            rp = ""
            if repin:
                rp = (f"Re-pinned by plm as part of {change or 'the pull request'} — "
                      "the child merged first, in the\nsame decision, so this "
                      "release pins its release tag instead of the draft\n"
                      "commit that was reviewed:\n"
                      + "".join(f"  {p}  {v['from'][:10]} (draft) -> "
                                f"{v['sha'][:10]} = {v['child']} {v['tag']}\n"
                                for p, v in sorted(repin.items())) + "\n")
            msg = (f"Release {number} v{version} as {target_state}\n\n"
                   + (note.strip() + "\n\n" if note.strip() else "")
                   + rp
                   + f"Plm-Item: {number}\nPlm-Version: {version}\n"
                     f"Plm-State: {target_state}\nPlm-Origin: release\n"
                     f"Plm-Actor: {approver}\nPlm-Approval: {approval_id}\n"
                   + (f"Plm-Change: {change}\n" if change else "")
                   + "".join(f"Plm-Repin: {p} {v['from']} -> {v['sha']} "
                             f"{v['tag']}\n" for p, v in sorted(repin.items())))
            merged = _commit_tree(repo, tree, [merged], msg, approver)

        # 3. the tag. Nothing but this function may create one.
        tag = f"v{version}"
        if gitwrite.tip(repo, f"refs/tags/{tag}"):
            raise _ops_error(
                f"{number} already has a tag {tag}. Tags are immutable: a "
                "release that could be\nmoved is not a release.", 409)
        # WHO DECIDED, IN THE TAG ITSELF. A clone has to be able to answer "did a
        # person look at this?" without asking plm, so the annotation carries the
        # KIND as well as the name: `policy` (the rule merged it, nobody looked),
        # `bot` (an agent decided, because the project's policy admits agents),
        # or `human`. Three different sentences about accountability.
        ann = [f"{number} v{version} — {target_state}", "",
               f"Maturity: {maturity or '(none)'}",
               f"State: {target_state}",
               f"Approved-By: {approver}",
               f"Approver-Kind: {approver_kind or 'human'}",
               f"Approval: {approval_id}",
               f"Date: {time.strftime('%Y-%m-%d')}"]
        if approver_kind == "policy":
            ann += ["",
                    "AUTO-MERGED BY POLICY — no human reviewed this revision.",
                    "The policy that merged it is quoted under Decision below."]
        elif approver_kind == "bot":
            ann += ["",
                    f"APPROVED BY AN AGENT ({approver}) — no human reviewed this",
                    "revision. The project's approval policy admits agents at "
                    "this maturity."]
        if note.strip():
            ann += ["", "Decision:", note.strip()]
        if revision.get("summary"):
            ann += ["", revision["summary"]]
        if repin:
            ann += ["", f"Re-pinned at merge ({change or 'change'}) — the child "
                        "merged first in the same decision:"]
            ann += [f"  {p}  {v['from'][:10]} (draft) -> {v['sha'][:10]} "
                    f"{v['child']} {v['tag']}" for p, v in sorted(repin.items())]
        if unreleased:
            ann += ["", "Unreleased children at approval time (§5.1 warning):"]
            ann += [f"  {u['path']} @ {u['pin']} ({u['version']})"
                    for u in unreleased]
        # ⭐ THE CAS FIRST, THE TAG SECOND (M7). It used to be the other way
        # round, and the failure mode was nasty in exactly the way the error
        # text denied: `_update_ref` is a real compare-and-swap and says
        # "Nothing was merged; decide again" when it loses — but the IMMUTABLE
        # tag had already been written, so the re-attempt hit the immutable-tag
        # refusal above and the item was wedged until somebody deleted a tag by
        # hand. Moving `main` first makes the losing path exactly what its
        # message promises: nothing written, decide again.
        #
        # The remaining window is the mirror image and is strictly better: the
        # CAS lands and the tag fails, leaving `main` moved with no tag. That is
        # a re-runnable state (the tag write is the next thing a re-decide
        # does), and it is visible — `git tag` does not list a release that
        # `main` claims. Both halves in one `update-ref --stdin` transaction
        # would close it entirely; `git tag -a` cannot join that transaction
        # without hand-building the tag object, which is a bigger change than
        # this review earns.
        _update_ref(repo, main_ref, merged, main_sha)
        _annotated_tag(repo, tag, merged, "\n".join(ann) + "\n", approver)
        # HEAD follows `main` the moment there IS a main. On an item's FIRST
        # release HEAD is still the draft branch this approval is about to
        # delete, and a repo whose HEAD names a dead ref clones with "remote
        # HEAD refers to nonexistent ref, unable to checkout" and an empty
        # working tree — the first thing anybody would do with a newly released
        # part. (split.py points HEAD at the newest draft only while nothing has
        # ever been released; the design notes.)
        _git(repo, "symbolic-ref", "HEAD", main_ref, check=False)
        if branch:
            _git(repo, "update-ref", "-d", branch, commit, check=False)
        gitwrite.mark_written(repo, {"commit": merged, "ref": main_ref,
                                     "origin": "release", "actor": approver,
                                     "version": version, "tag": tag})
    # PIN SITE (stage 11b): `revisions.git_commit` keeps naming the PUSHED
    # commit and the tag names the release commit. Both are on `main` by
    # construction (fast-forward, merge parent, or the release commit's
    # parent) — so this is an assertion that costs one `rev-list` each and
    # writes nothing, unless a future change to the merge breaks it.
    from . import gitkeep
    kept = gitkeep.protect(
        [(number, commit, f"approved {number} v{version} (pushed commit)"),
         (number, merged, f"released {number} v{version} ({tag})")]
        + [(v["child"], v["sha"], f"{number} {tag} re-pinned {p}")
           for p, v in repin.items()]
        + [(v["child"], v["from"], f"{number} v{version} pushed pin {p} "
                                   "(superseded by the re-pin)")
           for p, v in repin.items()],
        where=f"approve {number}")
    out_repin = []
    for p, v in sorted(repin.items()):
        pinned = gitvalidate.tree_entries(repo, merged).get(p)
        crepo = REPOS / f"{v['child']}.git"
        on = [f"v{t}" for t, sha in gitvalidate.releases(crepo).items()
              if pinned and sha == pinned.sha]
        out_repin.append({"path": p, "child": v["child"], "from": v["from"],
                          "to": pinned.sha if pinned else "", "tag": v["tag"],
                          "on_tag": on})
    return {"merged": how, "main": merged, "tag": tag, "commit": merged,
            "pushed_commit": commit, "branch_deleted": branch,
            "unreleased_children": unreleased,
            **({"repinned": out_repin} if out_repin else {}),
            **({"kept": kept} if kept else {})}


def _ops_error(msg: str, code: int = 400):
    from . import ops
    return ops.OpsError(msg, code)


def _commit_tree(repo: Path, tree: str, parents: list[str], message: str,
                 actor: str) -> str:
    args = ["commit-tree", tree]
    for p in parents:
        args += ["-p", p]
    when = f"{int(time.time())} +0000"
    env = {**os.environ, "GIT_AUTHOR_NAME": actor,
           "GIT_AUTHOR_EMAIL": gitwrite.BOT_EMAIL, "GIT_AUTHOR_DATE": when,
           "GIT_COMMITTER_NAME": actor,
           "GIT_COMMITTER_EMAIL": gitwrite.BOT_EMAIL,
           "GIT_COMMITTER_DATE": when}
    return _git(repo, *args, stdin=message.encode(), env=env).strip()


def _released_readme(repo: Path, commit: str, doc: dict, number: str) -> bytes:
    """The regenerated README for a release commit, or b"" when the tree's
    README is not plm's (no generated marker) or would not change."""
    from . import split
    cur = gitvalidate.git(repo, "cat-file", "-p", f"{commit}:README.md",
                          check=False) or ""
    if split.README_MARKER not in cur:
        return b""
    new = split.readme_for_doc(number, doc)
    return b"" if new.decode() == cur else new


def _write_one_file(repo: Path, base: str, path: str, data: bytes,
                    gitlinks: dict | None = None,
                    extra: dict | None = None) -> str:
    idx = repo / f"release-index-{os.getpid()}"
    env = {**os.environ, "GIT_INDEX_FILE": str(idx)}
    try:
        if idx.exists():
            idx.unlink()
        _git(repo, "read-tree", base, env=env)
        sha = _git(repo, "hash-object", "-w", "--stdin", stdin=data,
                   env=env).strip()
        _git(repo, "update-index", "--add", "--cacheinfo",
             f"100644,{sha},{path}", env=env)
        for xp, xdata in sorted((extra or {}).items()):
            xsha = _git(repo, "hash-object", "-w", "--stdin", stdin=xdata,
                        env=env).strip()
            _git(repo, "update-index", "--add", "--cacheinfo",
                 f"100644,{xsha},{xp}", env=env)
        for gp, gsha in sorted((gitlinks or {}).items()):
            # a gitlink is mode 160000 pointing at a commit in ANOTHER repo;
            # the index takes it as-is (no object of ours to check)
            _git(repo, "update-index", "--cacheinfo",
                 f"{MODE_GITLINK},{gsha},{gp}", env=env)
        return _git(repo, "write-tree", env=env).strip()
    finally:
        if idx.exists():
            idx.unlink()


def _annotated_tag(repo: Path, name: str, target: str, message: str,
                   tagger: str) -> None:
    when = f"{int(time.time())} +0000"
    env = {**os.environ, "GIT_COMMITTER_NAME": tagger,
           "GIT_COMMITTER_EMAIL": gitwrite.BOT_EMAIL,
           "GIT_COMMITTER_DATE": when}
    _git(repo, "-c", f"user.name={tagger}",
         "-c", f"user.email={gitwrite.BOT_EMAIL}", "tag", "-a", "-F", "-",
         "--end-of-options", name, target, stdin=message.encode(), env=env)


def _update_ref(repo: Path, ref: str, new: str, old: str) -> None:
    try:
        safegit.git(repo, "update-ref", "--end-of-options", ref, new,
                    old or ("0" * 40))
    except safegit.SafeError as exc:
        raise IndexError_(
            f"{ref} moved while this approval was being decided "
            f"({str(exc)[:200]}). Nothing was merged; decide again.") from None


# -- rebuild: the DB is derived, and here is the proof ------------------------

def indexable_refs(repo: Path) -> list[tuple[str, str]]:
    """Every ref of this repo that stands for a revision: release tags and
    draft branches. `main` is not one — it is whichever tag is newest."""
    out = [(f"refs/tags/v{v}", sha) for v, sha in
           sorted(gitvalidate.releases(repo).items(),
                  key=lambda kv: tuple(int(x) for x in kv[0].split(".")))]
    out += sorted(gitvalidate.draft_branches(repo).items())
    return out


def rebuild(conn, number: str, *, only_live: bool = True) -> dict:
    """Re-derive every design row of an item from its repo's refs alone.

    This is the claim "the database is a derived index" made checkable. It only
    runs on a LIVE repo (one this plm has written to or been pushed to) unless
    forced, because a repo `split` generated is a projection of the very rows
    this would rewrite — re-indexing it would prove nothing and would flatten
    the vault-era `files.path` values the read surface still uses.
    """
    repo = repo_of(number)
    if only_live and not gitwrite.is_live(repo):
        raise IndexError_(
            f"{number}'s repo was generated from the registry and has never "
            "been written to,\nso rebuilding the registry from it would be "
            "circular. Use --force to do it anyway.", 409)
    item = db.resolve_item(conn, number)
    done = []
    for ref, sha in indexable_refs(repo):
        try:
            res = index_commit(conn, number, sha, ref=ref, origin="rebuild")
        except IndexError_ as exc:
            done.append({"ref": ref, "error": str(exc)})
            continue
        except Exception as exc:                            # noqa: BLE001
            # ⚠️ ONE BAD REF MUST NOT KILL THE BATCH. Only `IndexError_` was
            # caught here, so a refusal raised further down — `storage`'s H1 key
            # confinement, say — aborted `--rebuild` for the whole item and, in
            # a loop over the registry, for every item after it. Found in the
            # stage-8 rehearsal: ONE revision (CCR-ASM-0011 v0.1.0) declares a
            # storage key inside ANOTHER item's namespace, and it stopped a
            # 177-item rebuild at the 30th. The refusal is still a refusal; it
            # is now recorded against the ref that caused it.
            done.append({"ref": ref, "error": f"{type(exc).__name__}: {exc}"})
            continue
        done.append({"ref": ref, "version": res["version"],
                     "state": res["state"], "files": res["files"],
                     "bom": len(res["bom"])})
    db.refresh_latest(conn, item["id"])
    return {"ok": True, "number": number, "refs": done}


def design_rows(conn, number: str) -> dict:
    """The design facts of an item, as comparable plain data. Used by
    `--rebuild --check` to show that wiping and re-deriving is a no-op."""
    from . import relations as relmod
    item = db.resolve_item(conn, number)
    out: dict = {}
    homes = relmod.home_facts(conn, item["id"])
    for rev in conn.execute(
            "SELECT * FROM revisions WHERE item_id=? ORDER BY vmaj,vmin,vpat",
            (item["id"],)):
        out[rev["version"]] = {
            **manifest.db_facts(conn, rev["id"]),
            # stage 10: the note-home relations of this revision (refs/notes/plm)
            "note_relations": homes["note"].get(rev["version"], [])}
    # …and the item-home ones (item.toml on refs/plm/item), compared once.
    out["@item"] = {"item_relations": homes["item"]}
    return out


def _keep(v: str, have: set) -> bool:
    """A `design_rows` key the repo can re-derive: a version with a ref, or the
    item-level block (stage 10), which `refs/plm/item` re-derives."""
    return v in have or v == "@item"


def ref_versions(repo: Path) -> set[str]:
    """The versions this repo can re-derive: one per indexable ref."""
    out = set()
    for ref, sha in indexable_refs(repo):
        if ref.startswith("refs/tags/v"):
            out.add(ref[len("refs/tags/v"):])
            continue
        doc, _ = gitvalidate._manifest_of(repo, sha)
        v = str((doc or {}).get("version") or "")
        if v:
            out.add(v)
    return out


def wipe_design_rows(conn, number: str, only: set[str] | None = None) -> int:
    """Clear the design rows of the revisions `--rebuild` can put back.

    `only` is that set, and passing it is not optional in practice: a revision
    whose ref is GONE (a draft branch somebody deleted while the row survived,
    a revision `split` could not represent) has nothing to be re-derived FROM,
    so wiping it is not a test of anything — it is data loss with a PASS
    printed underneath. Found the hard way on GSI-ASM-0001, whose two rejected
    demo proposals lost their rows to a `--check` that then reported IDENTICAL
    on the second run.
    """
    from . import relations as relmod
    item = db.resolve_item(conn, number)
    n = 0
    wiped: set[int] = set()
    for rev in conn.execute("SELECT id, version FROM revisions WHERE item_id=?",
                            (item["id"],)):
        if only is not None and rev["version"] not in only:
            continue
        _clear_design_rows(conn, rev["id"], item["id"])
        wiped.add(rev["id"])
        n += 1
    # Relations are reconciled (not wiped) by the normal index, because their
    # ids are addressable. The CHECK wipes them anyway — a comparison of rows
    # nobody re-derived proves nothing: every claim whose home this repo holds,
    # i.e. the claims of the wiped revisions and, when the item ref exists, the
    # item-home ones.
    has_ref = bool(itemref.head(repo_of(number)))
    for r in conn.execute("SELECT * FROM relations WHERE item_id=?",
                          (item["id"],)).fetchall():
        if relmod.home(r["relation"]) == "item":
            if has_ref:
                conn.execute("DELETE FROM relations WHERE id=?", (r["id"],))
        elif relmod.claimant(conn, r)["revision_id"] in wiped:
            conn.execute("DELETE FROM relations WHERE id=?", (r["id"],))
    return n


def _dry_run_check(number: str, *, quiet: bool = False,
                    summary: dict | None = None) -> int:
    """`--rebuild --check --dry-run`: the two-pass test, rolled back.

    Pass 1 compares the registry as it is with what the repo re-derives — for
    a `read`-mode item the repo is `split --normalize-names` of the registry,
    so the ONLY legitimate difference is a file's name (§6c-1), and every other
    field difference is counted separately and must be zero. Pass 2 re-derives
    a second time from the same refs and must be IDENTICAL: the projection is a
    fixed point. Then everything is rolled back.
    """
    import re as _re
    from . import checks as _checks, gitsync
    repo = repo_of(number)
    have = ref_versions(repo)
    conn = db.connect()
    try:
        # rolled back below, so nothing for the repo follower to project and
        # nothing for the check runner to measure — and either one's thread
        # would otherwise take the writer between items (stage 16b)
        gitsync._suspended += 1
        _checks._suspended += 1
        conn.execute("BEGIN")
        before = {v: f for v, f in design_rows(conn, number).items() if _keep(v, have)}
        wipe_design_rows(conn, number, only=have)
        r1 = rebuild(conn, number, only_live=False)
        mid = {v: f for v, f in design_rows(conn, number).items() if _keep(v, have)}
        d1 = manifest._diff(before, mid)
        wipe_design_rows(conn, number, only=have)
        rebuild(conn, number, only_live=False)
        after = {v: f for v, f in design_rows(conn, number).items() if _keep(v, have)}
        d2 = manifest._diff(mid, after)
    finally:
        conn.rollback()
        conn.close()
        gitsync._suspended -= 1
        _checks._suspended -= 1
    errors = [r for r in r1["refs"] if r.get("error")]
    # a filename-only line names a files/source path; anything else is a fact.
    # `files_external` too (stage 10 found it): an external file's NAME
    # is normalised like a tree path's, while its key and sha256 stay put —
    # the regex's `\bfiles\b` never matched it, because `_` is a word character.
    name_only = _re.compile(r"\b(files|files_external|source|primary|path)\b",
                            _re.I)
    other = [line for line in d1 if not name_only.search(line)]
    ok = not d2 and not other and not errors
    if summary is not None:
        summary.update({"number": number, "refs": len(have),
                        "pass1": len(d1), "pass1_not_a_name": len(other),
                        "pass2": len(d2), "errors": errors, "ok": ok,
                        "examples": (other or d2)[:6]})
    if quiet:
        return 0 if ok else 1
    print(f"{number}: {len(have)} ref(s) re-derived, inside a transaction that "
          "was ROLLED BACK")
    print(f"  pass 1 (registry vs repo)   field differences {len(d1)}"
          f"  — of which NOT a file name: {len(other)}")
    for line in (other or d1)[:12]:
        print("    " + line)
    print(f"  pass 2 (repo vs repo)       field differences {len(d2)}")
    for line in d2[:12]:
        print("    " + line)
    if errors:
        print(f"  refs that could not be indexed: {errors}")
    print(f"  IDENTICAL            {ok}")
    print("  (the invariant: DESIGN FACTS round-trip through the repo; the "
          "lifecycle fields —\n   state, maturity, summary, author, date — are "
          "plm's: a draft's are kept from the\n   registry, a tag's are read "
          "from the plm.toml plm wrote in the release commit)")
    return 0 if ok else 1


def _dry_run_all() -> int:
    """`--rebuild --check --dry-run --all`: the two-pass test for EVERY item
    that has a repository, each in its own rolled-back transaction.

    One process instead of one per item (stage 12's rehearsal runs it over
    ~290 repos). A design item with no repo, or a repo with no indexable ref
    (a revision-less item), is counted and listed, never silently skipped —
    "every item" has to say which ones it could not mean.
    """
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT number, kind FROM items ORDER BY number")]
    design = [r["number"] for r in rows
              if r["kind"] not in manifest.CATALOG_KINDS]
    t0 = time.monotonic()
    done, failed, no_repo, no_refs = [], [], [], []
    tot = {"refs": 0, "pass1": 0, "pass1_not_a_name": 0, "pass2": 0}
    for number in design:
        repo = repo_of(number)
        if not (repo / "HEAD").exists():
            no_repo.append(number)
            continue
        if not indexable_refs(repo):
            no_refs.append(number)
            continue
        summ: dict = {}
        try:
            _dry_run_check(number, quiet=True, summary=summ)
        except Exception as exc:                            # noqa: BLE001
            summ = {"number": number, "ok": False, "refs": 0, "pass1": 0,
                    "pass1_not_a_name": 0, "pass2": 0,
                    "errors": [f"{type(exc).__name__}: {exc}"],
                    "examples": []}
        done.append(summ)
        for k in tot:
            tot[k] += summ.get(k, 0)
        if not summ["ok"]:
            failed.append(summ)
    print("plm.gitindex --rebuild --check --dry-run --all  (every transaction "
          "ROLLED BACK)")
    print("-" * 72)
    print(f"  design items                 {len(design)}")
    print(f"  items re-derived             {len(done)}")
    print(f"  refs re-derived              {tot['refs']}")
    print(f"  no repository                {len(no_repo)}"
          + (f"  ({', '.join(no_repo[:8])}{' …' if len(no_repo) > 8 else ''})"
             if no_repo else ""))
    print(f"  repo with no indexable ref   {len(no_refs)}"
          + (f"  ({', '.join(no_refs[:8])}{' …' if len(no_refs) > 8 else ''})"
             if no_refs else ""))
    print(f"  pass 1 field differences     {tot['pass1']}  — of which NOT a "
          f"file name: {tot['pass1_not_a_name']}")
    print(f"  pass 2 field differences     {tot['pass2']}")
    print(f"  refs that could not be indexed "
          f"{sum(len(s['errors']) for s in done)}")
    print(f"  seconds                      {time.monotonic() - t0:.1f}")
    for s_ in failed:
        print(f"  ✗ {s_['number']}: pass1 non-name {s_['pass1_not_a_name']}, "
              f"pass2 {s_['pass2']}, errors {len(s_['errors'])}")
        for line in s_["examples"][:4]:
            print(f"      {line}")
        for e in s_["errors"][:2]:
            print(f"      error: {str(e)[:300]}")
    print(f"  IDENTICAL                    {not failed}  "
          f"({len(done) - len(failed)} of {len(done)} items)")
    return 0 if not failed else 1


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m plm.gitindex",
        description="Index a commit into the registry, or rebuild an item's "
                    "design rows from its repo.")
    ap.add_argument("--item", help="item number")
    ap.add_argument("--commit", help="index this one commit")
    ap.add_argument("--ref", default="", help="the ref it arrived on")
    ap.add_argument("--actor", default="", help="who")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-derive design rows from the repo's refs")
    ap.add_argument("--check", action="store_true",
                    help="with --rebuild: wipe the design rows first and diff "
                         "what comes back against what was there")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even a repo that split generated")
    ap.add_argument("--catalog", action="store_true",
                    help="rebuild every catalog item from catalog.git instead "
                         "(§4); with --check, diff what comes back")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --rebuild --check: do it inside a transaction "
                         "and ROLL IT BACK — the registry is not changed. Runs "
                         "the stage-8 two-pass test: pass 1 against the "
                         "registry as it is (the migration's filename delta "
                         "is expected there), pass 2 re-derives again and must "
                         "be IDENTICAL. For a project in PLM_GIT_MODE=read, "
                         "where the registry is the record and must not move")
    ap.add_argument("--all", action="store_true",
                    help="with --rebuild --check --dry-run: every design item "
                         "that has a repository, one rolled-back transaction "
                         "each, and a summary")
    args = ap.parse_args(argv)
    if args.catalog:
        return _main_catalog(check=args.check, dry_run=args.dry_run)
    if args.all:
        if not (args.rebuild and args.check and args.dry_run):
            print("--all is only offered with --rebuild --check --dry-run: a "
                  "registry-wide rebuild that\nCOMMITS would rewrite every "
                  "file row to the repos' names — do that per item, "
                  "deliberately.", file=sys.stderr)
            return 2
        return _dry_run_all()
    if not args.item:
        ap.print_help()
        return 2
    if args.dry_run and args.rebuild and args.check:
        return _dry_run_check(args.item)
    with db.connect() as conn:
        if args.commit:
            print(json.dumps(index_commit(conn, args.item, args.commit,
                                          ref=args.ref, actor=args.actor),
                             indent=2))
            return 0
        if args.rebuild:
            have = ref_versions(repo_of(args.item))
            before = {v: f for v, f in design_rows(conn, args.item).items()
                      if _keep(v, have)} if args.check else {}
            if args.check:
                nrev = wipe_design_rows(conn, args.item, only=have)
                print(f"wiped the design rows of {nrev} revision(s) of "
                      f"{args.item} — files, attributes, BOM, routing, "
                      f"revision-scoped tags")
                skipped = sorted(set(design_rows(conn, args.item)) - have
                                 - {"@item"})
                if skipped:
                    print(f"left alone: v{', v'.join(skipped)} — no tag and no "
                          "draft branch in the repo, so there is nothing to "
                          "re-derive them from")
            res = rebuild(conn, args.item, only_live=not args.force)
            for r in res["refs"]:
                print("  " + json.dumps(r))
            if args.check:
                after = {v: f for v, f in design_rows(conn, args.item).items()
                         if _keep(v, have)}
                diffs = manifest._diff(before, after)
                print("-" * 72)
                print(f"  revisions compared   {len(before)}")
                print(f"  field differences    {len(diffs)}")
                for line in diffs[:12]:
                    print("    " + line)
                print("  IDENTICAL            " + str(not diffs))
                return 0 if not diffs else 1
            return 0
    ap.print_help()
    return 2


def _main_catalog(*, check: bool, dry_run: bool = False) -> int:
    """`--rebuild --check` for `catalog.git` (§4).

    Same claim, same proof, different repo: wipe what the catalog can put back,
    put it back, and diff. A catalog entry has no files, no BOM and no gitlinks,
    so what is compared is identity, the revision's own fields, its attributes
    and its suppliers — which is the whole of what plm knows about something you
    buy.

    `--dry-run` ROLLS THE TRANSACTION BACK, as it does for an item. It used to
    be accepted and ignored here (stage 10's known bug, stage 12 item 3): the
    `with db.connect()` block committed on the way out, so a "dry run" of the
    catalog re-inserted every catalog attribute and supplier row under new ids
    in whatever registry it was pointed at — a `read`-mode registry included.
    """
    from . import catalog as cat
    from pathlib import Path as _P
    numbers = [_P(p).stem for p in cat.entries()]
    from . import gitsync
    conn = db.connect()
    try:
        if dry_run:
            from . import checks as _checks
            with gitsync.suspended(), _checks.suspended():   # rolled back
                rc = _catalog_check(conn, cat, numbers, check=check)
        else:
            rc = _catalog_check(conn, cat, numbers, check=check)
    except BaseException:
        conn.rollback()
        conn.close()
        raise
    if dry_run:
        conn.rollback()
        print("  (--dry-run: the transaction was ROLLED BACK — the registry is "
              "unchanged)")
    else:
        conn.commit()
    conn.close()
    return rc


def _catalog_check(conn, cat, numbers: list[str], *, check: bool) -> int:
    """The wipe / re-derive / diff, on the caller's connection; the caller
    decides commit or rollback."""
    before = {n: cat.facts(conn, n) for n in numbers} if check else {}
    if check:
        for n in numbers:
            item = db.resolve_item(conn, n)
            for rev in conn.execute(
                    "SELECT id FROM revisions WHERE item_id=?",
                    (item["id"],)):
                conn.execute("DELETE FROM attributes WHERE revision_id=?",
                             (rev["id"],))
                conn.execute("UPDATE revisions SET material='', mass_g=NULL "
                             "WHERE id=?", (rev["id"],))
            conn.execute("DELETE FROM sources WHERE item_id=?", (item["id"],))
        print(f"wiped the derived rows of {len(numbers)} catalog item(s) — "
              "attributes and supplier sources")
    res = cat.rebuild(conn)
    print(f"  indexed {len(res['indexed'])} entries from catalog.git"
          + (f", {len(res['failed'])} failed" if res["failed"] else ""))
    for f in res["failed"][:8]:
        print("    " + json.dumps(f))
    if not check:
        return 0
    after = {n: cat.facts(conn, n) for n in numbers}
    diffs = manifest._diff(before, after)
    print("-" * 72)
    print(f"  catalog items compared {len(before)}")
    print(f"  field differences      {len(diffs)}")
    for line in diffs[:12]:
        print("    " + line)
    print("  IDENTICAL              " + str(not diffs))
    return 0 if not diffs else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
