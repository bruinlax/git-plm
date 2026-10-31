"""What flipping a project's PLM_GIT_MODE will DO — before anybody does it.

Stage 10. Porting a project is
automatic: `split` generates the repos and manifests, `read` keeps them in sync,
items cross at their first revise in `write`. What was missing is knowing what a
flip will change, and what stands in its way, while it is still a decision:

    python -m plm.gitmode --project <P> --readiness       (the CLI)
    GET /api/projects/{p}/readiness                        (the API)
    project_readiness(project)                             (the MCP)
    /projects/<p>                                          (the UI)

All four are this module's `report()`; nothing here writes. The flip itself
stays the documented command (`python -m plm.gitmode --project <p> --mode …`,
the rollout runbook steps 2 and 3).

A VERDICT PER TARGET MODE, and every line that feeds it. Each finding is a
`block` (the flip would break or strand something, and the fix is named), a
`warn` (true and worth knowing; the flip still works) or `info`. The rules that
decide which are below, in one place, and each says why — the readiness of a
project is a judgement, so the judgement is written down where it can be argued
with rather than buried in a colour.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from . import db, manifest

#: How old an open checkout must be to be called stale. In `write` a lock is
#: advisory (stage 3c) — a stale one is a sign somebody's work is parked.
STALE_CHECKOUT_DAYS = 7

#: What a pending migrate_cleanup rule means for a flip to `write`, and why.
#: `block` = the repos would become the record of a wrong fact (a prose find
#: number, a stale duplicate mesh in a tree); `warn` = true and worth doing,
#: but nothing wrong is recorded by flipping first. Counted from the rule's
#: own per-item `pending` (stage 12): what `--apply` would STILL change in
#: this project — so a project whose lines were fixed stops being blocked,
#: and one whose BOMs only lack find numbers is no longer reported clean.
CLEANUP_LEVEL = {
    "find-numbers": ("block", "prose in find_number becomes a [[bom]] `find` "
                              "string in every tree"),
    # the same rule, on items whose BOMs only LACK find numbers: an absent
    # fact, not a wrong one — true and worth fixing, not a reason to wait
    "find-numbers:missing": ("warn", "BOM lines with no find number carry no "
                                     "`find` in the tree"),
    "dupes": ("block", "a stale duplicate mesh rides in the tree under a role "
                       "it should not have"),
    "numbers": ("warn", "a malformed or revision-less item has no repo to "
                        "carry"),
    "unrecoverable": ("warn", "file rows with no bytes: the repos cannot carry "
                              "those files (apply records why, on the revision)"),
    # stage 15: a clone told to regenerate from a file it does not have
    "source-file": ("warn", "`[source] file` names a file the revision does "
                            "not carry — a clone cannot regenerate it and "
                            "render-matches-source renders the wrong thing"),
    # stage 16: a selector render-matches-source has to READ rather than run
    "selector": ("warn", "`[source] selector` is in an annotated form — "
                         "render-matches-source reads it, but only a match "
                         "proves it; a mismatch is skipped, not caught"),
    # stage 19: standing instructions that describe the MCP write loop
    "instructions": ("warn", "INSTRUCTIONS.md tells an agent to checkout / "
                             "revise / iterate on the DRAFT — in `write` the "
                             "loop is git, and the agent has to guess which "
                             "instruction wins"),
}

#: The exact fix per cleanup rule, through ORDINARY ops on the live registry
#: (`--apply` refuses production by design). Printed with each finding.
CLEANUP_FIX = {
    "find-numbers": "renumber the BOM lines 10/20/30… on the parent's next "
                    "revision (update_bom_line / the BOM editor), prose moved "
                    "to the line's notes — or --apply on the migration copy",
    "find-numbers:missing": "give the lines find numbers 10/20/30… on the "
                            "parent's next revision (update_bom_line) — or "
                            "--apply on the migration copy",
    "dupes": "set the stale copy's role to `archive` (PUT /api/files/{id}/role "
             "{\"role\": \"archive\"}) on each named revision",
    "numbers": "decide each: delete the item, check in a v0.1.0, or "
               "renumber it (ops.renumber_item / migrate_cleanup "
               "--fix-numbers --new-number)",
    "unrecoverable": "nothing to restore: --apply writes the context entry "
                     "that says the bytes are gone",
    "source-file": "unambiguous (the file the value names, or the only "
                   "source file): --apply writes it (on the migration copy), "
                   "or set `[source] file` to it on the next revision; "
                   "otherwise say which file regenerates the part",
    "selector": "lossless (notes, `(wrapper …)`, the other mesh's recipe): "
                "--apply writes the `-D` line on the migration copy, or set "
                "`[source] selector` to it on the next revision; a dropped "
                "prose clause is a person's call",
    "instructions": "replace the lines with plm/workflow_text.REPLACEMENT "
                    "(the `instructions-current` check prints it): --apply "
                    "rewrites the migration's generated block on an open "
                    "draft; otherwise in the item's next change",
}


def _finding(level: str, key: str, text: str, *, count: int = 0,
             fix: str = "", items: list | None = None) -> dict:
    out = {"level": level, "key": key, "text": text}
    if count:
        out["count"] = count
    if fix:
        out["fix"] = fix
    if items:
        out["items"] = items[:40]
        out["more"] = max(0, len(items) - 40)
    return out


def report(conn, project: str) -> dict:
    """The whole readiness report for one project, as data."""
    from . import (gitmode, gitwrite, migrate_cleanup, policy as pol,
                   relations as relmod, split)
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM items WHERE project=? ORDER BY number", (project,))]
    if not items:
        raise LookupError(f"no items in project {project!r}")
    ids = [i["id"] for i in items]
    by_id = {i["id"]: i for i in items}
    q = ",".join("?" * len(ids))
    revs = [dict(r) for r in conn.execute(
        f"SELECT * FROM revisions WHERE item_id IN ({q}) "
        "ORDER BY item_id, vmaj, vmin, vpat", ids)]
    rev_by_id = {r["id"]: r for r in revs}
    mode = gitmode.explain(project, conn)
    design = [i for i in items if i["kind"] not in manifest.CATALOG_KINDS]

    # -- counts -------------------------------------------------------------
    kinds = Counter(i["kind"] for i in items)
    states = Counter(r["state"] for r in revs)
    drafts = [r for r in revs if r["state"] in ("DRAFT", "IN_REVIEW")]
    catalog_refs = sorted({r["number"] for r in conn.execute(
        f"SELECT DISTINCT c.number FROM bom b JOIN items c "
        f"ON c.id=b.child_item_id WHERE b.parent_rev_id IN "
        f"(SELECT id FROM revisions WHERE item_id IN ({q})) AND c.kind IN "
        f"({','.join('?' * len(manifest.CATALOG_KINDS))})",
        (*ids, *manifest.CATALOG_KINDS))})
    counts = {"items": len(items), "design_items": len(design),
              "catalog_items_in_project": len(items) - len(design),
              "kinds": dict(sorted(kinds.items())),
              "revisions": len(revs), "drafts": len(drafts),
              "releases": len(revs) - len(drafts),
              "states": dict(sorted(states.items())),
              "catalog_items_referenced": len(catalog_refs)}

    # -- repos as they stand ---------------------------------------------------
    have_repo, live = [], []
    for i in design:
        repo = split.REPOS / f"{i['number']}.git"
        if (repo / "HEAD").exists():
            have_repo.append(i["number"])
            if gitwrite.is_live(repo):
                live.append(i["number"])
    layout = split.layout()
    repos = {"root": str(split.REPOS), "layout": layout,
             "built": len(have_repo), "missing": sorted(
                 i["number"] for i in design if i["number"] not in have_repo),
             "live": sorted(live)}

    # -- filenames that will be normalised (§6c-1) ------------------------------
    reg_files: dict[int, list[dict]] = {}
    for f in conn.execute(
            f"SELECT * FROM files WHERE revision_id IN "
            f"(SELECT id FROM revisions WHERE item_id IN ({q})) "
            "ORDER BY revision_id, path, id", ids):
        reg_files.setdefault(f["revision_id"], []).append(dict(f))
    renames: list[str] = []
    collisions: list[str] = []
    seen_pairs: set = set()
    for r in revs:
        it = by_id[r["item_id"]]
        if it["kind"] in manifest.CATALOG_KINDS:
            continue
        ren = split.stable_map(it["number"], reg_files.get(r["id"], []),
                               r["version"], collisions)
        for old, new in sorted(ren.items()):
            if old != new and (it["number"], old, new) not in seen_pairs:
                seen_pairs.add((it["number"], old, new))
                renames.append(f"{it['number']}: {old} -> {new}")
    filenames = {"normalise": bool((layout or {}).get("normalize_names", True)),
                 "renames": len(renames), "collisions": collisions,
                 "examples": renames[:40], "more": max(0, len(renames) - 40)}

    # -- pins ---------------------------------------------------------------------
    pin_states: Counter = Counter()
    blocked_parents: list[dict] = []
    newest: dict[int, dict] = {}
    for r in revs:
        newest[r["item_id"]] = r          # ordered by version: last wins
    for r in revs:
        lines = conn.execute(
            "SELECT b.*, c.number AS child, c.kind AS child_kind, c.id AS cid "
            "FROM bom b JOIN items c ON c.id=b.child_item_id "
            "WHERE b.parent_rev_id=?", (r["id"],)).fetchall()
        blockers = []
        for b in lines:
            if b["child_kind"] in manifest.CATALOG_KINDS:
                pin_states["catalog"] += 1
                continue
            if not b["child_version"]:
                pin_states["imprecise"] += 1
                rel_child = conn.execute(
                    "SELECT 1 FROM revisions WHERE item_id=? AND "
                    "state='RELEASED'", (b["cid"],)).fetchone()
                if not rel_child:
                    blockers.append(f"{b['child']} (imprecise, and it has no "
                                    "RELEASED revision to resolve to)")
                continue
            cr = db.find_revision(conn, b["child"], b["child_version"])
            if not cr and "-" in b["child_version"]:
                # a Go-style pseudo-version: pinned at an unreleased COMMIT
                pin_states["unreleased"] += 1
                blockers.append(f"{b['child']} @ an unreleased commit "
                                f"({b['child_version']})")
            elif not cr:
                pin_states["dangling"] += 1
                blockers.append(f"{b['child']} v{b['child_version']} (does "
                                "not exist)")
            elif cr["state"] in ("DRAFT", "IN_REVIEW"):
                pin_states["unreleased"] += 1
                blockers.append(f"{b['child']} v{b['child_version']} "
                                f"({cr['state']})")
            elif cr["state"] != "RELEASED":
                pin_states["released_not_RELEASED"] += 1
                blockers.append(f"{b['child']} v{b['child_version']} "
                                f"({cr['state']})")
            else:
                pin_states["released"] += 1
        if blockers and newest.get(r["item_id"]) is r:
            blocked_parents.append(
                {"item": by_id[r["item_id"]]["number"], "version": r["version"],
                 "state": r["state"], "blocked_by": blockers})
    pins = {"lines": dict(sorted(pin_states.items())),
            "parents_not_releasable": blocked_parents,
            "rule": "§5.1: a parent cannot be RELEASED above its least-mature "
                    "child. Counted for each item's NEWEST revision; a "
                    "PROTOTYPE pin is a tag but not a release."}

    # -- sources: what render-matches-source will say ---------------------------
    from . import checks as checksmod
    src = {"regenerable": [], "prose": [], "none": []}
    for i in design:
        r = newest.get(i["id"])
        if not r:
            continue
        label = f"{i['number']} v{r['version']}"
        if not r["source"]:
            src["none"].append(label)
            continue
        # stage 16: the forms render-matches-source reads (annotated `-D`,
        # a module call) are regenerable; only real prose is the to-do
        why = checksmod.selector_recipe(r["selector"] or "")["why"]
        if why:
            src["prose"].append(f"{label}: {r['selector'][:70]}")
        else:
            src["regenerable"].append(label)
    sources = {k: {"count": len(v), "items": v[:40]} for k, v in src.items()}
    sources["rule"] = ("render-matches-source re-renders `[source] file` with a "
                       "parameter selector; a prose selector or no source is "
                       "`skipped` with the reason — the finding is the to-do.")

    # -- external files ----------------------------------------------------------
    from . import gitvalidate
    cap = gitvalidate.MAX_FILE_BYTES
    # Rows outside the tree AND outside plm's LFS store, in ONE query, split
    # here: a RETIRED backend's row (plm cannot read its bytes) blocks below
    # with the command that re-points it; any other is what a repo carries as
    # `[[files.external]]` (manifest.is_external) — information.
    from . import manifest as _mf
    from .storage import RETIRED_BACKENDS
    in_tree = ",".join("?" * len(_mf.IN_TREE_BACKENDS))
    outside = [dict(r) for r in conn.execute(
        f"SELECT f.path, f.size, f.backend, i.number, r.version FROM files f "
        f"JOIN revisions r ON r.id=f.revision_id JOIN items i ON i.id=r.item_id "
        f"WHERE r.item_id IN ({q}) AND COALESCE(f.backend,'') NOT IN "
        f"({in_tree}) ORDER BY f.size DESC", (*ids, *_mf.IN_TREE_BACKENDS))]
    retired = sorted(f"{e['number']} v{e['version']}: {e['path']} "
                     f"({e['backend']})" for e in outside
                     if e["backend"] in RETIRED_BACKENDS)
    ext = [e for e in outside if e["backend"] not in RETIRED_BACKENDS]
    over = [e for e in ext if (e["size"] or 0) > cap]
    external = {"count": len(ext),
                "bytes": sum(e["size"] or 0 for e in ext),
                "largest": [f"{e['number']} v{e['version']} {Path(e['path']).name}"
                            f" — {(e['size'] or 0) / 1e6:.1f} MB" for e in ext[:8]],
                "over_cap": len(over), "cap_bytes": cap,
                "over_cap_items": sorted({e["number"] for e in over})}

    # -- facts a repo cannot carry faithfully (stage 12) ----------------------
    # Two registry facts the readiness report did not see and the rehearsal
    # did: a row on a RETIRED backend (above), and a BOM cycle (split omits
    # the back-edge gitlink, so those lines have no submodule pin).
    _order, all_cycles = split.build_order(split.load(conn))
    numbers_ = {i["number"] for i in items}
    cycles = [c for c in all_cycles if set(c) & numbers_]

    # -- cleanup for this project --------------------------------------
    # Every rule SCOPED to the project (stage 12) — not the global dry run
    # with its lines filtered by item number, which was an approximation.
    res = migrate_cleanup.run(conn, apply=False, actor="readiness",
                              # filedrop-repoint walks trees (a git call
                              # per blob) and blocks nothing: not here
                              skip=("relations", "filedrop-repoint"),
                              project=project)
    cleanup_rules = []
    for rule in res["rules"]:
        pend = rule.get("pending") or {}
        if not pend or rule["name"] not in CLEANUP_LEVEL:
            continue
        parts = [(rule["name"], pend)]
        if rule["name"] == "find-numbers":
            prose = set(rule.get("prose_items") or ())
            parts = [("find-numbers",
                      {k: v for k, v in pend.items() if k in prose}),
                     ("find-numbers:missing",
                      {k: v for k, v in pend.items() if k not in prose})]
        for key, items_ in parts:
            if not items_:
                continue
            level, why = CLEANUP_LEVEL[key]
            cleanup_rules.append({
                "rule": key, "title": rule["title"],
                "lines": sum(items_.values()), "items": items_,
                "examples": [ln for ln in rule.get("lines") or []
                             if ln.split(" ", 1)[0] in items_][:6],
                "level": level, "why": why, "fix": CLEANUP_FIX[key]})
    rel_rule = migrate_cleanup.rule_relations(conn, apply=False, actor="",
                                              project=project)
    cleanup = {"rules": cleanup_rules,
               "relations_would_change": rel_rule["changed"],
               "note": "each rule run scoped to this project (python -m "
                       f"plm.migrate_cleanup --dry-run --project {project}); "
                       "`lines` = what --apply would still change, per item; "
                       "--apply refuses production by design"}

    # -- relations: old -> new homes, coupons with/without results ---------------
    homes = Counter()
    for k, v in rel_rule["counts"].items():
        homes[k] += v
    coupons = [dict(r) for r in conn.execute(
        f"SELECT rel.*, f.number AS cpn, t.number AS part FROM relations rel "
        f"JOIN items f ON f.id=rel.item_id JOIN items t "
        f"ON t.id=rel.target_item_id WHERE rel.relation='coupon_for' AND "
        f"(rel.item_id IN ({q}) OR rel.target_item_id IN ({q}))", (*ids, *ids))]
    relations = {"by_home": dict(sorted(homes.items())),
                 "moves": rel_rule["lines"][:60],
                 "more": max(0, len(rel_rule["lines"]) - 60),
                 "coupons": len(coupons),
                 "coupons_with_result": sum(1 for c in coupons if c["result"]),
                 "coupons_passing": sum(1 for c in coupons
                                        if c["result"] == "pass"),
                 "coupons_without_result": sorted(
                     f"{c['cpn']} -> {c['part']} v{c['target_version']}"
                     for c in coupons if not c["result"])[:40]}

    # -- open work ----------------------------------------------------------------
    approvals = [dict(r) for r in conn.execute(
        f"SELECT ap.id, ap.target_state, ap.requested_by, i.number, r.version "
        f"FROM approvals ap JOIN revisions r ON r.id=ap.revision_id "
        f"JOIN items i ON i.id=r.item_id WHERE ap.decision='' AND "
        f"r.item_id IN ({q}) ORDER BY ap.id", ids)]
    stale_before = db.now() - STALE_CHECKOUT_DAYS * 86400
    checkouts = [dict(r) for r in conn.execute(
        f"SELECT c.actor, c.at, i.number FROM checkouts c JOIN items i "
        f"ON i.id=c.item_id WHERE c.released_at IS NULL AND c.item_id IN ({q})",
        ids)]
    open_releases = [dict(r) for r in conn.execute(
        f"SELECT rel.id, rel.version, rel.state, i.number FROM releases rel "
        f"JOIN items i ON i.id=rel.item_id WHERE rel.state IN "
        f"('requested','in_review') AND rel.item_id IN ({q})", ids)] \
        if _has_table(conn, "releases") else []
    open_work = {
        "drafts": [f"{by_id[r['item_id']]['number']} v{r['version']} "
                   f"({r['state']})" for r in drafts][:60],
        "approvals_pending": [f"#{a['id']} {a['number']} v{a['version']} -> "
                              f"{a['target_state']} (by {a['requested_by']})"
                              for a in approvals],
        "releases_open": [f"#{r['id']} {r['number']} v{r['version']} "
                          f"({r['state']})" for r in open_releases],
        "checkouts": [f"{c['number']} by {c['actor']}" for c in checkouts],
        "checkouts_stale": [f"{c['number']} by {c['actor']}" for c in checkouts
                            if (c["at"] or 0) < stale_before]}

    pol_released = pol.for_target(project, "RELEASED", conn=conn)
    pol_proto = pol.for_target(project, "PROTOTYPE", conn=conn)

    # -- the verdicts ---------------------------------------------------------------
    read_f: list[dict] = []
    write_f: list[dict] = []
    if repos["missing"]:
        read_f.append(_finding(
            "block", "repos-missing",
            f"{len(repos['missing'])} of {len(design)} design items have no "
            "repository yet, so `read` would serve 404s for them until a legacy "
            "write happens to sync each one.",
            count=len(repos["missing"]), items=repos["missing"],
            fix="build them first: python -m plm.split --all --prune "
                "(the rollout runbook step 2, Prepare)"))
    if not layout:
        read_f.append(_finding(
            "block", "layout-unmarked",
            "the repos root has no recorded filename layout "
            f"({split.LAYOUT_FILE}); a sync would refuse rather than guess.",
            fix="the step-2 build records it: python -m plm.split --all "
                "--normalize-names --prune"))
    if filenames["renames"]:
        read_f.append(_finding(
            "info", "filenames",
            f"{filenames['renames']} tracked filename(s) lose their version or "
            "number stamp in the repos (§6c-1); the registry keeps its paths.",
            count=filenames["renames"], items=filenames["examples"]))
    if collisions:
        read_f.append(_finding(
            "warn", "filename-collisions",
            f"{len(collisions)} pair(s) of files normalise to the same name; "
            "both are kept (the second as `~2`).", items=collisions))
    if relations["by_home"].get("revision:INFERRED"):
        read_f.append(_finding(
            "info", "relations-inferred",
            f"{relations['by_home']['revision:INFERRED']} coupon claim(s) name "
            "no claimant revision; the repos declare each in the revision the "
            "inference picks (the coupon's first revision after the link).",
            fix="write them down: python -m plm.migrate_cleanup --apply --only "
                "relations (on a COPY — see its refusal)"))
    if relations["by_home"].get("revision:homeless"):
        read_f.append(_finding(
            "warn", "relations-homeless",
            f"{relations['by_home']['revision:homeless']} claim(s) belong to "
            "an item with no revision at all; no repo can show them (they stay "
            "registry rows).",
            items=[m for m in rel_rule["lines"] if "[homeless]" in m]))
    if retired:
        read_f.append(_finding(
            "block", "retired-backend",
            f"{len(retired)} file row(s) name a retired storage backend "
            f"({', '.join(RETIRED_BACKENDS)}): plm cannot read those bytes.",
            items=retired,
            fix=f"{RETIRED_BACKENDS['filedrop']} (re-points each row to the "
                "LFS store by its bytes)"))
    if cycles:
        read_f.append(_finding(
            "warn", "bom-cycle",
            f"a BOM cycle reaches this project ({'; '.join(' <-> '.join(c) for c in cycles)}): "
            "split builds the members with the back-edge gitlink omitted, so "
            "those [[bom]] lines carry no submodule pin.",
            items=[" <-> ".join(c) for c in cycles],
            fix="remove the line that closes the loop on the parent's next "
                "revision (a BOM edit); a frozen revision keeps it as history"))
    if external["count"]:
        read_f.append(_finding(
            "info", "external",
            f"{external['count']} file(s), {external['bytes'] / 1e6:.0f} MB, "
            "live outside the tree and plm's LFS store and appear as "
            "[[files.external]].", count=external["count"]))

    write_f += [f for f in read_f if f["level"] == "block"]
    for level in ("block", "warn"):
        rules = [c for c in cleanup_rules if c["level"] == level]
        if not rules:
            continue
        n = sum(c["lines"] for c in rules)
        write_f.append(_finding(
            level, f"cleanup-{level}",
            f"migrate_cleanup still lists {n} line(s) of this project"
            + (" — in `write` the repos become the record, so fix them first "
               "(rollout step 3 precondition)." if level == "block" else
               " that a flip would carry as they are."),
            count=n, items=[f"{c['rule']}: {c['lines']} line(s) on "
                            f"{', '.join(f'{k} ({v})' for k, v in c['items'].items())}"
                            f" — {c['why']}. Fix: {c['fix']}"
                            for c in rules],
            fix=f"python -m plm.migrate_cleanup --dry-run --project {project} "
                "names every line; fix each through ordinary ops (or --apply "
                "on the migration copy)"))
    if rel_rule["changed"]:
        write_f.append(_finding(
            "warn", "relations-claimants",
            f"{rel_rule['changed']} relation row(s) do not say their claimant "
            "revision (or carry one an item-level claim cannot have). The "
            "repos place each by relations.claimant either way; writing it "
            "down makes the registry say what the trees say.",
            count=rel_rule["changed"],
            items=[m for m in rel_rule["lines"] if " -> write " in m
                   or " -> drop " in m],
            fix="python -m plm.migrate_cleanup --apply --only relations "
                "(on the migration copy)"))
    if approvals:
        write_f.append(_finding(
            "warn", "approvals-open",
            f"{len(approvals)} approval(s) pending. They decide through the "
            "legacy path (a legacy revision has no branch to merge) — fine, "
            "but decide them first if you want the queue to be all one kind.",
            items=open_work["approvals_pending"]))
    if open_work["checkouts_stale"]:
        write_f.append(_finding(
            "warn", "checkouts-stale",
            f"{len(open_work['checkouts_stale'])} checkout(s) older than "
            f"{STALE_CHECKOUT_DAYS} days. In `write` a lock is advisory — "
            "somebody's parked work stops being protected.",
            items=open_work["checkouts_stale"]))
    if blocked_parents:
        write_f.append(_finding(
            "info", "pins-not-releasable",
            f"{len(blocked_parents)} item(s) could not be RELEASED today "
            "because a child pin is not RELEASED — true in every mode, and the "
            "release gate says so by name in `write`.",
            items=[f"{p['item']} v{p['version']}: {', '.join(p['blocked_by'][:3])}"
                   for p in blocked_parents]))
    if external["over_cap"]:
        write_f.append(_finding(
            "warn", "over-cap",
            f"{external['over_cap']} file(s) are over the "
            f"{cap / 1e6:.0f} MB in-tree cap. They stay external; a change to "
            "one cannot be pushed until LFS (Phase 4).",
            items=external["over_cap_items"]))
    if src["prose"] or src["none"]:
        write_f.append(_finding(
            "info", "sources",
            f"render-matches-source will skip {len(src['prose'])} prose "
            f"selector(s) and {len(src['none'])} item(s) with no source; "
            f"{len(src['regenerable'])} are regenerable.",
            count=len(src["prose"]) + len(src["none"])))

    def verdict(findings: list[dict], target: str) -> dict:
        # The verdict is the judgement of the DATA — the same answer whatever
        # the project is set to today, so a project already in `write` (the
        # POC) is still told what would stop it; `now` says where it stands.
        blocks = [f for f in findings if f["level"] == "block"]
        warns = [f for f in findings if f["level"] == "warn"]
        if blocks:
            word = f"blocked for {target}"
        elif warns:
            word = f"ready for {target}, with warnings"
        else:
            word = f"ready for {target}"
        order = {"off": 0, "read": 1, "write": 2}
        now = ("this project is in " + mode["mode"]
               + (" — already there" if mode["mode"] == target else
                  " — past it (write includes read)"
                  if order[mode["mode"]] > order[target] else ""))
        return {"target": target, "verdict": word, "ok": not blocks,
                "now": now, "current": mode["mode"] == target,
                "blocks": blocks, "warnings": warns,
                "findings": findings}

    will = {
        "read": [
            f"serve {len(design)} item repositories at /git/<NUMBER>.git "
            f"({len(have_repo)} built today) and a catalog repo for "
            f"{len(catalog_refs)} purchased/hardware item(s) this project uses",
            "regenerate an item's repo ~2 s after each legacy write "
            "(plm/gitsync.py), and the parents and coupon claimants whose "
            "pins moved, one level per pass",
            f"show {relations['by_home'].get('revision:declared', 0) + relations['by_home'].get('revision:INFERRED', 0)} "
            f"revision-home claim(s) in plm.toml, "
            f"{relations['by_home'].get('item:item', 0)} item-home claim(s) in "
            f"item.toml, {relations['by_home'].get('note:declared', 0)} note(s)",
            f"rename {filenames['renames']} tracked file(s) in the repos "
            "(never in the registry)",
            "refuse every `git push`, teachably — writes stay on the API/MCP/UI",
            "change nothing in the registry: the database stays the record"],
        "write": [
            "make every design write on a CROSSED item a commit; an item "
            f"crosses at its first revise ({len(live)} of {len(design)} have "
            "crossed already)",
            "accept `git push` to draft/* and refs/for/main as proposals",
            f"auto-merge at {', '.join(k for k, v in (('PROTOTYPE', pol_proto), ('RELEASED', pol_released)) if v.get('auto')) or 'no maturity'} "
            f"by policy; RELEASED needs {', '.join(pol_released.get('approver_kinds') or ())} "
            f"({pol_released.get('source', '')})",
            "make locks advisory "
            f"({len(checkouts)} open checkout(s) today)",
            "write post-release facts (attributes, tags, installed_on, status) "
            "as refs/notes/plm notes, never by moving a tag",
            "keep syncing not-yet-crossed items exactly as `read` does"],
    }
    return {
        "project": project, "mode": mode, "counts": counts,
        "catalog_referenced": catalog_refs[:60], "repos": repos,
        "filenames": filenames, "pins": pins, "sources": sources,
        "external": external, "cleanup": cleanup, "relations": relations,
        "open": open_work,
        "policy": {"PROTOTYPE": {k: pol_proto.get(k) for k in
                                 ("auto", "approver_kinds", "source", "rule")},
                   "RELEASED": {k: pol_released.get(k) for k in
                                ("auto", "approver_kinds", "source", "rule")}},
        "verdicts": {"read": verdict(read_f, "read"),
                     "write": verdict(write_f, "write")},
        "will": will,
        "flip": {"read": f"python -m plm.gitmode --project {project} --mode "
                         "read --actor <you> --note '<why>'",
                 "write": f"python -m plm.gitmode --project {project} --mode "
                          "write --actor <you> --note '<why>'",
                 "runbook": "the rollout runbook steps 2 (read) and 3 (write)"},
    }


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                        "name=?", (name,)).fetchone() is not None


def render(rep: dict) -> str:
    """The report as text — what `python -m plm.gitmode --readiness` prints."""
    out = []
    c = rep["counts"]
    out.append(f"READINESS — project {rep['project']}  (now: {rep['mode']['mode']}"
               f", {rep['mode']['source']})")
    out.append("=" * 72)
    out.append(f"  {c['items']} items ({c['design_items']} design), "
               f"{c['revisions']} revisions: {c['releases']} released/"
               f"prototype, {c['drafts']} draft/in review; "
               f"{c['catalog_items_referenced']} catalog items referenced")
    out.append(f"  repos: {rep['repos']['built']} built, "
               f"{len(rep['repos']['missing'])} missing, "
               f"{len(rep['repos']['live'])} live (crossed)")
    out.append(f"  filenames normalised: {rep['filenames']['renames']}, "
               f"collisions {len(rep['filenames']['collisions'])}")
    out.append(f"  BOM pins: " + ", ".join(
        f"{k} {v}" for k, v in rep["pins"]["lines"].items()))
    out.append(f"  sources: " + ", ".join(
        f"{k} {rep['sources'][k]['count']}"
        for k in ("regenerable", "prose", "none")))
    e = rep["external"]
    out.append(f"  external: {e['count']} file(s), "
               f"{e['bytes'] / 1e6:.0f} MB, {e['over_cap']} over the cap")
    r = rep["relations"]
    out.append("  relations by home: " + ", ".join(
        f"{k} {v}" for k, v in r["by_home"].items()))
    out.append(f"  coupons: {r['coupons']} claims, {r['coupons_with_result']} "
               f"with a result ({r['coupons_passing']} pass)")
    o = rep["open"]
    out.append(f"  open: {c['drafts']} drafts, "
               f"{len(o['approvals_pending'])} approvals, "
               f"{len(o['releases_open'])} releases, {len(o['checkouts'])} "
               f"checkouts ({len(o['checkouts_stale'])} stale)")
    for target in ("read", "write"):
        v = rep["verdicts"][target]
        out.append("")
        out.append(f"-> {target.upper()}: {v['verdict']}   ({v['now']})")
        for f in v["findings"]:
            out.append(f"   [{f['level']}] {f['text']}")
            if f.get("fix"):
                out.append(f"          fix: {f['fix']}")
            for it in (f.get("items") or [])[:5]:
                out.append(f"          · {it}")
        out.append(f"   flipping to {target} will:")
        for w in rep["will"][target]:
            out.append(f"     - {w}")
        out.append(f"   the flip: {rep['flip'][target]}")
    return "\n".join(out)


def main(project: str, *, as_json: bool = False) -> int:
    with db.connect() as conn:
        try:
            rep = report(conn, project)
        except LookupError as exc:
            print(str(exc))
            return 2
    print(json.dumps(rep, indent=2, default=str) if as_json else render(rep))
    return 0
