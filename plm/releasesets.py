"""The RELEASE SET — one approval for an assembly's unreleased closure (stage 14).

Geoff, 2026-10-05: "…and then also approve a release." A product is released
from the top: the assembly AND every descendant it pins that is not RELEASED
yet. Stage 3c's release object promotes ONE tagged revision through a workflow
(checklist attested line by line, quorum, checks, child gate, evidence, a
`[status]` note). This module groups those:

    release_sets            one row per request: the ROOT (item, version, tag
                            commit), the state, who asked / decided, the
                            BLOCKERS found when the closure was computed, an
                            explicit resumable promotion log, and the SET
                            MANIFEST (a canonical JSON file listing every
                            member's evidence, whose sha256 is recorded).
    releases.release_set_id the member rows — one `releases` row per member,
    releases.set_order      each keeping its OWN workflow, checklist, checks,
                            evidence file and note. Order = bottom-up.

THE RULES, decided with the brief:

  * `request()` computes the UNRELEASED CLOSURE from git (`release.pin_closure`
    — gitlinks + status read from notes/tags): the root if it is not RELEASED,
    plus every design descendant that is not RELEASED. RELEASED descendants are
    listed, not promoted. The catalog never gates (§5.4).
  * A descendant pinned at a COMMIT that is not on a tag cannot be promoted —
    there is no immutable revision to promote. It is a BLOCKER, not a member,
    and the page names the change that must merge first (the proposal whose
    draft head IS that commit) and links it.
  * ATTEST BY COUPON where a passing coupon exists: the requester (usually the
    agent that knows the part was fitted) attests the workflow's fit line by
    REFERENCE to the passing `coupon_for` claim on the member's exact commit —
    `ops.sign_release(evidence=…)`, stage 10's checked door, nothing new.
  * BULK ATTEST: one human statement may legitimately cover several members
    ("I printed all three"); `attest()` signs that line on every member that
    asks for it and is still open — one ROW per member, each with the person's
    name, because "geoff attested this line on these three at 14:02" is the
    record and one boolean would not be.
  * ONE DECISION, pre-flighted: policy, blockers, every member's child gate
    (with the set's own members counted as released — they will be, bottom-up,
    before their parents), checks, blocking threads. Then promote bottom-up,
    each member through `ops._finish_release` (gate, evidence, note — stage
    3c's order, unchanged) and its own DB transaction, logged. A failure after
    pre-flight leaves the set `partial`, says what was promoted, and resumes.
  * Every release is a set: `POST /api/items/{n}/releases` (one revision)
    wraps its release in a set of one, and existing open releases are wrapped
    by `backfill()`. A member of a MULTI-member set is decided with its set.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

from . import config, db, webroutes

STATES = ("requested", "in_review", "approved", "rejected", "withdrawn")
OPEN = ("requested", "in_review")
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS release_sets (
  id INTEGER PRIMARY KEY,
  root_item_id INTEGER NOT NULL REFERENCES items(id),
  root_version TEXT NOT NULL DEFAULT '',
  root_tag_sha TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  notes_md TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'requested',  -- requested|in_review|approved|
                                            -- rejected|withdrawn
  blockers TEXT NOT NULL DEFAULT '[]',      -- pins at untagged commits
  closure TEXT NOT NULL DEFAULT '[]',       -- the whole closure, as computed
  origin TEXT NOT NULL DEFAULT '',          -- set|single|backfill
  requested_by TEXT NOT NULL DEFAULT '',
  requested_at REAL NOT NULL,
  decided_by TEXT NOT NULL DEFAULT '',
  decided_kind TEXT NOT NULL DEFAULT '',
  decided_at REAL,
  decision_note TEXT NOT NULL DEFAULT '',
  promote_state TEXT NOT NULL DEFAULT '',   -- '' | promoting | partial | done
  promote_log TEXT NOT NULL DEFAULT '[]',
  manifest_key TEXT NOT NULL DEFAULT '',
  manifest_sha256 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_release_sets_state ON release_sets(state);
"""

_ready = False
_lock = threading.Lock()


def ensure(conn: sqlite3.Connection) -> None:
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        from . import proposals
        for stmt in proposals._statements(_SCHEMA):
            conn.execute(stmt)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(releases)")}
        if "release_set_id" not in cols:
            conn.execute("ALTER TABLE releases ADD COLUMN release_set_id INTEGER")
        if "set_order" not in cols:
            conn.execute("ALTER TABLE releases ADD COLUMN set_order INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_releases_set "
                     "ON releases(release_set_id)")
        # ready BEFORE the backfill: it reads rows through functions that ask
        # ensure() again (wrap_single -> row), and the lock is not re-entrant
        _ready = True
        try:
            backfill(conn)
        except Exception as exc:                               # noqa: BLE001
            print(f"[releasesets] backfill skipped: {exc}", flush=True)


