"""Shared sources — one source file, many repos.

The migration vendored each revision's generating source into its own repo, so
a parametric model that drives fourteen parts (`camper_rack_v1_as_built.scad`
in the camper rack) is fourteen copies. Cold run 1 changed one parameter in
it, updated three copies and the project folder, and nothing told it eleven
other items now carry a different "same" file. Two layers:

1. NOW, general, cheap — GIT BLOB IDENTITY. When a change modifies a file that
   other items of the project carry at the SAME PATH with the SAME pre-change
   blob, those items are affected exactly like a parent that pins a changed
   child: `changes.impact` lists them ("shares X — not in this change") with
   dispositions through the existing machinery (`bump` reads "re-vendor in
   this change"), and `draft_dispositions` drafts them. The `shared-source`
   check reports DRIFT on a commit — the same path, a different blob, across
   a project. Nothing to migrate for this to work.

2. THE PATTERN — `lib/<NUMBER>` SUBMODULES. A shared parametric source is its
   own item (kind `document`: controlled information other items reference —
   docs/SHARED-SOURCES.md says why not a new kind), and a consumer PINS it as
   a submodule at `lib/<NUMBER>`, exactly as an assembly pins a child at
   `parts/<NUMBER>` — existence and keep rules included. It is indexed here as
   a SOURCE PIN (`source_pins`), never a BOM line: no quantity, invisible to
   rolled BOMs and build plans. A change to the source item then reaches its
   consumers through ordinary where-used impact — one file, one history.

Own table, own `ensure()` (the `changes.py` pattern): created only where git
is in use, so an `off` instance's schema is master's.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

from . import db

#: What counts as a SOURCE when comparing blobs across repos: a file the
#: manifest declares as source, or one with a CAD-source suffix. (A mesh shared
#: by blob is a copy of an output, not a shared input.)
SOURCE_SUFFIXES = (".scad",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_pins (
    revision_id     INTEGER NOT NULL,   -- the CONSUMER revision
    item_id         INTEGER NOT NULL,   -- the consumer item
    path            TEXT NOT NULL,      -- lib/<NUMBER>
    source_item_id  INTEGER,            -- the shared-source item
    source_number   TEXT NOT NULL,
    commit_sha      TEXT NOT NULL,      -- the gitlink IS the pin
    version         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (revision_id, path)
);
CREATE INDEX IF NOT EXISTS source_pins_by_source ON source_pins(source_item_id);
"""
_ready = False


def ensure(conn: sqlite3.Connection) -> None:
    global _ready
    if _ready:
        return
    # statement by statement on the CALLER's connection: no executescript
    # (it would COMMIT the caller's open transaction — changes.ensure's rule)
    for stmt in _SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    _ready = True


# -- layer 2: lib/<NUMBER> source pins ------------------------------------------

def index(conn: sqlite3.Connection, rev_id: int, item_id: int,
          entries: dict) -> list[dict]:
    """Re-derive one revision's source pins from its tree's `lib/*` gitlinks.
    Called by `gitindex.index_commit`; idempotent."""
    from . import gitindex
    ensure(conn)
    conn.execute("DELETE FROM source_pins WHERE revision_id=?", (rev_id,))
    out = []
    for path, e in sorted(entries.items()):
        if e.mode != gitindex.MODE_GITLINK or not path.startswith("lib/"):
            continue
        number = path.split("/", 1)[1]
        it = db.resolve_item(conn, number)
        lab = gitindex.pin_label(number, e.sha)
        conn.execute(
            "INSERT INTO source_pins (revision_id, item_id, path, source_item_id,"
            " source_number, commit_sha, version) VALUES (?,?,?,?,?,?,?)",
            (rev_id, item_id, path, it["id"] if it else None,
             it["number"] if it else number, e.sha, lab["version"]))
        out.append({"path": path, "source": number, "commit": e.sha,
                    "version": lab["version"], "released": lab["released"]})
    return out


def for_revision(conn: sqlite3.Connection, rev_id: int) -> list[dict]:
    ensure(conn)
    return [dict(r) for r in conn.execute(
        "SELECT path, source_number, commit_sha, version FROM source_pins "
        "WHERE revision_id=? ORDER BY path", (rev_id,))]


