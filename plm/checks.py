"""Checks — CI for physical things (PLM_REVIEW_MODEL.md §2.6).

A *check* is a judgement about ONE COMMIT, recorded and shown instead of only
refused. Everything here is keyed by the commit SHA, because that is what a
reviewer signs: "these checks were green" has to mean "green on the bytes that
shipped", and a re-push must get a fresh set rather than inheriting the old
one's verdict.

⭐ THE THREE RULES THIS MODULE IS BUILT AROUND

1. **A check never blocks a push.** `plm/gitvalidate.py` refuses what cannot be
   a revision at all; a check is the slower, richer half — it measures, it
   reports, and it lets the author decide. Policy (§2.3) may later decide that a
   red check blocks a *release*; that is a promotion decision made by
   `required_for()` below, not a door slammed at `git push`.

2. **A check that cannot run says `skipped` WITH THE REASON.** Never a silent
   pass. "No `[source] file`", "the mesh is not in the tree", "OpenSCAD is not in
   this image" are all answers; a green tick that means "we did not look" is the
   one outcome that makes the whole board worthless.

3. **The rules are not re-implemented here.** `manifest`, `files-present` and
   `pins-released` ask `plm/gitvalidate.py` and `gitindex.release_gate` the same
   questions the receive hook asks. One code path per behaviour: if the hook and
   the check could disagree, the check is a lie with a nice icon.

⚠️ NO NETWORK, NO NEW DEPENDENCY, NO NEW SERVICE. The STL reader is stdlib. The
re-render shells out to the `openscad` that is already in this image, exactly as
`plm/thumbs.py` does. The runner is one thread and a queue inside the API
process. A check that wanted a service would be an *integration* posting a
result (§2.6's shape) — the `source` column is there so that day costs nothing.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import queue
import re
import struct
import subprocess
import tempfile
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import (config, db, gitindex, gitrepo, gitvalidate, lfs, manifest,
               requirements, roles, thumbs, workflow_text)

# -- knobs (every one has a working default; an unset env is not a broken stack)
#: PLM_CHECKS — what THIS process does with a request for checks:
#:   1      run them here (the API process: one runner thread)
#:   relay  record the request in the shared `check_requests` table; the
#:          process that runs checks picks it up within PLM_CHECKS_SWEEP_S.
#:          The MCP process (stage 12, loose end 4: an MCP write's checks used
#:          to be DROPPED — `enqueue` returned early with checks off there)
#:   0      off: nothing is run or recorded (an `off` instance, the git service)
_MODE = os.environ.get("PLM_CHECKS", "1").strip().lower()
ENABLED = _MODE not in ("0", "", "no", "false", "relay")
RELAY = _MODE == "relay"
SWEEP_S = float(os.environ.get("PLM_CHECKS_SWEEP_S", "2"))
RENDER_TIMEOUT_S = int(os.environ.get("PLM_CHECKS_RENDER_TIMEOUT", "180"))
#: A `running` row older than this was orphaned by a crash, not by a slow check.
STALE_AFTER_S = RENDER_TIMEOUT_S * 3
#: Rendered-geometry cache, keyed by (source blob sha, selector). Local disk,
#: beside the SQLite index and the thumbnails — derived data, never precious.
CACHE = config.CACHE_DIR / "checks-render"

#: What produced a row. `builtin` is this in-process runner; when a real
#: integration starts posting results (§2.6) it names itself here, so the audit
#: trail can never confuse "plm measured this" with "something told plm".
SOURCE_BUILTIN = "builtin"

STATUSES = ("queued", "running", "pass", "fail", "warn", "skipped", "error")


# =============================================================================
# A. THE REGISTRY — data, like plm/actions.py ACTIONS and plm/roles.py
#    MEDIA_KINDS. Adding a check is an entry here plus one function.
# =============================================================================

#: `applies` is answered against the COMMIT, never against the item row — the
#: tree is the fact:
#:
#:   "*"                always
#:   "assembly"         the tree carries at least one `parts/*` gitlink
#:   "has-mesh"         the manifest declares a mesh (in the tree OR external)
#:   "has-coupon"       a coupon claim (coupon_for) pins this commit — the one
#:                      `applies` answered from the registry, because the
#:                      claim lives in the COUPON's repo (stage 10)
#:   "has-instructions" the tree carries standing instructions (INSTRUCTIONS.md)
#:   "has-requirements" the manifest has purchase REQUIREMENT lines (stage 19)
#:   "change"           the subject is a PULL REQUEST (`CHG-n`), not a commit:
#:                      the row's `sha` is its head-set key (§2.10)
#:   "has-source+mesh"  …the same population. The `+source` half is answered by
#:                      the check itself as `skipped: no [source] file`, because
#:                      "nothing here records how this geometry was made" is a
#:                      finding a reviewer must see, not a row that vanishes.
#:
#: `cost` is scheduling, not decoration: `minutes` checks run one at a time.
#: `advisory` marks a check that may never be required by policy.
CHECKS: dict[str, dict] = {
    "manifest": {
        "label": "Manifest is valid",
        "applies": "*", "cost": "instant",
        "help": "plm.toml parses, declares a legal shape, and the tree is "
                "within the size and symlink rules the receive hook enforces.",
    },
    "pins-released": {
        "label": "Every BOM pin is a release",
        "applies": "assembly", "cost": "instant",
        "help": "§5.1 — you cannot release above your least-mature child. "
                "Every parts/* gitlink must sit on a release tag.",
    },
    "explodes": {
        "label": "Assembly explodes in the viewer",
        "applies": "assembly", "cost": "instant",
        "help": "Every first-level child that has a mesh carries a `viz` posed "
                "in this assembly's coordinates, so the viewer can pull it "
                "apart (thumbs.explode_status — the viewer's own resolver). "
                "Submitting an assembly of two or more made children that does "
                "not explode needs an `explode_waiver`.",
    },
    "files-present": {
        "label": "Declared files exist",
        "applies": "*", "cost": "instant",
        "help": "Every path in [files] is in this tree or declared under "
                "[[files.external]], and `primary` names real geometry.",
    },
    "source-present": {
        "label": "Source is in the repo",
        "applies": "*", "cost": "instant",
        "help": "Stage 15. `[source] file` is in this tree (or in a `lib/<N>` "
                "shared-source submodule) and is one of `[files].source`, and "
                "every `include <…>` / `use <…>` it makes resolves inside the "
                "repo — so a clone can regenerate the geometry. A source that "
                "includes a file from the project folder does not render from "
                "a clone.",
    },
    "stable-names": {
        "label": "No version in filenames",
        "applies": "*", "cost": "instant", "advisory": True,
        "help": "§6c-1 — the tag carries the version and the repo carries the "
                "number, so a file that repeats them turns every revision into "
                "a delete plus an add and breaks `git log` on the part.",
    },
    "geometry-facts": {
        "label": "Measured bbox / volume / mass",
        "applies": "has-mesh", "cost": "seconds",
        "help": "Reads the primary mesh and measures it: bounding box, "
                "signed-tetrahedron volume, triangle count, open edges, and "
                "mass at the declared material's density. Disagreement with "
                "the declared attributes is a warning naming both numbers.",
    },
    "verified-by-coupon": {
        "label": "Verified by a coupon",
        "applies": "has-coupon", "cost": "instant", "advisory": True,
        "help": "Stage 10. The coupons whose coupon_for claim pins THIS commit "
                "(declared in each coupon's own plm.toml — the inverse is "
                "derived) and their results. A passing one can attest the "
                "release checklist's fit line by reference.",
    },
    "shared-source": {
        "label": "Shared sources agree",
        "applies": "*", "cost": "seconds", "advisory": True,
        "help": "Stage 15. Each source file of this tree, and the other items "
                "of the project that carry the SAME PATH on what they ship: "
                "the same blob (shared, in step) or a different one (DRIFT — "
                "one of them was changed and the others were not). The home "
                "for a source many items share is a `lib/<NUMBER>` submodule "
                "(docs/SHARED-SOURCES.md).",
    },
    "purchase-requirements": {
        "label": "Purchases selected",
        "applies": "has-requirements", "cost": "instant",
        "help": "Stage 19. `[[bom]]` lines with `requirement` and no `path` "
                "are purchases nobody has selected yet (plm/requirements.py). "
                "A green mesh or explode check says nothing about whether the "
                "assembly can be ORDERED: this one warns with the count and "
                "the list. Required — so a release is refused — only when the "
                "manifest says `[release] required = "
                "[\"purchase-requirements\"]`.",
    },
    "instructions-current": {
        "label": "Instructions match the write loop",
        "applies": "has-instructions", "cost": "instant", "advisory": True,
        "help": "Stage 19. In a `write` project the loop is git (branch → "
                "bump → push into a change → a human approves). An "
                "INSTRUCTIONS.md that still says checkout / revise / "
                "put_file / \"iterate on this DRAFT\" contradicts it; the "
                "check names the lines and prints the replacement paragraph "
                "(plm/workflow_text.py).",
    },
    "physical-diff": {
        "label": "Physical diff",
        "applies": "change", "cost": "minutes", "advisory": True,
        "help": "§2.10. A check on the PULL REQUEST, not a commit: every mesh "
                "that changed across its members, old and new from one camera "
                "— removed red, added green — as images (GitHub's "
                "`output.images`), the composite first. Re-runs when any "
                "member's head moves; an unchanged head set is never rendered "
                "twice. pass = every changed mesh rendered; warn = one could "
                "not be (named); skipped = no mesh changed.",
    },
    "render-matches-source": {
        "label": "Mesh matches its source",
        "applies": "has-source+mesh", "cost": "minutes",
        "help": "Re-renders [source] file + selector with OpenSCAD and "
                "compares the result to the committed mesh BY GEOMETRY — "
                "volume, bbox, triangle count — never by bytes. A failure "
                "means the STL in this revision was not generated from the "
                "source in this revision.",
    },
}

#: Volume must agree to this fraction, bbox to this many millimetres. OpenSCAD
#: is not byte-deterministic (mesh decimation and CGAL's triangulation both
#: wander), so the comparison has to be geometric or it is noise.
VOLUME_TOL = 0.001          # 0.1 %
BBOX_TOL_MM = 0.01


def describe() -> dict:
    """The registry as data, for `GET /api/checks`, the MCP and the docs."""
    return {
        "checks": [{"name": n, **spec} for n, spec in CHECKS.items()],
        "statuses": list(STATUSES),
        "policy": {m: required_for(m)
                   for m in ("concept", "design", "prototype", "released")},
        "tolerances": {"volume_fraction": VOLUME_TOL, "bbox_mm": BBOX_TOL_MM},
    }


# =============================================================================
# THE POLICY HOOK (§2.3) — the ONE home for "what must be green", and the ONE
# gate a promotion asks. Stage 7 wired it: `plm/policy.py` and `plm/release.py`
# used to carry their own copies of the required list, which is two more answers
# to a question that may only have one. Both now call `required_for()`, and
# `ops._release_progress` / `ops._finish_release` call `gate_for()`.
# =============================================================================

#: The maturities at which an unreleased BOM pin REFUSES rather than warns.
#: ⭐ ONE PREDICATE, THREE CALLERS — `required_for` below, `gitindex.approve_commit`
#: and `ops.release_gate_check`. Before stage 7 each drew the line itself and
#: `required_for` drew it in a different place (it required `pins-released` at
#: OBSOLETE, which neither of the other two blocked), so "warn at prototype,
#: block above it" was true three times over with three different meanings.
PINS_MUST_BE_RELEASED_AT = ("released", "production")


def pins_must_be_released(target_maturity: str) -> bool:
    """§5.1's line: below RELEASED an unreleased pin warns; at RELEASED it
    refuses. Retiring a revision (OBSOLETE) asks nothing of its children — you
    do not have to release a part in order to stop using it."""
    return (target_maturity or "").strip().lower() in PINS_MUST_BE_RELEASED_AT


def required_for(target_maturity: str) -> list[str]:
    """Which checks policy requires before a revision may reach `target`.

    §2.3's table, as data, and the **single home** for it: `plm/policy.py`'s
    `for_target()["checks"]` and `plm/release.py`'s `workflow_for()["checks"]`
    both return exactly this list rather than a copy of their own.

    There is deliberately no write path. "Which checks are required" is a
    property of the release level, not a setting a caller can post — so it
    cannot be relaxed by whoever is asking to be released.

    ⚠️ `pins-released` IS NOT REQUIRED AT PROTOTYPE. §2.3's table says "warn on
    unreleased pins" for prototype and "every pin on a release tag" only for
    released — which is the same line `gitindex.release_gate` already draws
    (PROTOTYPE warns, above it refuses). Requiring it at prototype read as red
    on a perfectly normal prototype assembly and would have taught everybody to
    ignore the board. §2.3's other prototype rows — "BOM resolves", "mesh
    present" — are what `manifest` and `files-present` already ask.
    """
    t = (target_maturity or "").strip().lower()
    req = ["manifest", "files-present"]
    if pins_must_be_released(t):
        req.append("pins-released")
    if t in ("released", "production"):
        req.append("render-matches-source")
    return req


def rollup(rows: list[dict], *, target_maturity: str = "",
           applicable: list[str] | None = None,
           extra_required: list[str] | None = None) -> dict:
    """`{state, required_missing, …}` for a set of rows — the chip, and the
    thing a release gate reads.

    - `red`   if any check failed or errored, **or** any REQUIRED check RAN and
      did not pass (a required `warn` or `skipped` is red — "we could not
      measure it" is not permission to release).
    - `pending` if anything is queued or running, **or** a required check has no
      row at all. ⚠️ NOT RUN IS NOT FAILED. The first version of this made a
      commit nobody had measured read `failed` in the approvals queue, which is
      an accusation about a part on no evidence. A release gate still refuses on
      anything but `green`, so nothing is let through by the distinction — it
      only stops the board lying about what it knows.
    - `amber` if something warned or was skipped but nothing required is hurt.
    - `green` otherwise.
    """
    req = required_for(target_maturity) if target_maturity else []
    if extra_required and (target_maturity or "").strip().lower() in (
            "released", "production"):
        # stage 19: the manifest's own `[release] required` may name a check
        req = req + [n for n in extra_required if n not in req]
    by = {r["name"]: r for r in rows}
    applicable = list(applicable if applicable is not None else by.keys())
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    missing, failed = [], []
    for name in req:
        if name not in applicable and name not in by:
            continue                       # does not apply to this commit
        row = by.get(name)
        if row is None or row["status"] in ("queued", "running"):
            missing.append(name)
        elif row["status"] != "pass":
            failed.append(name)

    pending = counts["queued"] + counts["running"] > 0
    if counts["fail"] or counts["error"] or failed:
        state = "red"
    elif pending or missing:
        state = "pending"
    elif counts["warn"] or counts["skipped"]:
        state = "amber"
    elif counts["pass"]:
        state = "green"
    else:
        state = "pending"
    return {
        "state": state,
        "target_maturity": (target_maturity or "").lower(),
        "required": req,
        "required_missing": missing,
        "required_failed": failed,
        "counts": counts,
        "total": len(rows),
        # Everything a refusal has to name. A REQUIRED check that did not pass
        # blocks because policy says so; a check that outright `fail`ed or
        # `error`ed blocks whether or not it was required, because a finding is
        # a finding. Marked `required` either way so the text can say which.
        "blocking": ([{"name": n, "status": by[n]["status"],
                       "summary": by[n]["summary"], "required": True}
                      for n in failed if n in by]
                     + [{"name": r["name"], "status": r["status"],
                         "summary": r["summary"], "required": False}
                        for r in rows
                        if r["status"] in ("fail", "error")
                        and r["name"] not in failed]),
    }


def rollup_for(conn, number: str, sha: str, *,
               target_maturity: str = "") -> dict:
    """⭐ THE ONE CALL a release/policy workflow makes. See the design notes."""
    rows = read(conn, number, sha)
    ctx = Ctx(number, sha)
    return rollup(rows, target_maturity=target_maturity,
                  applicable=applies_to(number, sha, ctx),
                  extra_required=manifest_required(ctx))


def manifest_required(ctx: "Ctx") -> list[str]:
    """Check names the manifest's `[release] required` makes REQUIRED at
    release (stage 19: `purchase-requirements`). Its other entries name
    release artifacts (§3), not checks, and are ignored here."""
    req = [str(x) for x in ((ctx.doc.get("release") or {}).get("required")
                            or [])]
    return [n for n in req if n in CHECKS]


# -- the gate: may this commit be promoted to that maturity? ------------------

def gate_for(conn, number: str, sha: str, *, target_maturity: str) -> dict:
    """The rollup, plus the yes/no a promotion acts on. **The only gate.**

    `plm/ops.py` calls this; nothing else decides whether checks permit a
    release. The verdict is the rollup's own colour:

      green   every required check ran and passed, nothing warned   -> promote
      amber   an ADVISORY check warned or was skipped               -> promote
      red     something failed, or a required check did not pass    -> refuse
      pending a required check has not run yet, or one is running   -> refuse

    ⚠️ WHY AMBER IS A YES. `rollup()` turns ANY warn or skip amber, including
    `stable-names` (registered `advisory`) and a `render-matches-source` that
    was skipped because the selector is prose a machine cannot reproduce.
    Refusing on amber would mean a lint warning blocks a release — a rule that
    would be routed around within a week, which is the same argument
    `required_for` makes about `pins-released` at prototype. What policy
    requires is in `required`; a required check that warns or is skipped is
    already RED by `rollup()`, so the strictness lives where it belongs.

    ⚠️ WHY PENDING IS A NO. "Nobody has measured it" is not "it is fine". The
    refusal says so and says to wait — it is the one blocker that clears itself.
    """
    roll = rollup_for(conn, number, sha, target_maturity=target_maturity)
    ok = roll["state"] in ("green", "amber")
    roll["gate"] = {
        "ok": ok, "state": roll["state"],
        "target_maturity": (target_maturity or "").lower(),
        "why": "" if ok else refusal(number, sha, roll),
    }
    return roll


def refusal(number: str, sha: str, roll: dict) -> str:
    """The 409. Names every blocking check, its own summary, and the command
    that re-runs it — a gate that only says "checks are not green" makes the
    person go and find the board themselves."""
    target = roll.get("target_maturity") or "this maturity"
    lines = []
    for b in roll.get("blocking") or []:
        lines.append(f"  · {b['name']} — {b['status']}: "
                     f"{b['summary'] or '(no summary)'}"
                     + ("" if b.get("required") else
                        "   [not required here, but it FAILED]"))
    for name in roll.get("required_missing") or []:
        lines.append(f"  · {name} — has not run yet "
                     f"({CHECKS.get(name, {}).get('cost', '')})")
    head = (f"{number} cannot be promoted to {target.upper()}: the checks on "
            f"{sha[:10]} are {roll['state'].upper()}.\n"
            f"Required at {target}: {', '.join(roll.get('required') or []) or '(none)'}\n")
    if roll["state"] == "pending":
        tail = (
            "\nNothing is WRONG — nobody has measured it yet. The runner is "
            "in-process and normally\ntakes seconds; look again, or start it:\n"
            f"  GET  /api/repo/{number}/checks?sha={sha}\n"
            f"  POST /api/repo/{number}/checks/run  "
            f'{{"sha": "{sha}"}}\n')
    else:
        tail = (
            "\nSee the board, or re-run them:\n"
            f"  GET  /api/repo/{number}/checks?sha={sha}\n"
            f"  POST /api/repo/{number}/checks/run  "
            f'{{"sha": "{sha}", "names": []}}   # [] = everything that applies\n')
    tail += ("\nA check is a measurement of ONE COMMIT "
             "(docs/PLM_REVIEW_MODEL.md §2.6). Pushing a fix\nmakes a new "
             "commit and a fresh set — the old verdict never follows the "
             "change.")
    return head + "\n".join(lines) + ("\n" if lines else "") + tail


# =============================================================================
# B. THE STORE
# =============================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL DEFAULT 0,
  number      TEXT    NOT NULL,
  sha         TEXT    NOT NULL,
  name        TEXT    NOT NULL,
  status      TEXT    NOT NULL DEFAULT 'queued',
  summary     TEXT    NOT NULL DEFAULT '',
  details_md  TEXT    NOT NULL DEFAULT '',
  facts_json  TEXT    NOT NULL DEFAULT '',
  points_json TEXT    NOT NULL DEFAULT '',
  images_json TEXT    NOT NULL DEFAULT '',   -- GitHub's output.images (§2.10)
  source      TEXT    NOT NULL DEFAULT '',
  started_at  REAL,
  ended_at    REAL,
  created_at  REAL    NOT NULL,
  UNIQUE(number, sha, name)
);
CREATE INDEX IF NOT EXISTS checks_sha  ON checks(sha);
CREATE INDEX IF NOT EXISTS checks_item ON checks(number, sha);
""" + """
-- A request for checks made by a process that does not run them (PLM_CHECKS=
-- relay, the MCP). The runner's sweep turns each into an ordinary job and
-- deletes it in the same transaction that writes the job's `queued` rows.
CREATE TABLE IF NOT EXISTS check_requests (
  id          INTEGER PRIMARY KEY,
  number      TEXT    NOT NULL,
  sha         TEXT    NOT NULL,
  names       TEXT    NOT NULL DEFAULT '',   -- JSON list; '' = all that apply
  item_id     INTEGER NOT NULL DEFAULT 0,
  source      TEXT    NOT NULL DEFAULT '',
  requested_by TEXT   NOT NULL DEFAULT '',   -- which process kind asked
  created_at  REAL    NOT NULL
);
"""

#: the table alone, as ONE statement — safe inside a caller's transaction
#: (executescript would COMMIT it)
_REQUESTS_DDL = _SCHEMA[_SCHEMA.index("CREATE TABLE IF NOT EXISTS check_requests"):]

_schema_done = False


def ensure_schema(conn=None) -> None:
    """Create the table from HERE, not from `plm/db.py`'s schema block.

    Deliberate: the feature is one module plus one route file, so it merges as
    an addition rather than as a hunk in the middle of the schema everything
    else in the system also edits. Idempotent; safe to call on every request.

    ⚠️ NEVER `executescript` ON A CALLER'S CONNECTION (stage 16b). It issues
    an implicit COMMIT first, and `read(conn)` — which calls this — runs inside
    other writers' transactions (`changes.view` during `submit_change`): every
    board read committed the submit half-way. Statement by statement on the
    caller's transaction, once per process.
    """
    global _schema_done
    if _schema_done:
        return
    from .proposals import _statements
    if conn is None:
        with db.connect() as c:
            for st in _statements(_SCHEMA):
                c.execute(st)
            _add_columns(c)
            c.commit()
        _schema_done = True
        return
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('checks','check_requests')")}
    if len(have) == 2 and not _add_columns(conn):
        _schema_done = True          # committed by somebody: safe to remember
        return
    for st in _statements(_SCHEMA):  # in the caller's transaction, which may
        conn.execute(st)             # roll back — so not remembered
    _add_columns(conn)


#: additive columns on a `checks` table created before them (db.py's pattern)
_ADDED = {"images_json": "TEXT NOT NULL DEFAULT ''"}


def _add_columns(conn) -> bool:
    """Add what an older `checks` table lacks; True when it changed anything."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(checks)")}
    if not cols:
        return False
    todo = [c for c in _ADDED if c not in cols]
    for c in todo:
        conn.execute(f"ALTER TABLE checks ADD COLUMN {c} {_ADDED[c]}")
    return bool(todo)


def _row_out(r) -> dict:
    d = dict(r)
    spec = CHECKS.get(d["name"], {})
    d["label"] = spec.get("label", d["name"])
    d["cost"] = spec.get("cost", "")
    d["advisory"] = bool(spec.get("advisory"))
    d["help"] = spec.get("help", "")
    d["facts"] = json.loads(d.pop("facts_json") or "null")
    d["points_at"] = json.loads(d.pop("points_json") or "[]")
    d["images"] = json.loads(d.pop("images_json", "") or "[]")
    st, en = d.get("started_at"), d.get("ended_at")
    d["duration_s"] = round(en - st, 2) if st and en else None
    return d


def read(conn, number: str, sha: str) -> list[dict]:
    """Every recorded check for one commit, in registry order."""
    ensure_schema(conn)
    rows = [_row_out(r) for r in conn.execute(
        "SELECT * FROM checks WHERE number=? AND sha=?", (number, sha))]
    order = list(CHECKS)
    rows.sort(key=lambda r: (order.index(r["name"])
                             if r["name"] in order else 99, r["name"]))
    return rows


# =============================================================================
# THE CONTEXT — everything a check is allowed to look at, read once per commit.
# =============================================================================

class Ctx:
    """One commit, opened once and handed to every check that runs on it.

    A check gets the repo, the parsed manifest and the tree; it does NOT get a
    database connection. A check measures a commit — the moment one could read
    the registry it could also disagree with `gitindex` about what the registry
    says, and then nobody knows which is right.
    """

    def __init__(self, number: str, sha: str):
        self.number = number
        self.sha = sha
        self.repo = gitrepo.repo_path(number)
        self.error = ""
        self.doc: dict = {}
        self.text = ""
        self.entries: dict = {}
        try:
            self.entries = gitvalidate.tree_entries(self.repo, sha)
        except Exception as exc:                     # a tree we cannot read
            self.error = str(exc)
        try:
            self.text = gitvalidate.git(
                self.repo, "cat-file", "-p", f"{sha}:plm.toml")
            self.doc = manifest.parse(self.text)
        except Exception as exc:
            self.error = self.error or str(exc)

    # -- what the tree says about itself --------------------------------------
    @property
    def gitlinks(self) -> dict:
        return {p: e.sha for p, e in self.entries.items()
                if e.mode == gitvalidate.MODE_GITLINK and p.startswith("parts/")}

    @property
    def external(self) -> dict:
        return {e.get("name", ""): e for e in (self.doc.get("files_external") or [])}

    def mesh_path(self) -> str:
        """The mesh this revision IS — `primary`, else the first mesh under any
        role. `manifest.primary_file` already owns that ordering for the DB
        side; here the manifest has already answered it, so `primary` is taken
        at its word and the fallback only covers a hand-written file."""
        p = self.doc.get("primary") or ""
        if p:
            return p
        for role in ("viz", "mesh", "artifact"):
            for cand in (self.doc.get("files") or {}).get(role, []) or []:
                if Path(cand).suffix.lower() in manifest.MESH_EXT:
                    return cand
        for cand, e in sorted(self.entries.items()):
            if (e.mode != gitvalidate.MODE_GITLINK
                    and Path(cand).suffix.lower() in manifest.MESH_EXT):
                return cand
        return ""

    def blob(self, path: str) -> bytes:
        # stage 17: a mesh committed through LFS is measured as the MESH, not
        # as its 130-byte pointer — every check reads geometry through here
        e = self.entries.get(path)
        if e is not None and e.size <= lfs.POINTER_MAX:
            ptr = lfs.blob_pointer(self.repo, e.sha, e.size)
            if ptr:
                return lfs.read(ptr[0])
        return gitvalidate.git(self.repo, "cat-file", "-p", f"{self.sha}:{path}",
                               binary=True)

    def blob_sha(self, path: str) -> str:
        e = self.entries.get(path)
        return e.sha if e else ""

    @property
    def libs(self) -> dict:
        """`lib/<N>` gitlinks: a shared SOURCE pin (stage 15)."""
        return {p: e.sha for p, e in self.entries.items()
                if e.mode == gitvalidate.MODE_GITLINK and p.startswith("lib/")}

    def lib_blob(self, path: str) -> str:
        """Blob sha of `lib/<N>/<rest>` inside the pinned source repo, or ''."""
        bits = path.split("/", 2)
        if len(bits) < 3 or bits[0] != "lib":
            return ""
        pin = self.libs.get(f"lib/{bits[1]}")
        if not pin:
            return ""
        try:
            child = gitrepo.repo_path(bits[1])
            out = gitvalidate.git(child, "rev-parse", "--verify", "--quiet",
                                  f"{pin}:{bits[2]}", check=False).strip()
        except Exception:                                      # noqa: BLE001
            return ""
        return out

    def has_path(self, path: str) -> bool:
        e = self.entries.get(path)
        if e is not None and e.mode != gitvalidate.MODE_GITLINK:
            return True
        return bool(self.lib_blob(path))

    def base(self) -> str:
        """The release this commit's line of work started from: the newest
        `v*` tag reachable from its PARENT (so a draft of several commits is
        measured against what it changes, not against its own previous
        push); the parent when no tag is reachable."""
        tag = gitvalidate.git(self.repo, "describe", "--tags", "--abbrev=0",
                              "--match", "v*", f"{self.sha}^",
                              check=False).strip()
        if tag:
            sha = gitvalidate.git(self.repo, "rev-parse", "--verify", "-q",
                                  f"{tag}^{{commit}}", check=False).strip()
            if sha:
                return sha
        return self.parent()

    def parent(self) -> str:
        out = gitvalidate.git(self.repo, "rev-list", "--parents", "-n", "1",
                              self.sha, check=False).split()
        return out[1] if len(out) > 1 else ""


def applies_to(number: str, sha: str, ctx: Ctx | None = None) -> list[str]:
    """Which checks exist for this commit at all. Answered from the TREE —
    or, for a pull request subject (`CHG-n`), the registry's `change` rows."""
    if subject(number).is_change:
        return [n for n, s in CHECKS.items() if s["applies"] == "change"]
    ctx = ctx or Ctx(number, sha)
    has_mesh = bool(ctx.mesh_path()) or any(
        Path(n).suffix.lower() in manifest.MESH_EXT for n in ctx.external)
    out = []
    for name, spec in CHECKS.items():
        a = spec["applies"]
        if a == "*":
            out.append(name)
        elif a == "assembly" and ctx.gitlinks:
            out.append(name)
        elif a in ("has-mesh", "has-source+mesh") and has_mesh:
            out.append(name)
        elif a == "has-coupon" and _has_claims(number, sha):
            out.append(name)
        elif a == "has-instructions" and any(
                p in ctx.entries for p in workflow_text.NAMES):
            out.append(name)
        elif a == "has-requirements" and requirements.lines(ctx.doc):
            out.append(name)
    return out


def _has_claims(number: str, sha: str) -> bool:
    from . import db, relations
    try:
        with db.connect() as conn:
            return bool(relations.claims_on(conn, number, sha))
    except Exception:  # noqa: BLE001 — a registry we cannot read: not applicable
        return False


# =============================================================================
# A. THE SIX CHECKS. Each: run(ctx) -> {status, summary, details_md, facts?,
#    points_at?}. Raising is allowed — the runner records `error` with the text.
# =============================================================================

def _ok(summary, details="", facts=None, points=None):
    return {"status": "pass", "summary": summary, "details_md": details,
            "facts": facts, "points_at": points or []}


def _skip(reason, details=""):
    return {"status": "skipped", "summary": reason, "details_md": details}


def check_manifest(ctx: Ctx) -> dict:
    """`plm.toml` is a legal revision document and the tree is a legal shape.

    Asks `plm/gitvalidate.py` — the same module the pre-receive hook asks — so
    this can never disagree with what a push would have refused. `_check_version`
    is deliberately NOT asked: bump legality is a fact about a *ref being
    written*, not about a commit, and re-asking it of an already-landed SHA
    would accuse a merged revision of claiming its own version.
    """
    def _fail(exc):
        return {"status": "fail", "summary": str(exc).split("\n", 1)[0],
                "details_md": f"```\n{exc}\n```",
                "facts": None, "points_at": [{"path": "plm.toml"}]}

    try:
        # (Stage 15: `_load_manifest` no longer refuses a tree whose lifecycle
        # fields say what plm wrote at release — they are plm's — so the old
        # "suppress the state clause on a tagged commit" exception is gone.)
        gitvalidate._load_manifest(ctx.repo, ctx.sha, ctx.number)
    except gitvalidate.Refused as exc:
        return _fail(exc)
    try:
        gitvalidate._check_shape(ctx.entries)
        nfiles, nbytes = gitvalidate._check_size(ctx.entries)
        gitvalidate._check_routing(ctx.doc)
    except gitvalidate.Refused as exc:
        return _fail(exc)
    d = ctx.doc
    # (no `state` in the summary: on a draft the file's copy is stale by
    # design — lifecycle fields are plm's, stage 15)
    return _ok(
        f"v{d.get('version') or '?'} · "
        f"{nfiles} file{'' if nfiles == 1 else 's'} · {nbytes:,} bytes",
        facts={"version": d.get("version", ""), "state": d.get("state", ""),
               "files": nfiles, "bytes": nbytes,
               "schema": d.get("schema", 0)})


def check_files_present(ctx: Ctx) -> dict:
    """Every declared path is in the tree or declared external."""
    try:
        gitvalidate._check_files(ctx.doc, ctx.entries, ctx.number)
    except gitvalidate.Refused as exc:
        msg = str(exc)
        pts = [{"path": p} for p in ctx.entries
               if p in msg] or [{"path": "plm.toml"}]
        return {"status": "fail", "summary": msg.split("\n", 1)[0],
                "details_md": f"```\n{msg}\n```", "facts": None,
                "points_at": pts}
    declared = sum(len(v) for k, v in (ctx.doc.get("files") or {}).items())
    ext = len(ctx.external)
    prim = ctx.doc.get("primary") or ""
    return _ok(
        f"{declared} declared path{'' if declared == 1 else 's'}"
        + (f" · {ext} external" if ext else "")
        + (f" · primary {Path(prim).name}" if prim else " · no primary"),
        facts={"declared": declared, "external": ext, "primary": prim})


# =============================================================================
# source-present — stage 15
# =============================================================================

_INCLUDE = re.compile(r"^\s*(include|use)\s*<([^>]+)>", re.M)


def _includes(ctx: Ctx, src: str, data: bytes) -> list[tuple[str, str]]:
    """(kind, target) for every `include <…>` / `use <…>` in an OpenSCAD file
    that does NOT resolve inside the revision — the source's own folder, the
    repo root, or a `lib/<N>` submodule (OpenSCAD's own search order, with the
    repo as the library path)."""
    bad = []
    base = str(Path(src).parent)
    for kind, target in _INCLUDE.findall(data.decode(errors="replace")):
        t = target.strip()
        cands = [os.path.normpath(os.path.join(base, t)), os.path.normpath(t)]
        cands += [os.path.normpath(f"{lp}/{t}") for lp in ctx.libs]
        if not any(not c.startswith("..") and not c.startswith("/")
                   and ctx.has_path(c) for c in cands):
            bad.append((kind, t))
    return bad


def check_source_present(ctx: Ctx) -> dict:
    src = (ctx.doc.get("source") or {}).get("file") or ""
    declared = list((ctx.doc.get("files") or {}).get("source") or [])
    if not src and not declared:
        return _skip("no `[source] file` and no `[files].source` — nothing here "
                     "says how this revision is regenerated")
    problems: list[str] = []
    points = [{"path": "plm.toml"}]
    if src and not ctx.has_path(src):
        problems.append(
            f"`[source] file = {src!r}` is not in this tree"
            + (f"; `[files].source` lists {', '.join(repr(d) for d in declared)}"
               if declared else "")
            + (" — name the file that IS here" if declared else
               " — add it (self-contained: a clone must be able to "
               "regenerate the revision) and say so in the change"))
    elif src and declared and src not in declared:
        problems.append(f"`[source] file = {src!r}` is not one of "
                        f"`[files].source` ({', '.join(declared)})")
    for d in declared:
        if not ctx.has_path(d):
            problems.append(f"`[files].source` names {d!r}, which is not in "
                            "this tree")
    unresolved: dict[str, list] = {}
    for f in ([src] if src else []) + [d for d in declared if d != src]:
        if Path(f).suffix.lower() != ".scad" or not ctx.has_path(f):
            continue
        try:
            if f in ctx.entries:
                data = ctx.blob(f)
            else:
                bits = f.split("/", 2)
                data = gitvalidate.git(gitrepo.repo_path(bits[1]), "cat-file",
                                       "-p", ctx.lib_blob(f), binary=True)
        except Exception:                                      # noqa: BLE001
            continue
        bad = _includes(ctx, f, data)
        if bad:
            unresolved[f] = bad
            points.append({"path": f})
    facts = {"source": src, "declared": declared,
             "unresolved_includes": {k: [t for _, t in v]
                                     for k, v in unresolved.items()}}
    advice: list[str] = []
    if unresolved:
        lines, holders_f, short = _include_advice(ctx, unresolved)
        facts["holders"] = holders_f
        problems += short
        advice = lines
    if problems:
        tail = ("\n\n" + "\n".join(advice)) if advice else (
            "\n\nThe fix is SELF-CONTAINED: a clone must be able to regenerate "
            "the revision. Point `[source] file` at the file that IS here, or "
            "add the missing one, and say so in the change.")
        return {"status": "warn",
                "summary": problems[0][:300] + (f" (+{len(problems) - 1} more)"
                                                if len(problems) > 1 else ""),
                "details_md": "\n".join(f"- {p}" for p in problems) + tail,
                "facts": facts, "points_at": points}
    return _ok(f"`{src or declared[0]}` is in the repo"
               + (" and every include resolves inside it" if src.endswith(".scad")
                  else ""), facts=facts)


def _tree_resolves(tree, where: str, target: str) -> bool:
    """Does `include <target>` written in file `where` resolve in `tree` (a
    path->blob map) — its own folder, then the repo root?"""
    base = str(Path(where).parent)
    for c in (os.path.normpath(os.path.join(base, target)),
              os.path.normpath(target)):
        if not c.startswith("..") and not c.startswith("/") and c in tree:
            return True
    return False


def _include_advice(ctx: Ctx, unresolved: dict) -> tuple[list, dict, list]:
    """Stage 16. For every include that does not
    resolve inside the repo: WHO holds a file of that name (any project — a
    library include crosses projects), whether their copies agree, whether a
    holder's copy is COMPLETE (its own includes resolve in its own repo), and
    the paste-able fix — a `lib/<N>` pin plus the include line, or, when no
    item holds it, "add the file and say so". Run 3 met
    `include <../../gate-status-indicator/cad/gate_oled_puck.scad>`, was told
    only "outside the repo", spent ten calls finding the file and COPIED it
    into three repos — exactly the duplicated shared source `lib/<N>` exists
    to prevent. Returns (detail lines, facts, one short problem per include)."""
    from . import db, sources
    names = sorted({Path(t).name for bad in unresolved.values() for _, t in bad})
    # pre-existing? the same include, unresolved, in the release this started from
    base_bad, base_v = set(), ""
    try:
        bsha = ctx.base()
        if bsha and bsha != ctx.sha:
            bctx = Ctx(ctx.number, bsha)
            base_v = str(bctx.doc.get("version") or bsha[:10])
            for f in unresolved:
                if f in bctx.entries:
                    base_bad |= {(f, t) for _, t in
                                 _includes(bctx, f, bctx.blob(f))}
    except Exception:                                          # noqa: BLE001
        pass
    with db.connect() as conn:
        found = sources.holders(conn, names, exclude={ctx.number})
        # one level down: what each holder's copy itself needs
        nested: dict[str, set] = {}
        for n, hs in found.items():
            for h in hs:
                if not h["tree"] or Path(h["path"]).suffix.lower() != ".scad":
                    continue
                try:
                    data = gitvalidate.git(gitrepo.repo_path(h["number"]),
                                           "cat-file", "-p", h["blob"],
                                           binary=True)
                except Exception:                              # noqa: BLE001
                    continue
                miss = {Path(t).name for _, t in
                        _INCLUDE.findall(data.decode(errors="replace"))
                        if not _tree_resolves(h["tree"], h["path"], t.strip())}
                h["missing"] = sorted(miss)
                nested.setdefault(n, set()).update(miss)
        more = sorted({m for s in nested.values() for m in s} - set(names))
        found_more = sources.holders(conn, more, exclude={ctx.number}) if more else {}

    lines, facts, short = [], {}, []
    for f, bad in unresolved.items():
        for kind, t in bad:
            n = Path(t).name
            hs = found.get(n) or []
            repo_hs = [h for h in hs if h["tree"]]
            cat_hs = [h for h in hs if not h["tree"]]
            blobs = sorted({h["blob"] for h in repo_hs})
            complete = [h for h in repo_hs if not h.get("missing")]
            pre = (f, t) in base_bad
            facts[t] = [{"number": h["number"], "kind": h["kind"],
                         "ref": h["ref"], "tag": h["tag"], "path": h["path"],
                         "blob": h["blob"][:12],
                         "missing": h.get("missing", [])} for h in hs]
            who = ", ".join(h["number"] for h in repo_hs[:3]) + (
                f" +{len(repo_hs) - 3}" if len(repo_hs) > 3 else "")
            short.append(
                f"`{f}` {kind}s `{n}` from outside the repo — "
                + (f"held by {who}"
                   + (f" ({len(blobs)} different copies)" if len(blobs) > 1 else "")
                   + ("" if complete else "; no copy is complete")
                   if repo_hs else "no item's repo holds it")
                + (f" · pre-existing (v{base_v} has it too)" if pre else ""))
            out = [f"**`{kind} <{t}>`** in `{f}`"
                   + (f" — pre-existing: v{base_v} has the same include, so it "
                      "is not your change's to fix. Say so in the change note; "
                      "if you do fix it, fix it as below." if pre else "")]
            for h in repo_hs[:8]:
                out.append(f"  - {h['number']} ({h['kind']}, {h['project'] or '—'}) "
                           f"`{h['ref']}`{' ' + h['tag'] if h['tag'] else ''}: "
                           f"`{h['path']}` blob {h['blob'][:10]}"
                           + (f" — needs {', '.join(h['missing'])}, which its "
                              "repo does not hold" if h.get("missing") else ""))
            if len(repo_hs) > 8:
                out.append(f"  - … and {len(repo_hs) - 8} more")
            for h in cat_hs[:3]:
                out.append(f"  - {h['number']} ({h['kind']}) registers `{h['path']}`"
                           " — a catalog item has no repo, so nothing can pin it")
            # the fix
            if len(blobs) > 1:
                out.append(f"  The copies DIFFER ({len(blobs)} versions of "
                           f"`{n}`)" + (" — pin the one this source was "
                                        "written against, and name it in the "
                                        "change." if complete else "."))
            best = sorted(complete, key=lambda h: (
                h["kind"] != "document", not h["tag"], h["number"]))
            if best:
                b = best[0]
                lib = f"lib/{b['number']}"
                rel = os.path.relpath(f"{lib}/{b['path']}",
                                      str(Path(f).parent) or ".")
                out += ["  Fix — PIN it, never copy it (a copy is one more "
                        "`shared-source` to drift):", "  ```",
                        f"  git submodule add ../{b['number']}.git {lib}",
                        f"  git -C {lib} checkout {b['tag'] or b['ref']}",
                        f"  # in {f}:  {kind} <{rel}>", "  ```"]
                if b["kind"] != "document":
                    out.append(f"  ({b['number']} is a {b['kind']} that vendors "
                               "it; the lasting home of a library is a "
                               "`document` item — docs/SHARED-SOURCES.md § "
                               "\"A library another project owns\".)")
            elif repo_hs:
                need = sorted({m for h in repo_hs for m in h.get("missing", [])})
                where = []
                for m in need:
                    mh = found_more.get(m) or []
                    where.append(f"`{m}` — " + (", ".join(
                        x["number"] + ("" if x["tree"] else " (no repo)")
                        for x in mh[:4]) if mh else "no item holds it"))
                out.append("  No item holds a COMPLETE copy — every copy needs "
                           + "; ".join(where) + ". Pinning one would not "
                           "render either: the library needs a home first (a "
                           "`document` item with the file and what it "
                           "includes — docs/SHARED-SOURCES.md § \"A library "
                           "another project owns\"). That is a decision for "
                           "the human: name it in the change, do NOT copy the "
                           "files into this repo.")
            else:
                out.append("  No item holds it: add the file to this repo, "
                           f"point the `{kind}` at it, and say so in the change.")
            lines += out + [""]
    return lines, facts, short


def check_pins_released(ctx: Ctx) -> dict:
    """§5.1, via `gitindex.release_gate` — the SAME function the approval asks.

    ⚠️ THE STATUS IS THE FACT, NOT THE VERDICT. An unreleased pin is a `warn`
    here whatever the target, because the check does not know what maturity is
    being asked for. `required_for('released')` puts this check in the required
    set, and `rollup()` turns a required warn RED — so one row reads amber on a
    prototype and blocking on a release, which is exactly §2.3's table.
    """
    bad = gitindex.release_gate(ctx.repo, ctx.sha)
    n = len(ctx.gitlinks)
    if not bad:
        return _ok(f"{n} pin{'' if n == 1 else 's'}, all on release tags",
                   facts={"pins": n, "unreleased": 0})
    lines = "\n".join(
        f"- `{b['path']}` → **{b['version'] or b['pin']}** — not a release"
        for b in bad)
    fix = "\n".join(f"    git -C {b['path']} checkout v<x.y.z>" for b in bad)
    return {
        "status": "warn",
        "summary": f"{len(bad)} of {n} pin{'' if n == 1 else 's'} "
                   f"{'is' if len(bad) == 1 else 'are'} not a release: "
                   + ", ".join(b["child"] for b in bad[:4]),
        "details_md":
            "You cannot release above your least-mature child (§5.1).\n\n"
            + lines
            + "\n\nBelow RELEASED this warns. At RELEASED the approval is "
              "refused until every pin is a release tag:\n\n```\n" + fix
            + "\n```",
        "facts": {"pins": n, "unreleased": len(bad),
                  "children": [b["child"] for b in bad]},
        "points_at": [{"path": b["path"]} for b in bad],
    }


def _revision_at(conn, ctx: Ctx, version: str) -> tuple[dict | None, str]:
    """The registry revision whose rows ARE commit `ctx.sha`, or (None, why)."""
    from . import db, gitvalidate, ops
    item = db.resolve_item(conn, ctx.number)
    if not item:
        return None, f"{ctx.number} is not an item in the registry"
    row = conn.execute("SELECT * FROM revisions WHERE item_id=? AND git_commit=?",
                       (item["id"], ctx.sha)).fetchone()
    if row:
        return dict(row), ""
    rev = db.find_revision(conn, ctx.number, version)
    if not rev:
        return None, (f"{ctx.number} v{version} is not in the registry (yet) — "
                      "the explode rule is answered from its rows")
    origin = ops.git_origin(conn, dict(rev) | {"number": ctx.number})
    if not origin:
        # A legacy revision: its repo refs are GENERATED from its rows, so the
        # rows answer for the commit the repo currently names for it.
        here = {gitvalidate.releases(ctx.repo).get(version, "")}
        tips = gitvalidate.draft_branches(ctx.repo)
        here |= {tips.get(ref, "") for ref, v in
                 gitvalidate.draft_versions(ctx.repo).items() if v == version}
        if ctx.sha in here:
            return dict(rev), ""
    now = (origin or {}).get("commit") or rev.get("git_commit") or ""
    return None, (f"commit {ctx.sha[:10]} is no longer indexed: the registry's "
                  f"{ctx.number} v{version} is "
                  + (f"commit {now[:10]}" if origin else "a newer projection")
                  + " (re-pushed since?). The explode rule is answered from the "
                  "rows of the commit it measures, and this one has none — run "
                  "the checks on the current head.")


def check_explodes(ctx: Ctx) -> dict:
    """Master's explode gate (2026-09-24), as a row on the board.

    ⚠️ THE ONE CHECK THAT READS THE REGISTRY, and why. `Ctx` deliberately has no
    database connection — a check measures a commit. But "does this explode?"
    is DEFINED as what the viewer's resolver (`thumbs.explode_status`) does with
    the registry's rows, and the submit gate asks exactly that function. A
    re-implementation over the tree would be the explode rule's second home,
    which is the fault every Critical in the security review turned out to be.
    So this opens its own read connection, finds the revision whose rows ARE
    this commit, and asks the one function.

    BY SHA, NOT BY VERSION (stage 9b). The check is keyed by commit; so is its
    subject. A re-pushed draft keeps its version and gets a new commit, and the
    registry indexes only the newest — looking the revision up by version made
    a check on the OLDER sha measure the newer rows. Now: the revision whose
    `git_commit` is this sha; else a legacy (split-replayed) revision whose
    repo ref for that version IS this sha (its `git_commit` is a vault sha, and
    the registry row is what that ref was generated from); else `skipped`,
    saying that commit is no longer indexed.

    THE ROW IS THE FACT, THE SUBMIT GATE IS THE VERDICT — `pins-released`'s
    rule. A non-exploding assembly of made children is a `warn` here, never a
    `fail`: a `fail` blocks a PROMOTION through `rollup()`, and master's rule
    gates SUBMISSION, where a written waiver lets a deliberate exception
    through. Making the board stricter than the gate would have quietly changed
    the semantics the brief said to keep.
    """
    from . import db, ops, thumbs
    version = str(ctx.doc.get("version") or "")
    if not version:
        return _skip("plm.toml names no version, so there is no revision to "
                     "ask the viewer about")
    reg = None
    with db.connect() as conn:
        rev, why = _revision_at(conn, ctx, version)
        if not rev:
            return _skip(why)
        st = thumbs.explode_status(conn, rev["id"])
        item = conn.execute("SELECT * FROM items WHERE id=?",
                            (rev["item_id"],)).fetchone()
        # stage 15: in `write` the submit gate compares with the base, so the
        # row says which of the two this is — the board must not tell an agent
        # it needs a waiver the gate will not ask for.
        if (st["required"] and not st["explodable"] and item
                and ops.git_writes(conn, item)):
            reg = thumbs.explode_regression(conn, rev["id"])
    kids = st.get("children") or []
    facts = {"explodable": st["explodable"], "bodies": st["bodies"],
             "required": st["required"],
             "children": {k["number"]: k["status"] for k in kids}}
    if st["explodable"]:
        return _ok(st["reason"], facts=facts)
    fixes = [k for k in kids if k.get("fix")]
    details = (f"{st['reason']}.\n\n"
               + "\n".join(f"- `{k['number']}` — {k['fix']}" for k in fixes)
               + f"\n\nRule: {st['rule']}.")
    if not st["required"]:
        return {"status": "skipped",
                "summary": "fewer than two made children — the explode gate "
                           "does not apply: " + st["reason"],
                "details_md": details, "facts": facts}
    if reg and reg["pre_existing"]:
        facts["pre_existing"] = True
        facts["base_version"] = reg["base_version"]
        return {"status": "warn",
                "summary": f"does not explode — pre-existing (v"
                           f"{reg['base_version']} did not either): "
                           + st["reason"],
                "details_md": details + "\n\n"
                + thumbs.explode_sentence(ctx.number, version, reg),
                "facts": facts,
                "points_at": [{"path": f"parts/{k['number']}"} for k in fixes]}
    if reg:
        facts["regression"] = True
        facts["worse"] = [k["number"] for k in reg["worse"]]
    return {"status": "warn",
            "summary": "does not explode — " + (
                ("this change made it worse (v" + reg["base_version"]
                 + (" exploded" if reg["base_explodable"] else
                    ": " + ", ".join(k["number"] for k in reg["worse"][:4])
                    + " did not block") + "): ")
                if reg and reg["base_version"] else "") + st["reason"],
            "details_md": details + "\n\nSubmitting this needs "
                        "`explode_waiver='<why>'` (recorded on the approval).",
            "facts": facts,
            "points_at": [{"path": f"parts/{k['number']}"} for k in fixes]}


def check_verified_by_coupon(ctx: Ctx) -> dict:
    """Which coupons claim to have tested THIS commit, and what they found.

    ⚠️ A SECOND CHECK THAT READS THE REGISTRY, and for the reason `explodes`
    does: the claim is not in this commit. A coupon declares `coupon_for` in
    ITS OWN revision's plm.toml (the claimant's home, stage 10), so "verified
    by" is the DERIVED inverse — the registry's index over every coupon repo,
    exactly a forge's "Used by". `relations.claims_on` is that index, asked by
    commit: a claim pinned at this commit, or at the commit plm's release
    commit on top of it was reviewed as (`relations.same_design`).

    The row states the fact; the release CHECKLIST is where it is used (a
    passing coupon attests "Fit-checked…" by reference, plm/release.py). So:
    any `fail` with no `pass` is a `fail` (a coupon that failed against the
    exact bytes being released is a reason not to release them); a `pass`
    and a `fail` together is a `warn` naming both — the board does not decide
    which print to believe; claims with no result yet are a `warn`.
    """
    from . import db, relations
    with db.connect() as conn:
        claims = relations.claims_on(conn, ctx.number, ctx.sha)
    if not claims:
        return _skip("no coupon claims this commit — a coupon declares "
                     "coupon_for in its own revision, pinned at a commit")
    rows = [f"| `{c['ref']}` | {c['result'] or '—'} | "
            f"`{c['commit'][:10]}`{'' if c['exact'] else ' (reviewed commit)'}"
            f" | {(c['result_note'] or c['note'])[:120]} |" for c in claims]
    details = ("| coupon | result | pinned | note |\n|---|---|---|---|\n"
               + "\n".join(rows)
               + "\n\nDerived: each line is recorded in the COUPON's repo "
                 "(its plm.toml), not here.")
    facts = {"claims": [{"ref": c["ref"], "result": c["result"],
                         "commit": c["commit"], "exact": c["exact"],
                         "measured": c["measured"]} for c in claims]}
    passes = [c["ref"] for c in claims if c["result"] == "pass"]
    fails = [c["ref"] for c in claims if c["result"] == "fail"]
    if fails and not passes:
        return {"status": "fail", "facts": facts, "details_md": details,
                "summary": f"failed by {', '.join(fails)} — a coupon tested "
                           "this commit and it did not pass"}
    if fails:
        return {"status": "warn", "facts": facts, "details_md": details,
                "summary": f"conflicting: pass from {', '.join(passes)}, "
                           f"fail from {', '.join(fails)}"}
    if passes:
        return _ok(f"verified by {', '.join(passes)}", details, facts)
    return {"status": "warn", "facts": facts, "details_md": details,
            "summary": f"{len(claims)} coupon claim(s) on this commit, none "
                       "with a result yet (record it: result = pass|fail)"}


#: `gitvalidate._lint` is the ONE implementation of "this will hurt later". It
#: returns a flat list of warnings; this is the sentence its filename rule opens
#: with, and it is how the stable-names finding is told from the other lints.
#: (Coupling stated out loud rather than by re-implementing the regex here —
#: the design notes lists it as a known sharp edge.)
_NAME_LINT = "a NEW tracked filename"


def check_stable_names(ctx: Ctx) -> dict:
    """§6c-1 — a new tracked file must not carry the version or the number."""
    prev = {}
    p = ctx.parent()
    if p:
        try:
            prev = gitvalidate.tree_entries(ctx.repo, p)
        except Exception:
            prev = {}
    ver = ctx.doc.get("version", "")
    lint = gitvalidate._lint(ctx.doc, ctx.entries, prev, ctx.number, ver)
    introduced = [ln for ln in lint if _NAME_LINT in ln]
    other = [ln for ln in lint if _NAME_LINT not in ln]
    # ⭐ THE SAME RULE, ASKED OF THE WHOLE TREE. `_lint` only flags names this
    # commit INTRODUCED, which is right for a push (you cannot fix history at
    # the door). A standing check on a revision is a different question — "does
    # this part's tree carry versioned filenames?" — and it is answered by
    # running gitvalidate's own rule with an empty `previous`, so every tracked
    # path counts as new. One implementation of the regex, two populations.
    everything = [ln for ln in
                  gitvalidate._lint(ctx.doc, ctx.entries, {}, ctx.number, ver)
                  if _NAME_LINT in ln]
    path_of = lambda ln: ln.split(":", 1)[0]                        # noqa: E731
    flagged = [path_of(ln) for ln in everything if path_of(ln) in ctx.entries]
    new_flagged = {path_of(ln) for ln in introduced}

    details = ""
    if introduced:
        details += ("**Introduced by this commit**\n\n"
                    + "\n\n".join(introduced))
    inherited = [p for p in flagged if p not in new_flagged]
    if inherited:
        details += ("\n\n**Already in the tree** (same rule, older commits) —\n"
                    + "".join(f"\n- `{p}`" for p in inherited))
    if other:
        details += ("\n\n---\n\n**Other manifest lint on this commit**\n\n"
                    + "\n\n".join(other))
    facts = {"flagged": len(flagged), "introduced": len(new_flagged),
             "tracked": len(ctx.entries), "other_lint": len(other),
             "paths": flagged}
    if not flagged:
        return _ok(
            f"{len(ctx.entries)} tracked path"
            f"{'' if len(ctx.entries) == 1 else 's'}, none carry a version"
            + (f" · {len(other)} other lint" if other else ""),
            details, facts)
    return {"status": "warn",
            "summary": f"{len(flagged)} filename"
                       f"{' carries' if len(flagged) == 1 else 's carry'} the "
                       "version or the item number"
                       + (f" ({len(new_flagged)} new here)" if new_flagged
                          else " (none new here)"),
            "details_md": details, "facts": facts,
            "points_at": [{"path": p} for p in flagged[:12]]}


# =============================================================================
# geometry-facts — a stdlib STL reader, and a density registry that GLOSSES
# =============================================================================

#: g/cm³. A registry that glosses, not one that gates: an unknown material is a
#: warning and a volume, never a refusal and never a silently-assumed density.
#: Matched by the LONGEST substring of the lower-cased material string, so
#: "Elegoo PLA (white)" is PLA and "6063-T5 aluminum" is aluminium — the
#: registry holds what people actually type, which is the whole point of
#: glossing. Add a row; nothing else changes.
DENSITIES: dict[str, float] = {
    # polymers (filament and resin)
    "pla": 1.24, "petg": 1.27, "pet": 1.38, "abs": 1.04, "asa": 1.07,
    "tpu": 1.21, "nylon": 1.14, "pa12": 1.01, "pa6": 1.14, "pc": 1.20,
    "polycarbonate": 1.20, "hips": 1.04, "pva": 1.23, "pp": 0.90,
    "hdpe": 0.95, "ldpe": 0.92, "pom": 1.41, "delrin": 1.41,
    "acrylic": 1.18, "pmma": 1.18, "resin": 1.15, "nylon 6": 1.14,
    # metals
    "mild steel": 7.85, "stainless steel": 7.90, "stainless": 7.90,
    "steel": 7.85, "aluminium": 2.70, "aluminum": 2.70, "brass": 8.50,
    "bronze": 8.80, "copper": 8.96, "titanium": 4.51, "zinc": 7.14,
    "cast iron": 7.20,
    # other
    "plywood": 0.60, "pine": 0.50, "oak": 0.75, "glass": 2.50,
}


def density_for(material: str) -> tuple[float, str]:
    """(g/cm³, the registry key that matched) — or (0.0, "") if nothing does."""
    m = (material or "").lower()
    best = ""
    for key in DENSITIES:
        if key in m and len(key) > len(best):
            best = key
    return (DENSITIES[best], best) if best else (0.0, "")


class MeshError(ValueError):
    pass


def read_stl(data: bytes) -> list[tuple]:
    """Triangles from a binary OR ASCII STL — stdlib only, no numpy.

    ⚠️ THE FORMAT IS DECIDED BY THE BYTE COUNT, NOT BY THE WORD "solid". Plenty
    of binary STLs start their 80-byte header with "solid" (several exporters
    do it), and plenty of ASCII ones do not start with it at all. The only
    reliable test is arithmetic: a binary STL is exactly 84 + 50·n bytes.
    """
    if len(data) >= 84:
        (n,) = struct.unpack_from("<I", data, 80)
        if 0 < n and len(data) == 84 + 50 * n:
            tris = []
            off = 84
            for _ in range(n):
                v = struct.unpack_from("<9f", data, off + 12)
                tris.append(((v[0], v[1], v[2]), (v[3], v[4], v[5]),
                             (v[6], v[7], v[8])))
                off += 50
            return tris
    # ASCII
    text = data.decode("utf-8", errors="replace")
    if "facet" not in text:
        raise MeshError(
            "not an STL plm can read: the byte count does not match a binary "
            "STL (84 + 50·n) and there is no `facet` keyword for the ASCII "
            "reading.")
    nums = re.compile(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)")
    verts = []
    for m in nums.finditer(text):
        try:
            verts.append(tuple(float(x) for x in m.groups()))
        except ValueError:
            raise MeshError(f"unparseable vertex on this ASCII STL: "
                            f"{m.group(0)[:60]!r}") from None
    if len(verts) < 3 or len(verts) % 3:
        raise MeshError(f"{len(verts)} vertices in this ASCII STL — not a "
                        "whole number of triangles.")
    return [tuple(verts[i:i + 3]) for i in range(0, len(verts), 3)]


def mesh_facts(data: bytes, *, edges: bool = True) -> dict:
    """bbox · signed-tetrahedron volume · triangle count · open-edge count.

    `edges=False`: bbox and volume only (no area, no edge map) — the
    physical-diff tile's caption, on a mesh nothing else has measured.

    Volume is the classic divergence-theorem sum — Σ v0·(v1×v2)/6 over the
    triangles — which is exact for a closed, consistently-wound surface and
    meaningless for one that is neither. That is precisely why the open-edge
    count travels beside it: in a watertight mesh every edge is shared by
    exactly two triangles, so any other count is the number that says "do not
    trust the volume above". Vertices are quantised to 1e-4 mm before the edges
    are counted, because STL stores float32 and two triangles that meet
    perfectly in CAD do not meet bit-for-bit here.
    """
    tris = read_stl(data)
    if not tris:
        raise MeshError("this STL has no triangles.")
    inf = float("inf")
    lo = [inf, inf, inf]
    hi = [-inf, -inf, -inf]
    vol6 = 0.0
    area = 0.0
    seen: dict[tuple, int] = {}
    q = lambda p: (round(p[0], 4), round(p[1], 4), round(p[2], 4))  # noqa: E731
    for a, b, c in tris:
        for p in (a, b, c):
            for i in range(3):
                if p[i] < lo[i]:
                    lo[i] = p[i]
                if p[i] > hi[i]:
                    hi[i] = p[i]
        cx = b[1] * c[2] - b[2] * c[1]
        cy = b[2] * c[0] - b[0] * c[2]
        cz = b[0] * c[1] - b[1] * c[0]
        vol6 += a[0] * cx + a[1] * cy + a[2] * cz
        if not edges:
            continue
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        area += 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)
        qa, qb, qc = q(a), q(b), q(c)
        for e in ((qa, qb), (qb, qc), (qc, qa)):
            k = e if e[0] <= e[1] else (e[1], e[0])
            seen[k] = seen.get(k, 0) + 1
    out = {
        "triangles": len(tris),
        "volume_mm3": round(abs(vol6) / 6.0, 4),
        "bbox_mm": [round(hi[i] - lo[i], 4) for i in range(3)],
        "min_mm": [round(v, 4) for v in lo],
        "max_mm": [round(v, 4) for v in hi],
        "inverted": vol6 < 0,
    }
    if edges:
        open_edges = sum(1 for v in seen.values() if v != 2)
        out.update(area_mm2=round(area, 3), open_edges=open_edges,
                   watertight=open_edges == 0)
    return out


