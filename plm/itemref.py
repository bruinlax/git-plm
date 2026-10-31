"""The item manifest — `item.toml` on `refs/plm/item`.

WHY A SECOND REF. `name`, `kind`, `project`, `product`, item-level tags and
item-level relations are facts about the ITEM. `plm.toml` lives in a
per-revision tree, so before this every one of them had to be written into every
open draft — "row + a commit on every branch" — and `--rebuild` could take a
name out of the OLDEST tag and re-assert it, because a tag's tree is a record of
what the item was called in 2025, not of what it is called now.

So item facts get an item-level home: one small orphan ref with its own linear
history, one commit per item-level change. A rename is ONE commit, whatever the
item's branch topology, and the indexer reads identity from HERE — never from a
tag it happens to be re-deriving.

WHAT `plm.toml` KEEPS. `number`, `name` and `kind` stay in every revision's
manifest as a **denormalised snapshot**: a zip of a tag still has to know what it
is, and a clone that fetched one ref should not be illegible. The item ref is
the source of truth; a pushed `plm.toml` whose `name`/`kind` disagrees with it is
a LINT (corrected in the release commit), not a refusal — refusing would mean a
rename could break every open branch in the world.

WHO MAY WRITE IT. plm, and only plm. `refs/plm/*` is already refused at the push
door (`gitvalidate.check_ref_namespace`) because identity is plm's — the same
reason a tree may not rename its own number.

FETCHING IT:

    git fetch origin refs/plm/item:refs/plm/item
    git show refs/plm/item:item.toml
    git log refs/plm/item            # every rename, with who and when
"""
from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

from . import db, manifest, safegit

GIT = os.environ.get("PLM_GIT", "git")

REF = "refs/plm/item"
PATH = "item.toml"
SCHEMA = 1

#: The fields the item row owns, in the order `item.toml` writes them.
FIELDS = ("name", "kind", "project", "product")


class ItemRefError(RuntimeError):
    pass


def _git(repo: Path, *args: str, stdin: bytes | None = None,
         env: dict | None = None, check: bool = True) -> str:
    """One name over `safegit.git` — the shared timeout, capture ceiling and
    clean environment (M5)."""
    try:
        return safegit.git(repo, *args, stdin=stdin, env=env, check=check)
    except safegit.SafeError as exc:
        raise ItemRefError(str(exc)) from None


# -- the document -------------------------------------------------------------

def document(m: dict) -> str:
    head = {"schema": SCHEMA, "number": m.get("number", "")}
    for k in ("name", "kind", "project"):
        if m.get(k):
            head[k] = m[k]
    if m.get("product"):
        head["product"] = True
    if m.get("former_numbers"):
        head["former_numbers"] = list(m["former_numbers"])
    if m.get("tags"):
        head["tags"] = sorted(set(m["tags"]))
    doc: list[tuple] = [("kv", "", head)]
    if m.get("relations"):
        doc += [("blank", "", None),
                ("aot", "relations",
                 sorted(m["relations"],
                        key=lambda r: (str(r.get("type") or ""),
                                       str(r.get("item") or ""))))]
    return manifest.dumps(doc)


def parse(text: str) -> dict:
    if not (text or "").strip():
        return {}
    try:
        t = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    return {"schema": t.get("schema", 0), "number": t.get("number", ""),
            "name": t.get("name", ""), "kind": t.get("kind", ""),
            "project": t.get("project", ""),
            "product": bool(t.get("product", False)),
            "former_numbers": [str(x) for x in (t.get("former_numbers") or [])],
            "tags": [str(x) for x in (t.get("tags") or [])],
            "relations": [dict(r) for r in (t.get("relations") or [])]}


# -- reading ------------------------------------------------------------------

def read(repo: Path) -> dict:
    """The item manifest at `refs/plm/item`, or {} where the ref does not exist.

    {} is a normal answer, not a failure: the ref is generated for every item by
    `plm/split.py` and written by `create_item`, but a repo somebody hands us
    from elsewhere may not have one, and then `plm.toml`'s snapshot is all there
    is — which is exactly what it is for.
    """
    text = _git(repo, "cat-file", "blob", f"{REF}:{PATH}", check=False)
    return parse(text)


def head(repo: Path) -> str:
    return _git(repo, "rev-parse", "--verify", "--quiet", REF,
                check=False).strip()