def consumers(conn: sqlite3.Connection, source_item_id: int) -> list[dict]:
    """Where-used for a shared source: every item whose revision pins it."""
    ensure(conn)
    return [dict(r) for r in conn.execute(
        "SELECT DISTINCT i.id, i.number, i.name, i.project FROM source_pins s "
        "JOIN items i ON i.id=s.item_id WHERE s.source_item_id=? "
        "ORDER BY i.number", (source_item_id,))]


# -- layer 1: blob identity across a project -------------------------------------

def _repo(number: str) -> Path | None:
    from . import gitwrite
    try:
        r = gitwrite.repo_path(number)
    except Exception:                                      # noqa: BLE001
        return None
    return r if (r / "HEAD").exists() else None


def _shipping_ref(repo: Path) -> str:
    """What ships now: `main`, else the default ref (an item never released)."""
    from . import gitvalidate, gitwrite
    return ("refs/heads/main" if gitwrite.tip(repo, "refs/heads/main")
            else gitvalidate.default_ref(repo))


def blobs_at(repo: Path, ref: str, paths: list[str]) -> dict[str, str]:
    """path -> blob sha at `ref`, one `cat-file --batch-check` for all paths."""
    from . import safegit
    if not paths:
        return {}
    out = safegit.git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                      stdin="".join(f"{ref}:{p}\n" for p in paths).encode(),
                      check=False)
    res = {}
    for p, line in zip(paths, (out or "").splitlines()):
        bits = line.split()
        if len(bits) == 2 and bits[1] == "blob":
            res[p] = bits[0]
    return res


def source_paths(doc: dict, entries: dict) -> list[str]:
    """The SOURCE files of one tree: `[source] file`, `[files].source`, and any
    blob with a CAD-source suffix."""
    from . import gitindex
    want = set()
    src = (doc.get("source") or {}).get("file") or ""
    if src:
        want.add(src)
    for p in ((doc.get("files") or {}).get("source") or []):
        want.add(p)
    for p, e in entries.items():
        if e.mode != gitindex.MODE_GITLINK and Path(p).suffix.lower() in SOURCE_SUFFIXES:
            want.add(p)
    return sorted(p for p in want if p in entries
                  and entries[p].mode != gitindex.MODE_GITLINK)


_CACHE: dict = {}
_CLOCK = threading.Lock()
#: Off by default: a cached answer outlived a merge in the stage-15 proof
#: (consumers that had just moved the file into lib/ were still listed). A
#: project's scan is ~50 `cat-file --batch-check` calls, ~0.4 s cold.
CACHE_S = float(os.environ.get("PLM_SHARED_SOURCE_CACHE_S", "0"))


def project_copies(conn: sqlite3.Connection, project: str, paths: list[str],
                   *, exclude: set[int] | None = None) -> dict[str, list[dict]]:
    """path -> [{item_id, number, name, blob, ref}] for every OTHER design item
    of the project whose shipping ref carries that path (optionally cached:
    PLM_SHARED_SOURCE_CACHE_S, off by default)."""
    from . import manifest
    if not project or not paths:
        return {}
    key = (project, tuple(sorted(paths)))
    now = time.monotonic()
    with _CLOCK:
        hit = _CACHE.get(key)
    if hit and CACHE_S > 0 and now - hit[0] < CACHE_S:
        rows = hit[1]
    else:
        rows = {p: [] for p in paths}
        for it in conn.execute(
                "SELECT id, number, name, kind FROM items WHERE project=? "
                "ORDER BY number", (project,)):
            if it["kind"] in manifest.CATALOG_KINDS:
                continue
            repo = _repo(it["number"])
            if not repo:
                continue
            ref = _shipping_ref(repo)
            if not ref:
                continue
            for p, b in blobs_at(repo, ref, paths).items():
                rows[p].append({"item_id": it["id"], "number": it["number"],
                                "name": it["name"], "blob": b,
                                "ref": ref.replace("refs/heads/", "")})
        if CACHE_S > 0:
            with _CLOCK:
                _CACHE[key] = (now, rows)
    ex = exclude or set()
    return {p: [r for r in v if r["item_id"] not in ex] for p, v in rows.items()}


def changed_sources(number: str, base: str, head: str) -> list[dict]:
    """[{path, base_blob, head_blob}] — the SOURCE files a draft modified
    relative to its base (deleted ones too: a sharer still carries the old)."""
    from . import gitvalidate
    repo = _repo(number)
    if not repo or not base or not head or base == head:
        return []
    try:
        b_ent = gitvalidate.tree_entries(repo, base)
        h_ent = gitvalidate.tree_entries(repo, head)
        b_doc, _ = gitvalidate._manifest_of(repo, base)
    except Exception:                                      # noqa: BLE001
        return []
    out = []
    for p in source_paths(b_doc, b_ent):
        hb = h_ent.get(p)
        if hb is not None and hb.sha == b_ent[p].sha:
            continue
        out.append({"path": p, "base_blob": b_ent[p].sha,
                    "head_blob": hb.sha if hb else ""})
    return out