def _declared(ctx: Ctx) -> dict:
    """`mass_g` / `material` / `bbox_*_mm` as the manifest declares them."""
    out = {"mass_g": None, "material": "", "bbox": [None, None, None]}
    attrs = ctx.doc.get("attributes") or {}
    for i, k in enumerate(("bbox_x_mm", "bbox_y_mm", "bbox_z_mm")):
        try:
            out["bbox"][i] = float(attrs[k]["value"])
        except (KeyError, TypeError, ValueError):
            pass
    for k in ("mass_g", "material"):
        try:
            v = attrs[k]["value"]
            out[k] = float(v) if k == "mass_g" else str(v)
        except (KeyError, TypeError, ValueError):
            pass
    return out


def check_geometry_facts(ctx: Ctx) -> dict:
    path = ctx.mesh_path()
    if not path:
        return _skip("no mesh is declared on this revision — nothing to measure")
    if path not in ctx.entries:
        where = ctx.external.get(path) or ctx.external.get(Path(path).name)
        if where:
            return _skip(
                f"`{path}` lives in {where.get('backend') or 'the object store'}, "
                "not in this tree",
                "A `[[files.external]]` mesh is not part of the commit, so "
                "there is nothing at this SHA to measure, and a check must not "
                "reach outside the bytes it is judging.")
        return _skip(f"`{path}` is declared but not in this tree")
    if Path(path).suffix.lower() != ".stl":
        return _skip(
            f"`{Path(path).name}` is {Path(path).suffix or 'extensionless'}, and "
            "plm's stdlib reader only measures STL",
            "3MF is a zip of XML and STEP is a B-rep; neither is readable "
            "without a dependency this POC does not take. Declare an STL "
            "under `primary` if you want it measured.")
    try:
        f = mesh_facts(ctx.blob(path))
    except MeshError as exc:
        return {"status": "fail", "summary": str(exc).split("\n")[0],
                "details_md": str(exc), "facts": None,
                "points_at": [{"path": path}]}

    dec = _declared(ctx)
    material = dec["material"] or ""
    dens, key = density_for(material)
    vol_cm3 = f["volume_mm3"] / 1000.0
    solid_g = round(vol_cm3 * dens, 2) if dens else None
    bb = f["bbox_mm"]
    facts = {**f, "path": path, "volume_cm3": round(vol_cm3, 4),
             "material": material, "density_g_cm3": dens or None,
             "density_key": key, "solid_mass_g": solid_g,
             "declared_mass_g": dec["mass_g"], "declared_bbox_mm": dec["bbox"]}

    warns: list[str] = []
    # -- bbox ---------------------------------------------------------------
    for i, axis in enumerate("XYZ"):
        d = dec["bbox"][i]
        if d is None:
            continue
        tol = max(0.05, abs(d) * 0.005)     # 0.05 mm or 0.5 %, whichever is bigger
        if abs(d - bb[i]) > tol:
            warns.append(
                f"**bbox {axis}** — declared `{d:g} mm`, measured "
                f"`{bb[i]:g} mm` (Δ {d - bb[i]:+.3f} mm).")
    # -- mass ---------------------------------------------------------------
    # ⭐ A PRINTED PART IS NOT SOLID, so "declared ≠ measured" is not by itself
    # a finding: a slicer's grams are the geometry's volume times an infill
    # fraction, and a check that warned about that would warn about every good
    # part in the registry. The measurement is therefore read as a CEILING for
    # anything printed (or of unknown process — most of this registry) and as
    # an equality only for `process = "make"`, where the part really is solid
    # stock. The absurd case the board exists to catch — a declared mass above
    # the solid mass — is impossible either way and is the loud one.
    process = (ctx.doc.get("process") or "").lower()
    fill = (dec["mass_g"] / solid_g) if (dec["mass_g"] and solid_g) else None
    if dec["mass_g"] is None:
        pass
    elif not dens:
        warns.append(
            f"**mass** — `mass_g = {dec['mass_g']:g}` is declared but "
            f"`material = {material or '(none)'!r}` is not in plm's density "
            "registry, so there is nothing to check it against. "
            "`plm.checks.DENSITIES` is a dict — add the row.")
    elif dec["mass_g"] > solid_g * 1.02:
        warns.append(
            f"**mass** — declared `{dec['mass_g']:g} g`, but this geometry is "
            f"only `{f['volume_mm3']:,.0f} mm³` = `{vol_cm3:.2f} cm³`, which at "
            f"{dens:g} g/cm³ ({key}) is **`{solid_g:g} g` SOLID**. A part "
            f"cannot weigh more than its own volume — that is "
            f"**{fill:.0%} of solid**.")
    elif process == "make" and abs(dec["mass_g"] - solid_g) > solid_g * 0.05:
        warns.append(
            f"**mass** — declared `{dec['mass_g']:g} g`, measured "
            f"`{solid_g:g} g` solid at {dens:g} g/cm³ ({key}). This revision is "
            "`process = \"make\"`, so it is cut from solid stock and the two "
            "should agree within 5 %.")
    elif fill is not None and fill < 0.03:
        warns.append(
            f"**mass** — declared `{dec['mass_g']:g} g` against a solid mass of "
            f"`{solid_g:g} g` implies **{fill:.1%} fill**, which is below "
            "anything a slicer produces.")
    # -- stage 15: measured values that went STALE ------
    stale, paste = _stale_measured(ctx, path, f, dens, key, solid_g)
    if stale:
        facts["stale_attributes"] = [x["name"] for x in stale]
        facts["paste"] = paste
        # the bbox lines above are the same fact said twice — keep the stale one
        stale_axes = {x["name"] for x in stale}
        warns = [w for w in warns if not any(
            w.startswith(f"**bbox {a}**") and f"bbox_{a.lower()}_mm" in stale_axes
            for a in "XYZ")]
        warns.insert(0, _stale_text(ctx, stale, paste))
    # ⚠️ OPEN EDGES ARE A FACT ABOUT THE MESH, NOT A DISAGREEMENT (stage 16,
    # finding 4). They used to be appended to `warns`, so an
    # assembly viz with 2 open edges and NOTHING declared read "1 disagreement"
    # — and the bot had to explain to the human that there was nothing to fix.
    # Now: the summary says what it is ("not watertight — N open edges"); it
    # WARNS only where it matters — a part (something a slicer will print), or a
    # declared value that rests on the volume (mass, volume). An assembly's
    # union viz with nothing declared is a pass that states the fact.
    leaky = ""
    if not f["watertight"]:
        n_open = f["open_edges"]
        leaky = (f"**not watertight — {n_open} open "
                 f"edge{'' if n_open == 1 else 's'}**: the volume above (and "
                 "any mass derived from it) is an estimate, not a measurement. "
                 "A slicer will have its own opinion about how to fill it.")
    rests_on_volume = (dec["mass_g"] is not None or any(
        k in (ctx.doc.get("attributes") or {})
        for k in ("mesh_volume_mm3", "volume_mm3", "volume_cm3")))
    leak_warns = bool(leaky) and (not ctx.gitlinks or rests_on_volume)

    size = " × ".join(f"{v:g}" for v in bb)
    lead = (f"{size} mm · {vol_cm3:.2f} cm³ · {f['triangles']:,} triangles"
            + (f" · {solid_g:g} g solid ({key})" if solid_g else "")
            + (f" · declared {dec['mass_g']:g} g = {fill:.0%} fill"
               if fill is not None and process != "make" else ""))
    if leaky:
        lead += (f" · not watertight ({f['open_edges']} open "
                 f"edge{'' if f['open_edges'] == 1 else 's'})")
    table = (
        "| | measured | declared |\n|---|---|---|\n"
        + "".join(
            f"| bbox {a} | {bb[i]:g} mm | "
            f"{(str(dec['bbox'][i]) + ' mm') if dec['bbox'][i] is not None else '—'} |\n"
            for i, a in enumerate("XYZ"))
        + f"| volume | {vol_cm3:.3f} cm³ | — |\n"
        + f"| surface area | {f['area_mm2']:,.1f} mm² | — |\n"
        + f"| triangles | {f['triangles']:,} | — |\n"
        + f"| open edges | {f['open_edges']} | — |\n"
        + (f"| mass ({key} @ {dens:g} g/cm³) | {solid_g:g} g solid | "
           f"{(str(dec['mass_g']) + ' g') if dec['mass_g'] is not None else '—'} |\n"
           if dens else
           f"| mass | volume only — `{material or '(no material)'}` is not in "
           "the density registry | "
           f"{(str(dec['mass_g']) + ' g') if dec['mass_g'] is not None else '—'} |\n"))
    details = (f"`{path}`\n\n{table}\n"
               "These are offered as *facts*, not written back: an attribute "
               "with `source = check` is a notes-ref write and belongs to the "
               "release/notes path (§2.1b), not to the thing that measured it.")
    if warns:
        details = ("\n\n".join(warns + ([leaky] if leaky else []))
                   + "\n\n---\n\n" + details)
        return {"status": "warn",
                "summary": lead + f" — {len(warns)} disagreement"
                                  f"{'' if len(warns) == 1 else 's'} with "
                                  "what is declared",
                "details_md": details, "facts": facts,
                "points_at": [{"path": path}, {"path": "plm.toml"}]}
    if leak_warns:
        return {"status": "warn", "summary": lead,
                "details_md": leaky + "\n\n---\n\n" + details, "facts": facts,
                "points_at": [{"path": path}]}
    if leaky:
        details = (leaky + " Nothing declared rests on the volume and this is "
                   "an assembly's viz, so it is stated, not warned.\n\n---\n\n"
                   + details)
    if not dens and material:
        return {"status": "warn",
                "summary": lead + f" — `{material}` is not in the density "
                                  "registry, so mass was not computed",
                "details_md": details, "facts": facts,
                "points_at": [{"path": path}]}
    return _ok(lead, details, facts, [{"path": path}])


