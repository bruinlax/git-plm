"""Approval policy — who may say yes, to what maturity, in which project.

ONE PLACE DECIDES. Every door that used to ask its own version of the question
("is this actor an approver?", "is this a bot?") asks here instead:
`ops.decide_approval`, `ops.decide_release`, a release workflow's approval
signoff, and the UI's decision bar. If you are about to write
`if user["kind"] == "bot": 403` anywhere else, stop — it belongs in this module,
because a rule with two homes is a rule with two answers.

WHAT THE DATA SAID. `PLM_REVIEW_MODEL.md` §2.3's first table asked for one human
approval at every maturity, prototype included. Measured on production the day
this was written: **446 of 454 non-draft revisions have no approval row at all**,
because bots mint PROTOTYPE directly through `checkin`, and the human gate has
been used about eight times ever. A rule that would need three human clicks a day
on bot iterations is a rule that gets routed around — which is what the 446 are.

So the policy has two independent knobs, and the distinction between them is the
whole design:

  `auto`              does this maturity need a decision AT ALL? Stage 3c
                      shipped "no" at concept/design/prototype. ⭐ Stage 14
                      FLIPPED THE DEFAULT (Geoff: "the agent is asking me to
                      approve a pull request"): every change waits for a human
                      unless its project is in "just build it" mode
                      (`--just-build-it`), where it merges approved by the
                      POLICY, stamped AUTO.
  `approver_kinds`    when a decision IS needed, whose signature counts. Default
                      `("human",)`. Geoff, 2026-09-17: *"we don't need to be hard
                      about bots not approving for now — sometimes I just want
                      things built. That should be a setting somehow."* So it is
                      a setting, not a law.

THE RECORD IS HONEST IN ALL THREE CASES, and that is what makes the setting safe
to have. An approval records `approver` and `approver_kind`, and the tag
annotation, the evidence file and the History stamp all carry it:

    policy   nobody looked; the rule Geoff wrote said this maturity is automatic
    bot      an agent decided, because the project's policy admits agents
    human    a person decided

"Policy approved it" and "the bot approved its own work" are different sentences
about who is accountable, and a ledger that blurred them would be worse than no
ledger.

WHERE A SETTING LIVES (one home, most specific wins):

  1. `POLICY` below — the registry default, per maturity.
  2. `PLM_POLICY_AUTO` / `PLM_POLICY_HUMAN` — comma-separated maturities, for
     flipping `auto` on an instance without a deploy.
  3. `PLM_POLICY_BOT_APPROVALS` — `none` (default) · `released` · `all`, or a
     comma-separated list of maturities at which bots may approve.
  4. the `policy` TABLE — a row per (project, maturity), which is what a human
     edits when one project is different from the rest. Written with
     `python -m plm.policy --project … --target … --bots yes`, read everywhere,
     and exposed read-only at `GET /api/policy`.

There is no HTTP write path on purpose: changing who may approve is a change to
the gate, and a gate that can be widened by the thing it is gating is not one.
"""
from __future__ import annotations

import argparse
import os

#: The literal written into `approvals.approver` for an auto-merge. Not a user:
#: `users.kind` is human|bot and policy is neither, and inventing a third kind
#: would let something query for "the policy user's other decisions", which is
#: not a thing that exists.
APPROVER = "policy"

KINDS = ("human", "bot")

#: target state -> the rule.
#:
#: ⚠️ THERE IS NO `checks` KEY HERE, ON PURPOSE (stage 7). It used to carry one
#: — `("manifest", "pins-released")` at RELEASED — and `plm/release.py`'s
#: workflow registry carried a third copy of the same idea. Three lists, three
#: places to forget. `plm/checks.py::required_for()` is now the only home, and
#: `for_target()` below asks it. If you are about to add a check name to this
#: dict, add it to `checks.CHECKS` and `checks.required_for()` instead.
#: ⭐ STAGE 14 — THE DEFAULT FLIPPED. Geoff, 2026-10-05: "I'd like my
#: workflow to be like the agent is asking me to approve a pull request to main
#: for the project." Below RELEASED a merge used to be automatic by default;
#: now every change waits for a HUMAN by default, and "just build it" is a
#: per-project SETTING (`python -m plm.policy --project X --just-build-it`,
#: which writes `auto = yes` rows at CONCEPT, DESIGN and PROTOTYPE — never at
#: RELEASED). The record stamps are unchanged: AUTO, BOT, or a person.
JUST_BUILD_IT = ("CONCEPT", "DESIGN", "PROTOTYPE")

