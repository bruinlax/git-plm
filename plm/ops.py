"""Revision operations: revise (rev up) and propagate (push a change up the tree).

Teamcenter semantics (see docs/PLM_DATA_MODEL.md): Revise seeds a new DRAFT
revision from the latest one — files carried forward (same immutable vault
paths), BOM copied. Propagate is the explicit "the child changed, rev the
precise parents" walk; imprecise BOM lines (child_version='') never need it
because they resolve to latest at read time.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from . import config, db, roles, vault


class OpsError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


# -- PLM_GIT_MODE: the one question every git fork in this file asks ----------
#
# `plm/gitmode.py` is the resolver; these two helpers are how ops asks it. A
# chokepoint (git_draft, frozen_note, catalog_revision, item_fields_commit,
# draft_fields_commit, new_item_repo, the renumber's repo move, _revise_base,
# _checkin_base, locks_advisory, the approval's merge, the submit's policy)
# returns its "not in git — take the old path" answer when the project is not in
# `write`, which is what makes `off` behave like `master` with no `if` anywhere
# else. `ops.git_origin` stays the per-ITEM truth; this is the per-PROJECT
# permission.

def git_writes(conn: sqlite3.Connection, item) -> bool:
    """May a write to this item/revision/project go to git?"""
    from . import gitmode
    if isinstance(item, str):           # a project name
        return gitmode.mode_for(item, conn) == "write"
    return gitmode.writes_git(conn, item)


def _stranded(conn: sqlite3.Connection, rev: dict) -> None:
    """THE RULE: a revision that LIVES in git is written through git, or not at
    all. Refuse a legacy (registry-only) write to one, by name.

    Going back from `write` to `read`/`off` is a flip of a setting, and it must
    not silently fork the record: a revision whose dataset is a commit is
    written by `gitindex` and nothing else, so a legacy DB write to it would
    put the registry ahead of the repo with no way back. That holds for a DRAFT
    (its branch) and for a FROZEN revision (its tag) alike.

    The ONE exception is not here, it is in `write` mode: post-release facts
    about a frozen git-origin revision (attributes, tags, suppression, a
    lifecycle state) land in `refs/notes/plm` (`frozen_note`, stage 3c). Outside
    `write` there is no note, so they are refused like everything else.

    A legacy revision — `git_commit` a vault sha, or no repo at all — is not
    stranded and takes the old path; with no repos (step 1's shape) nothing is
    git-origin and this never fires. That is the whole point of `off`.
    """
    from . import gitmode
    origin = git_origin(conn, rev)
    if not origin:
        return
    mode = gitmode.for_item(conn, rev)
    state = str(rev.get("state") or "")
    frozen = state not in ("", "DRAFT", "IN_REVIEW")
    raise OpsError(
        f"{origin['number']} v{rev.get('version', '?')} lives in git: its "
        f"source of truth is commit {origin['commit'][:10]} in its repository"
        + (f" (it is {state}, behind an immutable tag)" if frozen else "")
        + f", and this project is in `{mode}` mode, where writes take the "
        "legacy path.\n"
        "A registry-only write here would fork the registry from the "
        "repository, so it is refused.\n"
        "  - change it where it lives: put the project in `write` "
        "(python -m plm.gitmode --project <p> --mode write)"
        + (" — there a fact about a frozen revision is a note in "
           "refs/notes/plm" if frozen else "") + ", or\n"
        "  - revise it: a NEW draft made in this mode is a legacy revision.",
        409)


def registry_only_write(conn: sqlite3.Connection, rev: dict, *,
                        number: str = "") -> dict:
    """For the verbs that write a revision ROW directly (`state`, `material`,
    on both doors): `{}` = not git-origin, write the row as master does.

    Otherwise the revision lives in git: outside `write` this refuses
    (`_stranded`); in `write` it returns the origin and the caller takes the
    git path (a note on a frozen revision, `set_attributes` on a draft).
    """
    grev = dict(rev) | ({"number": number} if number else {})
    origin = git_origin(conn, grev)
    if not origin:
        return {}
    if not git_writes(conn, grev):
        _stranded(conn, grev)
    return origin


def set_state_git(conn: sqlite3.Connection, rev: dict, state: str, *,
                  number: str, actor: str = "", note: str = "") -> dict:
    """A lifecycle state on a revision that lives in git, in `write` mode.

    A FROZEN revision's state is a later decision about an immutable thing —
    exactly what the `[status]` note is for (gitindex reads it first, §2.7). A
    DRAFT or IN_REVIEW revision, or a move back INTO those states, is not a
    note: that lifecycle is submit / decide / withdraw, so it is refused rather
    than written behind the proposal's back.
    """
    grev = dict(rev) | {"number": number}
    cur = str(rev.get("state") or "")
    if cur in ("DRAFT", "IN_REVIEW") or state in ("DRAFT", "IN_REVIEW"):
        raise OpsError(
            f"{number} v{rev['version']} lives in git and is {cur}; moving it "
            f"to {state} is a proposal's lifecycle (submit / decide / "
            "withdraw), not a raw state flip.", 409)
    fz = frozen_note(conn, grev)
    if not fz:          # pragma: no cover — registry_only_write said git
        raise OpsError(f"{number} v{rev['version']} has no note surface", 409)

    def mutate(n: dict) -> None:
        n["status"] = {**(n.get("status") or {}), "state": state,
                       "maturity": state.lower()}

    return note_write(
        conn, fz, grev, mutate, actor=actor,
        message=(f"{number} v{rev['version']}: {cur} → {state}"
                 + (f"\n\n{note}" if note else "")
                 + "\n\nA lifecycle decision about a frozen revision: a "
                   "`[status]` note on the\nrelease commit (docs §2.7); the "
                   "tree and the tag are untouched.\n"))


def _with_git(d: dict, git) -> dict:
    """`d` plus `"git": git` only when there IS a git result — so a response or
    an event written in `off` mode carries exactly the keys master's did."""
    if git:
        d["git"] = git
    return d


# -- the EXPLODE gate (master, 2026-09-24; ported to every door in stage 9) -----
#
# Explodability is a FACT computed by the viewer's own resolver
# (`thumbs.explode_status`), and a RULE at submission (`thumbs.explode_gate`):
# an assembly of two or more MADE children does not submit until it explodes,
# unless a human-readable waiver says why. On `master` the rule lived in the two
# submit handlers (API and MCP) and the fact was bolted onto three API responses
# and one MCP response. Here both live in ops, so every door — the API, the MCP,
# a `refs/for/main` push (which submits through the API) and the policy
# auto-merge (which runs inside `submit_for_release`) — asks the same function,
# and every write verb that can change the answer carries it back.

#: The marker the waiver is recorded under, on the approval's `note`. It is
#: master's exact format (`[explode waiver] <why>`), so an approval written by
#: either line of code reads the same; `explode_waiver_of` is its one reader.
EXPLODE_WAIVER_MARK = "[explode waiver]"


def explode_block(conn: sqlite3.Connection, number: str,
                  version: str = "") -> dict | None:
    """"Does it still explode?" for a revision with a BOM, else None — the
    block master put on every check-in/files response, so the agent finds out
    at check-in, not when Geoff drags a slider that moves nothing."""
    from . import thumbs
    rev = db.find_revision(conn, number, version)
    if not rev or not db.get_bom(conn, rev["id"]):
        return None
    st = thumbs.explode_status(conn, rev["id"])
    return {k: st[k] for k in ("explodable", "bodies", "required", "reason")} | {
        "fix": [f"{k['number']}: {k['fix']}" for k in st["children"] if k["fix"]]}


def with_explode(conn: sqlite3.Connection, out: dict, number: str = "",
                 version: str = "") -> dict:
    """Attach `explode` to a write verb's response (in place, and returned)."""
    try:
        out["explode"] = explode_block(conn, number or out.get("number") or "",
                                       version or out.get("version") or "")
    except Exception:            # noqa: BLE001 — a diagnosis never fails a write
        out["explode"] = None
    return out



def _explodes(fn):
    """Decorate a write verb so its response carries `explode` — master's
    check-in block, on every verb that can change the answer (files, BOM lines,
    a revise that carries a BOM forward). `None` when the revision has no BOM,
    exactly as master answers."""
    import functools

    @functools.wraps(fn)
    def wrapper(conn, *a, **kw):
        out = fn(conn, *a, **kw)
        if isinstance(out, dict) and "version" in out:
            number = out.get("number") or ""
            if not number and out.get("item_id"):
                r = conn.execute("SELECT number FROM items WHERE id=?",
                                 (out["item_id"],)).fetchone()
                number = r["number"] if r else ""
            if number:
                with_explode(conn, out, number, out["version"])
        return out
    return wrapper


def explode_gate(conn: sqlite3.Connection, rev, waiver: str = "", *,
                 number: str = "") -> str:
    """Refuse (409) an assembly that does not explode, or return the note
    suffix that records the waiver. '' when there is nothing to record."""
    from . import thumbs
    refusal = thumbs.explode_gate(conn, rev["id"], waiver or "")
    if refusal:
        raise OpsError(f"{number} v{rev['version']}: {refusal}", 409)
    w = (waiver or "").strip()
    return f"\n\n{EXPLODE_WAIVER_MARK} {w}" if w else ""


def explode_gate_write(conn: sqlite3.Connection, rev, waiver: str = "", *,
                       number: str = "") -> tuple[str, dict]:
    """The explode gate in a `write` project (stage 15): refuse only what THIS
    change made worse (`thumbs.explode_regression`); a pre-existing failure is
    a warning in the response and on the checks board, no waiver needed.
    Returns (note suffix, explode note for the response). `off`/`read` keep
    `explode_gate` above, byte for byte master's."""
    from . import thumbs
    reg = thumbs.explode_regression(conn, rev["id"])
    w = (waiver or "").strip()
    suffix = f"\n\n{EXPLODE_WAIVER_MARK} {w}" if w else ""
    if reg["regression"] and not w:
        head = reg["head"]
        raise OpsError(
            thumbs.explode_sentence(number, rev["version"], reg)
            + ". Fix: " + "; ".join(f"{k['number']}: {k['fix']}"
                                    for k in head["children"] if k["fix"])[:900]
            + ". Or pass explode_waiver='<why>' to submit anyway.", 409)
    if reg["pre_existing"]:
        return suffix, {"explodes": False, "pre_existing": True,
                        "base_version": reg["base_version"],
                        "warning": thumbs.explode_sentence(
                            number, rev["version"], reg)}
    return suffix, {}


def explode_waiver_of(note: str) -> str:
    """The waiver recorded on an approval's note, or ''."""
    note = note or ""
    i = note.rfind(EXPLODE_WAIVER_MARK)
    return note[i + len(EXPLODE_WAIVER_MARK):].strip() if i >= 0 else ""


def _item(conn: sqlite3.Connection, number: str | int) -> sqlite3.Row:
    """The item behind a reference: an id, the current number, or a number it
    used to have (db.resolve_item — renumbering never breaks a caller)."""
    row = db.resolve_item(conn, number)
    if not row:
        raise OpsError(f"unknown item: {number}", 404)
    return row


# -- identity: plm owns the numbers -------------------------------------------
# A part number is an IDENTIFIER, not a classification. The only meaning it
# carries is the one plm itself assigns: <PREFIX>-<CLASS>-<SEQ>, where CLASS is a
# key of config.KIND_BY_CLASS and therefore says exactly what `kind` already
# says. Everything else a human might want to encode — owned vs to-buy, electrical
# vs mechanical, now vs future — is DATA: kind, project, name, attributes,
# relations, context. Encode it in the number and you have built a taxonomy that
# cannot be queried, cannot be corrected without a migration, and goes stale the
# first time a "future" part is bought.
#
# So: every explicit number is validated here, on the server, once. Callers that
# want a number get one from next_number(); callers that invent one get a 400
# with the law in it.

NUMBER_RE = re.compile(r"^([A-Z][A-Z0-9]{0,7})-([A-Z]{2,6})-(\d{3,6})$")

IDENTITY_LAW = (
    "plm owns identity — omit `number` and let plm assign it (pass `prefix`, "
    "e.g. GL, and the right `kind`), or use an approved class")


def _class_menu() -> str:
    return ", ".join(f"{cls}={kind}" for cls, kind in sorted(config.KIND_BY_CLASS.items()))


def validate_number(number: str, kind: str = "") -> str:
    """An explicit item number, or a 400 that teaches the rule.

    Enforces the shape, that CLASS is in the approved registry, and that the
    class AGREES with the kind being registered — a number that says PUR on an
    assembly is a second, contradicting source of truth for the same fact."""
    number = (number or "").strip().upper()
    m = NUMBER_RE.match(number)
    if not m:
        raise OpsError(
            f"{number!r} is not a part number. Expected <PREFIX>-<CLASS>-<SEQ> "
            f"(e.g. GL-PUR-0004): prefix letters/digits, CLASS one of "
            f"[{_class_menu()}], SEQ 3-6 digits. {IDENTITY_LAW}.")
    cls = m.group(2)
    if cls not in config.KIND_BY_CLASS:
        raise OpsError(
            f"{number}: {cls!r} is not an approved number class. Approved "
            f"classes are [{_class_menu()}] — a class is the KIND of item, "
            f"nothing else. Classification like OWNED/FUTURE/ELEC/MECH does not "
            f"belong in identity: put it in kind, project, name, attributes or a "
            f"relation, where it can be queried and corrected. {IDENTITY_LAW}.")
    if kind and config.KIND_BY_CLASS[cls] != kind:
        raise OpsError(
            f"{number} is class {cls}, which means kind "
            f"'{config.KIND_BY_CLASS[cls]}', but this item is a '{kind}'. The "
            f"number and the kind must agree — use "
            f"{m.group(1)}-{config.CLASS_BY_KIND.get(kind, '?')}-{m.group(3)}, "
            f"or {IDENTITY_LAW}.")
    return number


# Coupons are verification artifacts, not occurrences (docs/PLM_RELATIONS.md).
# The Teamcenter posture: V&V trace links, never EBOM structure — distinct from
# SUPPRESSION, which is for real product parts that are conditionally absent.
# Every BOM write goes through one of the two guards below, so there is no path
# by which a coupon reaches a BOM (and therefore none by which it reaches a
# rollup, a build plan or a product card).
COUPON_IN_BOM = ("coupons relate, they don't assemble — link with coupon_for "
                 "instead")

# Assets are the SAME posture for the opposite reason: not a thing you
# verify with, a thing you attach TO — a camper, a truck bed, a garage wall.
# Barred from BOMs because a BOM line means "this is part of what I build", and
# an asset is neither built nor bought; letting one in would put a camper in a
# purchasing plan.
ASSET_IN_BOM = ("an asset is what this mounts to — link it with mounts_to "
                "(or installed_on / integration_envelope_for), don't assemble "
                "it. You neither build nor buy it, so it is not an occurrence "
                "and must never reach a rollup or a build plan")

# kind -> why it cannot be a BOM child. THE dict both write paths consult;
# config.NON_STRUCTURAL_KINDS is the same list without the prose.
NOT_IN_BOM = {"coupon": COUPON_IN_BOM, "asset": ASSET_IN_BOM}


def _a(kind: str) -> str:
    """"a coupon" / "an asset" — a refusal that misreads is a refusal people
    quote back at you."""
    return f"{'an' if kind[:1].lower() in 'aeiou' else 'a'} {kind}"


# -- checkouts: RETIRED, kept as an advisory no-op (§1) -----------------------
#
# Geoff, 2026-09-17, on check-out/check-in: *"it's kinda an old concept and not
# relevant anymore, right?"* Measured on production: **384 check-out events in
# August, 2 in September.** The concept is dead, and branches are what killed
# it — two agents working on one part now get two `draft/*` branches and a merge,
# which is strictly better than one of them waiting.
#
# So the endpoints and the MCP tools STAY, for compatibility with every agent
# and script that still calls them, and become **advisory no-ops on an item that
# lives in a repo**: they succeed, they record an event, and they block nothing.
# The response says so in words rather than pretending.
#
# WHAT WAS DELIBERATELY NOT DONE: a lock flag in `plm.toml`. A lock that needs a
# commit to set and another to clear re-creates the problem inside git, and adds
# a write to the critical path of every edit. Opaque binary CAD will get real
# LFS locks in Phase 4, which is the case that genuinely needs them.
#
# The old machinery below still runs for a vault-era item with no repo — the
# same "convertible one item at a time" rule the rest of stage 3b/3c follows.

def locks_advisory(conn: sqlite3.Connection, item) -> bool:
    """Is this item's concurrency handled by git rather than by a lock?

    True once the item has a repository (branches are the mechanism) or lives in
    `catalog.git` (a per-file compare-and-swap is). False only for a vault-era
    item plm never split, where the old lock is still the only thing there is.
    """
    from . import gitwrite, manifest as mf
    if not git_writes(conn, item):
        return False     # `off`/`read`: the lock is the mechanism, as on master
    if item["kind"] in mf.CATALOG_KINDS:
        from . import catalog as cat
        return cat.repo_path() is not None
    return (gitwrite.REPOS / f"{item['number']}.git" / "HEAD").exists()


ADVISORY_NOTE = ("branches replaced locks — work on a draft/* branch "
                 "(docs/PLM_GIT_FIRST.md §5.2). This checkout is advisory: it "
                 "is recorded and it blocks nothing.")


def claim_lock(conn: sqlite3.Connection, number: str | int, actor: str, *,
               note: str = "") -> dict:
    """`POST /api/items/{n}/checkout` — ONE code path for the API and the MCP.

    On a git-first item nothing is inserted into `checkouts`: a row that blocks
    nothing would still show up in `GET /api/checkouts`, which is the *stranded
    lock* recovery view, and filling a recovery view with locks that were never
    locks is how a useful list stops being read.
    """
    item = _item(conn, number)
    if locks_advisory(conn, item):
        db.add_event(conn, "checkout.advisory", item_id=item["id"], actor=actor,
                     data={"note": note, "advisory": True})
        return {"ok": True, "number": item["number"], "actor": actor,
                "advisory": True, "note": ADVISORY_NOTE, "locked": False}
    co = db.active_checkout(conn, item["id"])
    if co and co["actor"] != actor:
        raise OpsError(
            f"{item['number']} is already checked out by {co['actor']}", 409)
    if not co:
        conn.execute(
            "INSERT INTO checkouts (item_id, actor, note, at) VALUES (?,?,?,?)",
            (item["id"], actor, note, db.now()))
        db.add_event(conn, "checkout", item_id=item["id"], actor=actor,
                     data={"note": note})
    return {"ok": True, "number": item["number"], "actor": actor,
            "advisory": False, "locked": True}


# The advisory checkout was Teamcenter's: it protects work in progress, and the
# holder releases it by checking in or revising. That contract has a hole in it —
# an agent that dies mid-work releases NOTHING, and `checkouts.released_at IS
# NULL` then strands the item forever. Before this, the only signal was a 409 on
# every subsequent write and the only cure was a hand-written UPDATE.
#
# Recovery, and the asymmetry is deliberate:
#
#   your own lock            release it whenever you like.
#   somebody else's, STALE   any actor may take it over
#   (config.CHECKOUT_       automatically. This is not stealing; the holder is
#    STALE_SECONDS)          gone and the age proves it. Evented as
#                            checkout.takeover, with the age and the actor
#                            displaced, so the trail says who broke what.
#   somebody else's, FRESH   HUMANS ONLY, and only through an explicit
#                            force-release with a reason. A bot deciding that
#                            another actor's live work no longer matters is
#                            exactly the judgement the estate keeps for people.

def _human(conn: sqlite3.Connection, actor: str) -> bool:
    user = db.get_user(conn, actor)
    return bool(user) and user["kind"] == "human"


def _age(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 60:.0f} min"


def claim_checkout(conn: sqlite3.Connection, item: sqlite3.Row, actor: str, *,
                   op: str) -> dict | None:
    """May `actor` write to this item? Returns the takeover record, or refuses.

    Called by every op that used to raise a bare 'checked out by X' — so the
    stale-lock recovery is a property of WRITING, not of one endpoint. Returns
    None when there was nothing in the way."""
    if locks_advisory(conn, item):
        return None      # §1: branches replaced locks; nothing here may block
    co = db.active_checkout(conn, item["id"])
    if not co or not actor or not co["actor"] or co["actor"] == actor:
        return None
    if not co["stale"]:
        raise OpsError(
            f"{item['number']} is checked out by {co['actor']} "
            f"(held {_age(co['age_s'])}). Wait for them to check in, or — if you "
            f"know that work is abandoned — a HUMAN can break the lock: POST "
            f"/api/items/{item['number']}/checkout/release with force=true and a "
            f"reason. plm takes an abandoned lock over by itself once it is "
            f"{_age(config.CHECKOUT_STALE_SECONDS)} old.", 409)
    # Stale: take it over, loudly.
    db.release_checkout(conn, item["id"])
    note = (f"{actor} took over {item['number']}'s checkout from {co['actor']} "
            f"to {op}: the lock had been held {_age(co['age_s'])}, past the "
            f"{_age(config.CHECKOUT_STALE_SECONDS)} staleness threshold, which "
            "means the agent holding it is gone. Nothing about the previous "
            "holder's work is lost — a checkout is a lock, not content.")
    db.add_event(conn, "checkout.takeover", item_id=item["id"], actor=actor,
                 data={"from": co["actor"], "age_s": round(co["age_s"], 1),
                       "op": op, "automatic": True, "note": note})
    return {"from": co["actor"], "age_s": round(co["age_s"], 1), "note": note}


def force_release_checkout(conn: sqlite3.Connection, number: str | int, *,
                           actor: str = "", reason: str = "",
                           force: bool = False) -> dict:
    """Release an item's checkout, including somebody else's.

    Own lock, or a stale one: anybody. Another actor's LIVE lock: force=True,
    a reason, and a human — see the asymmetry above."""
    item = _item(conn, number)
    if locks_advisory(conn, item):
        # §1: nothing was ever held, so nothing is broken by releasing it. It
        # succeeds rather than 404-ing, because every existing agent script ends
        # its work this way and a compatibility shim that errors is not one.
        db.add_event(conn, "checkout.advisory", item_id=item["id"], actor=actor,
                     data={"released": True, "advisory": True,
                           "reason": reason})
        return {"number": item["number"], "released": True, "advisory": True,
                "was": "", "note": ADVISORY_NOTE}
    co = db.active_checkout(conn, item["id"])
    if not co:
        return {"number": item["number"], "released": False,
                "note": f"{item['number']} was not checked out."}
    mine = (not co["actor"]) or co["actor"] == actor
    if not mine and not co["stale"]:
        if not force:
            raise OpsError(
                f"{item['number']} is checked out by {co['actor']}, held "
                f"{_age(co['age_s'])} — that is live work, not a stranded lock. "
                "Pass force=true with a reason to break it anyway.", 409)
        if not _human(conn, actor):
            raise OpsError(
                f"breaking {co['actor']}'s live lock on {item['number']} is a "
                f"human's call, and {actor or '(nobody)'} is not a human "
                "identity in plm. A stale lock "
                f"(>{_age(config.CHECKOUT_STALE_SECONDS)}) needs no force and "
                "no human — this one is not stale.", 403)
        if not (reason or "").strip():
            raise OpsError(
                "force-releasing another actor's live lock requires a reason — "
                "it goes in the event, and 'who broke this and why' is the "
                "question somebody will ask.", 400)
    db.release_checkout(conn, item["id"])
    why = ("your own lock" if mine else
           f"a stale lock (held {_age(co['age_s'])})" if co["stale"] else
           f"{co['actor']}'s live lock, forced")
    note = (f"{actor or 'someone'} released {item['number']}'s checkout — {why}."
            + (f" Reason: {reason}" if reason else ""))
    db.add_event(conn, "checkout.release", item_id=item["id"], actor=actor,
                 data={"from": co["actor"], "age_s": round(co["age_s"], 1),
                       "stale": co["stale"], "forced": bool(force and not mine),
                       "reason": reason, "note": note})
    return {"number": item["number"], "released": True, "was": co["actor"],
            "age_s": round(co["age_s"], 1), "stale": co["stale"],
            "forced": bool(force and not mine), "note": note}


def _child_kind(conn: sqlite3.Connection, child_number: str) -> str:
    """What a BOM child IS — from the item row when it exists, otherwise from
    its number's class token, because checkin AUTO-REGISTERS unknown children
    and a refusal must not depend on the item existing yet."""
    row = db.item_by_number(conn, child_number)
    if row:
        return row["kind"]
    return config.KIND_BY_CLASS.get(
        child_number.split("-")[1] if "-" in child_number else "", "")


def _refuse_nonstructural_child(conn: sqlite3.Connection,
                                child_number: str) -> None:
    """Checked BEFORE any write, so a refused BOM line cannot leave half a
    check-in behind. A child that does not exist yet is judged by its number
    class, so KML-CPN-0007 / CCR-AST-0001 is refused even on its first
    mention."""
    kind = _child_kind(conn, child_number)
    if kind in NOT_IN_BOM:
        raise OpsError(f"{child_number} is {_a(kind)}: {NOT_IN_BOM[kind]}", 400)


def _vet_bom_child(conn: sqlite3.Connection, child_number: str) -> None:
    """Every pre-write check for one BOM line's child, in one place: identity and
    the non-structural bar (coupons, assets). Check-in AUTO-REGISTERS an unknown
    child, which makes a BOM line the other door into the number space — so it is
    validated by exactly the same law create_item obeys, before anything is
    written."""
    if not db.item_by_number(conn, child_number):
        validate_number(child_number)
    _refuse_nonstructural_child(conn, child_number)


def _bom_child_id(conn: sqlite3.Connection, child_number: str,
                  project: str) -> int:
    """The child item's id — resolving a former number to the item that owns it
    now, and auto-registering a genuinely new (already vetted) number."""
    row = db.item_by_number(conn, child_number)
    if row:
        return row["id"]
    return db.upsert_item(conn, validate_number(child_number), project=project)


# -- file inputs: inline bytes, a staged handle, or a fetched link -------------
# Both write paths (checkin, upload_files) take the same entry shape:
#
#   {"name", "role", "data": b"..."}                        inline (small text)
#   {"name", "role", "staged": {token, handle, sha256, size, path}}  a verified
#                                                   handle, in plm's inbox
#   {"name", "role", "local": {path, sha256, size}}  bytes fetched from a url
#                                                   or a raw PUT
#
# Both by-reference forms are a verified FILE ON LOCAL DISK (`path`), so a
# 300 MB STL never passes through plm's memory (docs/PLM_UPLOADS.md) and both
# are written by the one storage.put_file. By the time an entry gets here it
# has already been verified — the Sink that wrote it measured sha256 and size
# (a PUT to the inbox, a raw PUT, a url fetch), and staging.claim() held the
# caller's numbers to them — so this layer only has to ROUTE it, and the store
# adopts the file by those numbers without reading it again. Transport does
# not influence destination; that is storage.py's call.
# They differ only in who deletes the file afterwards (_consume_inputs).

def _arrived(f: dict) -> dict | None:
    """The on-disk arrival behind a by-reference entry (`staged` or `local`,
    both {path, sha256, size}), or None for inline bytes."""
    return f.get("staged") or f.get("local")


def _entry_size(f: dict) -> int:
    src = _arrived(f)
    return src["size"] if src else len(f["data"])


def classify_inputs(files: list[dict]) -> dict[str, dict]:
    """Decide what every incoming file IS, ONCE, before anything is stored. Returns {basename: classification} and stamps the resolved role
    back onto each entry.

    Run at the FRONT of both write paths, for two reasons:

    - the bytes are still reachable here. An inline entry has `data`; a
      `staged` or `local` entry has a file on disk we can read a header from.
      By the time storage.put has run, the record is just a path and a hash,
      and the one chance to MEASURE the format instead of guessing at it is
      gone.
    - stamping the resolved role back on the entry means storage.choose_backend
      sees it, so an unstated `.scad` that infers to `source` gets pinned to the
      vault the way a declared one does. That was a real hole: pinning consulted
      a role the caller had not necessarily set.
    """
    out: dict[str, dict] = {}
    for f in files or []:
        name = f.get("name") or ""
        data = f.get("data")
        if data is None and _arrived(f):
            try:
                with open(_arrived(f)["path"], "rb") as fh:
                    data = fh.read(8192)
            except OSError:
                data = None
        size = _entry_size(f)
        c = roles.classify(name, f.get("role") or "", data, size,
                           f.get("media") or "")
        f["role"] = c["role"]          # so choose_backend sees the real role
        out[Path(name).name] = c
    return out


def _split_destinations(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """(vault, offload) per storage.choose_backend — decided from the SIZE, so a
    staged file is routed without downloading it. A handle for a small .md still
    lands in the git vault; a big STL still offloads."""
    from . import storage as stg
    vault_files, offload = [], []
    for f in files:
        dest = stg.choose_backend(f["name"], f.get("role", ""), _entry_size(f))
        (vault_files if dest == "vault" else offload).append(f)
    return vault_files, offload


def _fill_vault_bytes(files: list[dict]) -> None:
    """A vault write needs real bytes and a git commit, so a file that arrived
    by reference and is bound for the vault is materialized here. That is fine:
    the vault is pinned to text, sources and viz — the classes that are small on
    purpose."""
    for f in files:
        if f.get("data") is None and _arrived(f):
            f["data"] = Path(_arrived(f)["path"]).read_bytes()


def _put_row(conn: sqlite3.Connection, rev_id: int, fr: dict,
             cls: dict | None = None) -> dict:
    """Insert one files row and stamp the record with its id and blob URL, so
    every write response can hand back a retrievable link (requirement 2).

    THE CLASSIFICATION SEAM. Every file that enters plm through
    checkin or upload_files passes through here, so this is where what-it-is is
    decided and where its PROVENANCE is recorded.

    Two things this used to do wrong:

    - it never wrote `media` AT ALL, which is why 613 of 837 prod rows had no
      format recorded and why every consumer fell back to sniffing the filename;
    - `fr["role"] or "artifact"` silently defaulted an unstated purpose into the
      junk drawer, indistinguishable from a role somebody had chosen.

    Now an unregistered role is REFUSED (roles.require — a purpose no surface
    acts on is a typo), and an unstated one is inferred but stamped
    `role_source='extension'` so it renders as the guess it is.
    """
    c = (cls or {}).get(_base(fr)) or roles.classify(
        fr.get("name") or _base(fr), fr.get("role") or "", fr.get("data"),
        int(fr.get("size") or 0), fr.get("media") or "")
    if c["warning"]:
        fr["warning"] = c["warning"]
    cur = conn.execute(
        """INSERT INTO files (revision_id,role,path,sha256,size,backend,key,
                              media,role_source,media_source)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (rev_id, c["role"], fr["path"], fr["sha256"],
         fr["size"], fr.get("backend", ""), fr.get("key", ""),
         c["media"], c["role_source"], c["media_source"]))
    fr["id"] = cur.lastrowid
    fr["url"] = f"/api/blob?id={cur.lastrowid}"
    fr["role"], fr["media"] = c["role"], c["media"]
    fr["role_source"], fr["media_source"] = c["role_source"], c["media_source"]
    return fr


def _consume_inputs(conn: sqlite3.Connection, files: list[dict],
                    actor: str = "") -> None:
    """The revision owns its own immutable copy now, so release the arrivals.

    Called only after the write succeeded — on failure a stage survives to its
    TTL and can be retried. Two very different things happen here, and the
    difference is OWNERSHIP:

    - a `staged` upload was minted by plm, in plm's own inbox, for this
      check-in: plm deletes it (and its ticket).
    - a `local` copy was fetched from a URL: plm deletes its own temp copy and
      NOTHING ELSE. The object behind the link belongs to whoever staged it
      (typically an agent's filedrop MCP inbox object, possibly handed to more
      than one service); deleting other people's objects is not plm's
      business. The file bus's TTL / delete-on-download settles it.
    """
    from . import staging
    for f in files or []:
        if f.get("staged"):
            staging.consume(conn, f["staged"]["token"], actor=actor)
        elif f.get("local"):
            staging.release_local(f["local"])


# -- supersession: version-stamped filenames defeat basename replacement -------
# revise() carries every file ROW forward, and that is correct dataset semantics:
# a new DRAFT starts life as a copy of what it was seeded from. The clutter
# comes from the OTHER half of the contract never firing. upload_files
# retires a prior row by BASENAME, and this vault's exported filenames carry the
# version they came from — KML-PRT-1012_v0.3.0_bottom-insert-thin-carrier.stl —
# so a new generation's STL never has the same name as the STL it replaces.
# Nothing is individually wrong; together, thirteen rows spanning three
# generations on one revision are a manifest nobody can read.
#
# The rule here is deliberately narrow. An inherited row is retired only when
# this revision gained a file OF ITS OWN that plainly stands in for it:
#
#   same role AND same extension          the STL / PNG / source case — the
#                                         names are version-stamped, so
#                                         role+extension is the only stable
#                                         identity they have.
#   same filename inside this revision's  the MANIFEST.sha256 / VERSION.md case
#   own version folder                    — names that never change, written
#                                         into the folder by vault.write_checkin
#                                         without a row of their own.
#
# and only rows whose bytes provably live in an OLDER version folder of the same
# item are candidates. Three guarantees follow, and they are the point:
#
#   - a row whose generation cannot be read off its path/key is NEVER touched;
#   - same-generation siblings are NEVER touched (two STLs on one revision are
#     variants — an exploded view beside an assembled one — and both are real);
#   - nothing is deleted from disk or from the object store. Git keeps every
#     vault copy, offloaded objects keep their immutable key. A files row is a POINTER, and
#     this retires pointers.
#
# ops.remove_file is the manual counterpart for what a rule cannot judge.

def _row_get(row, field: str, default: str = "") -> object:
    """One field of a files row that may be a sqlite3.Row (offloaded columns
    always present) or a freshly built dict (vault records carry no `key`)."""
    try:
        val = row[field]
    except (IndexError, KeyError, TypeError):
        return default
    return default if val is None else val


def _base(row) -> str:
    return str(_row_get(row, "path")).rsplit("/", 1)[-1]


def _fkey(row) -> tuple[str, str]:
    """The supersession key: (role, extension)."""
    return (str(_row_get(row, "role") or "artifact"),
            Path(_base(row)).suffix.lower())


def file_generation(row, folder: str = "", number: str = "") -> str:
    """Which version folder a file row's bytes belong to, or '' when the
    path/key does not PROVE it.

    Two shapes, because storage is an independent axis (plm/storage.py): a vault
    row's `path` is '<folder>/versions/v<ver>/…', an offloaded row's key (and its
    display path) is '<number>/v<ver>/<name>'. Anything else — an imported
    hand-placed file, a legacy row — returns '', which makes it untouchable by
    every rule that keys on generation."""
    path = str(_row_get(row, "path"))
    if folder and path.startswith(f"{folder}/versions/v"):
        m = re.match(re.escape(folder) + r"/versions/v(\d+\.\d+(?:\.\d+)?)/", path)
        if m:
            return m.group(1)
    if number:
        pat = re.escape(number) + r"/v(\d+\.\d+(?:\.\d+)?)/"
        for hay in (path, str(_row_get(row, "key"))):
            m = re.search(pat, hay)
            if m:
                return m.group(1)
    return ""


def _own_folder_names(folder: str, version: str) -> set[str]:
    """Filenames this revision's own version folder physically holds.

    vault.write_checkin refreshes MANIFEST.sha256 in the folder without
    registering a row for it, so the index alone cannot see that a revision has
    its own manifest. The folder can."""
    if not folder:
        return set()
    try:
        vdir = vault.safe_path(f"{folder}/versions/v{version}")
    except vault.VaultError:
        return set()
    if not vdir.is_dir():
        return set()
    return {p.name.lower() for p in vdir.rglob("*") if p.is_file()}


def supersede_inherited(conn: sqlite3.Connection, *, item_id: int, number: str,
                        folder: str, rev_id: int, version: str,
                        actor: str = "", dry_run: bool = False) -> list[dict]:
    """Retire the carried-forward rows this revision's own files replace.

    Returns one record per retirement — what went, where it came from, and what
    stood in for it — which is also what the file.superseded event carries."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM files WHERE revision_id=?", (rev_id,))]
    target = db.parse_version(version)
    own, inherited = [], []
    for r in rows:
        r["_gen"] = file_generation(r, folder, number)
        if r["_gen"] == version:
            own.append(r)
        elif r["_gen"] and db.parse_version(r["_gen"]) < target:
            inherited.append(r)
    if not own or not inherited:
        return []

    by_key: dict[tuple[str, str], list[str]] = {}
    for r in own:
        by_key.setdefault(_fkey(r), []).append(_base(r))
    own_names = {_base(r).lower() for r in own} | _own_folder_names(folder, version)

    retired: list[dict] = []
    for r in inherited:
        base, key = _base(r), _fkey(r)
        if base.lower() in own_names:
            why, by = f"v{version} has its own {base}", [base]
        elif key in by_key:
            why, by = (f"replaced by this revision's own {key[0]} "
                       f"{key[1] or 'file'}"), by_key[key]
        else:
            continue
        if not dry_run:
            conn.execute("DELETE FROM files WHERE id=?", (r["id"],))
        retired.append({
            "id": r["id"], "name": base, "path": r["path"], "role": r["role"],
            "size": r["size"], "backend": r["backend"] or "vault",
            "from_version": r["_gen"], "replaced_by": by, "why": why})
    if retired and not dry_run:
        db.add_event(
            conn, "file.superseded", item_id=item_id, revision_id=rev_id,
            actor=actor,
            data={"version": version, "retired": len(retired), "files": retired,
                  "note": (f"{number} v{version} gained files of its own, so "
                           f"{len(retired)} row(s) carried in from older version "
                           "folders were retired from its manifest. The bytes are "
                           "untouched — vault copies stay in git history, "
                           "offloaded objects keep their key.")})
    return retired


def set_file_role(conn: sqlite3.Connection, file_id: int, role: str,
                  actor: str = "") -> dict:
    """DECLARE what a file is for — the verb that drains the review
    queue.

    The role VALUE may not even change; what changes is `role_source`, from a
    guess to 'declared'. That is not bookkeeping, it is the entire point: plm
    inferred 837 classifications and could not tell you which of them anybody had
    ever actually agreed with. Confirming an inference is a real act.

    ALLOWED ON A FROZEN REVISION, unlike remove_file — and the distinction is
    worth stating. Removing a file changes what the record CONTAINS. Saying what
    an already-present file IS adds a fact about it and takes nothing away; a
    released drawing that plm had mislabelled a render is a labelling error, and
    refusing to let anyone correct it would leave the record permanently wrong to
    protect its immutability. The bytes, the hash and the manifest are untouched.

    `media` is deliberately NOT settable here. Format is measured, not declared:
    if the sniffer is wrong about the bytes, that is a bug in roles.MEDIA_MAGIC,
    and papering over it per-row would hide it.
    """
    roles.require(role)
    row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not row:
        raise OpsError(f"no such file id: {file_id}", 404)
    rev = conn.execute("SELECT * FROM revisions WHERE id=?",
                       (row["revision_id"],)).fetchone()
    item = conn.execute("SELECT * FROM items WHERE id=?",
                        (rev["item_id"],)).fetchone()
    was, was_src = row["role"], _row_get(row, "role_source")

    # In a repo, a file's role IS its line in `[files]` — the manifest is what a
    # clone reads, and plm's `files.role` is the index of it. So declaring a
    # role on a git-origin draft moves that line, in a commit.
    grev = _rev_for_git(conn, rev["id"])
    g = git_draft(conn, grev, require=False) if grev else {}
    if g:
        path = row["path"]
        moved = False
        files = g["doc"].setdefault("files", {})
        for r, paths in list(files.items()):
            if path in paths:
                rest = [p for p in paths if p != path]
                if rest:
                    files[r] = rest
                else:
                    files.pop(r)
                moved = True
        for ex in g["doc"].get("files_external") or []:
            if ex.get("name") == path:
                ex["role"] = role
                moved = True
        if not moved and path not in files.get(role, []):
            raise OpsError(
                f"{_base(row)} is not named in {grev['number']} "
                f"v{grev['version']}'s plm.toml, so plm cannot move its role "
                "there. Re-index the revision and try again.", 409)
        if path not in (files.get(role) or []) and not any(
                ex.get("name") == path
                for ex in g["doc"].get("files_external") or []):
            files[role] = sorted((files.get(role) or []) + [path])
        g["doc"]["files"] = dict(sorted(files.items()))
        res = git_write(
            conn, g, grev, actor=actor,
            message=(f"{_base(row)} is a {role} on {grev['number']} "
                     f"v{grev['version']}" + (f" (was {was})" if was != role
                                              else " (confirmed)")))
        new = conn.execute(
            "SELECT * FROM files WHERE revision_id=? AND path=?",
            (rev["id"], path)).fetchone()
        return {"file_id": new["id"] if new else file_id,
                "number": item["number"], "version": rev["version"],
                "file": _base(row), "role": role,
                "role_source": roles.DECLARED, "was": was,
                "was_source": was_src or "",
                "media": new["media"] if new else row["media"],
                "media_source": _row_get(new or row, "media_source"),
                "warning": roles.mismatch(role, (new or row)["media"] or ""),
                "git": res}

    with conn:
        conn.execute("UPDATE files SET role=?, role_source=? WHERE id=?",
                     (role, roles.DECLARED, file_id))
        db.add_event(conn, "file.role_declared", item_id=item["id"],
                     revision_id=rev["id"], actor=actor,
                     data={"file": _base(row), "file_id": file_id,
                           "role": role, "was": was,
                           "was_source": was_src or "unconfirmed"})
    return {"file_id": file_id, "number": item["number"],
            "version": rev["version"], "file": _base(row),
            "role": role, "role_source": roles.DECLARED,
            "was": was, "was_source": was_src or "",
            "media": row["media"], "media_source": _row_get(row, "media_source"),
            "warning": roles.mismatch(role, row["media"] or "")}


@_explodes
def remove_file(conn: sqlite3.Connection, number: str | int = "", *,
                version: str = "", file_id: int = 0, name: str = "",
                actor: str = "") -> dict:
    """Take one file off a revision's manifest — the manual counterpart to
    supersede_inherited, for what a rule cannot judge.

    A files row is a POINTER, so removing it removes a claim, not content: the
    vault copy stays in git history and an offloaded object keeps its immutable
    key. That is what makes this safe enough to offer at all.

    Two regimes, from the same law that governs BOM edits (_draft_revision):

    - a DRAFT is a working revision — any row may go.
    - a frozen revision (IN_REVIEW and everything past it) is a RECORD, and a
      record does not lose its own files. Only rows carried in from an OLDER
      version folder may be retired there, because those are provably not this
      revision's content — the release was approved with its own files, and this
      only stops the manifest claiming the previous generation's as well.

    A row whose generation cannot be proven from its path/key counts as the
    revision's own, so ambiguity is refused rather than resolved in favour of
    deleting something.
    """
    if not file_id and not name:
        raise OpsError("say which file: pass file_id, or name=<filename> "
                       "(with the item number)")
    if file_id:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            raise OpsError(f"no such file id: {file_id}", 404)
        rev = conn.execute("SELECT * FROM revisions WHERE id=?",
                           (row["revision_id"],)).fetchone()
        item = conn.execute("SELECT * FROM items WHERE id=?",
                            (rev["item_id"],)).fetchone()
        if number:
            ref = _item(conn, number)
            if ref["id"] != item["id"]:
                raise OpsError(f"file {file_id} belongs to {item['number']}, "
                               f"not {ref['number']}", 409)
    else:
        item = _item(conn, number)
        found = db.find_revision(conn, item["id"], version)
        if not found:
            raise OpsError(f"no revision for {item['number']} "
                           f"{version or '(latest)'}", 404)
        rev = conn.execute("SELECT * FROM revisions WHERE id=?",
                           (found["id"],)).fetchone()
        rows = [r for r in conn.execute(
            "SELECT * FROM files WHERE revision_id=?", (rev["id"],))]
        want = Path(name).name.lower()
        hits = [r for r in rows if _base(r).lower() == want]
        if not hits:
            have = ", ".join(sorted(_base(r) for r in rows)) or "(no files)"
            raise OpsError(f"{item['number']} v{rev['version']} has no file "
                           f"named {name!r}. It has: {have}", 404)
        if len(hits) > 1:
            ids = ", ".join(f"{_base(r)}#{r['id']}" for r in hits)
            raise OpsError(f"{len(hits)} rows on {item['number']} "
                           f"v{rev['version']} are named {name!r} — pass "
                           f"file_id to say which: {ids}", 409)
        row = hits[0]

    base = _base(row)
    gen = file_generation(row, item["folder"] or "", item["number"])
    inherited = bool(gen) and gen != rev["version"]

    # In a repo, taking a file off the manifest means taking it OUT OF THE TREE:
    # the row and the bytes are the same statement there, and leaving a blob
    # nobody references would be a clone that still carries a file the revision
    # says it does not have. Git keeps the old bytes in history either way,
    # which is what makes this as safe as removing a pointer was.
    grev = _rev_for_git(conn, rev["id"])
    g = git_draft(conn, grev, require=False) if grev else {}
    if g and (row["backend"] or "") in ("git", "lfs", ""):
        path = row["path"]
        files_tbl = {r: [p for p in (v if isinstance(v, list) else [v])
                         if p != path]
                     for r, v in (g["doc"].get("files") or {}).items()}
        g["doc"]["files"] = {r: v for r, v in sorted(files_tbl.items()) if v}
        g["doc"]["files_external"] = [
            e for e in (g["doc"].get("files_external") or [])
            if e.get("name") != path]
        if g["doc"].get("primary") == path:
            from . import manifest as mf
            g["doc"]["primary"] = mf.primary_file(g["doc"]["files"],
                                                  g["doc"]["files_external"])
        external = (row["backend"] or "") not in ("git", "lfs")
        res = git_write(
            conn, g, grev, actor=actor,
            changes=None if external else {path: None},
            message=(f"Remove {base} from {item['number']} "
                     f"v{rev['version']}"))
        note = (f"Removed {base} from {item['number']} v{rev['version']} "
                f"({rev['state']}) in commit {res.get('commit', '')[:10]}. The "
                "bytes are untouched and remain in this repository's history.")
        db.add_event(conn, "file.remove", item_id=item["id"],
                     revision_id=rev["id"], actor=actor,
                     data={"file_id": row["id"], "name": base, "path": path,
                           "role": row["role"], "size": row["size"],
                           "backend": row["backend"] or "git",
                           "from_version": gen, "inherited": inherited,
                           "state": rev["state"], "note": note, "git": res})
        return {"number": item["number"], "version": rev["version"],
                "revision_id": rev["id"], "file_id": row["id"],
                "removed": base, "path": path, "role": row["role"],
                "size": row["size"], "backend": row["backend"] or "git",
                "inherited": inherited, "from_version": gen,
                "state": rev["state"], "note": note, "git": res}

    if rev["state"] != "DRAFT" and not inherited:
        where = (f"it lives in this revision's own versions/v{gen}/ folder"
                 if gen == rev["version"] else
                 "its path does not prove which version folder it came from, so "
                 "plm treats it as this revision's own")
        raise OpsError(
            f"{item['number']} v{rev['version']} is {rev['state']} and frozen, "
            f"and {base} is not a carried-forward row — {where}. A released "
            "record does not lose its own files. What you can do: Revise ↑ "
            "first and remove it on the new DRAFT, or upload the file that "
            "replaces it (same role+extension), which retires the old row as "
            "superseded. Only rows inherited from an OLDER version folder can "
            "be retired on a frozen revision.", 409)

    conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
    backend = row["backend"] or "vault"
    keeps = ("git history" if backend == "vault"
             else f"the {backend} object store under its immutable key")
    note = (
        f"Removed {base} from {item['number']} v{rev['version']} "
        f"({rev['state']}). "
        + (f"It was a row carried forward from v{gen} — a pointer at an older "
           "generation's bytes, not this revision's content."
           if inherited else
           "It was this revision's own row; the revision is a DRAFT, so its "
           "manifest is still being decided.")
        + f" The bytes are untouched and remain in {keeps}.")
    db.add_event(conn, "file.remove", item_id=item["id"], revision_id=rev["id"],
                 actor=actor,
                 data={"file_id": row["id"], "name": base, "path": row["path"],
                       "role": row["role"], "size": row["size"],
                       "backend": backend, "from_version": gen,
                       "inherited": inherited, "state": rev["state"],
                       "note": note})
    return {"number": item["number"], "version": rev["version"],
            "revision_id": rev["id"], "file_id": row["id"], "removed": base,
            "path": row["path"], "role": row["role"], "size": row["size"],
            "backend": backend, "inherited": inherited, "from_version": gen,
            "state": rev["state"], "note": note}


def backfill_supersede(conn: sqlite3.Connection, *, apply: bool = False,
                       actor: str = "plm", number: str = "") -> dict:
    """The one-time sweep: apply supersede_inherited to history.

    Same helper, same conservative rule, so the backfill cannot diverge from the
    write path — a revision that was cluttered before the fix lands exactly where
    it would have landed had the fix always existed. apply=False reports and
    writes nothing."""
    out: dict = {"applied": apply, "items": [], "revisions_touched": 0,
                 "files_before": 0, "files_after": 0, "retired": 0}
    where, args = "", []
    if number:
        item = _item(conn, number)
        where, args = "WHERE id=?", [item["id"]]
    for it in conn.execute(f"SELECT * FROM items {where} ORDER BY number", args):
        revs = []
        for rev in conn.execute(
                "SELECT * FROM revisions WHERE item_id=? ORDER BY vmaj,vmin,vpat",
                (it["id"],)):
            before = conn.execute("SELECT COUNT(*) FROM files WHERE revision_id=?",
                                  (rev["id"],)).fetchone()[0]
            retired = supersede_inherited(
                conn, item_id=it["id"], number=it["number"],
                folder=it["folder"] or "", rev_id=rev["id"],
                version=rev["version"], actor=actor, dry_run=not apply)
            out["files_before"] += before
            out["files_after"] += before - len(retired)
            if not retired:
                continue
            out["revisions_touched"] += 1
            out["retired"] += len(retired)
            revs.append({"version": rev["version"], "state": rev["state"],
                         "before": before, "after": before - len(retired),
                         "retired": [{"name": r["name"],
                                      "from_version": r["from_version"],
                                      "why": r["why"]} for r in retired]})
        if revs:
            out["items"].append({"number": it["number"], "name": it["name"],
                                 "retired": sum(len(r["retired"]) for r in revs),
                                 "revisions": revs})
    if apply and out["retired"]:
        db.add_event(
            conn, "files.backfill", actor=actor,
            data={"reason": "de-clutter file manifests",
                  "retired": out["retired"],
                  "revisions_touched": out["revisions_touched"],
                  "items": [i["number"] for i in out["items"]],
                  "note": (
                      " resolved. revise() carries file ROWS forward "
                      "(correct dataset semantics), but version-stamped export "
                      "filenames meant upload_files' same-basename replacement "
                      "never retired the generation being replaced, so revisions "
                      "accumulated rows spanning several version folders. "
                      "checkin and upload_files now call ops.supersede_inherited "
                      "at write time: an inherited row retires only when this "
                      "revision has its own file of the same role+extension, or "
                      "its own copy of the same filename. This event records the "
                      "one-time sweep of existing history under that same rule. "
                      "No bytes were deleted — vault copies stay in git history, "
                      "offloaded objects keep their keys — and ops.remove_file "
                      "(DELETE /api/files/{id}, MCP remove_file) is the manual "
                      "counterpart for what the rule cannot judge.")})
    return out


def _revise_base(conn: sqlite3.Connection, item, latest) -> dict:
    """The commit in the item's repo that IS the revision being revised — the
    tree the new DRAFT starts from.

    THIS IS WHERE AN ITEM CROSSES OVER TO GIT-FIRST, and the rule is wider than
    `git_origin`'s on purpose. `git_origin` asks "is this revision's dataset a
    commit", which is false for everything `split` replayed (their `git_commit`
    is a VAULT sha). But the repo DOES hold that revision — as the tag `vX.Y.Z`
    it was released under, or as the `draft/*` branch it sits on — and a revise
    off that commit is exactly "carry everything forward", for free, because it
    is the same tree (§4: `ops.revise`'s copy logic deleted).

    Returns {} when there is no repo or no ref for that revision: the old
    row-copying path then runs unchanged, which is what keeps this convertible
    one item at a time instead of as a flag day.
    """
    from . import gitvalidate as gv, gitwrite
    if not git_writes(conn, item):
        return {}           # `off`/`read`: no crossover — the row-copying revise
    try:
        repo = gitwrite.repo_path(item["number"])
    except gitwrite.WriteError:
        return {}
    version = latest["version"]
    origin = git_origin(conn, dict(latest) | {"number": item["number"]})
    if origin:
        return {"repo": repo, "commit": origin["commit"], "via": "its commit"}
    sha = gv.releases(repo).get(version, "")
    if sha:
        return {"repo": repo, "commit": sha, "via": f"tag v{version}"}
    for ref, v in gv.draft_versions(repo).items():
        if v == version:
            return {"repo": repo, "commit": gitwrite.tip(repo, ref),
                    "via": ref[len("refs/heads/"):]}
    return {}


def _next_version(repo, bump: str) -> str:
    """The version a new draft may claim, by the repo's own rule.

    The same one `plm/gitvalidate.py` enforces on a push — newest release tag,
    else newest open draft — because the repo is the record of what is taken.
    Asking the DB instead would mint a version the push validator then refused,
    and "plm made it and plm won't take it" is the worst kind of bug.
    """
    from . import gitvalidate as gv
    base, _kind = gv.bump_base(repo)
    legal = gv.legal_bumps(base)
    return legal.get(bump) or legal.get("initial") or legal["patch"]


def _revise_git(conn: sqlite3.Connection, item, latest, base: dict, *,
                bump: str, summary: str, actor: str,
                rebind: str = "keep",
                rebind_lines: dict[int, str] | None = None) -> dict:
    """revise, as a branch and one commit. See `revise`."""
    from . import gitwrite
    number = item["number"]
    repo = base["repo"]
    new_version = _next_version(repo, bump)
    if conn.execute("SELECT 1 FROM revisions WHERE item_id=? AND version=?",
                    (item["id"], new_version)).fetchone():
        raise OpsError(f"{number} v{new_version} already exists", 409)
    branch = f"refs/heads/draft/v{new_version}"

    doc = gitwrite.read_manifest(repo, base["commit"])
    doc["version"] = new_version
    doc["state"] = "DRAFT"
    doc["maturity"] = ""
    doc["summary"] = summary or f"Revised from v{latest['version']}"
    doc["author"] = actor or doc.get("author") or ""
    doc["date"] = time.strftime("%Y-%m-%d")
    # Item-level master data has ONE home — the item row — and the manifest
    # carries a copy so a clone is complete. A rename that happened while no
    # draft was open lands here, at the next commit plm authors.
    doc["number"], doc["name"] = number, item["name"]
    doc["kind"], doc["project"] = item["kind"], item["project"] or ""
    doc["product"] = bool(item["product"])

    links = _rebind_links(conn, doc, rebind=rebind, rebind_lines=rebind_lines)
    res = gitwrite.commit(
        number, branch, manifest_doc=doc, links=links or None,
        actor=actor or "plm", origin="api", parent=base["commit"],
        new_branch=True,
        message=(doc["summary"]
                 + f"\n\nRevised {number} v{latest['version']} -> "
                   f"v{new_version} ({bump}) off {base['via']}.\n"))
    from . import gitindex
    index_draft(conn, number, res["commit"], ref=branch,
                          actor=actor or item["name"], origin="api",
                          state="DRAFT")
    db.release_checkout(conn, item["id"])
    db.refresh_latest(conn, item["id"])
    row = conn.execute(
        "SELECT * FROM revisions WHERE item_id=? AND version=?",
        (item["id"], new_version)).fetchone()
    db.add_event(conn, "revise", item_id=item["id"], revision_id=row["id"],
                 actor=actor,
                 data={"from": latest["version"], "to": new_version,
                       "bump": bump, "git": {"branch": branch,
                                             "commit": res["commit"],
                                             "base": base["commit"],
                                             "via": base["via"]}})
    return dict(row)


def _rebind_links(conn: sqlite3.Connection, doc: dict, *, rebind: str,
                  rebind_lines: dict[int, str] | None) -> dict:
    """How BOM child bindings carry over, expressed as gitlinks.

    On the DB path `rebind` rewrites `bom.child_version`; here the pin IS the
    gitlink (§5.1), so the same three answers are three ways of moving it.
    `keep` moves nothing — the new tree inherits the parent's pins, which is
    what "the same tree" already means.
    """
    links: dict[str, str] = {}
    if rebind == "keep" and not rebind_lines:
        return links
    for line in (doc.get("bom") or []):
        path = str(line.get("path") or "")
        if not path.startswith("parts/"):
            continue
        child_number = path.split("/", 1)[1]
        child = db.item_by_number(conn, child_number)
        want = ""
        if rebind_lines and child and child["id"] in rebind_lines:
            want = rebind_lines[child["id"]]
        elif rebind == "latest" and not line.get("track"):
            row = conn.execute(
                "SELECT r.version FROM items i JOIN revisions r "
                "ON r.id=i.latest_rev_id WHERE i.id=?",
                (child["id"],)).fetchone() if child else None
            want = row["version"] if row else ""
        elif rebind == "imprecise":
            line["track"] = "release"
        else:
            continue
        if rebind == "imprecise":
            want = ""
        try:
            sha, _why = _pin_for(conn, child_number, want)
        except OpsError:
            continue          # the child cannot offer that commit; leave the pin
        links[path] = sha
        if want:
            line.pop("track", None)
    return links


@_explodes
def revise(conn: sqlite3.Connection, number: str, *, bump: str = "patch",
           summary: str = "", actor: str = "", rebind: str = "keep",
           rebind_lines: dict[int, str] | None = None) -> dict:
    """New DRAFT revision seeded from the latest. rebind: how BOM child
    versions carry over — keep | latest | imprecise. rebind_lines overrides
    specific children (child_item_id -> version), used by propagate.

    **On an item that lives in a repo this creates the draft BRANCH** —
    `draft/v<next>` off the commit the revision being revised is, carrying one
    commit whose only change is the bumped `version` in `plm.toml`. Nothing is
    copied: the files, the BOM, the gitlinks, the attributes and the routing are
    carried forward by being the same tree, which is the whole reason the
    version folders went away (§4). Everything downstream — attribute edits, BOM
    verbs, uploads — then has a branch to commit onto.
    """
    item = _item(conn, number)
    claim_checkout(conn, item, actor, op=f"revise {number}")
    latest = conn.execute("SELECT * FROM revisions WHERE id=?",
                          (item["latest_rev_id"],)).fetchone()
    if not latest:
        raise OpsError(f"{number} has no revisions to revise — check one in first", 400)
    base = _revise_base(conn, item, latest)
    if base:
        return _revise_git(conn, item, latest, base, bump=bump,
                           summary=summary, actor=actor, rebind=rebind,
                           rebind_lines=rebind_lines)
    new_version = db.bump_version(latest["version"], bump)
    if conn.execute("SELECT 1 FROM revisions WHERE item_id=? AND version=?",
                    (item["id"], new_version)).fetchone():
        raise OpsError(f"{number} v{new_version} already exists", 409)

    rev_id = db.upsert_revision(
        conn, item["id"], new_version, state="DRAFT",
        summary=summary or f"Revised from v{latest['version']}",
        author=actor, source=latest["source"], selector=latest["selector"],
        # The column-backed facts (plm/attributes.py) describe the geometry this
        # revision starts life with, so they ride along like the files do — and
        # are re-measured when the geometry actually changes.
        material=latest["material"], mass_g=latest["mass_g"],
        # HOW it is made rides along too (plm/fab.py): a revise of a
        # cut part is still a cut part. Same argument as material.
        process=latest["process"])

    # Attributes carry forward for the same reason files do: a new DRAFT starts
    # as a copy of what it was seeded from (plm/attributes.py).
    from . import attributes as attrs
    attrs.copy(conn, latest["id"], rev_id)
    # And so does the ROUTING (plm/fab.py), for the same reason and with the
    # same caveat: the new DRAFT starts as a copy, and its operations are then
    # corrected wherever the geometry actually moved.
    from . import fab
    fab.copy(conn, latest["id"], rev_id)
    # Revision-level TAGS ride along too (plm/tags.py). A caveat MUST
    # survive a revision that did not address it: incrementing a version number
    # does not go and measure the truck. Clearing it stays a deliberate act,
    # which is the whole property that makes the tag worth having.
    from . import tags as _tags
    _tags.copy(conn, latest["id"], rev_id)

    # Files ride along on revise (dataset semantics): same vault paths until
    # new content replaces them at the next check-in.
    #
    # The classification and its PROVENANCE ride along too. Copying the
    # role without `role_source` would silently promote an inherited guess into
    # something that looks declared, which is the exact laundering this ticket
    # exists to stop — and it would launder it once per revision, so the guess
    # would look more established the longer it survived.
    for f in conn.execute("SELECT * FROM files WHERE revision_id=?", (latest["id"],)):
        conn.execute(
            """INSERT INTO files (revision_id,role,path,sha256,size,media,backend,
                                  key,role_source,media_source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (rev_id, f["role"], f["path"], f["sha256"], f["size"], f["media"],
             f["backend"], f["key"], _row_get(f, "role_source"),
             _row_get(f, "media_source")))

    for line in conn.execute("SELECT * FROM bom WHERE parent_rev_id=?", (latest["id"],)):
        cv = line["child_version"]
        if rebind_lines and line["child_item_id"] in rebind_lines:
            cv = rebind_lines[line["child_item_id"]]
        elif rebind == "latest" and cv:
            child = conn.execute(
                """SELECT r.version FROM items i JOIN revisions r ON r.id=i.latest_rev_id
                   WHERE i.id=?""", (line["child_item_id"],)).fetchone()
            cv = child["version"] if child else cv
        elif rebind == "imprecise":
            cv = ""
        conn.execute(
            """INSERT INTO bom (parent_rev_id,find_number,child_item_id,child_version,
                                qty,unit,alt_group,notes,suppressed) VALUES (?,?,?,?,?,?,?,?,?)""",
            (rev_id, line["find_number"], line["child_item_id"], cv,
             line["qty"], line["unit"], line["alt_group"], line["notes"],
             line["suppressed"]))

    db.release_checkout(conn, item["id"])
    db.refresh_latest(conn, item["id"])
    db.add_event(conn, "revise", item_id=item["id"], revision_id=rev_id, actor=actor,
                 data={"from": latest["version"], "to": new_version, "bump": bump})
    row = conn.execute("SELECT * FROM revisions WHERE id=?", (rev_id,)).fetchone()
    return dict(row)


def _new_folder(project: str, kind: str, number: str, name: str) -> str | None:
    """Kinds that carry FILES of their own get a vault folder. An asset does: its
    scan/CAD/measurement record is the whole reason it exists as an item."""
    from . import vault as vlt
    return (vlt.item_folder(project, kind, number, name)
            if kind in ("part", "assembly", "coupon", "asset") else None)


def _mint(conn: sqlite3.Connection, prefix: str, cls: str, *, name: str,
          kind: str, project: str, tries: int = 8) -> tuple[str, int]:
    """Allocate the next free number and register the item under it.

    next_number() reads, then upsert_item() writes, and between those two a
    concurrent create can take the number — SQLite serializes the writes but not
    the read that chose the value. The loser used to surface `UNIQUE constraint
    failed: items.number`, which is a 500 for a caller who did nothing wrong; it
    now simply takes the next number. Bounded, so a genuinely broken sequence
    fails with an explanation instead of spinning.

    Note what this does NOT claim: two SIMULTANEOUS identical creates still make
    two items. That is correct — plm cannot tell an in-flight twin from a second
    real part — and it is not the failure reported. That one is a client
    TIMEOUT followed by a retry, where the first attempt has committed and
    plm/idem.py answers from the replay record."""
    last = None
    for _ in range(tries):
        number = next_number(conn, prefix, cls)
        try:
            item_id = db.upsert_item(
                conn, number, name=name, kind=kind, project=project,
                folder=_new_folder(project, kind, number, name))
            return number, item_id
        except sqlite3.IntegrityError as exc:
            last = exc          # somebody took it between the read and the write
    raise OpsError(
        f"could not allocate a free {prefix}-{cls}-#### number after {tries} "
        f"attempts — concurrent creates keep taking it ({last}). Retry, or pass "
        "an explicit number.", 409)


def create_item(conn: sqlite3.Connection, name: str, kind: str, project: str, *,
                number: str = "", prefix: str = "", actor: str = "",
                idempotency_key: str = "") -> dict:
    """Register an item. Explicit number (VALIDATED — see validate_number), or
    auto-assigned from prefix+class (e.g. prefix KML, kind part -> next
    KML-PRT-####). The auto path is the intended one.

    IDEMPOTENT. This is one of exactly two ops where plm MINTS
    identity, so it is the one place a retry could not be refused by uniqueness:
    attempt two would allocate the NEXT free number and succeed, which is how a
    client timeout on a write that had already committed left three empty
    CCR-AST shells behind. A repeat of the same create — same actor, same
    arguments, within config.IDEMPOTENCY_AUTO_WINDOW, or any time at all with a
    matching `idempotency_key` — replays the first result instead (plm/idem.py).
    An explicit `number` never needed this: that path is a 409 already."""
    from . import config as cfg, idem, vault as vlt
    if kind not in cfg.CLASS_BY_KIND:
        raise OpsError(f"kind must be one of {sorted(cfg.CLASS_BY_KIND)}")
    fields = {"name": name, "kind": kind, "project": project,
              "number": number, "prefix": prefix}
    if not number:
        replay = idem.lookup(conn, op="create_item", actor=actor, fields=fields,
                             key=idempotency_key)
        if replay:
            return replay
        if not prefix:
            raise OpsError("auto-assign needs a prefix (e.g. KML)")
        number, item_id = _mint(conn, prefix, cfg.CLASS_BY_KIND[kind],
                                name=name, kind=kind, project=project)
    else:
        number = validate_number(number, kind)
        if db.item_by_number(conn, number):
            raise OpsError(f"item already exists: {number}", 409)
        # Kinds that carry FILES of their own get a vault folder. An asset does:
        # its scan/CAD/measurement record is the whole reason it exists.
        item_id = db.upsert_item(
            conn, number, name=name, kind=kind, project=project,
            folder=_new_folder(project, kind, number, name))
    # The repo IS the item (§6d). Created here, from a template, because plm
    # owns the number and therefore there is no push-to-create: a repository
    # exists because plm allocated an identity for it.
    git = (new_item_repo(conn, number, name=name, kind=kind, project=project,
                         actor=actor) if git_writes(conn, project) else {})
    db.add_event(conn, "item.create", item_id=item_id, actor=actor,
                 data=_with_git({"number": number}, git))
    out = _with_git({"number": number, "name": name, "kind": kind,
                     "project": project, "id": item_id}, git)
    if not fields["number"]:
        idem.record(conn, op="create_item", actor=actor, fields=fields,
                    key=idempotency_key, response=out, item_id=item_id)
    return out


def _relink(conn: sqlite3.Connection, res: dict, relation: str, a: str, b: str,
            *, from_version: str = "", to_version: str = "", note: str = "",
            actor: str = ""):
    """relations.add, tolerating the retry case.

    add_coupon/add_asset are create_item + link. When create_item REPLAYS a
    previous attempt (plm/idem.py), the link it made is already there, and
    relations.add would answer 409 — turning a clean retry into an error. So on
    a replay the existing link is looked up instead of re-added. Freshly created
    items still get the strict path: a duplicate link on a NEW item would be a
    real bug."""
    from . import relations as rel
    try:
        # Through the ops verb, not `relations.add`: in `write` the claim must
        # land in its HOME (the new coupon's draft plm.toml), or the next index
        # of that draft would reconcile a registry-only row away (stage 10).
        return add_relation(conn, relation, a, b, from_version=from_version,
                            to_version=to_version, note=note, actor=actor)
    except rel.RelationError as exc:
        if res.get("replayed") and exc.code == 409:
            found = conn.execute(
                """SELECT r.id FROM relations r
                   JOIN items f ON f.id=r.item_id
                   JOIN items t ON t.id=r.target_item_id
                   WHERE r.relation=? AND f.number=? AND t.number=?""",
                (relation, a, b)).fetchone()
            if found:
                return rel.record(conn, found["id"])
        raise OpsError(str(exc), exc.code) from exc


def add_coupon(conn: sqlite3.Connection, name: str, for_number: str, *,
               for_version: str = "", project: str = "", prefix: str = "",
               number: str = "", note: str = "", actor: str = "",
               idempotency_key: str = "") -> dict:
    """The one-call coupon: register a `coupon` item (auto-numbered in the CPN
    class of the part's own prefix) and link it `coupon_for` the revision it
    validates. Nothing coupon-specific happens here that the two underlying
    calls would not do — this exists because "make a coupon for this revision"
    is one thought, and two round-trips is how the link gets forgotten."""
    from . import relations as rel
    parent = _item(conn, for_number)
    if not number and not prefix:
        parts = for_number.split("-")
        if len(parts) < 3:
            raise OpsError(f"cannot derive a number scheme from {for_number}; "
                           "pass prefix or number")
        prefix = parts[0]
    res = create_item(conn, name, "coupon", project or parent["project"],
                      number=number, prefix=prefix, actor=actor,
                      idempotency_key=idempotency_key)
    link = _relink(conn, res, "coupon_for", res["number"], for_number,
                   to_version=for_version, note=note, actor=actor)
    return {**res, "verifies": link["to"], "verifies_version": link["to_version"],
            "relation_id": link["id"]}


# Which relation an asset may be registered WITH in one call. All three are
# ordinary registry entries (plm/relations.py); this tuple only says which of
# them make sense at the moment an asset is first written down, so the
# convenience call cannot invent a link type of its own.
ASSET_RELATIONS = ("mounts_to", "installed_on", "integration_envelope_for")


def add_asset(conn: sqlite3.Connection, name: str, for_number: str = "", *,
              relation: str = "mounts_to", project: str = "", prefix: str = "",
              number: str = "", for_version: str = "", asset_version: str = "",
              note: str = "", actor: str = "",
              idempotency_key: str = "") -> dict:
    """The one-call asset: register an `asset` item (auto-numbered in the AST
    class) and link the design to it.

    The exact counterpart of add_coupon, and for the same reason — "the rack
    bolts to the camper" is one thought, and two round-trips is how the link
    gets forgotten — but the DIRECTION differs, because the direction is the
    meaning:

      mounts_to / installed_on        design → asset   (the design does the
                                                        mounting)
      integration_envelope_for        asset → design   (the asset does the
                                                        constraining)

    for_number may be omitted to register a bare asset (a camper nothing is
    bolted to yet, or a type to hang instances off). Nothing asset-specific
    happens here that create_item + relations.add would not do.
    """
    from . import relations as rel
    if relation not in ASSET_RELATIONS:
        raise OpsError(
            f"{relation!r} is not an asset-registration relation — use one of "
            f"{', '.join(ASSET_RELATIONS)}, or create the asset and call "
            "link_items for anything else in the registry")
    parent = _item(conn, for_number) if for_number else None
    if not parent and not project:
        raise OpsError("a bare asset needs a project — it gets a vault folder "
                       "under projects/<project>/assets/, and there is no "
                       "related item to inherit one from")
    if not number and not prefix:
        base = for_number or number
        parts = base.split("-")
        if len(parts) < 3:
            raise OpsError(
                "cannot derive a number scheme — pass prefix (e.g. CCR) or "
                "number, or name the item this asset relates to")
        prefix = parts[0]
    res = create_item(conn, name, "asset",
                      project or (parent["project"] if parent else ""),
                      number=number, prefix=prefix, actor=actor,
                      idempotency_key=idempotency_key)
    if not parent:
        return {**res, "linked": None}
    envelope = relation == "integration_envelope_for"
    a, b = ((res["number"], for_number) if envelope
            else (for_number, res["number"]))
    link = _relink(conn, res, relation, a, b, actor=actor, note=note,
                   from_version=asset_version if envelope else for_version,
                   to_version=for_version if envelope else asset_version)
    return {**res, "relation": relation, "linked": link["to"],
            "from": link["from"], "from_version": link["from_version"],
            "to_version": link["to_version"], "relation_id": link["id"]}


def set_product(conn: sqlite3.Connection, number: str, product: bool = True, *,
                actor: str = "") -> dict:
    """Designate (or undesignate) an item as a PRODUCT — the end item the
    dashboard renders. ONE write path for the HTTP API and the MCP, because the
    refusal below has to be impossible to route around.

    A non-structural kind (config.NON_STRUCTURAL_KINDS — coupons, assets) can
    never be a product. The BOM guards already keep it out of every rollup; this
    keeps it off the dashboard, which is the OTHER way a coupon or a camper could
    become 'what Geoff is building'. /api/products filters on the same list, so
    a row flagged before this guard existed is excluded at read time too."""
    item = _item(conn, number)
    if product and item["kind"] in config.NON_STRUCTURAL_KINDS:
        raise OpsError(
            f"{item['number']} is {_a(item['kind'])}, which is never a product: "
            f"{NOT_IN_BOM.get(item['kind'], '')}. A product is the end item a "
            "project builds.", 400)
    conn.execute("UPDATE items SET product=?, updated_at=? WHERE id=?",
                 (1 if product else 0, db.now(), item["id"]))
    git = item_fields_commit(
        conn, item, {"product": bool(product)}, actor=actor,
        message=(f"{item['number']} is "
                 + ("a product — the end item a project builds"
                    if product else "no longer flagged a product")))
    db.add_event(conn, "item.product", item_id=item["id"], actor=actor,
                 data=_with_git({"product": product, "kind": item["kind"]}, git))
    return _with_git({"number": item["number"], "kind": item["kind"],
                      "product": product}, git)


@_explodes
def checkin(conn: sqlite3.Connection, number: str, version: str, *,
            state: str = "PROTOTYPE", summary: str = "", ecn: str = "",
            author: str = "", source: str = "", selector: str = "",
            files: list[dict] | None = None, bom: list[dict] | None = None) -> dict:
    """The canonical write — **now a shim over git** (§1). DEPRECATED: `git push`.

    57 calls this month, every one from an agent: this is how bots create
    revisions today, and it is not going away. What has to change is what it
    MEANS. Until stage 3c it minted a revision directly at `state`
    (`PROTOTYPE` by default) with **no approval row at all** — 446 of 454
    non-draft revisions in production got there this way — which git-first has no
    honest shape for, because a commit is a DRAFT until somebody says otherwise
    and only `approve_commit` may write a tag.

    So on an item that lives in a repo, one `checkin` is now:

      1. **one commit** on `draft/v<version>` — the revise, the files and the
         manifest together, built on the newest release (or the newest draft) so
         everything else carries forward by BEING the same tree;
      2. **a submission** with `target_state` = the state the caller asked for;
      3. **`plm/policy.py` decides.** At prototype and below it merges here and
         now, approved by the policy and recorded as such. At RELEASED it waits
         for a human, which is the point.

    The response keeps every key it had and gains `git`, `approval_id`,
    `policy`. The one value that changes is `state`, and it changes to the
    truth: `PROTOTYPE` when policy merged it, `IN_REVIEW` when it is waiting.
    Callers that treated the old answer as "it is released now" were being told
    something plm had not checked.

    A vault-era item with no repo keeps the old path unchanged, and its first
    `revise` is the crossing (stage 3b's rule).
    """
    from . import config as cfg, storage as stg, vault as vlt
    item_row = _item(conn, number)
    item_id = item_row["id"]
    takeover = claim_checkout(conn, item_row, author, op=f"check in {number}")
    if state not in cfg.STATES:
        raise OpsError(f"state must be one of {cfg.STATES}")
    base = _checkin_base(conn, item_row)
    if base:
        return _checkin_git(conn, item_row, version, base, state=state,
                            summary=summary, ecn=ecn, author=author,
                            source=source, selector=selector, files=files,
                            bom=bom)
    if conn.execute("SELECT 1 FROM revisions WHERE item_id=? AND version=?",
                    (item_id, version)).fetchone():
        raise OpsError(f"{number} v{version} already exists — versions are immutable", 409)
    for line in (bom or []):
        _vet_bom_child(conn, line["child"])

    # THE TRANSACTION BOUNDARY. Everything above is validation; below
    # is a slow vault write (files onto the synced volume + a git commit) and
    # then a burst of row writes. SQLite has one writer, so the row writes must
    # not begin until the vault work is done — and nothing already pending may
    # ride along either, which is what this commit is for. It is not cosmetic:
    # with a `url`-form file the caller's transaction was already open (an
    # upload.fetched event), and it stayed open across the git commit, which is
    # how unrelated writers ended up waiting out busy_timeout and reporting
    # `database is locked`.
    if conn.in_transaction:
        conn.commit()

    folder = item_row["folder"]
    commit_sha = ""
    file_rows: list[dict] = []
    # For the rollback below: what the vault looked like before we touched it.
    head_before, dir_existed, wrote_vault = "", False, False
    cls: dict[str, dict] = {}
    if files:
        # What is each of these? Decided here, while the bytes are still in
        # reach, and refused now if a role is unregistered — before any vault
        # commit or object upload has happened.
        cls = classify_inputs(files)
        # Bulk binaries may be offloaded to the storage default (lfs/local);
        # text, sources and viz stay in the vault (storage.py pins).
        # With PLM_BLOBSTORE_DEFAULT=vault (the default) nothing is offloaded.
        vault_files, offload = _split_destinations(files)
        if vault_files or not offload:
            if not folder:
                folder = vlt.item_folder(item_row["project"], item_row["kind"],
                                         number, item_row["name"])
            _fill_vault_bytes(vault_files)
            head_before = vlt.head()
            dir_existed = vlt.version_dir_exists(folder, version)
            wrote_vault = True
            file_rows = vlt.write_checkin(folder, version, vault_files)
            commit_sha = vlt.commit(
                folder, f"plm: check in {number} v{version} — {summary}")
        for f in offload:
            if _arrived(f):
                # A staged upload or a url arrival, already on local disk:
                # streamed into the backend a megabyte at a time.
                src = _arrived(f)
                file_rows.append(stg.put_file(
                    Path(src["path"]), number=number, version=version,
                    name=f["name"], role=f.get("role", ""),
                    sha256=src["sha256"], size=src["size"]))
            else:
                file_rows.append(stg.put(f["data"], number=number, version=version,
                                         name=f["name"], role=f.get("role", "")))

    # ATOMICITY: the rows go in one transaction that this function
    # OWNS, and if any of it fails the vault write above is undone before the
    # error leaves. Two stores, one outcome — no version folder in git that no
    # revision row points at, and no orphan rows.
    try:
        with conn:
            rev_id = db.upsert_revision(
                conn, item_id, version, state=state, summary=summary, ecn=ecn,
                author=author, source=source, selector=selector,
                git_commit=commit_sha)
            if folder and not item_row["folder"]:
                conn.execute("UPDATE items SET folder=? WHERE id=?", (folder, item_id))
            for fr in file_rows:
                _put_row(conn, rev_id, fr, cls)
            # Normally a no-op: check-in mints a version that did not exist, so
            # there is nothing carried forward to supersede. It runs anyway,
            # through the one shared helper, so "a revision's own file retires
            # the generation it replaces" is a property of WRITING FILES rather
            # than of one endpoint.
            superseded = supersede_inherited(
                conn, item_id=item_id, number=number, folder=folder or "",
                rev_id=rev_id, version=version, actor=author) if file_rows else []
            for line in (bom or []):
                child = _bom_child_id(conn, line["child"], item_row["project"])
                conn.execute(
                    """INSERT INTO bom (parent_rev_id,find_number,child_item_id,
                                        child_version,qty,unit,notes)
                       VALUES (?,?,?,?,?,?,?)""",
                    (rev_id, line.get("find"), child, line.get("version", ""),
                     line.get("qty", 1), line.get("unit", "EA"),
                     line.get("notes", "")))
            db.release_checkout(conn, item_id)
            db.refresh_latest(conn, item_id)
            db.add_event(conn, "checkin", item_id=item_id, revision_id=rev_id,
                         actor=author,
                         data={"version": version, "files": len(file_rows),
                               "takeover": takeover})
    except BaseException as exc:
        if wrote_vault:
            undo = vlt.rollback_checkin(
                folder, version, head_before=head_before, existed=dir_existed,
                message=(f"plm: roll back failed check-in of {number} "
                         f"v{version} — no revision row was written"))
            # Out of band on purpose: the transaction that would have carried
            # this event is the one that just rolled back.
            db.event_out_of_band(
                "checkin.rolled_back", item_id=item_id, actor=author,
                data={"version": version, "error": str(exc)[:500], **undo,
                      "note": (
                          f"The database half of {number} v{version} failed, so "
                          "the vault half was undone: the version folder plm had "
                          "just written (and committed) was removed and the undo "
                          "committed. Nothing partial survives — no revision "
                          "row, no orphan files rows, and the staged uploads were "
                          "NOT consumed, so the same check-in can be retried "
                          "with the same handles.")})
            raise OpsError(
                f"check-in of {number} v{version} failed and was rolled back "
                f"completely — the vault folder plm had written was removed and "
                f"nothing was registered. Underlying error: {exc}", 500) from exc
        raise

    # AFTER the commit, and in this order for a reason: the revision now
    # durably references these bytes, so it is finally safe to release the
    # arrivals. Doing it inside the transaction meant a rollback could delete
    # the staged object while the revision that owned it never existed.
    _consume_inputs(conn, files or [], actor=author)
    out = {"number": number, "version": version, "revision_id": rev_id,
           "git_commit": commit_sha, "files": file_rows,
           "superseded": superseded}
    if takeover:
        out["checkout_takeover"] = takeover
    return out


def _checkin_base(conn: sqlite3.Connection, item) -> dict:
    """The commit a `checkin` builds its one commit on, or {} for the old path.

    Newest RELEASE first, then the newest open draft, then an empty repo (an
    item created by `create_item` whose v0.1.0 draft is still there — which is
    the usual case, since `create_item` makes one). Same widening as
    `_revise_base`: the question is "does this item live in a repo", not "is this
    particular revision a commit".
    """
    from . import gitvalidate as gv, gitwrite, manifest as mf
    if item["kind"] in mf.CATALOG_KINDS:
        return {}         # a catalog item is a FILE, not a repo (§4)
    if not git_writes(conn, item):
        return {}         # `off`/`read`: the legacy check-in, as on master
    try:
        repo = gitwrite.repo_path(item["number"])
    except gitwrite.WriteError:
        return {}
    rels = gv.releases(repo)
    if rels:
        v = max(rels, key=lambda s: tuple(int(x) for x in s.split(".")))
        return {"repo": repo, "commit": rels[v], "via": f"tag v{v}"}
    drafts = gv.draft_versions(repo)
    if drafts:
        ref = max(drafts, key=lambda r: tuple(int(x) for x in
                                              drafts[r].split(".")))
        return {"repo": repo, "commit": gitwrite.tip(repo, ref),
                "via": ref[len("refs/heads/"):]}
    head = gitwrite.tip(repo, gv.default_ref(repo))
    return {"repo": repo, "commit": head, "via": "HEAD"} if head else \
        {"repo": repo, "commit": "", "via": "an empty repository"}


def _checkin_git(conn: sqlite3.Connection, item, version: str, base: dict, *,
                 state: str, summary: str, ecn: str, author: str, source: str,
                 selector: str, files: list[dict] | None,
                 bom: list[dict] | None) -> dict:
    """checkin, as ONE commit plus a submission. See `checkin`."""
    from . import gitindex, gitwrite, itemref, manifest as mf
    number = item["number"]
    repo = base["repo"]
    if conn.execute("SELECT 1 FROM revisions WHERE item_id=? AND version=?",
                    (item["id"], version)).fetchone():
        raise OpsError(f"{number} v{version} already exists — versions are "
                       "immutable", 409)
    if state != "DRAFT" and state not in SUBMIT_STATES:
        raise OpsError(
            f"`state = {state!r}` is not something a check-in can ask for. A "
            "check-in proposes a\nrevision at a maturity and plm's policy "
            f"decides: {', '.join(SUBMIT_STATES)}, or DRAFT to\nleave it open. "
            f"{state} is a LIFECYCLE decision about a revision that already "
            "exists —\nPOST /api/items/<n>/state, or a release promotion.", 400)

    doc = (gitwrite.read_manifest(repo, base["commit"]) if base["commit"]
           else mf.parse(f"schema = {mf.SCHEMA_VERSION}\n"))
    doc["version"] = version
    doc["state"] = "DRAFT"          # §5.5 — a commit is never born released
    doc["maturity"] = ""
    doc["summary"] = summary or f"Check in {number} v{version}"
    doc["author"] = author or doc.get("author") or "plm"
    doc["date"] = time.strftime("%Y-%m-%d")
    doc["ecn"] = ecn or ""
    if source or selector:
        doc["source"] = {"file": source or "", "selector": selector or ""}
    doc["number"] = number
    doc.update(itemref.snapshot_fields(conn, item["id"]))

    changes: dict[str, bytes] = {}
    if files:
        changes, _named = stage_files_into_doc(doc, files)

    links: dict[str, str] = {}
    for line in (bom or []):
        child_number = line["child"]
        _bom_child_id(conn, child_number, item["project"])   # auto-registers
        kind = _child_kind(conn, child_number)
        path, link_path, sha, _why = bom_pin(conn, child_number, kind,
                                             line.get("version", ""))
        entry: dict = {"path": path}
        if line.get("find") is not None:
            entry["find"] = line["find"]
        entry["qty"] = float(line.get("qty", 1))
        entry["unit"] = line.get("unit", "EA")
        if line.get("notes"):
            entry["notes"] = line["notes"]
        if not line.get("version"):
            entry["track"] = "release"
        doc["bom"] = [b for b in (doc.get("bom") or [])
                      if str(b.get("path") or "") != path] + [entry]
        links[link_path] = sha
    if bom:
        doc["bom"] = sorted(doc["bom"], key=_bom_key)

    branch = f"refs/heads/draft/v{version}"
    res = gitwrite.commit(
        number, branch, manifest_doc=doc, changes=changes or None,
        links=links or None, actor=author or "plm", origin="api",
        parent=base["commit"], new_branch=True,
        message=(doc["summary"] + "\n\n"
                 + f"Checked in {number} v{version} off {base['via']}.\n"
                   "One commit: the revision, its files and its manifest. "
                   "Everything else carried\nforward by being the same tree.\n"))
    _undo_ref(conn, number, branch, back_to="", only_if=res["commit"])
    _fault("after-git-write")
    index_draft(conn, number, res["commit"], ref=branch,
                          actor=author or "plm", origin="api", state="DRAFT")
    _consume_inputs(conn, files or [], actor=author)
    db.release_checkout(conn, item["id"])
    rev = db.find_revision(conn, item["id"], version)
    git = {"commit": res["commit"], "branch": branch, "backend": "git",
           "created": True, "base": base["commit"], "via": base["via"]}
    db.add_event(conn, "checkin", item_id=item["id"], revision_id=rev["id"],
                 actor=author, data={"version": version,
                                     "files": len(changes), "git": git,
                                     "shim": "revise+files+manifest in one "
                                             "commit (stage 3c §1)"})
    out = {"number": number, "version": version, "revision_id": rev["id"],
           "git_commit": res["commit"], "git": git,
           "files": [dict(r) for r in conn.execute(
               "SELECT * FROM files WHERE revision_id=? ORDER BY path",
               (rev["id"],))],
           # Nothing is carried forward to retire: the new revision inherits by
           # BEING the next commit on the same tree (stage 3b, `_upload_files_git`).
           "superseded": [], "state": "DRAFT",
           "deprecated": "checkin is a compatibility shim over git — prefer "
                         "`git push HEAD:refs/for/main -o maturity=<m>`, which "
                         "keeps your commit byte for byte."}
    if state == "DRAFT":
        out["note"] = ("left as a DRAFT: nothing was submitted, so no policy "
                       "was applied and no tag was written.")
        return out
    sub = submit_for_release(conn, number, version=version, target_state=state,
                             actor=author or "plm",
                             note=summary or f"Checked in v{version}")
    out.update({"state": sub["state"], "approval_id": sub["approval_id"],
                "target_state": sub["target_state"], "policy": sub["policy"]})
    if sub.get("approved_by_policy"):
        out["approved_by_policy"] = True
    if sub.get("held_by_policy"):
        # stage 16b: say why "just build it" did not merge it, and where
        out["held_by_policy"] = sub["held_by_policy"]
        out["change"] = sub.get("change") or {}
    if sub.get("git"):
        out["release"] = sub["git"]
    ev = sub.pop("_events", None) or ([sub.pop("_event")] if sub.get("_event")
                                      else [])
    out["_events"] = ev
    return out


@_explodes
def upload_files(conn: sqlite3.Connection, number: str, files: list[dict], *,
                 version: str = "", actor: str = "", message: str = "",
                 derived: bool = False) -> dict:
    """Working upload: put files onto a DRAFT revision without minting a new
    version. Released/prototype revisions are frozen — this is the 'just for
    changes' path while iterating. Git commits every overwrite, so history
    survives even though the PLM version number stands still.

    derived=True is the exception to the freeze: visualization derivatives
    (role 'viz' — e.g. an assembled STL regenerated from the revision's own
    source+selector) may attach to ANY revision, because derived geometry is
    not a design change — the Teamcenter JT-on-release pattern."""
    from . import storage as stg, vault as vlt
    item = _item(conn, number)
    rev = db.find_revision(conn, number, version)
    if not rev:
        raise OpsError(f"no revision for {number} {version or '(latest)'}", 404)
    if derived:
        for f in files:
            f["role"] = "viz"
    elif rev["state"] != "DRAFT":
        raise OpsError(
            f"{number} v{rev['version']} is {rev['state']} and frozen — "
            "Revise ↑ first (new DRAFT), or check in a new version", 409)
    takeover = claim_checkout(conn, item, actor, op=f"upload files to {number}")

    # GIT UNDER THE HOOD: on a revision that lives in a repo the bytes go into
    # the TREE, and `[files]` names them. Nothing is copied into the vault and
    # nothing is offloaded — a clone of this revision gets the file.
    grev = dict(rev) | {"number": item["number"]}
    g = git_draft(conn, grev, require=False)
    if g:
        out = _upload_files_git(conn, item, grev, g, files, actor=actor,
                                message=message)
        if takeover:
            out["checkout_takeover"] = takeover
        return out
    if git_origin(conn, grev):
        raise OpsError(
            f"{number} v{rev['version']} lives in a repository and is "
            f"{rev['state']} — its tree is behind an immutable tag, so nothing "
            "can be added to it.\nRevise ↑ and upload onto the new DRAFT. "
            "(Derived artifacts belong to the RELEASE rather than to the "
            "revision — docs/PLM_GIT_FIRST.md §6b — and release assets are a "
            "later phase.)", 409)

    # Same boundary as checkin(): the vault write and its git commit happen with
    # NO transaction open, so SQLite's one writer lock is never held across them
    #.
    if conn.in_transaction:
        conn.commit()

    folder = item["folder"] or vlt.item_folder(item["project"], item["kind"],
                                               number, item["name"])
    # Classify before storing, same as checkin(): measure the format from the
    # bytes while we still have them, and refuse an unregistered role before any
    # vault commit.
    cls = classify_inputs(files)
    # Same split as checkin(): bulk binaries follow PLM_BLOBSTORE_DEFAULT, while
    # text, 'source' and 'viz' stay pinned to the vault (storage.choose_backend).
    # With the default 'vault' this is exactly the old single-branch behavior.
    vault_files, offload = _split_destinations(files)

    # A file that used to be a vault copy and is now offloaded must not stay
    # behind in the version folder: a stale duplicate on disk would diverge
    # from the row (and a re-import would re-register it). Delete it, and let
    # the same commit record the removal — git keeps the old bytes.
    stale: list[str] = []
    for f in offload:
        prev = vlt.safe_path(
            f"{folder}/versions/v{rev['version']}/{Path(f['name']).name}")
        if prev.is_file():
            prev.unlink()
            stale.append(prev.name)

    file_rows: list[dict] = []
    commit_sha = ""
    if vault_files or stale:
        _fill_vault_bytes(vault_files)
        file_rows = vlt.write_checkin(folder, rev["version"], vault_files)
        commit_sha = vlt.commit(
            folder, message or (f"plm: viz derivative for {number} v{rev['version']}"
                                if derived else
                                f"plm: wip update {number} v{rev['version']}"))
    for f in offload:
        if src := _arrived(f):
            file_rows.append(stg.put_file(
                Path(src["path"]), number=number, version=rev["version"],
                name=f["name"], role=f.get("role", ""),
                sha256=src["sha256"], size=src["size"]))
        else:
            file_rows.append(stg.put(f["data"], number=number, version=rev["version"],
                                     name=f["name"], role=f.get("role", "")))

    # ONE transaction for the row writes, opened AFTER the vault work is done
    # and closed immediately. `with conn:` here is what makes the
    # ordering explicit; nothing slow may appear inside it.
    with conn:
        superseded = _upload_rows(
            conn, item, rev, folder, file_rows, offload,
            derived=derived, actor=actor, commit_sha=commit_sha,
            had_folder=bool(item["folder"]), wrote_vault=bool(vault_files),
            cls=cls)
    # After the commit: the revision durably references these bytes now, so it
    # is safe to release the arrivals (see staging.consume).
    _consume_inputs(conn, files, actor=actor)
    out = {"number": number, "version": rev["version"], "git_commit": commit_sha,
           "files": file_rows, "superseded": superseded}
    if takeover:
        out["checkout_takeover"] = takeover
    return out


def _tree_path_for(g: dict, name: str) -> str:
    """Where an uploaded file lands in the tree.

    In place of whatever is already called that (so a new generation of a mesh
    is a MODIFICATION of the file, with history and a diff — §6c-1), else at the
    root under its own name. plm does not invent a directory layout: the repo's
    own is whatever the author checked in.
    """
    base = Path(name).name
    for paths in (g["doc"].get("files") or {}).values():
        for p in (paths if isinstance(paths, list) else [paths]):
            if Path(p).name.lower() == base.lower():
                return p
    return base


def stage_files_into_doc(doc: dict, files: list[dict]
                         ) -> tuple[dict[str, bytes], list[tuple[str, str]]]:
    """Bytes -> blobs to commit, and the `[files]` roles that describe them.

    Shared by `upload_files` and by `checkin` (§1), which is the whole reason it
    is a function: the two used to differ in where a file landed and what role
    it was given, and "the same file checked in two ways" is not allowed to mean
    two different trees.

    Returns (path -> bytes, [(path, role)]) and mutates `doc["files"]` /
    `doc["primary"]` in place.
    """
    from . import gitvalidate as gv, manifest as mf
    cls = classify_inputs(files)
    changes: dict[str, bytes] = {}
    named: list[tuple[str, str]] = []
    for f in files:
        size = _entry_size(f)
        # stage 17: a file the LFS policy names is stored as an LFS object
        # and committed as its pointer (gitwrite._lfs_changes), so its cap is
        # the object store's, not git's
        lfs_file = _lfs.wants_lfs(f["name"], size)
        cap = _lfs.config.LFS_MAX_OBJECT if lfs_file else gv.MAX_FILE_BYTES
        if size > cap:
            raise OpsError(
                f"{f['name']} is {size:,} bytes, over the {cap:,} byte "
                f"per-file limit for {'an LFS object' if lfs_file else 'a plain git blob'}.\n"
                + ("PLM_LFS_MAX_OBJECT_BYTES moves it, if this is genuinely one "
                   "file." if lfs_file else
                   "**git is the wrong transport above this size.** Commit the "
                   "`[source]` that generates it, or ship it as a binary "
                   "(non-text) file, which plm keeps in Git LFS "
                   "(docs/LARGE-FILES.md)."), 413)
    _fill_vault_bytes(files)          # staged/local handles -> real bytes
    for f in files:
        data = f.get("data")
        if data is None:
            raise OpsError(f"no bytes for {f.get('name')!r}")
        path = _tree_path_for({"doc": doc}, f["name"])
        changes[path] = data
        named.append((path, (cls.get(Path(f['name']).name) or {}).get("role")
                      or f.get("role") or "artifact"))

    files_tbl = {r: list(v) for r, v in (doc.get("files") or {}).items()}
    for path, role in named:
        for r in list(files_tbl):
            files_tbl[r] = [p for p in files_tbl[r] if p != path]
            if not files_tbl[r]:
                files_tbl.pop(r)
        files_tbl[role] = sorted(set(files_tbl.get(role, []) + [path]))
    doc["files"] = dict(sorted(files_tbl.items()))
    # `primary` is the geometry the viewer, the thumbnail and the 3-D diff use.
    # A clone cannot re-derive plm's preference, so it is written down — but
    # only when the tree did not already name one: overruling somebody's choice
    # because they uploaded a mesh would be plm deciding what the part looks like.
    if not doc.get("primary"):
        doc["primary"] = mf.primary_file(doc["files"],
                                         doc.get("files_external") or [])
    return changes, named


def _upload_files_git(conn: sqlite3.Connection, item, rev: dict, g: dict,
                      files: list[dict], *, actor: str = "",
                      message: str = "") -> dict:
    """upload_files, as a commit. The bytes are blobs in this revision's tree.

    NO SUPERSESSION HERE, and that is the point rather than an omission: the
    carry-forward rule (`supersede_inherited`) exists because a revise
    COPIED file rows and version-stamped names defeated basename replacement.
    In a repo a revision inherits by BEING the next commit on the same tree, so
    there is nothing carried forward to retire — a file is replaced by writing
    to its path, which is what git has always meant by a change.
    """
    changes, named = stage_files_into_doc(g["doc"], files)
    res = git_write(
        conn, g, rev, actor=actor, changes=changes,
        message=(message or (f"{item['number']} v{rev['version']}: "
                             + ", ".join(p for p, _ in named))))
    _consume_inputs(conn, files, actor=actor)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM files WHERE revision_id=? ORDER BY path", (rev["id"],))]
    for r in rows:
        r["url"] = f"/api/blob?id={r['id']}"
    db.add_event(conn, "files.update", item_id=item["id"],
                 revision_id=rev["id"], actor=actor,
                 data={"files": len(named), "wip": True, "offloaded": 0,
                       "superseded": 0, "git": res})
    return {"number": item["number"], "version": rev["version"],
            "git_commit": res.get("commit") or "",
            "files": [r for r in rows if r["path"] in changes],
            "superseded": [], "git": res}


def _upload_rows(conn: sqlite3.Connection, item, rev, folder: str,
                 file_rows: list[dict], offload: list[dict], *,
                 derived: bool, actor: str, commit_sha: str,
                 had_folder: bool, wrote_vault: bool,
                 cls: dict | None = None) -> list[dict]:
    """The database half of upload_files: files rows, viz cleanup, supersession
    and the event. Split out so the caller can wrap exactly this in one
    transaction, with the slow vault work strictly outside it."""
    if not had_folder and wrote_vault:
        conn.execute("UPDATE items SET folder=? WHERE id=?", (folder, item["id"]))
    for fr in file_rows:
        name = fr["path"].rsplit("/", 1)[-1]
        # Replace any prior row for this filename — matched by basename, so a
        # replacement lands correctly even when the old row lived on another
        # backend (its `path` is then a display path, not a vault path). The
        # superseded bytes are left where they are: vault copies stay in git
        # history, offloaded objects keep their immutable key.
        conn.execute(
            "DELETE FROM files WHERE revision_id=? AND (path=? OR path LIKE ?)",
            (rev["id"], fr["path"], f"%/{name}"))
        _put_row(conn, rev["id"], fr, cls)
    if derived:
        # A viz derivative carried forward by revise() describes the PREVIOUS
        # revision's geometry — its name and vault folder both say so. Once
        # this revision has derived viz of its own, those inherited rows are
        # stale and must go, or _pick_stl/the viewer can show the old shape.
        # Only rows OUTSIDE this revision's own version folder are dropped, so
        # a second viz attached to this revision (exploded beside assembled)
        # survives.
        conn.execute(
            "DELETE FROM files WHERE revision_id=? AND role='viz' AND path NOT LIKE ?",
            (rev["id"], f"{folder}/versions/v{rev['version']}/%"))
    # the same-basename replacement above only fires when the new file
    # is NAMED like the one it replaces, and this vault's exported filenames
    # carry their version. So the general rule runs here too: a file of this
    # revision's own retires the older generation's row it stands in for.
    superseded = supersede_inherited(
        conn, item_id=item["id"], number=item["number"], folder=folder,
        rev_id=rev["id"], version=rev["version"], actor=actor) if file_rows else []
    if commit_sha:
        conn.execute("UPDATE revisions SET git_commit=? WHERE id=?",
                     (commit_sha, rev["id"]))
    db.add_event(conn, "files.update", item_id=item["id"], revision_id=rev["id"],
                 actor=actor, data={"files": len(file_rows), "wip": True,
                                    "offloaded": len(offload),
                                    "superseded": len(superseded)})
    return superseded


def _draft_revision(conn: sqlite3.Connection, number: str,
                    version: str = "") -> tuple[sqlite3.Row, dict]:
    """The mutable revision to edit, or a 409 explaining what to do instead.
    Structure (BOM) is design data: it may only change on a DRAFT."""
    item = _item(conn, number)
    rev = db.find_revision(conn, number, version)
    if not rev:
        raise OpsError(f"no revision for {number} {version or '(latest)'}", 404)
    if rev["state"] != "DRAFT":
        raise OpsError(
            f"{number} v{rev['version']} is {rev['state']} and frozen — "
            "Revise ↑ first (new DRAFT), then edit its BOM", 409)
    return item, rev


# == EDITING A DRAFT's PRODUCT STRUCTURE ============================
#
# A DRAFT is a WORKING revision. Iterating on its BOM — adding the four
# weldment children you just decided on, fixing a quantity, swapping a child for
# the part that replaced it, deleting a line that was a mistake — is ordinary
# design work and must not require revising, freezing or re-forking anything.
# The five verbs, and the distinctions between them are the data model:
#
#   add_bom_line      a new occurrence.
#   update_bom_line   the occurrence stays, its NUMBERS change: qty, unit,
#                     find_number, notes. Not the child — see replace.
#   replace_bom_line  the same POSITION in the assembly is now filled by a
#                     different child, and/or bound to a different revision of
#                     it. find number, qty and notes survive, because the
#                     position and how many of them are properties of the
#                     assembly, not of the part that happens to sit there.
#   remove_bom_line   THIS WAS NEVER PART OF IT. The line goes; history keeps
#                     it only in the event log.
#   suppress (a flag) THIS IS PART OF IT, AND ABSENT IN THIS CONFIGURATION.
#
# SUPPRESSION IS NOT REMOVAL, and confusing them is how a 150% BOM turns into a
# lie. A suppressed line is a real occurrence of the product that this
# configuration does not include — the mounting boss you print only for the
# tall variant, the bracket the fork inherited and does not use. It stays in the
# record, it stays visible, and rollups/build plans/the 3D view skip it
# (db.rolled_bom, thumbs.geometry_bodies). Removal says something different and
# stronger: this occurrence was a mistake, it is not part of the product in ANY
# configuration, and nothing downstream should ever have to reason about it.
# Pick by which sentence is true; they are not interchangeable and plm will not
# guess.
#
# All five are DRAFT-only (structure is design data), all five refuse
# non-structural children (ops.NOT_IN_BOM — coupons relate, assets are what you
# mount to), all five are evented, and none of them touch the parent's version.
#
# NOTHING ELSE NEEDS INVALIDATING. Everything downstream of a BOM is derived at
# READ time or content-addressed: rolled_bom and _build_plan walk the live rows;
# thumbnails key on the sha/size/mtime of the STLs that geometry_bodies collects
# through the BOM, so a structure change changes the key and re-renders by
# itself; the FTS index does not carry BOM content at all. There is no cache to
# bust, which is a property worth keeping.

def _bom_line_row(conn: sqlite3.Connection, line_id: int):
    return conn.execute(
        """SELECT b.*, i.number AS parent_number, i.id AS parent_id,
                  r.id AS rev_id, r.version AS parent_version,
                  r.state AS parent_state, c.number AS child_number,
                  c.name AS child_name, c.kind AS child_kind
           FROM bom b JOIN revisions r ON r.id=b.parent_rev_id
                      JOIN items i ON i.id=r.item_id
                      JOIN items c ON c.id=b.child_item_id
           WHERE b.id=?""", (line_id,)).fetchone()


def resolve_bom_line(conn: sqlite3.Connection, *, line_id: int = 0,
                     parent: str | int = "", child: str = "",
                     find: int | None = None, version: str = ""):
    """ONE addressing rule for a BOM line, shared by the API and the MCP.

    `line_id` when you have it (get_bom returns every line's `id`). Otherwise
    name the parent and the child — or the parent and a find number — and plm
    finds the line on the parent's DRAFT (or the revision `version` names). An
    ambiguous reference is refused with the candidates listed rather than
    resolved by guessing, because guessing which of two occurrences you meant is
    how the wrong one gets deleted."""
    if line_id:
        row = _bom_line_row(conn, line_id)
        if not row:
            raise OpsError(f"no BOM line {line_id}", 404)
        return row
    if not parent:
        raise OpsError("say which line: pass line_id, or parent plus child "
                       "(or parent plus find)")
    item = _item(conn, parent)
    rev = db.find_revision(conn, item["id"], version)
    if not rev:
        raise OpsError(f"no revision for {item['number']} "
                       f"{version or '(latest)'}", 404)
    where, args = ["b.parent_rev_id=?"], [rev["id"]]
    if child:
        kid = db.item_by_number(conn, child)
        if not kid:
            raise OpsError(f"unknown child item: {child}", 404)
        where.append("b.child_item_id=?")
        args.append(kid["id"])
    if find is not None:
        where.append("b.find_number=?")
        args.append(int(find))
    rows = conn.execute(
        f"SELECT b.id FROM bom b WHERE {' AND '.join(where)} "
        "ORDER BY b.find_number", args).fetchall()
    if not rows:
        have = ", ".join(
            f"{l['child_number']}@{l['find_number']}"
            for l in db.get_bom(conn, rev["id"])) or "(empty BOM)"
        raise OpsError(
            f"no BOM line on {item['number']} v{rev['version']} matching "
            f"{child or ''}{'@' + str(find) if find is not None else ''}. "
            f"It has: {have}", 404)
    if len(rows) > 1:
        lines = [_bom_line_row(conn, r["id"]) for r in rows]
        which = ", ".join(f"line {l['id']} = {l['child_number']} "
                          f"@find {l['find_number']} (qty {l['qty']:g})"
                          for l in lines)
        raise OpsError(
            f"{len(rows)} occurrences match on {item['number']} "
            f"v{rev['version']} — pass line_id or find to say which: {which}",
            409)
    return _bom_line_row(conn, rows[0]["id"])


def _editable_line(conn: sqlite3.Connection, line, verb: str):
    """A BOM line you may change, or the 409 that says what to do instead.

    Same law as _draft_revision, stated at the LINE: released structure is a
    record of what was approved. The refusal names both alternatives, because
    the right one depends on which sentence is true (see the block comment
    above) and only the caller knows that."""
    if line["parent_state"] != "DRAFT":
        raise OpsError(
            f"{line['parent_number']} v{line['parent_version']} is "
            f"{line['parent_state']} and frozen, so its structure cannot be "
            f"{verb}. Either Revise ↑ first — which is what you want if the "
            "product changed, because the change then has a revision of its own "
            "to be approved with — or, if this occurrence is simply ABSENT in "
            "the configuration you are describing, suppress the line: it stays "
            "in the record and drops out of rollups, plans and the 3D view.",
            409)
    return line


def _find_is_free(conn: sqlite3.Connection, rev_id: int, find: int,
                  except_line: int) -> None:
    """A find number is a POSITION in the assembly, so at most one occurrence may
    hold it. Verified across the production registry before this was enforced:
    zero collisions, i.e. the convention was already the rule and nothing was
    relying on it being loose. Two of the same part is a QUANTITY, not two lines
    sharing a position."""
    row = conn.execute(
        """SELECT b.id, c.number FROM bom b JOIN items c ON c.id=b.child_item_id
           WHERE b.parent_rev_id=? AND b.find_number=? AND b.id!=?""",
        (rev_id, find, except_line)).fetchone()
    if row:
        raise OpsError(
            f"find number {find} is already {row['number']} on this revision "
            f"(line {row['id']}). A find number is a position in the assembly, "
            f"so it holds one occurrence: pick another find number, or — if you "
            f"meant more of the same part — that is a quantity "
            f"(update_bom_line(line_id={row['id']}, qty=…)).", 409)


@_explodes
def add_bom_line(conn: sqlite3.Connection, number: str, child: str, *,
                 version: str = "", child_version: str = "", qty: float = 1,
                 unit: str = "EA", find: int | None = None, notes: str = "",
                 actor: str = "") -> dict:
    """Add one occurrence to a DRAFT revision's BOM. child_version empty = an
    imprecise (resolve-to-latest) binding. find defaults to the next free
    decade, keeping the hand-maintained 10/20/30 find-number convention.

    A retry-safe default: with an AUTO find number, adding a child
    that is already on this BOM at the same binding is refused rather than
    silently creating a second line, because "next free decade" is the one
    non-deterministic thing about this call and a duplicated occurrence is the
    one outcome nobody wants. Two genuine occurrences of the same child are
    still legal — pass the find number you mean."""
    item, rev = _draft_revision(conn, number, version)
    kid = db.item_by_number(conn, child)
    if not kid:
        raise OpsError(f"unknown child item: {child} — create it first", 404)
    _refuse_nonstructural_child(conn, child)
    if kid["id"] == item["id"]:
        raise OpsError(f"{number} cannot contain itself")
    if child_version and not db.find_revision(conn, child, child_version):
        raise OpsError(f"no revision {child_version} on {child}", 404)
    if find is None:
        # The retry guard. Deliberately keyed on the CHILD alone, not on
        # (child, binding): a second line for a child already on this BOM is
        # overwhelmingly a duplicate — a retried call, or an agent that did not
        # read the BOM first — and a differently-bound second occurrence is
        # exactly the shape a retry takes when the caller left child_version off
        # the second time. Refusing needs a reason to be undone, so the refusal
        # carries all three ways forward.
        dupe = conn.execute(
            "SELECT id, find_number, qty, child_version FROM bom "
            "WHERE parent_rev_id=? AND child_item_id=? ORDER BY find_number",
            (rev["id"], kid["id"])).fetchone()
        if dupe:
            bound = (f"v{dupe['child_version']}" if dupe["child_version"]
                     else "imprecise")
            raise OpsError(
                f"{child} is already on {number} v{rev['version']}'s BOM at find "
                f"number {dupe['find_number']} ({bound}, qty {dupe['qty']:g}, "
                f"line {dupe['id']}). If this call is a retry, it already "
                f"succeeded — nothing more to do. If you want MORE of them, that "
                f"is a quantity: update_bom_line(line_id={dupe['id']}, qty=…). "
                f"If you want it bound to a different revision, that is "
                f"replace_bom_line(line_id={dupe['id']}, child_version=…). Only "
                f"if you genuinely mean a SECOND, distinct occurrence at another "
                f"position should you pass an explicit find number.", 409)
        top = conn.execute("SELECT MAX(find_number) AS m FROM bom WHERE parent_rev_id=?",
                           (rev["id"],)).fetchone()["m"] or 0
        find = int(top) + 10
    _find_is_free(conn, rev["id"], int(find), 0)

    # GIT UNDER THE HOOD: on a revision that lives in a repo, an occurrence is a
    # `[[bom]]` line AND the submodule gitlink that pins it (§5.1) — written in
    # one commit, because either alone is a BOM line that resolves to nothing.
    g = git_draft(conn, rev)
    if g:
        path, link, sha, why = bom_pin(conn, child, kid["kind"], child_version)
        if _bom_entry(g["doc"], path):
            raise OpsError(f"{path} is already a line on {number} "
                           f"v{rev['version']}'s BOM", 409)
        entry: dict = {}
        if find is not None:
            entry["find"] = int(find)
        entry["path"] = path
        entry["qty"] = float(qty)
        entry["unit"] = unit or "EA"
        if notes:
            entry["notes"] = notes
        if not child_version:
            entry["track"] = "release"
        g["doc"]["bom"] = sorted((g["doc"].get("bom") or []) + [entry],
                                 key=_bom_key)
        res = git_write(
            conn, g, rev, actor=actor, links={link: sha},
            message=(f"Add {child} to {number} v{rev['version']} BOM "
                     f"(find {find}, qty {qty:g} {unit})"
                     + f"\n\nPinned at {why}.\n"))
        db.add_event(conn, "bom.add", item_id=item["id"], revision_id=rev["id"],
                     actor=actor, data={"child": child, "version": child_version,
                                        "qty": qty, "find": find, "git": res})
        return _bom_line_after(conn, rev["id"], kid["id"], item["number"],
                               rev["version"], res)

    cur = conn.execute(
        """INSERT INTO bom (parent_rev_id,find_number,child_item_id,child_version,
                            qty,unit,notes) VALUES (?,?,?,?,?,?,?)""",
        (rev["id"], find, kid["id"], child_version, qty, unit, notes))
    db.add_event(conn, "bom.add", item_id=item["id"], revision_id=rev["id"],
                 actor=actor, data={"child": child, "version": child_version,
                                    "qty": qty, "find": find})
    return {"number": item["number"], "version": rev["version"],
            "line": cur.lastrowid, "find": find, "child": child,
            "child_version": child_version, "qty": qty, "unit": unit,
            "suppressed": False}


UPDATABLE = ("qty", "unit", "find_number", "notes")


@_explodes
def update_bom_line(conn: sqlite3.Connection, line_id: int = 0, *,
                    parent: str | int = "", child: str = "",
                    version: str = "", qty: float | None = None,
                    unit: str | None = None, find: int | None = None,
                    notes: str | None = None, actor: str = "") -> dict:
    """Change one occurrence's NUMBERS on a DRAFT: qty, unit, find_number, notes.

    The occurrence itself is unchanged — the same child, in the same assembly.
    This is the verb for "four of these, not two" and for renumbering a line
    into the right position in the 10/20/30 sequence, which is the everyday edit
    a DRAFT existed to allow and which previously required revising or
    re-forking.

    It is NOT how you change the child or its pinned version: that is
    replace_bom_line, and the difference is worth keeping because the two
    changes mean different things to anybody reading the event log. It is also
    NOT suppression — see the block comment above: suppression is a statement
    about a CONFIGURATION, this is a statement about a quantity.

    Every field is optional; omitted ones are left alone (passing qty=0 is a
    real quantity of zero, not 'unset'). Idempotent by construction: the same
    call twice writes the same values, and a call that changes nothing says so
    instead of inventing an event."""
    line = _editable_line(
        conn, resolve_bom_line(conn, line_id=line_id, parent=parent,
                               child=child, version=version), "edited")
    sets: dict[str, object] = {}
    if qty is not None:
        if float(qty) < 0:
            raise OpsError("qty cannot be negative — a BOM line is a count of "
                           "occurrences. To take the line out, remove it "
                           "(never part of it) or suppress it (absent in this "
                           "configuration).")
        sets["qty"] = float(qty)
    if unit is not None:
        if not str(unit).strip():
            raise OpsError("unit is empty — pass e.g. EA, mm, g")
        sets["unit"] = str(unit).strip()
    if notes is not None:
        sets["notes"] = str(notes)
    if find is not None:
        find = int(find)
        _find_is_free(conn, line["parent_rev_id"], find, line["id"])
        sets["find_number"] = find
    if not sets:
        raise OpsError("nothing to update — pass at least one of qty, unit, "
                       "find, notes. (To change the CHILD or its pinned "
                       "version, use replace_bom_line; to mark the occurrence "
                       "absent in this configuration, suppress it.)")
    before = {k: line[k] for k in UPDATABLE}
    changed = {k: v for k, v in sets.items() if before[k] != v}
    if not changed:
        return {"number": line["parent_number"], "version": line["parent_version"],
                "line": line["id"], "child": line["child_number"],
                "changed": {}, **before,
                "note": "already exactly that — nothing written."}

    # The numbers on a line are the manifest's half of an occurrence; the pin
    # does not move, so this is a `plm.toml` edit and nothing else.
    rev = _rev_for_git(conn, line["parent_rev_id"])
    g = git_draft(conn, rev) if rev else {}
    if g:
        from . import manifest as mf
        path = mf.bom_path(line["child_number"], line["child_kind"])
        entry = _bom_entry(g["doc"], path)
        if entry is None:
            raise OpsError(
                f"{line['parent_number']} v{line['parent_version']}'s plm.toml "
                f"has no `[[bom]]` line at {path!r}, so there is nothing to "
                "edit. Re-index the revision and try again.", 409)
        for k, v in changed.items():
            if k == "find_number":
                entry["find"] = int(v)
            elif k == "qty":
                entry["qty"] = float(v)
            elif k == "notes":
                entry["notes"] = str(v)
                if not v:
                    entry.pop("notes")
            else:
                entry[k] = v
        g["doc"]["bom"] = sorted(g["doc"]["bom"], key=_bom_key)
        res = git_write(
            conn, g, rev, actor=actor,
            message=(f"{line['parent_number']} v{line['parent_version']}: "
                     + ", ".join(f"{k} {before[k]} -> {v}"
                                 for k, v in changed.items())
                     + f" on {line['child_number']}"))
        db.add_event(conn, "bom.update", item_id=line["parent_id"],
                     revision_id=line["rev_id"], actor=actor,
                     data={"line": line["id"], "child": line["child_number"],
                           "from": {k: before[k] for k in changed},
                           "to": changed, "git": res})
        out = _bom_line_after(conn, line["parent_rev_id"],
                              line["child_item_id"], line["parent_number"],
                              line["parent_version"], res)
        return {**out, "changed": changed}

    conn.execute(
        f"UPDATE bom SET {', '.join(f'{k}=?' for k in changed)} WHERE id=?",
        (*changed.values(), line["id"]))
    db.add_event(conn, "bom.update", item_id=line["parent_id"],
                 revision_id=line["rev_id"], actor=actor,
                 data={"line": line["id"], "child": line["child_number"],
                       "from": {k: before[k] for k in changed},
                       "to": changed})
    after = {**before, **changed}
    return {"number": line["parent_number"], "version": line["parent_version"],
            "line": line["id"], "child": line["child_number"],
            "child_version": line["child_version"], "changed": changed,
            "find": after["find_number"], "qty": after["qty"],
            "unit": after["unit"], "notes": after["notes"]}


@_explodes
def replace_bom_line(conn: sqlite3.Connection, line_id: int = 0, *,
                     parent: str | int = "", child: str = "",
                     version: str = "", new_child: str = "",
                     child_version: str | None = None, qty: float | None = None,
                     actor: str = "") -> dict:
    """Swap what fills one position in a DRAFT's BOM: the child, its pinned
    revision, or both.

    "The weldment we designed replaces the purchased angle bracket" and "pin
    this occurrence to v1.2.0 now that it is released" are the same edit — the
    assembly's structure is unchanged and one line's binding moves — so they are
    one verb. find_number, unit and notes are deliberately preserved: the
    position in the assembly and the note explaining it belong to the parent's
    design, not to whichever part currently sits there. qty is preserved too
    unless you say otherwise.

    child_version semantics, which are the fiddly part and therefore explicit:
        None (default) — leave the binding alone.
        "1.2.0"        — PRECISE: pin this occurrence to that revision.
        ""             — IMPRECISE: resolve to the child's latest at read time
                         (the plm default, and what propagate() exists to
                         rebind when it is not what you want).

    Same guards as add: DRAFT only, no coupons or assets (they relate / are
    mounted to, they are not occurrences), no self-containment, the named
    revision must exist."""
    line = _editable_line(
        conn, resolve_bom_line(conn, line_id=line_id, parent=parent,
                               child=child, version=version), "replaced")
    if not new_child and child_version is None:
        raise OpsError("nothing to replace — pass new_child (a different child "
                       "item) and/or child_version (the revision to bind: a "
                       "version to pin it, '' to make it imprecise)")
    kid = None
    if new_child:
        kid = db.item_by_number(conn, new_child)
        if not kid:
            raise OpsError(f"unknown child item: {new_child} — create it first",
                           404)
        _refuse_nonstructural_child(conn, new_child)
        if kid["id"] == line["parent_id"]:
            raise OpsError(f"{line['parent_number']} cannot contain itself")
    new_number = kid["number"] if kid else line["child_number"]
    new_id = kid["id"] if kid else line["child_item_id"]
    new_ver = line["child_version"] if child_version is None else str(child_version)
    if new_ver and not db.find_revision(conn, new_number, new_ver):
        raise OpsError(f"no revision {new_ver} on {new_number}", 404)
    if new_id == line["child_item_id"] and new_ver == line["child_version"] \
            and qty is None:
        return {"number": line["parent_number"], "version": line["parent_version"],
                "line": line["id"], "child": new_number, "child_version": new_ver,
                "changed": False,
                "note": "already exactly that binding — nothing written."}
    dupe = conn.execute(
        "SELECT id FROM bom WHERE parent_rev_id=? AND child_item_id=? AND "
        "child_version=? AND id!=?",
        (line["parent_rev_id"], new_id, new_ver, line["id"])).fetchone()
    if dupe:
        raise OpsError(
            f"{new_number} {'v' + new_ver if new_ver else '(imprecise)'} is "
            f"already on {line['parent_number']} v{line['parent_version']} "
            f"(line {dupe['id']}) — replacing this line with it would leave two "
            "identical occurrences, which is a quantity rather than a "
            f"structure. Update line {dupe['id']}'s qty and remove this one.",
            409)
    new_qty = line["qty"] if qty is None else float(qty)

    # Replacing a line moves the GITLINK, which is the pin (§5.1): the old
    # child's submodule comes out of the tree and the new one goes in, pinned at
    # the revision this line now binds — one commit, one occurrence.
    rev = _rev_for_git(conn, line["parent_rev_id"])
    g = git_draft(conn, rev) if rev else {}
    if g:
        from . import manifest as mf
        old_path = mf.bom_path(line["child_number"], line["child_kind"])
        entry = _bom_entry(g["doc"], old_path)
        if entry is None:
            raise OpsError(
                f"{line['parent_number']} v{line['parent_version']}'s plm.toml "
                f"has no `[[bom]]` line at {old_path!r}.", 409)
        new_kind = (kid["kind"] if kid else line["child_kind"])
        path, link, sha, why = bom_pin(conn, new_number, new_kind, new_ver)
        entry["path"] = path
        entry["qty"] = new_qty
        if new_ver:
            entry.pop("track", None)
        else:
            entry["track"] = "release"
        g["doc"]["bom"] = sorted(g["doc"]["bom"], key=_bom_key)
        links = {link: sha}
        if old_path != path and old_path.startswith("parts/"):
            links[old_path] = None
        res = git_write(
            conn, g, rev, actor=actor, links=links,
            message=(f"{line['parent_number']} v{line['parent_version']} find "
                     f"{line['find_number']}: {line['child_number']} -> "
                     f"{new_number} {'v' + new_ver if new_ver else '(imprecise)'}"
                     + f"\n\nPinned at {why}.\n"))
        db.add_event(conn, "bom.replace", item_id=line["parent_id"],
                     revision_id=line["rev_id"], actor=actor,
                     data={"line": line["id"], "find": line["find_number"],
                           "from": {"child": line["child_number"],
                                    "version": line["child_version"],
                                    "qty": line["qty"]},
                           "to": {"child": new_number, "version": new_ver,
                                  "qty": new_qty}, "git": res})
        out = _bom_line_after(conn, line["parent_rev_id"], new_id,
                              line["parent_number"], line["parent_version"],
                              res)
        return {**out, "was": line["child_number"],
                "was_version": line["child_version"], "changed": True,
                "note": (f"find {line['find_number']} on "
                         f"{line['parent_number']} v{line['parent_version']} "
                         f"now binds {new_number} "
                         f"{'v' + new_ver if new_ver else '(imprecise → latest)'}"
                         f", pinned at {why}. Position, unit and notes were "
                         "kept — they describe the assembly, not the part that "
                         "sits there.")}

    conn.execute("UPDATE bom SET child_item_id=?, child_version=?, qty=? WHERE id=?",
                 (new_id, new_ver, new_qty, line["id"]))
    db.add_event(conn, "bom.replace", item_id=line["parent_id"],
                 revision_id=line["rev_id"], actor=actor,
                 data={"line": line["id"], "find": line["find_number"],
                       "from": {"child": line["child_number"],
                                "version": line["child_version"],
                                "qty": line["qty"]},
                       "to": {"child": new_number, "version": new_ver,
                              "qty": new_qty}})
    return {"number": line["parent_number"], "version": line["parent_version"],
            "line": line["id"], "find": line["find_number"],
            "was": line["child_number"], "was_version": line["child_version"],
            "child": new_number, "child_version": new_ver, "qty": new_qty,
            "unit": line["unit"], "changed": True,
            "note": (f"find {line['find_number']} on {line['parent_number']} "
                     f"v{line['parent_version']} now binds {new_number} "
                     f"{'v' + new_ver if new_ver else '(imprecise → latest)'}. "
                     "Position, unit and notes were kept — they describe the "
                     "assembly, not the part that sits there.")}


@_explodes
def suppress_bom_line(conn: sqlite3.Connection, line_id: int = 0, *,
                      parent: str | int = "", child: str = "",
                      version: str = "", suppressed: bool = True,
                      actor: str = "") -> dict:
    """Mark one occurrence ABSENT IN THIS CONFIGURATION (150% BOM), or restore it.

    The line stays in the record: rolled quantities, build plans and the 3D view
    skip it while suppressed, and every read still shows it, flagged. That is
    the whole difference from remove_bom_line, which says the occurrence was
    never part of the product at all.

    Unlike the other four verbs this is allowed on a FROZEN revision on purpose:
    a suppression does not change what the product IS, it records which variant
    you are describing — the Teamcenter variant-condition posture. It is the
    answer the frozen-structure refusals point at."""
    line = resolve_bom_line(conn, line_id=line_id, parent=parent, child=child,
                            version=version)
    want = 1 if suppressed else 0
    if (line["suppressed"] or 0) == want:
        return {"number": line["parent_number"], "version": line["parent_version"],
                "line": line["id"], "child": line["child_number"],
                "suppressed": bool(want), "changed": False}

    # `require=False`: this is the ONE structural verb that is legal on a frozen
    # revision, and a released revision's tree is behind an immutable tag. So a
    # suppression on a DRAFT is a commit like everything else, and a suppression
    # on a FROZEN git-origin revision is a NOTE (§5) — effectivity, not design,
    # which is what stage 3b's open question 2 recommended and stage 3c decided.
    rev = _rev_for_git(conn, line["parent_rev_id"])
    fz = frozen_note(conn, rev) if rev else {}
    if fz:
        def mutate(note: dict) -> None:
            rest = [s for s in note["suppress"]
                    if str(s.get("child")) != line["child_number"]
                    or (s.get("find") or None) != (line["find_number"] or None)]
            if want:
                row: dict = {"child": line["child_number"]}
                if line["find_number"] not in (None, ""):
                    row["find"] = line["find_number"]
                rest.append(row)
            note["suppress"] = rest

        res = note_write(
            conn, fz, rev, mutate, actor=actor,
            message=(f"{'Suppress' if want else 'Restore'} "
                     f"{line['child_number']} on {line['parent_number']} "
                     f"v{line['parent_version']}\n\n"
                     "Absent in this configuration (150% BOM) — effectivity, "
                     "not design. The revision\nis frozen behind an immutable "
                     "tag, so this is a note on the release commit\n(docs §5); "
                     "the tree and the tag are untouched.\n"))
        db.add_event(conn, "bom.suppress" if suppressed else "bom.unsuppress",
                     item_id=line["parent_id"], revision_id=line["rev_id"],
                     actor=actor, data={"line": line["id"],
                                        "child": line["child_number"],
                                        "child_item_id": line["child_item_id"],
                                        "git": res})
        return {"number": line["parent_number"],
                "version": line["parent_version"], "line": line["id"],
                "child": line["child_number"], "suppressed": bool(want),
                "changed": True, "git": res}
    g = git_draft(conn, rev, require=False) if rev else {}
    if g:
        from . import manifest as mf
        path = mf.bom_path(line["child_number"], line["child_kind"])
        entry = _bom_entry(g["doc"], path)
        if entry is None:
            raise OpsError(
                f"{line['parent_number']} v{line['parent_version']}'s plm.toml "
                f"has no `[[bom]]` line at {path!r}.", 409)
        if want:
            entry["suppressed"] = True
        else:
            entry.pop("suppressed", None)
        res = git_write(
            conn, g, rev, actor=actor,
            message=(f"{'Suppress' if want else 'Restore'} "
                     f"{line['child_number']} on {line['parent_number']} "
                     f"v{line['parent_version']}"
                     + ("\n\nAbsent in this configuration (150% BOM): the "
                        "occurrence stays in the record and\ndrops out of "
                        "rollups, build plans and the 3-D view.\n" if want
                        else "\n\nBack in this configuration.\n")))
        db.add_event(conn, "bom.suppress" if suppressed else "bom.unsuppress",
                     item_id=line["parent_id"], revision_id=line["rev_id"],
                     actor=actor, data={"line": line["id"],
                                        "child": line["child_number"],
                                        "child_item_id": line["child_item_id"],
                                        "git": res})
        out = _bom_line_after(conn, line["parent_rev_id"],
                              line["child_item_id"], line["parent_number"],
                              line["parent_version"], res)
        return {**out, "changed": True}

    conn.execute("UPDATE bom SET suppressed=? WHERE id=?", (want, line["id"]))
    db.add_event(conn, "bom.suppress" if suppressed else "bom.unsuppress",
                 item_id=line["parent_id"], revision_id=line["rev_id"],
                 actor=actor, data={"line": line["id"],
                                    "child": line["child_number"],
                                    "child_item_id": line["child_item_id"]})
    return {"number": line["parent_number"], "version": line["parent_version"],
            "line": line["id"], "child": line["child_number"],
            "suppressed": bool(want), "changed": True}


@_explodes
def remove_bom_line(conn: sqlite3.Connection, line_id: int = 0, *,
                    parent: str | int = "", child: str = "",
                    version: str = "", actor: str = "") -> dict:
    """Take one occurrence OFF a DRAFT revision's BOM: this was never part of it.

    Use this for a line that was a mistake — the wrong child, a duplicate, an
    occurrence that belongs on a different sub-assembly. Do NOT use it for a
    part that IS part of the product but absent in the configuration you are
    describing: that is suppression, which keeps the record (see the block
    comment above). Released structure is immutable, so on a frozen revision
    this refuses and points at both alternatives."""
    line = _editable_line(
        conn, resolve_bom_line(conn, line_id=line_id, parent=parent,
                               child=child, version=version), "removed from")

    # The line AND its gitlink AND its `.gitmodules` stanza: an occurrence that
    # was never part of the product leaves nothing behind that a
    # `--recurse-submodules` clone would still fetch.
    rev = _rev_for_git(conn, line["parent_rev_id"])
    g = git_draft(conn, rev) if rev else {}
    if g:
        from . import manifest as mf
        path = mf.bom_path(line["child_number"], line["child_kind"])
        entry = _bom_entry(g["doc"], path)
        if entry is None:
            raise OpsError(
                f"{line['parent_number']} v{line['parent_version']}'s plm.toml "
                f"has no `[[bom]]` line at {path!r}.", 409)
        g["doc"]["bom"] = [b for b in g["doc"]["bom"] if b is not entry]
        links = {path: None} if path.startswith("parts/") else {}
        if not links and not [b for b in g["doc"]["bom"]
                              if str(b.get("path") or "").startswith("catalog/")]:
            links = {"catalog": None}   # the last purchased line went with it
        res = git_write(
            conn, g, rev, actor=actor, links=links or None,
            message=(f"Remove {line['child_number']} from "
                     f"{line['parent_number']} v{line['parent_version']} BOM "
                     f"(find {line['find_number']})"
                     "\n\nNever part of it — not suppressed, which would have "
                     "kept the occurrence in the\nrecord as absent from this "
                     "configuration.\n"))
        db.add_event(conn, "bom.remove", item_id=line["parent_id"],
                     revision_id=line["parent_rev_id"], actor=actor,
                     data={"child": line["child_number"],
                           "find": line["find_number"], "qty": line["qty"],
                           "child_version": line["child_version"],
                           "suppressed": bool(line["suppressed"]), "git": res})
        return {"number": line["parent_number"],
                "version": line["parent_version"], "line": line["id"],
                "removed": line["child_number"], "find": line["find_number"],
                "qty": line["qty"], "git": res}

    conn.execute("DELETE FROM bom WHERE id=?", (line["id"],))
    db.add_event(conn, "bom.remove", item_id=line["parent_id"],
                 revision_id=line["parent_rev_id"], actor=actor,
                 data={"child": line["child_number"], "find": line["find_number"],
                       "qty": line["qty"], "child_version": line["child_version"],
                       "suppressed": bool(line["suppressed"])})
    return {"number": line["parent_number"], "version": line["parent_version"],
            "line": line["id"], "removed": line["child_number"],
            "find": line["find_number"], "qty": line["qty"]}


def rename_item(conn: sqlite3.Connection, number: str, name: str, *,
                actor: str = "") -> dict:
    """Set an item's descriptive name. Identity (the number) never changes;
    the name is item-level master data, not revision data, so this is not a
    revision-controlled edit — but it IS logged, and the old name is returned
    so callers can record it in context."""
    item = _item(conn, number)
    name = name.strip()
    if not name:
        raise OpsError("name is empty")
    old = item["name"] or ""
    if old == name:
        return {"number": number, "name": name, "old_name": old, "changed": False}
    conn.execute("UPDATE items SET name=?, updated_at=? WHERE id=?",
                 (name, db.now(), item["id"]))
    git = item_fields_commit(conn, item, {"name": name}, actor=actor,
                             message=f"{item['number']}: rename to {name!r}"
                                     f"\n\nWas {old!r}. The number never "
                                     "changes; the name is item master data.\n")
    db.add_event(conn, "item.rename", item_id=item["id"], actor=actor,
                 data=_with_git({"from": old, "to": name}, git))
    return _with_git({"number": number, "name": name, "old_name": old,
                      "changed": True}, git)


def renumber_item(conn: sqlite3.Connection, number: str, *, new_number: str = "",
                  actor: str = "", note: str = "") -> dict:
    """Give an item the number it should have had — the repair tool for identity.

    Numbers are supposed to be immutable, and they still are in the sense that
    matters: nothing about the item changes except the label, the OLD label keeps
    resolving forever (items.former_numbers, honoured by db.item_by_number), and
    the reason is written into the item's context. This exists because the
    alternative — living with GL-OWNED-001 — means the registry teaches every
    future agent that classification belongs in identity.

    Referential integrity is free: BOM lines, relations, files, revisions,
    events, approvals and context all reference the item's integer ID, never its
    number. The one thing that does carry the number is the vault FOLDER, so it
    is renamed with a git commit in the data repo and every files.path under it
    is repointed in the same transaction.

    new_number empty = plm assigns the next free number in the item's own prefix
    and its kind's class, which is the intended path.
    """
    from . import vault as vlt
    item = _item(conn, number)
    old = item["number"]
    kind = item["kind"]
    prefix = old.split("-")[0]
    if new_number:
        new_number = validate_number(new_number, kind)
    else:
        if kind not in config.CLASS_BY_KIND:
            raise OpsError(f"{old} has kind {kind!r}, which has no number class — "
                           "pass new_number")
        if not re.fullmatch(r"[A-Z][A-Z0-9]{0,7}", prefix):
            raise OpsError(f"cannot derive a prefix from {old} — pass new_number")
        new_number = next_number(conn, prefix, config.CLASS_BY_KIND[kind])
    if new_number == old:
        raise OpsError(f"{old} already has that number", 409)
    clash = db.item_by_number(conn, new_number)
    if clash:
        raise OpsError(f"{new_number} is already taken by item {clash['id']} "
                       f"({clash['number']})", 409)

    # The vault folder carries the number; keep the two in step.
    old_folder = item["folder"] or ""
    new_folder = old_folder
    commit = ""
    files_repointed = 0
    if old_folder:
        new_folder = vlt.item_folder(item["project"], kind, new_number, item["name"])
        if (config.DATA_ROOT / old_folder).is_dir():
            commit = vlt.move_folder(
                old_folder, new_folder,
                f"plm: renumber {old} -> {new_number} (plm owns identity)")
            cur = conn.execute(
                "UPDATE files SET path = ? || substr(path, ?) WHERE path LIKE ?",
                (new_folder, len(old_folder) + 1, f"{old_folder}/%"))
            files_repointed = cur.rowcount or 0
        # else: a folder was RECORDED but never written (no check-in yet) —
        # retarget the record and leave the disk alone.

    # The REPOSITORY carries the number too — it is the repo's name, and
    # `gitvalidate` refuses a manifest that disagrees with it. So the directory
    # moves with the number and the old name is left as a redirect, exactly the
    # way a renamed GitHub repo keeps resolving (§4).
    from . import manifest as _mf
    # A design item's repository directory carries the number; a catalog item's
    # FILE NAME does (§4). Same fact, two shapes, one of them applies.
    git = ({} if not git_writes(conn, item) else
           rename_catalog_entry(conn, old, new_number, actor=actor)
           if kind in _mf.CATALOG_KINDS else rename_repo(old, new_number))

    former = [n for n in (item["former_numbers"] or "").split(",") if n]
    if old not in former:
        former.append(old)
    conn.execute(
        "UPDATE items SET number=?, folder=?, former_numbers=?, updated_at=? WHERE id=?",
        (new_number, new_folder or None, ",".join(former), db.now(), item["id"]))
    if git.get("renamed"):
        fresh = db.resolve_item(conn, item["id"])
        msg = (f"Renumber {old} -> {new_number}\n\nplm owns identity: a "
               "number is an identifier, not a classification.\nThe old "
               "number still resolves.\n")
        git.update(item_fields_commit(conn, fresh, {"number": new_number},
                                      actor=actor, message=msg))
        # …and the drafts too, because `number` is the one item-level field a
        # tree is REFUSED for getting wrong (`draft_fields_commit`).
        git["drafts"] = draft_fields_commit(
            conn, fresh, {"number": new_number}, actor=actor, message=msg)

    used_in = conn.execute("SELECT COUNT(*) FROM bom WHERE child_item_id=?",
                           (item["id"],)).fetchone()[0]
    rel_count = conn.execute(
        "SELECT COUNT(*) FROM relations WHERE item_id=? OR target_item_id=?",
        (item["id"], item["id"])).fetchone()[0]
    body = (f"**Renumbered {old} → {new_number}.** plm owns identity: a number is "
            f"an identifier, not a classification — the class token says only what "
            f"`kind` already says ({kind}). The old number still resolves.")
    if note:
        body += f"\n\n{note}"
    # Through the one context write path (plm/context.py). event=False: the
    # narration of an operation that already emits item.renumber below.
    from . import context as ctx
    ctx.add(conn, body, target_type="item", target=item["id"],
            actor=actor or "plm", event=False)
    db.add_event(conn, "item.renumber", item_id=item["id"], actor=actor,
                 data={"from": old, "to": new_number, "folder": new_folder,
                       "git_commit": commit, "files_repointed": files_repointed,
                       "bom_lines": used_in, "relations": rel_count,
                       **({"git": git} if git else {})})
    return {"id": item["id"], "number": new_number, "former_number": old,
            "former_numbers": former, "kind": kind, "name": item["name"],
            "folder": new_folder or None, "git_commit": commit,
            "files_repointed": files_repointed, "bom_lines_intact": used_in,
            "relations_intact": rel_count, **({"git": git} if git else {})}


def reclassify_item(conn: sqlite3.Connection, number: str, kind: str, *,
                    actor: str = "", note: str = "",
                    renumber: bool = True) -> dict:
    """Change what an item IS — the repair tool for classification.

    `kind` is the fact; the number's CLASS TOKEN is a projection of that fact
    (config.KIND_BY_CLASS), and the vault FOLDER is another one
    (vault.item_folder puts parts under parts/, coupons under coupons/). So a
    kind change that leaves the number alone makes the number lie: GL-PRT-9001
    with kind=coupon reads PRT to every human and every existing tool. This
    op therefore does the whole change or refuses it:

      1. kind on the item row (+ an item.kind event),
      2. THEN renumber_item, which — because it reads the item's *current*
         kind — assigns the right class and moves the vault folder to the right
         subdir with a git commit, keeping former_numbers so the old number
         still resolves,
      3. a context entry saying why, because a reclassification is a judgement
         and the judgement is the part of it that will be questioned later.

    Order matters: renumber after the kind is already written, or the new number
    gets the OLD class. Everything else is referentially safe — BOM lines,
    relations, files, events, approvals and context all key on the integer id.

    renumber=False records the kind alone; it refuses when that would leave the
    number's class contradicting the kind, so the escape hatch cannot be used
    by accident. Kinds with no class of their own are allowed through as-is.
    """
    item = _item(conn, number)
    if kind not in config.CLASS_BY_KIND:
        raise OpsError(f"kind must be one of {sorted(config.CLASS_BY_KIND)}")
    old_kind = item["kind"]
    old_number = item["number"]
    if old_kind == kind:
        raise OpsError(f"{old_number} is already a {kind}", 409)
    # The bar on non-structural kinds is maintained at BOM WRITE time, so a
    # reclassification into one has to honour it too — otherwise this op is a
    # third door into the same illegal state (a coupon or an asset sitting in a
    # BOM, and therefore in a rollup and a build plan).
    if kind in NOT_IN_BOM and conn.execute(
            "SELECT 1 FROM bom WHERE child_item_id=? LIMIT 1",
            (item["id"],)).fetchone():
        used = conn.execute(
            """SELECT DISTINCT p.number, r.version FROM bom b
               JOIN revisions r ON r.id=b.parent_rev_id
               JOIN items p ON p.id=r.item_id WHERE b.child_item_id=?""",
            (item["id"],)).fetchall()
        where = ", ".join(f"{u['number']} v{u['version']}" for u in used)
        raise OpsError(
            f"{old_number} is used in a BOM ({where}), so it cannot become "
            f"{_a(kind)}: {NOT_IN_BOM[kind]}. Remove the occurrence(s) first — on a "
            "frozen parent that means Revise ↑ the parent, then remove the "
            "line.")

    cls = old_number.split("-")[1] if "-" in old_number else ""
    class_lies = config.KIND_BY_CLASS.get(cls) not in (None, kind)
    if class_lies and not renumber:
        raise OpsError(
            f"{old_number} is class {cls} (= kind "
            f"{config.KIND_BY_CLASS.get(cls)!r}); making it a {kind} without a "
            f"new number would leave the number contradicting the fact. Pass "
            f"renumber=True (plm assigns "
            f"{old_number.split('-')[0]}-{config.CLASS_BY_KIND[kind]}-…).")

    conn.execute("UPDATE items SET kind=?, updated_at=? WHERE id=?",
                 (kind, db.now(), item["id"]))
    # `kind` travels with a clone (§2: it is the part, not the process), so the
    # manifest of every open draft says the new one. The renumber below moves
    # the repository and writes the new NUMBER the same way.
    git = item_fields_commit(
        conn, db.resolve_item(conn, item["id"]), {"kind": kind}, actor=actor,
        message=(f"{old_number} is a {kind}, not a {old_kind}\n\n"
                 "The number's class token and the folder are projections of "
                 "this fact.\n"))
    db.add_event(conn, "item.kind", item_id=item["id"], actor=actor,
                 data=_with_git({"from": old_kind, "to": kind}, git))

    renum = None
    if class_lies and renumber:
        renum = renumber_item(
            conn, item["id"], actor=actor,
            note=f"Reclassified {old_kind} → {kind}, so the class token had to "
                 f"follow the fact. The reasoning is in the next context entry.")

    from . import context as ctx
    body = (f"**Reclassified {old_number}: kind {old_kind} → {kind}.**"
            + (f" Renumbered {renum['former_number']} → {renum['number']} so the "
               f"class token matches the fact; the old number still resolves."
               if renum else ""))
    if note:
        body += f"\n\n{note}"
    ctx.add(conn, body, target_type="item", target=item["id"],
            actor=actor or "plm", event=False)
    return {"id": item["id"], "number": renum["number"] if renum else old_number,
            "former_number": old_number if renum else "",
            "kind": kind, "old_kind": old_kind, "name": item["name"],
            "renumbered": bool(renum), **({"git": git} if git else {}),
            "git_renumber": renum.get("git") if renum else None,
            "folder": renum["folder"] if renum else item["folder"],
            "git_commit": renum["git_commit"] if renum else "",
            "former_numbers": renum["former_numbers"] if renum else
            [n for n in (item["former_numbers"] or "").split(",") if n]}


def next_number(conn: sqlite3.Connection, prefix: str, cls: str) -> str:
    """Next free number in a class, e.g. (KML, PRT) -> KML-PRT-2014."""
    import re
    top = 0
    for (num,) in conn.execute("SELECT number FROM items WHERE number LIKE ?",
                               (f"{prefix}-{cls}-%",)):
        m = re.search(r"(\d+)$", num)
        if m:
            top = max(top, int(m.group(1)))
    return f"{prefix}-{cls}-{top + 1:04d}"


def fork(conn: sqlite3.Connection, number: str, new_name: str, *,
         actor: str = "", version: str = "", new_number: str = "",
         reason: str = "", idempotency_key: str = "") -> dict:
    """Save-As, the PLM way: a NEW item seeded from a source revision — files
    and BOM carried exactly like revise — plus a derived_from relation and an
    auto context entry. The source item is untouched.

    IDEMPOTENT on the auto-numbered path, for the same reason create_item is:
    this mints identity, so a retry would fork twice (plm/idem.py)."""
    from . import idem
    src = _item(conn, number)
    src_rev = db.find_revision(conn, number, version)
    if not src_rev:
        raise OpsError(f"no revision for {number} {version or '(latest)'}", 404)

    src_base = _revise_base(conn, src, src_rev)
    fields = {"from": src["number"], "from_version": src_rev["version"],
              "new_name": new_name, "new_number": new_number}
    if not new_number:
        replay = idem.lookup(conn, op="fork", actor=actor, fields=fields,
                             key=idempotency_key)
        if replay:
            return replay
        parts = number.split("-")
        if len(parts) < 3:
            raise OpsError(f"cannot derive a number scheme from {number}; pass new_number")
        # Allocate and insert together, so losing the race for a number costs a
        # retry rather than a UNIQUE-constraint 500 (_mint).
        new_number, new_id = _mint(conn, parts[0], parts[1], name=new_name,
                                   kind=src["kind"], project=src["project"])
    else:
        if conn.execute("SELECT 1 FROM items WHERE number=?",
                        (new_number,)).fetchone():
            raise OpsError(f"item already exists: {new_number}", 409)
        new_id = db.upsert_item(
            conn, new_number, name=new_name, kind=src["kind"],
            project=src["project"],
            folder=_new_folder(src["project"], src["kind"], new_number, new_name))
    # A FORK IS A CLONE (§4): the new number gets the source repository's
    # HISTORY, and its first commit is a child of the revision it was forked
    # from. Nothing is copied row by row, because there is nothing to copy — the
    # tree is already there.
    if src_base:
        return _fork_git(conn, src, src_rev, src_base, new_number=new_number,
                         new_id=new_id, new_name=new_name, reason=reason,
                         actor=actor, fields=fields,
                         idempotency_key=idempotency_key)
    rev_id = db.upsert_revision(
        conn, new_id, "0.1.0", state="DRAFT", author=actor,
        summary=f"Forked from {number} v{src_rev['version']}",
        source=src_rev["source"], selector=src_rev["selector"],
        material=src_rev["material"], mass_g=src_rev["mass_g"],
        process=src_rev["process"])
    from . import attributes as attrs
    attrs.copy(conn, src_rev["id"], rev_id)
    # A fork of a cut part is a cut part: the routing seeds the new number too
    # (plm/fab.py). NOT the cut_from link, though — relations are provenance and
    # a fork records only derived_from; the fork chooses its own stock, which is
    # frequently the whole point of forking a bracket.
    from . import fab
    fab.copy(conn, src_rev["id"], rev_id)
    # Caveats follow the geometry into the fork: a fork inherits the unproven
    # load path it was forked from until somebody actually proves it.
    from . import tags as _tags
    _tags.copy(conn, src_rev["id"], rev_id)
    for f in conn.execute("SELECT * FROM files WHERE revision_id=?", (src_rev["id"],)):
        conn.execute(
            """INSERT INTO files (revision_id,role,path,sha256,size,media,backend,
                                  key,role_source,media_source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (rev_id, f["role"], f["path"], f["sha256"], f["size"], f["media"],
             f["backend"], f["key"], _row_get(f, "role_source"),
             _row_get(f, "media_source")))
    for line in conn.execute("SELECT * FROM bom WHERE parent_rev_id=?", (src_rev["id"],)):
        conn.execute(
            """INSERT INTO bom (parent_rev_id,find_number,child_item_id,child_version,
                                qty,unit,alt_group,notes,suppressed) VALUES (?,?,?,?,?,?,?,?,?)""",
            (rev_id, line["find_number"], line["child_item_id"], line["child_version"],
             line["qty"], line["unit"], line["alt_group"], line["notes"], line["suppressed"]))
    # Provenance through the same typed surface every other link uses
    # (plm/relations.py) — so a fork shows up in the Related items card, and in
    # get_item's `related` block, without a second code path.
    from . import relations as rel
    rel.add(conn, "derived_from", new_number, number,
            to_version=src_rev["version"], note=reason, actor=actor)
    from . import context as ctx
    ctx.add(conn, f"Forked from {number} v{src_rev['version']}."
            + (f"\n\n**Why:** {reason}" if reason else ""),
            target_type="revision", target=rev_id, actor=actor, event=False)
    db.refresh_latest(conn, new_id)
    db.add_event(conn, "fork", item_id=new_id, revision_id=rev_id, actor=actor,
                 data={"from": number, "from_version": src_rev["version"]})
    out = {"number": new_number, "name": new_name, "version": "0.1.0",
           "from": src["number"], "from_version": src_rev["version"],
           "id": new_id}
    if not fields["new_number"]:
        idem.record(conn, op="fork", actor=actor, fields=fields,
                    key=idempotency_key, response=out, item_id=new_id)
    return out


def revision_diff(conn: sqlite3.Connection, number: str, a: str, b: str) -> str:
    """What changed between two revisions — always answerable from the index.

    Layers: metadata delta, BOM delta (the whole story for revise/propagate
    revisions: rebound children), file delta from DB records, then content
    diffs from git or the vault folders when those exist. Revisions created by
    revise/propagate have no folder of their own — files are carried forward —
    so the content layer degrades to a note instead of an error."""
    item = _item(conn, number)
    ra = db.find_revision(conn, number, a)
    rb = db.find_revision(conn, number, b)
    if not ra or not rb:
        raise OpsError("unknown revision", 404)

    out = [f"{number} — v{a} → v{b}", "=" * 46]
    for field in ("state", "date", "ecn", "summary", "source", "selector"):
        va, vb = ra[field] or "—", rb[field] or "—"
        if va != vb:
            out.append(f"{field:>9}: {va}  →  {vb}")

    # BOM delta — occurrence-keyed (child + find number)
    def bom_map(rev_id: int) -> dict:
        return {(l["child_number"], l["find_number"]): l
                for l in db.get_bom(conn, rev_id)}
    ba, bb = bom_map(ra["id"]), bom_map(rb["id"])
    lines = []
    # ⚠️ `find_number` is an INTEGER column holding prose on 75 prod rows
    # ('enclosure shell'); sorting ints and strings together raised TypeError,
    # so `what_changed` / `GET …/diff` crashed on any assembly carrying one
    # (GL-ASM-0000 — on master too; found by the compatibility harness). Numbers first, in
    # order, then prose.
    def _bom_order(k):
        f = k[1] or 0
        return (0, f, "", k[0]) if isinstance(f, (int, float)) else (1, 0, str(f), k[0])
    for key in sorted(set(ba) | set(bb), key=_bom_order):
        la, lb = ba.get(key), bb.get(key)
        if la and not lb:
            lines.append(f"  - removed  {key[0]}  ({la['qty']:g} {la['unit']})")
        elif lb and not la:
            lines.append(f"  + added    {key[0]}  v{lb['child_version'] or 'latest'}"
                         f"  ({lb['qty']:g} {lb['unit']})")
        elif la and lb:
            deltas = []
            if la["child_version"] != lb["child_version"]:
                deltas.append(f"v{la['child_version'] or 'latest'} → "
                              f"v{lb['child_version'] or 'latest'}")
            if la["qty"] != lb["qty"]:
                deltas.append(f"qty {la['qty']:g} → {lb['qty']:g}")
            if deltas:
                lines.append(f"  ~ rebound  {key[0]}  {', '.join(deltas)}")
    if lines:
        out += ["", "BOM changes:"] + lines
    elif ba or bb:
        out += ["", "BOM: unchanged"]

    # File delta from the index, by basename. Backend-agnostic on purpose:
    # identity is sha256 (falling back to size), never the presence of a vault
    # folder — an offloaded row's `path` is only a display name.
    def file_map(rev_id: int) -> dict:
        return {Path(f["path"]).name: f for f in conn.execute(
            "SELECT * FROM files WHERE revision_id=?", (rev_id,))}

    def where(f) -> str:
        return (f["backend"] or "vault")
    fa, fb = file_map(ra["id"]), file_map(rb["id"])
    flines, carried = [], 0
    for name in sorted(set(fa) | set(fb)):
        xa, xb = fa.get(name), fb.get(name)
        if xa and not xb:
            flines.append(f"  - removed  {name}")
        elif xb and not xa:
            flines.append(f"  + added    {name}  ({xb['size']:,} B)"
                          + (f"  [{where(xb)}]" if where(xb) != "vault" else ""))
        elif (xa["sha256"] and xa["sha256"] == xb["sha256"]) or \
                xa["path"] == xb["path"] or xa["size"] == xb["size"]:
            # Same content — carried forward, re-written into a new version
            # folder, or moved to another storage backend.
            if where(xa) != where(xb):
                flines.append(f"  = moved    {name}  {where(xa)} → {where(xb)}"
                              " (content unchanged)")
            else:
                carried += 1
        else:
            flines.append(f"  ~ changed  {name}  {xa['size']:,} B → {xb['size']:,} B"
                          + (f"  [{where(xa)} → {where(xb)}]"
                             if where(xa) != where(xb) else ""))
    if flines or carried:
        out += ["", "Files:"] + flines
        if carried:
            out.append(f"  = {carried} file(s) carried forward unchanged")

    # Content layer, when there is content of its own to diff
    out.append("")
    if ra["git_commit"] and rb["git_commit"] and item["folder"]:
        out += ["Content diff (git):", vault.diff_commits(
            ra["git_commit"], rb["git_commit"], item["folder"])]
    elif item["folder"]:
        da = (config.DATA_ROOT / item["folder"] / "versions" / f"v{a}").is_dir()
        db_ = (config.DATA_ROOT / item["folder"] / "versions" / f"v{b}").is_dir()
        if da and db_:
            out += ["Content diff (vault folders):",
                    vault.diff_folders(item["folder"], a, b)]
        else:
            missing = [f"v{v}" for v, ok in ((a, da), (b, db_)) if not ok]
            out.append(f"(no content of its own to diff: {', '.join(missing)} "
                       "exists only in the index — files carried forward)")
    else:
        out.append("(item has no vault folder)")
    return "\n".join(out)


def propagate(conn: sqlite3.Connection, number: str, version: str = "",
              actor: str = "") -> list[dict]:
    """The child changed: rev up every ancestor whose *latest* revision still
    binds an older precise revision of it, rebinding that occurrence. Walks to
    the top of the tree; imprecise lines are skipped (they already resolve to
    latest). Returns the created revisions, child-first."""
    item = _item(conn, number)
    rev = db.find_revision(conn, number, version)
    if not rev:
        raise OpsError(f"no revision for {number} {version or '(latest)'}", 404)

    created: list[dict] = []
    visited: set[int] = set()
    frontier: list[tuple[int, str, str]] = [(item["id"], number, rev["version"])]
    while frontier:
        child_id, child_num, child_ver = frontier.pop(0)
        parents = conn.execute(
            """SELECT DISTINCT i.number FROM items i
               JOIN revisions r ON r.id=i.latest_rev_id
               JOIN bom b ON b.parent_rev_id=r.id
               WHERE b.child_item_id=? AND b.child_version!='' AND b.child_version!=?""",
            (child_id, child_ver)).fetchall()
        for p in parents:
            parent = _item(conn, p["number"])
            if parent["id"] in visited:
                continue
            visited.add(parent["id"])
            new_rev = revise(
                conn, parent["number"], bump="patch",
                summary=f"Rebind {child_num} to v{child_ver}", actor=actor,
                rebind_lines={child_id: child_ver})
            created.append({"number": parent["number"], **new_rev})
            frontier.append((parent["id"], parent["number"], new_rev["version"]))
    return created


# -- the release workflow: submit -> IN_REVIEW -> approve/reject --------------
#
# ONE CODE PATH PER BEHAVIOUR. Until stage 3 these three verbs were
# written out twice — once in plm/api.py's handlers and once in plm_mcp's tools —
# with the SQL copied between them. That was survivable while approving was four
# UPDATEs; it stopped being survivable the moment approval also had to move a git
# branch and write a tag, because the second copy would have been the one that
# merged nothing. They live here now and both surfaces are thin callers.
#
# Each returns `_event`: (type, payload) for plm/api.py's `dispatch_event`, which
# talks to the integrations framework and is an HTTP concern, not an ops one.
# Callers that have no webhook plane (the MCP) pop it and drop it.

SUBMIT_STATES = ("CONCEPT", "DESIGN", "PROTOTYPE", "RELEASED")
#: master's two — what a submission may target in `off` and `read` (no policy).
LEGACY_SUBMIT_STATES = ("PROTOTYPE", "RELEASED")



def submit_for_release(conn: sqlite3.Connection, number: str, *,
                       version: str = "", target_state: str = "PROTOTYPE",
                       note: str = "", actor: str,
                       explode_waiver: str = "", _defer_auto: bool = False) -> dict:
    """Freeze a DRAFT at IN_REVIEW and open an approval — then let POLICY decide.

    Identical for a DRAFT that plm authored (revise + put_file) and one that
    arrived as a `git push` — a submission is a request for a decision, and
    where the bytes came from does not change the question being asked.

    WHAT STAGE 3C ADDS: `plm/policy.py` answers "does this maturity need a
    human?" before the request goes into the queue. At prototype and below the
    answer is no and the proposal merges here and now, with a real approval row
    whose approver is `policy` — so the ledger and the tag say honestly that
    nobody looked. At RELEASED it waits, because that is the one decision the
    charter reserves for a person.

    The response shape is the stage-4 frozen one plus `policy` (and `git` when
    the merge happened). `state` tells the truth: IN_REVIEW when it is waiting,
    the granted state when policy merged it.
    """
    from . import policy as pol
    rev = db.find_revision(conn, number, version)
    item0 = _item(conn, number) if rev else None
    # ⭐ THE GIT MODE GATES THE POLICY (stage 9). Auto-merge IS a merge — a tag
    # on `main` — so it exists only where git-first is switched on (`write`).
    # In `off` and `read` every submission waits for a human, exactly as on
    # master, and the targets are master's two. Chosen over "the policy default
    # under off is human" because the alternative leaves the policy table able
    # to auto-approve into a registry with no repo behind it: one switch, one
    # place that can say yes without a person.
    writes = bool(item0) and git_writes(conn, item0)
    allowed = SUBMIT_STATES if writes else LEGACY_SUBMIT_STATES
    if target_state not in allowed:
        raise OpsError(f"target_state must be {' or '.join(allowed)}"
                       if not writes else
                       f"target_state must be one of {', '.join(allowed)}", 422)
    if not rev:
        raise OpsError(f"no revision for {number} {version or '(latest)'}", 404)
    if rev["state"] != "DRAFT":
        raise OpsError(
            f"{number} v{rev['version']} is {rev['state']} — only DRAFTs can "
            "be submitted", 409)
    if not db.get_user(conn, actor):
        raise OpsError(f"unknown user: {actor}", 403)
    # THE EXPLODE GATE, before anything moves — and therefore before the
    # policy auto-merge below, which is the door master never had.
    request_note = note
    explode_note: dict = {}
    if writes:
        # stage 15: in `write` the gate compares the head with its base
        sfx, explode_note = explode_gate_write(conn, rev, explode_waiver,
                                               number=number)
        note = note + sfx
    else:
        note = note + explode_gate(conn, rev, explode_waiver, number=number)
    item = item0
    # `conn=` matters: without it `policy._row` never reads the `policy` TABLE,
    # so a per-project "a human approves here" row was silently ignored at the
    # one place it decides anything (found by stage 13's demo).
    spec = (pol.for_target(item["project"] or "", target_state, conn=conn)
            if writes else None)
    conn.execute("UPDATE revisions SET state='IN_REVIEW' WHERE id=?", (rev["id"],))
    cur = conn.execute(
        """INSERT INTO approvals (revision_id, target_state, note, requested_by,
                                  requested_at) VALUES (?,?,?,?,?)""",
        (rev["id"], target_state, note, actor, db.now()))
    approval_id = cur.lastrowid
    data = {"target": target_state, "note": request_note}
    if spec is not None:
        data["policy"] = spec
    db.add_event(conn, "submit", item_id=rev["item_id"], revision_id=rev["id"],
                 actor=actor, data=data)
    out = {"ok": True, "number": item["number"], "version": rev["version"],
           "state": "IN_REVIEW", "approval_id": approval_id,
           "target_state": target_state,
           "_event": ("submit", {"number": item["number"],
                                 "version": rev["version"],
                                 "target_state": target_state, "actor": actor})}
    # STAGE 13: the round belongs to a PROPOSAL (plm/proposals.py) — the object
    # that survives reject -> re-push -> resubmit — and the response says which,
    # with its open-thread count ("impossible to approve without seeing them",
    # August §5, kept). Additive keys only; in `off` no event row is written.
    from . import proposals
    auto = bool(spec and spec["auto"])
    # STAGE 14: a member of a MULTI-member change never auto-merges on its own
    # — its parent may pin it, and the change merges children first. When the
    # whole change is in review and every member's policy is automatic, the
    # CHANGE merges by policy (`_auto_change` below), in dependency order.
    multi = False
    if writes:
        from . import changes
        cur_p = proposals.for_revision(conn, rev["id"])
        if cur_p and cur_p.get("change_id"):
            multi = changes.member_count(conn, cur_p["change_id"]) > 1
    pbrief, pevents = proposals.on_submit(
        conn, approval_id=approval_id, rev=rev, item=item, actor=actor,
        target_state=target_state, note=request_note,
        notify=not auto or multi)
    out["proposal"] = pbrief
    out["threads"] = pbrief["threads"]
    if explode_note:
        out["explode"] = explode_note
    if pbrief.get("change"):
        out["change"] = pbrief["change"]
    if pevents:
        out["_events"] = [out["_event"], *pevents]
    if spec is None:
        return out
    out["policy"] = spec
    if multi:
        if out.get("change", {}).get("state") == "open" and not _defer_auto:
            got = _auto_change(conn, out["change"]["id"])
            if got:
                out["change_decision"] = got
                out["state"] = "merged with its pull request"
                out.setdefault("_events", [out["_event"]])
                out["_events"] += got.pop("_events", [])
        return out
    if not spec["auto"]:
        return out
    if writes and (out.get("change") or {}).get("id"):
        # stage 16b: a change of ONE is held by the same rules as a change of
        # many — an undisposed impact or a failing pre-flight goes to a human
        held = auto_hold(conn, out["change"]["id"], pending_ok=True)
        if held:
            from . import changes
            c1 = changes.row(conn, out["change"]["id"])
            out["held_by_policy"] = held
            out.setdefault("_events", [out.get("_event")])
            out["_events"].append(changes.attention(
                conn, c1, changes.members(conn, c1["id"]), reason="submitted",
                actor=actor))
            return out
    decided = decide_approval(
        conn, approval_id, decision="approved", actor=pol.APPROVER,
        note=pol.decision_note(spec), by_policy=True)
    out["state"] = decided["state"]
    out["approved_by_policy"] = True
    out["decision"] = {k: v for k, v in decided.items() if k != "_event"}
    if decided.get("git"):
        out["git"] = decided["git"]
    out["_events"] = [out.pop("_event"), decided["_event"]]
    if decided.get("proposal"):
        out["proposal"] = decided["proposal"]
    return out


def _pending_approval(conn: sqlite3.Connection, approval_id: int) -> sqlite3.Row:
    a = conn.execute("SELECT * FROM approvals WHERE id=?",
                     (approval_id,)).fetchone()
    if not a:
        raise OpsError(f"no approval {approval_id}", 404)
    if a["decision"]:
        raise OpsError(
            f"already {a['decision']}"
            + (f" by {a['approver']}" if a["approver"] else ""), 409)
    return a


def decide_approval(conn: sqlite3.Connection, approval_id: int, *,
                    decision: str, actor: str, note: str = "",
                    identity_via: str = "", by_policy: bool = False,
                    head_sha: str = "", via_change: int = 0,
                    repin: dict | None = None) -> dict:
    """The gate — and, for a revision that arrived by push, **the merge**.

    Approval is one operation with two halves that must not be able to disagree:
    git moves `main` and gains the annotated tag, and the registry moves the
    revision to its target state. The git half runs FIRST and the DB write is
    last, inside the caller's transaction, because the failure modes are not
    symmetric:

      git fails  -> nothing is written anywhere, the approval stays pending, and
                    the approver is told what git said (a conflicting merge, a
                    child that is not released). That is a decision deferred.
      DB fails   -> the transaction rolls back, but `main` has already moved.
                    Re-deciding is idempotent (`gitindex.approve_commit` is
                    write-if-absent), so the second attempt converges.

    A revision plm authored has no git origin; the git half is simply absent and
    approval is what it always was.
    """
    from . import policy as pol
    if decision not in ("approved", "rejected"):
        raise OpsError("decision must be approved or rejected")
    a = _pending_approval(conn, approval_id)
    rev = conn.execute("SELECT * FROM revisions WHERE id=?",
                       (a["revision_id"],)).fetchone()
    item = conn.execute("SELECT * FROM items WHERE id=?",
                        (rev["item_id"],)).fetchone()
    # ONE PLACE DECIDES WHO MAY APPROVE (plm/policy.py). Not a `kind == "bot"`
    # test here, because the same question is asked by `decide_release`, by a
    # release workflow's approval signoff and by the UI's decision bar, and four
    # copies of it would eventually be four answers.
    writes = git_writes(conn, item)
    if by_policy:
        # The ONE non-human door, and it is not reachable from outside: it is
        # opened by `submit_for_release` after `plm/policy.py` has answered, and
        # `actor` is the literal `policy`, never a username.
        if actor != pol.APPROVER:
            raise OpsError("a policy decision is recorded as `policy` and "
                           "nothing else", 403)
        approver_kind = "policy"
    elif not writes:
        # `off` / `read`: master's rule, verbatim — the `approver` role. Policy
        # (and with it "bots may approve") is a `write`-mode behaviour.
        if not db.user_has_role(conn, actor, "approver"):
            raise OpsError(f"{actor} is not an approver", 403)
        user = db.get_user(conn, actor) or {}
        approver_kind = user.get("kind") or ""
    else:
        verdict = pol.may_approve(conn, item["project"] or "",
                                  a["target_state"], actor)
        if not verdict["ok"]:
            raise OpsError(verdict["why"], 403)
        approver_kind = verdict["kind"]
    new_state = a["target_state"] if decision == "approved" else "DRAFT"

    # STAGE 13 — THE REVIEW IS BOUND TO A SHA (§2.2). `head_sha` is what the
    # decider looked at; if the proposal's head has moved since, the decision
    # is about bytes that will not ship and is refused. Then the opt-in
    # BLOCKING threads, honoured at RELEASED (§2.4) — before the git half, so a
    # refusal leaves nothing merged. Side-effect free beyond THIS proposal, so
    # a stage-14 change can call this N times in dependency order and stop at
    # the first refusal.
    from . import proposals, threads
    prop = proposals.for_approval(conn, approval_id)
    reviewed = rev["git_commit"] or ""
    # STAGE 14 — ONE PIECE OF WORK, ONE DECISION. A proposal that is one of
    # several in a CHANGE is decided by deciding the change: deciding it alone
    # could merge a parent before the child it pins, or ship half the work.
    # A single-member change is decided right here (it follows its proposal).
    if writes and prop and prop.get("change_id") and not via_change:
        from . import changes
        n = changes.member_count(conn, prop["change_id"])
        if n > 1:
            raise OpsError(
                f"proposal {proposals.label(prop['id'])} is one of {n} proposals in "
                f"pull request {changes.ref(prop['change_id'])} — one piece of work, "
                "one decision. Decide the pull request:\n"
                f"  {changes.url(prop['change_id'])}\n"
                f"  POST /api/pulls/{prop['change_id']}/decide "
                '{"decision": "approved", "heads": {...}}', 409)
    if head_sha and prop and prop["head_sha"] \
            and not prop["head_sha"].startswith(head_sha.strip().lower()):
        raise OpsError(
            f"you reviewed {head_sha[:10]}, but proposal {proposals.label(prop['id'])}'s head is "
            f"now {prop['head_sha'][:10]} — somebody pushed since. Reload it and "
            "look at what will actually be merged.", 409)
    if decision == "approved" and writes and prop \
            and a["target_state"] == "RELEASED":
        blockers = threads.blocking_open(conn, [("proposal", prop["id"])])
        if blockers:
            raise OpsError(threads.refusal(
                blockers, f"Approving proposal {proposals.label(prop['id'])} at RELEASED"), 409)

    git_result: dict = {}
    if decision == "approved" and writes:
        from . import gitindex
        # Raises OpsError on a conflicting merge or a release-gate violation,
        # BEFORE anything in the registry has moved.
        git_result = gitindex.approve_commit(
            conn, number=item["number"], revision=dict(rev),
            target_state=a["target_state"], approver=actor,
            approval_id=approval_id, note=note, approver_kind=approver_kind,
            repin=repin, change=f"CHG-{via_change}" if via_change else "")

    conn.execute(
        "UPDATE approvals SET decision=?, approver=?, approver_kind=?, "
        "decided_at=?, decision_note=? WHERE id=?",
        (decision, actor, approver_kind, db.now(), note, approval_id))
    conn.execute("UPDATE revisions SET state=? WHERE id=?",
                 (new_state, rev["id"]))
    release_rec: dict = {}
    if decision == "approved" and a["target_state"] == "RELEASED" and writes:
        # THE SHORTCUT (§2.1b): a proposal may target RELEASED directly, and
        # then it merges AND cuts the release in one decision. The release
        # record is created either way, so every RELEASED revision has one and
        # an evidence file — whichever door it came through.
        release_rec = record_proposal_release(
            conn, item=item, rev=dict(rev) | {"state": a["target_state"]},
            approval_id=approval_id, actor=actor, note=note,
            git_result=git_result)
    if git_result.get("tag"):
        # `git_commit` follows the RELEASE commit, not the pushed one: that is
        # the commit the tag is on and the one `main` carries. The pushed commit
        # is still its parent, byte for byte, and the `files` rows keep pointing
        # at it (their keys name an immutable commit on purpose).
        conn.execute("UPDATE revisions SET git_tag=?, git_commit=? WHERE id=?",
                     (git_result["tag"], git_result["commit"], rev["id"]))
    if git_result.get("repinned"):
        # The release commit's TREE differs from the pushed one in its gitlinks
        # (stage 14's re-pin), so the BOM rows — derived from the pushed tree,
        # pinned at the child's draft — are re-derived from the tag. One
        # writer: the indexer, from git, exactly as a promotion re-indexes.
        from . import gitindex as _gi
        _gi.index_commit(conn, item["number"], git_result["commit"],
                         ref=f"refs/tags/{git_result['tag']}", actor=actor,
                         origin="release")
        for r in git_result["repinned"]:
            db.add_event(conn, "change.repinned", item_id=rev["item_id"],
                         revision_id=rev["id"], actor=actor,
                         data={"change": via_change, "path": r["path"],
                               "child": r["child"], "from": r["from"],
                               "to": r["to"], "tag": r["tag"],
                               "on_tag": r["on_tag"],
                               **({"proposal": prop["id"]} if prop else {})})
    db.add_event(
        conn, "release" if decision == "approved" else "release.rejected",
        item_id=rev["item_id"], revision_id=rev["id"], actor=actor,
        data={"decision": decision, "state": new_state, "note": note,
              "approver_kind": approver_kind,
              **({"identity": identity_via} if identity_via else {}),
              **({"git": git_result} if git_result else {}),
              **({"proposal": prop["id"], "head": reviewed}
                 if writes and prop else {})})
    pbrief = proposals.on_decide(
        conn, approval=dict(a), decision=decision, actor=actor,
        approver_kind=approver_kind, note=note, head_sha=reviewed,
        git_result=git_result, identity=identity_via, via_change=via_change)
    out = {"ok": True, "number": item["number"], "version": rev["version"],
           "decision": decision, "state": new_state, "approver": actor,
           "approver_kind": approver_kind,
           "_event": ("release" if decision == "approved" else "release.rejected",
                      {"number": item["number"], "version": rev["version"],
                       "state": new_state, "actor": actor,
                       "approver_kind": approver_kind})}
    if git_result:
        out["git"] = git_result
    if release_rec:
        out["release"] = release_rec
    if pbrief:
        out["proposal"] = pbrief
    return out


def withdraw_approval(conn: sqlite3.Connection, approval_id: int, *,
                      actor: str, note: str = "") -> dict:
    """Pull a submission back: IN_REVIEW returns to DRAFT, nothing is merged and
    no tag is written. A git-origin draft keeps its branch — withdrawing is
    'not yet', not 'never'."""
    a = _pending_approval(conn, approval_id)
    if actor != a["requested_by"] and not db.user_has_role(conn, actor, "approver"):
        raise OpsError(
            f"only {a['requested_by']} or an approver can withdraw this", 403)
    rev = conn.execute("SELECT * FROM revisions WHERE id=?",
                       (a["revision_id"],)).fetchone()
    item = conn.execute("SELECT number FROM items WHERE id=?",
                        (rev["item_id"],)).fetchone()
    conn.execute("UPDATE approvals SET decision='withdrawn', approver=?, "
                 "decided_at=?, decision_note=? WHERE id=?",
                 (actor, db.now(), note, approval_id))
    conn.execute("UPDATE revisions SET state='DRAFT' WHERE id=?", (rev["id"],))
    db.add_event(conn, "release.withdrawn", item_id=rev["item_id"],
                 revision_id=rev["id"], actor=actor, data={"note": note})
    from . import proposals
    pbrief = proposals.on_withdraw(conn, approval_id=approval_id, actor=actor,
                                   why=note or "the round was withdrawn")
    return {"ok": True, "number": item["number"], "version": rev["version"],
            "state": "DRAFT", **({"proposal": pbrief} if pbrief else {}),
            "_event": ("release.withdrawn",
                       {"number": item["number"], "version": rev["version"],
                        "actor": actor, "note": note})}


# -- RELEASE: promotion of a revision that already exists (§2b) ----------------
#
# The second object. A proposal asks "is this change right?"; a release asks "is
# this revision ready to build against?", acts on an existing tag with NO file
# change, and is always a decision. `plm/release.py` holds the workflow registry,
# the gate, the generated notes and the evidence; the verbs are here because
# ops.py is the one place the API, the MCP and the UI share.
#
# THREE THINGS A PROMOTION MUST NOT DO, and the demo proves each: move a tag,
# write a commit on `main`, or change a single byte of the tree. What it writes
# is a `[status]` note on the release commit and a row here.

def _release_row(conn: sqlite3.Connection, release_id: int) -> dict:
    row = conn.execute(
        "SELECT rel.*, i.number, i.name AS item_name, i.kind, i.project "
        "FROM releases rel JOIN items i ON i.id=rel.item_id WHERE rel.id=?",
        (release_id,)).fetchone()
    if not row:
        raise OpsError(f"no release {release_id}", 404)
    return dict(row)


def get_release(conn: sqlite3.Connection, release_id: int) -> dict:
    """The whole release: the workflow as a signable checklist, the gate, the
    signoffs so far, and what is still missing before it can be approved."""
    from . import release as rel_mod
    rel = _release_row(conn, release_id)
    wf = rel_mod.workflow_for(rel["from_maturity"], rel["to_maturity"])
    signoffs = [dict(s) for s in conn.execute(
        "SELECT * FROM release_signoffs WHERE release_id=? ORDER BY id",
        (release_id,))]
    progress = _release_progress(conn, rel, wf, signoffs)
    gate = rel_mod.child_gate(gitwrite_repo(rel["number"]), rel["tag_sha"]) \
        if rel["tag_sha"] else []
    return {"release": rel, "workflow": wf, "signoffs": signoffs,
            "progress": progress, "gate": gate,
            "blockers": [g for g in gate if g.get("gates")],
            "pins": rel_mod.pin_closure(rel["number"], rel["tag_sha"])
            if rel["tag_sha"] else [],
            "evidence_url": (f"/api/releases/{release_id}/evidence"
                             if rel["evidence_sha256"] else None)}


def gitwrite_repo(number: str):
    from . import gitwrite
    return gitwrite.repo_path(number)


def _release_progress(conn: sqlite3.Connection, rel: dict, wf: dict,
                      signoffs: list[dict]) -> dict:
    """What the workflow still wants. The release page renders this verbatim.

    Three independent conditions, and a release becomes `approved` only when all
    three are satisfied: the QUORUM of approvals, every required CHECK green, and
    every CHECKLIST line attested by name. One approver in a small deployment;
    the quorum is still a number, because that is the difference between a gate
    and a habit.
    """
    from . import policy as pol
    need = int((wf.get("approvals") or {}).get("required") or 0)
    spec = pol.for_target(rel["project"] or "", "RELEASED", conn=conn)
    approvals = [s for s in signoffs
                 if s["kind"] == "approval" and s["verdict"] == "approve"
                 and s["signer_kind"] in spec["approver_kinds"]]
    rejections = [s for s in signoffs
                  if s["kind"] == "approval" and s["verdict"] == "reject"]
    attested = {s["item"]: s for s in signoffs if s["kind"] == "checklist"
                and s["verdict"] in ("attest", "pass")}
    from . import release as _rel_mod
    by_ref = _rel_mod.evidence_options(conn, rel, wf)
    lines = [{"line": ln, "signed_by": attested.get(ln, {}).get("signer", ""),
              "signer_kind": attested.get(ln, {}).get("signer_kind", ""),
              "note": attested.get(ln, {}).get("note", ""),
              "at": attested.get(ln, {}).get("signed_at"),
              "done": ln in attested,
              # stage 10, additive: what this line was attested BY REFERENCE
              # to, and — while open — which passing claims could attest it
              "evidence": attested.get(ln, {}).get("evidence", "") or "",
              "evidence_type": (wf.get("evidence") or {}).get(ln, ""),
              "by_reference": [
                  {k: c[k] for k in ("ref", "from", "from_version", "commit",
                                     "exact", "result", "result_note",
                                     "measured")}
                  for c in by_ref.get(ln, [])]}
             for ln in (wf.get("checklist") or [])]
    # ⭐ THE CHECKS ARE THE REAL ONES (stage 7). This block used to SYNTHESISE
    # them — `manifest` was reported `pass` because the revision had been
    # indexed, `pins-released` was recomputed from `child_gate`, and everything
    # else was `skipped: no runner posts this yet`. All three sentences were out
    # of date the moment plm/checks.py landed, and the middle one meant the same
    # question was answered twice by two different functions. Now the rollup for
    # the TAG COMMIT is read from the store, with `checks.required_for` deciding
    # what is required and `checks.gate_for` deciding whether it may proceed.
    from . import checks as checksmod, release as rel_mod
    roll = (checksmod.gate_for(conn, rel["number"], rel["tag_sha"],
                               target_maturity=rel["to_maturity"] or "")
            if rel.get("tag_sha") else
            checksmod.rollup([], target_maturity=rel["to_maturity"] or "",
                             applicable=[]))
    roll.setdefault("gate", {"ok": False, "state": roll["state"],
                             "why": "this release has no tag commit to check"})
    by = {r["name"]: r for r in checksmod.read(conn, rel["number"],
                                               rel["tag_sha"])} \
        if rel.get("tag_sha") else {}
    # Which required checks even APPLY to this commit — `pins-released` does
    # not apply to a part with no gitlinks, and reporting it as "not measured
    # yet" on a page somebody is about to sign from would be a lie about what is
    # holding the release up. `rollup()` already skips it; this says so.
    applies = set(checksmod.applies_to(rel["number"], rel["tag_sha"])
                  if rel.get("tag_sha") else [])
    checks = []
    for name in roll.get("required") or []:
        if name in by:
            checks.append({"name": name, "status": by[name]["status"],
                           "summary": by[name]["summary"], "required": True})
        elif name not in applies:
            checks.append({"name": name, "status": "skipped", "required": True,
                           "summary": "does not apply to this commit "
                                      f"({checksmod.CHECKS.get(name, {}).get('applies', '?')})"})
        else:
            checks.append({"name": name, "status": "queued", "required": True,
                           "summary": "required for this promotion and not "
                                      "measured yet — POST "
                                      f"/api/repo/{rel['number']}/checks/run"})
    # The advisory half, so the page shows the whole board rather than only the
    # rows that can refuse. Same list `GET /api/repo/{n}/checks` serves.
    checks += [{"name": r["name"], "status": r["status"],
                "summary": r["summary"], "required": False}
               for r in by.values() if r["name"] not in
               set(roll.get("required") or [])]
    # `blockers` stays the CHILD list, because the §5.1 refusal names children
    # and the release page renders them as links. It is the same question
    # `pins-released` asks — asked of the same function, not re-implemented.
    blockers = ([g for g in rel_mod.child_gate(gitwrite_repo(rel["number"]),
                                               rel["tag_sha"]) if g.get("gates")]
                if rel.get("tag_sha")
                and checksmod.pins_must_be_released(rel["to_maturity"] or "")
                else [])
    checks_ok = bool(roll["gate"]["ok"])
    return {
        "approvals": {"required": need, "have": len(approvals),
                      "met": len(approvals) >= need,
                      "by": [{"signer": s["signer"], "kind": s["signer_kind"],
                              "at": s["signed_at"]} for s in approvals],
                      "who_may": spec["approver_kinds"],
                      "policy": spec},
        "rejected": bool(rejections),
        "checks": checks,
        "checks_ok": checks_ok,
        "rollup": {k: roll[k] for k in
                   ("state", "required", "required_missing", "required_failed",
                    "blocking", "counts", "total", "gate") if k in roll},
        "checklist": lines,
        "checklist_ok": all(ln["done"] for ln in lines),
        "blockers": blockers,
        "ready": (len(approvals) >= need and checks_ok
                  and all(ln["done"] for ln in lines) and not rejections),
    }


def request_release(conn: sqlite3.Connection, number: str, *, version: str = "",
                    to_maturity: str = "released", notes: str = "",
                    actor: str = "", _wrap: bool = True) -> dict:
    """Ask for an EXISTING tagged revision to be promoted. No file changes.

    This is the door stage 3b did not have: `submit_for_release` refuses
    anything that is not a DRAFT, so PROTOTYPE → RELEASED used to cost a fake
    revision. A bot may request one — requesting is not deciding — and should,
    because it is the agent that knows the part was printed and fitted.
    """
    from . import release as rel_mod
    item = _item(conn, number)
    rev = db.find_revision(conn, item["id"], version)
    if not rev:
        raise OpsError(f"no revision for {number} {version or '(latest)'}", 404)
    if rev["state"] in ("DRAFT", "IN_REVIEW"):
        raise OpsError(
            f"{number} v{rev['version']} is {rev['state']} — a promotion acts "
            "on a revision that\nalready exists as a release tag, and this one "
            "has not been merged yet.\n"
            "\n  submit the proposal instead: POST /api/items/"
            f"{number}/submit\n"
            "  (a proposal may target RELEASED directly — that is the "
            "shortcut, and it\n   creates the same release record.)", 409)
    to_maturity = (to_maturity or "released").lower()
    from_maturity = rel_mod.maturity_of(rev["state"])
    if from_maturity == to_maturity:
        raise OpsError(
            f"{number} v{rev['version']} is already {to_maturity}. A promotion "
            "changes what a\nrevision IS; there is nothing here to change.", 409)
    repo = gitwrite_repo(number)
    tag, tag_sha = rel_mod.tag_of(repo, rev["version"])
    if not tag_sha:
        raise OpsError(
            f"{number} v{rev['version']} has no release tag in its repository, "
            "so there is no\nimmutable revision to promote. Everything "
            "`plm/split.py` replayed has one; a\nrevision that does not is a "
            "DRAFT or a registry-only record.", 409)
    open_ = conn.execute(
        "SELECT id FROM releases WHERE item_id=? AND version=? AND "
        "state IN ('requested','in_review')", (item["id"], rev["version"])
    ).fetchone()
    if open_:
        raise OpsError(
            f"release {open_['id']} is already open on {number} "
            f"v{rev['version']} — one promotion at a\ntime, or the signoffs "
            "would be spread across two records. Decide or withdraw it.", 409)
    wf = rel_mod.workflow_for(from_maturity, to_maturity)
    cur = conn.execute(
        "INSERT INTO releases (item_id, revision_id, version, tag, tag_sha, "
        "from_maturity, to_maturity, workflow, state, notes_md, origin, "
        "requested_by, requested_at) "
        "VALUES (?,?,?,?,?,?,?,?,'requested',?,'promotion',?,?)",
        (item["id"], rev["id"], rev["version"], tag, tag_sha, from_maturity,
         to_maturity, wf["key"], notes or "", actor or "", db.now()))
    rel = _release_row(conn, cur.lastrowid)
    if not notes:
        # Generated, then edited (GitHub's auto-notes). Drafted here so the
        # requester has something to correct rather than a blank box.
        try:
            draft = rel_mod.notes_draft(
                conn, {**rel, "pins": rel_mod.pin_closure(number, tag_sha)})
            conn.execute("UPDATE releases SET notes_md=? WHERE id=?",
                         (draft, rel["id"]))
            rel["notes_md"] = draft
        except Exception as exc:  # noqa: BLE001 — notes are not the gate
            rel["notes_error"] = str(exc)[:200]
    db.add_event(conn, "release.requested", item_id=item["id"],
                 revision_id=rev["id"], actor=actor,
                 data={"release": rel["id"], "from": from_maturity,
                       "to": to_maturity, "tag": tag, "tag_sha": tag_sha,
                       "workflow": wf["key"]})
    # ⭐ ASK FOR THE CHECKS THE GATE WILL WANT (stage 7). A release is gated on
    # `checks.gate_for(…, tag_sha)`, and the rows are keyed by COMMIT — but the
    # runner is fed by `gitindex.index_commit`, which sees the commit a pusher
    # made, not necessarily the one a tag ends up on (`approve_commit` can add a
    # release commit on top). So the tag's own commit could have an empty board
    # forever, and the promotion would refuse PENDING with nothing to wait for.
    # Requesting a promotion is exactly the moment to measure the thing being
    # promoted. Queue-only: it never writes and never raises (plm/checks.py).
    from . import checks as checksmod
    checksmod.enqueue(number, tag_sha, item_id=item["id"])
    out = get_release(conn, rel["id"])
    if _wrap and git_writes(conn, item):
        # STAGE 14 — EVERY RELEASE IS A SET (of one, here); the queue shows one
        # kind of thing. A release set over a whole closure is
        # `releasesets.request` (POST /api/release-sets).
        from . import releasesets
        rs = releasesets.wrap_single(conn, rel["id"], actor=actor)
        if rs:
            out["release_set"] = {"id": rs["id"], "number": releasesets.label(
                rs["id"]), "url": releasesets.url(rs["id"]),
                "page": releasesets.page(rs["id"])}
    out["_event"] = ("release.requested",
                     {"number": number, "version": rev["version"],
                      "to_maturity": to_maturity, "actor": actor,
                      "release": rel["id"]})
    return out


def sign_release(conn: sqlite3.Connection, release_id: int, *, kind: str,
                 item: str = "", verdict: str = "", note: str = "",
                 actor: str = "", identity_via: str = "",
                 evidence: str = "") -> dict:
    """One signoff: an approval, or a checklist line attested BY NAME.

    WHO MAY DO WHICH. A checklist line is a statement of fact — "I printed this",
    "every buy line has a live source" — and an agent that can prove one should
    attest it, recorded as `signer_kind = bot` with its evidence in the note.
    An APPROVAL is a decision, and who may make one is `plm/policy.py`'s answer:
    humans by default, agents when the project's policy admits them. Not a test
    in this function, on purpose.
    """
    from . import policy as pol, release as rel_mod
    if kind not in ("approval", "checklist"):
        raise OpsError("kind must be `approval` or `checklist`")
    rel = _release_row(conn, release_id)
    if rel["state"] not in ("requested", "in_review"):
        raise OpsError(
            f"release {release_id} is {rel['state']} — it is decided, and a "
            "signoff on a decided\nrelease would be a signature on something "
            "that already shipped.", 409)
    user = db.get_user(conn, actor)
    if not user:
        raise OpsError(f"unknown user: {actor}", 403)
    signer_kind = user["kind"]
    wf = rel_mod.workflow_for(rel["from_maturity"], rel["to_maturity"])

    if kind == "approval":
        verdict = verdict or "approve"
        if verdict not in ("approve", "reject"):
            raise OpsError("an approval signoff is `approve` or `reject`")
        _set_guard(conn, release_id, "approve")
        v = pol.may_approve(conn, rel["project"] or "", "RELEASED", actor)
        if not v["ok"]:
            raise OpsError(v["why"], 403)
    else:
        verdict = verdict or "attest"
        lines = wf.get("checklist") or []
        if item not in lines:
            raise OpsError(
                f"{item!r} is not a line on this release's checklist.\n"
                "The workflow for "
                f"{rel['from_maturity']} → {rel['to_maturity']} asks for:\n"
                + ("".join(f"  · {ln}\n" for ln in lines) or
                   "  (none — this transition has no checklist)"), 400)
        if verdict not in ("attest", "fail"):
            raise OpsError("a checklist signoff is `attest` or `fail`")
    # ATTESTED BY REFERENCE (stage 10): the line points at the claim that
    # proves it — a passing coupon on this exact commit — and the reference is
    # checked, not trusted (release.resolve_evidence).
    claim: dict = {}
    if evidence:
        if kind != "checklist" or verdict != "attest":
            raise OpsError("evidence attests a checklist line; it is not an "
                           "approval and cannot fail one", 400)
        try:
            claim = rel_mod.resolve_evidence(conn, rel, wf, item, evidence)
        except rel_mod.ReleaseError as exc:
            raise OpsError(str(exc), exc.code) from None
        evidence = claim["ref"]
        note = note or (
            f"attested by reference to {claim['ref']}: {claim['type']} "
            f"{claim['to']} v{claim['to_version']} @ {claim['commit'][:10]} "
            f"— result {claim['result']}"
            + (f" ({claim['result_note']})" if claim["result_note"] else ""))

    conn.execute(
        "INSERT INTO release_signoffs (release_id, kind, item, signer, "
        "signer_kind, verdict, note, tag_sha, identity, signed_at, evidence) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (release_id, kind, item, actor, signer_kind, verdict, note,
         rel["tag_sha"], identity_via or "trusted-network", db.now(),
         evidence or ""))
    if rel["state"] == "requested":
        conn.execute("UPDATE releases SET state='in_review' WHERE id=?",
                     (release_id,))
    db.add_event(conn, "release.signoff", item_id=rel["item_id"],
                 revision_id=rel["revision_id"], actor=actor,
                 data={"release": release_id, "kind": kind, "item": item,
                       "verdict": verdict, "signer_kind": signer_kind,
                       "tag_sha": rel["tag_sha"], "note": note,
                       **({"evidence": evidence} if evidence else {})})
    out = get_release(conn, release_id)
    # The quorum, the checks and the checklist are all satisfied: the release
    # flips itself. A workflow that needed somebody to press "done" after the
    # last signature would be a workflow with a step that means nothing.
    if out["progress"]["ready"]:
        out = _finish_release(conn, release_id, actor=actor,
                              note="every signoff the workflow asked for is in",
                              identity_via=identity_via)
    return out


def _set_guard(conn: sqlite3.Connection, release_id: int, what: str) -> None:
    """STAGE 14 — a member of a MULTI-member release set is decided with its
    set (bottom-up, pre-flighted); a set of one is decided right here."""
    from . import releasesets
    rs = releasesets.set_of(conn, release_id)
    if rs and releasesets._size(conn, rs["id"]) > 1 \
            and rs["state"] in releasesets.OPEN:
        raise OpsError(
            f"release {release_id} is one of {releasesets._size(conn, rs['id'])} "
            f"members of release set {rs['id']} — {what} the set, so the "
            "closure is promoted bottom-up in one decision:\n"
            f"  {releasesets.url(rs['id'])}\n"
            f"  POST /api/release-sets/{rs['id']}/decide", 409)


def decide_release(conn: sqlite3.Connection, release_id: int, *, decision: str,
                   actor: str, note: str = "", identity_via: str = "",
                   attest_rest: bool = True, _in_set: bool = False) -> dict:
    """The convenience: "approve, and I attest the remaining checklist lines".

    The long way round is `sign_release` per line and then one approval; this is
    the one click a human wants when they have in fact done all four things. It
    is not a shortcut PAST the workflow — every line still becomes its own row
    with this person's name on it, because "geoff attested four lines at 14:02"
    is the record, and a single boolean would not be.
    """
    from . import policy as pol, release as rel_mod
    if decision not in ("approved", "rejected"):
        raise OpsError("decision must be approved or rejected")
    rel = _release_row(conn, release_id)
    if rel["state"] not in ("requested", "in_review"):
        raise OpsError(f"release {release_id} is already {rel['state']}", 409)
    if not _in_set:
        _set_guard(conn, release_id, "decide")
    v = pol.may_approve(conn, rel["project"] or "", "RELEASED", actor)
    if not v["ok"]:
        raise OpsError(v["why"], 403)
    if decision == "approved":
        # Before any signoff row is written: a refusal here leaves the release
        # exactly as it was, rather than half-attested by somebody whose click
        # did not land. The child gate first (its refusal has somewhere to go),
        # then the check rollup.
        release_gate_check(conn, rel)
        checks_gate_check(conn, rel)
        threads_gate_check(conn, rel)
    wf = rel_mod.workflow_for(rel["from_maturity"], rel["to_maturity"])

    if decision == "rejected":
        conn.execute(
            "INSERT INTO release_signoffs (release_id, kind, item, signer, "
            "signer_kind, verdict, note, tag_sha, identity, signed_at) "
            "VALUES (?,'approval','',?,?,'reject',?,?,?,?)",
            (release_id, actor, v["kind"], note, rel["tag_sha"],
             identity_via or "trusted-network", db.now()))
        conn.execute(
            "UPDATE releases SET state='rejected', decided_by=?, "
            "decided_kind=?, decided_at=?, decision_note=? WHERE id=?",
            (actor, v["kind"], db.now(), note, release_id))
        db.add_event(conn, "release.rejected", item_id=rel["item_id"],
                     revision_id=rel["revision_id"], actor=actor,
                     data={"release": release_id, "note": note})
        out = get_release(conn, release_id)
        out["_event"] = ("release.rejected",
                         {"number": rel["number"], "version": rel["version"],
                          "actor": actor, "release": release_id})
        return out

    if attest_rest:
        signed = {s["item"] for s in conn.execute(
            "SELECT item FROM release_signoffs WHERE release_id=? AND "
            "kind='checklist' AND verdict IN ('attest','pass')", (release_id,))}
        for line in (wf.get("checklist") or []):
            if line in signed:
                continue
            conn.execute(
                "INSERT INTO release_signoffs (release_id, kind, item, signer, "
                "signer_kind, verdict, note, tag_sha, identity, signed_at) "
                "VALUES (?,'checklist',?,?,?,'attest',?,?,?,?)",
                (release_id, line, actor, v["kind"],
                 note or "attested with the decision", rel["tag_sha"],
                 identity_via or "trusted-network", db.now()))
    conn.execute(
        "INSERT INTO release_signoffs (release_id, kind, item, signer, "
        "signer_kind, verdict, note, tag_sha, identity, signed_at) "
        "VALUES (?,'approval','',?,?,'approve',?,?,?,?)",
        (release_id, actor, v["kind"], note, rel["tag_sha"],
         identity_via or "trusted-network", db.now()))
    return _finish_release(conn, release_id, actor=actor, note=note,
                           identity_via=identity_via)


def release_gate_check(conn: sqlite3.Connection, rel: dict) -> None:
    """§5.1, asked of a PROMOTION: every `parts/*` pin must itself be RELEASED.

    Asked FIRST — before the readiness tally and before a single signoff row is
    written — because it is the refusal with somewhere to go. "check
    pins-released: 1 child pin is not RELEASED" is true and useless; this one
    names the child, the command that releases it and the command that bumps the
    pin afterwards.

    ⭐ WHEN it applies is `checks.pins_must_be_released()` and nowhere else —
    the same predicate `checks.required_for()` and `gitindex.approve_commit`
    ask, so "warn at prototype, refuse above it" has one definition.
    """
    from . import checks as checksmod, release as rel_mod
    if not checksmod.pins_must_be_released(rel.get("to_maturity") or "") \
            or not rel.get("tag_sha"):
        return
    blockers = [g for g in rel_mod.child_gate(gitwrite_repo(rel["number"]),
                                              rel["tag_sha"]) if g.get("gates")]
    if blockers:
        raise OpsError(
            rel_mod.gate_refusal(rel["number"], rel["version"], blockers), 409)


def threads_gate_check(conn: sqlite3.Connection, rel: dict) -> None:
    """Open BLOCKING threads hold a promotion (PLM_REVIEW_MODEL §2.1b's gate:
    "no open blocking thread", stage 13). The scope is the release itself plus
    the proposal(s) that produced the revision — a concern raised in review
    does not evaporate because the change merged. Asked beside the §5.1 child
    gate and the check rollup, before any signoff is written. The way past it
    without fixing anything is an approver resolving the thread WITH A REASON,
    which `plm/threads.py` records."""
    from . import threads
    blockers = threads.blocking_open(conn, threads.scope_for_release(conn, rel))
    if blockers:
        raise OpsError(threads.refusal(
            blockers, f"Releasing {rel['number']} v{rel['version']}"), 409)


def checks_gate_check(conn: sqlite3.Connection, rel: dict) -> None:
    """The CHECK rollup, asked of a promotion — `checks.gate_for()` and nothing
    else decides it (stage 7).

    Asked beside `release_gate_check` and for the same reason: a refusal here
    must leave the release exactly as it was, not half-attested by somebody
    whose click did not land. The §5.1 child gate runs first because when both
    would refuse, its text is the one with somewhere to go.
    """
    from . import checks as checksmod
    if not rel.get("tag_sha"):
        return
    roll = checksmod.gate_for(conn, rel["number"], rel["tag_sha"],
                              target_maturity=rel.get("to_maturity") or "")
    if not roll["gate"]["ok"]:
        raise OpsError(roll["gate"]["why"], 409)


def _finish_release(conn: sqlite3.Connection, release_id: int, *, actor: str,
                    note: str = "", identity_via: str = "") -> dict:
    """The approval itself: gate, evidence, note, state. In that order.

    The order is the whole safety argument. The GATE runs before anything is
    written, so a refusal leaves the release exactly as it was and the requester
    is told which child to release first. The EVIDENCE is frozen next, because
    its hash goes into the note. The NOTE is the only thing written into git —
    no tag moves, no commit lands on `main`. The registry moves last, inside the
    caller's transaction, with the note's ref move on the undo journal.
    """
    from . import gitindex, release as rel_mod
    rel = _release_row(conn, release_id)
    release_gate_check(conn, rel)
    threads_gate_check(conn, rel)
    progress = _release_progress(
        conn, rel, rel_mod.workflow_for(rel["from_maturity"],
                                        rel["to_maturity"]),
        [dict(s) for s in conn.execute(
            "SELECT * FROM release_signoffs WHERE release_id=? ORDER BY id",
            (release_id,))])
    if not progress["ready"]:
        # THE CHECKS REFUSE IN THEIR OWN WORDS. `checks.refusal()` names every
        # blocking row, its own summary and the command that re-runs it; a
        # release gate that flattened that to "check X: failed" would be the
        # third place in this file that re-words somebody else's finding.
        gate = (progress.get("rollup") or {}).get("gate") or {}
        if not progress["checks_ok"] and gate.get("why"):
            raise OpsError(gate["why"], 409)
        missing = []
        if not progress["approvals"]["met"]:
            missing.append(
                f"{progress['approvals']['required']} approval(s) required, "
                f"{progress['approvals']['have']} given")
        for ln in progress["checklist"]:
            if not ln["done"]:
                missing.append(f"checklist: {ln['line']}")
        for c in progress["checks"]:
            if c["status"] in ("fail", "error"):
                missing.append(f"check {c['name']}: {c['summary']}")
        raise OpsError(
            f"release {release_id} is not ready:\n"
            + "".join(f"  · {m}\n" for m in missing)
            + "\n  GET /api/releases/" + str(release_id)
            + " shows the whole workflow and who has signed what.", 409)

    before = rel_mod.repo_state(rel["number"])
    # THE DECISION IS WRITTEN BEFORE THE EVIDENCE IS FROZEN, and the order is
    # load-bearing: the evidence answers "who said so", so the row has to know
    # the answer before it is serialised. Freezing first produced a document
    # whose `decided_by` was empty and whose hash therefore could not be
    # reproduced from the rows a minute later — found by doing exactly that.
    decided_kind = (db.get_user(conn, actor) or {}).get("kind", "")
    conn.execute(
        "UPDATE releases SET decided_by=?, decided_kind=?, decided_at=?, "
        "decision_note=? WHERE id=?",
        (actor, decided_kind, db.now(), note, release_id))
    rel = _release_row(conn, release_id)
    ev = rel_mod.freeze_evidence(conn, rel)
    conn.execute(
        "UPDATE releases SET evidence_sha256=?, evidence_key=? WHERE id=?",
        (ev["sha256"], ev["key"], release_id))
    rel["evidence_sha256"], rel["evidence_key"] = ev["sha256"], ev["key"]
    note_res = rel_mod.write_status_note(conn, rel, actor=actor)
    if note_res.get("created"):
        _undo_ref(conn, rel["number"], gitnotes_ref(),
                  back_to=note_res.get("parent") or "",
                  only_if=note_res["commit"])
    _fault("after-git-write")

    conn.execute(
        "UPDATE releases SET state='approved', note_commit=? WHERE id=?",
        (note_res.get("commit", ""), release_id))
    # The revision's state follows, and then it is re-indexed from the tag so
    # the note's `[status]` is what wrote it — one writer, even here.
    gitindex.index_commit(conn, rel["number"], rel["tag_sha"],
                          ref=f"refs/tags/{rel['tag']}", actor=actor,
                          origin="release")
    db.refresh_latest(conn, rel["item_id"])
    after = rel_mod.repo_state(rel["number"])
    db.add_event(conn, "release.approved", item_id=rel["item_id"],
                 revision_id=rel["revision_id"], actor=actor,
                 data={"release": release_id, "to": rel["to_maturity"],
                       "tag": rel["tag"], "tag_sha": rel["tag_sha"],
                       "evidence_sha256": ev["sha256"],
                       "note_commit": note_res.get("commit", ""),
                       "identity": identity_via,
                       "unchanged": before == after,
                       "note": (
                           f"{rel['number']} v{rel['version']} promoted "
                           f"{rel['from_maturity']} → {rel['to_maturity']}. No "
                           "tag moved and nothing was committed on main; the "
                           "decision is a `[status]` note on the release "
                           "commit.")})
    from . import releasesets
    rs = releasesets.set_of(conn, release_id)
    if rs and releasesets._size(conn, rs["id"]) == 1 and rs["state"] in \
            releasesets.OPEN:
        conn.execute("UPDATE release_sets SET state='approved', decided_by=?, "
                     "decided_kind=?, decided_at=?, decision_note=?, "
                     "promote_state='done' WHERE id=?",
                     (actor, decided_kind, db.now(), note, rs["id"]))
        man = releasesets.freeze_manifest(conn, rs["id"])
        conn.execute("UPDATE release_sets SET manifest_key=?, manifest_sha256=? "
                     "WHERE id=?", (man["key"], man["sha256"], rs["id"]))
    out = get_release(conn, release_id)
    out["git"] = {"note_commit": note_res.get("commit", ""),
                  "notes_ref": gitnotes_ref(),
                  "tag": rel["tag"], "tag_sha": rel["tag_sha"],
                  "main_before": before["main"], "main_after": after["main"],
                  "tags_before": before["tags"], "tags_after": after["tags"],
                  "unchanged": before == after,
                  "evidence_sha256": ev["sha256"],
                  "evidence_bytes": ev["bytes"]}
    out["_event"] = ("release.approved",
                     {"number": rel["number"], "version": rel["version"],
                      "to_maturity": rel["to_maturity"], "actor": actor,
                      "release": release_id})
    return out


def gitnotes_ref() -> str:
    from . import gitnotes
    return gitnotes.REF


def withdraw_release(conn: sqlite3.Connection, release_id: int, *, actor: str,
                     note: str = "") -> dict:
    """Pull a promotion request back. Nothing was written, so nothing is undone."""
    rel = _release_row(conn, release_id)
    if rel["state"] not in ("requested", "in_review"):
        raise OpsError(f"release {release_id} is already {rel['state']}", 409)
    if actor != rel["requested_by"] and not db.user_has_role(conn, actor,
                                                             "approver"):
        raise OpsError(
            f"only {rel['requested_by']} or an approver can withdraw this", 403)
    conn.execute(
        "UPDATE releases SET state='withdrawn', decided_by=?, decided_at=?, "
        "decision_note=? WHERE id=?", (actor, db.now(), note, release_id))
    db.add_event(conn, "release.withdrawn", item_id=rel["item_id"],
                 revision_id=rel["revision_id"], actor=actor,
                 data={"release": release_id, "note": note})
    out = get_release(conn, release_id)
    out["_event"] = ("release.withdrawn",
                     {"number": rel["number"], "version": rel["version"],
                      "actor": actor, "release": release_id})
    return out


def list_releases(conn: sqlite3.Connection, *, pending: bool = True,
                  number: str = "", limit: int = 50) -> list[dict]:
    where, args = [], []
    if pending:
        where.append("rel.state IN ('requested','in_review')")
    if number:
        item = db.resolve_item(conn, number)
        where.append("rel.item_id=?")
        args.append(item["id"] if item else -1)
    sql = ("SELECT rel.*, i.number, i.name AS item_name, i.kind, i.project "
           "FROM releases rel JOIN items i ON i.id=rel.item_id "
           + ("WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY rel.requested_at DESC LIMIT ?")
    rows = [dict(r) for r in conn.execute(sql, (*args, limit))]
    from . import release as rel_mod
    for r in rows:
        wf = rel_mod.workflow_for(r["from_maturity"], r["to_maturity"])
        signoffs = [dict(s) for s in conn.execute(
            "SELECT * FROM release_signoffs WHERE release_id=? ORDER BY id",
            (r["id"],))]
        p = _release_progress(conn, r, wf, signoffs)
        r["workflow"] = wf
        r["progress"] = {"approvals": p["approvals"]["have"],
                         "approvals_required": p["approvals"]["required"],
                         "checklist_done": sum(1 for l in p["checklist"]
                                               if l["done"]),
                         "checklist_total": len(p["checklist"]),
                         "blockers": len(p["blockers"]), "ready": p["ready"]}
    return rows


def release_for_revision(conn: sqlite3.Connection, revision_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM releases WHERE revision_id=? AND state='approved' "
        "ORDER BY id DESC LIMIT 1", (revision_id,)).fetchone()
    return dict(row) if row else {}


def record_proposal_release(conn: sqlite3.Connection, *, item, rev,
                            approval_id: int, actor: str, note: str,
                            git_result: dict) -> dict:
    """THE SHORTCUT (§2.1b): a proposal that targeted RELEASED cuts the release
    in the same decision.

    Every RELEASED revision has a release record and an evidence file, whichever
    door it came through — otherwise "show me the evidence for what we shipped"
    would have two answers, one of them "there isn't any".

    The workflow is not replayed here: a human has just approved this exact
    revision through the proposal, and asking them to attest the same checklist
    twice would be the process people route around. The signoff row records that
    it came from the proposal, so the evidence says so.
    """
    from . import release as rel_mod
    tag = git_result.get("tag") or f"v{rev['version']}"
    tag_sha = git_result.get("commit") or ""
    wf = rel_mod.workflow_for(rel_mod.maturity_of(rev.get("state") or ""),
                              "released")
    cur = conn.execute(
        "INSERT INTO releases (item_id, revision_id, version, tag, tag_sha, "
        "from_maturity, to_maturity, workflow, state, origin, approval_id, "
        "requested_by, requested_at, decided_by, decided_kind, decided_at, "
        "decision_note) VALUES (?,?,?,?,?,?,?,?,'approved','proposal',?,?,?,?,"
        "?,?,?)",
        (item["id"], rev["id"], rev["version"], tag, tag_sha, "", "released",
         wf["key"], approval_id, actor, db.now(), actor,
         (db.get_user(conn, actor) or {}).get("kind", ""), db.now(), note))
    release_id = cur.lastrowid
    conn.execute(
        "INSERT INTO release_signoffs (release_id, kind, item, signer, "
        "signer_kind, verdict, note, tag_sha, identity, signed_at) "
        "VALUES (?,'approval','',?,?,'approve',?,?,'proposal',?)",
        (release_id, actor, (db.get_user(conn, actor) or {}).get("kind", ""),
         f"approved through proposal #{approval_id}" + (f": {note}" if note
                                                        else ""),
         tag_sha, db.now()))
    rel = _release_row(conn, release_id)
    try:
        rel["notes_md"] = rel_mod.notes_draft(
            conn, {**rel, "pins": rel_mod.pin_closure(item["number"], tag_sha)})
        conn.execute("UPDATE releases SET notes_md=? WHERE id=?",
                     (rel["notes_md"], release_id))
        ev = rel_mod.freeze_evidence(conn, rel)
        conn.execute(
            "UPDATE releases SET evidence_sha256=?, evidence_key=? WHERE id=?",
            (ev["sha256"], ev["key"], release_id))
        rel["evidence_sha256"] = ev["sha256"]
        # STAGE 12'S HAND-BACK, fixed here (stage 14 owns the decide paths):
        # a release made through a PROPOSAL wrote no `[status]` note, so a
        # clone could not see its evidence hash — 62 of 135 POC releases. Now
        # every RELEASED revision's decision reaches git the same way a
        # promotion's does; `python -m plm.release --backfill-notes` wrote the
        # missing ones.
        rel["evidence_key"] = ev["key"]
        note_res = rel_mod.write_status_note(conn, rel, actor=actor)
        if note_res.get("created"):
            _undo_ref(conn, item["number"], gitnotes_ref(),
                      back_to=note_res.get("parent") or "",
                      only_if=note_res["commit"])
        conn.execute("UPDATE releases SET note_commit=? WHERE id=?",
                     (note_res.get("commit", ""), release_id))
    except Exception as exc:  # noqa: BLE001 — the merge already happened
        db.add_event(conn, "release.evidence_failed", item_id=item["id"],
                     revision_id=rev["id"], actor=actor,
                     data={"release": release_id, "error": str(exc)[:400]})
    db.add_event(conn, "release.approved", item_id=item["id"],
                 revision_id=rev["id"], actor=actor,
                 data={"release": release_id, "to": "released", "tag": tag,
                       "tag_sha": tag_sha, "origin": "proposal",
                       "approval": approval_id,
                       "evidence_sha256": rel.get("evidence_sha256", "")})
    return {"id": release_id, "origin": "proposal",
            "evidence_sha256": rel.get("evidence_sha256", "")}


# -- git under the hood: a design write is a COMMIT ---------------------------
#
# Geoff, 2026-09-17: "it should be git under the hood for everything now going
# forward — if you want to keep the legacy ways (/api) open we can, we just
# change the backend way of doing it."
#
# So: the API, the MCP and the UI stay exactly as they are, and underneath, a
# write to a revision that LIVES IN A REPO becomes a commit on its draft branch
# plus an index pass. Nothing writes those rows twice; `plm/gitindex.py` is the
# only writer of design facts for a git-origin revision, and it derives them
# from the commit.
#
# A revision that does NOT live in a repo (everything `split` replayed out of
# the vault era) keeps the old path unchanged, which is what makes this
# convertible one verb at a time instead of as a flag day.

def git_origin(conn: sqlite3.Connection, rev: dict) -> dict:
    """Is this revision's dataset a commit in its item's repo, and where?

    Returns {} when it is not. `revisions.git_commit` holds a VAULT sha for
    every revision `split` replayed, so "the commit exists in this repo" is the
    test — not "the column is non-empty".
    """
    from . import gitwrite
    number = rev.get("number") or ""
    if not number:
        row = conn.execute("SELECT number FROM items WHERE id=?",
                           (rev["item_id"],)).fetchone()
        number = row["number"] if row else ""
    commit = (rev.get("git_commit") or "").strip()
    if not (number and commit):
        return {}
    try:
        repo = gitwrite.repo_path(number)
    except gitwrite.WriteError:
        return {}
    from . import safegit
    if not safegit.ok(repo, "cat-file", "-e", f"{commit}^{{commit}}"):
        return {}
    branch = gitwrite.branch_for_commit(repo, commit)
    return {"number": number, "repo": repo, "commit": commit, "branch": branch}


def _git_draft_branch(conn: sqlite3.Connection, rev: dict, origin: dict) -> str:
    """The draft branch a write goes onto, refusing rather than guessing.

    A DB-era DRAFT that `split` replayed already has one (`draft/vX.Y.Z`); a
    git-origin DRAFT has whatever the pusher named. A revision that is not a
    DRAFT has none, and that is the release gate, not an accident.
    """
    if rev["state"] != "DRAFT":
        raise OpsError(
            f"{origin['number']} v{rev['version']} is {rev['state']} — a "
            "revision that is not a DRAFT is frozen.\n"
            "Revise up (or withdraw the submission) and edit the draft.", 409)
    if not origin["branch"]:
        from . import gitwrite
        # The registry can be BEHIND the branch (a commit landed and the
        # database transaction that would have recorded it rolled back; a push
        # arrived mid-edit). Git is the record: catch up with the branch this
        # commit is on rather than refuse the edit — and only when there is
        # exactly one, because two would be a guess.
        origin["branch"] = gitwrite.branch_containing(origin["repo"],
                                                      origin["commit"])
    if not origin["branch"]:
        raise OpsError(
            f"{origin['number']} v{rev['version']} is a DRAFT whose commit "
            f"({origin['commit'][:10]}) is not on any `draft/*` branch, so "
            "there is nowhere to commit this change.\n"
            "Push it as a draft branch first, or edit it where it lives.", 409)
    return origin["branch"]


def git_draft(conn: sqlite3.Connection, rev: dict, *,
              require: bool = True) -> dict:
    """The editable git dataset of a revision: origin + branch + parsed manifest.

    `{}` means "this revision does not live in a repo — take the old path", and
    that is what makes the conversion one verb at a time rather than a flag day.
    With `require=False` a revision that IS in a repo but is FROZEN (or whose
    commit is no draft branch's tip) also comes back empty instead of refusing —
    for the two verbs that are deliberately legal on a frozen revision
    (suppression, declaring a file's role), where refusing would remove a
    behaviour the registry has today.
    """
    from . import gitwrite
    if not git_writes(conn, rev):
        _stranded(conn, rev)        # refuses a revision that lives in git
        return {}
    origin = git_origin(conn, rev)
    if not origin:
        return {}
    try:
        origin["branch"] = _git_draft_branch(conn, rev, origin)
    except OpsError:
        if require:
            raise
        return {}
    # Edit the BRANCH TIP, not whatever commit the registry last recorded: the
    # branch is the line of work and the repo is the record. When the two differ
    # (see `gitwrite.branch_containing`) this commit catches the registry up
    # rather than reverting the commits it had not seen.
    tip = gitwrite.tip(origin["repo"], origin["branch"])
    origin["behind"] = bool(tip and tip != origin["commit"])
    origin["commit"] = tip or origin["commit"]
    origin["doc"] = gitwrite.read_manifest(origin["repo"], origin["commit"])
    return origin


def git_write(conn: sqlite3.Connection, g: dict, rev: dict, *, message: str,
              actor: str = "", changes: dict | None = None,
              links: dict | None = None, manifest: bool = True) -> dict:
    """Commit the edited manifest (and any blobs/gitlinks) onto the draft branch
    and re-index — the second half of every converted write.

    ONE SHAPE FOR EVERY VERB. The commit is validated exactly as a push is, the
    ref moves under a compare-and-swap against the commit this edit was read
    from, and `plm/gitindex.py` — the only writer of design facts for a
    git-origin revision — brings the rows up to date from what landed. Callers
    build their response AFTER this returns, out of the database, because the
    indexer is what decided the rows (and, for instance, a `files` or `bom`
    row's id is new: it was re-derived, not patched).

    TWO STORES, ONE OUTCOME (stage 3c). git has no transaction, so the ref move
    is registered as an UNDO on the connection before anything else happens: if
    the caller's SQLite transaction rolls back — because the indexer refused,
    because a later step in the same op failed, because the HTTP handler raised
    — the branch goes back to the commit it was on, under the same lock and
    under a compare-and-swap (`gitwrite.reset_ref`). A COMMIT clears the
    journal. That closes stage 3b's known bug 1, which was real: the window was
    hit during development and left a branch ahead of the registry.
    """
    from . import gitindex, gitwrite
    before = g["commit"]
    res = gitwrite.commit(
        g["number"], g["branch"], manifest_doc=g["doc"] if manifest else None,
        changes=changes, links=links, actor=actor or "plm", origin="api",
        message=message, parent=g["commit"])
    if res.get("created"):
        _undo_ref(conn, g["number"], g["branch"], back_to=before,
                  only_if=res["commit"])
        _fault("after-git-write")
        idx = index_draft(conn, g["number"], res["commit"],
                          ref=g["branch"], actor=rev.get("author") or actor,
                          origin="api", state="DRAFT", mover=actor)
    else:
        idx = {}
    # `_events`: the rare "needs your attention" a head move raises (a rejected
    # proposal re-opened by a commit plm made) — present only when non-empty,
    # popped by the callers that dispatch it (threads.apply_suggestion).
    return {"commit": res.get("commit"), "branch": g["branch"],
            "backend": "git", "created": bool(res.get("created")),
            **({"note": res["note"]} if res.get("note") else {}),
            **({"_events": idx["_events"]} if idx.get("_events") else {})}


def index_draft(conn: sqlite3.Connection, number: str, commit: str, *,
                ref: str, actor: str = "", origin: str = "api",
                state: str = "DRAFT", old: str = "", mover: str = "",
                base: str = "") -> dict:
    """`gitindex.index_commit` for a commit on a DRAFT branch — plus the one
    thing the review model needs to hear about it: the branch's head moved
    (`plm/proposals.on_head` — open or follow the proposal, dismiss the reviews
    it made stale, re-open a rejected one). Every ops site that indexes a draft
    commit and the push door (`POST /api/git/index`) come through here, so the
    proposal cannot miss a head move whichever door made it.

    ⚠️ STAGE-12 HUNK: gitindex.py is not this stage's file, so the hook is
    here, at its callers, rather than at the bottom of `index_commit`."""
    from . import gitindex, proposals
    left = _push_leaves_review(conn, number, ref, commit, actor=mover or actor,
                               origin=origin)
    res = gitindex.index_commit(conn, number, commit, ref=ref, actor=actor,
                                origin=origin, state=state,
                                **({"old": old} if old else {}))
    if left:
        res["left_review"] = left
    got = proposals.on_head(conn, number, ref, res.get("commit") or commit,
                            actor=mover or actor, origin=origin, base=base)
    if got.get("proposal"):
        res["proposal"] = got["proposal"]
    if got.get("base_error"):
        res["base_error"] = got["base_error"]
    if got.get("dismissed"):
        res["dismissed_reviews"] = got["dismissed"]
    if got.get("_events"):
        res["_events"] = got["_events"]
    return res


def _push_leaves_review(conn: sqlite3.Connection, number: str, ref: str,
                        commit: str, *, actor: str, origin: str) -> dict:
    """STAGE 14 — a push to a draft that is IN REVIEW, inside a CHANGE.

    Stage 13 left this refused in POST-receive (after the branch had already
    moved, so the branch was ahead of the registry until somebody withdrew and
    re-pushed) and named the forge semantics as the answer it had not taken:
    "the push withdraws the round and dismisses its reviews". A change is where
    that answer is needed — the human's signoff is bound to every member's
    head, and a member just moved — so it is taken here, for change members in
    `write` mode only: the round is withdrawn (the revision goes back to DRAFT
    and is re-indexed at the new head by the caller), the change leaves review,
    and `proposals.on_head` dismisses every signoff that looked at the old
    head. The pusher is told; the change is submitted again when ready."""
    from . import proposals
    if not (ref or "").startswith("refs/heads/draft/") or origin != "push":
        return {}
    item = db.resolve_item(conn, number)
    if not item or not git_writes(conn, item):
        return {}
    p = conn.execute(
        "SELECT * FROM proposals WHERE item_id=? AND head_ref=? AND state='open' "
        "AND change_id IS NOT NULL ORDER BY id DESC LIMIT 1",
        (item["id"], ref)).fetchone()
    if not p or p["head_sha"] == commit:
        return {}
    a = conn.execute("SELECT id FROM approvals WHERE proposal_id=? AND "
                     "decision='' ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
    if not a:
        return {}
    from . import changes
    why = (f"{actor or 'somebody'} pushed {commit[:10]} to {ref[11:]} while "
           f"pull request {changes.ref(p['change_id'])} was in review")
    conn.execute("UPDATE approvals SET decision='withdrawn', approver=?, "
                 "decided_at=?, decision_note=? WHERE id=?",
                 (actor, db.now(), why, a["id"]))
    conn.execute("UPDATE revisions SET state='DRAFT' WHERE id=?",
                 (p["revision_id"],))
    db.add_event(conn, "release.withdrawn", item_id=item["id"],
                 revision_id=p["revision_id"], actor=actor,
                 data={"note": why, "proposal": p["id"], "by_push": True})
    proposals.on_withdraw(conn, approval_id=a["id"], actor=actor, why=why)
    return {"proposal": p["id"], "change": changes.label(p["change_id"]),
            "round": a["id"], "why": why,
            "next": f"submit_change({changes.label(p['change_id'])}) when the "
                    "work is ready to be looked at again"}


# -- STAGE 14: the CHANGE — one decision over N proposals ------------------------

def submit_change(conn: sqlite3.Connection, change, *, actor: str,
                  note: str = "", target_state: str = "",
                  explode_waiver: str = "") -> dict:
    """Submit every member of a change that is not yet in review, then ask the
    human ONCE. A member that is already in review keeps its round. Each member
    goes through `submit_for_release` — the one submit every door uses — with
    the policy auto-merge deferred to the change (children first)."""
    from . import changes, proposals
    c = changes.row(conn, change)
    if c["state"] in changes.CLOSED:
        raise OpsError(f"{changes.title_ref(c['id'])} is {c['state']}", 409)
    mem = changes.members(conn, c["id"])
    if not mem:
        raise OpsError(f"{changes.title_ref(c['id'])} has no proposals yet — push "
                       f"a draft with `-o pr={c['id']}`", 409)
    if not db.get_user(conn, actor):
        raise OpsError(f"unknown user: {actor}", 403)
    events: list = []
    submitted = []
    explode_warnings: list[str] = []
    for m in mem:
        if m["state"] in ("open", "merged", "approved", "withdrawn"):
            continue
        if not m["revision_id"]:
            raise OpsError(f"proposal {proposals.label(m['id'])} ({m['item_number']}) has no "
                           "draft revision to submit", 409)
        rev = conn.execute("SELECT version FROM revisions WHERE id=?",
                           (m["revision_id"],)).fetchone()
        out = submit_for_release(
            conn, m["item_number"], version=rev["version"],
            target_state=(target_state or m["target_maturity"]
                          or "PROTOTYPE").upper(),
            note=note or c["title"], actor=actor, explode_waiver=explode_waiver,
            _defer_auto=True)
        submitted.append({"proposal": m["id"], "item": m["item_number"],
                          "version": rev["version"], "state": out["state"]})
        if (out.get("explode") or {}).get("warning"):
            explode_warnings.append(out["explode"]["warning"])
        events += [e for e in (out.get("_events") or [out.get("_event")])
                   if e and e[0] != "submit"]
    view = changes.view(conn, c["id"])
    got = _auto_change(conn, c["id"])
    held = got.pop("held", []) if got else []
    if got:
        events += got.pop("_events", [])
        view = changes.view(conn, c["id"])
    # STAGE 15: the answer LEADS with the one line to
    # tell the human, then what they should know before approving — so an
    # agent never has to dig a URL out of a nested payload or curl the checks.
    state, link, label = view["change"]["state"], changes.url(c["id"]), changes.label(c["id"])
    say = changes.say(c["id"], state, held)
    warnings = explode_warnings + [
        w for mm in view["members"] for w in mm.get("warnings") or []
        if not (w.split(" · ", 1)[-1].startswith("explodes:")
                and any(w.split(" · ", 1)[0] in e for e in explode_warnings))]
    # STAGE 16 (finding 5): the doors `changes.settle()` first (bounded wait,
    # outside this transaction); whatever is STILL running is named here, so
    # `warnings` never silently means "the checks that had finished".
    running = changes.checks_pending(conn, changes.members(conn, c["id"]))
    heads = {m["item"]: m["head_sha"] for m in view["members"]}
    warnings += [f"{n} · checks still running: {', '.join(v)} — "
                 f"get_checks('{n}', ref='{(heads.get(n) or '')[:12]}', "
                 "wait=120) answers when they finish"
                 for n, v in running.items()]
    pending = [d for d in view["impact"] if not d.get("in_change")
               and not d.get("disposition")]
    if state == "merged":
        amber = [w for w in warnings if " · " in w]
        if amber:
            # stage 16b: amber does not stop "just build it", but nobody
            # looked — so the one line the human reads names it
            say += (f" — merged with {len(amber)} amber check(s): "
                    + "; ".join(w.split(": ", 1)[0] for w in amber[:6]))
    res = {"ok": True, "say": say, "url": link, "change": view,
           "warnings": warnings, "checks_pending": running,
           **({"held_by_policy": held} if held else {}),
           "next": ("draft_dispositions('" + label + "') — "
                    f"{len(pending)} affected item(s) need a disposition; "
                    + (f"then submit_change('{label}') again (policy merges "
                       "it once nothing is left undecided)" if held else
                       "then stop and tell the human the `say` line"))
           if pending else "stop and tell the human the `say` line",
           "submitted": submitted}
    if got:
        res["decision"] = got
    if events:
        res["_events"] = events
    return res


def _auto_change(conn: sqlite3.Connection, cid: int) -> dict:
    """"Just build it" mode, for a change: when it is open and EVERY member's
    policy is automatic, the change merges by policy — through `decide_change`,
    so the order, the re-pins and the pre-flight are the same ones a person's
    approval gets. Nothing is merged if any member needs a human."""
    from . import changes, policy as pol
    c = changes.row(conn, cid)
    if c["state"] != "open":
        return {}
    mem = changes.members(conn, cid)
    if not mem or not all(
            pol.for_target(m["item_project"] or "", m["target"], conn=conn)["auto"]
            for m in mem if m["state"] == "open"):
        return {}
    held = auto_hold(conn, cid)
    if held:
        return {"held": held}
    return decide_change(conn, cid, decision="approved", actor=pol.APPROVER,
                         note="", by_policy=True, _commit=False)


def auto_hold(conn: sqlite3.Connection, cid: int, *,
              pending_ok: bool = False) -> list[str]:
    """Why "just build it" must NOT merge this change by itself — [] when it
    may (stage 16b; docs/PLM_REVIEW_MODEL.md § "What just build it means").

    Run 5 (CHG-156) merged by policy with GSI-ASM-0009 — a grandparent that
    pins a bumped assembly — impacted and no disposition recorded: nobody had
    said "use as is" or "bump", so the merge decided it by silence. Policy
    removes the HUMAN, not the decisions a human would have been shown:
      1. every impact row NOT in the change has a disposition (drafted counts —
         `draft_dispositions` is the bot's legwork, and its note is the record);
      2. the decision's own pre-flight passes (a failing required check, a
         stale pin, a blocking thread) — asked HERE, so a refusal holds the
         change for a human instead of rolling the whole submit back.
    Amber (an advisory warn/skip) does not hold it: it is listed in `say`.
    A held change stays open, in front of a human, with these reasons.

    `pending_ok` (the change-of-one path a single `submit` takes): a check
    nobody has measured yet does not hold it — that path never waited for
    checks, and the rule is "no FAILING required check" (a `refs/for/main`
    push settles its checks before it submits)."""
    from . import changes, policy as pol
    out = []
    undisposed = [x["parent"] for x in changes.impact(conn, cid)
                  if not x.get("in_change") and not x.get("disposition")
                  and not x.get("advisory")]
    if undisposed:
        out.append(f"{len(undisposed)} impacted item(s) have no disposition: "
                   f"{', '.join(undisposed[:6])} — draft_dispositions("
                   f"'{changes.label(cid)}'), then submit_change again")
    pf = changes.preflight(conn, cid, actor=pol.APPROVER, by_policy=True)
    out += [f"{p['member']}: {p['why']}" for p in pf["problems"]
            if "policy does not auto-merge" not in p["why"]
            and not (pending_ok and "are PENDING" in p["why"])]
    return out


def decide_change(conn: sqlite3.Connection, change, *, decision: str, actor: str,
                  note: str = "", heads: dict | None = None,
                  identity_via: str = "", by_policy: bool = False,
                  _commit: bool = True) -> dict:
    """THE decision: approve (or reject) every proposal in a change, once.

    1. **What you reviewed is what merges.** `heads` = {proposal: sha} as the
       page showed them; any member whose head moved since is refused by name
       (a push after review also DISMISSES the change's signoff — visibly).
    2. **Pre-flight EVERYTHING** (`changes.preflight`): each member mergeable,
       policy, checks, blocking threads and dispositions where they apply, the
       plan's stale pins — BEFORE the first merge. A refusal merges NOTHING.
    3. **Merge in dependency order** (`changes.plan`): children first. A parent
       whose gitlink pointed at a member's draft head is re-pinned, inside its
       own release commit, to that member's release commit — the tag is
       asserted. Each member is its own DB transaction (`conn.commit()`), and
       the change's `merge_log` records every step, so if something STILL
       fails mid-way despite pre-flight the change is left `partial` and says
       exactly what merged; deciding again resumes with the rest.
    """
    from . import changes, policy as pol, proposals
    if decision not in ("approved", "rejected"):
        raise OpsError("decision must be approved or rejected")
    c = changes.row(conn, change)
    cid = c["id"]
    L = changes.label(cid)
    mem = changes.members(conn, cid)
    resume = c["merge_state"] == "partial"
    # WHAT YOU REVIEWED, FIRST: when a member moved, that is the reason the
    # change may also have left review — say the reason, not the symptom.
    if heads:
        want = {str(k).lstrip("#"): str(v or "").lower() for k, v in heads.items()}
        for m in mem:
            got = want.get(str(m["id"]))
            if got and m["head_sha"] and not m["head_sha"].startswith(got):
                raise OpsError(
                    f"you reviewed {proposals.label(m['id'])} {m['item_number']} at {got[:10]}, "
                    f"but its head is now {m['head_sha'][:10]} — somebody pushed "
                    f"since you loaded {L}. Nothing was merged.\n"
                    + (f"The push took {L} out of review ({c['state']}); it is "
                       "submitted again when the work is whole.\n"
                       if c["state"] != "open" else "")
                    + "Reload the pull request and look at what would actually merge.\n"
                    f"  {changes.url(cid)}", 409)
    if c["state"] != "open" and not resume:
        raise OpsError(
            f"{L} is {c['state']}, not in review — "
            + ("submit it first (submit_change)." if c["state"] in
               ("draft", "rejected") else "there is nothing left to decide."),
            409)
    if not by_policy:
        user = db.get_user(conn, actor)
        if not user:
            raise OpsError(f"unknown user: {actor}", 403)
    if decision == "rejected":
        live = [m for m in mem if m["round"]]
        if not live:
            raise OpsError(f"{L} has no member in review to reject", 409)
        for m in live:
            v = pol.may_approve(conn, m["item_project"] or "", m["target"], actor)
            if not v["ok"]:
                raise OpsError(v["why"], 403)
        changes.add_review(conn, cid, actor=actor, verdict="reject", body=note,
                           heads=changes.heads_of(mem), counts=True,
                           identity=identity_via)
        for m in live:
            decide_approval(conn, m["round"]["id"], decision="rejected",
                            actor=actor, note=note or f"rejected with {L}",
                            identity_via=identity_via, head_sha=m["head_sha"],
                            via_change=cid)
        fields_who = (db.get_user(conn, actor) or {}).get("kind", "")
        conn.execute("UPDATE changes SET state='rejected', decided_at=?, "
                     "decided_by=?, decided_kind=?, decision_note=?, updated_at=? "
                     "WHERE id=?", (db.now(), actor, fields_who, note, db.now(),
                                    cid))
        changes.event(conn, "change.rejected", cid, actor=actor,
                      data={"note": note, "proposals": [m["id"] for m in live]})
        out = changes.view(conn, cid)
        out["_events"] = [("change.rejected",
                           {"subject": {"type": "change", "id": cid, "number": L},
                            "url": changes.url(cid), "actor": actor,
                            "note": note, "title": c["title"]})]
        return out

    pf = changes.preflight(conn, cid, actor=actor, by_policy=by_policy,
                           resume=resume)
    if not pf["ok"]:
        raise OpsError(changes.refusal(c, pf["problems"]), 409)
    pl = pf["plan"]
    log = json.loads(c["merge_log"] or "[]")
    done = {e["proposal"]: e for e in log if e.get("ok")}
    kind = "policy" if by_policy else (db.get_user(conn, actor) or {}).get("kind", "")
    if not done:
        changes.add_review(conn, cid, actor=actor, verdict="approve", body=note,
                           heads=changes.heads_of(mem), counts=True,
                           identity=identity_via, kind=kind)
    conn.execute("UPDATE changes SET merge_state='merging', updated_at=? "
                 "WHERE id=?", (db.now(), cid))
    if _commit:
        conn.commit()
    released: dict[int, dict] = {pid: e for pid, e in done.items()}
    events: list = []
    for m in pl["order"]:
        if m["id"] in done:
            continue
        repin = {}
        for path, r in pl["info"][m["id"]]["repin"].items():
            child = released.get(r["proposal"]) or {}
            repin[path] = {"sha": child.get("commit", ""), "child": r["child"],
                           "from": r["from"], "tag": child.get("tag", "")}
        try:
            out = decide_approval(
                conn, m["round"]["id"], decision="approved",
                actor=pol.APPROVER if by_policy else actor,
                note=(note or f"approved with {L}: {c['title']}"),
                identity_via=identity_via, by_policy=by_policy,
                head_sha=m["head_sha"], via_change=cid, repin=repin)
        except Exception as exc:                               # noqa: BLE001
            if not _commit:
                raise
            conn.rollback()
            entry = {"proposal": m["id"], "item": m["item_number"],
                     "version": m["version"], "ok": False,
                     "error": str(exc)[:1200], "at": db.now()}
            log.append(entry)
            conn.execute("UPDATE changes SET merge_state='partial', merge_log=?, "
                         "updated_at=? WHERE id=?", (json.dumps(log), db.now(),
                                                     cid))
            changes.event(conn, "change.partial", cid, actor=actor,
                          data={"failed": m["id"], "error": str(exc)[:400],
                                "merged": [e["proposal"] for e in log
                                           if e.get("ok")]})
            conn.commit()
            merged = [e for e in log if e.get("ok")]
            raise OpsError(
                f"{L} is PARTIALLY MERGED — pre-flight passed, then "
                f"{proposals.label(m['id'])} {m['item_number']} failed:\n  {exc}\n\n"
                + ("Already merged (they stay merged; git has no undo for a "
                   "tag and plm will not fake one):\n"
                   + "".join(f"  · {proposals.label(e['proposal'])} {e['item']} -> "
                             f"{e.get('tag', '')}\n" for e in merged)
                   if merged else "Nothing had merged yet.\n")
                + "\nThe change stays in review with merge_state=partial. Fix "
                  "the cause and decide again: merged members are skipped and "
                  "the rest resume in order.\n  " + changes.url(cid), 409) from None
        g = out.get("git") or {}
        entry = {"proposal": m["id"], "item": m["item_number"],
                 "version": m["version"], "ok": True,
                 "tag": g.get("tag", ""), "commit": g.get("commit", ""),
                 "merged": g.get("merged", ""),
                 "repinned": g.get("repinned", []), "at": db.now()}
        log.append(entry)
        released[m["id"]] = entry
        conn.execute("UPDATE changes SET merge_log=?, updated_at=? WHERE id=?",
                     (json.dumps(log), db.now(), cid))
        if out.get("_event"):
            events.append(out["_event"])
        if _commit:
            conn.commit()
    changes._touch(conn, cid, state="merged", merge_state="done",
                   decided_at=db.now(), decided_by=pol.APPROVER if by_policy else actor,
                   decided_kind=kind, decision_note=note)
    changes.event(conn, "change.merged", cid,
                  actor=pol.APPROVER if by_policy else actor,
                  data={"order": [e["item"] for e in log if e.get("ok")],
                        "tags": {e["item"]: e.get("tag") for e in log
                                 if e.get("ok")}, "by_policy": by_policy})
    res = changes.view(conn, cid)
    res["merged"] = [e for e in log if e.get("ok")]
    res["_events"] = events + [("change.merged",
                                {"subject": {"type": "change", "id": cid,
                                             "number": L},
                                 "url": changes.url(cid),
                                 "actor": pol.APPROVER if by_policy else actor,
                                 "tags": {e["item"]: e.get("tag") for e in log
                                          if e.get("ok")}})]
    return res


def _undo_ref(conn: sqlite3.Connection, number: str, ref: str, *,
              back_to: str, only_if: str) -> None:
    """Register 'put this ref back' on the connection's rollback journal."""
    from . import gitwrite
    hook = getattr(conn, "on_rollback", None)
    if hook is None:      # a plain sqlite3.Connection (a test, a script)
        return
    hook(lambda: gitwrite.reset_ref(number, ref, back_to=back_to,
                                    only_if=only_if))


class FaultInjected(OpsError):
    """The deliberate failure `PLM_FAULT_INJECT` asks for."""


def _fault(point: str) -> None:
    """Fail on purpose, here, when asked to — so the undo path can be PROVEN.

    A two-store rollback that is never exercised is a comment, not a guarantee.
    `PLM_FAULT_INJECT=after-git-write` makes the next converted write raise in
    the window between the commit landing and the rows being derived, which is
    exactly the window stage 3b's bug 1 described. `the API demo script` sets it for
    one call and asserts the branch is back where it started.

    It is env-gated and off everywhere it is not set, and it names the point
    rather than being a bare boolean, so a future second fault point does not
    have to reuse this one's meaning.
    """
    if os.environ.get("PLM_FAULT_INJECT", "") == point:
        raise FaultInjected(
            f"fault injected at `{point}` (PLM_FAULT_INJECT) — the git commit "
            "landed and the\nregistry write is being abandoned on purpose. The "
            "branch must end up back where\nit was.", 500)


# -- frozen-revision facts: `refs/notes/plm` (§5) ------------------------------
#
# A release tag's tree is immutable and must stay that way — what gets printed
# is what was reviewed. But facts about a released revision keep arriving: the
# mass the slicer measured on the first real print, a `unverified-datum` tag, a
# BOM line suppressed on one build, the OBSOLETE decision. Stage 3b wrote them
# to the registry only, so a clone knew nothing about them and `--rebuild` could
# not re-derive them, and it wrote the open question down instead of guessing.
#
# The answer is git's own: a NOTE on the release commit (`plm/gitnotes.py`).
# Notes have history, so every one of these is a commit with an author and a
# date; they never touch a tree or a tag, so immutability holds; and the indexer
# overlays them onto the rows it derived, so the read surface is unchanged.
#
# The API and MCP surfaces do not change at all: `set_attributes`, `tag_item`
# and `suppress_bom_line` on a frozen git-origin revision now SUCCEED by writing
# a note where they used to write the registry alone.

def frozen_note(conn: sqlite3.Connection, rev: dict) -> dict:
    """Is this revision a frozen record living in a repo? Then notes are its
    write surface. `{}` means "not that case — take your other path".

    IN_REVIEW is deliberately excluded: it is not frozen because it is a record,
    it is frozen because somebody is looking at it, and the answer there is still
    "withdraw the submission first".
    """
    if str(rev.get("state") or "") in ("DRAFT", "IN_REVIEW"):
        return {}
    if not git_writes(conn, rev):
        # `off`/`read`: a fact about a frozen LEGACY revision is a registry row,
        # as it was on master. A frozen revision that lives in git has no note
        # surface outside `write`, so the fact is refused by name — the same
        # rule as a draft (`_stranded`; stage 9b, open question 2).
        _stranded(conn, rev)
        return {}
    origin = git_origin(conn, rev)
    if not origin:
        return {}
    return {"number": origin["number"], "repo": origin["repo"],
            "commit": origin["commit"]}


def note_write(conn: sqlite3.Connection, fz: dict, rev: dict, mutate, *,
               message: str, actor: str = "") -> dict:
    """Amend the revision's note, re-index, and hand back the git block.

    Same two-store discipline as `git_write`: the notes ref move is registered
    as an undo before the registry is touched, so a rollback leaves the
    annotation exactly where it was.
    """
    from . import gitindex, gitnotes
    _, res = gitnotes.amend(fz["repo"], fz["commit"], mutate, message=message,
                            actor=actor or "plm")
    if res.get("created"):
        _undo_ref(conn, fz["number"], gitnotes.REF,
                  back_to=res.get("parent") or "", only_if=res["commit"])
        _fault("after-git-write")
        gitindex.index_commit(conn, fz["number"], fz["commit"],
                              ref=f"refs/tags/v{rev['version']}",
                              actor=actor, origin="note")
    return {"commit": res.get("commit"), "ref": gitnotes.REF, "backend": "note",
            "annotates": fz["commit"], "created": bool(res.get("created")),
            **({"note": res["note"]} if res.get("note") else {})}


def _note_attributes(conn: sqlite3.Connection, fz: dict, rev: dict,
                     values: dict, *, source: str = "",
                     actor: str = "") -> dict:
    """`set_attributes` on a frozen git-origin revision — §5's first verb."""
    from . import attributes as attrs
    written, cleared = [], []

    def mutate(note: dict) -> None:
        for raw_name, raw in (values or {}).items():
            name = str(raw_name).strip()
            if not name:
                raise OpsError("attribute names cannot be empty")
            value, unit, src = attrs._entry(raw)
            if value == "":
                note["attributes"].pop(name, None)
                cleared.append(name)
                continue
            entry = {"value": value}
            if unit or attrs.spec(name).get("unit"):
                entry["unit"] = unit or attrs.spec(name)["unit"]
            # Same rule as the manifest: `mass_g`/`material` are revision
            # COLUMNS with no provenance field, so a source here would be a fact
            # the registry cannot hold.
            if (src or source) and name not in attrs.COLUMN_BACKED:
                entry["source"] = src or source
            note["attributes"][name] = entry
            written.append(name)

    # The subject names what is being written. It is built from the REQUEST,
    # because `mutate` (which fills `written`/`cleared`) runs inside note_write,
    # after this string exists — stage 3c's subjects came out empty.
    names = [str(n).strip() if attrs._entry(v)[0] != "" else f"-{str(n).strip()}"
             for n, v in (values or {}).items()]
    git = note_write(conn, fz, rev, mutate, actor=actor,
                     message=(f"{fz['number']} v{rev['version']}: "
                              + ", ".join(names)
                              + (f"\n\nSource: {source}" if source else "")
                              + "\n\nThe revision is frozen behind an immutable "
                                "tag, so this is a NOTE on the\nrelease commit "
                                "(docs §5) — the tree and the tag are "
                                "untouched.\n"))
    return {"written": written, "cleared": cleared,
            "column_backed": [n for n in written if n in attrs.COLUMN_BACKED],
            "attributes": attrs.get(conn, rev["id"]), "git": git}


# -- BOM children are submodules, and the gitlink IS the pin (§5.1) ------------

def _child_repo(child_number: str):
    from . import gitwrite as gw
    p = gw.REPOS / f"{child_number}.git"
    return p if (p / "HEAD").exists() else None


def bom_pin(conn: sqlite3.Connection, child_number: str, child_kind: str,
            child_version: str) -> tuple[str, str, str, str]:
    """(BOM path, gitlink path, pinned commit, why) for one line — the whole of
    the git half of a BOM edit, so all five verbs pin identically.

    The two paths differ for a purchased child: the line names
    `catalog/<N>.toml` (the file that IS the part) while the gitlink is the ONE
    `catalog` submodule that carries it (§5.4 — not repo-per-screw).
    """
    from . import manifest as mf
    path = mf.bom_path(child_number, child_kind)
    if path.startswith("catalog/"):
        return path, "catalog", _catalog_pin(path), "catalog tip"
    sha, why = _pin_for(conn, child_number, child_version)
    return path, path, sha, why


def _pin_for(conn: sqlite3.Connection, child_number: str,
             child_version: str) -> tuple[str, str]:
    """(commit, note) — which commit of the child a BOM line pins.

    A precise line pins that version's TAG; an imprecise one (`track =
    "release"`) pins the newest release, which is what "whatever is current"
    meant and is the state the bump bot then tracks. A named version that is
    still a DRAFT pins the draft's commit: git is happy to, and §5.1's release
    gate is what refuses to SHIP above it.
    """
    from . import gitvalidate as gv, gitwrite, safegit
    repo = _child_repo(child_number)
    if not repo:
        raise OpsError(
            f"{child_number} has no repository, so there is no commit for this "
            f"BOM line to pin.\nA gitlink IS the pin (docs/PLM_GIT_FIRST.md "
            f"§5.1): check {child_number} in first "
            f"(POST /api/items/{child_number}/checkin), then add the line.", 409)
    rels = gv.releases(repo)
    if child_version:
        if child_version in rels:
            return rels[child_version], f"v{child_version}"
        # Named, but not released: the draft branch that claims it, else the
        # commit the registry recorded for that revision.
        for ref, v in gv.draft_versions(repo).items():
            if v == child_version:
                return gitwrite.tip(repo, ref), \
                    f"v{child_version} (DRAFT, not a release)"
        row = db.find_revision(conn, child_number, child_version)
        sha = (row or {}).get("git_commit") or ""
        if sha and safegit.ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            return sha, f"v{child_version} (unreleased commit)"
        raise OpsError(
            f"{child_number} v{child_version} is not in {child_number}'s "
            f"repository — there is no tag\n`v{child_version}` and no draft "
            "branch claiming it, so plm cannot pin this line at it.\n"
            "Pin a released version, or leave child_version empty to follow "
            "the child's releases.", 409)
    if rels:
        newest = max(rels, key=lambda v: tuple(int(x) for x in v.split(".")))
        return rels[newest], f"newest release v{newest} (track = release)"
    head = gitwrite.tip(repo, "HEAD")
    if head:
        return head, "the child's default ref (it has never released)"
    raise OpsError(
        f"{child_number}'s repository is empty — nothing has ever been checked "
        "in, so there is no commit to pin.", 409)


def _catalog_pin(path: str) -> str:
    """The catalog commit a parent pins: catalog.git's tip. One submodule for
    every purchased line (§5.4 — not repo-per-screw), so the pin says "the
    catalog as it stood when this line was written"."""
    from . import gitwrite, safegit
    cat = gitwrite.REPOS / "catalog.git"
    if not (cat / "HEAD").exists():
        raise OpsError("there is no catalog repository here, so a purchased "
                       "child cannot be pinned (§5.4).", 409)
    sha = gitwrite.tip(cat, "refs/heads/main") or gitwrite.tip(cat, "HEAD")
    if not sha:
        raise OpsError("catalog.git is empty — nothing to pin.", 409)
    if not safegit.ok(cat, "cat-file", "-e",
                      f"{sha}:{safegit.safe_path(path.split('/', 1)[1])}"):
        raise OpsError(
            f"`{path}` does not exist at catalog.git's tip. A purchased line "
            "points at\n`catalog/<NUMBER>.toml`, and that file IS the part — "
            "check the item in first so\nthe catalog carries it.", 409)
    return sha


def _bom_entry(doc: dict, path: str) -> dict | None:
    for line in (doc.get("bom") or []):
        if str(line.get("path") or "") == path:
            return line
    return None


def _bom_row_id(conn: sqlite3.Connection, rev_id: int, child_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM bom WHERE parent_rev_id=? AND child_item_id=? "
        "ORDER BY id DESC LIMIT 1", (rev_id, child_id)).fetchone()
    return row["id"] if row else 0


def _rev_for_git(conn: sqlite3.Connection, rev_id: int) -> dict:
    """A revision row as the git helpers want it (with its item's number)."""
    row = conn.execute(
        "SELECT r.*, i.number FROM revisions r JOIN items i ON i.id=r.item_id "
        "WHERE r.id=?", (rev_id,)).fetchone()
    return dict(row) if row else {}


def _bom_key(line: dict) -> tuple:
    """`manifest._bom`'s order — find number first, no-find last — so a line
    inserted here lands where an export would have put it and the commit is one
    hunk instead of a reflow."""
    find = line.get("find")
    try:
        return (0, 0, int(find), str(line.get("path") or ""))
    except (TypeError, ValueError):
        return (0 if find is not None else 1, 1, 0,
                f"{find}\0{line.get('path') or ''}")


def _bom_line_after(conn: sqlite3.Connection, rev_id: int, child_id: int,
                    parent_number: str, version: str, git: dict) -> dict:
    """One BOM verb's response, read back from the rows the indexer just wrote.

    The rows were re-derived, not patched (`gitindex._clear_design_rows`), so a
    line's id is NEW after a git-origin edit. Callers get the id that exists
    now, because the one they passed in is gone.
    """
    line_id = _bom_row_id(conn, rev_id, child_id)
    row = _bom_line_row(conn, line_id) if line_id else None
    out = {"number": parent_number, "version": version, "line": line_id,
           "git": git}
    if row:
        out.update({"find": row["find_number"], "child": row["child_number"],
                    "child_version": row["child_version"], "qty": row["qty"],
                    "unit": row["unit"],
                    "suppressed": bool(row["suppressed"]),
                    "notes": row["notes"]})
    return out


def set_attributes(conn: sqlite3.Connection, rev: dict, values: dict, *,
                   source: str = "", actor: str = "") -> dict:
    """Record measured properties on a revision — mass, material, bbox, colour.

    **THE FIRST CONVERTED WRITE.** For a revision whose dataset is a commit,
    this edits `[attributes]` in `plm.toml`, commits that on the draft branch as
    `actor`, and re-indexes — so an agent who cloned the part yesterday gets the
    change with a plain `git pull`, and `git log -p plm.toml` says who changed
    the mass and why. The response shape is unchanged, because the surface is
    not what changed; the backend is.

    Everything else still goes straight to `plm/attributes.py`. Converting one
    verb at a time is deliberate: each one is a commit shape that has to be got
    right, and a flag day would get them all wrong at once.
    """
    from . import attributes as attrs
    cat = catalog_revision(conn, rev)
    if cat:
        # A purchased item's facts live in `catalog/<NUMBER>.toml` (§4), so the
        # rows are written first and the FILE is re-rendered from them — the
        # inverse order of a design item, because a catalog entry is a
        # projection of the item rather than a tree somebody authored.
        out = attrs.set_many(conn, rev["id"], values, source=source,
                             actor=actor)
        out["git"] = catalog_write(
            conn, cat["item"], cat["rev_id"], actor=actor,
            message=(f"{cat['number']} v{rev['version']}: "
                     + ", ".join(out.get("written") or [])
                     + (f"\n\nSource: {source}" if source else "") + "\n"))
        return out
    frozen = frozen_note(conn, rev)
    if frozen:
        return _note_attributes(conn, frozen, rev, values, source=source,
                                actor=actor)
    g = git_draft(conn, rev)
    if not g:
        return attrs.set_many(conn, rev["id"], values, source=source,
                              actor=actor)

    doc = g["doc"]
    before = dict(doc.get("attributes") or {})
    written, cleared = [], []
    for raw_name, raw in (values or {}).items():
        name = str(raw_name).strip()
        if not name:
            raise OpsError("attribute names cannot be empty")
        value, unit, src = attrs._entry(raw)
        if value == "":
            doc["attributes"].pop(name, None)
            cleared.append(name)
            continue
        entry = {"value": value}
        if unit or attrs.spec(name).get("unit"):
            entry["unit"] = unit or attrs.spec(name)["unit"]
        # `mass_g` and `material` live in REVISION COLUMNS (one home per fact,
        # so build plans can roll them up in SQL) and those columns have no
        # provenance field — `attributes.get` reports `source: ""` for them,
        # always. Writing a source into the manifest would put a fact in the
        # file that the registry cannot hold, and the next export would drop it:
        # the manifest and the DB would disagree by construction. So it is
        # omitted here, and `gitvalidate` warns a pusher who writes one.
        if (src or source) and name not in attrs.COLUMN_BACKED:
            entry["source"] = src or source
        doc["attributes"][name] = entry
        written.append(name)
    doc["attributes"] = dict(sorted(doc["attributes"].items()))
    if doc["attributes"] == before:
        return {"written": [], "cleared": [], "column_backed": [],
                "attributes": attrs.get(conn, rev["id"]), "git": {},
                "note": "no change"}

    what = ", ".join(written + [f"-{c}" for c in cleared])
    res = git_write(
        conn, g, rev, actor=actor or "plm",
        message=f"Set {what} on {g['number']} v{rev['version']}"
                + (f"\n\nSource: {source}" if source else ""))
    return {"written": written, "cleared": cleared,
            "column_backed": [n for n in written if n in attrs.COLUMN_BACKED],
            "attributes": attrs.get(conn, rev["id"]), "git": res}


# -- the manifest-only facts ---------------------------------------------------
#
# A routing, a process, a revision-scoped tag and a revision-level relation are
# all the same shape of write: one key in `plm.toml`, no bytes, no gitlink. They
# are converted together because they convert identically — read the manifest at
# the draft's tip, change the one thing, commit, let the indexer write the rows.
#
# They also share one boundary, and it is worth stating once rather than four
# times: these verbs live in `plm/fab.py`, `plm/tags.py` and
# `plm/relations.py`, which own the REGISTRIES that decide what is legal. Those
# modules keep every refusal they had; what moves here is only the decision of
# WHERE the fact gets written, because that is an orchestration question and
# ops.py is the one place the API, the MCP and the UI share.

def set_routing(conn: sqlite3.Connection, rev: dict, operations: list[dict], *,
                actor: str = "", replace: bool = True) -> dict:
    """A revision's routing — `[[routing]]` in the manifest on a git-origin
    DRAFT, `operations` rows otherwise. Same validation either way
    (`fab.prepare_routing`), same response."""
    from . import fab
    g = git_draft(conn, rev)
    if not g:
        return fab.set_routing(conn, rev["id"], operations, actor=actor,
                               replace=replace)
    ready = fab.prepare_routing(conn, rev["id"], operations, replace=replace)
    rows = [] if replace else list(g["doc"].get("routing") or [])
    base = max([int(r.get("seq") or 0) for r in rows] or [0])
    for n, p in enumerate(ready["prepared"], start=1):
        e: dict = {"seq": base + n * 10, "op": p["op"]}
        if p["params"]:
            e["params"] = {k: p["params"][k] for k in sorted(p["params"])}
        if p["work_center"]:
            e["work_center"] = p["work_center"]
        if p["notes"]:
            e["notes"] = p["notes"]
        rows.append(e)
    g["doc"]["routing"] = rows
    res = git_write(
        conn, g, rev, actor=actor,
        message=(f"Routing for {g['number']} v{rev['version']}: "
                 + (", ".join(p["op"] for p in ready["prepared"]) or "(none)")))
    return {"number": g["number"], "version": rev["version"],
            "operations": fab.get_routing(conn, rev["id"]),
            "warnings": ready["warnings"], "git": res}


def set_process(conn: sqlite3.Connection, rev: dict, process: str, *,
                actor: str = "") -> dict:
    """How a revision comes into existence: printed, or made. A top-level key in
    the manifest — `process` dispatches the build plan, so it is a validated
    field and not a free-text attribute."""
    from . import fab
    g = git_draft(conn, rev)
    if not g:
        return fab.set_process(conn, rev["id"], process, actor=actor)
    # fab owns the refusals (unknown process, wrong kind, frozen revision); ask
    # it before writing a commit nobody would want kept.
    _row, want = fab.prepare_process(conn, rev["id"], process)
    g["doc"]["process"] = want
    res = git_write(conn, g, rev, actor=actor,
                    message=(f"{g['number']} v{rev['version']} is "
                             f"{want or 'no longer dispatched to a process'}"))
    return {"number": g["number"], "version": rev["version"],
            "process": want, "git": res}


def set_tag(conn: sqlite3.Connection, item, tag: str, *, rev: dict | None = None,
            note: str = "", actor: str = "") -> dict:
    """Tag an item, or one of its revisions.

    Three homes, one per kind of fact (stage 3c closes the two stage 3b left
    open):

      revision-scoped, on a DRAFT    `tags` in `plm.toml` — it travels with a
                                     clone of that revision, because an
                                     unverified datum is a caveat about THOSE
                                     bytes.
      revision-scoped, FROZEN        a NOTE on the release commit (§5). The tree
                                     is behind an immutable tag and stays that
                                     way; the caveat still reaches a clone.
      ITEM-level                     `refs/plm/item` (§3) — it belongs to every
                                     revision, so it belongs to neither tree.
    """
    from . import tags as tagsmod
    if rev is None:
        # Item master data: one commit on the item ref, not N on the drafts.
        out = tagsmod.set_tag(conn, item["id"], tag, note=note, actor=actor)
        t0 = tagsmod.normalize(tag)
        _with_git(out, item_fields_commit(
            conn, item, {}, actor=actor,
            message=f"{item['number']}: tag {t0}"
                    + (f"\n\n{note}\n" if note else "\n")))
        return out
    fz = frozen_note(conn, rev)
    if fz:
        t = tagsmod.normalize(tag)
        res = note_write(
            conn, fz, rev, lambda n: n["tags"].append(t)
            if t not in n["tags"] else None, actor=actor,
            message=(f"Tag {fz['number']} v{rev['version']}: {t}"
                     + (f"\n\n{note}" if note else "")
                     + "\n\nThe revision is frozen behind an immutable tag, so "
                       "the caveat is a note on the\nrelease commit (docs §5) "
                       "rather than a change to what was approved.\n"))
        if note:
            tagsmod.set_tag(conn, item["id"], t, revision_id=rev["id"],
                            note=note, actor=actor)
        row = conn.execute(
            "SELECT * FROM tags WHERE item_id=? AND COALESCE(revision_id,0)=? "
            "AND tag=?", (item["id"], rev["id"], t)).fetchone()
        out = tagsmod._decorate(dict(row)) if row else {"tag": t}
        return {**out, "git": res}
    g = git_draft(conn, rev, require=False) if rev else {}
    if not g:
        out = tagsmod.set_tag(conn, item["id"], tag,
                              revision_id=rev["id"] if rev else None,
                              note=note, actor=actor)
        if rev:
            out["git"] = {"note": "recorded in the registry — this revision is "
                                  "not an editable draft in its repo"}
        return out
    t = tagsmod.normalize(tag)
    if t in (g["doc"].get("tags") or []):
        return tagsmod.set_tag(conn, item["id"], tag, revision_id=rev["id"],
                               note=note, actor=actor)
    g["doc"]["tags"] = sorted(set(g["doc"].get("tags") or []) | {t})
    res = git_write(conn, g, rev, actor=actor,
                    message=f"Tag {g['number']} v{rev['version']}: {t}"
                            + (f"\n\n{note}\n" if note else ""))
    if note:
        # The tag is the manifest's; the NOTE is the conversation about it, and
        # the conversation stays in the registry (§2).
        tagsmod.set_tag(conn, item["id"], t, revision_id=rev["id"], note=note,
                        actor=actor)
    row = conn.execute(
        "SELECT * FROM tags WHERE item_id=? AND COALESCE(revision_id,0)=? "
        "AND tag=?", (item["id"], rev["id"], t)).fetchone()
    out = tagsmod._decorate(dict(row)) if row else {"tag": t}
    return {**out, "git": res}


def clear_tag(conn: sqlite3.Connection, item, tag: str, *,
              rev: dict | None = None, actor: str = "") -> dict:
    """Remove a tag — which, for a caveat, is how it is RESOLVED."""
    from . import tags as tagsmod
    t = tagsmod.normalize(tag)
    fz = frozen_note(conn, rev) if rev else {}
    if fz and t in (gitnote_of(fz).get("tags") or []):
        res = note_write(
            conn, fz, rev,
            lambda n: n.update({"tags": [x for x in n["tags"] if x != t]}),
            actor=actor,
            message=f"Clear {t} on {fz['number']} v{rev['version']}\n\n"
                    "Resolving a caveat is itself a fact about the frozen "
                    "revision, so it is a note\ncommit like the caveat was "
                    "(docs §5).\n")
        tagsmod.clear_tag(conn, item["id"], t, revision_id=rev["id"])
        return {"removed": True, "tag": t, "git": res}
    g = git_draft(conn, rev, require=False) if rev else {}
    if g and t in (g["doc"].get("tags") or []):
        g["doc"]["tags"] = [x for x in g["doc"]["tags"] if x != t]
        res = git_write(conn, g, rev, actor=actor,
                        message=f"Clear {t} on {g['number']} v{rev['version']}")
        return {"removed": True, "tag": t, "git": res}
    gone = tagsmod.clear_tag(conn, item["id"], t,
                             revision_id=rev["id"] if rev else None)
    out = {"removed": gone, "tag": t}
    if rev is None and gone:
        _with_git(out, item_fields_commit(
            conn, item, {}, actor=actor,
            message=f"{item['number']}: clear tag {t}\n"))
    return out


def gitnote_of(fz: dict) -> dict:
    from . import gitnotes
    return gitnotes.read(fz["repo"], fz["commit"])


def _rel_entry(rec: dict) -> dict:
    """A relation record as the `[[relations]]` / note line that declares it."""
    e = {"type": rec["type"], "item": rec["to"]}
    if rec["to_version"]:
        e["version"] = rec["to_version"]
        if rec.get("to_commit"):
            e["commit"] = rec["to_commit"]
    if rec["note"]:
        e["note"] = rec["note"]
    if rec.get("result"):
        e["result"] = rec["result"]
    if rec.get("result_note"):
        e["result_note"] = rec["result_note"]
    if rec.get("measured"):
        e["measured"] = dict(sorted(rec["measured"].items()))
    return e


def _same_line(r: dict, rec: dict) -> bool:
    return (r.get("type") == rec["type"] and r.get("item") == rec["to"]
            and str(r.get("version") or "") == (rec["to_version"] or ""))


def _claimant_rev(conn: sqlite3.Connection, rec: dict) -> dict | None:
    """The claimant revision of a revision- or note-home record, as a dict with
    its number — `relations.claimant`, the one answer."""
    from . import relations as rel
    row = conn.execute("SELECT * FROM relations WHERE id=?",
                       (rec["id"],)).fetchone()
    c = rel.claimant(conn, row) if row else {"revision_id": None}
    if not c["revision_id"]:
        return None
    r = conn.execute("SELECT * FROM revisions WHERE id=?",
                     (c["revision_id"],)).fetchone()
    return dict(r) | {"number": rec["from"]} if r else None


def _frozen_in_git(conn: sqlite3.Connection, rev: dict | None) -> bool:
    return bool(rev and str(rev.get("state") or "") not in ("DRAFT", "IN_REVIEW")
                and git_origin(conn, rev))


def _refuse_frozen_claim(rec: dict, rev: dict, what: str) -> None:
    raise OpsError(
        f"{rev['number']} v{rev['version']} is {rev['state']}: its tree is "
        f"behind an immutable tag, and a {rec['type']} claim lives IN that "
        f"tree (home: the claimant revision's plm.toml). {what} would revise a "
        "released revision, which attaching or changing a claim must never "
        "do.\n"
        f"  revise {rev['number']} — the new draft carries its claims forward, "
        "and there the claim is editable — or\n"
        "  record a fact that arrived after release as a note-home relation "
        "(installed_on).", 409)


def _item_claim_commit(conn: sqlite3.Connection, number: str, message: str,
                       actor: str) -> dict:
    """An item-home claim reaches `refs/plm/item` (stage 10).

    Only on an item that has CROSSED (its repo is live): a generated repo is a
    projection the sync regenerates — item ref included — after every legacy
    write, and committing to it would mark it live and freeze it out of that
    sync for good."""
    from . import gitwrite
    item = _item(conn, number)
    try:
        if not gitwrite.is_live(gitwrite.repo_path(number)):
            return {}
    except gitwrite.WriteError:
        return {}
    return item_fields_commit(conn, item, {}, actor=actor, message=message)


def add_relation(conn: sqlite3.Connection, type_: str, from_number: str,
                 to_number: str, *, from_version: str = "",
                 to_version: str = "", note: str = "", actor: str = "",
                 result: str = "", result_note: str = "",
                 measured: dict | None = None) -> dict:
    """Link two items (plm/relations.py owns the registry and every refusal).

    WHERE IT IS WRITTEN is the registry's `home`, never a branch here
    (stage 10, relations.HOMES):

      revision  a `[[relations]]` line on the claimant revision's draft branch,
                with the target's version AND commit. A frozen git-origin
                claimant is refused: a claim in a released tree would revise it.
      item      the registry row, and `item.toml` on `refs/plm/item` once the
                item has crossed (before that, the sync carries it).
      note      a `[[<note_key>]]` entry in the claimant revision's
                refs/notes/plm note — a post-release fact; the tag does not move.

    Outside `write` (and on a legacy revision) every one of them is the
    registry row, exactly as on master. The inverse is never written: "to
    record that X verifies Y, write it in X".
    """
    from . import relations as rel
    rec = rel.add(conn, type_, from_number, to_number,
                  from_version=from_version, to_version=to_version, note=note,
                  actor=actor, result=result, result_note=result_note,
                  measured=measured)
    h = rel.home(rec["type"])
    title = (f"{rec['from']}" + (f" v{rec['from_version']}"
                                 if rec["from_version"] and h != "item" else "")
             + f" {rec['label']} {rec['to']}"
             + (f" v{rec['to_version']}" if rec["to_version"] else ""))
    if h == "item":
        return _with_git(rec, _item_claim_commit(conn, rec["from"], title,
                                                 actor))
    rev = _claimant_rev(conn, rec)
    if not rev:
        return rec           # no claimant revision yet: a registry row
    if h == "note":
        if str(rev.get("state") or "") in ("DRAFT", "IN_REVIEW") \
                and git_origin(conn, rev) and git_writes(conn, rev):
            raise OpsError(
                f"{rec['type']} is a post-release fact (home: a note on the "
                f"revision), and {rev['number']} v{rev['version']} is "
                f"{rev['state']} — nothing has been built from it yet. Submit "
                "it, then record where the built one is.", 409)
        fz = frozen_note(conn, rev)
        if not fz:
            return rec
        key = rel.spec(rec["type"])["note_key"]
        line = _rel_entry(rec)
        line.pop("type", None)
        res = note_write(
            conn, fz, rev,
            lambda n: n.update({key: [x for x in (n.get(key) or [])
                                      if not (x.get("item") == line["item"]
                                              and str(x.get("version") or "")
                                              == str(line.get("version") or ""))]
                                + [line]}),
            actor=actor,
            message=f"{title}\n\nA post-release fact, so a NOTE on the "
                    "revision's commit: the tag and its tree are unchanged "
                    "(stage 10, relations.HOMES).\n")
        return {**rel.record(conn, rec["id"]), "git": res}
    if _frozen_in_git(conn, rev) and git_writes(conn, rev):
        _refuse_frozen_claim(rec, rev, "Adding one")
    g = git_draft(conn, rev, require=False)
    if not g:
        return rec
    g["doc"]["relations"] = [
        r for r in (g["doc"].get("relations") or [])
        if not _same_line(r, rec)] + [_rel_entry(rec)]
    res = git_write(conn, g, rev, actor=actor, message=title)
    # The row was reconciled by the indexer (same id — relations are matched,
    # not rewritten), so hand back what exists now.
    row = conn.execute("SELECT id FROM relations WHERE id=?",
                       (rec["id"],)).fetchone() or conn.execute(
        """SELECT r.id FROM relations r JOIN items t ON t.id=r.target_item_id
           WHERE r.item_id=(SELECT id FROM items WHERE number=?)
             AND r.relation=? AND t.number=? AND r.revision_id=?
           ORDER BY r.id DESC LIMIT 1""",
        (rec["from"], rec["type"], rec["to"], rev["id"])).fetchone()
    return {**(rel.record(conn, row["id"]) if row else rec), "git": res}


def remove_relations_between(conn: sqlite3.Connection, type_: str,
                             from_number: str, to_number: str, *,
                             actor: str = "") -> list[dict]:
    """Unlink by the pair instead of by id — through the same verb, so a
    revision-level link still leaves the manifest in a commit."""
    from . import relations as rel
    a, b = rel._item(conn, from_number), rel._item(conn, to_number)
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM relations WHERE relation=? AND item_id=? AND "
        "target_item_id=?", (type_, a["id"], b["id"]))]
    if not ids:
        raise rel.RelationError(
            f"{from_number} has no {type_} relation to {to_number}", 404)
    return [remove_relation(conn, i, actor=actor) for i in ids]


def remove_relation(conn: sqlite3.Connection, relation_id: int, *,
                    actor: str = "") -> dict:
    """Unlink, from the claim's home (the mirror of `add_relation`): a line out
    of the claimant draft's plm.toml, an entry out of the revision's note, or
    the row (+ the item ref, once crossed). A claim in a FROZEN git-origin tree
    is refused — it is part of what was released."""
    from . import relations as rel
    rec = rel.record(conn, relation_id)
    h = rel.home(rec["type"])
    rev = _claimant_rev(conn, rec) if h != "item" else None
    if h == "revision" and _frozen_in_git(conn, rev) and git_writes(conn, rev):
        _refuse_frozen_claim(rec, rev, "Removing it")
    rel.remove(conn, relation_id, actor=actor)
    title = (f"{rec['from']} no longer {rec['label']} {rec['to']}"
             + (f" v{rec['to_version']}" if rec["to_version"] else ""))
    if h == "item":
        return _with_git(rec, _item_claim_commit(conn, rec["from"], title,
                                                 actor))
    if not rev:
        return rec
    if h == "note":
        fz = frozen_note(conn, rev)
        if not fz:
            return rec
        key = rel.spec(rec["type"])["note_key"]
        res = note_write(
            conn, fz, rev,
            lambda n: n.update({key: [
                x for x in (n.get(key) or [])
                if not (x.get("item") == rec["to"]
                        and str(x.get("version") or "")
                        == (rec["to_version"] or ""))]}),
            actor=actor, message=f"{title}\n")
        return {**rec, "git": res}
    g = git_draft(conn, rev, require=False)
    if not g:
        return rec
    keep = [r for r in (g["doc"].get("relations") or [])
            if not _same_line(r, rec)]
    if len(keep) == len(g["doc"].get("relations") or []):
        return rec
    g["doc"]["relations"] = keep
    res = git_write(conn, g, rev, actor=actor, message=title)
    return {**rec, "git": res}


def set_relation_result(conn: sqlite3.Connection, relation_id: int, *,
                        result: str = "", result_note: str = "",
                        measured: dict | None = None,
                        actor: str = "") -> dict:
    """Record what a coupon FOUND — `result` pass | fail | pending, a sentence,
    and a free `measured` table — on the claim that says what it tested.

    A `results` type is revision-home, so the outcome is written where the
    claim is: the claimant draft's plm.toml line (one commit), or the row on a
    legacy revision. On a FROZEN git-origin claimant (print the coupon, release
    it, THEN measure — the common case) it is a `[[results]]` entry in the
    revision's refs/notes/plm note (stage 12): the tree keeps the claim, the
    note says how it came out, the tag does not move, and the indexer overlays
    it onto the row — so `verified-by-coupon` and release attest-by-reference
    read it like any other result.
    """
    import json
    from . import relations as rel
    rec = rel.record(conn, relation_id)
    if not rel.spec(rec["type"])["results"]:
        raise OpsError(f"{rec['type']} does not carry a result — only "
                       "a registry type with `results` does (coupon_for)", 400)
    try:
        rel.check_result(rec["type"], result, measured)
    except rel.RelationError as exc:
        raise OpsError(str(exc), exc.code) from None
    rev = _claimant_rev(conn, rec)
    fz = (frozen_note(conn, rev)
          if _frozen_in_git(conn, rev) and git_writes(conn, rev) else {})
    if rev and not git_writes(conn, rev):
        _stranded(conn, rev)        # a git-origin claimant outside `write`
    m = json.dumps(measured, sort_keys=True) if measured else ""
    conn.execute("UPDATE relations SET result=?, result_note=?, measured=? "
                 "WHERE id=?", (result, result_note, m, relation_id))
    item_id = conn.execute("SELECT item_id FROM relations WHERE id=?",
                           (relation_id,)).fetchone()[0]
    db.add_event(conn, "relation.result", item_id=item_id, actor=actor,
                 data={"relation": relation_id, "type": rec["type"],
                       "from": rec["from"], "to": rec["to"],
                       "to_version": rec["to_version"], "result": result,
                       "result_note": result_note})
    if fz:
        # A result that arrived after release is a NOTE on the frozen commit
        # (`[[results]]`), never a revise; note_write re-indexes, and the
        # overlay (gitindex._index_relations) puts it on this row.
        res = note_write(
            conn, fz, rev,
            lambda n: rel.put_result(n, rec, result=result,
                                     result_note=result_note,
                                     measured=measured),
            actor=actor,
            message=(f"{rec['from']} v{rev['version']}: {result or 'no result'}"
                     f" for {rec['to']} v{rec['to_version']}\n\nRecorded after"
                     " release, as a refs/notes/plm note: the tag does not "
                     "move.\n" + (f"\n{result_note}\n" if result_note else "")))
        return {**rel.record(conn, relation_id), "git": res}
    rec = rel.record(conn, relation_id)
    g = git_draft(conn, rev, require=False) if rev else {}
    if not g:
        return rec
    lines = list(g["doc"].get("relations") or [])
    if not any(_same_line(r, rec) for r in lines):
        lines.append(_rel_entry(rec))
    for r in lines:
        if _same_line(r, rec):
            for k in ("result", "result_note", "measured"):
                r.pop(k, None)
            if not r.get("commit") and rec.get("to_commit"):
                r["commit"] = rec["to_commit"]
            if result:
                r["result"] = result
            if result_note:
                r["result_note"] = result_note
            if measured:
                r["measured"] = dict(sorted(measured.items()))
    g["doc"]["relations"] = lines
    res = git_write(
        conn, g, rev, actor=actor,
        message=(f"{rec['from']} v{rev['version']}: {result or 'no result'} "
                 f"for {rec['to']} v{rec['to_version']}"
                 + (f"\n\n{result_note}\n" if result_note else "")))
    return {**rel.record(conn, relation_id), "git": res}


# -- item-level master data: `refs/plm/item` (§3) ------------------------------
#
# A name, a project, a product flag, item-level tags and item-level relations:
# facts about the ITEM, not about any one revision. Stage 3b wrote them to the
# item row and then to the manifest of EVERY OPEN DRAFT — a commit each — and
# left the open question in the design notes, because a fact whose home is "all of
# them" has no home. Two things were wrong with it: a rename cost N commits, and
# `gitindex --rebuild` over an old tag could re-assert the name that tag shipped
# under.
#
# Stage 3c answers it: the facts live on their own ref, `refs/plm/item`, one
# commit per change (`plm/itemref.py`). `plm.toml` keeps `number`/`name`/`kind`
# as a DENORMALISED SNAPSHOT so a zip of a tag still knows what it is, and the
# release commit refreshes it — a disagreement is a lint, never a refusal.

def open_git_drafts(conn: sqlite3.Connection, item) -> list[tuple[dict, dict]]:
    """[(revision, git draft)] for every DRAFT of this item that has a branch."""
    out = []
    for row in conn.execute(
            "SELECT * FROM revisions WHERE item_id=? AND state='DRAFT' "
            "ORDER BY vmaj DESC, vmin DESC, vpat DESC", (item["id"],)):
        rev = dict(row) | {"number": item["number"]}
        g = git_draft(conn, rev, require=False)
        if g:
            out.append((rev, g))
    return out


def item_fields_commit(conn: sqlite3.Connection, item, fields: dict, *,
                       message: str, actor: str = "") -> dict:
    """Record an item-level change — ONE commit on `refs/plm/item`. `{}` when
    the item's project is not in `write` (the row is the record, as on master)."""
    from . import itemref
    if not git_writes(conn, item):
        return {}
    return itemref.commit_fields(conn, item, fields, message=message,
                                 actor=actor or "plm")


def draft_fields_commit(conn: sqlite3.Connection, item, fields: dict, *,
                        message: str, actor: str = "") -> dict:
    """Carry a change into every open draft's `plm.toml` — the OLD shape, kept
    for the one field that is not a snapshot.

    `number` is checked HARD: `gitvalidate` refuses a tree whose `plm.toml`
    claims a number other than the repository's, because that is what "plm owns
    identity" means on the wire. So a renumber cannot leave the drafts saying the
    old one — they would become unpushable, which is a worse answer than N
    commits. Everything else about an item is a snapshot and goes to
    `refs/plm/item` (§3).
    """
    if not git_writes(conn, item):
        return {}
    done = []
    for rev, g in open_git_drafts(conn, item):
        if all(g["doc"].get(k) == v for k, v in fields.items()):
            continue
        g["doc"].update(fields)
        done.append({"version": rev["version"],
                     **git_write(conn, g, rev, actor=actor, message=message)})
    if not done:
        return {"note": "no open draft to carry it into; a released tree is a "
                        "record and does not change."}
    return {"commits": done}


# -- a new item is a new repository -------------------------------------------
#
# §6d: "New part — `create_item` allocates the number and creates the repo from
# a template. Number ownership means no push-to-create." So identity stays a
# database decision (plm mints the number; `validate_number` is the law) and the
# repo is created in the same breath, from a template that is four files:
#
#   plm.toml        the manifest, at v0.1.0 DRAFT — the first thing a clone reads
#   README.md       the item page (§6c)
#   .gitattributes  merge=binary for meshes (§5.3 — binary CAD does not merge)
#   .gitignore      the build output nobody wants committed
#
# That first commit lands on `draft/v0.1.0` and is INDEXED, so the item is born
# with a v0.1.0 DRAFT and every write after it — attributes, files, BOM lines —
# has a branch to commit onto with no special case for "the first one". See
# the design notes for why a template commit with no revision behind it was
# the worse of the two answers.
#
# Purchased and hardware items get no repo (§5.4 — not repo-per-screw): they are
# one TOML each inside catalog.git, and writing into the catalog from an API
# call is not converted yet .

# Stage 17: the template is plm/lfs.py's — meshes are Git LFS at any size
# (`filter=lfs`, still `merge=binary`: §5.3), docs/sources plain. One home, so
# create_item, split and plm's own commits write the same file.
from . import lfs as _lfs  # noqa: E402
GITATTRIBUTES = _lfs.gitattributes()

GITIGNORE = b"""# Generated output belongs to the release (docs/PLM_GIT_FIRST.md \xc2\xa76b),
# not to every commit.
*.gcode
*.3mf.tmp
__pycache__/
.DS_Store
build/
out/
"""


def new_item_repo(conn: sqlite3.Connection, number: str, *, name: str,
                  kind: str, project: str, actor: str = "") -> dict:
    """Create the item's repository and its first commit. Returns the git block,
    or {} for a kind that has no repo of its own."""
    from . import gitindex, gitwrite, manifest as mf, split
    if kind in mf.CATALOG_KINDS:
        return new_catalog_entry(conn, number, name=name, kind=kind,
                                 project=project, actor=actor)
    repo = gitwrite.REPOS / f"{number}.git"
    if (repo / "HEAD").exists():
        return {"note": f"{number} already has a repository"}
    split.init_bare(repo)
    doc = mf.parse(f'schema = {mf.SCHEMA_VERSION}\n')
    doc.update({"number": number, "name": name, "kind": kind,
                "project": project or "", "version": "0.1.0", "state": "DRAFT",
                "date": time.strftime("%Y-%m-%d"), "author": actor or "plm",
                "summary": f"Registered {number} — {name}"})
    readme = (f"# {number} — {name}\n\n"
              f"| | |\n|---|---|\n| Kind | {kind} |\n"
              f"| Project | {project or '—'} |\n| This ref | v0.1.0 (DRAFT) |\n"
              "\nEverything plm knows about this revision is in `plm.toml`.\n"
              "Releases are annotated tags (`git tag`); drafts are `draft/*`\n"
              "branches; BOM children are submodules under `parts/`.\n\n"
              "Nothing has been checked in yet — clone it, add the geometry and\n"
              "its source, and push:\n\n"
              "```\n"
              f"git clone <clone_url>\n"
              f"cd {number} && git switch draft/v0.1.0\n"
              "git add . && git commit -m 'First geometry'\n"
              "git push origin HEAD:refs/for/main\n"
              "```\n\n"
              # stage 16: plm's page, regenerated by the first release commit
              # (split.README_MARKER) — delete this line to make it yours
              f"{split.README_MARKER}\n")
    branch = "refs/heads/draft/v0.1.0"
    res = gitwrite.commit(
        number, branch, manifest_doc=doc, new_branch=True,
        changes={"README.md": readme.encode(),
                 ".gitattributes": GITATTRIBUTES,
                 ".gitignore": GITIGNORE},
        actor=actor or "plm", origin="api",
        message=(f"Register {number} — {name}\n\n"
                 f"plm allocated this number and created its repository "
                 f"(docs/PLM_GIT_FIRST.md §6d).\nv0.1.0 is a DRAFT until a "
                 "human approves it; nothing here has been released.\n"))
    gitwrite._git(repo, "symbolic-ref", "HEAD", branch)
    gitwrite._git(repo, "config", "plm.number", number)
    gitwrite._git(repo, "config", "plm.kind", kind)
    (repo / "description").write_text(f"{number} — {name}\n")
    # The item manifest is written FIRST-CLASS, not derived later: from here on
    # `refs/plm/item` is where the item's name and kind live (§3), and an item
    # born without one would read its identity out of its own first tree.
    from . import itemref
    item_ref = itemref.write(
        repo, {"schema": itemref.SCHEMA, "number": number, "name": name,
               "kind": kind, "project": project or "", "product": False,
               "tags": [], "relations": []},
        message=(f"Register {number} — {name}\n\nItem-level facts live on this "
                 "ref (docs/PLM_GIT_FIRST.md §3): name, kind,\nproject, the "
                 "product flag, item-level tags and relations. One commit per\n"
                 "change, whatever the branch topology.\n"),
        actor=actor or "plm")
    index_draft(conn, number, res["commit"], ref=branch,
                          actor=actor or "plm", origin="api", state="DRAFT")
    return {"commit": res["commit"], "branch": branch, "backend": "git",
            "created": True, "version": "0.1.0",
            "item_ref": {"ref": itemref.REF, "commit": item_ref.get("commit")}}


# -- a new purchased item is a FILE in catalog.git (§4, §5.4) -----------------
#
# Not repo-per-screw. A `purchased` or `hardware` item is one `<NUMBER>.toml`
# inside the single `catalog.git` that every assembly pins as ONE submodule, so
# "create the item" means "commit the file". Until stage 3c nothing wrote into
# that repo from an API call, which made a purchased item created today
# registry-only until the next `split` — the last design fact living outside git.
#
# The state it is born in is DRAFT, and deliberately so: `create_item` mints
# identity, not a decision. A screw has no geometry to review, so nothing
# promotes it; `checkin` is what states what was actually bought. What it must
# NOT do is mint a released state with no approval record, which is the same rule
# §1 applies to `checkin` and for the same reason.

def new_catalog_entry(conn: sqlite3.Connection, number: str, *, name: str,
                      kind: str, project: str, actor: str = "") -> dict:
    """Register a catalog item's first revision and commit its TOML."""
    from . import catalog as cat
    item = db.resolve_item(conn, number)
    rev_id = db.upsert_revision(
        conn, item["id"], "0.1.0", state="DRAFT",
        summary=f"Registered {number} — {name}", author=actor or "plm",
        date=time.strftime("%Y-%m-%d"))
    db.refresh_latest(conn, item["id"])
    try:
        res = cat.write_entry(
            conn, item, rev_id, actor=actor or "plm",
            message=(f"Register {number} — {name}\n\n"
                     f"A {kind} item is one TOML in this repo, not a repository "
                     "of its own\n(docs/PLM_GIT_FIRST.md §5.4). An assembly that "
                     "buys it pins `catalog/` and its\n`[[bom]]` line points at "
                     f"`catalog/{number}.toml`.\n"))
    except Exception as exc:  # noqa: BLE001 — gitwrite.WriteError and friends
        raise OpsError(f"could not write {number} into catalog.git: {exc}",
                       500) from exc
    db.add_event(conn, "catalog.write", item_id=item["id"], revision_id=rev_id,
                 actor=actor, data={"number": number, "git": res})
    return {**res, "version": "0.1.0"}


def catalog_write(conn: sqlite3.Connection, item, rev_id: int, *, message: str,
                  actor: str = "") -> dict:
    """Commit a catalog item's current state into `catalog.git`. `{}` if it is
    not a catalog item — the caller then takes its ordinary path."""
    from . import catalog as cat, manifest as mf
    if item["kind"] not in mf.CATALOG_KINDS:
        return {}
    res = cat.write_entry(conn, item, rev_id, message=message,
                          actor=actor or "plm")
    db.add_event(conn, "catalog.write", item_id=item["id"], revision_id=rev_id,
                 actor=actor, data={"number": item["number"], "git": res})
    return res


def catalog_revision(conn: sqlite3.Connection, rev: dict) -> dict:
    """The (item, rev_id) of a catalog revision, or {} — the `git_origin` of §4.

    Catalog items have no repo of their own, so `git_origin` is always {} for
    them; this is the question the catalog verbs ask instead.
    """
    from . import manifest as mf
    item = conn.execute(
        "SELECT i.* FROM items i JOIN revisions r ON r.item_id=i.id "
        "WHERE r.id=?", (rev["id"],)).fetchone()
    if not item or item["kind"] not in mf.CATALOG_KINDS:
        return {}
    if not git_writes(conn, item):
        return {}
    from . import catalog as cat
    if not cat.repo_path():
        return {}
    return {"item": item, "rev_id": rev["id"], "number": item["number"]}


# -- identity moves the repository too -----------------------------------------

def rename_repo(old_number: str, new_number: str) -> dict:
    """Move `<OLD>.git` to `<NEW>.git` and leave a redirect behind.

    A repo's directory name IS the item number — `gitvalidate` refuses a
    `plm.toml` that claims a different one, which is how "plm owns identity"
    survives contact with a clone. So a renumber moves the directory, and the
    old name becomes a redirect file the git server answers 301 from: every
    clone anybody already has keeps working, which is exactly what
    `items.former_numbers` promises on the registry side (§4, "former numbers
    redirect, like a GitHub rename").
    """
    from . import gitwrite, safegit
    root = gitwrite.REPOS
    # ⛔ THE GATE (L2). `_rename_repo_dir` does `src.rename(dst)` — a filesystem
    # move built from two caller-supplied strings, which the review found with
    # no validation of either. Both go through `safegit.safe_number` here, at
    # the one place that constructs the paths.
    try:
        src = safegit.repo_dir(root, old_number)
        dst = safegit.repo_dir(root, new_number)
    except safegit.SafeError as exc:
        raise OpsError(str(exc), exc.code) from None
    if not (src / "HEAD").exists():
        return {"renamed": False,
                "note": f"{old_number} has no repository to move"}
    return _rename_repo_dir(src, dst, root, old_number, new_number)


def rename_catalog_entry(conn: sqlite3.Connection, old_number: str,
                         new_number: str, *, actor: str = "") -> dict:
    """The catalog half of a renumber: `<OLD>.toml` becomes `<NEW>.toml`.

    The file name IS the number here, exactly as a repo's directory name is for
    a design item, so leaving the old file behind would give one screw two
    catalogue entries and two `git log` histories.
    """
    from . import catalog as cat
    return cat.rename_entry(
        conn, old_number, new_number, actor=actor or "plm",
        message=(f"Renumber {old_number} -> {new_number}\n\n"
                 "plm owns identity: the file name is the number.\n"))


def _rename_repo_dir(src, dst, root, old_number: str, new_number: str) -> dict:
    from . import gitwrite
    if (dst / "HEAD").exists():
        raise OpsError(f"{new_number}.git already exists under {root} — "
                       "refusing to move a repository onto another", 409)
    src.rename(dst)
    (root / f"{old_number}.git.redirect").write_text(f"{new_number}.git\n")
    gitwrite._git(dst, "config", "plm.number", new_number)
    return {"renamed": True, "repo": dst.name, "redirect_from": src.name}


def _fork_git(conn: sqlite3.Connection, src, src_rev, base: dict, *,
              new_number: str, new_id: int, new_name: str, reason: str,
              actor: str, fields: dict, idempotency_key: str) -> dict:
    """fork, as `git clone --bare` plus one commit. See `fork`.

    WHAT COMES ACROSS AND WHAT DOES NOT. The commits come: that is the point of
    a fork, and `git log` in the new number shows where the design came from.
    The source's RELEASE TAGS do not — `v0.8.0` is a release of the number that
    made it, and a fork that carried it would be claiming somebody else's
    approval as its own (and would make its own first version illegal, since the
    bump rule reads the tags). The fork point is kept as
    `refs/plm/forked-from/<SOURCE>` and as the `derived_from` relation, which is
    where provenance belongs.
    """
    from . import gitindex, gitwrite, idem, safegit
    try:
        dst = safegit.repo_dir(gitwrite.REPOS, new_number)
    except safegit.SafeError as exc:
        raise OpsError(str(exc), exc.code) from None
    src_repo = base["repo"]
    if (dst / "HEAD").exists():
        raise OpsError(f"{new_number}.git already exists under "
                       f"{gitwrite.REPOS}", 409)
    try:
        # `clone` has no repository of its own, so it is the one call that
        # passes `repo=None`; it still gets the shared timeout and clean env.
        safegit.git(None, "clone", "--bare", "--quiet", "--end-of-options",
                    str(src_repo), str(dst),
                    timeout=safegit.TIMEOUT_LONG_S)
    except safegit.SafeError as exc:
        raise OpsError(f"could not clone {src['number']}'s repository: "
                       f"{exc}", 500) from None
    gitwrite._git(dst, "config", "--unset-all", "remote.origin.url",
                  check=False)
    gitwrite._git(dst, "update-ref", f"refs/plm/forked-from/{src['number']}",
                  base["commit"])
    for line in gitwrite._git(dst, "for-each-ref", "refs/tags/", "refs/heads/",
                              "--format=%(refname)").splitlines():
        if line.strip():
            gitwrite._git(dst, "update-ref", "-d", line.strip(), check=False)
    gitwrite._git(dst, "config", "plm.number", new_number)
    gitwrite._git(dst, "config", "plm.kind", src["kind"])
    (dst / "description").write_text(f"{new_number} — {new_name}\n")

    doc = gitwrite.read_manifest(dst, base["commit"])
    doc.update({"number": new_number, "name": new_name, "version": "0.1.0",
                "state": "DRAFT", "maturity": "",
                "date": time.strftime("%Y-%m-%d"), "author": actor or "plm",
                "summary": f"Forked from {src['number']} "
                           f"v{src_rev['version']}"})
    # Provenance in the file as well as in the registry: a clone of the fork
    # should be able to say what it came from without asking plm.
    doc["relations"] = [r for r in (doc.get("relations") or [])
                        if not (r.get("type") == "derived_from"
                                and r.get("item") == src["number"])]
    doc["relations"].append({"type": "derived_from", "item": src["number"],
                             "version": src_rev["version"],
                             **({"note": reason} if reason else {})})
    branch = "refs/heads/draft/v0.1.0"
    out_git = gitwrite.commit(
        new_number, branch, manifest_doc=doc, parent=base["commit"],
        new_branch=True, actor=actor or "plm", origin="api",
        message=(f"Fork {src['number']} v{src_rev['version']} as {new_number}"
                 + (f"\n\n{reason}\n" if reason else "\n\n")
                 + f"History came with it: this commit's parent is "
                   f"{src['number']} v{src_rev['version']}.\nThe source's "
                   "release tags did not — a release belongs to the number "
                   "that earned it.\n"))
    gitwrite._git(dst, "symbolic-ref", "HEAD", branch)
    # A fork is a new NUMBER, so it needs its own item manifest — the source's
    # came along in the clone and would claim the fork is the source (§3).
    from . import itemref
    itemref.write(
        dst, {"schema": itemref.SCHEMA, "number": new_number, "name": new_name,
              "kind": src["kind"], "project": src["project"] or "",
              "product": False, "tags": [],
              "relations": [{"type": "derived_from", "item": src["number"],
                             "version": src_rev["version"],
                             **({"note": reason} if reason else {})}]},
        message=(f"Fork {src['number']} as {new_number} — {new_name}\n\n"
                 "A fork is a new number, so it is a new item: its identity "
                 "starts here.\n"),
        actor=actor or "plm")
    index_draft(conn, new_number, out_git["commit"], ref=branch,
                          actor=actor or "plm", origin="api", state="DRAFT")
    rev = db.find_revision(conn, new_number, "0.1.0")
    from . import context as ctx
    ctx.add(conn, f"Forked from {src['number']} v{src_rev['version']}."
            + (f"\n\n**Why:** {reason}" if reason else ""),
            target_type="revision", target=rev["id"], actor=actor, event=False)
    db.refresh_latest(conn, new_id)
    db.add_event(conn, "fork", item_id=new_id, revision_id=rev["id"],
                 actor=actor, data={"from": src["number"],
                                    "from_version": src_rev["version"],
                                    "git": out_git})
    out = {"number": new_number, "name": new_name, "version": "0.1.0",
           "from": src["number"], "from_version": src_rev["version"],
           "id": new_id, "git": {"commit": out_git["commit"], "branch": branch,
                                 "backend": "git", "cloned_from": src_repo.name,
                                 "forked_at": base["commit"]}}
    if not fields["new_number"]:
        idem.record(conn, op="fork", actor=actor, fields=fields,
                    key=idempotency_key, response=out, item_id=new_id)
    return out