def build(conn, item_id: int) -> dict:
    """The item manifest as the registry currently sees it."""
    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        raise ItemRefError(f"no item {item_id}")
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM tags WHERE item_id=? AND revision_id IS NULL "
        "ORDER BY tag", (item_id,))]
    # ITEM-HOME relations only (stage 10, relations.HOMES): the claims the
    # item makes across its revisions. A revision-home claim (coupon_for) lives
    # in its claimant revision's plm.toml, even when the row predates the
    # claimant (`relations.claimant` infers it) — before stage 10 such a row was
    # written here AND into every plm.toml, two homes for one claim.
    from . import relations as relmod
    rels = []
    for r in conn.execute(
            "SELECT rel.relation, t.number AS target, rel.target_version, "
            "rel.note FROM relations rel JOIN items t ON t.id=rel.target_item_id "
            "WHERE rel.item_id=? ORDER BY rel.relation, t.number, "
            "rel.target_version", (item_id,)):
        if relmod.home(r["relation"]) != "item":
            continue
        row = {"type": r["relation"], "item": r["target"]}
        if r["target_version"]:
            row["version"] = r["target_version"]
        if r["note"]:
            row["note"] = r["note"]
        rels.append(row)
    return {"schema": SCHEMA, "number": item["number"], "name": item["name"],
            "kind": item["kind"], "project": item["project"] or "",
            "product": bool(item["product"]),
            "former_numbers": [n for n in (item["former_numbers"] or "").split(",")
                               if n],
            "tags": tags, "relations": rels}


# -- writing ------------------------------------------------------------------

def write(repo: Path, doc: dict, *, message: str, actor: str = "plm") -> dict:
    """One commit on `refs/plm/item`. Its own linear history, same trailers.

    Under the repo's write lock and a compare-and-swap, exactly like a design
    commit — this ref is small, but it is identity, and two agents renaming one
    part at once must not silently produce one name.
    """
    from . import gitwrite
    text = document(doc)
    with gitwrite.lock(repo):
        old = head(repo)
        idx = repo / f"itemref-index-{os.getpid()}"
        env = {"GIT_INDEX_FILE": str(idx)}
        try:
            if idx.exists():
                idx.unlink()
            sha = _git(repo, "hash-object", "-w", "--stdin",
                       stdin=text.encode()).strip()
            _git(repo, "update-index", "--add", "--cacheinfo",
                 f"100644,{sha},{PATH}", env=env)
            tree = _git(repo, "write-tree", env=env).strip()
        finally:
            if idx.exists():
                idx.unlink()
        if old and tree == _git(repo, "rev-parse", f"{old}^{{tree}}").strip():
            return {"ref": REF, "commit": old, "parent": old, "created": False,
                    "note": "no change — the item manifest already says this"}
        when = f"{int(time.time())} +0000"
        ident = {"GIT_AUTHOR_NAME": actor,
                 "GIT_AUTHOR_EMAIL": gitwrite.BOT_EMAIL, "GIT_AUTHOR_DATE": when,
                 "GIT_COMMITTER_NAME": actor,
                 "GIT_COMMITTER_EMAIL": gitwrite.BOT_EMAIL,
                 "GIT_COMMITTER_DATE": when}
        body = (message.rstrip("\n") + "\n\n"
                + f"Plm-Item: {doc.get('number', '')}\n"
                  f"Plm-Origin: item\nPlm-Actor: {actor}\n")
        sha = _git(repo, "commit-tree", tree, *(("-p", old) if old else ()),
                   stdin=body.encode(), env=ident).strip()
        _git(repo, "update-ref", REF, sha, old or ("0" * 40))
        gitwrite.mark_written(repo, {"commit": sha, "ref": REF,
                                     "origin": "item", "actor": actor})
    return {"ref": REF, "commit": sha, "parent": old, "created": True}


# -- applying it back ---------------------------------------------------------

def apply(conn, item_id: int, doc: dict) -> dict:
    """Write the item manifest's facts onto the item row.

    Identity itself (`number`) is NOT applied: the repo's directory name is the
    number, `gitvalidate` refuses a tree that claims another, and a renumber is
    an op that moves both together. Reading a number out of a file and writing it
    into the row would make the file able to rename the item, which is the one
    thing §3 exists to prevent.

    Tags and relations are UPSERTED, never deleted — the same asymmetry
    `gitindex._index_relations` has and for the same reason: this ref became the
    home for them part-way through the item's life, so "absent here" cannot yet
    mean "removed from the item".
    """
    from . import relations as relmod, tags as tagsmod
    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    changed = {}
    sets, vals = [], []
    for k in FIELDS:
        want = doc.get(k)
        if k == "product":
            want = 1 if want else 0
        if want in (None, ""):
            continue
        if item[k] != want:
            changed[k] = want
            sets.append(f"{k}=?")
            vals.append(want)
    if sets:
        conn.execute(f"UPDATE items SET {', '.join(sets)}, updated_at=? WHERE id=?",
                     (*vals, db.now(), item_id))
    have = {r["tag"] for r in conn.execute(
        "SELECT tag FROM tags WHERE item_id=? AND revision_id IS NULL",
        (item_id,))}
    for t in doc.get("tags") or []:
        if tagsmod.normalize(t) not in have:
            tagsmod.set_tag(conn, item_id, t, actor="itemref")
    for r in doc.get("relations") or []:
        # Tolerant of a pre-stage-10 item.toml: a revision-home line here was
        # the second copy of a claim whose plm.toml line (`scope = "item"`) the
        # indexer already reads, so it is skipped rather than written twice.
        if relmod.home(str(r.get("type") or "")) != "item":
            continue
        target = db.resolve_item(conn, str(r.get("item") or ""))
        if not target:
            continue
        exists = conn.execute(
            "SELECT 1 FROM relations WHERE item_id=? AND relation=? AND "
            "target_item_id=? AND target_version=?",
            (item_id, r.get("type"), target["id"],
             str(r.get("version") or ""))).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO relations (item_id, relation, target_item_id, "
            "target_version, note, from_version, created_at, actor) "
            "VALUES (?,?,?,?,?,'',?,?)",
            (item_id, r.get("type"), target["id"], str(r.get("version") or ""),
             str(r.get("note") or ""), db.now(), "itemref"))
    return changed


