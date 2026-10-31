"""plm.carry — move ONE PROJECT between two plm instances (stage 20).

WHY. New projects were being created on the instance where
git is `off` — a project (INK-*) is on production's legacy path, and a
Codex fit-correction landed there as three silent DRAFTS instead of a
reviewable change. Moving a project to the git-first instance, and later
moving a project from staging to production, are the same problem: items + their revisions + their forge history, ids
remapped, numbers kept. This is the one tool for it.

    python -m plm.carry export --project P --from <db> [--vault <dir>]
                               [--repos <root>] [--lfs <root>] --out <bundle>
    python -m plm.carry import --bundle <dir> --into <db> --repos <root>
                               --lfs <root> [--as-change --since YYYY-MM-DD]
                               [--mode write] --actor <who> [--dry-run]

THE BUNDLE (a directory; nothing in it is a secret):

    carry.json              what it is: project, source instance, mode per item,
                            counts and bytes, the numbers it references outside
                            the project (they must exist on the target)
    rows.json               every registry row of the project, SOURCE ids
    vault/<path>            a registry-source revision's bytes, as the source
                            vault serves them (its working tree), exec bit kept
    vault-history/<c>/<p>   bytes that are only in the vault's git history
    authors.json            vault commit -> (name, email, epoch): what split
                            uses to date and attribute a revision's commit
    lfs/objects/aa/bb/<oid> plm's LFS store's bytes, content-addressed,
                            re-hashed on import
    repos/<N>.bundle        a LIVE repo, `git bundle --all` (git-first source)
    repos/<N>.json          its HEAD, config and `plm-written` ledger

WHAT IMPORT DOES. Refuses a project that already exists on the target (any of
its numbers, its project name, or a repo directory); refuses a reference it
cannot resolve (a BOM child, relation target or source pin outside the project
that the target does not have) — except, with `--stub-external`, a reference
to an ITEM only (no revision named: the asset a design mounts_to), which is
created as `ops.create_item` makes one (its empty v0.1.0 draft, no files). Then,
in ONE transaction: rows inserted with
new ids (item NUMBERS kept — the numbering sequence advances because
`ops.next_number` reads the numbers; proposal and change ids kept when free on
the target, else renumbered and said), LFS objects stored by hash, and:

  * a live repo arrives as itself (`git fetch <bundle> refs/*:refs/*`), marked
    `plm-written` with the carry recorded in the ledger;
  * a registry-source item (the source was `off`/`read`) becomes repo history
    EXACTLY as `plm/split.py` makes it — it IS split, fed the bundle's bytes
    (`split.run(vault=…)`): released revisions are tags on `main`, drafts are
    `draft/*` branches on the revision they were revised from;
  * catalog items (purchased/hardware) are committed into the target's
    `catalog.git` (live) or built by split (generated).

`--as-change` (registry source, git-first `write` target): the source's
UNRELEASED revisions that postdate its last release (or `--since`) become ONE
change — a proposal per item on its `draft/vX.Y.Z` branch, the parent's gitlinks
already on the children's proposal heads (the re-pin the merge will move to the
release commits) — instead of N silent drafts. Everything before them,
including the printed revisions and every context note, is carried verbatim.

After the commit, every carried design item gets the rebuild check
(`gitindex --rebuild --check --dry-run`) and catalog items the catalog check;
the import exits 1 unless all are IDENTICAL.

Isolation: export only READS (DB `mode=ro`, vault reads, `git bundle`, the
object stores). Import writes only the target DB, repos root and LFS root it is
given.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from .storage import sha256_file

SCHEMA = 1

# -- the table spec -----------------------------------------------------------
#
# (table, scope, remaps). `scope` is the SQL predicate over SOURCE ids, with
# {I} = the project's item ids, {R} = their revision ids, {P} = their proposal
# ids, {C} = the changes those proposals belong to. `remaps` says what each id
# column points at. Order is insert order (foreign keys are ON in plm).
#
# Not carried, on purpose: `idempotency` (keys are per instance), `gitsync`
# (a sync log), `check_requests` (a queue), `search*` (rebuilt for the items),
# `sessions`/`users` (identity is the target's). `releases`, `release_sets`,
# `release_signoffs` carry release EVIDENCE files that live outside the DB —
# refused, loudly, until a project that has them needs carrying.

TABLES: list[tuple[str, str, dict]] = [
    ("items", "id IN ({I})", {"latest_rev_id": "defer"}),
    ("revisions", "item_id IN ({I})", {"item_id": "item"}),
    ("files", "revision_id IN ({R})", {"revision_id": "rev"}),
    ("bom", "parent_rev_id IN ({R})", {"parent_rev_id": "rev",
                                       "child_item_id": "item_ext"}),
    ("bom_requirements", "revision_id IN ({R})", {"revision_id": "rev"}),
    ("attributes", "revision_id IN ({R})", {"revision_id": "rev"}),
    ("operations", "revision_id IN ({R})", {"revision_id": "rev"}),
    ("tags", "item_id IN ({I})", {"item_id": "item", "revision_id": "rev"}),
    ("sources", "item_id IN ({I})", {"item_id": "item"}),
    ("source_pins", "revision_id IN ({R})", {"revision_id": "rev",
                                              "item_id": "item",
                                              "source_item_id": "item_ext"}),
    ("relations", "item_id IN ({I})", {"item_id": "item",
                                       "target_item_id": "item_ext",
                                       "revision_id": "rev"}),
    ("checkouts", "item_id IN ({I})", {"item_id": "item"}),
    ("changes", "id IN ({C})", {}),
    ("proposals", "item_id IN ({I})", {"item_id": "item", "revision_id": "rev",
                                       "change_id": "change"}),
    ("approvals", "revision_id IN ({R})", {"revision_id": "rev",
                                           "proposal_id": "proposal"}),
    ("reviews", "proposal_id IN ({P})", {"proposal_id": "proposal",
                                         "approval_id": "approval"}),
    ("change_reviews", "change_id IN ({C})", {"change_id": "change",
                                              "heads": "heads_json"}),
    ("change_impacts", "change_id IN ({C})", {"change_id": "change",
                                              "parent_item_id": "item_ext"}),
    ("threads", "item_id IN ({I})", {"item_id": "item", "revision_id": "rev",
                                     "subject_id": "subject"}),
    ("checks", "item_id IN ({I})", {"item_id": "item"}),
    ("context", "item_id IN ({I})", {"item_id": "item", "revision_id": "rev",
                                     "target_id": "ctx_target"}),
    ("events", "item_id IN ({I})", {"item_id": "item", "revision_id": "rev",
                                    "data": "event_json"}),
]
#: ids worth keeping: they are NAMES people and refs use (CHG-n, proposal #n
#: and its refs/plm/proposals/<n>/head) — kept when free on the target
KEEP_IDS = ("changes", "proposals")
REFUSED_TABLES = ("releases", "release_sets", "release_signoffs")
#: events.data keys that hold an id of a carried row, and the map they use
EVENT_KEYS = {"proposal": "proposals", "change": "changes",
              "thread": "threads", "approval_id": "approvals",
              "round": "approvals", "revision_id": "revisions"}
MAP_OF = {"item": "items", "rev": "revisions", "proposal": "proposals",
          "change": "changes", "approval": "approvals"}


class CarryError(RuntimeError):
    pass


def _ids(xs) -> str:
    xs = sorted({int(x) for x in xs if x is not None})
    return ",".join(str(x) for x in xs) or "NULL"


def _cols(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info([{table}])")]


def _has(conn, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name=?", (table,)).fetchone())


def _git(*args: str, cwd: str | None = None, binary: bool = False,
         check: bool = True, stdin: bytes | None = None):
    r = subprocess.run(["git", *args], cwd=cwd, input=stdin,
                       capture_output=True, timeout=600)
    if check and r.returncode != 0:
        raise CarryError(f"git {' '.join(args[:3])}: "
                         f"{r.stderr.decode(errors='replace').strip()[:400]}")
    return r.stdout if binary else r.stdout.decode(errors="replace")


def _lfs_obj(root: Path, oid: str) -> Path:
    return root / "objects" / oid[:2] / oid[2:4] / oid


def _put_obj(root: Path, src: Path | None, data: bytes | None, oid: str) -> int:
    """Store bytes content-addressed under `root`; the hash is the promise."""
    dest = _lfs_obj(root, oid)
    if dest.is_file() and sha256_file(dest)[0] == oid:
        return dest.stat().st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    if src is not None:
        shutil.copyfile(src, tmp)
    else:
        tmp.write_bytes(data or b"")
    got = sha256_file(tmp)[0]
    if got != oid:
        tmp.unlink()
        raise CarryError(f"bytes hash to {got[:12]}…, not the recorded "
                         f"{oid[:12]}… — not stored")
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)
    return dest.stat().st_size


# =============================================================================
# EXPORT
# =============================================================================

def _source_rows(conn, project: str) -> tuple[dict, dict]:
    """{table: [rows]} for the project, and the scope id sets."""
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM items WHERE project=? ORDER BY id", (project,))]
    if not items:
        raise CarryError(f"no items in project {project!r} on the source")
    I = [i["id"] for i in items]
    R = [r[0] for r in conn.execute(
        f"SELECT id FROM revisions WHERE item_id IN ({_ids(I)})")]
    P = ([r[0] for r in conn.execute(
        f"SELECT id FROM proposals WHERE item_id IN ({_ids(I)})")]
        if _has(conn, "proposals") else [])
    C = ([r[0] for r in conn.execute(
        f"SELECT DISTINCT change_id FROM proposals WHERE item_id IN ({_ids(I)}) "
        "AND change_id IS NOT NULL")] if P else [])
    for t in REFUSED_TABLES:
        if not _has(conn, t):
            continue
        col = "root_item_id" if t == "release_sets" else (
            "item_id" if t == "releases" else None)
        if col and conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} IN "
                                f"({_ids(I)})").fetchone()[0]:
            raise CarryError(
                f"{project} has `{t}` rows: release evidence lives outside the "
                "database and plm.carry does not move it yet (stage 20). Carry "
                "a project before its first release-set, or extend carry.")
    if C:
        foreign = conn.execute(
            f"SELECT p.change_id, i.number FROM proposals p JOIN items i ON "
            f"i.id=p.item_id WHERE p.change_id IN ({_ids(C)}) AND p.item_id NOT "
            f"IN ({_ids(I)})").fetchall()
        if foreign:
            raise CarryError(
                "a change of this project has members in another project: "
                + ", ".join(f"CHG-{c} {n}" for c, n in foreign)
                + " — carry both projects together, or split the change first")
    scope = {"I": _ids(I), "R": _ids(R), "P": _ids(P), "C": _ids(C)}
    rows: dict[str, list[dict]] = {}
    for table, where, _ in TABLES:
        if not _has(conn, table):
            continue
        rows[table] = [dict(r) for r in conn.execute(
            f"SELECT * FROM [{table}] WHERE {where.format(**scope)}"
            + (" ORDER BY id" if "id" in _cols(conn, table) else ""))]
    return rows, {"I": I, "R": R, "P": P, "C": C}


def _external_refs(conn, rows: dict, I: list[int]) -> dict:
    """Items the project references but does not contain: {source id: {number,
    kind, versions}}. They are not carried — the target must have them."""
    mine = set(I)
    want: dict[int, set] = defaultdict(set)
    for b in rows.get("bom", []):
        if b["child_item_id"] not in mine:
            want[b["child_item_id"]].add(b["child_version"] or "")
    for r in rows.get("relations", []):
        if r["target_item_id"] and r["target_item_id"] not in mine:
            want[r["target_item_id"]].add(r.get("target_version") or "")
    for s in rows.get("source_pins", []):
        if s["source_item_id"] and s["source_item_id"] not in mine:
            want[s["source_item_id"]].add(s.get("version") or "")
    for ci in rows.get("change_impacts", []):
        if ci["parent_item_id"] not in mine:
            want[ci["parent_item_id"]].add("")
    out = {}
    for iid, vs in want.items():
        it = conn.execute("SELECT number, kind, project, name FROM items "
                          "WHERE id=?", (iid,)).fetchone()
        if it:
            out[str(iid)] = {"number": it[0], "kind": it[1], "project": it[2],
                             "name": it[3], "versions": sorted(v for v in vs if v)}
    return out


def _left_behind(conn, project: str, I: list[int]) -> list[str]:
    """Claims OTHER projects' items make about this one — theirs to carry."""
    out = []
    if not _has(conn, "relations"):
        return out
    for r in conn.execute(
            f"SELECT r.relation, a.number, b.number FROM relations r JOIN items a "
            f"ON a.id=r.item_id JOIN items b ON b.id=r.target_item_id WHERE "
            f"r.target_item_id IN ({_ids(I)}) AND r.item_id NOT IN ({_ids(I)})"):
        out.append(f"{r[1]} {r[0]} {r[2]}")
    return out