class SetError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def label(sid: int) -> str:
    return f"RS-{sid}"


def page(sid: int) -> str:
    """The release set's page — a PATH (`/release-sets/n`, stage 22)."""
    from . import webroutes
    return webroutes.release_set(sid)


def url(sid: int) -> str:
    return f"{config.PUBLIC_URL}{page(sid)}"


def parse_id(ref) -> int:
    s = str(ref or "").strip().upper().replace("RS-", "").replace("#", "")
    if not s.isdigit():
        raise SetError(f"not a release set: {ref!r}", 400)
    return int(s)


def row(conn, sid) -> dict:
    ensure(conn)
    sid = parse_id(sid)
    r = conn.execute(
        "SELECT s.*, i.number AS root_number, i.name AS root_name "
        "FROM release_sets s JOIN items i ON i.id=s.root_item_id WHERE s.id=?",
        (sid,)).fetchone()
    if not r:
        raise SetError(f"no release set {sid}", 404)
    return dict(r)


def member_rows(conn, sid: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT rel.*, i.number, i.name AS item_name, i.kind, i.project "
        "FROM releases rel JOIN items i ON i.id=rel.item_id "
        "WHERE rel.release_set_id=? ORDER BY rel.set_order, rel.id", (sid,))]


def set_of(conn, release_id: int) -> dict | None:
    ensure(conn)
    r = conn.execute("SELECT release_set_id FROM releases WHERE id=?",
                     (release_id,)).fetchone()
    return row(conn, r[0]) if r and r[0] else None


def _event(conn, type_: str, s: dict, *, actor: str, data: dict | None = None):
    db.add_event(conn, type_, item_id=s["root_item_id"], actor=actor,
                 data={"release_set": s["id"], **(data or {})})


# -- the closure ---------------------------------------------------------------

def closure(conn, number: str, tag_sha: str, root_state: dict) -> dict:
    """{members (bottom-up), released, blockers} for the revision at tag_sha.

    `members` are design revisions on a TAG that are not RELEASED, deepest
    first; `released` are listed for the page; `blockers` are pins at commits
    that are on no tag — with the change that must merge first."""
    from . import gitindex, release as rel_mod
    pins = rel_mod.pin_closure(number, tag_sha)
    kids: dict[tuple, list] = {}
    for p in pins:
        kids.setdefault((p["parent"], None), []).append(p)
    nodes: dict[tuple, dict] = {}
    blockers, released = [], []
    root = (number, tag_sha)
    nodes[root] = {"number": number, "commit": tag_sha, "parent": "",
                   "path": "", "version": root_state["version"],
                   "maturity": root_state["maturity"],
                   "state": root_state["state"],
                   "released": root_state["released"], "on_a_tag": True,
                   "depth": 0}
    # depth by walking pins in order (pin_closure is a pre-order walk)
    depth = {number: 0}
    for p in pins:
        if p["kind"] == "catalog":
            continue
        d = depth.get(p["parent"], 0) + 1
        depth[p["child"]] = max(depth.get(p["child"], 0), d)
        lab = gitindex.pin_label(p["child"], p["commit"])
        ent = {"number": p["child"], "commit": p["commit"], "parent": p["parent"],
               "path": p["path"], "version": p["version"],
               "maturity": p["maturity"], "state": p["state"],
               "released": p["released"], "on_a_tag": lab["released"],
               "depth": d}
        key = (p["child"], p["commit"])
        if not lab["released"]:
            blockers.append({**ent, **_change_for(conn, p["child"],
                                                  p["commit"])})
            continue
        if p["released"]:
            released.append(ent)
            continue
        old = nodes.get(key)
        if not old or old["depth"] < d:
            nodes[key] = ent
    members = [v for k, v in nodes.items()
               if not v["released"] and (k != root or not root_state["released"])]
    members.sort(key=lambda v: (-v["depth"], v["number"]))
    return {"members": members, "released": released, "blockers": blockers,
            "pins": pins}


def _change_for(conn, number: str, commit: str) -> dict:
    """Which change must merge before this commit can be released — the
    proposal whose head (or history) is that commit."""
    from . import changes
    item = db.resolve_item(conn, number)
    if not item:
        return {"change": None}
    r = conn.execute(
        "SELECT id, change_id, state, head_ref FROM proposals WHERE item_id=? "
        "AND head_sha=? ORDER BY id DESC LIMIT 1", (item["id"], commit)).fetchone()
    if not r:
        return {"change": None, "proposal": None,
                "fix": f"{number} @ {commit[:10]} is on no tag and in no "
                       "proposal — push it as a draft, get it approved, and "
                       "bump the parent's pin to the tag"}
    out = {"proposal": r["id"], "proposal_state": r["state"],
           "change": None}
    if r["change_id"]:
        c = changes.row(conn, r["change_id"])
        out.update(change=changes.label(c["id"]), change_state=c["state"],
                   change_url=changes.url(c["id"]),
                   change_page=changes.page(c["id"]))
    return out