def sharers(conn: sqlite3.Connection, mem: list[dict]) -> dict[int, dict]:
    """item_id -> {shares: [...]} : the OTHER items of each member's project
    that carry a source file this change modified, at the pre-change blob.
    Members are returned too (`in_change`), with whether their head still
    carries the old copy — a member that forgot to re-vendor."""
    from . import proposals
    heads = {m["item_id"]: m for m in mem}
    out: dict[int, dict] = {}
    for m in mem:
        # stage 19: the REVIEW BASELINE (a first proposal on a never-released
        # item has no main, so `base_sha` was empty and nothing was compared)
        base = proposals.baseline(m, m["item_number"]).get("sha") or ""
        if not m.get("head_sha") or not base:
            continue
        changed = changed_sources(m["item_number"], base, m["head_sha"])
        if not changed:
            continue
        copies = project_copies(conn, m["item_project"] or "",
                                [c["path"] for c in changed],
                                exclude={m["item_id"]})
        for c in changed:
            for r in copies.get(c["path"]) or []:
                if r["blob"] != c["base_blob"]:
                    continue
                member = heads.get(r["item_id"])
                if member and not c["head_blob"]:
                    # the change REMOVED its copy (moved it into a lib/<N>
                    # item — the member that carries it IS its new home)
                    continue
                if member:
                    # a member: what does ITS head carry?
                    hb = blobs_at(_repo(member["item_number"]),
                                  member["head_sha"], [c["path"]]).get(c["path"])
                    if hb != c["base_blob"]:
                        continue                  # re-vendored in this change
                ent = out.setdefault(r["item_id"], {
                    "number": r["number"], "name": r["name"],
                    "in_change": bool(member), "shares": []})
                same = next((x for x in ent["shares"]
                             if x["path"] == c["path"]
                             and x["blob"] == c["base_blob"]), None)
                if same:          # several members changed the same copy
                    if m["item_number"] not in same["members"]:
                        same["members"].append(m["item_number"])
                        same["member"] = ", ".join(same["members"])
                    continue
                ent["shares"].append({
                    "path": c["path"], "blob": c["base_blob"],
                    "new_blob": c["head_blob"], "member": m["item_number"],
                    "members": [m["item_number"]],
                    "proposal": m["id"], "ref": r["ref"]})
    return out


def change_heads(conn: sqlite3.Connection, number: str, sha: str) -> dict:
    """{item_id: {number, head_sha, change}} for the OTHER members of the open
    change (draft or in review) whose member `number` has `sha` as its head —
    {} when this commit is not a change member's head.

    Stage 16: a change that re-vendors one source
    in three repos is ONE edit. Comparing each member against the others'
    `main` read "DRIFT — differs from 2 other copies" on every member, the bot
    explained it away as "clears when you approve", and a warning that is
    expected to be ignored trains everybody to ignore warnings. Within a change
    the comparison is against the change's own heads for member items."""
    from . import changes
    try:
        changes.ensure(conn)
        row = conn.execute(
            "SELECT p.change_id FROM proposals p "
            "JOIN changes c ON c.id=p.change_id "
            "JOIN items i ON i.id=p.item_id "
            "WHERE i.number=? AND p.head_sha=? "
            "AND c.state IN ('draft','open','rejected') "
            "ORDER BY p.change_id DESC LIMIT 1", (number, sha)).fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    out = {}
    for m in changes.members(conn, row["change_id"]):
        if m["item_number"] == number or not m.get("head_sha") \
                or m["state"] in ("withdrawn", "merged"):
            continue
        out[m["item_id"]] = {"number": m["item_number"],
                             "head_sha": m["head_sha"],
                             "change": changes.label(row["change_id"])}
    return out


