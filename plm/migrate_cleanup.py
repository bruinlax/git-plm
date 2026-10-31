""" as a scripted, rehearsable migration.

The registry has carried a handful of real data problems for a year: prose in an
INTEGER column, two lifecycle states the config has never heard of, a part number
with its class token twice, file rows whose bytes are gone. None of them stop
plm working, which is exactly why none of them ever got fixed.

The migration is the moment to fix them, and the moment they are most dangerous:
a hand-run `UPDATE` against the live registry on switchover night is how a switchover
becomes an incident. So every fix is a RULE in this module, each one separately
reportable and separately skippable, each one idempotent, and `--dry-run` prints
exactly what `--apply` would do.

  python -m plm.migrate_cleanup --dry-run
  python -m plm.migrate_cleanup --apply --actor geoff
  python -m plm.migrate_cleanup --dry-run --only find-numbers
  python -m plm.migrate_cleanup --apply --skip dupes --skip unrecoverable
  python -m plm.migrate_cleanup --dry-run --only filedrop-repoint --project <p>

WHAT THIS TOOL WILL NOT DO, and the reason each time:

- It will never `--apply` against production's database. The registry is
  migrated by copying it, cleaning the COPY and cutting over — never by editing
  the live one and hoping (see `_refuse_production`): the host's prod paths are
  refused outright, and the database the running environment SERVES (PLM_DB —
  `/var/plm/plm.sqlite3` inside the container) only with a typed
  `--i-know-this-is-production <basename>`.
- It never deletes a row. Rule 5 finds 18 file rows whose bytes are in neither
  the vault nor any recorded commit; the answer is a context entry saying so,
  because a row that records "there was a file here" is evidence, and deleting
  evidence to make a report green is the opposite of a migration.
- It never renumbers without `--fix-numbers`. An item's number is its identity
  and changing one is a decision, not a cleanup.
- Rule 2 changes no rows at all: `DESIGN` and `CONCEPT` are real maturities the
  policy already honours, so the fix is one line in `plm/config.py` (done) and a
  report line here that says the states are now declared.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from . import config, db, gitrepo, lfs, manifest

# Paths this tool refuses to `--apply` against, however it is invoked. Set
# PLM_PROD_DB_PATHS (colon-separated) to protect your live registry.
PROD_DB_PATHS = tuple(
    p for p in os.environ.get("PLM_PROD_DB_PATHS", "").split(":") if p)

# The find numbers a cleaned BOM gets. Tens, because that is what a drawing does
# — it leaves room to insert a line later without renumbering the sheet.
FIND_STEP = 10

def _in_project(project: str, alias: str = "i") -> tuple[str, tuple]:
    """` AND <alias>.project=?` and its argument — every rule's scope.

    Stage 12 (loose end 6): the readiness report used to run the GLOBAL dry run
    and keep the lines that happened to mention one of the project's item
    numbers. That miscounted both ways — a rule whose lines only name SOME of
    its changes (find-numbers lists the 75 prose rows, not the 107 empty ones)
    under-reported, and a rule that lists what it already fixed (dupes, after
    --apply) blocked a project forever. Each rule now selects by project
    itself and says, per item, what --apply would still change."""
    if not project:
        return "", ()
    return f" AND {alias}.project=?", (project,)


RULES = ("find-numbers", "states", "numbers", "fileless", "unrecoverable",
         "dupes", "relations", "source-file", "selector", "event-secrets",
         "instructions", "filedrop-repoint", "survey")


class CleanupError(RuntimeError):
    pass


def _same_file(a: Path, b: Path) -> bool:
    """Is `a` the file `b` is — by resolved path (symlinks followed), or by
    (device, inode) when both exist?"""
    ra, rb = a.resolve(), b.resolve()
    if str(ra) == str(rb):
        return True
    try:
        sa, sb = ra.stat(), rb.stat()
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _refuse_production(path: Path, *, configured: Path | None = None,
                       confirm: str = "") -> None:
    """`--apply` against the live registry is refused, from BOTH sides.

    1. From the HOST: by path, and by (device, inode) — so a prod database
       bind-mounted somewhere else under a friendly name is still recognised as
       itself. Not overridable.
    2. From INSIDE a service: the target is the database this process is
       CONFIGURED to serve (`config.DB_PATH` as the environment set it, before
       any `--db`), symlinks resolved. That is the check that fires in
       `docker exec plm python -m plm.migrate_cleanup --apply`, where prod's
       host path does not exist — the hole stage 9 found. It yields only to a
       typed confirmation: `--i-know-this-is-production <basename of that DB>`.

    `--dry-run` is never refused; it writes nothing.
    """
    real = path.resolve()
    ident = None
    try:
        st = real.stat()
        ident = (st.st_dev, st.st_ino)
    except OSError:
        pass
    for cand in PROD_DB_PATHS:
        c = Path(cand)
        if str(real) == str(c.resolve() if c.exists() else c):
            raise CleanupError(
                f"refusing --apply: {path} IS production's registry.\n"
                "  Migrate a COPY. `sqlite3 'file:…?mode=ro' \".backup out\"` "
                "makes one while plm is running.")
        try:
            cs = c.stat()
        except OSError:
            continue
        if ident and ident == (cs.st_dev, cs.st_ino):
            raise CleanupError(
                f"refusing --apply: {path} is the same file as {cand} "
                "(same device and inode),\n  reached under a different name. "
                "Migrate a COPY.")
    if configured is not None and _same_file(path, configured):
        want = configured.resolve().name
        if confirm != want:
            raise CleanupError(
                f"refusing --apply: {path} is the registry this environment "
                f"SERVES (PLM_DB → {configured.resolve()}).\n"
                "  Inside a service container that is production's database "
                "under another name.\n"
                "  Migrate a COPY: `--db <copy>` (an online `.backup` of it), "
                "then cut over.\n"
                "  If you really mean the live registry, say so: "
                f"--i-know-this-is-production {want}"
                + (f"\n  (you typed {confirm!r})" if confirm else ""))
        print(f"⚠️  --apply against the SERVED registry {configured.resolve()} "
              "— confirmed by name.", file=sys.stderr)


# -- rule 1: prose in bom.find_number -----------------------------------------

def _bom_rows(conn, rev_id: int) -> list:
    """This revision's BOM in the order the manifest emits it — integers first
    by value, then prose, then the unnumbered, each tie broken by child number
    and row id. Renumbering in a different order would reshuffle a BOM somebody
    has already read on paper."""
    return list(conn.execute(
        "SELECT b.*, c.number AS child_number FROM bom b "
        "JOIN items c ON c.id=b.child_item_id WHERE b.parent_rev_id=? "
        "ORDER BY CASE WHEN b.find_number IS NULL THEN 1 ELSE 0 END, "
        "b.find_number, c.number, b.id", (rev_id,)))


def rule_find_numbers(conn, *, apply: bool, actor: str,
                      project: str = "") -> dict:
    """Prose out of `find_number`, into `notes`; real find numbers 10, 20, 30…

    75 rows hold a DESCRIPTION where an integer belongs ('enclosure shell',
    'AKASO WT50 projector') and 107 hold nothing at all. SQLite's dynamic typing
    let both happen against an `INTEGER` column.

    The text is not thrown away — it is what somebody meant to say about the
    line, so it moves to `notes` with a `find: ` prefix that says where it came
    from. Then the whole BOM of any revision that had a bad row is numbered
    10, 20, 30…: a find number is only useful if it is unique within its parent,
    so half-numbering a sheet would be worse than not numbering it.

    Idempotent: a revision whose rows are all integers is not touched, and a
    note that already starts with the prefix is not prefixed twice.
    """
    lines: list[str] = []
    changed = 0
    prose = null = 0
    pending: dict[str, int] = defaultdict(int)
    prose_items: set[str] = set()
    scope, sargs = _in_project(project)
    revs = [r["id"] for r in conn.execute(
        "SELECT DISTINCT b.parent_rev_id AS id FROM bom b "
        "JOIN revisions r ON r.id=b.parent_rev_id JOIN items i ON i.id=r.item_id "
        "WHERE (b.find_number IS NULL OR typeof(b.find_number)!='integer')"
        + scope, sargs)]
    for rev_id in sorted(revs):
        rows = _bom_rows(conn, rev_id)
        meta = conn.execute(
            "SELECT i.number, r.version FROM revisions r JOIN items i "
            "ON i.id=r.item_id WHERE r.id=?", (rev_id,)).fetchone()
        if not meta:
            continue
        for n, b in enumerate(rows, start=1):
            find = b["find_number"]
            want = n * FIND_STEP
            note = b["notes"] or ""
            if find is None:
                null += 1
            elif not isinstance(find, int):
                prose += 1
                prose_items.add(meta["number"])
                text = str(find).strip()
                marker = f"find: {text}"
                if text and marker not in note:
                    note = f"{marker}\n{note}".strip() if note else marker
                lines.append(f"{meta['number']} v{meta['version']} "
                             f"-> {b['child_number']}: find={text!r} -> "
                             f"notes, find={want}")
            if find == want and note == (b["notes"] or ""):
                continue
            changed += 1
            pending[meta["number"]] += 1
            if apply:
                conn.execute("UPDATE bom SET find_number=?, notes=? WHERE id=?",
                             (want, note, b["id"]))
    nulls = defaultdict(int)
    for rev_id in revs:
        for b in _bom_rows(conn, rev_id):
            if b["find_number"] is None:
                nulls[rev_id] += 1
    for rev_id, n in sorted(nulls.items()):
        meta = conn.execute(
            "SELECT i.number, r.version FROM revisions r JOIN items i "
            "ON i.id=r.item_id WHERE r.id=?", (rev_id,)).fetchone()
        lines.append(f"{meta['number']} v{meta['version']}: {n} line(s) with "
                     "no find number")
    return {"name": "find-numbers", "title": "prose in bom.find_number",
            "found": prose + null, "changed": changed,
            "pending": dict(sorted(pending.items())),
            # which of those items hold PROSE (a wrong fact in the tree), as
            # against BOMs that only lack find numbers (an absent one)
            "prose_items": sorted(prose_items),
            "detail": f"{prose} prose, {null} missing, across "
                      f"{len(revs)} parent revision(s)",
            "action": "text -> notes ('find: …'), find numbers 10/20/30… in "
                      "BOM order for the whole revision",
            "lines": lines}


# -- rule 2: DESIGN / CONCEPT -------------------------------------------------

def rule_states(conn, *, apply: bool, actor: str, project: str = "") -> dict:
    """The two states the config never declared.

    26 revisions (25 DESIGN, 1 CONCEPT) are in states `config.STATES` does not
    list, and the rest of the system already treats them as real maturities —
    `manifest.MATURITY_BY_STATE` maps them, `split` tags them, the policy has a
    row for each. So the registry was right and the config was lying.

    THE FIX IS A CODE CHANGE, not a row change: the states are declared in
    `plm/config.py` now. This rule exists to report the population and to FAIL
    if the declaration is ever reverted while the rows are still there.
    """
    scope, sargs = _in_project(project)
    rows = [dict(r) for r in conn.execute(
        "SELECT r.state, COUNT(*) n FROM revisions r JOIN items i "
        "ON i.id=r.item_id WHERE 1=1" + scope +
        " GROUP BY r.state ORDER BY n DESC", sargs)]
    undeclared = [r for r in rows if r["state"] not in config.STATES]
    lines = [f"{r['state']}: {r['n']} revision(s)" for r in rows
             if r["state"] in ("DESIGN", "CONCEPT")]
    for r in undeclared:
        lines.append(f"⚠ {r['state']}: {r['n']} revision(s) and NOT in "
                     "config.STATES — declare it there")
    return {"name": "states", "title": "DESIGN / CONCEPT lifecycle states",
            "found": sum(r["n"] for r in rows
                         if r["state"] in ("DESIGN", "CONCEPT")),
            "changed": 0,
            "detail": ("declared in config.STATES" if not undeclared
                       else f"{len(undeclared)} state(s) still undeclared"),
            "action": "code change only (plm/config.py) — no row is touched",
            "lines": lines, "failed": bool(undeclared), "pending": {}}


# -- rule 3: malformed and revision-less items --------------------------------

def rule_numbers(conn, *, apply: bool, actor: str,
                 fix_numbers: bool = False, new_number: str = "",
                 project: str = "") -> dict:
    """`KML-PRT-PRT-0001` (the class token twice) and the revision-less items.

    Five items have no revisions at all, so `split` gives each an empty repo.
    One of them also carries a malformed number. The number is the item's
    identity, so this rule PROPOSES and only acts under `--fix-numbers`;
    `ops.renumber_item` does the work, which keeps the old number resolving
    (`items.former_numbers`), renames the vault folder in a commit and repoints
    every `files.path` under it.

    The other four are a judgement call this tool does not get to make — delete,
    or seed a v0.1.0 DRAFT — so they are reported with the recommendation.
    """
    from . import ops
    lines: list[str] = []
    changed = 0
    pending: dict[str, int] = {}
    scope, sargs = _in_project(project)
    empties = [dict(r) for r in conn.execute(
        "SELECT id, number, kind, name, folder FROM items i WHERE NOT EXISTS "
        "(SELECT 1 FROM revisions r WHERE r.item_id=i.id)" + scope +
        " ORDER BY number", sargs)]
    for it in empties:
        pending[it["number"]] = 1       # a decision is pending on each
        bits = it["number"].split("-")
        malformed = len(bits) > 3 or (len(bits) == 4)
        if malformed:
            klass = config.CLASS_BY_KIND.get(it["kind"], "")
            nxt = new_number or (ops.next_number(conn, bits[0], klass)
                                 if klass else "?")
            lines.append(f"{it['number']} — MALFORMED (class token twice); "
                         f"propose {nxt}"
                         + ("" if fix_numbers else "  [--fix-numbers to apply]"))
            if not new_number:
                # ⚠️ `ops.next_number` is the global max + 1 within the class,
                # and this registry uses NUMBER BLOCKS that mean something:
                # KML-PRT-1xxx is the drive family, 2xxx the rotating family,
                # 9xxx fixtures and measured references. So the auto answer
                # lands in 9xxx for a part that is neither. A number is
                # identity; let a person pick it.
                lines.append(
                    "    ⚠ that is the global max + 1 in the class, which in "
                    "this registry lands in the\n      9xxx (fixture / "
                    "reference) block. Pass --new-number to choose "
                    "deliberately.")
            if apply and fix_numbers and klass:
                res = ops.renumber_item(
                    conn, it["number"], new_number=new_number,
                    actor=actor or "migration",
                    note=" migration: the class token appeared twice "
                         "in this number.")
                changed += 1
                lines.append(f"    renumbered -> {res.get('number', nxt)}")
        else:
            lines.append(
                f"{it['number']} ({it['kind']}) — no revisions; recommend "
                + ("delete: a coupon with no revision verifies nothing"
                   if it["kind"] == "coupon"
                   else "seed v0.1.0 DRAFT from its vault folder"
                        if it["folder"] else "delete: no folder, no revisions")
                + "  [not acted on]")
    return {"name": "numbers", "title": "malformed / revision-less items",
            "found": len(empties), "changed": changed,
            "detail": f"{len(empties)} item(s) with no revisions; "
                      f"{sum(1 for i in empties if len(i['number'].split('-')) > 3)}"
                      " with a malformed number",
            "action": ("renumber the malformed one (ops.renumber_item)"
                       if fix_numbers else
                       "report only — pass --fix-numbers to renumber"),
            "lines": lines, "pending": pending}


# -- rule 4: revisions with no files ------------------------------------------

def rule_fileless(conn, *, apply: bool, actor: str, project: str = "") -> dict:
    """194 revisions carry no file at all.

    They become a `plm.toml` + `README.md` commit — which is a legitimate
    revision (a BOM change, a state change, an attribute correction) and for
    several items it is the v0.1.0. There is nothing to fix and nothing to
    guess, so this rule REPORTS and never acts: the grouping by item and state
    is what tells you whether it is normal (a v0.1.0 seeded from a BOM) or a
    check-in that silently lost its files.
    """
    scope, sargs = _in_project(project)
    rows = [dict(r) for r in conn.execute(
        "SELECT i.number, r.version, r.state FROM revisions r "
        "JOIN items i ON i.id=r.item_id WHERE NOT EXISTS "
        "(SELECT 1 FROM files f WHERE f.revision_id=r.id)" + scope +
        " ORDER BY i.number, r.vmaj, r.vmin, r.vpat", sargs)]
    by_state: dict = defaultdict(int)
    by_item: dict = defaultdict(list)
    for r in rows:
        by_state[r["state"]] += 1
        by_item[r["number"]].append(f"v{r['version']} {r['state']}")
    lines = [f"{k}: {v}" for k, v in sorted(by_state.items(),
                                            key=lambda kv: -kv[1])]
    lines += [f"{n}: {', '.join(v)}" for n, v in sorted(by_item.items())]
    return {"name": "fileless", "title": "revisions with no files",
            "found": len(rows), "changed": 0,
            "detail": f"{len(rows)} revision(s) across {len(by_item)} item(s)",
            "action": "report only — a file-less revision is a legal revision",
            "lines": lines, "pending": {}}


# -- rule 5: file rows whose bytes are gone -----------------------------------

_UNRECOVERABLE_NOTE = "file row had no recoverable bytes at migration"


def rule_unrecoverable(conn, *, apply: bool, actor: str,
                       project: str = "") -> dict:
    """File rows whose bytes are in neither the vault nor any recorded commit.

    The path is gone from the working tree and the revision has no
    `git_commit` to `git show` it out of, so `split` cannot put anything in the
    tree for them and says so under `files_not_recoverable`.

    THE ROW IS KEPT. It is the only remaining evidence that a file was ever
    there, and a migration that deletes evidence to make its own report clean is
    not a migration. `--apply` writes one context entry per revision naming the
    paths, so the fact travels with the part instead of living in a log file
    nobody will find in a year.
    """
    from . import context as ctx
    from .split import Vault
    vault = Vault(config.DATA_ROOT)
    missing: dict[tuple, list] = defaultdict(list)
    scope, sargs = _in_project(project)
    for f in conn.execute(
            "SELECT f.id, f.path, f.backend, f.key, r.id AS rev_id, "
            "r.git_commit, "
            "r.version, i.number FROM files f "
            "JOIN revisions r ON r.id=f.revision_id "
            "JOIN items i ON i.id=r.item_id WHERE 1=1" + scope +
            " ORDER BY i.number, f.path", sargs):
        if manifest.is_external(f):
            continue
        if Path(f["path"]).name in manifest.DROP_FROM_TREE:
            continue
        if f["backend"] == "lfs":
            # bytes in plm's LFS store by oid (filedrop-repoint, gitindex) —
            # the vault never had them, so asking it would be a false alarm
            if not lfs.has(str(f["key"] or "").lower()):
                missing[(f["number"], f["version"], f["rev_id"])].append(
                    f["path"])
            continue
        data, p = vault.read(f["path"], f["git_commit"] or "", quiet=True)
        if p is None and data is None:
            missing[(f["number"], f["version"], f["rev_id"])].append(f["path"])
    lines = []
    changed = 0
    pending: dict[str, int] = defaultdict(int)
    for (number, version, rev_id), paths in sorted(missing.items()):
        pending[number] += len(paths)   # the bytes stay gone after --apply
        for p in paths:
            lines.append(f"{number} v{version} {p}")
        body = (f"{_UNRECOVERABLE_NOTE}: "
                + ", ".join(sorted(paths))
                + ". The row is retained for the record; the bytes are in "
                  "neither the vault working tree nor the revision's recorded "
                  "commit. (git-first migration.)")
        already = conn.execute(
            "SELECT 1 FROM context WHERE revision_id=? AND body LIKE ?",
            (rev_id, f"%{_UNRECOVERABLE_NOTE}%")).fetchone()
        if already:
            continue
        changed += 1
        if apply:
            ctx.add(conn, body, target_type="revision", target=rev_id,
                    actor=actor or "migration", event=False)
    if not missing:
        lines.append(
            "none — every file row resolves. The POC measured 18 here in "
            "stage 1 against a vault clone that predated the checkpoint "
            "commit; committing the vault is what recovered them, which is "
            "why 'the vault is clean and committed' is a PRECONDITION of the "
            "switchover and not a tidy-up afterwards.")
    return {"name": "unrecoverable", "title": "file rows with no bytes",
            "found": sum(len(v) for v in missing.values()), "changed": changed,
            "detail": f"{sum(len(v) for v in missing.values())} row(s) across "
                      f"{len(missing)} revision(s)",
            "action": "one context entry per revision; the row is NEVER deleted",
            "lines": lines, "pending": dict(sorted(pending.items()))}


# -- rule 6: two copies of one mesh in one tree -------------------------------

def rule_dupes(conn, *, apply: bool, actor: str, project: str = "") -> dict:
    """A stale copy carried forward beside the new one, under the old name.

    `GSI-ASM-0002 v0.3.0` holds both `…_v0.2.0_sensor_node_assembled.stl` and
    `…_v0.3.0_sensor_node_assembled.stl`; `GSI-ASM-0003` does the same. Stage 5
    found them from the other end (the two `pairs_ambiguous` in a 229-compare
    census) and §6c-1's normaliser finds them as filename collisions, because
    they are the same fact seen twice.

    Both sets of bytes stay. The OLDER copy — the one whose own name carries the
    older version token — is re-roled `archive`, which takes it out of the
    `viz > mesh > artifact` primary race and out of the pairing pool, so the 3-D
    diff stops offering a two-revisions-stale mesh as this revision's geometry.

    Found by RULE, not by item number: any revision holding two in-tree files
    that normalise to one name is a hit.
    """
    from .split import _stamp_rank
    lines = []
    changed = 0
    pending: dict[str, int] = defaultdict(int)
    per_rev: dict[tuple, list] = defaultdict(list)
    scope, sargs = _in_project(project)
    for f in conn.execute(
            "SELECT f.id, f.path, f.role, f.backend, r.id AS rev_id, "
            "r.version, i.number "
            "FROM files f JOIN revisions r ON r.id=f.revision_id "
            "JOIN items i ON i.id=r.item_id WHERE 1=1" + scope +
            " ORDER BY i.number, f.path", sargs):
        if manifest.is_external(f):
            continue
        tp = manifest.tree_path(f["path"])
        if Path(tp).name in manifest.DROP_FROM_TREE:
            continue
        parent = str(Path(tp).parent)
        d = "" if parent == "." else parent + "/"
        key = d + gitrepo.stable_name(Path(tp).name, f["number"])
        per_rev[(f["number"], f["version"], f["rev_id"], key)].append(dict(f))
    for (number, version, rev_id, key), rows in sorted(per_rev.items()):
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (_stamp_rank(Path(r["path"]).name), r["path"]))
        keep, stale = rows[0], rows[1:]
        lines.append(f"{number} v{version} -> {key}: keep "
                     f"{Path(keep['path']).name} ({keep['role']})")
        for s in stale:
            lines.append(f"    archive {Path(s['path']).name} "
                         f"(was {s['role']})")
            if s["role"] == "archive":
                continue
            changed += 1
            pending[number] += 1
            if apply:
                conn.execute("UPDATE files SET role='archive', "
                             "role_source='declared' WHERE id=?", (s["id"],))
    return {"name": "dupes", "title": "stale duplicate files in one tree",
            "found": sum(1 for v in per_rev.values() if len(v) > 1),
            "changed": changed,
            "detail": f"{sum(len(v) - 1 for v in per_rev.values() if len(v) > 1)}"
                      " stale copy/copies",
            "action": "re-role the older copy `archive` — no bytes are dropped",
            "lines": lines, "pending": dict(sorted(pending.items()))}


# -- rule 7: everything else in, reported ----------------------------

def _old_home(row: dict) -> str:
    """Where the pre-stage-10 code wrote a relation (manifest._relations +
    itemref.build, as of 6b23e8e)."""
    if row["from_version"]:
        return f"plm.toml of v{row['from_version']}"
    return "plm.toml of EVERY revision (scope=\"item\") + item.toml"


def rule_relations(conn, *, apply: bool, actor: str,
                   project: str = "") -> dict:
    """Stage 10: every relation's old home -> its new one (relations.HOMES).

    Before stage 10 the HOME of a link was a field (`scope = "item"`) and an
    item-level link was written twice — into item.toml AND into every
    revision's plm.toml. Now the registry's `home` decides, and each claim is
    written once. This rule says, row by row, where each one was and where it
    goes, and `--apply` writes down the two things the new shape needs that the
    rows do not yet say:

      · an INFERRED claimant (relations.claimant): a revision-home row with no
        from_version gets the revision the inference names — the item's first
        revision created at or after the link, which on prod is always the
        check-in that followed `add_coupon` by a few minutes. Written to
        `from_version` AND `revision_id`, so it is `declared` from then on.
      · an ITEM-home row that carries a from_version (a fork's derived_from):
        an item-level claim has no claimant revision, so the version is dropped
        (reported line by line — it is the only value this rule removes).

    Rows whose claimant has no revision at all stay registry rows and are
    listed: no tree exists to declare them in. Nothing is deleted.
    """
    from . import relations as relmod
    lines: list[str] = []
    found = changed = 0
    counts: dict[str, int] = {}
    pending: dict[str, int] = defaultdict(int)
    sql = ("SELECT rel.*, f.number AS from_number, f.project AS project, "
           "t.number AS to_number FROM relations rel "
           "JOIN items f ON f.id=rel.item_id "
           "JOIN items t ON t.id=rel.target_item_id ")
    args: tuple = ()
    if project:
        sql += "WHERE f.project=? "
        args = (project,)
    # A registry from before the git-first schema (a prod snapshot taken while
    # prod still runs `master`) has no `relations.revision_id`. `--apply` runs
    # `db.init()` first, which adds it and backfills it from `from_version`
    # EXACTLY — so a DECLARED row's revision_id is, after init, the claimant's.
    # The dry run reads it that way instead of crashing on the missing column
    # (found by the stage-12 rehearsal: KeyError on today's prod snapshot).
    pre_schema = not any(c_[1] == "revision_id" for c_ in
                         conn.execute("PRAGMA table_info(relations)"))
    for r in conn.execute(sql + "ORDER BY f.number, rel.relation, rel.id", args):
        row = dict(r)
        found += 1
        h = relmod.home(row["relation"])
        c = relmod.claimant(conn, row)
        if pre_schema:
            row["revision_id"] = (c["revision_id"] if c["how"] == "declared"
                                  and h != "item" else None)
        old = _old_home(row)
        if h == "item":
            new = "item.toml on refs/plm/item"
        elif not c["revision_id"]:
            new = "NO HOME — the claimant has no revision (stays a registry row)"
        elif h == "note":
            new = f"refs/notes/plm note on v{c['version']}"
        else:
            new = f"plm.toml of v{c['version']}"
        verdict = {"item": "item", "declared": "declared",
                   "inferred": "INFERRED", "none": "homeless"}[c["how"]]
        counts[f"{h}:{verdict}"] = counts.get(f"{h}:{verdict}", 0) + 1
        fix = ""
        before = changed
        if h != "item" and c["how"] == "inferred":
            fix = f" -> write from_version={c['version']}"
            changed += 1
            if apply:
                conn.execute("UPDATE relations SET from_version=?, "
                             "revision_id=? WHERE id=?",
                             (c["version"], c["revision_id"], row["id"]))
        elif h != "item" and c["how"] == "declared" \
                and row["revision_id"] != c["revision_id"]:
            fix = " -> write revision_id"
            changed += 1
            if apply:
                conn.execute("UPDATE relations SET revision_id=? WHERE id=?",
                             (c["revision_id"], row["id"]))
        elif h == "item" and (row["from_version"] or row["revision_id"]):
            fix = (f" -> drop from_version={row['from_version']!r} (an "
                   "item-level claim has no claimant revision)")
            changed += 1
            if apply:
                conn.execute("UPDATE relations SET from_version='', "
                             "revision_id=NULL WHERE id=?", (row["id"],))
        if changed != before:
            pending[row["from_number"]] += changed - before
        lines.append(
            f"{row['from_number']} {row['relation']} {row['to_number']}"
            f"{' v' + row['target_version'] if row['target_version'] else ''}"
            f" [{verdict}]: {old} -> {new}{fix}")
    # LIVE repos are never regenerated, so the new shape reaches them by a
    # NORMAL COMMIT or not at all. The one that matters: an item-home claim
    # the registry has and the item's item.toml does not (before stage 10 a
    # revision-level derived_from lived only in a draft's plm.toml, which the
    # indexer now ignores for an item-home type). One commit on refs/plm/item
    # per such item. Everything else old-shaped in a live repo is in a FROZEN
    # tree and is read tolerantly (gitindex._index_relations) — never rewritten.
    from . import gitwrite, itemref
    live_fixes = 0
    for it in conn.execute(
            "SELECT DISTINCT i.* FROM items i JOIN relations r "
            "ON r.item_id=i.id" + (" WHERE i.project=?" if project else ""),
            (project,) if project else ()).fetchall():
        try:
            repo = gitwrite.repo_path(it["number"])
        except Exception:  # noqa: BLE001 — no repo: nothing live to migrate
            continue
        if not (repo / "HEAD").exists() or not gitwrite.is_live(repo):
            continue
        want = sorted((x["type"], x["item"], x.get("version", ""))
                      for x in itemref.build(conn, it["id"])["relations"])
        have = sorted((x["type"], x["item"], str(x.get("version") or ""))
                      for x in itemref.read(repo).get("relations") or []
                      if relmod.home(str(x.get("type") or "")) == "item")
        if want == have:
            continue
        live_fixes += 1
        changed += 1
        pending[it["number"]] += 1
        lines.append(
            f"{it['number']} [LIVE repo]: item.toml has {len(have)} item-home "
            f"claim(s), the registry {len(want)} -> one commit on "
            "refs/plm/item (a normal commit; no tree is rewritten)")
        if apply:
            itemref.commit_fields(
                conn, it, {}, actor=actor or "migration",
                message=f"{it['number']}: item-level relations move to "
                        "item.toml\n\nStage 10 (relations by home): an "
                        "item-level claim lives here, not in a revision's "
                        "plm.toml.\n")
    if live_fixes:
        counts["live:item-ref-commit"] = live_fixes
    return {"name": "relations",
            "title": "relations by home (stage 10): old home -> new home",
            "found": found, "changed": changed,
            "detail": ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
            "action": "write inferred claimants down (from_version + "
                      "revision_id); drop the claimant version of item-level "
                      "claims. Report-only for everything else.",
            "lines": lines, "counts": counts,
            "pending": dict(sorted(pending.items()))}


# -- rule 8: `[source] file` names a file the revision does not carry ---------

_SOURCE_NOTE = "[source] file corrected at migration"


def source_mismatches(conn, project: str = "") -> list[dict]:
    """Revisions whose `revisions.source` (→ `[source] file`) is not among the
    files the revision carries with role `source` (→ `[files].source`).

    Stage 15: KML-PRT-1012 v0.4.1 says `[source] file =
    "lantern_gobo_bearing.scad"` while its only source file is
    `thin_carrier_round_enclosed_flange_inserts.scad` — a carried-forward name
    from v0.3.0. A clone then says "regenerate me from a file I do not have",
    and the render-matches-source check renders the wrong thing. ONE predicate,
    asked by this rule, by the readiness report (through it) and mirrored by
    the `source-present` check on a commit."""
    scope, sargs = _in_project(project)
    out = []
    for r in conn.execute(
            "SELECT r.id, r.version, r.source, i.number FROM revisions r "
            "JOIN items i ON i.id=r.item_id WHERE COALESCE(r.source,'')<>''"
            + scope + " ORDER BY i.number, r.vmaj, r.vmin, r.vpat", sargs):
        srcs = [manifest.tree_path(f["path"]) for f in conn.execute(
            "SELECT path FROM files WHERE revision_id=? AND role='source' "
            "ORDER BY path", (r["id"],))]
        if r["source"] in srcs or not srcs:
            # (no source files at all: nothing to correct it TO — the
            # source-present check reports that case on the commit)
            continue
        # The answer is UNAMBIGUOUS when exactly one declared file is the one
        # the value names — `cad/x.scad` for a file that lands at `x.scad`,
        # `x.scad (include y.scad)` — or when only one source file exists.
        named = Path(r["source"].split(" (", 1)[0].strip()).name
        hits = [p for p in srcs if Path(p).name == named]
        fix = (hits[0] if len(hits) == 1 else
               srcs[0] if len(srcs) == 1 else "")
        out.append({"rev_id": r["id"], "number": r["number"],
                    "version": r["version"], "source": r["source"],
                    "files": srcs, "fix": fix})
    return out


def rule_source_file(conn, *, apply: bool, actor: str,
                     project: str = "") -> dict:
    """`[source] file` must name a source file the revision carries.

    When the answer is unambiguous — exactly one declared source file has the
    name the value gives (`cad/x.scad` for a file at `x.scad`, `x.scad
    (include y.scad)`), or only one source file exists — `--apply` writes it
    (with a context entry naming the old value, so the correction is on the
    record). Anything else is a decision — reported, never guessed."""
    from . import context as ctx
    rows = source_mismatches(conn, project)
    lines, changed = [], 0
    pending: dict[str, int] = defaultdict(int)
    for m in rows:
        if m["fix"]:
            lines.append(f"{m['number']} v{m['version']}: [source] file "
                         f"{m['source']!r} -> {m['fix']!r} (the source file "
                         "it names / the revision's only one)")
            changed += 1
            if apply:
                conn.execute("UPDATE revisions SET source=? WHERE id=?",
                             (m["fix"], m["rev_id"]))
                ctx.add(conn, f"{_SOURCE_NOTE}: it named "
                              f"`{m['source']}`, a path this revision does not "
                              f"carry; the source file it means is `{m['fix']}` "
                              "(the one it names, or the only one). "
                              "",
                        target_type="revision", target=m["rev_id"],
                        actor=actor or "migration", event=False)
            else:
                pending[m["number"]] += 1
        else:
            lines.append(f"{m['number']} v{m['version']}: [source] file "
                         f"{m['source']!r} is none of {m['files']} — decide "
                         "which one regenerates it")
            pending[m["number"]] += 1
    return {"name": "source-file",
            "title": "[source] file names a file the revision does not carry",
            "found": len(rows), "changed": changed,
            "detail": f"{len(rows)} revision(s); "
                      f"{sum(1 for m in rows if m['fix'])} unambiguous "
                      "(fixable)",
            "action": "unambiguous (the file it names, or the only one): "
                      "write it (+ a context entry); otherwise report only",
            "lines": lines, "pending": dict(sorted(pending.items()))}


# -- rule 10: a `[source] selector` in an annotated form ----------------------

_SELECTOR_NOTE = "[source] selector normalised at migration"


def selector_findings(conn, project: str = "") -> list[dict]:
    """Revisions whose `selector` is not the canonical `-D` form but reads as
    one (`checks.selector_recipe`, the ONE parser — the check that re-renders
    it asks the same function).

    Stage 16: render-matches-source SKIPPED all
    three swd-probe items because their selector was
    `part=probe_base (wrapper swd_probe_base.scad); viz part=probe_base_posed`;
    run 3 rewrote them by hand. `fix` is the canonical line when the reading is
    LOSSLESS (only bracketed notes, a `(wrapper …)` note and the other mesh's
    recipe were set aside); when a clause of prose was dropped the rule
    reports and does not write (`supports OFF` may be a fact somebody needs)."""
    from . import checks
    scope, sargs = _in_project(project)
    out = []
    for r in conn.execute(
            "SELECT r.id, r.version, r.selector, i.number FROM revisions r "
            "JOIN items i ON i.id=r.item_id WHERE COALESCE(r.selector,'')<>''"
            + scope + " ORDER BY i.number, r.vmaj, r.vmin, r.vpat", sargs):
        viz = conn.execute(
            "SELECT path FROM files WHERE revision_id=? AND role='viz'",
            (r["id"],)).fetchall()
        is_viz = any(Path(v["path"]).suffix.lower() in manifest.MESH_EXT
                     for v in viz)
        rec = checks.selector_recipe(r["selector"], primary_is_viz=is_viz)
        if rec["form"] != "annotated":
            continue
        out.append({"rev_id": r["id"], "number": r["number"],
                    "version": r["version"], "selector": r["selector"],
                    "canonical": rec["canonical"], "dropped": rec["dropped"],
                    "fix": None if rec.get("lossy") else rec["canonical"]})
    return out


def rule_selector(conn, *, apply: bool, actor: str, project: str = "") -> dict:
    """`[source] selector` in the form render-matches-source runs. Lossless
    readings are written (+ a context entry quoting the old value, so the notes
    stay on the record); lossy ones are reported for a human."""
    from . import context as ctx
    rows = selector_findings(conn, project)
    lines, changed = [], 0
    pending: dict[str, int] = defaultdict(int)
    for m in rows:
        if m["fix"] is not None:
            lines.append(f"{m['number']} v{m['version']}: selector "
                         f"{m['selector'][:70]!r} -> {m['fix']!r}")
            changed += 1
            if apply:
                conn.execute("UPDATE revisions SET selector=? WHERE id=?",
                             (m["fix"], m["rev_id"]))
                ctx.add(conn, f"{_SELECTOR_NOTE}: it read `{m['selector']}`; "
                              f"plm re-renders the primary mesh with "
                              f"`{m['fix'] or '(no parameters)'}` (set aside: "
                              + ", ".join(f"`{d}`" for d in m["dropped"])
                              + ").",
                        target_type="revision", target=m["rev_id"],
                        actor=actor or "migration", event=False)
            else:
                pending[m["number"]] += 1
        else:
            lines.append(f"{m['number']} v{m['version']}: selector "
                         f"{m['selector'][:70]!r} reads as {m['canonical']!r} "
                         "only by dropping "
                         + ", ".join(repr(d) for d in m["dropped"])
                         + " — decide")
            pending[m["number"]] += 1
    return {"name": "selector",
            "title": "[source] selector in an annotated form",
            "found": len(rows), "changed": changed,
            "detail": f"{len(rows)} revision(s); "
                      f"{sum(1 for m in rows if m['fix'] is not None)} "
                      "lossless (fixable)",
            "action": "lossless (notes, wrapper, the other mesh's recipe set "
                      "aside): write the canonical `-D` line (+ a context "
                      "entry); a dropped prose clause: report only",
            "lines": lines, "pending": dict(sorted(pending.items()))}


# -- rule 11: standing instructions that contradict a `write` project ---------

def instruction_findings(conn, project: str = "") -> list[dict]:
    """The newest revision of each item whose INSTRUCTIONS.md describes the
    MCP write loop (plm/workflow_text.py) — in a project that is, or is about
    to be, `write`. Read through the storage layer (vault or repo, whichever
    holds the bytes), so it works on a migration copy before any split."""
    from . import gitmode, storage, workflow_text
    scope, sargs = _in_project(project)
    out = []
    for it in conn.execute(
            "SELECT i.id, i.number, i.project FROM items i WHERE 1=1" + scope
            + " ORDER BY i.number", sargs):
        rev = conn.execute(
            "SELECT id, version, state FROM revisions WHERE item_id=? "
            "ORDER BY vmaj DESC, vmin DESC, vpat DESC, id DESC LIMIT 1",
            (it["id"],)).fetchone()
        if not rev:
            continue
        f = next((dict(r) for r in conn.execute(
            "SELECT * FROM files WHERE revision_id=?", (rev["id"],))
            if r["path"].rsplit("/", 1)[-1] in workflow_text.NAMES), None)
        if not f:
            continue
        text = ""
        try:
            path = storage.local_copy(f)
            text = path.read_text(errors="replace") if path.is_file() else ""
        except Exception:                                      # noqa: BLE001
            text = ""
        if not text:
            # a git-first item: the revision's commit holds the file
            gc = conn.execute("SELECT git_commit FROM revisions WHERE id=?",
                              (rev["id"],)).fetchone()[0] or ""
            try:
                repo = gitrepo.repo_path(it["number"])
                text = gitrepo.git(repo, "show", f"{gc}:{f['path'].rsplit('/', 1)[-1]}",
                                   check=False) if gc else ""
            except Exception:                                  # noqa: BLE001
                text = ""
        lines = workflow_text.stale_lines(text)
        if not lines:
            continue
        new, gen = workflow_text.rewrite(text)
        out.append({"item_id": it["id"], "number": it["number"],
                    "project": it["project"] or "",
                    "mode": gitmode.mode_for(it["project"] or "", conn),
                    "rev_id": rev["id"], "version": rev["version"],
                    "state": rev["state"], "name": f["path"].rsplit("/", 1)[-1],
                    "lines": lines, "generated": gen,
                    "rewritten": new if gen else ""})
    return out


def rule_instructions(conn, *, apply: bool, actor: str,
                      project: str = "") -> dict:
    """INSTRUCTIONS.md that tells an agent to checkout / revise / iterate on
    the DRAFT (stage 19, handoff finding 5). The migration skill's GENERATED
    two-line block is rewritten to the git-first paragraph — on an open DRAFT
    only (a frozen revision's file is history), through `ops.upload_files`
    (the one write path: a commit on the draft branch in `write`, the vault
    otherwise), and never on a member of an open CHANGE (its author rewrites
    it in that change). Hand-written stale lines are reported, not edited."""
    from . import ops, staging
    rows = instruction_findings(conn, project)
    lines, changed = [], 0
    pending: dict[str, int] = defaultdict(int)
    for m in rows:
        what = "; ".join(f"line {x['line']}: {x['why']}" for x in m["lines"][:3])
        in_change = conn.execute(
            "SELECT c.id FROM proposals p JOIN changes c ON c.id=p.change_id "
            "WHERE p.revision_id=? AND c.state IN ('draft','open','rejected') "
            "LIMIT 1", (m["rev_id"],)).fetchone() if _has_table(
                conn, "changes") else None
        if m["generated"] and m["state"] == "DRAFT" and not in_change:
            lines.append(f"{m['number']} v{m['version']}: {m['name']} — the "
                         f"generated checkout/revise block -> the git-first "
                         f"paragraph ({what})")
            changed += 1
            if apply:
                payload = staging.normalize(
                    conn, [{"name": m["name"], "text": m["rewritten"],
                            "role": "doc"}], layer="cleanup",
                    actor=actor or "migration")
                ops.upload_files(conn, m["number"], payload,
                                 version=m["version"],
                                 actor=actor or "migration",
                                 message="INSTRUCTIONS.md: the write loop is "
                                         "git here (migrate_cleanup "
                                         "instructions, stage 19)")
            else:
                pending[m["number"]] += 1
        else:
            why = ("hand-written — a person rewrites it" if not m["generated"]
                   else f"in open CHG-{in_change[0]} — rewrite it in that change"
                   if in_change else
                   f"v{m['version']} is {m['state']} — rewrite it in the next "
                   "change")
            lines.append(f"{m['number']} v{m['version']}: {m['name']} "
                         f"({what}) — {why}")
            pending[m["number"]] += 1
    return {"name": "instructions",
            "title": "INSTRUCTIONS.md describes the checkout/revise loop",
            "found": len(rows), "changed": changed,
            "detail": f"{len(rows)} item(s); "
                      f"{sum(1 for m in rows if m['generated'])} carry the "
                      "generated block",
            "action": "the generated block on an open DRAFT not in a change: "
                      "rewrite it (a commit on the draft); otherwise report",
            "lines": lines, "pending": dict(sorted(pending.items()))}


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name=?", (name,)).fetchone())


# -- rule 9: a credential in the event log ------------------------------------

def rule_event_secrets(conn, *, apply: bool, actor: str,
                       project: str = "") -> dict:
    """Event rows whose `data` carries a live credential in a URL.

    Security review C1/M4: master's integration dispatcher logged the FULL URL,
    api_key included, into `events` (served by `/api/events`). git-first stops
    writing it (`actions.scrub` on everything stored), but the rows production
    already has come along with every snapshot — stage 15's reseed brought 17
    back into the POC. `--apply` rewrites `data` through the same
    `actions.scrub` (`api_key=<redacted>`); the row, its time and its meaning
    stay. Instance-wide (these rows name an integration, not an item)."""
    from . import actions
    if project:
        return {"name": "event-secrets", "title": "credentials in the event log",
                "found": 0, "changed": 0, "detail": "instance-wide — run "
                "without --project", "action": "", "lines": [], "pending": {}}
    rows = [dict(r) for r in conn.execute(
        "SELECT id, type, data FROM events WHERE data LIKE '%api_key=%' "
        "AND data NOT LIKE '%api_key=<redacted>%'")]
    changed = 0
    for r in rows:
        new = actions.scrub(r["data"])
        if new != r["data"]:
            changed += 1
            if apply:
                conn.execute("UPDATE events SET data=? WHERE id=?",
                             (new, r["id"]))
    return {"name": "event-secrets", "title": "credentials in the event log",
            "found": len(rows), "changed": changed,
            "detail": f"{len(rows)} event row(s) carry a live `api_key=` "
                      "(security review C1/M4)",
            "action": "rewrite `data` through actions.scrub — the row stays",
            "lines": [f"event #{r['id']} {r['type']}" for r in rows],
            "pending": {}}


def rule_survey(conn, *, apply: bool, actor: str, project: str = "") -> dict:
    """The rest of counted, named, and deliberately not acted on.

    Each of these is a registry FACT the git-first design already answers, and
    changing rows to make a count look nicer would destroy the evidence that the
    answer works. They are here so the migration report carries the same numbers
    the POC's stage-1 census did, measured on the day.
    """
    scope, sargs = _in_project(project)
    q = lambda s: conn.execute(s.replace("{scope}", scope),  # noqa: E731
                               sargs).fetchone()[0]
    odd_prefix = 0
    for f in conn.execute(
            "SELECT f.path, r.version, i.folder FROM files f "
            "JOIN revisions r ON r.id=f.revision_id "
            "JOIN items i ON i.id=r.item_id WHERE COALESCE(f.backend,'')=''"
            + scope, sargs):
        want = f"{f['folder']}/versions/v{f['version']}/" if f["folder"] else ""
        if not want or not f["path"].startswith(want):
            odd_prefix += 1
    facts = {
        "file rows pointing at ANOTHER version's folder": (
            odd_prefix, "manifest.tree_path keys on the segment in the path, so "
                        "the file lands at the same tree path in both revisions "
                        "and the diff stays honest"),
        "items with no vault folder": (
            q("SELECT COUNT(*) FROM items i WHERE (i.folder IS NULL OR "
              "i.folder=''){scope}"),
            "expected for catalog kinds (they live in catalog.git)"),
        "BOM lines with an imprecise child_version": (
            q("SELECT COUNT(*) FROM bom b JOIN revisions r ON "
              "r.id=b.parent_rev_id JOIN items i ON i.id=r.item_id WHERE "
              "b.child_version=''{scope}"),
            "become track = \"release\" + a dated gitlink (§5.1)"),
        # Design kinds only, because that is the population that gets a repo —
        # a catalog item lives in catalog.git and has no `main` to be missing.
        "items with a repo and no released revision": (
            q("SELECT COUNT(*) FROM items i WHERE i.kind NOT IN "
              f"({','.join(repr(k) for k in manifest.CATALOG_KINDS)}) "
              "AND EXISTS (SELECT 1 FROM "
              "revisions r WHERE r.item_id=i.id) AND NOT EXISTS (SELECT 1 FROM "
              "revisions r WHERE r.item_id=i.id AND r.state NOT IN "
              "('DRAFT','IN_REVIEW')){scope}"),
            "no `main`; HEAD points at the newest draft/*"),
        "filedrop-backed file rows": (
            q("SELECT COUNT(*) FROM files f JOIN revisions r ON "
              "r.id=f.revision_id JOIN items i ON i.id=r.item_id WHERE "
              "f.backend='filedrop'{scope}"),
            "UNREADABLE (a retired backend) until `filedrop-repoint` "
            "re-points them to the LFS store"),
    }
    lines = [f"{k}: {v[0]} — {v[1]}" for k, v in facts.items()]
    return {"name": "survey", "title": "the rest of (report only)",
            "found": sum(v[0] for v in facts.values()), "changed": 0,
            "detail": "counted, not changed",
            "action": "report only", "lines": lines,
            "counts": {k: v[0] for k, v in facts.items()}, "pending": {}}


# -- rule 12: filedrop rows -> the LFS store -----------------------

_REPOINT_NOTE = "file row(s) re-pointed from the object store to plm's LFS store"


def _tree_blobs(repo: Path, ref: str) -> list[tuple[str, str, int]] | None:
    """`[(path, blob sha, size)]` for every blob at `ref`, or None when the
    ref is not in the repo. Submodules (gitlinks) are not blobs."""
    try:
        gitrepo.resolve(repo, ref)
    except gitrepo.RepoError:
        return None
    raw = gitrepo.git(repo, "ls-tree", "-r", "-l", "-z", ref)
    out = []
    for rec in raw.split("\0"):
        meta, _, path = rec.partition("\t")
        bits = meta.split()
        if len(bits) >= 4 and bits[1] == "blob" and bits[3].isdigit():
            out.append((path, bits[2], int(bits[3])))
    return out


def _tree_oid(repo: Path, blob: str, size: int, want_size: int,
              cache: dict) -> str:
    """The sha256 of the FILE a tree blob stands for: the oid an LFS pointer
    names, else the plain bytes' own sha256 — but only read for a blob whose
    size is the row's (anything else cannot be this file). '' when neither."""
    if blob in cache:
        return cache[blob]
    ptr = lfs.blob_pointer(repo, blob, size)
    if ptr:
        cache[blob] = ptr[0]
    elif size == want_size:
        cache[blob] = hashlib.sha256(gitrepo.git(
            repo, "cat-file", "blob", blob, binary=True,
            max_bytes=size + 1)).hexdigest()
    else:
        return ""           # not cached: another row may want this size
    return cache[blob]


def rule_filedrop_repoint(conn, *, apply: bool, actor: str,
                          project: str = "") -> dict:
    """Rows on a retired backend (`storage.RETIRED_BACKENDS`: filedrop,
    local) become `lfs` rows.

    `split --lfs` already put each such row's bytes into its revision's TREE
    (stage 17: a pointer for a mesh / big file, the plain bytes for a small
    one), and the object is in plm's LFS store. What is left is the
    registry: the row still says
    `[[files.external]]`. This writes the row `gitindex` would have written
    had the revision arrived as a commit — `backend='lfs'`, `key` = the
    sha256, `path` = where the bytes ARE in the tree.

    The tree path is FOUND, never derived: the revision's ref (`vX.Y.Z`, or
    `draft/vX.Y.Z` while DRAFT/IN_REVIEW) is walked for the blob carrying
    this sha256. One match is the answer; none, several, or one another row
    of the revision already claims is reported and skipped. A row whose
    object is not in the store at the recorded size is reported and skipped.
    Never deletes a row and never touches filedrop; idempotent (a re-pointed
    row is no longer selected).
    """
    from . import context as ctx
    from .split import rev_ref, stable_map
    from .storage import RETIRED_BACKENDS
    scope, sargs = _in_project(project)
    rows = [dict(r) for r in conn.execute(
        "SELECT f.id, f.path, f.key, f.backend, f.sha256, f.size, "
        "r.id AS rev_id, r.version, r.state, i.number, i.kind "
        "FROM files f JOIN revisions r ON r.id=f.revision_id "
        "JOIN items i ON i.id=r.item_id "
        f"WHERE f.backend IN ({','.join('?' * len(RETIRED_BACKENDS))})"
        + scope + " ORDER BY i.number, r.vmaj, r.vmin, r.vpat, f.path",
        (*RETIRED_BACKENDS, *sargs))]
    per_rev: dict[tuple, list] = defaultdict(list)
    for r in rows:
        per_rev[(r["number"], r["version"], r["state"], r["rev_id"],
                 r["kind"])].append(r)
    lines, changed, skipped = [], 0, 0
    pending: dict[str, int] = defaultdict(int)

    def skip(r, why):
        nonlocal skipped
        skipped += 1
        pending[r["number"]] += 1
        lines.append(f"{r['number']} v{r['version']} {r['path']}: SKIP — {why}")

    for (number, version, state, rev_id, kind), rs in per_rev.items():
        catalog = kind in manifest.CATALOG_KINDS
        if catalog:
            # A catalog item has NO tree (one TOML in catalog.git, no files):
            # the row is only ever read through storage.resolve, so it keeps
            # its display name as `path` (            # these files eventually live to Geoff; nothing here forecloses it).
            repo, ref, blobs, taken = None, "", [], set()
        else:
            try:
                repo = gitrepo.repo_path(number)
            except gitrepo.RepoError:
                for r in rs:
                    skip(r, "the item has no repo")
                continue
            ref = rev_ref(version, state)
            full = ("refs/heads/" if ref.startswith("draft/")
                    else "refs/tags/") + ref
            blobs = _tree_blobs(repo, full)
            if blobs is None:
                for r in rs:
                    skip(r, f"{ref} is not in the repo")
                continue
            # what the revision's IN-TREE rows already occupy, by split's map
            intree = [dict(f) for f in conn.execute(
                "SELECT path, backend FROM files WHERE revision_id=?",
                (rev_id,)) if not manifest.is_external(f)]
            ren = stable_map(number, intree, version, [])
            taken = {ren.get(manifest.tree_path(f["path"]),
                             manifest.tree_path(f["path"])) for f in intree}
        cache: dict[str, str] = {}
        done = []
        for r in rs:
            sha, size = str(r["sha256"] or "").lower(), int(r["size"] or 0)
            if not lfs.OID_RE.match(sha) or not lfs.has(sha, size):
                skip(r, f"object {sha[:12] or '(no sha256)'} is not in the "
                        f"LFS store at {size:,} bytes — push it first "
                        "(`git lfs push --all origin` from a clone that has "
                        "it)")
                continue
            if catalog:
                path = Path(r["path"]).name
                changed += 1
                lines.append(f"{number} v{version} {r['path']} -> {path} "
                             f"(catalog item, no tree; lfs {sha[:12]})")
                done.append((r, path))
                if apply:
                    conn.execute("UPDATE files SET backend='lfs', key=?, "
                                 "path=? WHERE id=?", (sha, path, r["id"]))
                else:
                    pending[number] += 1
                continue
            hits = [p for p, b, n in blobs
                    if _tree_oid(repo, b, n, size, cache) == sha]
            free = [p for p in hits if p not in taken]
            if len(free) > 1:
                # several copies: the one under the name split gives this file
                want = gitrepo.stable_name(Path(r["path"]).name, number)
                free = [p for p in free if Path(p).name == want] or free
            if not free:
                skip(r, f"no blob with sha256 {sha[:12]} at {ref}" if not hits
                     else f"its bytes at {ref} ({', '.join(hits)}) are already "
                          "another row's — a duplicate row, decide by hand")
                continue
            if len(free) > 1:
                skip(r, f"ambiguous at {ref}: {', '.join(free)}")
                continue
            path = free[0]
            taken.add(path)
            changed += 1
            lines.append(f"{number} v{version} {r['path']} -> {path} "
                         f"({ref}, lfs {sha[:12]})")
            done.append((r, path))
            if apply:
                conn.execute("UPDATE files SET backend='lfs', key=?, path=? "
                             "WHERE id=?", (sha, path, r["id"]))
            else:
                pending[number] += 1
        if apply and done:
            ctx.add(conn, f"{_REPOINT_NOTE}: " + ", ".join(
                        f"`{r['backend']}:{r['key']}` -> `{p}`" for r, p in done)
                    + (f". Same bytes (sha256 checked against the tree at "
                       f"`{ref}`)" if ref else
                       ". Same bytes (sha256 checked against the store; a "
                       "catalog item has no tree)")
                    + "; the rows were updated, not replaced. "
                      "(filedrop retirement.)",
                    target_type="revision", target=rev_id,
                    actor=actor or "migration", event=False)
    return {"name": "filedrop-repoint",
            "title": "object-store file rows -> plm's LFS store",
            "found": len(rows), "changed": changed,
            "detail": f"{len(rows)} filedrop/local row(s) across "
                      f"{len(per_rev)} revision(s); {changed} re-pointable, "
                      f"{skipped} skipped",
            "action": "UPDATE backend='lfs', key=<sha256>, path=<tree path> "
                      "(+ a context entry per revision); unmatched rows are "
                      "reported, never guessed; nothing is deleted",
            "lines": lines, "pending": dict(sorted(pending.items()))}


# -- the runner ---------------------------------------------------------------

def run(conn, *, apply: bool = False, actor: str = "",
        only: tuple = (), skip: tuple = (), fix_numbers: bool = False,
        new_number: str = "", project: str = "") -> dict:
    """Every rule, in order. `project` scopes EACH rule's selection to that
    project's items (stage 12): the counts, the lines and `pending` — per
    item, what --apply would still change (or, for a rule that only reports,
    what still needs a decision) — are that project's, measured, not filtered
    out of the global run."""
    out = []
    for name in RULES:
        if only and name not in only:
            continue
        if name in skip:
            out.append({"name": name, "title": "", "found": 0, "changed": 0,
                        "detail": "SKIPPED by flag", "action": "", "lines": [],
                        "pending": {}})
            continue
        fn = globals()["rule_" + name.replace("-", "_")]
        kw = {"apply": apply, "actor": actor, "project": project}
        if name == "numbers":
            kw["fix_numbers"] = fix_numbers
            kw["new_number"] = new_number
        out.append(fn(conn, **kw))
    return {"apply": apply, "project": project, "rules": out,
            "total_changed": sum(r["changed"] for r in out),
            "failed": any(r.get("failed") for r in out)}


def _print(res: dict, *, show: int) -> None:
    mode = "APPLY" if res["apply"] else "DRY RUN — nothing is written"
    print(f"plm.migrate_cleanup — {mode}")
    print(f"  database: {config.DB_PATH}")
    if res.get("project"):
        print(f"  project:  {res['project']} (every rule scoped to it)")
    print(f"  vault:    {config.DATA_ROOT}")
    print("=" * 72)
    for r in res["rules"]:
        head = f"{r['name']}"
        if r["detail"] == "SKIPPED by flag":
            print(f"  {head:<16} SKIPPED")
            continue
        print(f"  {head:<16} {r['title']}")
        print(f"  {'':<16} found {r['found']:>5}   "
              f"{'would change' if not res['apply'] else 'changed':>12} "
              f"{r['changed']:>5}")
        print(f"  {'':<16} {r['detail']}")
        print(f"  {'':<16} action: {r['action']}")
        for line in r["lines"][:show]:
            print(f"  {'':<18}- {line}")
        if len(r["lines"]) > show:
            print(f"  {'':<18}… and {len(r['lines']) - show} more")
        print()
    print("=" * 72)
    print(f"  {'rows changed' if res['apply'] else 'rows that WOULD change'}: "
          f"{res['total_changed']}")
    if not res["apply"]:
        print("  re-run with --apply (against a COPY of the registry) to write "
              "them.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m plm.migrate_cleanup",
        description=" registry cleanup, as a rehearsable migration.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="report what would change; write nothing")
    g.add_argument("--apply", action="store_true",
                   help="write the changes (refused against production)")
    ap.add_argument("--db", help="the registry to work on (default: PLM_DB)")
    ap.add_argument("--actor", default="migration", help="who is doing this")
    ap.add_argument("--only", action="append", default=[], choices=RULES,
                    help="run only this rule (repeatable)")
    ap.add_argument("--skip", action="append", default=[], choices=RULES,
                    help="skip this rule (repeatable)")
    ap.add_argument("--fix-numbers", action="store_true",
                    help="rule 3 may actually renumber the malformed item")
    ap.add_argument("--new-number", default="",
                    help="rule 3: the number to give it, instead of the "
                         "class's global max + 1 (which ignores this "
                         "registry's number blocks)")
    ap.add_argument("--project", default="",
                    help="only this project's items (every rule, scoped — "
                         "what the readiness report asks)")
    ap.add_argument("--show", type=int, default=10,
                    help="lines to print per rule (default 10)")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    ap.add_argument("--i-know-this-is-production", dest="confirm_prod",
                    default="", metavar="DB_BASENAME",
                    help="--apply to the database this environment serves "
                         "(PLM_DB) is refused unless you type its basename "
                         "here")
    args = ap.parse_args(argv)

    # What this environment SERVES, captured before `--db` re-points it: inside
    # the prod container that is /var/plm/plm.sqlite3, which no host path
    # check can recognise.
    configured = Path(config.DB_PATH)
    if args.db:
        config.DB_PATH = Path(args.db)
        os.environ["PLM_DB"] = args.db
    try:
        if args.apply:
            _refuse_production(config.DB_PATH, configured=configured,
                               confirm=args.confirm_prod)
    except CleanupError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if args.apply:
        # The schema first: additive tables/columns, idempotent — what the
        # merged code does at startup anyway (rollout step 1). A copy taken
        # from a `master` prod has none of them, and the rules write some
        # (`relations.revision_id`). A dry run never ALTERs anything.
        db.init()

    with db.connect() as conn:
        res = run(conn, apply=args.apply, actor=args.actor,
                  only=tuple(args.only), skip=tuple(args.skip),
                  fix_numbers=args.fix_numbers, new_number=args.new_number,
                  project=args.project)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        _print(res, show=args.show)
    return 1 if res["failed"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