# -- request ---------------------------------------------------------------------

def request(conn, number: str, *, version: str = "", actor: str,
            notes: str = "", auto_attest: bool = True) -> dict:
    """Compute the unreleased closure of `number vX` and propose ONE set."""
    from . import gitindex, ops, release as rel_mod
    ensure(conn)
    item = ops._item(conn, number)
    rev = db.find_revision(conn, item["id"], version)
    if not rev:
        raise SetError(f"no revision for {number} {version or '(latest)'}", 404)
    if rev["state"] in ("DRAFT", "IN_REVIEW"):
        raise SetError(
            f"{number} v{rev['version']} is {rev['state']} — a release set "
            "promotes revisions that are\nalready merged. Merge its pull request "
            "first; then request the release.", 409)
    if not db.get_user(conn, actor):
        raise SetError(f"unknown user: {actor}", 403)
    repo = ops.gitwrite_repo(number)
    tag, tag_sha = rel_mod.tag_of(repo, rev["version"])
    if not tag_sha:
        raise SetError(f"{number} v{rev['version']} has no release tag — "
                       "nothing immutable to promote", 409)
    st = gitindex.pin_status(number, tag_sha)
    cl = closure(conn, number, tag_sha,
                 {"version": rev["version"], "maturity": st["maturity"],
                  "state": st["state"], "released": st["is_released"]})
    if not cl["members"] and not cl["blockers"]:
        raise SetError(f"{number} v{rev['version']} and everything it pins are "
                       "already RELEASED — there is nothing to promote.", 409)
    open_ = conn.execute(
        "SELECT s.id FROM release_sets s WHERE s.root_item_id=? AND "
        "s.root_version=? AND s.state IN ('requested','in_review')",
        (item["id"], rev["version"])).fetchone()
    if open_:
        raise SetError(f"release set {open_[0]} is already open on {number} "
                       f"v{rev['version']} — decide or withdraw it first "
                       f"({url(open_[0])})", 409)
    cur = conn.execute(
        "INSERT INTO release_sets (root_item_id, root_version, root_tag_sha, "
        "project, title, state, blockers, closure, origin, requested_by, "
        "requested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (item["id"], rev["version"], tag_sha, item["project"] or "",
         f"Release {number} v{rev['version']} and its unreleased closure",
         "requested", json.dumps(cl["blockers"], sort_keys=True),
         json.dumps([{k: p[k] for k in ("parent", "path", "child", "commit",
                                        "version", "maturity", "released")}
                     for p in cl["pins"]], sort_keys=True),
         "set", actor, db.now()))
    sid = cur.lastrowid
    s = row(conn, sid)
    for i, m in enumerate(cl["members"]):
        _join(conn, s, m["number"], m["version"], i, actor=actor)
    if auto_attest:
        _attest_by_coupon(conn, sid, actor=actor)
    notes = notes or notes_draft(conn, sid)
    conn.execute("UPDATE release_sets SET notes_md=? WHERE id=?", (notes, sid))
    s = row(conn, sid)
    _event(conn, "release_set.requested", s, actor=actor,
           data={"members": [m["number"] for m in cl["members"]],
                 "blockers": len(cl["blockers"])})
    out = view(conn, sid)
    out["_events"] = [attention(conn, s, reason="requested", actor=actor)]
    return out


def _join(conn, s: dict, number: str, version: str, order: int, *,
          actor: str) -> dict:
    """One member: adopt an open release of that revision, or request one."""
    from . import ops
    item = db.resolve_item(conn, number)
    old = conn.execute(
        "SELECT * FROM releases WHERE item_id=? AND version=? AND state IN "
        "('requested','in_review') ORDER BY id DESC LIMIT 1",
        (item["id"], version)).fetchone()
    if old:
        if old["release_set_id"] and old["release_set_id"] != s["id"]:
            other = row(conn, old["release_set_id"])
            if other["state"] in OPEN and _size(conn, other["id"]) > 1:
                raise SetError(
                    f"{number} v{version} is already in release set "
                    f"{other['id']} ({url(other['id'])}) — decide or withdraw "
                    "that one first", 409)
            if other["state"] in OPEN:
                conn.execute("UPDATE release_sets SET state='withdrawn', "
                             "decision_note=? WHERE id=?",
                             (f"its one member joined release set {s['id']}",
                              other["id"]))
        rid = old["id"]
    else:
        res = ops.request_release(conn, number, version=version,
                                  to_maturity="released", actor=actor,
                                  _wrap=False)
        rid = res["release"]["id"]
    conn.execute("UPDATE releases SET release_set_id=?, set_order=? WHERE id=?",
                 (s["id"], order, rid))
    return {"release": rid}