POLICY: dict[str, dict] = {
    "CONCEPT": {
        "auto": False, "approver_kinds": ("human",),
        "rule": "a human approves (the default since stage 14): the agent asks, "
                "Geoff decides. A project in \"just build it\" mode merges "
                "concepts automatically.",
    },
    "DESIGN": {
        "auto": False, "approver_kinds": ("human",),
        "rule": "a human approves (the default since stage 14). A project in "
                "\"just build it\" mode merges designs automatically.",
    },
    "PROTOTYPE": {
        "auto": False, "approver_kinds": ("human",),
        "rule": "a human approves each change (the default since stage 14) — "
                "one piece of work, one pull request, one decision. A project "
                "in \"just build it\" mode merges prototypes automatically once "
                "they validate.",
    },
    "RELEASED": {
        "auto": False, "approver_kinds": ("human",),
        "rule": "RELEASED is a decision, not a merge (docs/PLM_REVIEW_MODEL.md "
                "§2.1b), and it is normally reached by PROMOTING a revision "
                "that was built and proven.",
    },
    "OBSOLETE": {
        "auto": False, "approver_kinds": ("human",),
        "rule": "retiring a revision is a decision about something other people "
                "may be building against.",
    },
}

#: The answer for a state nobody registered. Deliberately the strict one: a
#: maturity plm has never heard of is not one it may wave through.
FALLBACK = {
    "auto": False, "approver_kinds": ("human",),
    "rule": "no policy is registered for this maturity, so it takes the strict "
            "answer: a human decides. Add a row to plm/policy.py POLICY to "
            "change that.",
}

#: Per-project tightening or loosening in CODE, merged over POLICY and under the
#: database table. Empty today and that is honest — one shop, one policy — but
#: the seam is here so a project that needs a different answer is one entry.
BY_PROJECT: dict[str, dict[str, dict]] = {}


# -- the environment knobs ----------------------------------------------------

def _env_set(name: str) -> set[str]:
    return {s.strip().upper() for s in os.environ.get(name, "").split(",")
            if s.strip()}


def _bot_states() -> tuple[set[str], str]:
    """Which maturities `PLM_POLICY_BOT_APPROVALS` admits bots at, and its text.

    `none` (the default), `released`, `all`, or an explicit comma-separated list
    of maturities. The three words exist because they are what somebody types at
    3 a.m.; the list exists because "released" should not have to mean "and
    everything above it, whatever that turns out to be".
    """
    raw = (os.environ.get("PLM_POLICY_BOT_APPROVALS") or "none").strip()
    low = raw.lower()
    if low in ("", "none", "no", "off", "false", "0"):
        return set(), raw
    if low in ("all", "yes", "on", "true", "1"):
        return {s.upper() for s in POLICY} | {"*"}, raw
    return {s.strip().upper() for s in raw.split(",") if s.strip()}, raw


# -- the database table: one row per (project, maturity) ----------------------

def _row(conn, project: str, state: str):
    """The most specific `policy` row that applies, or None.

    Specificity order: exact project + exact maturity, then project + any
    maturity, then any project + exact maturity, then the global row. A
    registry-wide default that a project may narrow is the same shape
    `plm/relations.py` and `plm/roles.py` use.
    """
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT * FROM policy WHERE (project=? OR project='') "
            "AND (target_state=? OR target_state='')",
            (project or "", (state or "").upper())).fetchall()
    except Exception:  # noqa: BLE001 — the table may predate this instance
        return None
    if not rows:
        return None
    return max(rows, key=lambda r: (bool(r["project"]), bool(r["target_state"])))


def set_project_policy(conn, *, project: str = "", target_state: str = "",
                       bots: str | None = None, auto: str | None = None,
                       note: str = "", actor: str = "") -> dict:
    """Write one policy row. The ONLY write path, and it is not on the HTTP API.

    `bots` is `yes`/`no`/`''` (inherit); `auto` is the same three. `project=''`
    means every project and `target_state=''` means every maturity, so the
    global default is a row like any other.
    """
    from . import db
    if bots not in (None, "", "yes", "no"):
        raise ValueError("bots must be 'yes', 'no' or '' (inherit)")
    if auto not in (None, "", "yes", "no"):
        raise ValueError("auto must be 'yes', 'no' or '' (inherit)")
    kinds = {"yes": "human,bot", "no": "human"}.get(bots or "", "")
    state = (target_state or "").upper()
    old = conn.execute(
        "SELECT id FROM policy WHERE project=? AND target_state=?",
        (project or "", state)).fetchone()
    if old:
        conn.execute(
            "UPDATE policy SET approver_kinds=?, auto=?, note=?, actor=?, "
            "updated_at=? WHERE id=?",
            (kinds, auto or "", note, actor, db.now(), old["id"]))
    else:
        conn.execute(
            "INSERT INTO policy (project, target_state, approver_kinds, auto, "
            "note, actor, updated_at) VALUES (?,?,?,?,?,?,?)",
            (project or "", state, kinds, auto or "", note, actor, db.now()))
    db.add_event(conn, "policy.set", actor=actor,
                 data={"project": project, "target_state": state,
                       "approver_kinds": kinds, "auto": auto or "",
                       "note": note})
    return for_target(project, state or "RELEASED", conn=conn)