def drift(conn: sqlite3.Connection, number: str, project: str, doc: dict,
          entries: dict, *, sha: str = "") -> dict:
    """For the `shared-source` check: each source file of this tree, and who
    else in the project carries the same path — same blob, or a different one.

    Compared on what each item SHIPS (`main`, else its newest draft) — EXCEPT
    the other members of the change this commit is a head of (`sha`): those
    are compared at their change HEAD, because that is what merges with this
    one (stage 16). Each row says which (`ref`, `in_change`)."""
    paths = source_paths(doc, entries)
    me = db.resolve_item(conn, number)
    copies = project_copies(conn, project, paths,
                            exclude={me["id"]} if me else set())
    heads = change_heads(conn, number, sha) if sha else {}
    for iid, h in heads.items():
        repo = _repo(h["number"])
        at = blobs_at(repo, h["head_sha"], paths) if repo else {}
        for p in paths:
            rows = copies.setdefault(p, [])
            old = next((r for r in rows if r["item_id"] == iid), None)
            if p in at:
                new = {"item_id": iid, "number": h["number"],
                       "name": (old or {}).get("name", ""), "blob": at[p],
                       "ref": f"{h['change']} head", "in_change": True}
                if old:
                    rows[rows.index(old)] = new
                else:
                    rows.append(new)
            elif old:
                rows.remove(old)    # the change removed its copy (→ lib/)
    out = {}
    for p in paths:
        mine = entries[p].sha
        same = [r for r in copies.get(p) or [] if r["blob"] == mine]
        diff = [r for r in copies.get(p) or [] if r["blob"] != mine]
        out[p] = {"blob": mine, "same": same, "different": diff}
    return out


# -- stage 19: dispositions on drift, and the way to one home -------------------

def change_dispositions(conn: sqlite3.Connection, number: str, sha: str
                        ) -> tuple[int, dict]:
    """(change id, {item number: {disposition, label, note, confirmed}}) for
    the open change whose member `number` has `sha` as its head — the
    dispositions a reviewer recorded on the items that carry other copies.
    (0, {}) when this commit heads no open change."""
    from . import changes
    try:
        changes.ensure(conn)
        row = conn.execute(
            "SELECT p.change_id FROM proposals p "
            "JOIN changes c ON c.id=p.change_id JOIN items i ON i.id=p.item_id "
            "WHERE i.number=? AND p.head_sha=? "
            "AND c.state IN ('draft','open','rejected') "
            "ORDER BY p.change_id DESC LIMIT 1", (number, sha)).fetchone()
        if not row:
            return 0, {}
        out = {}
        for r in conn.execute(
                "SELECT i.number, ci.disposition, ci.note, ci.confirmed_by "
                "FROM change_impacts ci JOIN items i ON i.id=ci.parent_item_id "
                "WHERE ci.change_id=? AND ci.disposition<>''",
                (row["change_id"],)):
            if r["disposition"] == "bump":
                continue              # a promise to re-vendor, not a reason
            out[r["number"]] = {
                "disposition": r["disposition"],
                "label": changes.DISPOSITIONS.get(r["disposition"],
                                                  r["disposition"]),
                "note": r["note"], "confirmed": bool(r["confirmed_by"])}
        return row["change_id"], out
    except sqlite3.Error:
        return 0, {}


def lib_home(conn: sqlite3.Connection, project: str, paths: list[str]
             ) -> dict[str, dict]:
    """path -> {number, tag} for a `document` item of the project that
    already holds that file at the same path — the home to pin, if one
    exists."""
    out: dict[str, dict] = {}
    if not project or not paths:
        return out
    from . import gitvalidate
    for it in conn.execute("SELECT number FROM items WHERE project=? AND "
                           "kind='document' ORDER BY number", (project,)):
        repo = _repo(it["number"])
        if not repo:
            continue
        ref = _shipping_ref(repo)
        have = blobs_at(repo, ref, paths) if ref else {}
        for p in have:
            if p in out:
                continue
            rel = gitvalidate.releases(repo)
            tag = ("v" + max(rel, key=gitvalidate._vkey)) if rel else ""
            out[p] = {"number": it["number"], "tag": tag,
                      "ref": ref.replace("refs/heads/", "")}
    return out