def _size(conn, sid: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM releases WHERE release_set_id=?",
                        (sid,)).fetchone()[0]


def wrap_single(conn, release_id: int, *, actor: str, origin: str = "single") -> dict:
    """EVERY RELEASE IS A SET: a release requested on its own gets a set of
    one (its closure blockers are still computed, so the page can say "this
    has unreleased children — request the set")."""
    ensure(conn)
    rel = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
    if not rel or rel["release_set_id"]:
        return row(conn, rel["release_set_id"]) if rel and rel["release_set_id"] else {}
    item = conn.execute("SELECT * FROM items WHERE id=?", (rel["item_id"],)).fetchone()
    cur = conn.execute(
        "INSERT INTO release_sets (root_item_id, root_version, root_tag_sha, "
        "project, title, state, origin, requested_by, requested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (rel["item_id"], rel["version"], rel["tag_sha"], item["project"] or "",
         f"Release {item['number']} v{rel['version']}",
         rel["state"] if rel["state"] in OPEN else "requested", origin,
         rel["requested_by"] or actor, rel["requested_at"] or db.now()))
    conn.execute("UPDATE releases SET release_set_id=?, set_order=0 WHERE id=?",
                 (cur.lastrowid, release_id))
    return row(conn, cur.lastrowid)


def backfill(conn) -> int:
    n = 0
    for r in conn.execute(
            "SELECT r.id, r.item_id FROM releases r WHERE r.release_set_id IS "
            "NULL AND r.state IN ('requested','in_review') ORDER BY r.id").fetchall():
        item = conn.execute("SELECT * FROM items WHERE id=?",
                            (r["item_id"],)).fetchone()
        from . import ops
        if not item or not ops.git_writes(conn, item):
            continue
        wrap_single(conn, r["id"], actor="", origin="backfill")
        n += 1
    return n


def _attest_by_coupon(conn, sid: int, *, actor: str) -> list[dict]:
    """Where a passing coupon claim exists on a member's exact commit, the line
    it can attest IS attested — by reference (stage 10's checked door)."""
    from . import ops
    done = []
    for m in member_rows(conn, sid):
        if m["state"] not in OPEN:
            continue
        g = ops.get_release(conn, m["id"])
        for ln in g["progress"]["checklist"]:
            if ln["done"] or not ln.get("by_reference"):
                continue
            ref = ln["by_reference"][0]["ref"]
            try:
                ops.sign_release(conn, m["id"], kind="checklist", item=ln["line"],
                                 evidence=ref, actor=actor)
                done.append({"release": m["id"], "line": ln["line"],
                             "evidence": ref})
            except ops.OpsError as exc:
                done.append({"release": m["id"], "line": ln["line"],
                             "error": str(exc)[:200]})
    return done


# -- attest / decide ----------------------------------------------------------------

def attest(conn, sid, *, line: str, note: str, actor: str,
           releases: list[int] | None = None, identity_via: str = "",
           by_reference: bool = False) -> dict:
    """ONE statement, N rows: sign `line` on every open member that asks for it
    and has not got it (or on `releases`, a subset). `by_reference` instead
    attests each by its own passing claim, where one exists."""
    from . import ops
    s = row(conn, sid)
    if s["state"] not in OPEN:
        raise SetError(f"release set {s['id']} is {s['state']}", 409)
    if not by_reference and not (note or "").strip():
        raise SetError("say what you did — a bulk attestation is one statement "
                       "covering several parts, and it is the record", 400)
    signed, skipped = [], []
    for m in member_rows(conn, s["id"]):
        if releases and m["id"] not in releases:
            continue
        if m["state"] not in OPEN:
            continue
        g = ops.get_release(conn, m["id"])
        ln = next((x for x in g["progress"]["checklist"] if x["line"] == line),
                  None)
        if not ln:
            skipped.append({"release": m["id"], "item": m["number"],
                            "why": "its workflow does not ask for this line"})
            continue
        if ln["done"]:
            skipped.append({"release": m["id"], "item": m["number"],
                            "why": f"already attested by {ln['signed_by']}"})
            continue
        ev = ""
        if by_reference:
            if not ln.get("by_reference"):
                skipped.append({"release": m["id"], "item": m["number"],
                                "why": "no passing claim on its exact commit"})
                continue
            ev = ln["by_reference"][0]["ref"]
        ops.sign_release(conn, m["id"], kind="checklist", item=line,
                         note=note or "", actor=actor, identity_via=identity_via,
                         evidence=ev)
        signed.append({"release": m["id"], "item": m["number"],
                       **({"evidence": ev} if ev else {})})
    if s["state"] == "requested" and signed:
        conn.execute("UPDATE release_sets SET state='in_review' WHERE id=?",
                     (s["id"],))
    _event(conn, "release_set.attest", s, actor=actor,
           data={"line": line, "note": note, "signed": signed,
                 "by_reference": by_reference})
    out = view(conn, s["id"])
    out["attested"] = {"signed": signed, "skipped": skipped}
    return out