# -- the one write path every item-level verb uses ----------------------------

def commit_fields(conn, item, fields: dict, *, message: str,
                  actor: str = "plm") -> dict:
    """Record an item-level change on `refs/plm/item`. ONE commit — on an item
    that has CROSSED (its repo is live); `{}` for one whose repo is still the
    sync's projection, which picks the row up (see below).

    This replaces "the row plus a commit on every open draft". The drafts still
    carry the snapshot, and they pick the new value up at their next commit —
    `plm.toml` is re-emitted from the parsed document every time plm writes one,
    and `_snapshot_fields` below is what puts the current values into it.
    """
    from . import gitwrite, manifest as mf
    number = item["number"]
    if item["kind"] in mf.CATALOG_KINDS:
        # §4: a catalog item's identity lives in `catalog/<NUMBER>.toml`, which
        # already carries name/kind/project — a second item manifest for it
        # would be a second home for the same fact.
        from . import ops
        rev = conn.execute(
            "SELECT id FROM revisions WHERE item_id=? "
            "ORDER BY vmaj DESC, vmin DESC, vpat DESC LIMIT 1",
            (item["id"],)).fetchone()
        if not rev:
            return {"note": f"{number} has no revision to write into the catalog"}
        return ops.catalog_write(conn, item, rev["id"], message=message,
                                 actor=actor)
    try:
        repo = gitwrite.repo_path(number)
    except gitwrite.WriteError:
        return {"note": f"{number} has no repository of its own"}
    # ONLY A CROSSED ITEM'S REF IS COMMITTED TO (stage 12, loose end 5 — the
    # rule item-home relations already follow, `ops._item_claim_commit`). A
    # repo `split` generated is a projection: in `write` a not-yet-crossed
    # item is re-split by the sync after every legacy write (refs/plm/item
    # included, from the very row this change just wrote). A commit here would
    # mark the repo LIVE — `gitwrite.mark_written` — and the sync skips a live
    # repo for good, so a rename / product flag / item tag froze the repo out
    # of the sync while its revisions were still legacy rows nobody else
    # projects. `{}` = the registry row is the record; the sync carries it.
    if not gitwrite.is_live(repo):
        return {}
    doc = {**build(conn, item["id"]), **fields, "number": number}
    res = write(repo, doc, message=message, actor=actor)
    if res.get("created"):
        _undo(conn, number, res)
    return {"ref": REF, "commit": res.get("commit"),
            "created": bool(res.get("created")), "backend": "git",
            **({"note": res["note"]} if res.get("note") else {})}


def _undo(conn, number: str, res: dict) -> None:
    from . import gitwrite
    hook = getattr(conn, "on_rollback", None)
    if hook is None:
        return
    hook(lambda: gitwrite.reset_ref(number, REF, back_to=res.get("parent") or "",
                                    only_if=res["commit"]))


def snapshot_fields(conn, item_id: int) -> dict:
    """The item-level keys a revision's `plm.toml` denormalises."""
    d = build(conn, item_id)
    return {"name": d["name"], "kind": d["kind"], "project": d["project"],
            "product": d["product"]}


def lint(repo: Path, doc: dict) -> list[str]:
    """Does a pushed `plm.toml` agree with the item ref? A warning, never a no.

    §3: the snapshot is allowed to be stale — a rename does not invalidate every
    open branch in the world — and plm corrects it in the release commit. Saying
    so is the difference between "plm quietly changed my file" and "plm told me
    it would".
    """
    it = read(repo)
    if not it:
        return []
    out = []
    for k in ("name", "kind"):
        have, want = str(doc.get(k) or ""), str(it.get(k) or "")
        if want and have and have != want:
            out.append(
                f"plm.toml `{k} = {have!r}`, but the item manifest "
                f"(`refs/plm/item`) says {want!r}.\n  Item-level facts live on "
                f"that ref (docs §3); the copy here is a snapshot so a zip of a "
                f"tag\n  knows what it is. plm will write {want!r} into the "
                "release commit — fetch it with\n  `git fetch origin "
                "refs/plm/item:refs/plm/item` if you want them to match now.")
    return out