#: Attributes a MEASUREMENT produced. plm can re-measure the first group from
#: the mesh; the second comes from a slicer, which plm does not run.
MEASURABLE = {"bbox_x_mm": "bbox0", "bbox_y_mm": "bbox1", "bbox_z_mm": "bbox2",
              "mesh_volume_mm3": "volume_mm3", "volume_mm3": "volume_mm3",
              "volume_cm3": "volume_cm3", "mesh_watertight": "watertight",
              "triangles": "triangles", "mesh_triangles": "triangles"}
SLICED = ("mass_g", "print_time_min", "filament_g", "filament_m",
          "filament_mm", "filament_length_m")


def _stale_measured(ctx: Ctx, path: str, f: dict, dens, key: str,
                    solid_g) -> tuple[list[dict], list[str]]:
    """Measured attributes still DECLARED BEFORE THIS GEOMETRY: the mesh blob
    changed since the base release, and the attribute's value AND source did
    not. A measurable one only counts when it is now WRONG (a bbox that
    survives an internal change is still true); a slicer one always counts —
    plm cannot re-slice. Measured against the BASE release (`Ctx.base`), so
    pasting the measurable lines and pushing again clears them while an
    untouched slicer value keeps saying so. Returns (stale rows, paste-able
    manifest lines)."""
    par = ctx.base()
    if not par:
        return [], []
    try:
        pent = gitvalidate.tree_entries(ctx.repo, par)
        ptxt = gitvalidate.git(ctx.repo, "cat-file", "-p", f"{par}:plm.toml",
                               check=False)
        pdoc = manifest.parse(ptxt) if ptxt else {}
    except Exception:                                          # noqa: BLE001
        return [], []
    pmesh = (pdoc.get("primary") or "") if pdoc else ""
    pblob = (pent.get(pmesh) or pent.get(path))
    if pblob is not None and pblob.sha == ctx.blob_sha(path):
        return [], []                        # the geometry did not change
    before = (pdoc or {}).get("attributes") or {}
    now = ctx.doc.get("attributes") or {}
    vol_cm3 = f["volume_mm3"] / 1000.0
    meas = {"bbox0": f["bbox_mm"][0], "bbox1": f["bbox_mm"][1],
            "bbox2": f["bbox_mm"][2], "volume_mm3": f["volume_mm3"],
            "volume_cm3": vol_cm3, "watertight": f["watertight"],
            "triangles": f["triangles"]}
    stale, paste = [], []
    src_tag = f"plm geometry-facts @{ctx.sha[:10]}"
    for name, a in sorted(now.items()):
        b = before.get(name)
        if not b or str(b.get("value")) != str(a.get("value")) \
                or (b.get("source") or "") != (a.get("source") or ""):
            continue
        if name in MEASURABLE:
            m = meas[MEASURABLE[name]]
            try:
                v = float(a["value"]) if not isinstance(m, bool) else None
            except (TypeError, ValueError):
                v = None
            if isinstance(m, bool):
                wrong = str(a["value"]).lower() != str(m).lower()
                new = str(m)
            else:
                # wrong = no longer true AT THE PRECISION IT WAS WRITTEN: "113"
                # survives an internal change (±0.5), "43964.1" does not
                # survive three bores opened 0.2 mm (run 2: −28 mm³)
                txt = str(a["value"]).strip()
                dec = len(txt.split(".", 1)[1]) if "." in txt else 0
                tol = 0.5 * 10 ** -dec + 1e-9
                if MEASURABLE[name].startswith("bbox"):
                    tol = max(tol, 0.05)
                wrong = v is None or abs(v - m) > tol
                new = (f"{m:.4g}" if "volume_cm3" == MEASURABLE[name] else
                       f"{m:.1f}" if "volume" in MEASURABLE[name] else f"{m:g}")
            if not wrong:
                continue
            unit = a.get("unit") or ("mm" if name.startswith("bbox") else "")
            stale.append({"name": name, "value": a["value"], "measured": new,
                          "source": a.get("source") or "", "kind": "measurable"})
            paste.append(f'{name} = {{ value = "{new}"'
                         + (f', unit = "{unit}"' if unit else "")
                         + f', source = "{src_tag}" }}')
        elif name in SLICED:
            stale.append({"name": name, "value": a["value"], "measured": "",
                          "source": a.get("source") or "", "kind": "sliced"})
    if stale and solid_g and any(x["name"] == "mass_g" for x in stale):
        paste.append(f'solid_mass_g = {{ value = "{solid_g:g}", unit = "g", '
                     f'source = "{src_tag} ({key} @ {dens:g} g/cm³, 100% '
                     'solid — NOT a print mass)" }')
    return stale, paste