def preflight(conn, sid, *, actor: str, attest_rest: bool = True) -> dict:
    """Every member's gates, asked before the first promotion. The set's own
    members count as released for each other's child gate — they will be,
    bottom-up, before their parents are promoted."""
    from . import checks, ops, policy as pol, release as rel_mod, threads
    s = row(conn, sid)
    mem = member_rows(conn, s["id"])
    probs = []

    def add(m, why, fix=""):
        probs.append({"member": (f"{m['number']} v{m['version']} (release "
                                 f"{m['id']})" if m else label(s["id"])),
                      "release": m["id"] if m else None, "why": why, "fix": fix})

    resume = s["promote_state"] == "partial"
    if s["state"] not in OPEN and not resume:
        add(None, f"release set {s['id']} is {s['state']}")
    for b in json.loads(s["blockers"] or "[]"):
        add(None, f"{b['parent']} pins {b['number']} at {b['commit'][:10]} — a "
                  "commit that is on no tag, so there is no immutable revision "
                  "to promote",
            (f"{b['change']} must merge first ({b.get('change_state', '')}): "
             f"{b.get('change_url', '')}\n"
             f"then bump {b['parent']}'s pin to the child's tag, merge that, "
             "and request the release again") if b.get("change")
            else b.get("fix", ""))
    members = {(m["number"], m["tag_sha"]) for m in mem}
    for m in mem:
        if m["state"] == "approved":
            continue
        if m["state"] not in OPEN:
            add(m, f"it is {m['state']}")
            continue
        v = pol.may_approve(conn, m["project"] or "", "RELEASED", actor)
        if not v["ok"]:
            add(m, v["why"].split("\n")[0], "\n".join(v["why"].split("\n")[1:]))
        gate = [g for g in rel_mod.child_gate(ops.gitwrite_repo(m["number"]),
                                              m["tag_sha"]) if g.get("gates")]
        gate = [g for g in gate if (g["child"], g["commit"]) not in members]
        if gate:
            add(m, "you cannot release above your least-mature child: "
                   + ", ".join(f"{g['child']} {g['version'] or g['pin']} "
                               f"({g['why']})" for g in gate),
                "add it to the set (request the set from the top) or release "
                "it first")
        roll = checks.gate_for(conn, m["number"], m["tag_sha"],
                               target_maturity=m["to_maturity"] or "released")
        if not roll["gate"]["ok"]:
            # pins-released on a parent whose unreleased pins are all set
            # members is the same exception the change makes: they will be
            # released, bottom-up, before this member is promoted
            only_pins = (roll.get("required_failed") == ["pins-released"]
                         and not roll.get("required_missing"))
            if not only_pins:
                w = roll["gate"]["why"]
                add(m, w.split("\n")[0], "\n".join(w.split("\n")[1:]).strip())
        bl = threads.blocking_open(conn, threads.scope_for_release(conn, m))
        if bl:
            add(m, threads.refusal(bl, f"{m['number']} v{m['version']}")
                .split("\n")[0], "resolve them, or WITH A REASON")
        if not attest_rest:
            g = ops.get_release(conn, m["id"])
            miss = [ln["line"] for ln in g["progress"]["checklist"]
                    if not ln["done"]]
            if miss:
                add(m, "checklist not attested: " + "; ".join(miss))
    return {"ok": not probs, "problems": probs}


def refusal(s: dict, problems: list[dict]) -> str:
    lines = [f"release set {s['id']} ({s['root_number']} v{s['root_version']}) "
             f"was NOT approved — nothing was promoted. Pre-flight found "
             f"{len(problems)} problem{'s' if len(problems) != 1 else ''}:"]
    for p in problems:
        lines.append(f"  · {p['member']}: {p['why']}")
        for f in (p.get("fix") or "").splitlines():
            lines.append(f"      {f}")
    lines += ["", f"  {url(s['id'])}"]
    return "\n".join(lines)