def _open_ro(src_db: Path) -> sqlite3.Connection:
    """The source, read-only. A WAL database on a read-only mount cannot make
    its -shm file, so a SNAPSHOT there is opened `immutable` — never a live
    registry (take an online backup first, as the rehearsals do)."""
    conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        conn.execute("SELECT 1 FROM items LIMIT 1")
    except sqlite3.OperationalError:
        conn.close()
        conn = sqlite3.connect(f"file:{src_db}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def export(src_db: Path, project: str, out: Path, *, vault_root: Path | None,
           repos: Path | None, lfs_root: Path | None, git_mode: str,
           instance: str = "", dry_run: bool = False) -> dict:
    """Write the bundle for `project` from a source registry, READ-ONLY."""
    conn = _open_ro(src_db)
    rows, ids = _source_rows(conn, project)
    items = rows["items"]
    ext = _external_refs(conn, rows, ids["I"])
    behind = _left_behind(conn, project, ids["I"])
    catalog_kinds = ("purchased", "hardware")

    # per item: does it travel as its LIVE repo, or as registry rows that the
    # target's split turns into history?
    per_item = {}
    for it in items:
        how = "registry"
        if it["kind"] in catalog_kinds:
            how = "catalog"
        elif git_mode == "write" and repos is not None:
            r = repos / f"{it['number']}.git"
            if (r / "plm-written").is_file():
                how = "repo"
        per_item[it["number"]] = how
    for n, how in per_item.items():
        if how != "repo":
            continue
        # a live repo whose trees pin the SOURCE catalog cannot keep that pin
        # on another instance's catalog.git — refused until it is needed
        r = repos / f"{n}.git"
        for sha in _git("--git-dir", str(r), "for-each-ref",
                        "--format=%(objectname)", "refs/heads",
                        "refs/tags").split():
            tree = _git("--git-dir", str(r), "ls-tree", f"{sha}^{{commit}}",
                        check=False)
            if any(ln.endswith("\tcatalog") and " commit " in ln
                   for ln in tree.splitlines()):
                raise CarryError(
                    f"{n} is a live repo whose trees pin catalog.git; carrying "
                    "that pin to another instance's catalog is not supported "
                    "yet (stage 20)")

    plan = {"schema": SCHEMA, "project": project,
            "source": {"instance": instance, "db": str(src_db),
                       "git_mode": git_mode,
                       "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime())},
            "items": [{"number": i["number"], "kind": i["kind"],
                       "carry": per_item[i["number"]]} for i in items],
            "counts": {t: len(v) for t, v in rows.items() if v},
            "external": ext, "left_behind": behind}
    by_rev = {r["id"]: r for r in rows["revisions"]}
    item_of = {i["id"]: i for i in items}

    vault_files, hist_files, ext_objs, misses, mismatch = [], [], {}, [], []
    authors: dict[str, list] = {}
    from . import split as splitmod          # Vault: the same reader split uses
    vault = splitmod.Vault(vault_root) if vault_root else None
    for f in rows["files"]:
        rev = by_rev[f["revision_id"]]
        it = item_of[rev["item_id"]]
        how = per_item[it["number"]]
        backend = f["backend"] or "vault"
        if backend == "lfs" or (
                how != "repo" and backend == "git"):
            if backend == "git":
                continue          # a read-mode projection's row: rebuilt by split
            oid = (f["sha256"] or "").lower()
            if len(oid) == 64:
                ext_objs[oid] = f
            else:
                misses.append(f"{f['path']} ({backend}, no sha256)")
            continue
        if how == "repo" or backend == "git":
            continue              # the repo carries it
        if vault is None:
            raise CarryError("registry-source files need --vault (the source's "
                             "DATA_ROOT, read-only)")
        data, p = vault.read(f["path"], rev["git_commit"] or "", quiet=True)
        if p is None and data is None:
            misses.append(f"{f['path']} (vault: not on disk, not in "
                          f"{(rev['git_commit'] or 'no commit')[:10]})")
            continue
        got = sha256_file(p)[0] if p is not None else hashlib.sha256(data).hexdigest()
        if f["sha256"] and got != f["sha256"]:
            mismatch.append(f"{f['path']}: row {f['sha256'][:12]}… bytes "
                            f"{got[:12]}…")
        if p is not None:
            vault_files.append((f["path"], p))
        else:
            hist_files.append((rev["git_commit"], f["path"], data))
    if vault is not None:
        for it in items:
            if it["folder"] and per_item[it["number"]] != "repo":
                p = vault_root / it["folder"] / "ITEM.md"
                if p.is_file():
                    vault_files.append((f"{it['folder']}/ITEM.md", p))
        for c in sorted({r["git_commit"] for r in rows["revisions"]
                         if r["git_commit"]}):
            a = vault.author(c)
            if a:
                authors[c] = list(a)

    repo_meta = {}
    if repos is not None:
        for n, how in per_item.items():
            if how != "repo":
                continue
            r = repos / f"{n}.git"
            head = _git("--git-dir", str(r), "symbolic-ref", "-q", "HEAD",
                        check=False).strip()
            refs = _git("--git-dir", str(r), "for-each-ref",
                        "--format=%(refname) %(objectname)").splitlines()
            # every LFS pointer any ref reaches (blobs under 200 bytes that parse)
            oids = set()
            objs = _git("--git-dir", str(r), "rev-list", "--objects", "--all")
            names = [ln.split(" ", 1)[0] for ln in objs.splitlines() if ln]
            if names:
                chk = _git("--git-dir", str(r), "cat-file",
                           "--batch-check=%(objectname) %(objecttype) "
                           "%(objectsize)", stdin="\n".join(names).encode())
                small = [ln.split()[0] for ln in chk.splitlines()
                         if ln.split()[1:2] == ["blob"]
                         and int(ln.split()[2]) < 400]
                if small:
                    raw = _git("--git-dir", str(r), "cat-file", "--batch",
                               stdin="\n".join(small).encode(), binary=True)
                    for line in raw.split(b"\n"):
                        if line.startswith(b"oid sha256:"):
                            oids.add(line[11:].decode().strip())
            for oid in oids:
                ext_objs.setdefault(oid, {"backend": "lfs", "key": oid,
                                          "path": f"{n} (pointer)",
                                          "sha256": oid})
            repo_meta[n] = {"head": head, "refs": len(refs),
                            "config": {k: _git("--git-dir", str(r), "config",
                                               "--get", k, check=False).strip()
                                       for k in ("plm.number", "plm.kind")},
                            "written": (r / "plm-written").read_text()
                            if (r / "plm-written").is_file() else "",
                            "description": (r / "description").read_text()
                            if (r / "description").is_file() else "",
                            "lfs_pointers": len(oids)}

    plan["files"] = {"vault_on_disk": len({p for p, _ in vault_files}),
                     "vault_history_only": len(hist_files),
                     "objects": len(ext_objs),
                     "not_found": misses, "sha256_mismatch": mismatch}
    plan["repos"] = {n: {k: v for k, v in m.items()
                         if k not in ("written", "description")}
                     for n, m in repo_meta.items()}
    if dry_run:
        return plan

    if out.exists() and any(out.iterdir()):
        raise CarryError(f"{out} is not empty — a bundle is written once")
    out.mkdir(parents=True, exist_ok=True)
    nbytes = 0
    for rel, p in vault_files:
        dest = out / "vault" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        nbytes += dest.stat().st_size
    for commit, rel, data in hist_files:
        dest = out / "vault-history" / commit / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        nbytes += len(data)
    obj_root = out / "lfs"
    for oid, f in sorted(ext_objs.items()):
        src = _lfs_obj(lfs_root, oid) if lfs_root else None
        if src is not None and src.is_file():
            nbytes += _put_obj(obj_root, src, None, oid)
            continue
        misses.append(f"{f.get('path')}: LFS object {oid[:12]}… not in "
                      f"{lfs_root}")
    for n in repo_meta:
        rd = out / "repos"
        rd.mkdir(parents=True, exist_ok=True)
        _git("--git-dir", str(repos / f"{n}.git"), "bundle", "create",
             str(rd / f"{n}.bundle"), "--all")
        _git("bundle", "verify", "-q", str(rd / f"{n}.bundle"),
             cwd=str(repos / f"{n}.git"))
        (rd / f"{n}.json").write_text(json.dumps(repo_meta[n], indent=2) + "\n")
        nbytes += (rd / f"{n}.bundle").stat().st_size
    (out / "rows.json").write_text(json.dumps(rows, indent=1, default=str) + "\n")
    (out / "authors.json").write_text(json.dumps(authors, indent=1) + "\n")
    plan["files"]["not_found"] = misses
    plan["bytes"] = nbytes
    (out / "carry.json").write_text(json.dumps(plan, indent=2) + "\n")
    return plan


# =============================================================================
# IMPORT
# =============================================================================

class BundleVault:
    """`split.Vault`'s two methods, answered from the bundle — so split builds
    the carried revisions from exactly the bytes the source served, dated and
    attributed by the source vault's commits."""

    def __init__(self, bundle: Path):
        self.root = bundle / "vault"
        self.hist = bundle / "vault-history"
        try:
            raw = json.loads((bundle / "authors.json").read_text())
        except (OSError, ValueError):
            raw = {}
        self._authors = {k: (v[0], v[1], int(v[2])) for k, v in raw.items()}
        self.misses: list[str] = []

    def read(self, rel: str, git_commit: str = "", *, quiet: bool = False):
        p = self.root / rel
        if p.is_file():
            return None, p
        if git_commit:
            h = self.hist / git_commit / rel
            if h.is_file():
                return h.read_bytes(), None
        if not quiet:
            self.misses.append(rel)
        return None, None

    def author(self, commit: str):
        return self._authors.get(commit) if commit else None


def _load_bundle(bundle: Path) -> tuple[dict, dict]:
    try:
        meta = json.loads((bundle / "carry.json").read_text())
        rows = json.loads((bundle / "rows.json").read_text())
    except (OSError, ValueError) as exc:
        raise CarryError(f"{bundle} is not a carry bundle: {exc}") from None
    if meta.get("schema") != SCHEMA:
        raise CarryError(f"bundle schema {meta.get('schema')} != {SCHEMA}")
    return meta, rows


def _preflight(conn, meta: dict, rows: dict, repos: Path, *,
               stub_external: bool = False) -> dict:
    """Everything that refuses, before anything is written. `stub_external`:
    a referenced item the target lacks is CREATED (number, name, kind, project
    — as `ops.create_item` makes one, nothing else) when nothing names a
    revision of it: an asset a design mounts_to, typically. A reference to a
    revision still refuses."""
    from . import db
    problems, notes = [], []
    project = meta["project"]
    numbers = [i["number"] for i in rows["items"]]
    clash = [n for n in numbers if db.resolve_item(conn, n)]
    if clash:
        problems.append("these numbers already exist on the target (refusing "
                        "to import over an existing project): "
                        + ", ".join(clash))
    if conn.execute("SELECT COUNT(*) FROM items WHERE project=?",
                    (project,)).fetchone()[0]:
        problems.append(f"project {project!r} already has items on the target")
    have_repo = [n for n in numbers if (repos / f"{n}.git").exists()]
    if have_repo:
        problems.append("repo directories already exist under the target root: "
                        + ", ".join(have_repo))
    resolved, stubs = {}, {}
    for sid, e in (meta.get("external") or {}).items():
        it = db.resolve_item(conn, e["number"])
        if not it and stub_external and not e["versions"]:
            stubs[int(sid)] = e
            notes.append(f"{e['number']} ({e['kind']}) does not exist on the "
                         "target: created, with no files")
            continue
        if not it:
            problems.append(f"{e['number']} ({e['kind']}, project "
                            f"{e['project'] or '—'}) is referenced and does "
                            "not exist on the target — carry or create it "
                            "first" + ("" if e["versions"] else
                                       ", or pass --stub-external"))
            continue
        if it["kind"] != e["kind"]:
            notes.append(f"{e['number']} is {it['kind']} on the target, "
                         f"{e['kind']} on the source")
        missing = [v for v in e["versions"] if not conn.execute(
            "SELECT 1 FROM revisions WHERE item_id=? AND version=?",
            (it["id"], v)).fetchone()]
        if missing:
            problems.append(f"{e['number']} has no v{', v'.join(missing)} on "
                            "the target (a BOM line or claim names it)")
        resolved[int(sid)] = it["id"]
    for t, rs in rows.items():
        if rs and not _has(conn, t):
            problems.append(f"the target has no `{t}` table (carry needs "
                            "the same schema — start the target once first)")
    return {"problems": problems, "notes": notes, "external": resolved,
            "stubs": stubs}


def _as_change_members(meta: dict, rows: dict, since: str) -> list[dict]:
    """The revisions `--as-change` turns into ONE change: unreleased, newer
    than the project's last release (and than --since), the newest revision of
    their item. Refuses ambiguity rather than guess."""
    from . import manifest
    revs = rows["revisions"]
    released = [r for r in revs if r["state"] not in manifest.UNRELEASED_STATES]
    floor = max((r["created_at"] or 0 for r in released), default=None)
    cut = None
    if since:
        try:
            import calendar
            cut = (calendar.timegm(time.strptime(since, "%Y-%m-%d"))
                   if len(since) == 10 else float(since))
        except ValueError:
            raise CarryError(f"--since {since!r}: give YYYY-MM-DD or an epoch") \
                from None
    if floor is None and cut is None:
        days = defaultdict(int)
        for r in revs:
            days[time.strftime("%Y-%m-%d", time.gmtime(r["created_at"] or 0))] += 1
        raise CarryError(
            f"nothing in {meta['project']} was ever released, so \"the drafts "
            "after the last release\" is every draft. Say which with --since "
            "YYYY-MM-DD. Unreleased revisions by day (UTC): "
            + ", ".join(f"{d}: {n}" for d, n in sorted(days.items())))
    lo = max(x for x in (floor, cut) if x is not None)
    item_of = {i["id"]: i for i in rows["items"]}
    newest: dict[int, dict] = {}
    for r in revs:
        k = (r["vmaj"], r["vmin"], r["vpat"])
        cur = newest.get(r["item_id"])
        if not cur or k > (cur["vmaj"], cur["vmin"], cur["vpat"]):
            newest[r["item_id"]] = r
    picked = [r for r in revs if r["state"] in manifest.UNRELEASED_STATES
              and (r["created_at"] or 0) > lo]
    per_item = defaultdict(list)
    for r in picked:
        per_item[r["item_id"]].append(r)
    out = []
    for iid, rs in per_item.items():
        n = item_of[iid]["number"]
        if len(rs) > 1:
            raise CarryError(f"{n} has {len(rs)} unreleased revisions after the "
                             "cut (" + ", ".join("v" + r["version"] for r in rs)
                             + ") — one proposal per item per change; pick a "
                             "later --since")
        if rs[0]["id"] != newest[iid]["id"]:
            raise CarryError(f"{n} v{rs[0]['version']} is after the cut but "
                             f"v{newest[iid]['version']} is newer")
        out.append({"number": n, "version": rs[0]["version"],
                    "source_rev": rs[0]["id"], "author": rs[0]["author"],
                    "summary": rs[0]["summary"], "kind": item_of[iid]["kind"],
                    "created_at": rs[0]["created_at"]})
    if not out:
        raise CarryError("--as-change: no unreleased revision is newer than the "
                         "cut")
    return out


def _remap_row(table: str, row: dict, remaps: dict, maps: dict,
               ext: dict) -> dict:
    out = dict(row)
    for col, kind in remaps.items():
        v = out.get(col)
        if v is None or v == "":
            continue
        if kind == "defer":
            out[col] = None
        elif kind in MAP_OF:
            m = maps[MAP_OF[kind]]
            if int(v) not in m:
                raise CarryError(f"{table}.{col}={v}: not a carried "
                                 f"{MAP_OF[kind]} row")
            out[col] = m[int(v)]
        elif kind == "item_ext":
            out[col] = maps["items"].get(int(v)) or ext.get(int(v))
            if out[col] is None:
                raise CarryError(f"{table}.{col}={v}: unresolved item")
        elif kind == "heads_json":
            try:
                h = json.loads(v or "{}")
                out[col] = json.dumps({str(maps["proposals"].get(int(k), k)): s
                                       for k, s in h.items()})
            except (ValueError, TypeError):
                pass
        elif kind == "event_json":
            try:
                d = json.loads(v)
            except (ValueError, TypeError):
                continue
            if isinstance(d, dict):
                for k, t in EVENT_KEYS.items():
                    x = d.get(k)
                    if isinstance(x, int) and x in maps[t]:
                        d[k] = maps[t][x]
                out[col] = json.dumps(d)
        elif kind == "subject":
            st = row.get("subject_type")
            m = {"proposal": "proposals", "release": None}.get(st)
            if m and int(v) in maps[m]:
                out[col] = maps[m][int(v)]
            elif st and st != "item":
                raise CarryError(f"threads.subject_type={st!r} is not carried")
        elif kind == "ctx_target":
            tt = row.get("target_type") or ""
            if tt == "":
                if row.get("revision_id") is not None and int(v) == row["revision_id"]:
                    out[col] = maps["revisions"][int(v)]
                elif int(v) == row["item_id"]:
                    out[col] = maps["items"][int(v)]
            elif tt == "relation":
                out[col] = maps["relations"].get(int(v), v)
            elif tt == "thread":
                out[col] = maps["threads"].get(int(v), v)
    return out


def _insert_rows(conn, rows: dict, ext: dict) -> dict:
    """Every row, new ids (proposals and changes keep theirs when free)."""
    maps: dict[str, dict] = defaultdict(dict)
    counts = {}
    for table, _, remaps in TABLES:
        src = rows.get(table) or []
        if not src:
            continue
        cols = set(_cols(conn, table))
        dropped = set()
        n = 0
        for row in src:
            new = _remap_row(table, row, remaps, maps, ext)
            old_id = new.pop("id", None) if "id" in new else None
            if old_id is not None and table in KEEP_IDS and not conn.execute(
                    f"SELECT 1 FROM [{table}] WHERE id=?", (old_id,)).fetchone():
                new["id"] = old_id
            keep = {k: v for k, v in new.items() if k in cols}
            dropped |= {k for k, v in new.items() if k not in cols
                        and v not in (None, "", 0)}
            names = ", ".join(f"[{k}]" for k in keep)
            qs = ", ".join("?" for _ in keep)
            cur = conn.execute(f"INSERT INTO [{table}] ({names}) VALUES ({qs})",
                               list(keep.values()))
            if old_id is not None:
                maps[table][int(old_id)] = new.get("id") or cur.lastrowid
            n += 1
        counts[table] = n
        if dropped:
            raise CarryError(f"the target's `{table}` has no column(s) "
                             f"{sorted(dropped)} and the source fills them")
    for it in rows["items"]:
        if it.get("latest_rev_id") in maps["revisions"]:
            conn.execute("UPDATE items SET latest_rev_id=? WHERE id=?",
                         (maps["revisions"][it["latest_rev_id"]],
                          maps["items"][it["id"]]))
    return {"maps": maps, "counts": counts}


def _fetch_repo(bundle: Path, repos: Path, number: str, meta: dict,
                actor: str, source: str, pmap: dict) -> dict:
    """A LIVE repo arrives as itself: every ref, then plm's bookkeeping."""
    from . import gitwrite
    b = bundle / "repos" / f"{number}.bundle"
    rmeta = json.loads((bundle / "repos" / f"{number}.json").read_text())
    repo = repos / f"{number}.git"
    _git("init", "-q", "--bare", str(repo))
    _git("--git-dir", str(repo), "fetch", "-q", str(b), "refs/*:refs/*")
    if rmeta.get("head"):
        _git("--git-dir", str(repo), "symbolic-ref", "HEAD", rmeta["head"])
    for k, v in (rmeta.get("config") or {}).items():
        if v:
            _git("--git-dir", str(repo), "config", k, v)
    if rmeta.get("description"):
        (repo / "description").write_text(rmeta["description"])
    # proposal ids that changed on the way in: their refs follow
    moved = 0
    for line in _git("--git-dir", str(repo), "for-each-ref",
                     "--format=%(refname) %(objectname)",
                     "refs/plm/proposals").splitlines():
        ref, sha = line.split()
        parts = ref.split("/")
        old = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
        new = pmap.get(old) if old is not None else None
        if new and new != old:
            parts[3] = str(new)
            _git("--git-dir", str(repo), "update-ref", "/".join(parts), sha)
            _git("--git-dir", str(repo), "update-ref", "-d", ref, sha)
            moved += 1
    (repo / gitwrite.MARKER).write_text(rmeta.get("written") or "")
    gitwrite.mark_written(repo, {"origin": "carry", "actor": actor,
                                 "from": source, "refs": rmeta.get("refs")})
    return {"refs": len(_git("--git-dir", str(repo), "for-each-ref",
                             "--format=%(refname)").splitlines()),
            "proposal_refs_renumbered": moved}


def import_(bundle: Path, *, actor: str, as_change: bool = False,
            since: str = "", mode: str = "", submit: bool = False,
            dry_run: bool = False, vault_dir: Path | None = None,
            stub_external: bool = False) -> dict:
    from . import (catalog as catalogmod, changes, config, context as ctxmod,
                   db, gitmode, gitwrite, lfs, manifest, ops, search, split)
    meta, rows = _load_bundle(bundle)
    project = meta["project"]
    source = meta["source"].get("instance") or meta["source"].get("db") or "?"
    db.init()
    with db.connect() as conn:
        for mod in ("proposals", "changes", "threads", "requirements", "sources"):
            try:
                m = __import__(f"plm.{mod}", fromlist=["ensure"])
                if hasattr(m, "ensure"):
                    m.ensure(conn)
            except Exception:                                  # noqa: BLE001
                pass
    with db.connect() as conn:
        pre = _preflight(conn, meta, rows, config.REPOS,
                         stub_external=stub_external)
        target_mode = mode or gitmode.mode_for(project, conn)
    carry_of = {i["number"]: i["carry"] for i in meta["items"]}
    design = [i for i in rows["items"] if carry_of[i["number"]] != "catalog"]
    catalog_items = [i for i in rows["items"]
                     if carry_of[i["number"]] == "catalog"]
    serves = target_mode in ("read", "write")
    members = (_as_change_members(meta, rows, since) if as_change else [])
    if as_change and target_mode != "write":
        pre["problems"].append(f"--as-change needs the project in `write` on "
                               f"the target; it would be {target_mode!r} "
                               "(pass --mode write)")
    if as_change and any(carry_of[m["number"]] == "repo" for m in members):
        pre["problems"].append("--as-change is for a registry source: a live "
                               "repo's drafts already are its proposals")
    if any(v == "repo" for v in carry_of.values()) and not serves:
        pre["problems"].append(f"the source's live repos need a target that "
                               f"serves git for {project}; it would be "
                               f"{target_mode!r}")
    if not serves and design and vault_dir is None:
        pre["problems"].append(f"the target does not serve git for {project} "
                               f"({target_mode!r}): its rows name vault paths, "
                               "so --vault <the target's DATA_ROOT> is needed")
    objs = sorted(p.name for p in (bundle / "lfs" / "objects").rglob("*")
                  if p.is_file()) if (bundle / "lfs").is_dir() else []
    revs_by_item = defaultdict(list)
    for r in rows["revisions"]:
        revs_by_item[r["item_id"]].append(r)
    member_ids = {m["source_rev"] for m in members}
    plan = {
        "project": project, "from": source, "source_mode":
            meta["source"].get("git_mode"), "target_mode": target_mode,
        "items": {i["number"]: {
            "carry": carry_of[i["number"]],
            "becomes": ("its own repo, every ref" if carry_of[i["number"]] == "repo"
                        else "a catalog.git entry" if carry_of[i["number"]] == "catalog"
                        else "split history" if serves else "registry rows"),
            "revisions": [("v" + r["version"] + (" ->CHANGE" if r["id"] in member_ids
                           else "" if r["state"] in manifest.UNRELEASED_STATES
                           else " tag"))
                          for r in sorted(revs_by_item[i["id"]],
                                          key=lambda r: (r["vmaj"], r["vmin"],
                                                         r["vpat"]))]}
            for i in rows["items"]},
        "rows": {t: len(v) for t, v in rows.items() if v},
        "lfs_objects": len(objs),
        "external_resolved": len(pre["external"]),
        "external_stubbed": [e["number"] for e in pre["stubs"].values()],
        "as_change": [f"{m['number']} v{m['version']} ({m['author']})"
                      for m in members],
        "problems": pre["problems"], "notes": pre["notes"]}
    if dry_run or pre["problems"]:
        plan["dry_run"] = True
        return plan

    # -- LFS objects: content-addressed, re-hashed on the way in -------------
    stored = already = 0
    for p in (bundle / "lfs" / "objects").rglob("*") if objs else []:
        if not p.is_file():
            continue
        if lfs.has(p.name, p.stat().st_size):
            already += 1
            continue
        oid, _ = lfs.put_file(p)
        if oid != p.name:
            raise CarryError(f"bundle object {p.name[:12]}… hashes to "
                             f"{oid[:12]}…")
        stored += 1
    plan["lfs"] = {"stored": stored, "already": already}

    created_repos: list[Path] = []
    with db.connect() as conn:
        try:
            for sid, e in pre["stubs"].items():
                made = ops.create_item(conn, e.get("name") or e["number"],
                                       e["kind"], e["project"] or project,
                                       number=e["number"], actor=actor)
                pre["external"][sid] = made["id"]
            ins = _insert_rows(conn, rows, pre["external"])
            maps = ins["maps"]
            plan["inserted"] = ins["counts"]
            plan["renumbered"] = {
                t: {f"{o}": n for o, n in maps[t].items() if o != n}
                for t in KEEP_IDS if any(o != n for o, n in maps[t].items())}
            numbers = [i["number"] for i in rows["items"]]
            search.index_items(conn, numbers)
            if mode:
                gitmode.set_mode(conn, project=project, mode=mode, actor=actor,
                                 note=f"carried from {source}")
            for i in rows["items"]:
                db.add_event(conn, "item.carried", item_id=maps["items"][i["id"]],
                             actor=actor, data={"from": source, "project": project,
                                                "source_id": i["id"],
                                                "carry": carry_of[i["number"]]})
                ctxmod.add(conn,
                           f"**Carried** from `{source}` ({meta['source'].get('git_mode')}"
                           f" mode) by `{actor}` with `plm.carry` on "
                           f"{time.strftime('%Y-%m-%d', time.gmtime())}: "
                           f"{len(revs_by_item[i['id']])} revision(s) and their "
                           "history, numbers unchanged"
                           + (" — the unreleased correction arrived as a CHANGE "
                              "(see its proposal), not as silent drafts"
                              if any(m["number"] == i["number"] for m in members)
                              else "") + ".",
                           target=i["number"], actor=actor, event=False)
            # -- repos -------------------------------------------------------
            pmap = maps.get("proposals", {})
            for i in design:
                if carry_of[i["number"]] == "repo":
                    created_repos.append(config.REPOS / f"{i['number']}.git")
                    plan.setdefault("repos", {})[i["number"]] = _fetch_repo(
                        bundle, config.REPOS, i["number"], meta, actor, source,
                        pmap)
            reg_roots = [maps["items"][i["id"]] for i in design
                         if carry_of[i["number"]] == "registry"]
            if serves and (reg_roots or catalog_items):
                cat_override = None
                cat_repo = config.REPOS / "catalog.git"
                if catalog_items and gitwrite.is_live(cat_repo):
                    for i in catalog_items:
                        for r in sorted(revs_by_item[i["id"]],
                                        key=lambda r: (r["vmaj"], r["vmin"],
                                                       r["vpat"])):
                            catalogmod.write_entry(
                                conn, db.resolve_item(conn, i["number"]),
                                maps["revisions"][r["id"]], actor=actor,
                                message=f"{i['number']} v{r['version']}: carried "
                                        f"from {source}")
                if gitwrite.is_live(cat_repo):
                    cat_override = {"repo": cat_repo, "items": 0, "skipped": "live",
                                    "commits": [(0, catalogmod.tip())]}
                if reg_roots:
                    for n in (i["number"] for i in design
                              if carry_of[i["number"]] == "registry"):
                        created_repos.append(config.REPOS / f"{n}.git")
                    res = split.run(conn, roots=reg_roots,
                                    vault=BundleVault(bundle),
                                    catalog=cat_override)
                    bad = {k: v for k, v in res["problems"].items()
                           if k in ("files_not_recoverable",
                                    "vault_paths_not_found",
                                    "submodule_child_not_built",
                                    "submodule_child_unpinnable",
                                    "bom_lines_with_missing_child")}
                    if bad:
                        raise CarryError(f"split could not represent the carried "
                                         f"revisions: {json.dumps(bad)[:800]}")
                    plan["split"] = {k: res["summary"][k] for k in (
                        "item_repos", "commits", "tags", "draft_branches",
                        "submodule_links", "lfs") if k in res["summary"]}
                    plan["split"]["lfs_external_to_pointer"] = res["summary"].get(
                        "lfs_external_to_pointer", 0)
                    plan["split"]["problems"] = {k: len(v) for k, v in
                                                 res["problems"].items()}
                elif catalog_items and not gitwrite.is_live(cat_repo):
                    split.build_catalog(conn, split.load(conn),
                                        BundleVault(bundle))
            elif not serves and vault_dir is not None:
                plan["vault_files"] = _place_vault(bundle, vault_dir)
            # -- --as-change -------------------------------------------------
            if members:
                plan["change"] = _open_change(conn, members, maps, rows, meta,
                                              actor=actor, submit=submit)
            conn.commit()
        except Exception:
            conn.rollback()
            for r in created_repos:
                shutil.rmtree(r, ignore_errors=True)
            raise
    plan["next_number"] = _next_numbers(rows)
    plan["verify"] = verify(rows, catalog=bool(catalog_items), serves=serves)
    return plan


def _place_vault(bundle: Path, vault_dir: Path) -> dict:
    """Registry -> registry: the bytes land at the same vault paths. An
    existing file with DIFFERENT bytes is refused, never overwritten."""
    src = bundle / "vault"
    n = same = 0
    for p in sorted(src.rglob("*")) if src.is_dir() else []:
        if not p.is_file():
            continue
        dest = vault_dir / p.relative_to(src)
        if dest.exists():
            if sha256_file(dest) != sha256_file(p):
                raise CarryError(f"{dest} exists with different bytes")
            same += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        n += 1
    return {"written": n, "already_identical": same}


def _open_change(conn, members: list[dict], maps: dict, rows: dict, meta: dict,
                 *, actor: str, submit: bool) -> dict:
    """The carried correction as ONE change: children first, so the parent's
    proposal is indexed with its gitlinks already on the children's heads."""
    from . import changes, gitwrite, ops
    order = sorted(members, key=lambda m: (m["kind"] == "assembly", m["number"]))
    out = {"members": []}
    pids = []
    for m in order:
        ref = f"refs/heads/draft/v{m['version']}"
        repo = gitwrite.repo_path(m["number"])
        sha = gitwrite.tip(repo, ref)
        if not sha:
            raise CarryError(f"{m['number']}: split made no {ref}")
        res = ops.index_draft(conn, m["number"], sha, ref=ref,
                              actor=m["author"] or actor, origin="carry",
                              state="DRAFT", mover=m["author"] or actor)
        p = res.get("proposal") or {}
        if not p.get("id"):
            raise CarryError(f"{m['number']} {ref}: no proposal opened")
        pids.append(p["id"])
        out["members"].append({"number": m["number"], "version": m["version"],
                               "proposal": p["id"], "head": sha[:10],
                               "author": m["author"]})
    summaries = sorted({m["summary"] for m in members if m["summary"]})
    title = (summaries[0] if len(summaries) == 1 else
             f"{meta['project']}: carried drafts")[:200]
    body = (
        f"Carried from `{meta['source'].get('instance') or '?'}` "
        f"({meta['source'].get('git_mode')} mode) by `plm.carry`. These "
        f"{len(members)} revisions were written there as silent DRAFTS through "
        "the legacy tools; on this instance they are ONE "
        "change for a human to review: "
        + ", ".join(f"{m['number']} v{m['version']} by {m['author'] or '?'}"
                    for m in members)
        + ". The revisions before them — including the printed ones — are on "
          "their own `draft/*` branches, unchanged, with every context note.")
    c = changes.create(conn, title=title, body=body, project=meta["project"],
                       actor=actor, origin="carry")
    for pid in pids:
        changes.add_member(conn, c["id"], pid, actor=actor, origin="carry")
    mem = changes.members(conn, c["id"])
    repins = []
    for mrow in mem:
        for path, child in changes.plan_repins(mrow, mem).items():
            repins.append(f"{mrow['item_number']} {path} -> proposal "
                          f"#{child['id']} head {child['head_sha'][:10]}")
    out.update(change=changes.label(c["id"]), title=title, repins=repins)
    if submit:
        try:
            got = ops.submit_change(conn, c["id"], actor=actor,
                                    note="carried correction — please review")
            out["submitted"] = {k: got.get(k) for k in ("say", "state")
                                if k in got} or True
        except Exception as exc:                               # noqa: BLE001
            out["submit_refused"] = str(exc)[:600]
    out["state"] = changes.row(conn, c["id"])["state"]
    return out


def _next_numbers(rows: dict) -> dict:
    from . import db, ops
    out = {}
    with db.connect() as conn:
        for i in rows["items"]:
            parts = i["number"].split("-")
            if len(parts) == 3:
                k = (parts[0], parts[1])
                out[f"{k[0]}-{k[1]}"] = ops.next_number(conn, *k)
    return out


def verify(rows: dict, *, catalog: bool, serves: bool) -> dict:
    """The rebuild check on every carried design item, the catalog check when
    catalog items came along. Each in a rolled-back transaction."""
    import contextlib
    import io
    from . import gitindex, manifest
    if not serves:
        return {"skipped": "the target does not serve git for this project"}
    out = {"items": {}, "identical": True}
    for i in rows["items"]:
        if i["kind"] in manifest.CATALOG_KINDS:
            continue
        s: dict = {}
        gitindex._dry_run_check(i["number"], quiet=True, summary=s)
        out["items"][i["number"]] = {
            "refs": s.get("refs"), "pass1": s.get("pass1"),
            "pass1_not_a_name": s.get("pass1_not_a_name"),
            "pass2": s.get("pass2"), "errors": len(s.get("errors") or []),
            "ok": s.get("ok"), **({"examples": s["examples"]}
                                  if not s.get("ok") else {})}
        out["identical"] &= bool(s.get("ok"))
    if catalog:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gitindex._main_catalog(check=True, dry_run=True)
        out["catalog"] = {"rc": rc, "tail": buf.getvalue().strip().splitlines()[-3:]}
        out["identical"] &= rc == 0
    return out


# =============================================================================
# CLI
# =============================================================================

def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m plm.carry",
                                 description="Move one project between plm "
                                             "instances (stage 20).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="write a bundle (READ-ONLY on the source)")
    ex.add_argument("--project", required=True)
    ex.add_argument("--from", dest="src", required=True,
                    help="the source registry (opened mode=ro)")
    ex.add_argument("--vault", help="the source's DATA_ROOT (registry bytes)")
    ex.add_argument("--repos", help="the source's repos root (live repos)")
    ex.add_argument("--lfs", help="the source's LFS root")
    ex.add_argument("--git-mode", default="auto",
                    help="the project's mode ON THE SOURCE (auto: PLM_GIT_MODE "
                         "+ the source's policy rows)")
    ex.add_argument("--instance", default="",
                    help="what to call the source in the target's history")
    ex.add_argument("--out", required=True)
    ex.add_argument("--dry-run", action="store_true")
    im = sub.add_parser("import", help="bring a bundle into a registry")
    im.add_argument("--bundle", required=True)
    im.add_argument("--into", required=True, help="the target registry")
    im.add_argument("--repos", required=True, help="the target's repos root")
    im.add_argument("--lfs", required=True, help="the target's LFS root")
    im.add_argument("--vault", help="registry->registry: the target DATA_ROOT")
    im.add_argument("--mode", default="", choices=["", "off", "read", "write"],
                    help="set the project's git mode on the target")
    im.add_argument("--as-change", action="store_true")
    im.add_argument("--since", default="")
    im.add_argument("--no-submit", dest="submit", action="store_false",
                    help="with --as-change: leave the change a DRAFT instead "
                         "of submitting it for a human's review (the default)")
    im.add_argument("--stub-external", action="store_true",
                    help="create a referenced item the target lacks (number, "
                         "name, kind, project; no files) instead of refusing "
                         "— when nothing names a revision of it")
    im.add_argument("--actor", required=True)
    im.add_argument("--dry-run", action="store_true",
                    help="print the plan; write nothing")
    a = ap.parse_args(argv)

    if a.cmd == "export":
        mode = a.git_mode
        if mode == "auto":
            os.environ["PLM_DB"] = str(Path(a.src))
            from . import gitmode
            conn = _open_ro(Path(a.src))
            mode = gitmode.mode_for(a.project, conn)
            conn.close()
        try:
            plan = export(Path(a.src), a.project, Path(a.out),
                          vault_root=Path(a.vault) if a.vault else None,
                          repos=Path(a.repos) if a.repos else None,
                          lfs_root=Path(a.lfs) if a.lfs else None,
                          git_mode=mode, instance=a.instance,
                          dry_run=a.dry_run)
        except CarryError as exc:
            print(f"plm.carry export: REFUSED — {exc}", file=sys.stderr)
            return 3
        _print(plan)
        return 0 if not plan["files"]["not_found"] else 1

    # import: the target's paths go into the environment BEFORE plm's modules
    # read config (REPOS etc. are bound at import time, one home: plm/config.py)
    os.environ["PLM_DB"] = str(Path(a.into))
    os.environ["PLM_REPOS"] = str(Path(a.repos))
    os.environ["PLM_LFS_ROOT"] = str(Path(a.lfs))
    os.environ.setdefault("PLM_CHECKS", "relay")
    try:
        plan = import_(Path(a.bundle), actor=a.actor, as_change=a.as_change,
                       since=a.since, mode=a.mode, submit=a.submit,
                       dry_run=a.dry_run, stub_external=a.stub_external,
                       vault_dir=Path(a.vault) if a.vault else None)
    except CarryError as exc:
        print(f"plm.carry import: REFUSED — {exc}", file=sys.stderr)
        return 3
    _print(plan)
    if plan.get("problems"):
        print("plm.carry import: REFUSED (see problems) — nothing was written",
              file=sys.stderr)
        return 3
    if plan.get("dry_run"):
        return 0
    return 0 if plan["verify"].get("identical", True) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
