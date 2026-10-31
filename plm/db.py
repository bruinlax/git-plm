"""SQLite index for plm.

The database is an index over the git vault, not a second source of truth:
every row can be rebuilt by the importer. Reads must never touch the
filesystem — that is the whole speed contract of v2.
"""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  number TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'part',
  project TEXT NOT NULL DEFAULT '',
  folder TEXT,                       -- repo-relative vault folder, if any
  status TEXT NOT NULL DEFAULT '',   -- item-level status from the index csv
  latest_rev_id INTEGER,             -- denormalized: current revision
  meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_project ON items(project);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);

-- Generic revision attributes (plm/attributes.py, docs/PLM_DATA_MODEL.md): the
-- "things people build in CAD" — mass, material, bounding box, colour, print
-- time. REVISION-level, because geometry changes change them. Names are free
-- text; attributes.WELL_KNOWN is a display registry, never a gate. Two names
-- (material, mass_g) are column-backed on revisions and are merged into this
-- surface at read time — ONE HOME PER FACT, one uniform read.
CREATE TABLE IF NOT EXISTS attributes (
  id INTEGER PRIMARY KEY,
  revision_id INTEGER NOT NULL REFERENCES revisions(id),
  name TEXT NOT NULL,
  value TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',    -- where the number came from (slicer, CAD…)
  updated_at REAL NOT NULL,
  UNIQUE(revision_id, name)
);
CREATE INDEX IF NOT EXISTS idx_attr_rev ON attributes(revision_id);