def decide(conn, sid, *, decision: str, actor: str, note: str = "",
           identity_via: str = "", attest_rest: bool = True,
           _commit: bool = True) -> dict:
    from . import ops, policy as pol
    if decision not in ("approved", "rejected"):
        raise SetError("decision must be approved or rejected")
    s = row(conn, sid)
    user = db.get_user(conn, actor)
    if not user:
        raise SetError(f"unknown user: {actor}", 403)
    mem = member_rows(conn, s["id"])
    if decision == "rejected":
        if s["state"] not in OPEN:
            raise SetError(f"release set {s['id']} is {s['state']}", 409)
        for m in mem:
            if m["state"] in OPEN:
                ops.decide_release(conn, m["id"], decision="rejected",
                                   actor=actor, note=note or
                                   f"rejected with release set {s['id']}",
                                   identity_via=identity_via, _in_set=True)
        conn.execute("UPDATE release_sets SET state='rejected', decided_by=?, "
                     "decided_kind=?, decided_at=?, decision_note=? WHERE id=?",
                     (actor, user["kind"], db.now(), note, s["id"]))
        _event(conn, "release_set.rejected", s, actor=actor, data={"note": note})
        return view(conn, s["id"])
    pf = preflight(conn, s["id"], actor=actor, attest_rest=attest_rest)
    if not pf["ok"]:
        raise SetError(refusal(s, pf["problems"]), 409)
    log = json.loads(s["promote_log"] or "[]")
    done = {e["release"] for e in log if e.get("ok")}
    conn.execute("UPDATE release_sets SET promote_state='promoting' WHERE id=?",
                 (s["id"],))
    if _commit:
        conn.commit()
    for m in mem:
        if m["id"] in done or m["state"] == "approved":
            continue
        try:
            v = pol.may_approve(conn, m["project"] or "", "RELEASED", actor)
            wf_lines = ops.get_release(conn, m["id"])["progress"]["checklist"]
            for ln in wf_lines:
                if ln["done"] or not attest_rest:
                    continue
                conn.execute(
                    "INSERT INTO release_signoffs (release_id, kind, item, "
                    "signer, signer_kind, verdict, note, tag_sha, identity, "
                    "signed_at) VALUES (?,'checklist',?,?,?,'attest',?,?,?,?)",
                    (m["id"], ln["line"], actor, v["kind"],
                     note or f"attested with release set {s['id']}",
                     m["tag_sha"], identity_via or "trusted-network", db.now()))
            conn.execute(
                "INSERT INTO release_signoffs (release_id, kind, item, signer, "
                "signer_kind, verdict, note, tag_sha, identity, signed_at) "
                "VALUES (?,'approval','',?,?,'approve',?,?,?,?)",
                (m["id"], actor, v["kind"],
                 note or f"approved with release set {s['id']}", m["tag_sha"],
                 identity_via or "trusted-network", db.now()))
            out = ops._finish_release(conn, m["id"], actor=actor,
                                      note=note or f"release set {s['id']}",
                                      identity_via=identity_via)
        except Exception as exc:                               # noqa: BLE001
            if not _commit:
                raise
            conn.rollback()
            log.append({"release": m["id"], "item": m["number"],
                        "version": m["version"], "ok": False,
                        "error": str(exc)[:1200], "at": db.now()})
            conn.execute("UPDATE release_sets SET promote_state='partial', "
                         "promote_log=? WHERE id=?", (json.dumps(log), s["id"]))
            _event(conn, "release_set.partial", s, actor=actor,
                   data={"failed": m["id"], "error": str(exc)[:400]})
            conn.commit()
            ok = [e for e in log if e.get("ok")]
            raise SetError(
                f"release set {s['id']} is PARTIALLY PROMOTED — pre-flight "
                f"passed, then {m['number']} v{m['version']} failed:\n  {exc}\n\n"
                + ("Already promoted (their notes and evidence stand):\n"
                   + "".join(f"  · {e['item']} v{e['version']}\n" for e in ok)
                   if ok else "Nothing had been promoted yet.\n")
                + "\nFix the cause and decide again — promoted members are "
                  "skipped.\n  " + url(s["id"]), 409) from None
        g = out.get("git") or {}
        log.append({"release": m["id"], "item": m["number"],
                    "version": m["version"], "ok": True,
                    "evidence_sha256": g.get("evidence_sha256", ""),
                    "note_commit": g.get("note_commit", ""), "at": db.now()})
        conn.execute("UPDATE release_sets SET promote_log=? WHERE id=?",
                     (json.dumps(log), s["id"]))
        if _commit:
            conn.commit()
    conn.execute("UPDATE release_sets SET state='approved', decided_by=?, "
                 "decided_kind=?, decided_at=?, decision_note=?, "
                 "promote_state='done' WHERE id=?",
                 (actor, user["kind"], db.now(), note, s["id"]))
    man = freeze_manifest(conn, s["id"])
    conn.execute("UPDATE release_sets SET manifest_key=?, manifest_sha256=? "
                 "WHERE id=?", (man["key"], man["sha256"], s["id"]))
    _event(conn, "release_set.approved", row(conn, s["id"]), actor=actor,
           data={"manifest_sha256": man["sha256"],
                 "promoted": [e["item"] for e in log if e.get("ok")]})
    out = view(conn, s["id"])
    out["promoted"] = [e for e in log if e.get("ok")]
    out["_events"] = [("release.approved",
                       {"subject": {"type": "release_set", "id": s["id"],
                                    "number": label(s["id"])},
                        "url": url(s["id"]), "actor": actor,
                        "manifest_sha256": man["sha256"],
                        "items": [f"{e['item']} v{e['version']}" for e in log
                                  if e.get("ok")]})]
    return out