def _stale_text(ctx: Ctx, stale: list[dict], paste: list[str]) -> str:
    meas = [x for x in stale if x["kind"] == "measurable"]
    sliced = [x for x in stale if x["kind"] == "sliced"]
    out = [f"**declared before this geometry** — the mesh changed since "
           f"{ctx.base()[:10]} (the release this draft started from), and "
           "these attributes still carry the value AND source they had then:"]
    for x in meas:
        out.append(f"- `{x['name']}` = {x['value']} ({x['source'] or 'no source'})"
                   f" — measured now: **{x['measured']}**")
    for x in sliced:
        out.append(f"- `{x['name']}` = {x['value']} ({x['source'] or 'no source'})"
                   " — slicer-derived: plm cannot re-measure it")
    if paste:
        out.append("Paste into `[attributes]` (what plm measured on this "
                   "commit):\n\n```toml\n" + "\n".join(paste) + "\n```")
    if sliced:
        out.append("Slicer values: re-slice, or DROP them and say \"needs "
                   "re-slice\" in the change — never carry an old number "
                   "silently, never drop one silently.")
    return "\n".join(out)


# =============================================================================
# render-matches-source — the hand-edited-mesh catcher
# =============================================================================

#: `xvfb-run -a openscad` is exactly what plm/thumbs.py uses in this image
#: (OpenSCAD 2021.01 at /usr/bin/openscad). Same binary, same invocation style,
#: so if one of them can render a part, so can the other.
OPENSCAD = thumbs.OPENSCAD

#: A selector plm can reproduce is a `;`-separated list of `name=value`
#: assignments — the OpenSCAD `-D` form. 26 % of this registry's selectors are
#: that; the rest are PROSE a human wrote ("assembled product from four
#: product-frame child visualizations"), and there is no honest way to turn
#: prose into a render. Those get `skipped` with the reason, which is the
#: finding: an unreproducible render instruction is itself worth seeing.
_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")


def parse_selector(selector: str) -> tuple[list[str], str]:
    """(`-D` arguments, "" ) or ([], why it cannot be reproduced)."""
    s = (selector or "").strip()
    if not s:
        return [], ""
    # `;` and `,` both separate in the wild ('mode="cut_part", part="upright"').
    # Splitting on both is safe because every value is then required to be a
    # scalar — a vector like `[1,2,3]` is rejected with its reason rather than
    # silently torn in half.
    parts = [p for p in re.split(r"[;,]", s) if p.strip()]
    out = []
    for p in parts:
        m = _ASSIGN.match(p)
        if not m:
            return [], (f"the `[source] selector` is prose, not parameters — "
                        f"plm cannot turn {p.strip()[:60]!r} into a render")
        name, val = m.group(1), m.group(2).strip()
        if val.startswith(("'", '"')) or val.lstrip("-").replace(".", "", 1).isdigit() \
                or val in ("true", "false"):
            out.append(f"-D{name}={val}")
        elif re.fullmatch(r"[A-Za-z_]\w*", val):
            out.append(f'-D{name}="{val}"')      # a bare word is a string
        else:
            return [], (f"the `[source] selector` value {val[:40]!r} is not a "
                        "number, a quoted string or a bare word, so plm will "
                        "not guess at what OpenSCAD should be told")
    return out, ""


# -- stage 16: the forms the migration produced --
#
# The survey of every item's shipping commit on the POC : of the 107 revisions render-matches-source can reach, 84 are already the
# `-D` form, 16 are empty, and 23 were SKIPPED as prose. Most of those 23 are
# not prose at all: they are `-D` assignments with a human's ANNOTATION in
# brackets (`part=shell_print (dome-down, as printed)`), the swd-probe shape
# (`part=probe_base (wrapper swd_probe_base.scad); viz part=probe_base_posed`,
# one recipe per mesh), or a MODULE CALL (`tri_sector_divider(12)`). Run 3
# rewrote its three by hand. `selector_recipe` reads all three; only real prose
# is still skipped, and the skip prints the line it wants.