def migration_md(number: str, project: str, paths: list[str],
                 home: dict[str, dict], *, consumers: list[str],
                 change: str = "") -> str:
    """The `lib/<N>` migration, as lines to run — printed by the
    `shared-source` warning itself (stage 19, handoff finding 4: the pattern
    existed since stage 15, but nothing on the board said how to reach it)."""
    p0 = paths[0]
    h = home.get(p0)
    n = h["number"] if h else "<N>"
    tag = (h or {}).get("tag") or "v0.1.0"
    chg = change or "<CHG-n>"
    lines = ["**Give it one home** (docs/SHARED-SOURCES.md — a `document` item "
             "consumers pin at `lib/<N>`, like a child at `parts/<N>`):", "```"]
    if not h:
        from . import gitrepo
        lines += [f'create_item(name="{Path(p0).stem} shared source", '
                  f'kind="document", project="{project}")     # -> <N>',
                  f"git clone {gitrepo.CLONE_BASE.rstrip('/')}/<N>.git work/<N>"
                  f"   # add `{p0}`, [files] source, push -o pr={chg}"]
    else:
        lines.append(f"# {n} already holds `{p0}` ({tag or h['ref']})")
    lines += [f"# in EACH consumer ({', '.join(consumers[:8])}"
              + (" …" if len(consumers) > 8 else "") + "):",
              f"git submodule add ../{n}.git lib/{n}",
              f"git -C lib/{n} checkout {tag}",
              f"git rm --cached {p0} && rm {p0}",
              f"# include <{p0}>  ->  include <lib/{n}/{p0}>   "
              f"([source] file stays the consumer's own wrapper, or "
              f"lib/{n}/{p0})",
              f"git push -o pr={chg} origin HEAD:draft/<topic>", "```",
              "**Or keep the copy, with a reason** on the pull request: "
              "`set_disposition(change, item, 'historical' | "
              "'unchanged_geometry', note)` — `draft_dispositions(change, "
              "render=true)` renders each copy with that item's own selector "
              "and drafts `unchanged_geometry` when the geometry is identical."]
    return "\n".join(lines)


def selector_compare(carrier: str, ref: str, path: str, other: str,
                     other_sha: str) -> dict:
    """Does `carrier`'s OWN selected geometry change if its copy of `path` is
    replaced by `other`'s copy at `other_sha`? (stage 19, handoff finding 4c)

    Renders `carrier`'s `[source] file` with its `[source] selector` twice —
    as committed, and with the one file swapped — and compares the meshes'
    facts (volume, bbox, triangle count). `{same: True|False|None, why,
    a, b}`; None = could not say (no selector, prose, a render failed). Two
    OpenSCAD runs: called only when asked (`draft_dispositions(render=true)`).
    """
    from . import checks, gitvalidate, safegit
    repo, orepo = _repo(carrier), _repo(other)
    if not repo or not orepo:
        return {"same": None, "why": "no repo"}
    sha = safegit.git(repo, "rev-parse", "--verify", "--quiet",
                      f"{ref}^{{commit}}", check=False).strip()
    if not sha:
        return {"same": None, "why": f"{carrier}: no ref {ref}"}
    ctx = checks.Ctx(carrier, sha)
    src = ((ctx.doc.get("source") or {}).get("file") or "")
    sel = ((ctx.doc.get("source") or {}).get("selector") or "")
    if not src or src not in ctx.entries:
        return {"same": None, "why": f"{carrier} has no `[source] file` in its "
                "tree — nothing says which geometry it selects"}
    rec = checks.selector_recipe(sel)
    if rec["form"] == "prose":
        return {"same": None, "why": f"{carrier}'s selector is prose: {rec['why']}"}
    if not sel and src == path:
        return {"same": None, "why": f"{carrier} has no `[source] selector`, so "
                f"its geometry is `{path}`'s DEFAULT output — add the selector "
                "it was made with, then this can be compared"}
    try:
        theirs = gitvalidate.git(orepo, "cat-file", "blob",
                                 f"{other_sha}:{path}", binary=True)
    except Exception as exc:                                   # noqa: BLE001
        return {"same": None, "why": f"{other} has no `{path}` at "
                f"{other_sha[:10]}: {exc}"}
    try:
        a = checks._render(ctx, src, rec["dargs"], call=rec["call"])
        b = checks._render(ctx, src, rec["dargs"], call=rec["call"],
                           override={path: theirs})
    except Exception as exc:                                   # noqa: BLE001
        return {"same": None, "why": f"render failed: {str(exc)[:300]}"}

    def close(x, y, tol):
        return abs(float(x) - float(y)) <= tol * max(1.0, abs(float(x)))
    va, vb = a.get("volume_mm3") or 0, b.get("volume_mm3") or 0
    same = (close(va, vb, 1e-6)
            and all(close(p, q, 1e-6) for p, q in zip(
                (a.get("bbox_mm") or []), (b.get("bbox_mm") or [])))
            and a.get("triangles") == b.get("triangles"))
    how = rec["call"] or " ".join(rec["dargs"]) or "(no selector)"
    return {"same": same, "a": {k: a.get(k) for k in ("volume_mm3", "bbox_mm",
                                                     "triangles")},
            "b": {k: b.get(k) for k in ("volume_mm3", "bbox_mm", "triangles")},
            "selector": how,
            "why": (f"{carrier}'s `{src}` with `{how}` renders identically with "
                    f"{other}'s `{path}` ({other_sha[:10]}) — volume "
                    f"{va:.1f} mm³, {a.get('triangles')} triangles"
                    if same else
                    f"{carrier}'s `{src}` with `{how}` renders DIFFERENTLY with "
                    f"{other}'s `{path}`: volume {va:.1f} → {vb:.1f} mm³")}