# -- the set manifest ------------------------------------------------------------

def manifest_document(conn, sid: int) -> dict:
    """Deterministic: every value is a row or a hash. Re-rendered from the rows
    it must give the stored bytes (`--verify`)."""
    s = row(conn, sid)
    mem = member_rows(conn, s["id"])
    return {
        "schema": SCHEMA_VERSION, "kind": "plm.release_set",
        "release_set": s["id"], "root": s["root_number"],
        "root_version": s["root_version"], "root_tag_sha": s["root_tag_sha"],
        "project": s["project"], "requested_by": s["requested_by"],
        "decided_by": s["decided_by"], "decided_kind": s["decided_kind"],
        "decided_at": round(float(s["decided_at"] or 0), 3),
        "decision_note": s["decision_note"],
        "members": [{"order": m["set_order"], "release": m["id"],
                     "number": m["number"], "version": m["version"],
                     "tag": m["tag"], "tag_sha": m["tag_sha"],
                     "from_maturity": m["from_maturity"],
                     "to_maturity": m["to_maturity"], "state": m["state"],
                     "evidence_sha256": m["evidence_sha256"],
                     "evidence_file": (m["evidence_key"] or "").rsplit("/", 1)[-1],
                     "note_commit": m["note_commit"]}
                    for m in mem],
    }


def _canon(doc: dict) -> bytes:
    return (json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False)
            + "\n").encode()


def manifest_path(sid: int):
    from . import release as rel_mod
    return rel_mod.EVIDENCE_DIR / "_sets" / f"release-set-{sid}.json"