_CALL = re.compile(r"^\s*([A-Za-z_]\w*)\s*\((.*)\)\s*;?\s*$", re.S)
_NOTE = re.compile(r"\s+\(([^()]*)\)|^\(([^()]*)\)$")


def _split_top(s: str, seps: str) -> list[str]:
    """Split on any char of `seps` outside (), [] and quotes."""
    out, cur, depth, q = [], [], 0, ""
    for ch in s:
        if q:
            cur.append(ch)
            if ch == q:
                q = ""
            continue
        if ch in "\"'":
            q = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch in seps and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _scalar(val: str) -> str:
    """The value as OpenSCAD source (`-D` right-hand side), or "" when it is not
    a scalar plm will pass on (a vector, an expression, two alternatives)."""
    v = val.strip()
    if re.fullmatch(r'"[^"]*"|\'[^\']*\'', v):
        return '"' + v[1:-1] + '"'
    if re.fullmatch(r"-?\d+(\.\d+)?", v) or v in ("true", "false"):
        return v
    if re.fullmatch(r"[A-Za-z_]\w*", v):
        return f'"{v}"'                     # a bare word is a string
    return ""


def selector_recipe(selector: str, *, primary_is_viz: bool = False) -> dict:
    """How to re-render the PRIMARY mesh from `[source] selector`.

    {form, dargs, call, canonical, dropped, why}:
      params     the canonical `-D` form: `name=value; name=value`
      call       one OpenSCAD module call, `name(args)` — rendered through a
                 two-line wrapper that `use`s the source and calls it
      annotated  `-D` assignments wearing a human's notes — `(as printed)`,
                 `(wrapper x.scad)`, a `viz part=…` recipe for the viz mesh,
                 trailing prose clauses. READ, not trusted: a match proves it,
                 a mismatch is `skipped` (the notes may have meant something),
                 never `fail`. `canonical` is the line to write instead;
                 `dropped` what was ignored to get it.
      empty      no selector: render the source as it stands
      prose      nothing plm can run; `why` says so
    """
    s = (selector or "").strip()
    res = {"form": "empty", "dargs": [], "call": "", "canonical": "",
           "dropped": [], "why": ""}
    if not s:
        return res
    dargs, why = parse_selector(s)
    if not why:
        return {**res, "form": "params", "dargs": dargs, "canonical": s}
    m = _CALL.match(s)
    if m and "=" not in s.split("(", 1)[0]:
        call = f"{m.group(1)}({m.group(2).strip()})"
        return {**res, "form": "call", "call": call, "canonical": call}
    groups: dict[str, list[str]] = {"main": [], "viz": []}
    dropped: list[str] = []
    lossy = False
    for clause in _split_top(s, ";"):
        grp = "main"
        if re.match(r"^viz\b\s*", clause, re.I) and "=" in clause:
            grp, clause = "viz", re.sub(r"^viz\b\s*:?\s*", "", clause, flags=re.I)
        for note in _NOTE.finditer(clause):
            txt = note.group(1) or note.group(2) or ""
            dropped.append("(" + txt + ")")
            if "=" in txt:
                lossy = True     # `(…; viz_offset = [-70,0,2])` may be a parameter
        clause = _NOTE.sub("", clause).strip()
        for bit in _split_top(clause, ","):
            a = _ASSIGN.match(bit)
            if not a:
                dropped.append(bit)
                lossy = True
                continue
            val = _scalar(a.group(2))
            if not val:
                return {**res, "form": "prose", "why": (
                    f"the `[source] selector` value {a.group(2).strip()[:40]!r} "
                    "is not a number, a quoted string or a bare word, so plm "
                    "will not guess at what OpenSCAD should be told")}
            groups[grp].append(f"{a.group(1)}={val}")
    pick = groups["viz"] if (primary_is_viz and groups["viz"]) else groups["main"]
    if not pick and not groups["viz"] and dropped and not lossy:
        # nothing but a note — `(single part)`: render the source as it stands
        return {**res, "form": "annotated", "dropped": dropped, "lossy": False}
    if not pick:
        return {**res, "form": "prose", "dropped": dropped, "why": (
            "the `[source] selector` is prose, not parameters — plm cannot turn "
            f"{s[:60]!r} into a render")}
    if groups["viz"] and pick is groups["main"]:
        dropped.append("viz " + "; ".join(groups["viz"]))
    elif pick is groups["viz"] and groups["main"]:
        dropped.append("; ".join(groups["main"]) + " (the print recipe)")
    canonical = "; ".join(pick)
    return {**res, "form": "annotated", "canonical": canonical,
            "dargs": [f"-D{x}" for x in pick], "dropped": dropped,
            "lossy": lossy}


def _selector_options(data: bytes) -> tuple[str, list[str]]:
    """(variable, values) the source switches on — `part == "x"` — for a skip
    that can print a line to paste instead of "not parameters"."""
    seen: dict[str, list[str]] = {}
    for var, val in re.findall(r'\b([A-Za-z_]\w*)\s*==\s*"([^"]+)"',
                               data.decode(errors="replace")):
        if val not in seen.setdefault(var, []):
            seen[var].append(val)
    if not seen:
        return "", []
    var = max(seen, key=lambda k: (len(seen[k]), k == "part"))
    return var, seen[var]


def _cache_get(key: str) -> dict | None:
    p = CACHE / f"{key}.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _cache_put(key: str, val: dict) -> None:
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = CACHE / f"{key}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(val))
        os.replace(tmp, CACHE / f"{key}.json")
    except Exception:
        pass


def check_render_matches_source(ctx: Ctx) -> dict:
    """Re-render `[source]` and compare BY GEOMETRY, never by bytes.

    ⚠️ OPENSCAD IS NOT BYTE-DETERMINISTIC. Two runs of the same model on the
    same machine produce STLs that differ in triangle order, in triangle count
    after CGAL's own simplification, and in the last bits of every coordinate.
    A byte comparison here would fail on every part, every time, and teach
    everybody to ignore the board. Volume to 0.1 %, bbox to 0.01 mm and
    triangle count as *information* is the comparison that means something: a
    hand-edited mesh moves the volume, and nothing else does.
    """
    src = (ctx.doc.get("source") or {}).get("file") or ""
    sel = (ctx.doc.get("source") or {}).get("selector") or ""
    mesh = ctx.mesh_path()
    if not mesh:
        return _skip("no mesh is declared on this revision")
    if not src:
        return _skip(
            "no `[source] file` — nothing records how this geometry is "
            "regenerated",
            "There is no way to prove that the committed mesh came from a "
            "model in this revision, because the revision does not say which "
            "model. Add:\n\n```toml\n[source]\nfile = \"part.scad\"\n"
            "selector = \"part=\\\"body\\\"\"\n```\n\nThis check is required "
            "for RELEASED (§2.3), so it is worth fixing before promotion.")
    if not ctx.has_path(src):
        return _skip(
            f"`[source] file = {src!r}` is not in this tree",
            "The source is named but not committed, so the model that made "
            "this mesh is not part of the revision and cannot be re-run.")
    if Path(src).suffix.lower() != ".scad":
        return _skip(
            f"`{Path(src).name}` is not an OpenSCAD model",
            "plm can only re-run the CAD it has a renderer for. The image "
            "carries OpenSCAD; a STEP or Fusion source is a check an "
            "integration would post (§2.6), not one plm runs itself.")
    if mesh not in ctx.entries:
        return _skip(f"`{mesh}` is not in this tree — there is nothing at this "
                     "SHA to compare a render against")
    if Path(mesh).suffix.lower() != ".stl":
        return _skip(f"`{Path(mesh).name}` is not an STL, and the comparison is "
                     "made with plm's stdlib STL reader")

    viz = (ctx.doc.get("files") or {}).get("viz") or []
    rec = selector_recipe(sel, primary_is_viz=mesh in viz)
    dargs = rec["dargs"]
    if rec["why"]:
        return _skip(rec["why"], _selector_help(ctx, src, sel, rec))
    # a source that reaches outside its revision: rendered ANYWAY (a missing
    # `use` OpenSCAD can ignore still yields the committed geometry — 16 camper
    # rack parts pass that way), but a failure or a mismatch is then
    # `source-present`'s finding, not proof about the mesh
    try:
        outside = _includes(ctx, src, ctx.blob(src)) if src in ctx.entries else []
    except Exception:                                          # noqa: BLE001
        outside = []

    def _outside_skip(extra: str = "") -> dict:
        k, t = outside[0]
        return _skip(
            f"`{src}` {k}s `{t}` from outside the repo, so this revision's "
            "render is not evidence either way — see `source-present`",
            "The source reaches a file the revision does not carry; whatever "
            "OpenSCAD made without it is not the model. `source-present` names "
            "who holds the file and the fix; once the include resolves inside "
            "the repo (or a `lib/<N>` pin) this check can judge the mesh."
            + extra)

    if not Path("/usr/bin/openscad").exists() and not _which("openscad"):
        return _skip("OpenSCAD is not in this image",
                     "Nothing was installed to make this check run: plm uses "
                     "the `openscad` that `plm/thumbs.py` already renders "
                     "thumbnails with, and says so rather than passing.")

    # Cached by the immutable input tree and selector. Metadata-only changes
    # may re-render; imported files of any extension can affect the geometry.
    src_sha = ctx.blob_sha(src) or ctx.lib_blob(src)
    # Cover every input blob and gitlink, including imported meshes and pinned
    # nested parts/libs. Their immutable commits cover all transitive inputs.
    deps = sorted((p, e.mode, e.sha) for p, e in ctx.entries.items())
    if rec["call"] and src in ctx.entries:
        # the source already CALLS that module at top level (a wrapper file:
        # `use <lib.scad>; tri_bar_receiver_coupon();`) — the selector names
        # what it renders; run it as it stands
        name = rec["call"].split("(", 1)[0]
        body = ctx.blob(src).decode(errors="replace")
        if re.search(rf"^\s*{re.escape(name)}\s*\(", body, re.M):
            rec = {**rec, "call": ""}
    how_run = rec["call"] or " ".join(dargs)
    key = hashlib.sha256(
        f"render-tree-v2|{src_sha}|{how_run}|{src}|{deps}".encode()
    ).hexdigest()[:32]
    got = _cache_get(key)
    cached = got is not None
    t0 = time.monotonic()
    if not cached:
        try:
            got = _render(ctx, src, dargs, call=rec["call"])
        except subprocess.TimeoutExpired:
            return _skip(
                f"the re-render did not finish inside {RENDER_TIMEOUT_S} s",
                "A render is bounded on purpose — a check that can run forever "
                "is a check that takes the queue with it. Raise "
                "`PLM_CHECKS_RENDER_TIMEOUT` if this model is honestly that "
                "big.")
        except MeshError as exc:
            return {"status": "error", "summary": f"the re-render produced "
                    f"something plm could not read: {exc}",
                    "details_md": str(exc), "facts": None}
        except RuntimeError as exc:
            if outside:
                return _outside_skip(f"\n\n```\n{str(exc)[-600:]}\n```")
            if rec["form"] in ("annotated", "call"):
                # a recipe plm READ off the selector failed to run: that is a
                # finding about the selector, not proof about the mesh
                return _skip(
                    f"`{src}` did not render with the recipe read from the "
                    f"selector (`{rec['call'] or rec['canonical'] or '(none)'}`)"
                    f": {str(exc).splitlines()[0][:100]}",
                    _selector_help(ctx, src, sel, rec)
                    + f"\n\n```\n{str(exc)[-800:]}\n```")
            return {"status": "fail",
                    "summary": f"`{src}` does not render: "
                               f"{str(exc).splitlines()[0][:120]}",
                    "details_md":
                        "The source committed in this revision could not be "
                        "re-run at all, so the mesh beside it cannot have come "
                        f"from it in this state.\n\n```\n{exc}\n```",
                    "facts": None, "points_at": [{"path": src}]}
        _cache_put(key, got)
    secs = time.monotonic() - t0

    try:
        have = mesh_facts(ctx.blob(mesh))
    except MeshError as exc:
        return {"status": "fail", "summary": f"the committed mesh is unreadable: "
                f"{exc}", "details_md": str(exc), "facts": None,
                "points_at": [{"path": mesh}]}

    va, vb = have["volume_mm3"], got["volume_mm3"]
    dvol = abs(va - vb) / max(vb, 1e-9)
    dbb = [abs(have["bbox_mm"][i] - got["bbox_mm"][i]) for i in range(3)]
    facts = {"source": src, "selector": sel, "mesh": mesh,
             "selector_form": rec["form"], "recipe": how_run,
             "canonical": rec["canonical"], "dropped": rec["dropped"],
             "source_blob": src_sha[:12], "cached": cached,
             "render_seconds": None if cached else round(secs, 1),
             "committed": have, "rendered": got,
             "volume_delta_fraction": round(dvol, 6),
             "bbox_delta_mm": [round(d, 4) for d in dbb]}
    table = (
        "| | committed mesh | re-rendered from source |\n|---|---|---|\n"
        + f"| volume | **{va / 1000:.3f} cm³** | **{vb / 1000:.3f} cm³** |\n"
        + "".join(f"| bbox {a} | {have['bbox_mm'][i]:g} mm | "
                  f"{got['bbox_mm'][i]:g} mm |\n" for i, a in enumerate("XYZ"))
        + f"| triangles | {have['triangles']:,} | {got['triangles']:,} |\n"
        + f"| open edges | {have['open_edges']} | {got['open_edges']} |\n")
    how = (f"\n\n`{src}`"
           + (f" through `use <{src}>; {rec['call']};`" if rec["call"] else
              f" with `{' '.join(dargs)}`" if dargs else " with no parameters")
           + ((", re-rendered in under a second" if secs < 1
               else f", re-rendered in {secs:.0f} s") if not cached
              else ", from the render cache (same source blob, same selector)")
           + f" (OpenSCAD, {RENDER_TIMEOUT_S} s cap, no network).\n\n"
           "Triangle count is **information, not a verdict**: OpenSCAD is not "
           "byte-deterministic and its output tessellation moves between runs. "
           f"Volume is compared to {VOLUME_TOL:.1%} and each bbox axis to "
           f"{BBOX_TOL_MM} mm, which is what a hand-edit moves and a re-render "
           "does not.")

    bad = dvol > VOLUME_TOL or max(dbb) > BBOX_TOL_MM
    note = ""
    facts["compared"] = mesh
    if bad:
        # STAGE 16b (cold run 5): `primary` is, by convention, the VIZ — the
        # posed-in-assembly copy the viewer and the explode use — while a
        # part's selector usually makes the PRINT mesh. GSI-PRT-0010's render
        # (`part=plug`) failed against `plug_posed.stl`; the bot re-pointed
        # `primary` by hand and moved the external `plug.stl` into the tree.
        # So: before accusing anything, compare with the revision's OTHER
        # declared meshes, and say which file was compared.
        alt = _other_meshes(ctx, mesh)
        for path, role in alt["tree"]:
            try:
                o = mesh_facts(ctx.blob(path))
            except MeshError:
                continue
            ov = abs(o["volume_mm3"] - vb) / max(vb, 1e-9)
            ob = max(abs(o["bbox_mm"][i] - got["bbox_mm"][i]) for i in range(3))
            if ov <= VOLUME_TOL and ob <= BBOX_TOL_MM:
                facts.update({"compared": path, "compared_role": role,
                              "primary_not_rendered": mesh})
                return _ok(
                    f"{o['volume_mm3'] / 1000:.3f} cm³ — the render matches "
                    f"`{path}` ({role}), not the primary `{mesh}`",
                    f"`[source]` renders `{path}` (role `{role}`) within "
                    f"{ov:.3%}. The primary `{mesh}` is a different mesh — "
                    "normally the posed viz, which the viewer and the explode "
                    "use; this selector does not make it, which is fine. "
                    "Nothing to change." + how,
                    facts, [{"path": path}, {"path": src}])
        if alt["external"]:
            names = ", ".join(f"`{n}` ({r})" for n, r in alt["external"])
            return _skip(
                f"the render does not match the primary `{mesh}`; the "
                f"revision's other mesh {names} is outside the tree, so plm "
                "cannot compare it — commit it to the tree",
                f"Compared: `{mesh}` (the primary — by convention the posed "
                "viz). `[source]` most likely makes the PRINT mesh, which this "
                f"revision keeps outside the tree ({names}). Fix: add that "
                "file to the repo under the same name and drop its "
                "`[[files.external]]` entry (leave `primary` alone — the "
                "viewer and the explode use the viz), or give a selector that "
                "renders the primary.\n\n" + table + how) | {
                    "facts": facts, "points_at": [{"path": mesh}, {"path": src}]}
    if rec["form"] == "annotated":
        note = ("\n\nThe selector is in the migration's ANNOTATED form; plm "
                "read it as `" + (rec["canonical"] or "(no parameters)") + "`"
                + (" (ignoring " + "; ".join(f"`{d}`" for d in rec["dropped"])
                   + ")" if rec["dropped"] else "") + ". Write it as:\n\n"
                "```toml\n[source]\nselector = " + _toml_str(rec["canonical"])
                + "\n```")
    if outside:
        note += ("\n\n⚠ `" + src + "` " + outside[0][0] + "s `" + outside[0][1]
                 + "` from outside the repo (see `source-present`)"
                 + (" — the render matched without it." if not bad else "."))
    if bad and outside:
        return _outside_skip("\n\n" + table) | {
            "facts": facts, "points_at": [{"path": mesh}, {"path": src}]}
    if bad and rec["form"] == "annotated":
        # a recipe plm INFERRED from notes is not evidence enough to accuse
        # the mesh: the notes may have meant something the -D form lost
        return _skip(
            f"the re-render with the recipe read from the annotated selector "
            f"(`{rec['canonical'] or '(no parameters)'}`) does not match the "
            f"committed mesh ({dvol:.2%}) — write the selector that made it",
            table + how + note + "\n\n" + _selector_help(ctx, src, sel, rec),
            ) | {"facts": facts, "points_at": [{"path": mesh}, {"path": src}]}
    if bad:
        return {
            "status": "fail",
            "summary": (f"`{mesh}` was not generated from the source in "
                        f"this revision — {va / 1000:.3f} cm³ "
                        f"committed vs {vb / 1000:.3f} cm³ re-rendered "
                        f"({dvol:.2%})"),
            "details_md":
                "**The committed mesh and the model beside it describe "
                "different shapes.** Either the STL was edited by hand after "
                "it was generated, or a different part's mesh was committed, "
                "or the source changed without the mesh being re-exported.\n\n"
                + table + how,
            "facts": facts,
            "points_at": [{"path": mesh}, {"path": src}]}
    return _ok(
        f"`{mesh}` {va / 1000:.3f} cm³ matches the source within {dvol:.3%} "
        f"(bbox ≤ {max(dbb):.3f} mm)"
        + (f" — selector read as `{rec['canonical'] or '(no parameters)'}`"
           if rec["form"] == "annotated" else ""),
        table + how + note, facts, [{"path": mesh}, {"path": src}])


def _other_meshes(ctx: Ctx, primary: str) -> dict:
    """The revision's declared STL meshes other than `primary`, in the tree
    and outside it: {"tree": [(path, role)], "external": [(name, role)]}."""
    out = {"tree": [], "external": []}
    seen = {primary}
    for role in ("artifact", "mesh", "viz"):
        for p in (ctx.doc.get("files") or {}).get(role, []) or []:
            if p in seen or Path(p).suffix.lower() != ".stl":
                continue
            seen.add(p)
            if p in ctx.entries:
                out["tree"].append((p, role))
    for name, e in ctx.external.items():
        if name not in seen and Path(name).suffix.lower() == ".stl":
            seen.add(name)
            out["external"].append((name, e.get("role") or "external"))
    return out


def _toml_str(v: str) -> str:
    """A TOML basic-or-literal string for `v` (literal when it has quotes)."""
    if "'" not in v and '"' in v:
        return f"'{v}'"
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _selector_help(ctx: Ctx, src: str, sel: str, rec: dict) -> str:
    """The skip text for a selector plm cannot (or will not) run: what it
    reads, the forms plm runs, and — from the source's own `part == "…"`
    switch — the line to paste."""
    try:
        data = ctx.blob(src) if src in ctx.entries else b""
    except Exception:                                          # noqa: BLE001
        data = b""
    var, opts = _selector_options(data)
    out = [f"`[source] selector` on this revision reads:\n\n```\n{sel}\n```",
           "plm re-renders the PRIMARY mesh from one of: `-D` assignments "
           "(`part=\"body\"; jack=true` — what `openscad -D` takes), one "
           "module call (`tri_sector_divider(12)`), or nothing (render the "
           "source as it stands). Notes belong in the commit message, not "
           "the selector."]
    if opts:
        guess = f'{var}="{opts[0]}"'
        out.append(f"`{src}` switches on `{var}`: "
                   + ", ".join(f"`{o}`" for o in opts[:12])
                   + (" …" if len(opts) > 12 else "")
                   + ". Paste the one that makes the primary mesh, e.g.:\n\n"
                   f"```toml\n[source]\nselector = {_toml_str(guess)}\n```")
    elif rec.get("canonical"):
        out.append("```toml\n[source]\nselector = "
                   + _toml_str(rec["canonical"]) + "\n```")
    return "\n\n".join(out)


