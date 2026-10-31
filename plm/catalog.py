"""`catalog.git` — the things you buy, one TOML each, and how plm writes them.

§5.4 refuses repo-per-screw: a purchased or hardware item is not a design, it is
a *record of something you can order*, and giving each one a repository would
mean 105 repos whose entire content is six lines of supplier data. So they live
one `<NUMBER>.toml` per item inside a single `catalog.git`, and an assembly
pulls that in as ONE submodule at `catalog/`.

WHAT STAGE 3C ADDS. `plm/split.py` could already GENERATE that repo from the
registry; nothing could write into it from an API call, so a `purchased` item
created through the API was registry-only until the next `split` — the one
remaining place where a design fact existed outside git. Now `create_item`,
`checkin`, `set_attributes`, `rename_item` and `reclassify_item` commit here,
through the same `plm/gitwrite.py` primitive every other write uses: same
per-repo flock, same compare-and-swap, same trailers, same `plm-written` marker.

WHY THERE IS NO DRAFT BRANCH HERE. A design item's branch exists so a human can
review a change before it becomes a revision. A screw has no geometry to review;
what plm records about it is identity and where to buy it, and the record of
change that matters is `git log -- HDW-0012.toml`. So catalog writes land on
`main` directly, and the catalog never gates a release (§2b) — you can release a
part that buys an M3 screw without "releasing" the screw.

THE ONE COUPLING WORTH KNOWING. An assembly pins the catalog by gitlink, so a
BOM line naming a file that is not at the pinned commit is refused
(`gitvalidate._check_bom`). That is why `ops.add_bom_line` pins the catalog TIP
for a purchased child: the commit this module just wrote is the one that
contains the file.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import db, manifest, safegit

REPO = "catalog"
REF = "refs/heads/main"


def path_for(number: str) -> str:
    return f"{number}.toml"


def repo_path():
    from . import gitwrite
    p = gitwrite.REPOS / f"{REPO}.git"
    return p if (p / "HEAD").exists() else None


def tip() -> str:
    from . import gitwrite
    repo = repo_path()
    if not repo:
        return ""
    return gitwrite.tip(repo, REF) or gitwrite.tip(repo, "HEAD")


def entries(commit: str = "") -> list[str]:
    """The `<NUMBER>.toml` files present at a commit (default: the tip)."""
    repo = repo_path()
    sha = commit or tip()
    if not (repo and sha):
        return []
    out = safegit.git(repo, "ls-tree", "-r", "--name-only",
                      safegit.safe_ref(sha), check=False)
    return sorted(p for p in out.splitlines() if p.endswith(".toml"))


def read(number: str, commit: str = "") -> dict:
    """The parsed catalog entry, or {}."""
    repo = repo_path()
    sha = commit or tip()
    if not (repo and sha):
        return {}
    try:
        raw = safegit.git(repo, "cat-file", "blob",
                          f"{safegit.safe_ref(sha)}:"
                          f"{safegit.safe_path(path_for(number))}",
                          binary=True, check=False)
    except safegit.SafeError:
        return {}
    if not raw:
        return {}
    try:
        return manifest.parse(raw.decode(errors="replace"), catalog=True)
    except Exception:  # noqa: BLE001 — an unreadable entry is not a crash
        return {}


# -- the write ----------------------------------------------------------------

def render(conn, rev_id: int) -> str:
    """The catalog TOML for a revision, from the registry. `[[source]]` is the
    SUPPLIERS here and the rare generating file is `[generator]` — the
    vocabulary split `plm/manifest.document` resolves by file type."""
    return manifest.render(conn, rev_id, catalog=True)


def write_entry(conn, item, rev_id: int, *, message: str,
                actor: str = "plm") -> dict:
    """Commit one catalog item's TOML onto `catalog.git`'s `main`.

    Registers the ref move on the connection's undo journal exactly as a design
    commit does (`plm/db.py` `on_rollback`): the catalog is one repo shared by
    every purchased item, so leaving it ahead of a rolled-back registry would be
    a fact nobody asked for sitting in front of every assembly that pins the tip.
    """
    from . import gitwrite
    repo = repo_path()
    if not repo:
        raise gitwrite.WriteError(
            "there is no catalog repository here, so a purchased item has "
            "nowhere to live (§5.4).\n  python -m plm.split --all creates it.")
    number = item["number"]
    text = render(conn, rev_id)
    res = gitwrite.commit(
        REPO, REF, changes={path_for(number): text.encode()},
        actor=actor or "plm", origin="catalog", message=message,
        validate=False)          # catalog entries are not revisions of a repo
    if res.get("created"):
        hook = getattr(conn, "on_rollback", None)
        if hook is not None:
            hook(lambda: gitwrite.reset_ref(
                REPO, REF, back_to=res.get("parent") or "",
                only_if=res["commit"]))
    return {"repo": f"{REPO}.git", "ref": REF, "commit": res.get("commit"),
            "path": path_for(number), "backend": "catalog",
            "created": bool(res.get("created")),
            **({"note": res["note"]} if res.get("note") else {})}


def remove_entry(conn, number: str, *, message: str, actor: str = "plm") -> dict:
    from . import gitwrite
    repo = repo_path()
    if not repo or path_for(number) not in entries():
        return {"removed": False, "note": f"{number} is not in the catalog"}
    res = gitwrite.commit(REPO, REF, changes={path_for(number): None},
                          actor=actor or "plm", origin="catalog",
                          message=message, validate=False)
    return {"removed": bool(res.get("created")), "commit": res.get("commit"),
            "repo": f"{REPO}.git", "ref": REF}


def rename_entry(conn, old_number: str, new_number: str, *, message: str,
                 actor: str = "plm") -> dict:
    """A renumbered catalog item's file moves with its number — one commit.

    The file name IS the item number here, the same way a repo's directory name
    is for a design item, so a renumber that left the old name behind would give
    one part two catalogue entries.
    """
    from . import gitwrite
    repo = repo_path()
    if not repo:
        return {"renamed": False}
    old_path = path_for(old_number)
    if old_path not in entries():
        return {"renamed": False, "note": f"{old_number} was not in the catalog"}
    raw = gitwrite.read_blob(repo, tip(), old_path)
    text = raw.decode(errors="replace").replace(
        f'number = "{old_number}"', f'number = "{new_number}"', 1)
    res = gitwrite.commit(
        REPO, REF, changes={old_path: None,
                            path_for(new_number): text.encode()},
        actor=actor or "plm", origin="catalog", message=message, validate=False)
    return {"renamed": bool(res.get("created")), "commit": res.get("commit"),
            "from": old_path, "to": path_for(new_number)}


# -- the read-back: the indexer derives catalog rows from the file ------------

def index_entry(conn, number: str, commit: str = "") -> dict:
    """`<NUMBER>.toml` -> the item's rows. `gitindex.index_commit` for a screw.

    Deliberately narrower than the design indexer, because a catalog entry is
    narrower: identity, one revision, its attributes, its suppliers. No files, no
    BOM, no gitlinks — a purchased item has none of those by definition, and a
    catalog TOML that carried them would be a design item in the wrong repo.
    """
    from . import attributes as attrs
    doc = read(number, commit)
    if not doc:
        raise KeyError(f"{number} is not in catalog.git")
    item = db.resolve_item(conn, number)
    if not item:
        raise KeyError(f"{number} is not an item in plm")
    version = str(doc.get("version") or "")
    if not version:
        raise ValueError(f"{path_for(number)} carries no `version`")
    conn.execute(
        "UPDATE items SET name=?, kind=?, project=?, product=?, updated_at=? "
        "WHERE id=?",
        (doc.get("name") or item["name"], doc.get("kind") or item["kind"],
         doc.get("project") or "", 1 if doc.get("product") else 0, db.now(),
         item["id"]))
    rev_id = db.upsert_revision(
        conn, item["id"], version, state=str(doc.get("state") or "DRAFT"),
        summary=doc.get("summary") or "", author=doc.get("author") or "",
        date=doc.get("date") or "", ecn=doc.get("ecn") or "",
        verification=doc.get("verification") or "")
    conn.execute("UPDATE revisions SET state=? WHERE id=?",
                 (str(doc.get("state") or "DRAFT"), rev_id))
    conn.execute("DELETE FROM attributes WHERE revision_id=?", (rev_id,))
    conn.execute("UPDATE revisions SET material='', mass_g=NULL WHERE id=?",
                 (rev_id,))
    if doc.get("attributes"):
        attrs.set_many(conn, rev_id,
                       {n: dict(a) for n, a in doc["attributes"].items()},
                       actor="catalog")
    conn.execute("DELETE FROM sources WHERE item_id=?", (item["id"],))
    for s in doc.get("suppliers") or []:
        conn.execute(
            "INSERT INTO sources (item_id,supplier,manufacturer,mpn,url,status,"
            "notes) VALUES (?,?,?,?,?,?,?)",
            (item["id"], s.get("supplier", ""), s.get("manufacturer", ""),
             s.get("mpn", ""), s.get("url", ""), s.get("status", ""),
             s.get("notes", "")))
    db.refresh_latest(conn, item["id"])
    return {"number": number, "version": version, "revision_id": rev_id,
            "attributes": len(doc.get("attributes") or {}),
            "suppliers": len(doc.get("suppliers") or [])}


def facts(conn, number: str) -> dict:
    """The design facts of a catalog item, comparably — `--rebuild --check`."""
    item = db.resolve_item(conn, number)
    out = {}
    for rev in conn.execute(
            "SELECT id, version FROM revisions WHERE item_id=? "
            "ORDER BY vmaj,vmin,vpat", (item["id"],)):
        out[rev["version"]] = manifest.db_facts(conn, rev["id"], catalog=True)
    return out


def history(number: str) -> list[str]:
    """Every commit that touched this item's file, OLDEST first.

    This is why the catalog has a linear history instead of one squashed
    snapshot (`split.build_catalog`): a screw's older revisions are not in the
    tip's tree — there is one file per item and it holds the newest version — so
    `git log -- HDW-0012.toml` is the only place v1.0.0 still exists once v1.1.0
    is written. A rebuild that only read the tip would silently drop every
    superseded catalog revision and then report IDENTICAL over the hole.
    """
    repo = repo_path()
    if not repo:
        return []
    out = safegit.git(repo, "log", "--format=%H", "--reverse", REF,
                      "--", safegit.safe_path(path_for(number)), check=False)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def rebuild(conn, *, only: list[str] | None = None) -> dict:
    """Re-derive every catalog item's rows from `catalog.git` alone.

    Oldest commit first, so the newest version is written last and
    `items.latest_rev_id` ends up where it belongs.
    """
    done, failed = [], []
    for path in entries():
        number = Path(path).stem
        if only and number not in only:
            continue
        for commit in history(number) or [tip()]:
            try:
                done.append(index_entry(conn, number, commit))
            except (KeyError, ValueError) as exc:
                failed.append({"number": number, "commit": commit[:10],
                               "error": str(exc)})
    return {"indexed": done, "failed": failed}