def freeze_manifest(conn, sid: int) -> dict:
    raw = _canon(manifest_document(conn, sid))
    p = manifest_path(sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)
    return {"key": str(p), "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw)}


def verify(conn, s: dict) -> dict:
    """The stored set manifest vs its recorded hash (THE check), each member's
    evidence hash in it vs the release row, and a re-render (must be
    identical: the manifest is made of rows that do not change after it)."""
    out = {"release_set": s["id"], "recorded_sha256": s["manifest_sha256"]}
    p = manifest_path(s["id"]) if not s["manifest_key"] else __import__(
        "pathlib").Path(s["manifest_key"])
    if not p.is_file():
        return {**out, "stored": "MISSING", "ok": False}
    raw = p.read_bytes()
    dig = hashlib.sha256(raw).hexdigest()
    doc = json.loads(raw)
    rows = {m["id"]: m for m in member_rows(conn, s["id"])}
    mism = [m["release"] for m in doc.get("members") or []
            if (rows.get(m["release"]) or {}).get("evidence_sha256")
            != m["evidence_sha256"]]
    fresh = _canon(manifest_document(conn, s["id"]))
    return {**out, "stored_sha256": dig,
            "stored": "intact" if dig == s["manifest_sha256"] else "CHANGED",
            "members": len(doc.get("members") or []),
            "member_evidence": "agrees" if not mism else f"DISAGREES {mism}",
            "regenerated": "identical" if fresh == raw else "differs",
            "ok": dig == s["manifest_sha256"] and not mism}


# -- reads ----------------------------------------------------------------------

def notes_draft(conn, sid: int) -> str:
    """The set's release notes: the root's own drafted notes, then one line per
    member — generated, then edited (GitHub's shape), like every release."""
    s = row(conn, sid)
    mem = member_rows(conn, s["id"])
    out = [f"# {s['root_number']} v{s['root_version']} — release set "
           f"{s['id']}", "",
           f"Promotes {len(mem)} revision{'s' if len(mem) != 1 else ''} to "
           "RELEASED, bottom-up:", ""]
    for m in mem:
        first = (m["notes_md"] or "").strip().splitlines()
        body = next((ln for ln in first if ln.strip() and not ln.startswith("#")),
                    "")
        out.append(f"- **{m['number']} v{m['version']}** "
                   f"({m['from_maturity'] or '—'} → {m['to_maturity']})"
                   + (f" — {body[:160]}" if body else ""))
    bl = json.loads(s["blockers"] or "[]")
    if bl:
        out += ["", "Blocked until these merge (pins at commits, not tags):"]
        out += [f"- {b['parent']} → {b['number']} @ {b['commit'][:10]}"
                + (f" ({b['change']})" if b.get("change") else "") for b in bl]
    return "\n".join(out) + "\n"


def attention(conn, s: dict, *, reason: str, actor: str = "") -> tuple:
    mem = member_rows(conn, s["id"])
    bl = json.loads(s["blockers"] or "[]")
    head = (f"release set {s['id']} needs your approval — {s['root_number']} "
            f"v{s['root_version']} + {max(0, len(mem) - 1)} more"
            + (f" · BLOCKED by {len(bl)} unreleased pin(s)" if bl else ""))
    payload = {"subject": {"type": "release_set", "id": s["id"],
                           "number": label(s["id"])},
               "reason": reason, "root": s["root_number"],
               "version": s["root_version"],
               "items": [f"{m['number']} v{m['version']}" for m in mem],
               "blockers": len(bl), "actor": actor, "url": url(s["id"]),
               "headline": head}
    db.add_event(conn, "release.requested", item_id=s["root_item_id"],
                 actor=actor, data={"release_set": s["id"],
                                    **{k: v for k, v in payload.items()
                                       if k not in ("subject", "url")}})
    return ("release.requested", payload)


def view(conn, sid) -> dict:
    from . import ops, proposals, threads
    s = row(conn, sid)
    mem = member_rows(conn, s["id"])
    out_m = []
    for m in mem:
        g = ops.get_release(conn, m["id"])
        tc = threads.counts(conn, threads.scope_for_release(conn, m))
        out_m.append({
            "release": m["id"], "number": m["number"], "name": m["item_name"],
            "kind": m["kind"], "project": m["project"], "version": m["version"],
            "tag": m["tag"], "tag_sha": m["tag_sha"], "state": m["state"],
            "from": m["from_maturity"], "to": m["to_maturity"],
            "order": m["set_order"], "workflow": g["workflow"]["label"],
            "progress": g["progress"], "gate": g["gate"],
            "evidence_sha256": m["evidence_sha256"],
            "evidence_url": g["evidence_url"],
            "threads": {"open": sum(v["open"] for v in tc.values()),
                        "blocking": sum(v["blocking"] for v in tc.values())},
            "page": webroutes.release(m["number"], m["id"], m["tag"])})
    lines: dict[str, list] = {}
    for m in out_m:
        for ln in m["progress"]["checklist"]:
            lines.setdefault(ln["line"], []).append(
                {"release": m["release"], "number": m["number"],
                 "done": ln["done"], "signed_by": ln["signed_by"],
                 "evidence": ln.get("evidence", ""),
                 "by_reference": [x["ref"] for x in ln.get("by_reference") or []]})
    pf = preflight(conn, s["id"], actor="") if s["state"] in OPEN else \
        {"ok": False, "problems": []}
    pf["problems"] = [p for p in pf["problems"]
                      if "is not a user in plm" not in p["why"]]
    return {"release_set": {**s, "number": label(s["id"]), "url": url(s["id"]),
                            "page": page(s["id"]),
                            "blockers": json.loads(s["blockers"] or "[]"),
                            "closure": json.loads(s["closure"] or "[]"),
                            "promote_log": json.loads(s["promote_log"] or "[]")},
            "members": out_m, "lines": lines,
            "preflight": {"ok": pf["ok"] and not pf["problems"],
                          "problems": pf["problems"]},
            "manifest_url": (f"/api/release-sets/{s['id']}/manifest"
                             if s["manifest_sha256"] else None)}


def listing(conn, *, states: list[str] | None = None, limit: int = 100) -> list[dict]:
    ensure(conn)
    where, args = "", []
    if states:
        where = f"WHERE s.state IN ({','.join('?' * len(states))})"
        args = list(states)
    rows = [dict(r) for r in conn.execute(
        "SELECT s.*, i.number AS root_number, i.name AS root_name FROM "
        "release_sets s JOIN items i ON i.id=s.root_item_id " + where
        + " ORDER BY s.requested_at DESC LIMIT ?", (*args, limit))]
    for r in rows:
        mem = member_rows(conn, r["id"])
        r["number"], r["url"], r["page"] = label(r["id"]), url(r["id"]), page(r["id"])
        r["items"] = [{"item": m["number"], "version": m["version"],
                       "state": m["state"], "release": m["id"]} for m in mem]
        r["blockers"] = json.loads(r["blockers"] or "[]")
        r.pop("closure", None)
        r.pop("promote_log", None)
    return rows