def clear_project_policy(conn, *, project: str = "",
                         target_state: str = "") -> int:
    cur = conn.execute("DELETE FROM policy WHERE project=? AND target_state=?",
                       (project or "", (target_state or "").upper()))
    return cur.rowcount or 0


# -- the answer ---------------------------------------------------------------

def for_target(project: str, target_state: str, *, conn=None) -> dict:
    """The effective policy for one (project, maturity).

    Returns `{target_state, auto, approver, approver_kind, approver_kinds,
    checks, rule, bots_may_approve, source}`. `source` names which layer
    decided, because "why can this bot approve?" has to be answerable.
    """
    state = str(target_state or "").upper()
    spec = dict(POLICY.get(state) or FALLBACK)
    spec["approver_kinds"] = tuple(spec.get("approver_kinds") or ("human",))
    source = "registry" if state in POLICY else "fallback"

    proj = (BY_PROJECT.get(project or "") or {}).get(state)
    if proj:
        spec.update(proj)
        source = f"code:{project}"

    bot_states, bot_raw = _bot_states()
    if state in bot_states or "*" in bot_states:
        spec["approver_kinds"] = KINDS
        source = f"env:PLM_POLICY_BOT_APPROVALS={bot_raw}"

    if state in _env_set("PLM_POLICY_AUTO"):
        spec["auto"], source = True, "env:PLM_POLICY_AUTO"
        spec["rule"] = (f"{state} auto-merges because PLM_POLICY_AUTO says so "
                        "on this instance.")
    if state in _env_set("PLM_POLICY_HUMAN"):
        spec["auto"], source = False, "env:PLM_POLICY_HUMAN"
        spec["rule"] = (f"{state} needs a decision because PLM_POLICY_HUMAN "
                        "says so on this instance.")

    row = _row(conn, project, state)
    if row is not None:
        if row["approver_kinds"]:
            spec["approver_kinds"] = tuple(
                k.strip() for k in row["approver_kinds"].split(",") if k.strip())
            source = ("db:" + (row["project"] or "*") + "/"
                      + (row["target_state"] or "*"))
        if row["auto"] in ("yes", "no"):
            spec["auto"] = row["auto"] == "yes"
            source = ("db:" + (row["project"] or "*") + "/"
                      + (row["target_state"] or "*"))
        if row["note"]:
            spec["rule"] = row["note"]

    kinds = tuple(spec["approver_kinds"])
    # ⭐ ONE HOME for "which checks are required" — plm/checks.py. Asked here,
    # asked identically by plm/release.py's workflow, enforced once by
    # ops._finish_release through checks.gate_for(). Imported lazily because
    # checks imports gitindex, which imports this module.
    from . import checks as _checks, manifest as _manifest
    maturity = _manifest.MATURITY_BY_STATE.get(state, state.lower())
    return {"target_state": state, "auto": bool(spec["auto"]),
            "approver": APPROVER if spec["auto"] else "human",
            "approver_kind": "policy" if spec["auto"] else "human",
            "approver_kinds": list(kinds),
            "bots_may_approve": "bot" in kinds,
            "maturity": maturity,
            "checks": _checks.required_for(maturity),
            "rule": spec["rule"],
            "project": project or "", "source": source,
            "label": ("policy: automatic — no decision needed" if spec["auto"]
                      else "policy: humans only" if kinds == ("human",)
                      else "policy: bots may approve")}


def may_approve(conn, project: str, target_state: str, actor: str) -> dict:
    """May `actor` satisfy an approval here? `{ok, kind, spec, why}`.

    THE ASYMMETRY, STATED. A human additionally needs the `approver` role, which
    is how humans have always been admitted and is real user administration. A
    bot needs only the policy, because there is no role administration for bots
    and requiring both would make "let the agents get on with it" a two-step
    operation with a confusing half state. Policy admission IS the grant.
    """
    from . import db
    spec = for_target(project, target_state, conn=conn)
    user = db.get_user(conn, actor) if actor else None
    kind = (user or {}).get("kind") or ""
    if not user:
        return {"ok": False, "kind": "", "spec": spec,
                "why": f"{actor or '(nobody)'} is not a user in plm."}
    if kind not in spec["approver_kinds"]:
        return {"ok": False, "kind": kind, "spec": spec,
                "why": refusal(actor, kind, spec)}
    if kind == "human" and not db.user_has_role(conn, actor, "approver"):
        return {"ok": False, "kind": kind, "spec": spec,
                "why": f"{actor} does not hold the `approver` role."}
    return {"ok": True, "kind": kind, "spec": spec, "why": ""}