-- FABRICATION ROUTINGS (plm/fab.py, docs/PLM_DATA_MODEL.md): the
-- ordered list of work that turns raw stock into this part — CUT, DRILL, NOTCH,
-- COPE, BEND, DEBURR, WELD, FINISH (and CUT_PROFILE/SCRIBE for the sheet lane).
--
-- REVISION-level, for the reason attributes are: geometry changes change the
-- work. Shorten a rail and the CUT length moves; relocate a receiver and the
-- NOTCH datums move. The routing carries forward on revise() exactly like files
-- and attributes (ops.revise -> fab.copy).
--
-- ONE TABLE, not a routing/operation/resource trio. An operation is "a verb, a
-- place in the order, and some numbers", and the numbers differ per verb —
-- which is a JSON params column with a registry gloss, the same shape
-- `attributes` and `relations` already took. `op` is validated against
-- fab.OPERATIONS (a typo'd verb is work no shop surface will ever route);
-- `params` is open (a param nobody has thought of yet is still a fact somebody
-- measured). Standard/setup times and a work-centre master are deliberately
-- absent: there is one of each machine, so `work_center` is a text hint, and a
-- fabricated standard time on a controlled record is worse than no time at all.
CREATE TABLE IF NOT EXISTS operations (
  id INTEGER PRIMARY KEY,
  revision_id INTEGER NOT NULL REFERENCES revisions(id),
  seq INTEGER NOT NULL,                   -- 10, 20, 30… room to insert
  op TEXT NOT NULL,                       -- fab.OPERATIONS key
  params TEXT NOT NULL DEFAULT '{}',      -- JSON, unit-bearing dimensions
  work_center TEXT NOT NULL DEFAULT '',   -- a hint until there are two saws
  notes TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL,
  UNIQUE(revision_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ops_rev ON operations(revision_id);

CREATE TABLE IF NOT EXISTS revisions (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  version TEXT NOT NULL,
  vmaj INTEGER NOT NULL DEFAULT 0,   -- numeric triple for ordering
  vmin INTEGER NOT NULL DEFAULT 0,
  vpat INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'DRAFT',
  date TEXT NOT NULL DEFAULT '',
  ecn TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  verification TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',   -- generating source file (e.g. .scad)
  selector TEXT NOT NULL DEFAULT '', -- how to regenerate from it
  git_commit TEXT NOT NULL DEFAULT '',
  git_tag TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  UNIQUE(item_id, version)
);
CREATE INDEX IF NOT EXISTS idx_rev_item ON revisions(item_id, vmaj, vmin, vpat);

-- FILES (plm/roles.py). Two independent facts about a file, and the
-- whole of is refusing to tangle them again:
--
--   media  WHAT THE BYTES ARE. Measured — sniffed from magic numbers, never
--          from the filename. Works identically for a vault path and a
--          content-addressed object-store key, which is the bug it closes: the old
--          `path LIKE '%.stl'` predicates matched NOTHING for an offloaded
--          file, so flipping STORAGE_DEFAULT would have silently emptied the
--          thumbnailer.
--   role   WHAT THE FILE IS FOR. Declared, and validated against
--          roles.ROLES — a purpose no surface acts on is a typo. Not derivable
--          from the bytes: a PNG can be a render, a drawing, or a photograph
--          of a cracked weld, and only the author knows which.
--
-- *_source is PROVENANCE, and it is why this is not just a rename:
-- 'declared' (somebody said so) | 'sniffed' (measured) | 'extension' (guessed
-- from the name) | '' (never classified). The predecessor of this design wrote
-- importer.py's ROLE_BY_EXT guess straight into `role`, so a guess was
-- indistinguishable from a fact — which is precisely why no consumer trusted
-- the field and the filename regex grew back. A backfilled guess is now stored
-- AS a guess and rendered as one.
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  revision_id INTEGER NOT NULL REFERENCES revisions(id),
  role TEXT NOT NULL DEFAULT 'artifact',  -- roles.ROLES key (declared purpose)
  path TEXT NOT NULL,                     -- repo-relative
  sha256 TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  media TEXT NOT NULL DEFAULT ''          -- measured format (MIME)
);
CREATE INDEX IF NOT EXISTS idx_files_rev ON files(revision_id);
-- Geometry lookups are now `media IN (...)`, so they need this to stay as cheap
-- as the extension scan was.
CREATE INDEX IF NOT EXISTS idx_files_media ON files(revision_id, media);

-- TAGS (plm/tags.py): the cross-cutting labels that were being written
-- as prose. On prod, 51 occurrences of MEASURE / PROVISIONAL / UNPROVEN /
-- UNVERIFIED / OPEN lived inside operations.notes, revisions.summary and
-- revisions.verification — gate-worthy engineering facts in shouty capitals
-- because the author knew they mattered and had nowhere structured to put them.
-- "Which parts carry an unverified datum?" was a grep, so it was never asked.
--
-- The prose STAYS. A sentence explains; a tag finds. `note` holds the why so the
-- two never compete.
--
-- REGISTRY IS A GLOSS, NOT A GATE — the attributes.py rule, for the same reason:
-- the moment a new word needs a code change, the person holding the fact writes
-- a sentence instead and we are back to grepping. Enforcement lives on `role`,
-- where the vocabulary is genuinely closed.
--
-- item_id ALWAYS; revision_id only when the tag is revision-scoped. Some tags
-- describe the thing forever ('playa-exposed'), some describe one revision's
-- state of knowledge ('provisional-datum' — exactly what a revision fixes). A
-- revision-scoped caveat must not leak forward silently; tags.copy carries it
-- on revise deliberately, because bumping a version does not measure the truck.
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  revision_id INTEGER REFERENCES revisions(id),  -- NULL = applies to all revs
  tag TEXT NOT NULL,                             -- normalized: lower-hyphen
  note TEXT NOT NULL DEFAULT '',                  -- WHY. The sentence.
  actor TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
-- COALESCE in the index so an item-level tag (revision_id NULL) is unique too:
-- in SQLite, NULLs are distinct, so a plain UNIQUE would allow the same
-- item-level tag to be added without limit.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_uniq
  ON tags(item_id, COALESCE(revision_id,0), tag);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS bom (
  id INTEGER PRIMARY KEY,
  parent_rev_id INTEGER NOT NULL REFERENCES revisions(id),
  find_number INTEGER,
  child_item_id INTEGER NOT NULL REFERENCES items(id),
  child_version TEXT NOT NULL DEFAULT '',
  qty REAL NOT NULL DEFAULT 1,
  unit TEXT NOT NULL DEFAULT 'EA',
  alt_group TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bom_parent ON bom(parent_rev_id);
CREATE INDEX IF NOT EXISTS idx_bom_child ON bom(child_item_id);

-- Identity: humans and bots. `sub` binds a row to an OIDC subject once the
-- gatekeeper fronts this service; until then usernames are trusted on the
-- private network. Roles gate the release workflow (approver) and writes (editor).
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  sub TEXT NOT NULL DEFAULT '',
  display TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'human',   -- human | bot
  email TEXT NOT NULL DEFAULT '',
  roles TEXT NOT NULL DEFAULT '',       -- csv: approver,editor
  created_at REAL NOT NULL
);

-- The release gate: a pending row freezes a revision in IN_REVIEW until an
-- approver decides; approval runs the workflow (state -> target_state).
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY,
  revision_id INTEGER NOT NULL REFERENCES revisions(id),
  target_state TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  requested_by TEXT NOT NULL,
  requested_at REAL NOT NULL,
  approver TEXT NOT NULL DEFAULT '',
  decision TEXT NOT NULL DEFAULT '',    -- '' pending | approved | rejected
  decided_at REAL,
  decision_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals(decision, requested_at);

-- RELEASE — the second object (docs/PLM_REVIEW_MODEL.md §2.1b, plm/release.py).
--
-- A PROPOSAL answers "is this change right?" and ends in a merge and a version
-- tag. A RELEASE answers "is this revision ready to build against?", acts on an
-- EXISTING tagged revision with NO file change, and is a decision rather than a
-- merge. Before this, taking a part from PROTOTYPE to RELEASED meant minting a
-- revision whose design did not change — a version bump that records a decision,
-- which is wrong in Teamcenter terms (status is applied TO a revision) and wrong
-- in git terms (you do not commit to cut a release).
--
-- Nothing here moves a tag or writes a commit on `main`. The decision is forge
-- state, mirrored into `refs/notes/plm` so a clone can see it.
CREATE TABLE IF NOT EXISTS releases (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  revision_id INTEGER NOT NULL REFERENCES revisions(id),
  version TEXT NOT NULL,
  tag TEXT NOT NULL DEFAULT '',            -- vX.Y.Z — never moved by this
  tag_sha TEXT NOT NULL DEFAULT '',        -- the commit it points at
  from_maturity TEXT NOT NULL DEFAULT '',
  to_maturity TEXT NOT NULL DEFAULT '',
  workflow TEXT NOT NULL DEFAULT '',       -- the RELEASE_WORKFLOWS key
  state TEXT NOT NULL DEFAULT 'requested', -- requested|in_review|approved|
                                           -- rejected|withdrawn
  notes_md TEXT NOT NULL DEFAULT '',       -- generated, then edited
  evidence_sha256 TEXT NOT NULL DEFAULT '',
  evidence_key TEXT NOT NULL DEFAULT '',
  note_commit TEXT NOT NULL DEFAULT '',    -- the refs/notes/plm commit
  origin TEXT NOT NULL DEFAULT 'promotion',-- promotion | proposal (the shortcut)
  approval_id INTEGER,                     -- set when it came through a proposal
  requested_by TEXT NOT NULL DEFAULT '',
  requested_at REAL NOT NULL,
  decided_by TEXT NOT NULL DEFAULT '',
  decided_kind TEXT NOT NULL DEFAULT '',   -- human | bot (never policy: §2.1b)
  decided_at REAL,
  decision_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_releases_pending ON releases(state, requested_at);
CREATE INDEX IF NOT EXISTS idx_releases_item ON releases(item_id, version);

-- A release is a small WORKFLOW with recorded signoffs, not one click (§2.1b).
-- One row per approval or attested checklist line, BOUND TO THE TAG SHA with the
-- verified identity — which is the e-signature Teamcenter wants and never quite
-- had, because a signature over a commit sha is a signature over the exact bytes
-- of the part, its BOM pins and its metadata.
CREATE TABLE IF NOT EXISTS release_signoffs (
  id INTEGER PRIMARY KEY,
  release_id INTEGER NOT NULL REFERENCES releases(id),
  kind TEXT NOT NULL,                      -- approval | checklist | check
  item TEXT NOT NULL DEFAULT '',           -- the checklist line; '' for approval
  signer TEXT NOT NULL,
  signer_kind TEXT NOT NULL DEFAULT '',    -- human | bot
  verdict TEXT NOT NULL DEFAULT '',        -- approve|reject|attest|fail|pass
  note TEXT NOT NULL DEFAULT '',
  tag_sha TEXT NOT NULL DEFAULT '',
  identity TEXT NOT NULL DEFAULT '',       -- oidc sub | token | trusted-network
  signed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signoffs_release ON release_signoffs(release_id);

-- APPROVAL POLICY (plm/policy.py, stage 3c). Who may say yes, per project and
-- target maturity. One row is the whole setting; '' means "every", so the
-- registry-wide default is a row like any other and the most specific match
-- wins. Deliberately has no HTTP write route: a gate the gated thing can widen
-- is not a gate. Written by `python -m plm.policy`, read by every door.
CREATE TABLE IF NOT EXISTS policy (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL DEFAULT '',        -- '' = every project
  target_state TEXT NOT NULL DEFAULT '',   -- '' = every maturity
  approver_kinds TEXT NOT NULL DEFAULT '', -- csv human,bot  ('' = inherit)
  auto TEXT NOT NULL DEFAULT '',           -- '' inherit | yes | no
  note TEXT NOT NULL DEFAULT '',           -- shown as the effective rule
  actor TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL,
  UNIQUE (project, target_state)
);

-- Typed item-to-item relations (plm/relations.py, docs/PLM_RELATIONS.md): the
-- generic link surface. `relation` is a key in relations.REGISTRY, which owns
-- the labels, the allowed kinds and whether the versions are pinned —
-- derived_from (forks) is item-level, coupon_for points at the SPECIFIC
-- revision a coupon validates. Revision pointers are TEXT versions, the same
-- idiom bom.child_version uses, so pre-existing rows keep working unchanged.
CREATE TABLE IF NOT EXISTS relations (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),      -- the FROM side
  relation TEXT NOT NULL,
  target_item_id INTEGER NOT NULL REFERENCES items(id),  -- the TO side
  target_version TEXT NOT NULL DEFAULT '',            -- '' = item-level
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rel_item ON relations(item_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relations(target_item_id);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  supplier TEXT NOT NULL DEFAULT '',
  manufacturer TEXT NOT NULL DEFAULT '',
  mpn TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sources_item ON sources(item_id);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  item_id INTEGER,
  revision_id INTEGER,
  type TEXT NOT NULL,
  at REAL NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);

-- Advisory item-level lock, Teamcenter check-out style. A row with
-- released_at NULL is an active checkout; check-in and revise release it.
CREATE TABLE IF NOT EXISTS checkouts (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  actor TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL,
  released_at REAL
);
CREATE INDEX IF NOT EXISTS idx_checkouts_item ON checkouts(item_id, released_at);

-- Context (plm/context.py, docs/PLM_DATA_MODEL.md): the "why" as a first-class
-- object — "basically comments", and comments attach to ANY object the
-- context.TARGETS registry names. Append-only: a log of reasoning, never a
-- mutable field.
--
-- target_type '' is the CLASSIC form every pre-existing row is written in and
-- the ONE home for a comment on a part: item_id is the item, revision_id NULL
-- for item-level or a revision for revision-level. A non-empty target_type
-- ('relation', 'approval', …) puts the comment on that row instead, with
-- target_id naming it — and item_id/revision_id still populated as the target's
-- ANCHOR, which is why item_id could stay NOT NULL (two ADD COLUMNs, no table
-- rebuild) and why events and search keep working with no special cases.
CREATE TABLE IF NOT EXISTS context (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  revision_id INTEGER,
  body TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_item ON context(item_id);

-- Integrations framework: PLM stays PLM; everything downstream (printers,
-- notifiers, ...) registers here. action_label != '' puts a button on every
-- revision card; on_events (csv of event types) subscribes to webhooks.
CREATE TABLE IF NOT EXISTS integrations (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  url TEXT NOT NULL,
  action_label TEXT NOT NULL DEFAULT '',
  on_events TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  config TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);

-- OIDC (plm/identity.py). Sessions are server-side so a logout is real; the
-- cookie carries only an opaque id. `users.sub` is the durable link from a
-- provider subject to a plm identity, which is what makes an approval record
-- name a person rather than whatever string the client sent.
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  sub TEXT NOT NULL,
  email TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  groups TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oidc_flows (
  state TEXT PRIMARY KEY,
  nonce TEXT NOT NULL,
  verifier TEXT NOT NULL,
  redirect_to TEXT NOT NULL DEFAULT '/',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_sub ON users(sub);

-- Disposable 3D snippets (plm/snippets.py, docs/PLM_SNIPPETS.md): a temporary
-- STL an agent hands a human as a link — "hey, do you like this?" — with NO
-- registry footprint. Deliberately its own table and not items/revisions/files:
-- a tracked part is a promise to keep the bytes forever, a snippet is the
-- opposite promise. The bytes live at CACHE_DIR/<key>; both the row and the
-- bytes are garbage-collected once expires_at passes.
CREATE TABLE IF NOT EXISTS snippets (
  token TEXT PRIMARY KEY,            -- unguessable, IS the capability
  name TEXT NOT NULL DEFAULT '',     -- sanitized filename shown in the viewer
  note TEXT NOT NULL DEFAULT '',     -- what the agent wants looked at
  key TEXT NOT NULL,                 -- 'snippets/<token>/<name>'
  sha256 TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  actor TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snippets_expires ON snippets(expires_at);

-- Staged uploads (plm/staging.py, docs/PLM_UPLOADS.md): the ticket in the
-- coat-check for a big binary an agent PUT to plm's own upload inbox
-- (CACHE_DIR/inbox/<token>/<name>). Not a `files` row and never will be one:
-- check-in copies the bytes into the revision and consumes the stage. Whatever
-- is never claimed is garbage-collected at expires_at, the same contract
-- snippets have.
CREATE TABLE IF NOT EXISTS staging (
  token TEXT PRIMARY KEY,            -- unguessable; the handle's authority
  name TEXT NOT NULL DEFAULT '',     -- sanitized filename
  key TEXT NOT NULL,                 -- 'staging/<token>/<name>' = the handle
  sha256 TEXT NOT NULL DEFAULT '',   -- declared at mint; the ARRIVED hash once received_at is set
  size INTEGER NOT NULL DEFAULT 0,   -- declared at mint (0 = not declared); arrived once received
  actor TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  received_at REAL                   -- when the bytes landed (NULL = not yet)
);
CREATE INDEX IF NOT EXISTS idx_staging_expires ON staging(expires_at);

-- IDEMPOTENCY (plm/idem.py): the replay record for a write whose
-- RESPONSE was lost. Almost every plm write is already idempotent by
-- construction — a version is immutable, so a repeated check-in is a 409; a
-- number is unique, so a repeated create with an explicit number is a 409. The
-- gap this table closes is the one place plm MINTS identity for you
-- (auto-assigned numbers in create_item/fork): there, attempt two would happily
-- allocate a second number and leave an empty duplicate shell behind, which is
-- exactly what a 30 s client timeout produced. A row here means "this exact
-- request already ran; here is what it returned".
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,              -- the caller's key, or 'auto:<sha256>'
  op TEXT NOT NULL,                  -- create_item | fork | ...
  actor TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL DEFAULT '',   -- normalized request, for auto keys
  response TEXT NOT NULL DEFAULT '{}',    -- what the first attempt returned
  item_id INTEGER,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency(expires_at);
CREATE INDEX IF NOT EXISTS idx_idem_fingerprint ON idempotency(fingerprint, created_at);
"""


# -- the connection: ONE writer, bounded waiting --------------------
# SQLite serializes writers. plm's transactions are supposed to be milliseconds
# long, so contention should be invisible — but when it happens, a caller must
# get a bounded wait and then a refusal that says what it waited for, never a
# bare `database is locked` after three hopeful retries.
#
# Two layers, and the order matters:
#
#   1. PRAGMA busy_timeout — SQLite's own wait. A statement that cannot get the
#      writer lock sleeps and retries INSIDE sqlite for this long.
#   2. This connection class — after busy_timeout expires, retry the STATEMENT,
#      with exponential backoff and jitter, config.WRITE_RETRIES more times.
#
# Layer 2 is safe for a reason worth stating plainly: SQLITE_BUSY means the
# statement DID NOT RUN. There is no partial effect to compensate for, so
# re-issuing the identical statement is exactly equivalent to having issued it
# later — which is why this can live at the bottom of the stack and cover every
# call site instead of being sprinkled over a hundred endpoints.
#
# What this does NOT do, on purpose: retry a whole transaction. A transaction
# that failed half-way may have side effects OUTSIDE sqlite (a git commit in the
# vault, an object-store write), and replaying those is not a retry, it is a second
# write. Those paths are made short and atomic instead (ops.checkin).

BUSY_MARKERS = ("database is locked", "database table is locked",
                "database schema is locked")

# Observability: how much contention this process actually saw. Surfaced by
# GET /api/stats, so "is the registry contended?" is a question with an answer
# rather than a guess from log archaeology.
STATS = {"busy_retries": 0, "busy_failures": 0, "worst_wait_s": 0.0}
_STATS_LOCK = threading.Lock()


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in BUSY_MARKERS)


def _note(retries: int, waited: float, failed: bool = False) -> None:
    with _STATS_LOCK:
        STATS["busy_retries"] += retries
        if failed:
            STATS["busy_failures"] += 1
        STATS["worst_wait_s"] = max(STATS["worst_wait_s"], waited)


def _with_retry(what: str, fn, *args):
    """Run one sqlite statement, waiting out transient contention.

    Bounded: config.WRITE_RETRIES attempts after the first, each preceded by
    busy_timeout inside sqlite plus a backoff here. When the budget is gone the
    error explains what was contended and for how long, because "database is
    locked" on its own has cost this registry an afternoon."""
    started = time.monotonic()
    delay = 0.05
    for attempt in range(config.WRITE_RETRIES + 1):
        try:
            out = fn(*args)
            if attempt:
                _note(attempt, time.monotonic() - started)
            return out
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt >= config.WRITE_RETRIES:
                if _is_busy(exc):
                    waited = time.monotonic() - started
                    _note(attempt, waited, failed=True)
                    raise sqlite3.OperationalError(
                        f"{exc} — plm waited {waited:.1f}s across "
                        f"{attempt + 1} attempts for the SQLite writer while "
                        f"running {what}. Another write is holding the lock "
                        "longer than it should; nothing was written, so this is "
                        "safe to retry. If it persists, GET /api/stats "
                        "('sqlite') shows the contention counters."
                    ) from exc
                raise
            time.sleep(delay + random.uniform(0, delay))
            delay *= 3


class PlmConnection(sqlite3.Connection):
    """A connection whose statements wait out contention instead of surfacing it.

    Every write in plm goes through conn.execute / conn.executemany, so putting
    the bounded retry here covers all of them with no per-endpoint bookkeeping —
    and cannot be forgotten by the next endpoint somebody adds.

    IT ALSO CARRIES THE GIT UNDO JOURNAL (stage 3c, the design notes
    known bug 1). A git-first write is two stores: a commit lands on a branch,
    then rows are derived from it. SQLite can roll the rows back; git has no
    transaction, so without this the branch would be left ahead of the registry
    — a window that was hit during stage 3b's development.

    The fix is put where it cannot be forgotten rather than at twenty call
    sites: whoever moves a ref as part of a database operation registers the
    undo here (`conn.on_rollback`), and a ROLLBACK runs it. A COMMIT clears it,
    because at that point both halves are durable and the same journal would
    undo a write that succeeded.
    """

    #: zero-argument callables, run newest-first if the transaction rolls back.
    _undo: list

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        return _with_retry(_verb(sql), sqlite3.Connection.execute, self, sql,
                           parameters)

    def executemany(self, sql, parameters, /):  # type: ignore[override]
        return _with_retry(_verb(sql), sqlite3.Connection.executemany, self,
                           sql, parameters)

    def on_rollback(self, fn) -> None:
        """Run `fn` if this connection's transaction is rolled back.

        Registered AFTER the side effect has happened and cleared on commit, so
        the journal always describes exactly the work that is not yet durable in
        SQLite.
        """
        try:
            self._undo.append(fn)
        except AttributeError:
            self._undo = [fn]

    def _drain_undo(self) -> list:
        undo = list(getattr(self, "_undo", ()))
        self._undo = []
        return undo

    def _run_undo(self) -> None:
        for fn in reversed(self._drain_undo()):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                # An undo that fails must not replace the original error with
                # its own — the caller is already being told why the write was
                # refused. `gitindex --rebuild` heals whatever is left.
                _log_undo_failure(exc)

    def commit(self) -> None:  # type: ignore[override]
        _with_retry("COMMIT", sqlite3.Connection.commit, self)
        self._drain_undo()

    def rollback(self) -> None:  # type: ignore[override]
        sqlite3.Connection.rollback(self)
        self._run_undo()

    # ⭐ AND THE CONTEXT MANAGER, WHICH IS NOT THE SAME THING.
    #
    # `with conn:` does NOT go through the two methods above. `sqlite3`'s
    # `__exit__` is implemented in C and calls the interpreter's own commit /
    # rollback directly, so a subclass override of `commit`/`rollback` is
    # invisible to it. Every write in plm is inside a `with db.connect()`, so
    # without this the undo journal would have been drained by nothing and the
    # whole two-store guarantee would have been a comment.
    #
    # Found by the fault-injection test, which is exactly what it is for.
    def __enter__(self):
        sqlite3.Connection.__enter__(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        out = sqlite3.Connection.__exit__(self, exc_type, exc, tb)
        if exc_type is None:
            self._drain_undo()
        else:
            self._run_undo()
        return out


def _log_undo_failure(exc: Exception) -> None:
    print(f"plm: git undo after rollback failed: {exc}", file=sys.stderr,
          flush=True)


def _verb(sql: str) -> str:
    """The first few words of a statement — enough to name it in an error
    without pasting somebody's BOM into a log line."""
    return " ".join(str(sql).split()[:4])


def connect() -> PlmConnection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH,
                           timeout=config.BUSY_TIMEOUT_MS / 1000.0,
                           factory=PlmConnection)
    conn.row_factory = sqlite3.Row
    # journal_mode is persistent in the database file; the rest are per
    # connection. busy_timeout is set by `timeout=` above — stated again here
    # because a pragma you can read back is documentation and a constructor
    # argument is trivia.
    conn.execute(f"PRAGMA busy_timeout={config.BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Column migrations: additive only, safe to re-run.
        for stmt in (
                "ALTER TABLE bom ADD COLUMN suppressed INTEGER NOT NULL DEFAULT 0",
                # Material master data lives on the REVISION (geometry changes
                # change mass); the slicer is the instrument that measures it.
                "ALTER TABLE revisions ADD COLUMN material TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE revisions ADD COLUMN mass_g REAL",
                # Swappable storage (plm/storage.py): where a file's bytes
                # live. ''/'vault' = git working tree (all pre-existing rows).
                "ALTER TABLE files ADD COLUMN backend TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE files ADD COLUMN key TEXT NOT NULL DEFAULT ''",
                # Typed relations (plm/relations.py): the FROM side gets a
                # revision pointer of its own (a coupon revision verifies a
                # part revision), and links record who made them. Both default
                # to '' so every existing derived_from row stays valid.
                "ALTER TABLE relations ADD COLUMN from_version TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE relations ADD COLUMN actor TEXT NOT NULL DEFAULT ''",
                # RELATIONS BY HOME (stage 10, plm/relations.py HOMES). Five
                # additive columns, every default the truth about every
                # existing row:
                #   revision_id    the CLAIMANT revision of a revision- or
                #                  note-home claim; NULL = item-level. Backfilled
                #                  below from from_version, exactly — never
                #                  guessed (relations.claimant() owns the guess
                #                  and migrate_cleanup reports it).
                #   target_commit  the target's commit beside its version; ''
                #                  = not recorded (resolve from the version).
                #   result / result_note / measured   a `results` type's
                #                  outcome (coupon_for): pass|fail|pending, a
                #                  sentence, a JSON table. '' = none recorded.
                "ALTER TABLE relations ADD COLUMN revision_id INTEGER",
                "ALTER TABLE relations ADD COLUMN target_commit TEXT NOT NULL "
                "DEFAULT ''",
                "ALTER TABLE relations ADD COLUMN result TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE relations ADD COLUMN result_note TEXT NOT NULL "
                "DEFAULT ''",
                "ALTER TABLE relations ADD COLUMN measured TEXT NOT NULL "
                "DEFAULT ''",
                "CREATE INDEX IF NOT EXISTS idx_rel_revision ON "
                "relations(revision_id)",
                # PRODUCT = an EXPLICIT designation (the Teamcenter end-item
                # concept). The dashboard renders flagged products; product-
                # ness is never inferred from BOM topology, which cannot tell
                # a lantern from a bench-test rig.
                "ALTER TABLE items ADD COLUMN product INTEGER NOT NULL DEFAULT 0",
                # Identity history (ops.renumber_item): the numbers this item
                # USED to have, comma-separated. Lookups match these too, so an
                # old link, bookmark or agent memory still resolves instead of
                # 404-ing — the number is identity, and identity that changed
                # still has to be findable.
                "ALTER TABLE items ADD COLUMN former_numbers TEXT NOT NULL DEFAULT ''",
                # GENERIC CONTEXT (plm/context.py): a context entry is a
                # comment, and a comment hangs off whatever the TARGETS
                # registry names — a relation, an approval, later a change
                # order. Both columns default to the classic form, so every
                # pre-existing row keeps its item/revision meaning with no
                # data migration at all: '' means "read item_id/revision_id",
                # which is exactly what they already said.
                "ALTER TABLE context ADD COLUMN target_type TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE context ADD COLUMN target_id INTEGER",
                "CREATE INDEX IF NOT EXISTS idx_context_target "
                "ON context(target_type, target_id)",
                # FABRICATION (plm/fab.py): HOW this revision comes
                # into existence — '' | print | make. A COLUMN because it
                # DISPATCHES _build_plan and a dispatch key belongs where the
                # query can see it, exactly like material/mass_g.
                #
                # `process`, NOT `disposition`: docs/PLM_CHANGES.md already
                # burns that word for the ECN affected-item concept.
                #
                # DEFAULT '' IS THE WHOLE MIGRATION. Empty means "not stated",
                # and for a part _build_plan reads that as today's behaviour —
                # print. So every existing lantern part keeps printing with zero
                # rows changed, and MAKE is a thing you opt a revision into.
                # There is deliberately no 'buy' value: kind already says that.
                "ALTER TABLE revisions ADD COLUMN process TEXT NOT NULL DEFAULT ''",
                # CLASSIFICATION PROVENANCE (plm/roles.py). Where the
                # role and the media on a files row came from: declared |
                # sniffed | extension | '' (never classified).
                #
                # These two columns ARE the fix. plm already had `role`, and it
                # was populated by importer.py's ROLE_BY_EXT — a filename guess
                # written into a field that reads like a declaration. Because a
                # guess and a fact were indistinguishable, no consumer trusted
                # the field, and three `path LIKE '%.stl'` predicates grew back
                # to work around it. Measured on 837 prod rows: 60 STLs were not
                # role='artifact', and 122 role='artifact' rows were not
                # geometry at all.
                #
                # DEFAULT '' is the whole migration: every pre-existing row says
                # "never classified", which is the truth about them, and
                # roles.better_source guarantees the backfill can fill a blank
                # without ever overwriting something stated.
                "ALTER TABLE files ADD COLUMN role_source TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE files ADD COLUMN media_source TEXT NOT NULL DEFAULT ''",
                # WHO DECIDED, AS A KIND (stage 3c, plm/policy.py). `approver`
                # has always been a username; it is now also `policy` (the rule
                # merged it) or an agent (the project's policy admits agents).
                # Those three are different sentences about accountability and a
                # ledger that could not tell them apart would be worse than no
                # ledger — so the kind is stored, not inferred from the name.
                #
                # DEFAULT '' is the whole migration: every pre-existing row says
                # "not recorded", which is the truth about them (they were all
                # decided by geoff, but plm did not write that down).
                "ALTER TABLE approvals ADD COLUMN approver_kind TEXT NOT NULL "
                "DEFAULT ''",
                # PLM_GIT_MODE PER PROJECT (stage 9, plm/gitmode.py). off | read
                # | write, or '' = inherit. It rides on the `policy` table because
                # that table is already THE home of per-project behaviour; a row
                # with target_state='' carries it. DEFAULT '' is the whole
                # migration: every existing row inherits, which is `off` unless
                # the instance says otherwise.
                "ALTER TABLE policy ADD COLUMN git_mode TEXT NOT NULL DEFAULT ''",
                # ATTESTED BY REFERENCE (stage 10, plm/release.py EVIDENCE). A
                # checklist signoff may point at the claim that proves it —
                # `GL-CPN-0001@v0.1.0`, a passing coupon on the exact commit
                # being released — instead of a person's word alone. '' = by
                # hand, which is every existing row.
                "ALTER TABLE release_signoffs ADD COLUMN evidence TEXT NOT NULL "
                "DEFAULT ''",
                # The staged upload's bytes are hashed once, when they land
                # (plm/staging.py::land); this says they have.
                "ALTER TABLE staging ADD COLUMN received_at REAL"):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # already there
        # relations.revision_id from from_version — EXACT matches only, and only
        # for the types whose claimant is a revision (an item-home type's
        # revision_id stays NULL by definition). Idempotent: it only fills NULLs.
        try:
            from . import relations as _rel
            pinned = [t for t in _rel.REGISTRY if _rel.home(t) != "item"]
            conn.execute(
                "UPDATE relations SET revision_id=(SELECT v.id FROM revisions v "
                "WHERE v.item_id=relations.item_id AND "
                "v.version=relations.from_version) WHERE revision_id IS NULL "
                "AND from_version<>'' AND relation IN "
                f"({','.join('?' * len(pinned))})", pinned)
        except sqlite3.OperationalError:
            pass
        # Seed identities: the instance's admin approves (PLM_ADMIN, default
        # `admin` — the name plm/firstrun.py gives the first token);
        # agents and the importer are bots.
        admin = (os.environ.get("PLM_ADMIN") or "admin").strip()
        for username, kind, roles, display in (
                (admin, "human", "approver,editor", admin),
                ("agent", "bot", "editor", "Agent (bot)"),
                ("import", "bot", "", "vault importer"),
                ("mcp", "bot", "editor", "MCP agent")):
            conn.execute(
                "INSERT OR IGNORE INTO users (username,kind,roles,display,created_at) "
                "VALUES (?,?,?,?,?)", (username, kind, roles, display, now()))


def get_user(conn: sqlite3.Connection, username: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def user_has_role(conn: sqlite3.Connection, username: str, role: str) -> bool:
    u = get_user(conn, username)
    return bool(u) and role in [r.strip() for r in u["roles"].split(",") if r]


def now() -> float:
    return time.time()


# -- identity: resolving a reference to an item -------------------------------
# plm owns the numbers (ops.validate_number), and it owns their history: after a
# renumber the OLD number keeps resolving through items.former_numbers. Every
# read path goes through these two helpers rather than `WHERE number=?`, so the
# redirect is a property of the registry and not of one endpoint.

def item_by_number(conn: sqlite3.Connection, number: str) -> sqlite3.Row | None:
    """An item by its current number, or by any number it used to have."""
    number = (number or "").strip()
    if not number:
        return None
    row = conn.execute("SELECT * FROM items WHERE number=?", (number,)).fetchone()
    if row:
        return row
    return conn.execute(
        "SELECT * FROM items WHERE former_numbers!='' AND "
        "(','||former_numbers||',') LIKE ('%,'||?||',%')", (number,)).fetchone()


def resolve_item(conn: sqlite3.Connection, ref: str | int) -> sqlite3.Row | None:
    """An item by EITHER its integer id or a (current or former) number.

    Part numbers always carry their class token, so a bare integer is never
    ambiguous — which is what lets `/api/items/95` and
    `/api/items/GL-PRT-1001` be the same read (docs/PLM_DATA_MODEL.md)."""
    ref = ref if isinstance(ref, int) else str(ref).strip()
    if isinstance(ref, int) or str(ref).isdigit():
        return conn.execute("SELECT * FROM items WHERE id=?", (int(ref),)).fetchone()
    return item_by_number(conn, str(ref))


def item_id_for(conn: sqlite3.Connection, ref: str | int) -> int | None:
    row = resolve_item(conn, ref)
    return row["id"] if row else None


def parse_version(v: str) -> tuple[int, int, int]:
    m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?", (v or "").strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def bump_version(v: str, bump: str) -> str:
    maj, mnr, pat = parse_version(v)
    if bump == "major":
        return f"{maj + 1}.0.0"
    if bump == "minor":
        return f"{maj}.{mnr + 1}.0"
    return f"{maj}.{mnr}.{pat + 1}"


def active_checkout(conn: sqlite3.Connection, item_id: int) -> dict | None:
    """The item's live advisory lock, with its AGE and whether that age makes it
    stale (config.CHECKOUT_STALE_SECONDS). Age is part of the fact: a lock held
    for four minutes and a lock held by an agent that died yesterday are not the
    same object, and every caller that refuses a write needs to tell them
    apart (ops.claim_checkout)."""
    row = conn.execute(
        "SELECT * FROM checkouts WHERE item_id=? AND released_at IS NULL "
        "ORDER BY at DESC LIMIT 1", (item_id,)).fetchone()
    if not row:
        return None
    co = dict(row)
    co["age_s"] = max(0.0, now() - (co["at"] or 0))
    co["stale"] = co["age_s"] >= config.CHECKOUT_STALE_SECONDS
    return co


def release_checkout(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE checkouts SET released_at=? WHERE item_id=? AND released_at IS NULL",
                 (now(), item_id))


def list_checkouts(conn: sqlite3.Connection, *, stale_only: bool = False) -> list[dict]:
    """Every live checkout in the registry, oldest first — the recovery view.

    `released_at IS NULL` is the whole definition of held, so a row here whose
    holder is gone IS the stranded item; nothing else in the schema records
    that. Without this list the only way to find one was to try to write to it
    and read the 409."""
    out = []
    for row in conn.execute(
            """SELECT c.*, i.number, i.name, i.kind FROM checkouts c
               JOIN items i ON i.id=c.item_id
               WHERE c.released_at IS NULL ORDER BY c.at""").fetchall():
        co = dict(row)
        co["age_s"] = max(0.0, now() - (co["at"] or 0))
        co["age_h"] = round(co["age_s"] / 3600.0, 2)
        co["stale"] = co["age_s"] >= config.CHECKOUT_STALE_SECONDS
        if stale_only and not co["stale"]:
            continue
        out.append(co)
    return out


# -- writes -------------------------------------------------------------------

def upsert_item(conn: sqlite3.Connection, number: str, *, name: str = "",
                kind: str = "", project: str = "", folder: str | None = None,
                status: str = "", meta: dict | None = None) -> int:
    row = conn.execute("SELECT * FROM items WHERE number=?", (number,)).fetchone()
    if row:
        # Fill blanks, never blank out data that is already there.
        conn.execute(
            """UPDATE items SET
                 name=CASE WHEN ?!='' THEN ? ELSE name END,
                 kind=CASE WHEN ?!='' THEN ? ELSE kind END,
                 project=CASE WHEN ?!='' THEN ? ELSE project END,
                 folder=COALESCE(?, folder),
                 status=CASE WHEN ?!='' THEN ? ELSE status END,
                 updated_at=?
               WHERE id=?""",
            (name, name, kind, kind, project, project, folder,
             status, status, now(), row["id"]))
        return row["id"]
    if not kind:
        cls = number.split("-")[1] if "-" in number else ""
        kind = config.KIND_BY_CLASS.get(cls, "part")
    cur = conn.execute(
        "INSERT INTO items (number,name,kind,project,folder,status,meta,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (number, name, kind, project, folder, status,
         json.dumps(meta or {}), now(), now()))
    return cur.lastrowid


def upsert_revision(conn: sqlite3.Connection, item_id: int, version: str,
                    **fields: Any) -> int:
    vmaj, vmin, vpat = parse_version(version)
    row = conn.execute("SELECT id FROM revisions WHERE item_id=? AND version=?",
                       (item_id, version)).fetchone()
    if row:
        sets, vals = [], []
        for k, v in fields.items():
            if v not in (None, ""):
                sets.append(f"{k}=?")
                vals.append(v)
        if sets:
            conn.execute(f"UPDATE revisions SET {', '.join(sets)} WHERE id=?",
                         (*vals, row["id"]))
        return row["id"]
    cols = {"item_id": item_id, "version": version, "vmaj": vmaj, "vmin": vmin,
            "vpat": vpat, "created_at": now(), **{k: v for k, v in fields.items()
                                                  if v not in (None, "")}}
    cur = conn.execute(
        f"INSERT INTO revisions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        tuple(cols.values()))
    return cur.lastrowid


def refresh_latest(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        """UPDATE items SET latest_rev_id=(
             SELECT id FROM revisions WHERE item_id=?
             ORDER BY vmaj DESC, vmin DESC, vpat DESC LIMIT 1),
           updated_at=? WHERE id=?""",
        (item_id, now(), item_id))


def add_event(conn: sqlite3.Connection, type_: str, *, item_id: int | None = None,
              revision_id: int | None = None, actor: str = "", data: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO events (item_id,revision_id,type,at,actor,data) VALUES (?,?,?,?,?,?)",
        (item_id, revision_id, type_, now(), actor, json.dumps(data or {})))
    # PLM_GIT_MODE=read (stage 9): every write is evented, so this is the one
    # seam where a repo learns it is stale. In-memory only — `notice` never
    # touches the database (we are inside the writer's transaction) and returns
    # at once when git is off everywhere. A rolled-back write costs at most a
    # no-op re-split: `split` is deterministic.
    if item_id:
        from . import gitsync
        gitsync.notice(item_id)


def event_out_of_band(type_: str, *, item_id: int | None = None,
                      revision_id: int | None = None, actor: str = "",
                      data: dict | None = None) -> None:
    """Record an event on a connection of its OWN, committed immediately.

    For the events that have to survive the rollback of the transaction they
    describe: "this check-in failed and here is what plm undid in the vault" is
    worthless if it rolls back with the failure. Never raises — a lost audit
    line must not turn one failure into two."""
    try:
        with connect() as own:
            add_event(own, type_, item_id=item_id, revision_id=revision_id,
                      actor=actor, data=data)
    except Exception as exc:                                   # noqa: BLE001
        print(f"plm: could not record {type_} out of band: {exc}",
              file=sys.stderr, flush=True)


# -- reads --------------------------------------------------------------------

ITEM_LIST_SQL = """
SELECT i.id, i.number, i.name, i.kind, i.project, i.status, i.folder,
       i.updated_at,
       r.version AS latest_version, r.state AS latest_state, r.date AS latest_date,
       (SELECT COUNT(*) FROM revisions WHERE item_id=i.id) AS rev_count
FROM items i LEFT JOIN revisions r ON r.id = i.latest_rev_id
"""


def list_items(conn: sqlite3.Connection, q: str = "", kind: str = "",
               project: str = "", state: str = "") -> list[dict]:
    where, vals = [], []
    if q:
        where.append("(i.number LIKE ? OR i.name LIKE ?)")
        vals += [f"%{q}%", f"%{q}%"]
    if kind:
        where.append("i.kind=?"); vals.append(kind)
    if project:
        where.append("i.project=?"); vals.append(project)
    if state:
        where.append("r.state=?"); vals.append(state)
    sql = ITEM_LIST_SQL + (("WHERE " + " AND ".join(where)) if where else "")
    sql += " ORDER BY i.number"
    return [dict(r) for r in conn.execute(sql, vals)]


def get_item(conn: sqlite3.Connection, number: str | int) -> dict | None:
    """One item by id, current number, or former number. Every revision carries
    its `attributes` (the merged surface: rows plus the column-backed facts)."""
    from . import attributes as attrs
    found = resolve_item(conn, number)
    if not found:
        return None
    row = conn.execute(ITEM_LIST_SQL + "WHERE i.id=?", (found["id"],)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["former_numbers"] = [n for n in (found["former_numbers"] or "").split(",") if n]
    item["revisions"] = [dict(r) for r in conn.execute(
        "SELECT * FROM revisions WHERE item_id=? ORDER BY vmaj DESC, vmin DESC, vpat DESC",
        (item["id"],))]
    from . import fab as fabmod
    for rev in item["revisions"]:
        rev["attributes"] = attrs.get(conn, rev["id"])
        # The ROUTING rides in the same payload the attributes do, for the same
        # reason the BOM does: the UI shows the selected revision's work without
        # another round-trip, and an agent that read the item has already read
        # how the part is made. Empty list for everything that is not fabricated.
        rev["operations"] = fabmod.get_routing(conn, rev["id"])
    # TAGS (plm/tags.py). Item-level tags apply to every revision;
    # revision-level ones belong to the revision that carries them, so they are
    # returned separately rather than merged — a caveat that reads as
    # "unresolved on THIS revision" means something different from one that
    # describes the part forever.
    from . import tags as tagmod
    item["tags"] = [t for t in tagmod.for_item(conn, item["id"])
                    if t["scope"] == "item"]
    rev_ids = [r["id"] for r in item["revisions"]]
    item["files"] = {}
    if rev_ids:
        marks = ",".join("?" * len(rev_ids))
        state_of = {r["id"]: r["state"] for r in item["revisions"]}
        for f in conn.execute(f"SELECT * FROM files WHERE revision_id IN ({marks})", rev_ids):
            item["files"].setdefault(f["revision_id"], []).append(
                _file_out(dict(f), state_of.get(f["revision_id"], "")))
        for rev in item["revisions"]:
            rev["tags"] = [t for t in tagmod.for_item(conn, item["id"], rev["id"])
                           if t["scope"] == "revision"]
    item["sources"] = [dict(r) for r in conn.execute(
        "SELECT * FROM sources WHERE item_id=?", (item["id"],))]
    # WHO DECIDED EACH REVISION (stage 3c). `approver_kind` is `policy` when the
    # rule merged it, `bot` when the project's policy admits agents and one did,
    # `human` otherwise — three different sentences about accountability, so the
    # History row stamps them differently instead of all reading "approved".
    # Additive: a revision with no approval row carries empty strings, which is
    # what 446 of 454 production revisions honestly are.
    decided = {}
    if rev_ids:
        marks = ",".join("?" * len(rev_ids))
        for a in conn.execute(
                f"SELECT revision_id, approver, approver_kind, decision, "
                f"decided_at, target_state FROM approvals "
                f"WHERE revision_id IN ({marks}) AND decision!='' ORDER BY id",
                rev_ids):
            decided[a["revision_id"]] = dict(a)
        promoted = {}
        for r in conn.execute(
                f"SELECT * FROM releases WHERE revision_id IN ({marks}) "
                f"AND state='approved' ORDER BY id", rev_ids):
            promoted[r["revision_id"]] = {
                "id": r["id"], "to_maturity": r["to_maturity"],
                "decided_by": r["decided_by"], "decided_kind": r["decided_kind"],
                "decided_at": r["decided_at"],
                "evidence_sha256": r["evidence_sha256"], "origin": r["origin"]}
        for rev in item["revisions"]:
            d = decided.get(rev["id"]) or {}
            rev["approved_by"] = d.get("approver", "")
            rev["approver_kind"] = d.get("approver_kind", "")
            rev["release"] = promoted.get(rev["id"])
    return item


def _file_out(f: dict, state: str = "") -> dict:
    """One files row as an API payload: the stored columns plus what the SERVER
    already knows about them.

    Same principle as `thumb` (see plm/thumbs.py): a client must never have to
    re-derive a fact the server owns. The registry lives here, so the label, the
    'has anybody actually confirmed this' flag and the format/purpose mismatch
    warning are computed here — otherwise every surface reimplements the
    vocabulary and they drift, which is the failure this whole ticket is about.
    """
    from . import roles as rolemod
    spec = rolemod.ROLES.get(f.get("role") or "") or {}
    f["role_label"] = spec.get("label") or (f.get("role") or "")
    f["role_confirmed"] = not rolemod.is_guess(f.get("role_source") or "")
    f["role_legacy"] = bool(spec.get("legacy"))
    f["is_geometry"] = rolemod.is_geometry_media(f.get("media") or "")
    f["role_warning"] = rolemod.mismatch(f.get("role") or "", f.get("media") or "")
    # What else these bytes could plausibly be, when the answer is unconfirmed.
    # Handing the candidates over means the UI's confirm control is a short list
    # of real options rather than the whole vocabulary.
    f["role_candidates"] = ([] if f["role_confirmed"]
                            else rolemod.ambiguous(f.get("media") or ""))
    # ⭐ WHAT IT IS AND HOW IT IS SHOWN — `{kind, preview, view, diff}` from the
    # ONE media-kind table (plm/roles.MEDIA_KINDS). Before this, `web/app.js`
    # kept a second opinion (`previewKind()` over its own extension sets) and
    # they disagreed; now the server answers and the client renders.
    f.update(rolemod.media_block(str(f.get("path") or ""),
                                 str(f.get("media") or "")))
    # ⭐ WHAT YOU CAN DO WITH IT, decided here for the same reason the label is:
    # the registry lives on this side, and a client that re-derived "is this
    # printable?" would be a second opinion about the vocabulary. See
    # `plm/actions.py` — the answer is a function of the file's declared role and
    # its measured media, never of its filename.
    #
    # The revision's STATE rides along because two of the actions need it: only
    # a DRAFT can be edited in place, and an action that cannot tell must refuse
    # rather than offer. (Stage 3b: on a git-origin revision those edits are
    # real commits — `ops.set_file_role` / `ops.remove_file`.)
    from . import actions as actionmod
    if state:
        f["revision_state"] = state
    f["actions"] = actionmod.for_file(f)     # master's contract: send-to
    f["menu"] = actionmod.menu_for(f)         # the UI's whole ⋯ menu (stage 5)
    return f


def get_bom(conn: sqlite3.Connection, rev_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT b.*, c.number AS child_number, c.name AS child_name, c.kind AS child_kind
           FROM bom b JOIN items c ON c.id=b.child_item_id
           WHERE b.parent_rev_id=? ORDER BY b.find_number, c.number""", (rev_id,))]


# A pin that is not a release is displayed Go-style (docs/PLM_GIT_FIRST.md
# §5.1): `0.4.2-0.20260917103000-a1b2c3d4e5f6` means "after 0.4.1, not a
# release". It is a DISPLAY form, so it matches no `revisions.version` — but it
# CONTAINS the commit it stands for, which is the one fact needed to find the
# revision that was indexed from it. Without this a parent whose child is pinned
# at an unreleased commit had a BOM line that resolved to nothing: `rolled_bom`
# stopped walking there, and the whole subtree under that child fell out of the
# rollup, the build plan and the purchase list.
PSEUDO_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+-0\.\d{8,14}-([0-9a-f]{7,40})$")


def _by_pseudo_version(conn: sqlite3.Connection, item_id: int, version: str):
    m = PSEUDO_VERSION_RE.match(version or "")
    if not m:
        return None
    return conn.execute(
        "SELECT * FROM revisions WHERE item_id=? AND git_commit LIKE ? "
        "ORDER BY vmaj DESC, vmin DESC, vpat DESC LIMIT 1",
        (item_id, m.group(1) + "%")).fetchone()


def find_revision(conn: sqlite3.Connection, number: str | int,
                  version: str = "") -> dict | None:
    """A specific revision, or the item's latest when version is empty. The item
    may be addressed by id, current number or former number."""
    item = resolve_item(conn, number)
    if not item:
        return None
    if version:
        row = conn.execute("SELECT * FROM revisions WHERE item_id=? AND version=?",
                           (item["id"], version)).fetchone()
        if not row:
            row = _by_pseudo_version(conn, item["id"], version)
    else:
        row = conn.execute("SELECT * FROM revisions WHERE id=?",
                           (item["latest_rev_id"],)).fetchone()
    return dict(row) if row else None


def get_revision(conn: sqlite3.Connection, rev_id: int) -> dict | None:
    """One revision by its id, with the identity of the item it belongs to and
    its merged attributes — the id-addressed read (docs/PLM_DATA_MODEL.md)."""
    from . import attributes as attrs
    row = conn.execute(
        """SELECT r.*, i.number, i.name AS item_name, i.kind, i.project,
                  i.id AS item_id_ FROM revisions r JOIN items i ON i.id=r.item_id
           WHERE r.id=?""", (rev_id,)).fetchone()
    if not row:
        return None
    rev = dict(row)
    rev.pop("item_id_", None)
    rev["attributes"] = attrs.get(conn, rev_id)
    # The ROUTING beside the attributes, same as get_item: the
    # id-addressed read answers "what is this revision" and how it is MADE is
    # part of that answer. Empty for everything that is not fabricated.
    from . import fab as fabmod
    rev["operations"] = fabmod.get_routing(conn, rev_id)
    rev["files"] = [dict(f) for f in conn.execute(
        "SELECT * FROM files WHERE revision_id=?", (rev_id,))]
    rev["bom"] = get_bom(conn, rev_id)
    return rev


def where_used(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT DISTINCT p.id AS item_id, p.number, p.name, p.kind,
                  r.id AS revision_id, r.version AS parent_version,
                  b.qty, b.unit, b.child_version
           FROM bom b
           JOIN revisions r ON r.id=b.parent_rev_id
           JOIN items p ON p.id=r.item_id
           WHERE b.child_item_id=?
           ORDER BY p.number, r.vmaj DESC, r.vmin DESC, r.vpat DESC""", (item_id,))]


def activity(conn: sqlite3.Connection, limit: int = 30,
             offset: int = 0) -> list[dict]:
    """Most recent revisions across the registry — the dashboard feed.

    stl_count counts by MEASURED FORMAT (roles.media_sql), not by filename: the old `path LIKE '%.stl'` could not see a content-addressed
    object-store blob at all.
    """
    from . import roles
    pred, margs = roles.media_sql("f.media", "renderable")
    return [dict(r) for r in conn.execute(
        f"""SELECT r.id AS revision_id, r.version, r.state, r.date, r.summary,
                  r.ecn, r.created_at, i.id AS item_id, i.number, i.name,
                  i.kind, i.project,
                  (SELECT COUNT(*) FROM files f WHERE f.revision_id=r.id
                     AND {pred}) AS stl_count
           FROM revisions r JOIN items i ON i.id=r.item_id
           ORDER BY COALESCE(NULLIF(r.date,''), date(r.created_at,'unixepoch')) DESC,
                    r.created_at DESC
           LIMIT ? OFFSET ?""", (*margs, limit, offset))]


def activity_count(conn: sqlite3.Connection) -> int:
    """How many revisions the feed could show. One cheap COUNT so a pager can
    say "24 of 334" and know when it has reached the end — a pager that only
    discovers the end by fetching nothing is one that always shows a dead
    "next" button."""
    return conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]


def rolled_bom(conn: sqlite3.Connection, rev_id: int, depth: int = 12) -> list[dict]:
    """Flatten the BOM tree under one revision, multiplying quantities.

    Python recursion instead of a SQL CTE: child rows bind to (item, version)
    text, and resolving each child's revision row is clearer here. The corpus
    is hundreds of rows, not millions.

    NO KIND FILTER HERE, ON PURPOSE. A rollup's job is to report the structure
    that exists, and the rule that keeps non-structural items (coupons, assets —
    config.NON_STRUCTURAL_KINDS) out of rollups, purchasing plans and product
    cards is enforced where structure is WRITTEN (ops.NOT_IN_BOM, on both BOM
    write paths, plus ops.reclassify_item). Filtering here as well would hide a
    broken row instead of making it impossible, and would quietly disagree with
    GET /api/items/{n}/bom about what the BOM contains.
    """
    totals: dict[tuple[str, str], dict] = {}

    def walk(rid: int, mult: float, path: list[str], d: int) -> None:
        if d > depth:
            return
        for row in get_bom(conn, rid):
            if row.get("suppressed"):
                continue  # 150% BOM: suppressed occurrences never roll up
            qty = mult * (row["qty"] or 1)
            child_rev = find_revision(conn, row["child_number"], row["child_version"])
            key = (row["child_number"], row["child_version"])
            entry = totals.setdefault(key, {
                "item_id": row["child_item_id"],
                "revision_id": child_rev["id"] if child_rev else None,
                "number": row["child_number"], "name": row["child_name"],
                "kind": row["child_kind"], "version": row["child_version"],
                "qty": 0.0, "unit": row["unit"], "paths": []})
            entry["qty"] += qty
            entry["paths"].append(" > ".join(path + [row["child_number"]]))
            if child_rev:
                walk(child_rev["id"], qty, path + [row["child_number"]], d + 1)

    root = conn.execute(
        """SELECT i.number FROM revisions r JOIN items i ON i.id=r.item_id
           WHERE r.id=?""", (rev_id,)).fetchone()
    walk(rev_id, 1.0, [root["number"]] if root else [], 0)
    return sorted(totals.values(), key=lambda e: e["number"])