def _which(name: str) -> str:
    for d in os.environ.get("PATH", "").split(":"):
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return ""


def _materialize_render_tree(repo: Path, sha: str, work: Path, *,
                             active: frozenset = frozenset(),
                             budget: list[int] | None = None) -> None:
    """Archive pinned local item trees; never follow .gitmodules URLs.

    Includes nested assemblies and shared-source pins. Catalog metadata is not
    CAD input. Reject escaping archives and dependency cycles; bound expansion
    even when a small DAG fans out into many repeated occurrences.
    """
    budget = [128] if budget is None else budget
    identity = (str(repo.resolve()), sha)
    if identity in active or len(active) >= 16:
        raise RuntimeError("render dependency cycle or depth limit (16)")
    budget[0] -= 1
    if budget[0] < 0:
        raise RuntimeError("render dependency tree exceeds 128 repositories")
    archive = gitvalidate.git(repo, "archive", "--format=tar", sha, binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        # data_filter checks traversal, escaping links and special files.
        tree.extractall(work, filter="data")
    entries = gitvalidate.tree_entries(repo, sha)
    for path, entry in sorted(entries.items()):
        if entry.mode != gitvalidate.MODE_GITLINK or path == "catalog":
            continue
        bits = path.split("/")
        if len(bits) != 2 or bits[0] not in ("parts", "lib"):
            raise RuntimeError(f"unsupported render dependency path: {path}")
        # repo_path validates the item number and only opens the local store.
        child = gitrepo.repo_path(bits[1])
        dest = work / path
        if dest.is_symlink() or work.resolve() not in dest.resolve().parents:
            raise RuntimeError(f"render dependency escapes the tree: {path}")
        dest.mkdir(parents=True, exist_ok=True)
        _materialize_render_tree(child, entry.sha, dest,
                                 active=active | {identity}, budget=budget)


def _render(ctx: Ctx, src: str, dargs: list[str], *, call: str = "",
            override: dict | None = None) -> dict:
    """Extract the tree, run OpenSCAD on `src`, return the geometry facts.

    The WHOLE tree is extracted, not just the one file, because a real model
    `include<>`s and `use<>`s its neighbours. `git archive` writes the commit's
    tree exactly, so the render sees precisely the files the reviewer is
    looking at and nothing else on the machine.
    """
    with tempfile.TemporaryDirectory(prefix="plmcheck") as td:
        work = Path(td)
        _materialize_render_tree(ctx.repo, ctx.sha, work)
        # stage 17: `git archive` writes LFS POINTERS (no smudge filter runs
        # here, by design); a model that `import()`s a committed mesh needs
        # the mesh
        lfs.smudge_dir(work)
        for rel, data in (override or {}).items():
            # stage 19: the selector-aware compare renders THIS tree with one
            # file swapped for another item's copy (sources.selector_compare)
            dest = (work / rel).resolve()
            if work.resolve() not in dest.parents:
                raise RuntimeError(f"override path escapes the tree: {rel}")
            dest.write_bytes(data)
        out = work / "_plm_check_render.stl"
        target = work / src
        if call:
            # a MODULE-CALL selector (stage 16): `use` the source (its modules,
            # none of its top-level geometry) and call the one named
            target = work / "_plm_check_call.scad"
            target.write_text(f"use <{src}>\n{call};\n")
        cmd = [*OPENSCAD, "-o", str(out), *dargs, str(target)]
        res = subprocess.run(cmd, capture_output=True, cwd=str(work),
                             timeout=RENDER_TIMEOUT_S,
                             env={**os.environ, "HOME": str(work)})
        if res.returncode != 0 or not out.is_file() or not out.stat().st_size:
            err = (res.stderr or res.stdout).decode(errors="replace").strip()
            raise RuntimeError(err[-1500:] or
                               f"openscad exited {res.returncode} with no output")
        return mesh_facts(out.read_bytes())


# =============================================================================
# physical-diff — the pull request's picture (PLM_REVIEW_MODEL §2.10)
# =============================================================================
#
# Geoff, 2026-10-16: "i'd like to be able to quickly review the physical changes
# to the stls … lets mirror what github does — it's really meta data AROUND a
# pull request". So the picture is the OUTPUT of a check, GitHub's Checks API
# shape: a check run on the PULL REQUEST (subject `CHG-n`, not a commit), whose
# result carries `images` = `output.images` ({alt, image_url, caption}) — the
# composite first, then one per changed mesh.
#
# ONE MORE REGISTRATION, not a subsystem: the same queue, runner thread, row,
# relay outbox and re-run route as every other check. What differs is the
# SUBJECT. A commit check's row is (item, commit sha); this one's is
# (`CHG-n`, the HEAD-SET KEY) — a hash of the sorted (item, head, baseline)
# triples, so an unchanged set is never rendered twice and a push to any member
# makes a new row. `ChangeCtx` is `Ctx` for that subject.
#
# The PNGs are derived data under PLM_CACHE_DIR (`checks-images/`, beside
# `checks-render/` and the thumbnails): never in the DB, never in a repo; the
# last few sets per pull request are kept, and a missing file is re-rendered
# on demand by `image_path`'s caller.

#: rendered-picture cache: `<subject>/<set key>/<check name>/<i>.png`; the
#: newest IMAGES_KEEP sets per (subject, name) are kept, a closed pull
#: request's are dropped, and the whole store is held under a byte cap
IMAGES = config.CACHE_DIR / "checks-images"
IMAGES_KEEP = 3
IMAGES_MAX_BYTES = int(os.environ.get("PLM_CHECKS_IMAGES_MAX_MB", "512")) << 20
#: the meshes a picture fetched, by (item, commit, path) — immutable in git
MESH_CACHE = config.CACHE_DIR / "checks-meshes"
MESH_CACHE_MAX_BYTES = int(os.environ.get("PLM_CHECKS_MESH_CACHE_MB", "2048")) << 20
#: per tile and per picture (§2.10 budget: 20 s a tile, 2 min a picture)
TILE_TIMEOUT_S = int(os.environ.get("PLM_PHYSDIFF_TILE_TIMEOUT", "20"))
PICTURE_BUDGET_S = int(os.environ.get("PLM_PHYSDIFF_BUDGET", "120"))
TILE_WORKERS = 3                # tiles of one picture in parallel
MAX_TILES = 12
TILE_PX = (800, 600)            # rendered size: the composite's source
TILE_SERVE_PX = (440, 330)      # a tile as served: the ~400 px it fills
MESH_READ_MAX = 200 << 20
#: the four diff tokens of web/index.html (§2.9 "Sides and colours") + a
#: ghost. The CSS is the source; tests/test_physical_diff.py holds this and
#: web/diffwidgets.js TOK_FALLBACK equal to it.
COLOURS = {"old": "#3E6FB0", "new": "#CC7A12", "removed": "#CF222E",
           "added": "#1A7F37", "other": "#8C959F"}
CAMERA = "0,0,0,55,0,25,0"      # isometric-ish, the same for every tile


@dataclass(frozen=True)
class Subject:
    """What a check row is ABOUT: an item (rows keyed by commit) or a pull
    request (`CHG-n`, rows keyed by its head-set key). THE resolver — every
    "is this a pull request?" question (`applies_to`, the runner, the image
    store, the git-mode gate, `main`) asks `subject(number)`, which parses
    with `changes.LABEL_RE` (one parser)."""
    kind: str                       # "item" | "change"
    number: str                     # the rows' `number`: the item, or CHG-n
    cid: int = 0

    @property
    def is_change(self) -> bool:
        return self.kind == "change"

    def resolve(self, sha: str = "", ref: str = "") -> tuple[str, str]:
        """(the row key, the ref it came from) — one answer for `GET
        /api/repo/<n>/checks` and the MCP's get_checks. An item resolves a sha
        or ref in its repo (`default_ref` as the fallback: many items have no
        `main`); a pull request resolves to its CURRENT head-set key."""
        if self.is_change:
            if sha:
                return sha, ref
            from . import changes
            with db.connect() as conn:
                return set_key(changes.head_set(conn, self.cid)), ""
        repo = gitrepo.repo_path(self.number)
        if sha:
            return gitrepo.resolve(repo, sha), ref
        r = ref or gitrepo.refs(self.number).get("default_ref") or "HEAD"
        return gitrepo.resolve(repo, r), r

    def project(self, conn) -> str:
        """The project the subject belongs to ('' when it does not exist)."""
        from . import changes
        if self.is_change:
            try:
                return changes.row(conn, self.cid)["project"] or ""
            except changes.ChangeError:
                return ""
        r = db.resolve_item(conn, self.number)
        return (r["project"] if r else "") or ""


def subject(number) -> Subject:
    """`CHG-380` / `#380` / `380` → the pull request; anything else is an item
    number (which never parses as a pull request)."""
    from . import changes
    m = changes.LABEL_RE.match(str(number or ""))
    if m:
        return Subject("change", changes.label(int(m.group(1))), int(m.group(1)))
    return Subject("item", str(number or ""))


def set_key(heads: list[dict]) -> str:
    """The pull request's head set → a stable 40-hex key (the row's `sha`)."""
    trip = sorted((h["item"], h["head"] or "", h["base"] or "") for h in heads)
    return hashlib.sha256(json.dumps(trip).encode()).hexdigest()[:40]


def image_dir(number: str, sha: str, name: str) -> Path:
    """Where one check row's images live: `<subject>/<key>/<check name>/` — by
    NAME, so a second image-emitting check never overwrites another's."""
    s = subject(number)
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha or "") or name not in CHECKS or not (
            s.is_change or gitrepo.safe_number(s.number)):
        raise ValueError("not a check subject")
    return IMAGES / s.number / sha / name


#: `…/images/<i>.png` in a row's `image_url` — the FILE index (the composite
#: is 0; without Pillow the first image is tile 1)
_IMAGE_INDEX = re.compile(r"/images/(\d+)\.png")


def image_indices(row: dict) -> list[int]:
    """The file indices a row's `images` point at, in order."""
    return [int(m.group(1)) for im in row.get("images") or []
            if (m := _IMAGE_INDEX.search(im.get("image_url") or ""))]


def image_url(subject: str, name: str, sha: str, i: int) -> str:
    from urllib.parse import quote
    return (f"/api/repo/{quote(subject, safe='')}/checks/{name}/images/"
            f"{i}.png?sha={sha}")