def refusal(actor: str, kind: str, spec: dict) -> str:
    """The 403 text. It names the SETTING and how to change it, because
    "agent is not an approver" tells somebody what happened and nothing about
    what to do — and the answer here is usually "flip the policy", which is a
    decision Geoff is allowed to make."""
    where = f"{spec['project'] or 'every project'}/{spec['target_state']}"
    return (
        f"{actor} is a {kind or 'unknown'} and the approval policy for {where} "
        f"admits {', '.join(spec['approver_kinds'])} only.\n"
        f"  effective rule: {spec['rule']}\n"
        f"  where it comes from: {spec['source']}\n"
        "\n"
        "  to let agents approve at this maturity — this is a real setting, not "
        "a workaround:\n"
        f"    docker exec plm python3 -m plm.policy --project "
        f"{spec['project'] or ''} \\\n"
        f"        --target {spec['target_state']} --bots yes --actor geoff \\\n"
        "        --note 'why you decided this'\n"
        "  or, for the whole instance: PLM_POLICY_BOT_APPROVALS="
        f"{spec['target_state'].lower()}\n"
        "\n"
        "  either way the record stays honest: the approval is stored with "
        "approver_kind = bot,\n  and the tag annotation, the evidence file and "
        "the History stamp all say a bot decided.")


def table(project: str = "", *, conn=None) -> list[dict]:
    """The whole policy, as rows — `GET /api/policy` and the decision bar."""
    return [for_target(project, state, conn=conn) for state in POLICY]


def decision_note(spec: dict) -> str:
    """What goes in `approvals.decision_note` and the tag annotation.

    Written to be read by whoever finds it in a year and wants to know whether a
    person looked at this revision. The answer is no, and it says so first.
    """
    return ("Auto-merged by POLICY — no human reviewed this revision.\n"
            f"{spec['rule']}\n"
            f"(policy source: {spec['source']}"
            + (f"; required checks: {', '.join(spec['checks'])}"
               if spec["checks"] else "; no checks are required at this maturity")
            + ")")


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m plm.policy",
        description="Read or set the approval policy. Setting it is deliberately "
                    "not an HTTP route: a gate that the thing it gates can widen "
                    "is not a gate.")
    ap.add_argument("--project", default="", help="'' = every project")
    ap.add_argument("--target", default="", help="maturity; '' = every maturity")
    ap.add_argument("--bots", choices=("yes", "no", ""), default=None,
                    help="may agents satisfy an approval here?")
    ap.add_argument("--auto", choices=("yes", "no", ""), default=None,
                    help="does this maturity merge without any decision?")
    ap.add_argument("--note", default="", help="why — it is shown as the rule")
    ap.add_argument("--actor", default="", help="who is deciding this")
    ap.add_argument("--clear", action="store_true", help="remove the row")
    ap.add_argument("--just-build-it", action="store_true",
                    help="this project merges CONCEPT/DESIGN/PROTOTYPE without "
                         "a decision (auto = yes rows; RELEASED is untouched)")
    ap.add_argument("--human-approves", action="store_true",
                    help="undo --just-build-it: remove those three rows")
    args = ap.parse_args(argv)
    from . import db
    with db.connect() as conn:
        if args.just_build_it or args.human_approves:
            if not args.project:
                ap.error("--just-build-it / --human-approves need --project")
            for st in JUST_BUILD_IT:
                if args.just_build_it:
                    set_project_policy(
                        conn, project=args.project, target_state=st, auto="yes",
                        note=args.note or f"\"just build it\": {st} merges "
                        "without a decision in this project (the record says "
                        "AUTO)", actor=args.actor)
                else:
                    clear_project_policy(conn, project=args.project,
                                         target_state=st)
        elif args.clear:
            n = clear_project_policy(conn, project=args.project,
                                     target_state=args.target)
            print(f"removed {n} policy row(s)")
        elif args.bots is not None or args.auto is not None:
            set_project_policy(conn, project=args.project,
                               target_state=args.target, bots=args.bots,
                               auto=args.auto, note=args.note, actor=args.actor)
        width = max(len(s) for s in POLICY)
        print(f"approval policy — project {args.project or '(every project)'}")
        print("-" * 72)
        for spec in table(args.project, conn=conn):
            who = ("policy (automatic)" if spec["auto"]
                   else "+".join(spec["approver_kinds"]))
            print(f"  {spec['target_state']:<{width}}  {who:<12}  "
                  f"[{spec['source']}]")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