# -- stage 16: who holds a file an include reaches for --------------------------

def _tree_names(repo: Path, ref: str) -> dict[str, str]:
    """path -> blob sha for every blob of `ref` (gitlinks are not descended)."""
    from . import safegit
    out = safegit.git(repo, "ls-tree", "-r", "--full-tree", ref, check=False)
    res = {}
    for line in (out or "").splitlines():
        meta, _, path = line.partition("\t")
        bits = meta.split()
        if len(bits) == 3 and bits[1] == "blob":
            res[path] = bits[2]
    return res


def holders(conn: sqlite3.Connection, names: list[str], *,
            exclude: set[str] | None = None) -> dict[str, list[dict]]:
    """basename -> every item (ANY project — a library include crosses
    projects) that holds a file of that name:

      repo items:   [{number, kind, project, ref, tag, path, blob, tree}]
                    at the item's shipping ref (`main`, else its newest draft),
                    with its newest release tag when it has one; `tree` is the
                    holder's own path->blob map, so a caller can ask whether
                    the holder's copy resolves ITS includes.
      no-repo items (purchased / hardware — catalog.git has no files): their
                    newest revision's registered file rows, `ref = ""`, so a
                    reply can say "registered on GSI-PUR-0002, which has no
                    repo to pin".

    Stage 16: run 3's source included a library
    from another project's folder; `source-present` said "outside the repo" and
    stopped, so the bot spent ten calls finding the file and then COPIED it into
    three repos. The check now names the holders; this is how."""
    from . import gitvalidate, manifest
    want = {n for n in names if n}
    out: dict[str, list[dict]] = {n: [] for n in want}
    if not want:
        return out
    ex = exclude or set()
    for it in conn.execute(
            "SELECT id, number, kind, project FROM items ORDER BY number"):
        if it["number"] in ex:
            continue
        if it["kind"] in manifest.CATALOG_KINDS:
            continue
        repo = _repo(it["number"])
        if not repo:
            continue
        ref = _shipping_ref(repo)
        if not ref:
            continue
        tree = _tree_names(repo, ref)
        hits = [(p, b) for p, b in tree.items() if Path(p).name in want]
        if not hits:
            continue
        rel = gitvalidate.releases(repo)
        tag = ""
        if rel:
            tag = "v" + max(rel, key=lambda v: tuple(
                int(x) if x.isdigit() else 0 for x in v.split(".")))
        for p, b in sorted(hits):
            out[Path(p).name].append({
                "number": it["number"], "kind": it["kind"],
                "project": it["project"] or "",
                "ref": ref.replace("refs/heads/", ""), "tag": tag,
                "path": p, "blob": b, "tree": tree})
    # items with no repo of their own (catalog kinds): the registry's file rows
    for n in sorted(want):
        for r in conn.execute(
                "SELECT i.number, i.kind, i.project, r.version, f.path "
                "FROM files f JOIN revisions r ON r.id=f.revision_id "
                "JOIN items i ON i.id=r.item_id "
                "WHERE (f.path=? OR f.path LIKE ?) AND i.kind IN ("
                + ",".join("?" * len(manifest.CATALOG_KINDS)) + ") "
                "AND r.id=(SELECT MAX(r2.id) FROM revisions r2 "
                "          WHERE r2.item_id=i.id) ORDER BY i.number",
                (n, "%/" + n, *manifest.CATALOG_KINDS)):
            if r["number"] in ex:
                continue
            out[n].append({"number": r["number"], "kind": r["kind"],
                           "project": r["project"] or "", "ref": "",
                           "tag": f"v{r['version']}", "path": r["path"],
                           "blob": "", "tree": {}})
    return out