def _prune_images(number: str, keep: str, name: str) -> None:
    """Keep `keep` and the newest IMAGES_KEEP-1 other sets of one check's
    images for a subject; then hold the whole store under its byte cap."""
    import shutil
    root = IMAGES / subject(number).number
    try:
        dirs = sorted((d / name for d in root.iterdir() if (d / name).is_dir()),
                      key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        dirs = []
    for d in [d for d in dirs if d.parent.name != keep][IMAGES_KEEP - 1:]:
        shutil.rmtree(d, ignore_errors=True)
        try:
            d.parent.rmdir()                 # the set dir, once it is empty
        except OSError:
            pass
    try:
        _cap_dir([s for t in IMAGES.iterdir() for s in t.iterdir()], IMAGES_MAX_BYTES)
    except OSError:
        pass


def drop_images(number: str) -> None:
    """A subject's images, all of them — a pull request that closed."""
    import shutil
    shutil.rmtree(IMAGES / subject(number).number, ignore_errors=True)


def _cap_dir(units: list[Path], max_bytes: int) -> None:
    """Delete the oldest `units` (files or directories) until their total
    size is under `max_bytes` — a cache's size guard. A unit that vanishes
    meanwhile (another writer's temp file) is simply not counted."""
    import shutil

    def stat(u: Path) -> tuple[float, int] | None:
        try:
            if u.is_file():
                st = u.stat()
                return st.st_mtime, st.st_size
            return u.stat().st_mtime, sum(f.stat().st_size for f in u.rglob("*")
                                          if f.is_file())
        except OSError:
            return None
    sized = sorted((st + (u,) for u in units if (st := stat(u))), key=lambda x: x[0])
    total = sum(n for _, n, _ in sized)
    for _, n, u in sized:
        if total <= max_bytes:
            break
        shutil.rmtree(u, ignore_errors=True) if u.is_dir() else u.unlink(missing_ok=True)
        total -= n


class ChangeCtx:
    """`Ctx` for a pull request: its members' heads and review baselines, read
    once. `sha` is the head-set key the row is filed under."""

    def __init__(self, s: Subject):
        from . import changes
        self.number, self.cid = s.number, s.cid
        with db.connect() as conn:
            self.title = changes.row(conn, s.cid)["title"]
            self.heads = changes.head_set(conn, s.cid)
        self.sha = set_key(self.heads)


# -- the pure parts: tiles, layout, the .scad (string-tested) -----------------

def picture_layout(n: int, width: int = 1200) -> dict:
    """Where everything goes on the composite: a header strip, then a grid of
    up to MAX_TILES tiles (1, 2, 3 or 4 a row — ~400 px each at 1200 wide),
    each with a one-line label under it. Never shorter than 630 (GitHub's
    social-preview proportion); `more` = tiles beyond the cap."""
    shown = max(1, min(n, MAX_TILES))
    cols = 1 if shown == 1 else 2 if shown in (2, 4) else 3 if shown <= 9 else 4
    rows = -(-shown // cols)
    header, label, pad = 64, 30, 8
    tw = (width - pad * (cols + 1)) // cols
    th = min(tw * 3 // 4, 520) if rows == 1 else tw * 3 // 4
    height = max(630, header + rows * (th + label + pad) + pad)
    tiles = []
    for i in range(shown):
        r, c = divmod(i, cols)
        tiles.append((pad + c * (tw + pad), header + pad + r * (th + label + pad),
                      tw, th))
    return {"width": width, "height": height, "header": header, "label": label,
            "cols": cols, "rows": rows, "tiles": tiles,
            "more": max(0, n - MAX_TILES)}


def physdiff_scad(old, new, *, eps: float = 0.08, mode: str = "diff",
                  ghosts=()) -> str:
    """The scene of one tile, as OpenSCAD source (rendered in PREVIEW, where
    color() and alpha survive into the PNG). `old` / `new`: a path, a list of
    paths (an assembly's bodies), or None.

    `diff`  new = amber, translucent; REMOVED (old proud of new) = red and
            ADDED (new proud of old) = green, solid. Each difference subtracts
            the other side GROWN by `eps` along ±x/±y/±z: the faces both
            revisions share are coplanar, and OpenCSG's depth-buffer CSG paints
            coplanar faces as stripes — the grow removes those and keeps any
            real change bigger than `eps`. One side only: added green /
            removed red.
    `ghost` the cheap form (a CSG preview over the budget; an assembly's many
            bodies, always): old = blue ghost, new = amber solid; no red/green.
    `ghosts` extra bodies drawn as grey ghosts (an assembly's other children).
    """
    from .thumbs import _scad_str
    C = COLOURS
    olds = [old] if isinstance(old, (str, Path)) else list(old or [])
    news = [new] if isinstance(new, (str, Path)) else list(new or [])
    imp = lambda p: f'import("{_scad_str(p)}", convexity=10);'  # noqa: E731
    out = ["// plm physical-diff tile — generated; PLM_REVIEW_MODEL §2.10"]
    if olds:
        out.append("module O() { " + " ".join(imp(p) for p in olds) + " }")
    if news:
        out.append("module N() { " + " ".join(imp(p) for p in news) + " }")
    out.append("module grow(e) { children(); for (v = [[e,0,0],[-e,0,0],"
               "[0,e,0],[0,-e,0],[0,0,e],[0,0,-e]]) translate(v) children(); }")
    out += [f'color("{C["other"]}", 0.25) {imp(g)}' for g in ghosts]
    if mode == "diff" and olds and news:
        out += [f'color("{C["removed"]}") difference() {{ O(); grow({eps}) N(); }}',
                f'color("{C["added"]}") difference() {{ N(); grow({eps}) O(); }}',
                f'color("{C["new"]}", 0.45) N();']
    elif mode == "diff":
        out.append(f'color("{C["added"]}") N();' if news else
                   f'color("{C["removed"]}", 0.6) O();' if olds else "")
    else:
        out += ([f'color("{C["old"]}", 0.35) O();'] if olds else []) + \
               ([f'color("{C["new"]}") N();'] if news else [])
    return "\n".join(out) + "\n"


def _grow_eps(facts: list[dict]) -> float:
    """0.08 mm, or 0.04 % of the biggest bbox diagonal when that is larger — a
    printed part's real changes are millimetres; float noise is microns."""
    d = 0.0
    for f in facts:
        if f and f.get("bbox_mm"):
            d = max(d, math.sqrt(sum(x * x for x in f["bbox_mm"])))
    return round(max(0.08, d * 4e-4), 3)


def _fmt_delta(a, b, unit: str, nd: int = 1) -> str:
    if a is None or b is None:
        return ""
    d = b - a
    pct = f" ({d / a * 100:+.1f} %)" if a else ""
    return f"{d:+,.{nd}f} {unit}{pct}" if abs(d) >= 10 ** -nd else "the same"


def _bbox(f) -> str:
    return "×".join(f"{x:.1f}" for x in f["bbox_mm"]) if f else "—"


# -- what to draw: one member's tiles -----------------------------------------

def _tree_side(side: dict | None) -> str:
    """A mesh chooser side's path when it is IN THE TREE (an external primary
    is not fetched for a picture)."""
    return (side or {}).get("path", "") if (side or {}).get("source", "tree") == "tree" else ""


def _tiles_for(h: dict) -> list[dict]:
    """One member's tiles: every changed mesh the Files tab's chooser offers
    (`gitrepo.compare`'s `meshes` — renames paired, primary first), skipping
    the ones whose bytes did not change; plus — when the member re-pinned BOM
    children — the assembly posed: each child's mesh at its head pin, the
    re-pinned ones as old/new, the rest as grey ghosts. A tile's `old`/`new`
    are lists of (item, commit, path)."""
    number, base, head = h["item"], h["base"], h["head"]
    cmp = gitrepo.compare(number, base, head, max_patch=0)
    vers = (cmp["base"].get("version") or "", cmp["head"].get("version") or "")
    changed = {f["path"]: f for f in cmp["files"] if f["kind"] == "mesh"}
    ident = lambda x: (x or {}).get("lfs", {}).get("oid") or (x or {}).get("blob")  # noqa: E731
    tiles, seen = [], {}
    for m in cmp["meshes"]:
        a, b = _tree_side(m["base"]), _tree_side(m["head"])
        ra, rb = changed.get(a), changed.get(b)
        if not (ra or rb) or (ra is rb and ra.get("same_content") is True):
            continue                         # this mesh's bytes did not change
        twin = (ident((ra or {}).get("ident_base")), ident((rb or {}).get("ident_head")))
        if twin[0] and twin[0] == twin[1]:
            continue                         # renamed, same bytes
        if all(twin) and twin in seen:
            # the print mesh and its posed viz are often the SAME bytes: one tile
            seen[twin]["path"] += f", {b or a}"
            seen[twin]["primary"] |= bool(m.get("is_primary"))
            continue
        tiles.append({"item": number, "path": b or a, "versions": vers,
                      "primary": bool(m.get("is_primary")),
                      "old": [(number, base, a)] if a else [],
                      "new": [(number, head, b)] if b else [], "ghosts": []})
        seen[twin] = tiles[-1]
    moved = {s["path"]: s for s in cmp["submodules"]
             if s["path"].startswith("parts/") and s.get("to")}
    if moved:
        olds, news, ghosts = [], [], []
        for path, pin in sorted(Ctx(number, head).gitlinks.items()):
            child = path.split("/", 1)[1]
            spec = _child_mesh(child, pin)
            if not spec:
                continue
            if path not in moved:
                ghosts.append(spec)
                continue
            news.append(spec)
            frm = (moved[path].get("from") or {}).get("commit")
            if frm and (o := _child_mesh(child, frm)):
                olds.append(o)
        if news:
            tiles.append({"item": number, "path": "assembly (posed)",
                          "versions": vers, "assembly": True,
                          "old": olds, "new": news, "ghosts": ghosts,
                          "children": [c for c, _, _ in news]})
    return tiles


def hero_tiles(row: dict | None, members: list[dict]) -> list[dict]:
    """The pictures of the COMPLETED thing, for the top of the pull request
    page (Geoff: "can a pull request by default show the visual diff of the
    completed assembly as one of the first cards"). One per ROOT member — one
    no other member waits on (`after` lists member ids; none → the highest
    `order`). Its best tile: the primary mesh's, else the first non-assembly
    diff tile (rows from before `primary` was recorded; `gitrepo.compare`
    lists primary first, so the first is right), else the posed assembly.
    Only tiles that rendered. Pure: the `physical-diff` row and the members."""
    tiles = ((row or {}).get("facts") or {}).get("tiles") or []
    if not tiles or not members:
        return []
    waited = {a for m in members for a in m.get("after") or []}
    roots = [m for m in members if m.get("proposal") not in waited]
    if not roots:
        roots = [max(members, key=lambda m: m.get("order") or 0)]
    out = []
    for m in sorted(roots, key=lambda m: m.get("order") or 0):
        mine = [t for t in tiles if t.get("item") == m["item"] and t.get("image")]
        pick = (next((t for t in mine if t.get("primary")), None)
                or next((t for t in mine if t.get("mode") == "diff"), None)
                or next((t for t in mine if t.get("mode") == "posed"), None))
        if not pick:
            continue
        f = pick.get("facts") or {}
        path = pick.get("path") or ""
        out.append({"item": m["item"], "path": path, "mode": pick.get("mode") or "",
                    "label": pick.get("label") or "",
                    "image_url": image_url(row["number"], "physical-diff",
                                           row["sha"], pick["image"]),
                    "old": f.get("old"), "new": f.get("new"),
                    "delta": _what(f.get("old"), f.get("new")) if f else "",
                    "compare_mesh": "" if pick.get("mode") == "posed"
                    else path.split(",")[0].strip()})
    return out


def _child_mesh(child: str, pin: str) -> tuple | None:
    """(child, pin, path) of the mesh a BOM child IS at that pin — `Ctx`'s
    `mesh_path` (manifest.primary_file's viz > mesh > artifact) — when it is
    in the tree and OpenSCAD can import it."""
    try:
        c = Ctx(child, pin)
    except Exception:                                       # noqa: BLE001
        return None
    p = c.mesh_path()
    return (child, pin, p) if p in c.entries and roles.is_scad_importable(p) else None


def _fetch(spec) -> Path:
    """A mesh's bytes as a local file — cached by (item, commit, path), which
    git makes immutable, under PLM_CACHE_DIR (the ghost children of every
    re-render of a pull request are the same blobs)."""
    number, sha, path = spec
    key = hashlib.sha256(f"{number}\0{sha}\0{path}".encode()).hexdigest()[:32]
    hit = MESH_CACHE / f"{key}{Path(path).suffix.lower()}"
    if not hit.is_file():
        data, _ = gitrepo.blob(number, sha, path, max_bytes=MESH_READ_MAX)
        MESH_CACHE.mkdir(parents=True, exist_ok=True)
        _atomic_write(hit, lambda tmp: tmp.write_bytes(data))
    return hit


def _measure(number: str, sha: str, path: str, local: Path) -> dict | None:
    """bbox + volume of one side: the `geometry-facts` row already measured
    for that commit when it is this mesh, else a bbox/volume-only pass."""
    if local.suffix.lower() != ".stl":
        return None
    with db.connect() as conn:
        row = next((r for r in read(conn, number, sha)
                    if r["name"] == "geometry-facts" and (r.get("facts") or {}).get("path") == path), None)
    if row and row["facts"].get("bbox_mm"):
        return row["facts"]
    return mesh_facts(local.read_bytes(), edges=False)


# -- rendering: a tile, the picture --------------------------------------------

def _shoot(scad: str, out: Path) -> None:
    from . import thumbs
    thumbs.render_png(scad, out, size=f"{TILE_PX[0]},{TILE_PX[1]}",
                      timeout=TILE_TIMEOUT_S,
                      extra=(f"--camera={CAMERA}", "--projection=o",
                             "--colorscheme=Tomorrow"))


def _assembly_tile(t: dict, png: Path, entry: dict) -> None:
    # an assembly: many bodies → the cheap form, always
    _shoot(physdiff_scad([_fetch(s) for s in t["old"]], [_fetch(s) for s in t["new"]],
                         mode="ghost", ghosts=[_fetch(s) for s in t["ghosts"]]), png)
    entry["mode"] = "posed"
    entry["line"] = (f"- **{t['item']}** posed: re-pinned {', '.join(t['children'])} "
                     f"(amber), {len(t['ghosts'])} other child(ren) ghosted")


def _what(fo: dict | None, fn: dict | None, sides: tuple | None = None) -> str:
    """One tile's change in words — the check's line and the hero card say it
    the same way: "volume -12,373.7 mm³ (-5.6 %) · bbox … → … mm". `sides`
    says which files EXIST (a file that would not measure is still there);
    without it, the facts' presence stands in."""
    has_old, has_new = sides or (bool(fo), bool(fn))
    if not (has_old and has_new):
        return "deleted" if has_old else "added"
    fo, fn = fo or {}, fn or {}
    vol = _fmt_delta(fo.get("volume_mm3"), fn.get("volume_mm3"), "mm³")
    bb = ("unchanged" if fo and fn and fo.get("bbox_mm") == fn.get("bbox_mm")
          else f"{_bbox(fo)} → {_bbox(fn)} mm")
    return f"volume {vol or '—'} · bbox {bb}"


def _mesh_tile(t: dict, png: Path, entry: dict) -> None:
    path = (t["new"] or t["old"])[0][2]
    if not roles.is_scad_importable(path):
        raise RuntimeError(f"{Path(path).suffix or path} is not a format OpenSCAD imports")
    old = _fetch(t["old"][0]) if t["old"] else None
    new = _fetch(t["new"][0]) if t["new"] else None
    fo = _measure(*t["old"][0], old) if old else None
    fn = _measure(*t["new"][0], new) if new else None
    try:
        _shoot(physdiff_scad(old, new, eps=_grow_eps([fo, fn])), png)
        entry["mode"] = "diff" if old and new else "single"
    except subprocess.TimeoutExpired:
        # the CSG preview was too slow: old ghost + new solid
        _shoot(physdiff_scad(old, new, mode="ghost"), png)
        entry["mode"] = "ghost"
    what = _what(fo, fn, (bool(old), bool(new)))
    entry["line"] = (f"- **{t['item']}** `{t['path']}` v{t['versions'][0] or '?'} → "
                     f"v{t['versions'][1] or '?'}: {what}"
                     + (" · *(ghost form: the difference was over the time "
                        "budget)*" if entry["mode"] == "ghost" else ""))
    entry["facts"] = {"old": fo and {k: fo.get(k) for k in ("bbox_mm", "volume_mm3")},
                      "new": fn and {k: fn.get(k) for k in ("bbox_mm", "volume_mm3")}}


def _render_tile(t: dict, i: int, work: Path, t0: float) -> dict:
    """One tile → `work/<i>.png` and its entry (label, mode, line, error)."""
    label = (f"{t['item']} · {t['path']} · old v{t['versions'][0] or '?'}"
             f" → new v{t['versions'][1] or '?'}")
    entry = {"label": label, "item": t["item"], "path": t["path"], "mode": "",
             "seconds": None, "error": "", "line": "",
             "primary": bool(t.get("primary")), "versions": list(t["versions"])}
    if time.monotonic() - t0 > PICTURE_BUDGET_S:
        entry["error"] = f"not rendered: the {PICTURE_BUDGET_S}s budget ran out"
        return entry
    s0 = time.monotonic()
    try:
        (_assembly_tile if t.get("assembly") else _mesh_tile)(t, work / f"{i}.png", entry)
        entry["image"] = i
    except Exception as exc:                                # noqa: BLE001
        entry["error"] = ("OpenSCAD timed out" if isinstance(
            exc, subprocess.TimeoutExpired) else str(exc).split("\n")[0])
    entry["seconds"] = round(time.monotonic() - s0, 2)
    return entry


def check_physical_diff(ctx: ChangeCtx) -> dict:
    """Render the pull request's physical change — one tile per changed mesh,
    old and new from one camera, removed red / added green — composite them,
    and VERDICT: pass when every changed mesh rendered, warn naming the ones
    that did not (an LFS object missing, OpenSCAD over the budget), skipped
    when no mesh changed (GitHub's `neutral`). Budget → render → record."""
    t0 = time.monotonic()
    tiles, problems = [], []
    for h in ctx.heads:
        if not h["head"]:
            continue
        if not h["base"]:
            problems.append(f"{h['item']}: first revision — no baseline to "
                            "compare with (every file is new)")
            continue
        try:
            tiles += _tiles_for(h)
        except Exception as exc:                            # noqa: BLE001
            problems.append(f"{h['item']}: could not compare "
                            f"{h['base'][:10]}...{h['head'][:10]}: {exc}")
    n_mem, n = len(ctx.heads), len(tiles)
    facts = {"members": n_mem, "meshes": n, "tiles": [], "set": ctx.sha}
    if not tiles:
        return {"status": "warn" if problems else "skipped",
                "summary": "no geometry changed — text/BOM only"
                           + (f" ({len(problems)} member(s) not compared)"
                              if problems else ""),
                "details_md": "No mesh file changed in any member."
                              + "".join(f"\n- {p}" for p in problems),
                "facts": facts, "images": []}
    more = max(0, n - MAX_TILES)
    tally = (f"{n_mem} member{'s' if n_mem != 1 else ''} · {n} "
             f"mesh{'es' if n != 1 else ''} changed")
    from . import changes
    header = f"{changes.ref(ctx.cid)} · {ctx.title}"
    images, composite = [], False
    with tempfile.TemporaryDirectory(prefix="plmdiff") as td:
        work = Path(td)
        # tiles in parallel inside the one job; OpenSCAD itself is throttled
        # by thumbs' render slots, which the thumbnails share
        with ThreadPoolExecutor(TILE_WORKERS) as pool:
            entries = list(pool.map(lambda x: _render_tile(x[1], x[0], work, t0),
                                    enumerate(tiles[:MAX_TILES], start=1)))
        rendered = [e for e in entries if e.get("image")]
        if MESH_CACHE.is_dir():
            _cap_dir([*MESH_CACHE.iterdir()], MESH_CACHE_MAX_BYTES)
        if rendered:
            out_dir = image_dir(ctx.number, ctx.sha, "physical-diff")
            out_dir.mkdir(parents=True, exist_ok=True)
            composite = _composite(work, out_dir, header, tally, rendered, more)
            for e in rendered:
                _publish_tile(work / f"{e['image']}.png", out_dir / f"{e['image']}.png")
    for e in entries:
        if e["error"]:
            problems.append(f"{e['label']}: {e['error']}")
        facts["tiles"].append({k: v for k, v in e.items() if k != "line"})
    if composite:
        images.append({"alt": f"{header} · {tally}", "caption": f"{header} · {tally}",
                       "image_url": image_url(ctx.number, "physical-diff", ctx.sha, 0)})
    images += [{"alt": e["label"], "caption": e["label"],
                "image_url": image_url(ctx.number, "physical-diff", ctx.sha, e["image"])}
               for e in rendered]
    _prune_images(ctx.number, ctx.sha, "physical-diff")
    facts["seconds"] = round(time.monotonic() - t0, 2)
    facts["composite"] = composite
    lines = [e["line"] for e in entries if e["line"]]
    if more:
        lines.append(f"- … and {more} more mesh(es) not drawn (cap {MAX_TILES})")
    summary = f"{n} mesh{'es' if n != 1 else ''} changed"
    if problems:
        summary += f" · {len(problems)} not rendered"
    legend = ("Old = blue ghost, new = amber; **red** = old surface proud of "
              "new (removed), **green** = new proud of old (added) — the "
              "colours of the Files tab's overlay.")
    det = "\n".join(lines) + "\n\n" + legend
    if problems:
        det += "\n\n**Not rendered:**\n" + "\n".join(f"- {p}" for p in problems)
    if not composite and rendered:
        det += ("\n\n*(No composite: Pillow is not in this image — each tile "
                "is its own image.)*")
    return {"status": "warn" if problems else "pass", "summary": summary,
            "details_md": det, "facts": facts, "images": images}


def _atomic_write(dest: Path, write) -> None:
    """`write(tmp)`, then rename into place: a reader never sees half a file."""
    tmp = dest.with_name(f"{dest.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
                         f"{dest.suffix}")
    try:
        write(tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _publish_tile(src: Path, dest: Path) -> None:
    """A tile as served: downscaled to ~440 px (the 800×600 render is the
    composite's source only); the full render when Pillow is absent."""
    try:
        from PIL import Image
    except ImportError:
        import shutil
        _atomic_write(dest, lambda tmp: shutil.copyfile(src, tmp))
        return
    im = Image.open(src)
    im.thumbnail(TILE_SERVE_PX, Image.LANCZOS)
    _atomic_write(dest, lambda tmp: im.save(tmp, optimize=True))


def _composite(work: Path, out_dir: Path, header: str, tally: str,
               rendered: list[dict], more: int) -> bool:
    """The tiles + labels + a header strip and legend on one PNG (Pillow). A
    missing Pillow is not a failure: the tiles are still the check's images."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    L = picture_layout(len(rendered))
    img = Image.new("RGB", (L["width"], L["height"]), "#FFFFFF")
    d = ImageDraw.Draw(img)

    def font(size, bold=False):
        try:
            return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans"
                                      f"{'-Bold' if bold else ''}.ttf", size)
        except OSError:
            return ImageFont.load_default()
    big, small, small_b, more_f = font(20, True), font(13), font(13, True), font(14, True)
    d.rectangle((0, 0, L["width"], L["header"] - 1), fill="#F6F8FA")
    d.line((0, L["header"] - 1, L["width"], L["header"] - 1), fill="#D0D7DE")
    d.text((14, 12), _fit(d, header, big, L["width"] - 28), fill="#1F2328", font=big)
    x = 14
    for key in ("old", "new", "removed", "added"):
        d.rectangle((x, 44, x + 11, 55), fill=COLOURS[key])
        d.text((x + 16, 41), key, fill="#59636E", font=small)
        x += 16 + int(d.textlength(key, font=small)) + 18
    d.text((L["width"] - 14 - int(d.textlength(tally, font=small_b)), 41),
           tally, fill="#1F2328", font=small_b)
    for (tx, ty, tw, th), e in zip(L["tiles"], rendered):
        t = Image.open(work / f"{e['image']}.png").convert("RGB")
        t.thumbnail((tw, th), Image.LANCZOS)
        img.paste(t, (tx + (tw - t.width) // 2, ty + (th - t.height) // 2))
        d.rectangle((tx, ty, tx + tw - 1, ty + th - 1), outline="#D0D7DE")
        d.text((tx + 4, ty + th + 6), _fit(d, e["label"], small, tw - 8),
               fill="#1F2328", font=small)
    if more:
        d.text((L["width"] - 140, L["height"] - 24), f"+{more} more",
               fill="#59636E", font=more_f)
    _atomic_write(out_dir / "0.png", lambda tmp: img.save(tmp, optimize=True))
    return True


def _fit(d, text: str, font, width: int) -> str:
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


# =============================================================================
# shared-source — stage 15: one source, many repos
# =============================================================================

def check_shared_source(ctx: Ctx) -> dict:
    from . import db, sources
    paths = sources.source_paths(ctx.doc, ctx.entries)
    if not paths:
        return _skip("no source file in this tree to compare across the project")
    project = str(ctx.doc.get("project") or "")
    with db.connect() as conn:
        d = sources.drift(conn, ctx.number, project, ctx.doc, ctx.entries,
                          sha=ctx.sha)
        in_change = sources.change_heads(conn, ctx.number, ctx.sha)
        # stage 19 (handoff finding 4): the change this commit heads, and the
        # dispositions its reviewer recorded on the items that carry a
        # different copy — `historical`, `unchanged_geometry`, …
        cid, disp = sources.change_dispositions(conn, ctx.number, ctx.sha)
        lib_home = sources.lib_home(conn, project, paths)
    shared = {p: v for p, v in d.items() if v["same"] or v["different"]}
    facts = {p: {"blob": v["blob"][:12],
                 "same": [r["number"] for r in v["same"]],
                 "different": {r["number"]: r["blob"][:12]
                               for r in v["different"]}}
             for p, v in shared.items()}
    chg = next(iter(in_change.values()), {}).get("change", "") or \
        (f"CHG-{cid}" if cid else "")
    if chg:
        facts["_change"] = chg
    if disp:
        facts["_dispositions"] = {k: v["disposition"] for k, v in disp.items()}
    if not shared:
        return _ok("not shared — no other item of the project carries "
                   + ", ".join(f"`{p}`" for p in paths[:3]), facts=facts)
    drifted = {p: v for p, v in shared.items() if v["different"]}

    def who(r):
        return f"{r['number']} ({r.get('ref') or 'main'})"

    def tag(r):
        x = disp.get(r["number"])
        return (f" — {x['label']} ({chg}{'' if x['confirmed'] else ', drafted'})"
                if x else "")
    lines = []
    for p, v in shared.items():
        groups: dict[str, list] = {}
        for r in v["different"]:
            groups.setdefault(r["blob"], []).append(who(r) + tag(r))
        lines.append(
            f"- `{p}` (this tree: {v['blob'][:10]}) — the same blob in "
            f"{len(v['same'])} other item(s)"
            + (": " + ", ".join(who(r) for r in v["same"][:12]) if v["same"] else "")
            + "".join(f"\n  - a DIFFERENT blob {b[:10]} in {len(n)}: "
                      f"{', '.join(n[:12])}" for b, n in groups.items()))
    details = ("\n".join(lines) + "\n\nCompared on what each item SHIPS "
               "(`main`, else its newest draft)"
               + (f" — and, for the other members of {chg}, on the change's "
                  "own head, because that is what merges with this one"
                  if chg else "")
               + ". One source carried by many repos drifts the moment one "
               "copy changes; the home for it is one item pinned as "
               "`lib/<NUMBER>` (docs/SHARED-SOURCES.md).")
    if drifted:
        details += "\n\n" + sources.migration_md(
            ctx.number, project, sorted(drifted), lib_home,
            consumers=sorted({ctx.number} | {r["number"] for v in shared.values()
                                             for r in v["same"] + v["different"]}),
            change=chg)
    if drifted:
        undecided = sorted({who(r) for v in drifted.values()
                            for r in v["different"] if r["number"] not in disp})
        decided = sorted({f"{r['number']} {disp[r['number']]['label']}"
                          for v in drifted.values() for r in v["different"]
                          if r["number"] in disp})
        if not undecided:
            # every differing copy has a reviewable reason on the change
            return _ok(f"drift dispositioned in {chg} — " + "; ".join(decided[:6])
                       + (f" (+{len(decided) - 6})" if len(decided) > 6 else ""),
                       details, facts, [{"path": p} for p in drifted])
        return {"status": "warn",
                "summary": f"DRIFT — {', '.join(f'`{p}`' for p in drifted)} "
                           f"differs in {', '.join(undecided[:6])}"
                           + (f" (+{len(undecided) - 6})" if len(undecided) > 6
                              else "")
                           + (f"; dispositioned: {', '.join(decided[:3])}"
                              if decided else "")
                           + " — one home: lib/<N> (details), or a "
                             "disposition on the change",
                "details_md": details, "facts": facts,
                "points_at": [{"path": p} for p in drifted]}
    n = sum(len(v["same"]) for v in shared.values())
    return _ok(f"shared and in step — the same blob in {n} other item"
               f"{'' if n == 1 else 's'}", details, facts,
               [{"path": p} for p in shared])


# =============================================================================
# purchase-requirements — stage 19 (handoff finding 6)
# =============================================================================

def check_purchase_requirements(ctx: Ctx) -> dict:
    reqs = [b for b in requirements.lines(ctx.doc) if not b.get("suppressed")]
    if not reqs:
        return _skip("no purchase requirement lines in this manifest")
    gate = requirements.release_required(ctx.doc)
    rows = []
    for b in reqs:
        cands = requirements.candidates(b)
        rows.append(f"- find {b.get('find', '—')}: {float(b.get('qty') or 1):g} "
                    f"{b.get('unit') or 'EA'} {b['requirement']}"
                    + (f" — candidates {', '.join(cands)}" if cands else
                       " — no candidate yet")
                    + (f" ({b['notes']})" if b.get("notes") else ""))
    n = len(reqs)
    details = (
        f"{n} purchase requirement{'s' if n != 1 else ''} in this BOM name what "
        "must be BOUGHT, and nothing is selected for "
        f"{'them' if n != 1 else 'it'} yet:\n\n" + "\n".join(rows)
        + "\n\nA green explode or mesh check is about the made parts; this "
          "is about whether the assembly can be ordered. Select one by "
          "replacing the line with `path = \"catalog/<N>.toml\"` (register "
          "the catalog item first). "
        + ("This manifest's `[release] required` names "
           "`purchase-requirements`: a RELEASE is refused until none remain."
           if gate else
           "Advisory at every maturity — add `purchase-requirements` to "
           "`[release] required` to make a release wait for them."))
    return {"status": "warn",
            "summary": f"{n} purchase requirement{'s' if n != 1 else ''} "
                       "unresolved (not selected)"
                       + (" — required before release" if gate else ""),
            "details_md": details,
            "facts": {"unresolved": n, "release_required": gate,
                      "requirements": [b["requirement"] for b in reqs]},
            "points_at": [{"path": "plm.toml"}]}


# =============================================================================
# instructions-current — stage 19 (handoff finding 5)
# =============================================================================

def check_instructions_current(ctx: Ctx) -> dict:
    from . import db, gitmode
    name = next((p for p in workflow_text.NAMES if p in ctx.entries), "")
    if not name:
        return _skip("no INSTRUCTIONS.md in this tree")
    project = str(ctx.doc.get("project") or "")
    with db.connect() as conn:
        mode = gitmode.mode_for(project, conn)
    text = ctx.blob(name).decode(errors="replace")
    v = workflow_text.verdict(text, mode=mode)
    if v is None:
        return _ok(f"`{name}` does not contradict the `{mode}` project's write loop",
                   facts={"mode": mode})
    lines = v["lines"]
    details = (
        f"Project `{project}` is `write`: a change is a branch, a version bump, "
        "a push into a CHANGE and a human approval (GIT-FIRST.md). These lines "
        "describe the old MCP loop:\n\n"
        + "\n".join(f"- line {x['line']}: `{x['text']}` — {x['why']}"
                     for x in lines)
        + "\n\nPrecedence while it says so: the user's words > the project's "
          "GIT-FIRST.md / AGENTS.md > this file. Replace them with:\n\n> "
        + v["replacement"]
        + ("\n\nThe two lines are the migration's GENERATED block: "
           "`python -m plm.migrate_cleanup --only instructions --apply` "
           "rewrites it on an open draft (or do it in your change)."
           if v["generated"] else ""))
    return {"status": "warn",
            "summary": f"`{name}` line{'s' if len(lines) > 1 else ''} "
                       + ", ".join(str(x["line"]) for x in lines[:5])
                       + (" describe" if len(lines) > 1 else " describes")
                       + " the old checkout/revise loop — this "
                         "project's loop is git (replacement in details)",
            "details_md": details,
            "facts": {"mode": mode, "lines": [x["line"] for x in lines],
                      "generated": v["generated"]},
            "points_at": [{"path": name, "line": x["line"]} for x in lines]}


#: name -> callable. The registry above is the vocabulary; this is the code.
RUNNERS = {
    "manifest": check_manifest,
    "pins-released": check_pins_released,
    "explodes": check_explodes,
    "files-present": check_files_present,
    "source-present": check_source_present,
    "stable-names": check_stable_names,
    "geometry-facts": check_geometry_facts,
    "verified-by-coupon": check_verified_by_coupon,
    "render-matches-source": check_render_matches_source,
    "shared-source": check_shared_source,
    "instructions-current": check_instructions_current,
    "purchase-requirements": check_purchase_requirements,
    "physical-diff": check_physical_diff,
}


# =============================================================================
# B. THE RUNNER — one thread, one queue, crash-safe.
# =============================================================================

_q: "queue.Queue[tuple]" = queue.Queue()
#: (number, sha) of the job the runner is executing right now
_current: tuple | None = None
_worker: threading.Thread | None = None
_lock = threading.Lock()


#: >0 while a caller's writes will be ROLLED BACK (stage 16b): a request for
#: checks on a commit whose indexing is undone means nothing — and in
#: `gitindex --rebuild --check --dry-run --all` it started the runner thread IN
#: THE TOOL'S PROCESS, whose row writes then took the writer from the next
#: item's wipe ("database is locked … DELETE FROM files", 24-36 items a run).
_suspended = 0


class suspended:
    """`with checks.suspended():` — `enqueue` is a no-op for the duration."""
    def __enter__(self):
        global _suspended
        _suspended += 1
        return self

    def __exit__(self, *exc):
        global _suspended
        _suspended -= 1
        return False


def enqueue(number: str, sha: str, *, names: list[str] | None = None,
            item_id: int = 0, source: str = SOURCE_BUILTIN,
            conn=None) -> None:
    """Ask for checks on a commit. **Never raises, never blocks.**

    In a process that runs checks (PLM_CHECKS=1) it never writes either —
    below. In a RELAY process (PLM_CHECKS=relay — the MCP) the request is
    written to `check_requests`: on `conn` when the caller passes its
    connection, so the request commits or rolls back WITH the write that
    caused it (a true outbox — `gitindex.index_commit` does this); otherwise by
    a relay thread on its own connection a moment later, once the caller's
    transaction is out of the way (the same reason the runner below writes its
    rows on its own connection). Either way the runner's sweep picks it up.

    ⚠️ IT DOES NOT TOUCH THE DATABASE, and that is the whole design of this
    function. Its one production caller is `gitindex.index_commit`, which is
    *inside* the caller's write transaction; a second connection writing rows
    from here would either deadlock on the write lock or commit checks for a
    revision whose indexing then rolled back. The worker creates the rows on
    its own connection, a few milliseconds later, once the caller is done.

    The cost is a real (small) window: a crash between this call and the
    worker's first write loses the request with no row to recover from.
    `requeue_stale()` heals everything after that point; this window is stated
    rather than papered over, and a re-push or the Re-run button closes it.
    """
    if _suspended:
        return
    if RELAY:
        _relay(number, sha, names, item_id, source, conn)
        return
    if not ENABLED:
        return
    try:
        start_runner()
        _q.put((number, sha, list(names) if names else None, item_id, source,
                ()))
    except Exception:
        pass            # a check request must never break the write that caused it


_rq: "queue.Queue[tuple]" = queue.Queue()
_relayer: threading.Thread | None = None


def _request_row(c, number, sha, names, item_id, source) -> None:
    c.execute(_REQUESTS_DDL)
    c.execute(
        "INSERT INTO check_requests (number, sha, names, item_id, source, "
        "requested_by, created_at) VALUES (?,?,?,?,?,?,?)",
        (number, sha, json.dumps(sorted(names)) if names else "",
         item_id or 0, source, "relay", db.now()))


def _relay(number, sha, names, item_id, source, conn) -> None:
    """Record a request for the runner in another process. Never raises."""
    global _relayer
    try:
        if conn is not None:
            _request_row(conn, number, sha, names, item_id, source)
            return
        with _lock:
            if _relayer is None or not _relayer.is_alive():
                _relayer = threading.Thread(target=_relay_loop, daemon=True,
                                            name="plm-checks-relay")
                _relayer.start()
        _rq.put((number, sha, names, item_id, source))
    except Exception as exc:                                # noqa: BLE001
        print(f"[checks] could not relay the request for {number}@"
              f"{str(sha)[:10]}: {exc}", flush=True)


def _relay_loop() -> None:
    while True:
        job = _rq.get()
        try:
            with db.connect() as c:
                _request_row(c, *job)
        except Exception as exc:                            # noqa: BLE001
            print(f"[checks] relay write failed for {job[0]}@"
                  f"{str(job[1])[:10]}: {exc}", flush=True)
        finally:
            _rq.task_done()


def _sweep() -> int:
    """Turn other processes' requests into jobs on THIS runner's queue.

    Called by the runner thread when it is idle, so a request waits at most
    PLM_CHECKS_SWEEP_S plus whatever is already queued. Requests for one
    commit are merged (any `all` wins). Each job carries its request ids; the
    rows are deleted by `_run_job` in the transaction that writes the job's
    `queued` check rows — a crash in between leaves them for the next sweep.
    """
    try:
        with db.connect() as c:
            c.execute(_REQUESTS_DDL)
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM check_requests ORDER BY id LIMIT 500")]
    except Exception:                                       # noqa: BLE001
        return 0
    jobs: dict[tuple, dict] = {}
    for r in rows:
        j = jobs.setdefault((r["number"], r["sha"]),
                            {"names": set(), "all": False, "ids": [],
                             "item_id": r["item_id"], "source": r["source"]})
        j["ids"].append(r["id"])
        if r["names"]:
            j["names"].update(json.loads(r["names"]))
        else:
            j["all"] = True
    for (number, sha), j in jobs.items():
        _q.put((number, sha, None if j["all"] else sorted(j["names"]),
                j["item_id"], j["source"] or SOURCE_BUILTIN, tuple(j["ids"])))
    return len(jobs)


def start_runner() -> None:
    global _worker
    if not ENABLED:
        return
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run_loop, name="plm-checks",
                                   daemon=True)
        _worker.start()


def _run_loop() -> None:
    global _current
    while True:
        try:
            number, sha, names, item_id, source, req_ids = _q.get(
                timeout=SWEEP_S)
        except queue.Empty:
            _sweep()            # idle: anything another process asked for?
            continue
        except Exception:                                   # pragma: no cover
            return
        _current = (number, sha)
        try:
            _run_job(number, sha, names, item_id, source, req_ids)
        except Exception as exc:                            # never die
            try:
                with db.connect() as conn:
                    _finish(conn, number, sha, "manifest",
                            {"status": "error",
                             "summary": f"the check runner failed: {exc}",
                             "details_md": f"```\n{exc}\n```"})
            except Exception:
                pass
        finally:
            _current = None
            _q.task_done()


def _run_job(number: str, sha: str, names, item_id: int, source: str,
             req_ids: tuple = ()) -> None:
    subj = subject(number)
    number = subj.number
    if subj.is_change:
        ctx = _change_job(subj, sha, req_ids)
        if ctx is None:
            return
        sha = ctx.sha
    else:
        ctx = Ctx(number, sha)
    want = names or applies_to(number, sha, ctx)
    want = [n for n in CHECKS if n in want]          # registry order, always
    if not want:
        if req_ids:
            with db.connect() as conn:
                _done_requests(conn, req_ids)
        return
    with db.connect() as conn:
        ensure_schema(conn)
        _done_requests(conn, req_ids)
        if not item_id:
            r = conn.execute("SELECT id FROM items WHERE number=?",
                             (number,)).fetchone()
            item_id = r["id"] if r else 0
        # Every applicable row exists as `queued` BEFORE the first one runs, so
        # the board is complete from the first poll (a board that grows a row at
        # a time reads as "green so far", which is the one thing it must not)
        # and so a crash mid-run leaves rows for requeue_stale to find.
        now = db.now()
        for n in want:
            conn.execute(
                "INSERT INTO checks (item_id, number, sha, name, status, source,"
                " created_at) VALUES (?,?,?,?, 'queued', ?, ?) "
                "ON CONFLICT(number, sha, name) DO UPDATE SET "
                "status='queued', summary='', details_md='', facts_json='',"
                " points_json='', started_at=NULL, ended_at=NULL, source=excluded.source",
                (item_id, number, sha, n, source, now))
        conn.commit()

    for n in want:
        with db.connect() as conn:
            conn.execute("UPDATE checks SET status='running', started_at=? "
                         "WHERE number=? AND sha=? AND name=?",
                         (db.now(), number, sha, n))
            conn.commit()
        try:
            res = RUNNERS[n](ctx)
        except gitvalidate.Refused as exc:
            res = {"status": "fail", "summary": str(exc).split("\n", 1)[0],
                   "details_md": f"```\n{exc}\n```"}
        except Exception as exc:
            res = {"status": "error",
                   "summary": f"{type(exc).__name__}: {exc}",
                   "details_md": f"```\n{type(exc).__name__}: {exc}\n```"}
        with db.connect() as conn:
            _finish(conn, number, sha, n, res)
            if subj.is_change:
                _announce(conn, ctx, n, res)
            conn.commit()


def _change_job(subj: Subject, requested: str, req_ids: tuple):
    """A pull request's checks: run against its CURRENT head set. A request
    with no key (the hooks: submit, a member's head moved, a member added) runs
    only when that set has no finished row with its images on disk — the
    idempotence. A request naming a key (Re-run, a missing image, a restart)
    runs when the key is still current; a superseded key's unfinished rows say
    so instead of waiting forever."""
    ctx = ChangeCtx(subj)
    with db.connect() as conn:
        ensure_schema(conn)
        _done_requests(conn, req_ids)
        if requested and requested != ctx.sha:
            conn.execute(
                "UPDATE checks SET status='skipped', ended_at=?, summary="
                "'superseded — a member moved; the current head set has its "
                "own run' WHERE number=? AND sha=? AND status IN "
                "('queued','running')", (db.now(), subj.number, requested))
            conn.commit()
            return None
        if not requested:
            done = [r for r in read(conn, subj.number, ctx.sha)
                    if r["status"] not in ("queued", "running")]
            if done and all(_images_present(subj.number, r) for r in done):
                conn.commit()
                return None
        conn.commit()
    return ctx


def _images_present(number: str, row: dict) -> bool:
    try:
        d = image_dir(number, row["sha"], row["name"])
    except ValueError:
        return False
    return all((d / f"{i}.png").is_file() for i in image_indices(row))


def _announce(conn, ctx: "ChangeCtx", name: str, res: dict) -> None:
    """The bot's line on the pull request's timeline, GitHub's way:
    "physical-diff passed · 4 meshes changed", the first image inline."""
    from . import changes
    imgs = res.get("images") or []
    changes.event(conn, "checks.completed", ctx.cid, actor="plm-checks",
                  data={"check": name, "status": res.get("status", ""),
                        "summary": res.get("summary", ""), "commit": ctx.sha,
                        "image": imgs[0] if imgs else None})


def _done_requests(conn, req_ids: tuple) -> None:
    if req_ids:
        conn.execute(f"DELETE FROM check_requests WHERE id IN "
                     f"({','.join('?' * len(req_ids))})", tuple(req_ids))


def _finish(conn, number: str, sha: str, name: str, res: dict) -> None:
    status = res.get("status", "error")
    if status not in STATUSES:
        status = "error"
    conn.execute(
        "UPDATE checks SET status=?, summary=?, details_md=?, facts_json=?, "
        "points_json=?, images_json=?, ended_at=? "
        "WHERE number=? AND sha=? AND name=?",
        (status, (res.get("summary") or "")[:2000], res.get("details_md") or "",
         json.dumps(res["facts"]) if res.get("facts") is not None else "",
         json.dumps(res.get("points_at") or []),
         json.dumps(res["images"]) if res.get("images") else "",
         db.now(), number, sha, name))


def requeue_stale() -> int:
    """Crash safety, run once at startup.

    A `running` row older than the timeout cannot still be running — this
    process has only just started. Those, and anything left `queued`, go back on
    the queue. Deliberately not a `fail`: the commit was never judged, and a
    crash is not a finding about the part.
    """
    if not ENABLED:
        return 0
    try:
        with db.connect() as conn:
            ensure_schema(conn)
            cut = db.now() - STALE_AFTER_S
            jobs: dict[tuple, dict] = {}
            for r in conn.execute(
                    "SELECT number, sha, item_id, name FROM checks "
                    "WHERE status='queued' OR (status='running' AND "
                    "COALESCE(started_at,0) < ?)", (cut,)):
                job = jobs.setdefault((r["number"], r["sha"]),
                                      {"item_id": r["item_id"], "names": []})
                job["names"].append(r["name"])
            conn.execute(
                "UPDATE checks SET status='queued', started_at=NULL "
                "WHERE status='running' AND COALESCE(started_at,0) < ?", (cut,))
            conn.commit()
    except Exception:
        return 0
    # Only the checks that were actually in flight. Re-running the whole board
    # would be correct but wasteful — and on a repo full of history it would
    # queue a `minutes`-cost render for every commit a restart caught.
    for (number, sha), job in jobs.items():
        enqueue(number, sha, names=job["names"], item_id=job["item_id"] or 0)
    return len(jobs)


def _in_memory(number: str = "", sha: str = "") -> int:
    """Jobs on THIS process's queue (or being picked up) that have not written
    their `queued` rows yet — the window right after a push."""
    n = 0
    try:
        with _q.mutex:
            jobs = list(_q.queue)
        cur = _current
        for j in jobs + ([cur] if cur else []):
            if (not number or j[0] == number) and (not sha or j[1] == sha):
                n += 1
    except Exception:                                       # noqa: BLE001
        return 0
    return n


def wait_settled(number: str, sha: str, wait: float, *,
                 grace: float = 10.0) -> list[dict]:
    """Stage 16: block — at most `wait` seconds
    (capped at 300) — until this commit's checks have FINISHED, and return its
    rows. ONE implementation for `GET …/checks?wait=` and the MCP's
    `get_checks(wait=)`.

    ⚠️ "No rows" is not "done". Right after a push the job sits on the
    runner's in-memory queue (or in another process's relay outbox) before its
    `queued` rows exist; the first `?wait=` returned at once in that window,
    with an empty board. A commit with no rows is therefore waited on for
    `grace` seconds (normally the runner needs milliseconds), and only then
    reported as "nothing measured"."""
    t0 = time.monotonic()
    deadline = t0 + min(max(float(wait or 0), 0.0), 300.0)
    grace_end = t0 + min(grace, max(float(wait or 0), 0.0))
    while True:
        with db.connect() as conn:
            rows = read(conn, number, sha)
        busy = any(r["status"] in ("queued", "running") for r in rows) \
            or pending(number, sha) or _in_memory(number, sha)
        now = time.monotonic()
        if now >= deadline:
            return rows
        if rows and not busy:
            return rows
        if not rows and not busy and now >= grace_end:
            return rows
        time.sleep(0.4)


def pending(number: str = "", sha: str = "") -> int:
    """How many rows are still queued or running — what `?wait=` waits on."""
    try:
        with db.connect() as conn:
            ensure_schema(conn)
            q = ("SELECT COUNT(*) FROM checks WHERE status IN "
                 "('queued','running')")
            # …and the requests a relay process made that no runner has
            # turned into rows yet: "pending" must not read 0 in that window
            r = "SELECT COUNT(*) FROM check_requests WHERE 1=1"
            args: list = []
            if number:
                q += " AND number=?"
                r += " AND number=?"
                args.append(number)
            if sha:
                q += " AND sha=?"
                r += " AND sha=?"
                args.append(sha)
            return (conn.execute(q, args).fetchone()[0]
                    + conn.execute(r, args).fetchone()[0])
    except Exception:
        return 0


def main(argv: list[str] | None = None) -> int:
    """`python -m plm.checks <ITEM|CHG-n> [--ref R] [--names a,b] [--force]` —
    run a subject's checks NOW, in this process, and print the rows. For an
    operator or a test; the service runs the same `_run_job` on its thread.
    A pull request without `--force` re-renders only a head set it has no
    finished `physical-diff` for (the hooks' idempotence)."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m plm.checks")
    ap.add_argument("subject", help="an item number, or a pull request CHG-n")
    ap.add_argument("--ref", default="")
    ap.add_argument("--names", default="", help="comma-separated; default all")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    ensure_schema()
    subj = subject(a.subject)
    sha, _ = subj.resolve("", a.ref)
    names = [n for n in a.names.split(",") if n] or None
    _run_job(subj.number, sha if (a.force or not subj.is_change) else "", names,
             0, SOURCE_BUILTIN)
    with db.connect() as conn:
        rows = read(conn, subj.number, sha)
    for r in rows:
        print(f"{r['status']:8} {r['name']}: {r['summary']}"
              + (f"  ({r['duration_s']}s)" if r.get("duration_s") else ""))
        for im in r["images"]:
            print(f"         image: {im['image_url']}  — {im['caption']}")
    return 0 if all(r["status"] != "error" for r in rows) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
