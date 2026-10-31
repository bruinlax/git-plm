"""Release — the second object. Promotion of a revision that already exists.

docs/PLM_REVIEW_MODEL.md §2.1b is the spec; this is it, built.

    PROPOSAL                            RELEASE
    "is this change right?"             "is this revision ready to build against?"
    acts on a draft/* branch            acts on an EXISTING tag — no file change
    ends in merge + version tag         ends in a STATUS, mirrored to git notes
    policy may auto-merge it (§2)       always a decision

Before this, taking a part from PROTOTYPE to RELEASED cost a fake revision:
`ops.submit_for_release` refuses anything that is not a DRAFT, so the only way
to change a revision's maturity was to mint a new one whose design had not
changed. That is wrong twice over — in Teamcenter terms status is applied TO a
revision, and in git terms you do not commit in order to cut a release. Real life
is: iterate as prototypes, many merged proposals and many pre-release tags, then
**promote the one that was built and proven.**

THE THREE PROMISES THIS MODULE KEEPS, and the demo proves each one:

  1. **No tag moves and no commit lands on `main`.** The tag sha before a
     promotion is the tag sha after it. `vX.Y.Z` stays the one immutable name;
     a release that could be moved is not a release.
  2. **The decision reaches a clone.** It is written into `refs/notes/plm` on
     the release commit as `[status]`, so `git log --notes=plm` says what a
     revision IS now — the thing plain git has no answer for (§2.7).
  3. **The evidence is a file whose hash is reproducible.** "What exactly did we
     release, and who said so?" is one deterministic JSON document: the manifest
     at the tag, the FULL pin closure with every descendant's maturity, the
     proposals and approvals that led here, every signoff, and each file's
     sha256. Its own sha256 goes in the note and on the row.

A RELEASE IS A WORKFLOW, NOT A CLICK. `RELEASE_WORKFLOWS` below is a registry
keyed by the maturity transition, giving the required human approvals (a
quorum) and a **checklist somebody attests to by name** (Teamcenter's do-tasks).
Agents may attest the lines they can prove; whether an agent may satisfy the
APPROVAL quorum is `plm/policy.py`'s answer and nobody else's.

⚠️ THE WORKFLOW DOES NOT LIST ITS OWN CHECKS (stage 7). It used to — every entry
carried `"checks": ["manifest", "pins-released"]` — and so did `plm/policy.py`'s
POLICY, and so did `plm/checks.py::required_for()`. A rule with three homes has
three answers, and they had already drifted (`required_for` asked for
`render-matches-source` at RELEASED; the workflow did not, so a promotion never
saw it). `workflow_for()` now asks `checks.required_for(to_maturity)`, and
`ops._finish_release` refuses on `checks.gate_for()` — one list, one gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import db, gitnotes, gitvalidate, gitwrite, manifest

SCHEMA = 1

#: Where the frozen evidence documents live. Under the runtime root, never in
#: an object store and never in the worktree.
EVIDENCE_DIR = Path(os.environ.get("PLM_EVIDENCE_DIR", "/var/evidence"))


class ReleaseError(RuntimeError):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


# -- the workflow registry ----------------------------------------------------
#
# A registry that GLOSSES rather than gates: a transition nobody
# registered still works, it just gets the generic workflow and says so. What it
# must never do is silently ask for nothing.

RELEASE_WORKFLOWS: dict[tuple[str, str], dict] = {
    ("prototype", "released"): {
        "label": "Prototype → Released",
        "approvals": {"required": 1, "roles": ["approver"]},
        "checklist": [
            "Built/printed at least once at this revision",
            "Fit-checked against mating parts / the assembly",
            "BOM is purchasable (every buy line has a live source)",
            "Instructions reviewed",
        ],
        # line -> the relation type whose PASSING claim on the exact commit
        # may attest it by reference (stage 10). Data, like the checklist.
        "evidence": {
            "Fit-checked against mating parts / the assembly": "coupon_for",
        },
    },
    ("design", "released"): {
        "label": "Design → Released",
        "approvals": {"required": 1, "roles": ["approver"]},
        "checklist": [
            "Built/printed at least once at this revision",
            "BOM is purchasable (every buy line has a live source)",
        ],
    },
    ("released", "obsolete"): {
        "label": "Released → Obsolete",
        "approvals": {"required": 1, "roles": ["approver"]},
        "checklist": [
            "Nothing in production still pins this revision, or the parents "
            "have been told",
        ],
    },
}

GENERIC = {
    "label": "Generic promotion",
    "approvals": {"required": 1, "roles": ["approver"]},
    "checklist": [],
    "generic": True,
}


def workflow_key(from_maturity: str, to_maturity: str) -> str:
    return f"{(from_maturity or '').lower()}->{(to_maturity or '').lower()}"


def workflow_for(from_maturity: str, to_maturity: str) -> dict:
    """The workflow for one transition. Never raises: an unregistered transition
    gets `GENERIC` and is MARKED generic, so the release page can say "nobody has
    written the checklist for this transition yet" instead of implying that one
    approval is the considered answer.

    `checks` is not stored on the entry — it is `checks.required_for(to)`, asked
    here so every reader of a workflow (the release page, the evidence document,
    the MCP) sees the same list the gate will enforce.
    """
    from . import checks as checksmod
    key = ((from_maturity or "").lower(), (to_maturity or "").lower())
    spec = RELEASE_WORKFLOWS.get(key)
    out = dict(spec or GENERIC)
    out["key"] = workflow_key(*key)
    out["from_maturity"], out["to_maturity"] = key
    out["registered"] = spec is not None
    out["checks"] = checksmod.required_for(key[1])
    return out


def registry() -> list[dict]:
    return [workflow_for(a, b) for a, b in RELEASE_WORKFLOWS]


# -- what a revision is, and whether it can be promoted ------------------------

def maturity_of(state: str) -> str:
    return manifest.MATURITY_BY_STATE.get((state or "").upper(),
                                          (state or "").lower())


def state_for_maturity(maturity: str) -> str:
    for state, mat in manifest.MATURITY_BY_STATE.items():
        if mat == (maturity or "").lower():
            return state
    return (maturity or "").upper()


def tag_of(repo: Path, version: str) -> tuple[str, str]:
    """(tag name, the commit it points at) — or ('','') when there is no tag."""
    sha = gitvalidate.releases(repo).get(version, "")
    return (f"v{version}", sha) if sha else ("", "")


# -- the released-children gate (§5.1, by STATUS) ------------------------------

def child_gate(repo: Path, commit: str) -> list[dict]:
    """Every `parts/*` pin at this commit, with the child's LIFECYCLE.

    The proposal gate asks "is this pin on a release tag". A promotion asks the
    stricter question, and it has to: a PROTOTYPE tag is still a tag, and a
    parent that shipped because its child happened to be tagged would make "you
    cannot release above your least-mature child" a rule about bookkeeping.
    `gitindex.revision_status` answers from the note, then the tag annotation,
    then the tree — all three readable from a clone.

    The catalog is reported and never gates (§5.4): a purchased screw has no
    design to release, and requiring one would mean releasing the hardware store.
    """
    from . import gitindex
    out = []
    for path, e in sorted(gitvalidate.tree_entries(repo, commit).items()):
        if e.mode != gitindex.MODE_GITLINK:
            continue
        if path == "catalog":
            out.append({"path": path, "child": "catalog", "pin": e.sha[:10],
                        "commit": e.sha, "version": "", "released": True,
                        "gates": False,
                        "why": "the purchased catalog never gates a release "
                               "(§5.4) — a screw has no design to release"})
            continue
        child = path.split("/", 1)[1] if path.startswith("parts/") else path
        st = gitindex.pin_status(child, e.sha)
        out.append({"path": path, "child": child, "pin": e.sha[:10],
                    "commit": e.sha, "version": st["version"],
                    "on_a_tag": st["released"], "maturity": st["maturity"],
                    "state": st["state"], "status_source": st["status_source"],
                    "released": st["is_released"], "gates": not st["is_released"],
                    "why": ("released" if st["is_released"] else
                            f"{st['state'] or 'not on a release tag'} — "
                            "not RELEASED")})
    return out


def gate_refusal(number: str, version: str, blockers: list[dict]) -> str:
    """The 409. It names the children and the fix, same tone as the §5.1 gate."""
    lines = "".join(
        f"  · {b['path']} @ {b['pin']} — {b['child']} "
        f"v{b['version'] or '?'} is {b['why']}\n" for b in blockers)
    first = blockers[0]
    return (
        f"{number} v{version} cannot be promoted to RELEASED: you cannot "
        "release above your\nleast-mature child (docs/PLM_GIT_FIRST.md §5.1). "
        "These BOM children are pinned at\nrevisions that are not RELEASED:\n"
        + lines
        + "\nRelease the child first — it is the same promotion, one level "
          "down:\n"
          f"  POST /api/items/{first['child']}/releases "
          f'{{"version": "{first["version"] or "<x.y.z>"}", '
          '"to_maturity": "released"}\n'
          "\nThen, if the child's RELEASED revision is a different one, bump "
          "this parent's pin\nand release the result:\n"
          f"  git -C {first['path']} fetch origin --tags && "
          f"git -C {first['path']} checkout v<x.y.z>\n"
          f"  git add {first['path']} && git commit && "
          "git push HEAD:refs/for/main\n"
          "\nThe status is read from each child's `refs/notes/plm` (then its "
          "tag annotation),\nso `git fetch origin refs/notes/plm:refs/notes/plm "
          "&& git log --notes=plm` shows\nyou the same answer plm just used.")


# -- attested BY REFERENCE (stage 10): a claim as checklist evidence ----------
#
# Teamcenter's rule — attaching evidence to a released revision never revises
# it — and the forge's: the claim lives where it was made (the coupon's own
# plm.toml) and the release POINTS at it. A checklist line listed under a
# workflow's `evidence` may be attested with `evidence = "GL-CPN-0001@v0.1.0"`
# instead of a person's word alone, when that claim (a) is of the named type,
# (b) pins the EXACT commit being released (or the commit plm's release commit
# was reviewed as — relations.same_design), and (c) recorded `result = pass`.
# No such claim → the line is attested by hand, as before. The signoff stores
# the reference; the evidence document resolves it, so the frozen file says
# which coupon, at which commit, measured what.

def evidence_options(conn, rel: dict, wf: dict) -> dict:
    """line -> [passing claims that may attest it] for this release's tag."""
    from . import relations
    spec = wf.get("evidence") or {}
    if not spec or not rel.get("tag_sha"):
        return {}
    claims = relations.claims_on(conn, rel["number"], rel["tag_sha"])
    return {line: [c for c in claims if c["type"] == type_
                   and c["result"] == "pass"]
            for line, type_ in spec.items()}


def resolve_evidence(conn, rel: dict, wf: dict, line: str, ref: str) -> dict:
    """The claim `ref` names, if it may attest `line` on this release — else a
    refusal that says what would."""
    opts = evidence_options(conn, rel, wf)
    if line not in (wf.get("evidence") or {}):
        raise ReleaseError(
            f"{line!r} cannot be attested by reference — the workflow lists "
            "evidence only for:\n"
            + ("".join(f"  · {ln} (a passing {t})\n"
                       for ln, t in (wf.get("evidence") or {}).items())
               or "  (no line on this workflow)\n")
            + "Attest it by hand instead.", 400)
    want = ref.strip().replace(" v", "@v")
    for c in opts.get(line) or []:
        if want in (c["ref"], c["from"]):
            return c
    have = ", ".join(c["ref"] for c in opts.get(line) or []) or "none"
    raise ReleaseError(
        f"{ref!r} is not a passing {wf['evidence'][line]} claim on "
        f"{rel['number']} v{rel['version']} at {rel['tag_sha'][:10]}.\n"
        f"Passing claims on this exact commit: {have}.\n"
        "A coupon counts when its own revision declares the claim pinned at "
        "this commit\n(or at the commit this release commit was reviewed as) "
        "with result = \"pass\".", 409)


def evidence_claims(conn, rel: dict, signoffs: list[dict]) -> list[dict]:
    """The claims the signoffs reference, resolved — for the evidence file.
    Deterministic: every value is a registry row or a commit id."""
    from . import relations
    refs = {s.get("evidence") for s in signoffs if s.get("evidence")}
    if not refs:
        return []
    out = []
    for c in relations.claims_on(conn, rel["number"], rel["tag_sha"]):
        if c["ref"] in refs:
            out.append({k: c[k] for k in (
                "ref", "type", "from", "from_version", "to", "to_version",
                "commit", "exact", "result", "result_note", "measured",
                "note")})
    return sorted(out, key=lambda c: c["ref"])


# -- release notes, generated (GitHub's auto-notes) ---------------------------

def previous_released(conn, item_id: int, version: str) -> dict:
    """The newest RELEASED revision of this item below `version`, or {}.

    The range the notes cover. "Since the last release" is the only honest
    starting point — everything between then and now is what somebody building
    against the previous one has not seen.
    """
    want = db.parse_version(version)
    best: dict = {}
    for r in conn.execute(
            "SELECT * FROM revisions WHERE item_id=? AND state='RELEASED' "
            "ORDER BY vmaj, vmin, vpat", (item_id,)):
        if db.parse_version(r["version"]) < want:
            best = dict(r)
    return best


def notes_draft(conn, rel: dict) -> str:
    """Assemble the release notes from what actually merged since last time.

    Four sources, in the order a reader wants them: what changed and who did it
    (the proposals), what moved underneath (pin bumps, with the CHILD's commit
    subjects — `git diff --submodule=log` without a clone), what the numbers say
    now (attribute deltas), and what is in the box (the pin closure).

    The requester edits it; it is stored on the release and goes into the
    evidence. Generated-then-edited is GitHub's shape and it is the right one:
    nobody writes release notes from a blank page, and nobody should ship the
    machine's first draft either.
    """
    from . import gitrepo
    item = conn.execute("SELECT * FROM items WHERE id=?",
                        (rel["item_id"],)).fetchone()
    number, version = item["number"], rel["version"]
    prev = previous_released(conn, rel["item_id"], version)
    head = f"v{version}"
    base = f"v{prev['version']}" if prev else ""

    out = [f"# {number} v{version} — {item['name']}", ""]
    out.append(f"Promoted {rel['from_maturity'] or '(none)'} → "
               f"{rel['to_maturity']}."
               + (f" Previous release: v{prev['version']}." if prev
                  else " **This is the first release of this item.**"))
    out.append("")

    if not base:
        out += ["## What this is", "",
                item["name"] + ".",
                "", "*No previous RELEASED revision, so there is nothing to "
                "compare against — this section is the place to say what the "
                "part does.*", ""]
    else:
        try:
            cmp_ = gitrepo.compare(number, base, head, max_patch=0,
                                   max_files=400)
        except Exception as exc:  # noqa: BLE001 — a missing ref is not a crash
            return "\n".join(out + [f"*could not diff {base}…{head}: {exc}*",
                                    ""])
        out += ["## Changes since v" + prev["version"], ""]
        revs = _revisions_between(conn, rel["item_id"], prev["version"], version)
        if revs:
            for r in revs:
                # WHO PUT THIS IN, spelled out. "approved by policy" is not a
                # detail somebody reading release notes should have to decode:
                # it means nobody looked, and that is exactly what a person
                # deciding whether to build against this needs to know.
                who = {
                    "policy": "*auto-merged by policy — no human reviewed it*",
                    "bot": f"*approved by the agent {r['approver']}*",
                }.get(r["approver_kind"] or "",
                      r["approver"] or r["author"] or "—")
                what = r["summary"] or "(no summary)"
                out.append(f"- **v{r['version']}** — {what} · {who}")
            out.append("")
        subs = cmp_.get("submodules") or []
        if subs:
            out += ["### Children that moved", ""]
            for s in subs:
                a_, b_ = s.get("from") or {}, s.get("to") or {}
                out.append(
                    f"- `{s.get('path')}` "
                    f"{('v' + a_['version']) if a_.get('version') else (a_.get('commit') or '—')[:10]}"
                    f" → "
                    f"{('v' + b_['version']) if b_.get('version') else (b_.get('commit') or '—')[:10]}"
                    + ("" if (b_.get("released") is not False) else
                       "  ⚠ not a release"))
                for c in (s.get("commits") or [])[:6]:
                    out.append(f"    - {c.get('subject') or c}")
            out.append("")
        delta = cmp_.get("manifest_delta") or {}
        attrs = delta.get("attributes") or {}
        rows = ([("+", a) for a in attrs.get("added") or []]
                + [("−", a) for a in attrs.get("removed") or []]
                + [("~", a) for a in attrs.get("changed") or []])
        if rows:
            out += ["### Numbers", ""]
            for mark, a in rows:
                out.append(f"- {mark} `{a.get('name')}` "
                           f"{_attr_text(a.get('from'))} → "
                           f"{_attr_text(a.get('to'))}")
            out.append("")
        fields = [f for f in (delta.get("fields") or [])
                  if f.get("name") not in ("version", "state", "maturity",
                                           "author", "date")]
        if fields:
            out += ["### What it says about itself", ""]
            for f in fields:
                out.append(f"- `{f['name']}` {f.get('from') or '—'} → "
                           f"{f.get('to') or '—'}")
            out.append("")
        st = cmp_.get("stats") or {}
        out.append(f"*{st.get('files_changed', 0)} file(s) changed, "
                   f"+{st.get('additions', 0)} −{st.get('deletions', 0)} "
                   f"between v{prev['version']} and v{version}.*")
        out.append("")

    pins = [p for p in (rel.get("pins") or []) if p.get("child") != "catalog"]
    if pins:
        out += ["## What is in it", ""]
        for p in pins:
            out.append(f"- `{p['child']}` v{p.get('version') or '?'} — "
                       f"{p.get('maturity') or p.get('state') or '?'}")
        out.append("")
    out += ["", "*Drafted by plm from the merged proposals, the pin bumps and "
            "the attribute deltas. Edit it — it is stored on the release and "
            "goes into the evidence file.*"]
    return "\n".join(out)


def _attr_text(a) -> str:
    """An attribute cell, whichever shape the delta hands over."""
    if a is None:
        return "—"
    if isinstance(a, dict):
        return (str(a.get("value", "")) + (f" {a['unit']}" if a.get("unit")
                                           else "")) or "—"
    return str(a)


def _revisions_between(conn, item_id: int, lo: str, hi: str) -> list[dict]:
    a, b = db.parse_version(lo), db.parse_version(hi)
    out = []
    for r in conn.execute(
            "SELECT r.*, ap.approver, ap.approver_kind FROM revisions r "
            "LEFT JOIN approvals ap ON ap.revision_id=r.id "
            "AND ap.decision='approved' WHERE r.item_id=? "
            "ORDER BY r.vmaj, r.vmin, r.vpat", (item_id,)):
        v = db.parse_version(r["version"])
        if a < v <= b:
            out.append(dict(r))
    return out


# -- evidence: one deterministic JSON document --------------------------------

def _canon(obj) -> bytes:
    """The ONE serialisation, and it has no wall clock in it.

    Sorted keys, no insignificant whitespace, UTF-8, one trailing newline. Every
    value comes from the database or from git, so re-serialising the same release
    produces the same bytes and therefore the same sha256 — which is the whole
    claim the hash in the note is making.
    """
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode()


def evidence_document(conn, rel: dict) -> dict:
    """Everything "what exactly did we release, and who said so?" needs.

    BOUNDED BY THE DECISION (stage 12, loose end 2). It is called once, at
    the decision, and its bytes are what is stored and hashed; `python -m
    plm.release --verify` checks the STORED file against that hash and never
    asks this function to agree with itself later. One input was read with no
    bound at all: approvals — every decided approval of the item, so a LATER
    proposal of the same item changed every older release's re-render (27 of
    135 POC releases). Now only those decided at or before this release's
    `decided_at`, which a re-render reproduces.

    The rest is what it was AT THE DECISION and is recorded as such, because
    that is what the gate and the signers saw: the item row's name (the tag's
    `plm.toml` snapshot is not refreshed by a promotion, so it can be staler
    than the row — measured on 35 POC releases, the demo renames), the check
    verdicts on the tag's commit, each pinned child's maturity, a referenced
    claim's result. A rename, a re-run, a child's promotion or a later coupon
    result changes the live answer, not the evidence — `--verify` reports that
    as "would differ if regenerated today (informational)".
    """
    from . import checks as checksmod, gitindex, lfs
    item = conn.execute("SELECT * FROM items WHERE id=?",
                        (rel["item_id"],)).fetchone()
    number = item["number"]
    repo = gitwrite.repo_path(number)
    sha = rel["tag_sha"]
    doc = manifest.parse(
        gitwrite.read_blob(repo, sha, "plm.toml").decode(errors="replace"))
    decided_at = rel.get("decided_at") or db.now()
    entries = gitvalidate.tree_entries(repo, sha)
    files = []
    for path, e in sorted(entries.items()):
        if e.mode == gitindex.MODE_GITLINK:
            continue
        # stage 17: an LFS pointer is evidence of the OBJECT — the sha256 and
        # size recorded are the mesh's, which is what "the bytes that were
        # reviewed are the bytes that get printed" is about; `lfs` says so.
        # Absent on every other file, so evidence frozen before LFS hashes to
        # what it did.
        ptr = (lfs.blob_pointer(repo, e.sha, e.size)
               if e.size <= lfs.POINTER_MAX else None)
        if ptr:
            files.append({"path": path, "size": ptr[1], "blob": e.sha,
                          "sha256": ptr[0], "lfs": True,
                          "role": _role_of(doc, path)})
            continue
        files.append({"path": path, "size": e.size, "blob": e.sha,
                      "sha256": gitindex._blob_sha256(repo, e.sha),
                      "role": _role_of(doc, path)})
    approvals = [
        {"id": a["id"], "target_state": a["target_state"],
         "requested_by": a["requested_by"], "requested_at": a["requested_at"],
         "approver": a["approver"], "approver_kind": a["approver_kind"],
         "decision": a["decision"], "decided_at": a["decided_at"],
         "note": a["decision_note"], "version": a["version"]}
        for a in conn.execute(
            "SELECT ap.*, r.version FROM approvals ap "
            "JOIN revisions r ON r.id=ap.revision_id WHERE r.item_id=? "
            "AND ap.decision!='' AND COALESCE(ap.decided_at, 0) <= ? "
            "ORDER BY ap.id", (item["id"], decided_at))]
    signoffs = [
        {"kind": s["kind"], "item": s["item"], "signer": s["signer"],
         "signer_kind": s["signer_kind"], "verdict": s["verdict"],
         "note": s["note"], "tag_sha": s["tag_sha"], "identity": s["identity"],
         "signed_at": s["signed_at"],
         # stage 10: present only when the line was attested BY REFERENCE, so
         # every evidence file frozen before it still hashes to what it did
         **({"evidence": s["evidence"]}
            if "evidence" in s.keys() and s["evidence"] else {})}
        for s in conn.execute(
            "SELECT * FROM release_signoffs WHERE release_id=? ORDER BY id",
            (rel["id"],))]
    wf = workflow_for(rel["from_maturity"], rel["to_maturity"])
    return {
        "schema": SCHEMA,
        "item": {"number": number, "name": item["name"], "kind": item["kind"],
                 "project": item["project"] or ""},
        "revision": {"version": rel["version"], "tag": rel["tag"],
                     "tag_sha": sha,
                     "from_maturity": rel["from_maturity"],
                     "to_maturity": rel["to_maturity"]},
        "manifest": manifest.toml_facts(
            gitwrite.read_blob(repo, sha, "plm.toml").decode(errors="replace")),
        "files": files,
        "pins": pin_closure(number, sha),
        "approvals": approvals,
        "release": {"id": rel["id"], "workflow": wf["key"],
                    "workflow_registered": wf["registered"],
                    "origin": rel["origin"],
                    "requested_by": rel["requested_by"],
                    "requested_at": rel["requested_at"],
                    "decided_by": rel["decided_by"],
                    "decided_kind": rel["decided_kind"],
                    "decided_at": rel["decided_at"],
                    "decision_note": rel["decision_note"]},
        "signoffs": signoffs,
        # THE CHECKS THAT WERE GREEN WHEN THIS WAS APPROVED. Recorded here, at
        # freeze time, because the evidence answers "what exactly did we
        # release" — and "the board said green" is part of that answer. It used
        # to be a hardcoded `[]` with a comment saying §2.6 was not built; it is
        # built (plm/checks.py) and the gate that let this release through read
        # exactly these rows (stage 7).
        #
        # ⚠️ THE VERDICT, NOT THE MEASUREMENT. `facts` is deliberately absent:
        # `render-matches-source` records `cached` and `render_seconds`, which
        # differ between the first run and the second, so including them made
        # `evidence_sha256` un-reproducible the moment anything re-ran the
        # checks — measured, not theorised (a re-index after the promotion
        # re-ran them and the hash moved). The board keeps the numbers;
        # `GET /api/repo/{n}/checks?sha=` at the tag sha is still the whole
        # picture, and it is keyed by the immutable commit this document names.
        "checks": [
            {"name": r["name"], "status": r["status"], "summary": r["summary"],
             "source": r["source"]}
            for r in checksmod.read(conn, number, sha)],
        "checks_required": checksmod.required_for(rel["to_maturity"] or ""),
        "notes_md": rel["notes_md"] or "",
        # stage 10: the claims a checklist line was attested BY REFERENCE to,
        # resolved — which coupon, at which commit, found what. Absent when no
        # line was (the same reproducibility rule as the signoff key above).
        **({"evidence_claims": ev} if (ev := evidence_claims(
            conn, {**rel, "number": number}, signoffs)) else {}),
    }


def _role_of(doc: dict, path: str) -> str:
    for role, paths in (doc.get("files") or {}).items():
        for p in (paths if isinstance(paths, list) else [paths]):
            if p == path:
                return role
    return "primary" if doc.get("primary") == path else ""


def pin_closure(number: str, commit: str, *, depth: int = 12,
                _seen: set | None = None) -> list[dict]:
    """Every descendant reachable through gitlinks, with its maturity.

    The whole point of a baseline: a release tag on a top assembly pins every
    descendant by gitlink, so "what exactly was this on the 17th" is a
    `git checkout` — and the evidence file writes that closure down so the answer
    survives even if a child repo is later lost.
    """
    from . import gitindex
    _seen = _seen if _seen is not None else set()
    if depth <= 0 or (number, commit) in _seen:
        return []
    _seen.add((number, commit))
    repo = gitwrite.REPOS / f"{number}.git"
    if not (repo / "HEAD").exists():
        return []
    out = []
    for path, e in sorted(gitvalidate.tree_entries(repo, commit).items()):
        if e.mode != gitindex.MODE_GITLINK:
            continue
        child = path.split("/", 1)[1] if path.startswith("parts/") else path
        if child == "catalog":
            out.append({"parent": number, "path": path, "child": "catalog",
                        "commit": e.sha, "version": "", "maturity": "",
                        "state": "", "released": True, "kind": "catalog"})
            continue
        st = gitindex.pin_status(child, e.sha)
        out.append({"parent": number, "path": path, "child": child,
                    "commit": e.sha, "version": st["version"],
                    "maturity": st["maturity"], "state": st["state"],
                    "released": st["is_released"], "kind": "design"})
        out += pin_closure(child, e.sha, depth=depth - 1, _seen=_seen)
    return out


def evidence_path(number: str, release_id: int, version: str) -> Path:
    return EVIDENCE_DIR / number / f"v{version}-release-{release_id}.json"


def freeze_evidence(conn, rel: dict) -> dict:
    """Write the evidence document and return `{key, sha256, bytes}`.

    Written once, at approval. It is a record of a decision, so it is not
    regenerated afterwards — but it IS reproducible: `evidence_document` over the
    same rows gives the same bytes, which is what makes the hash in the note
    worth anything.
    """
    doc = evidence_document(conn, rel)
    # PIN SITE (stage 11b): the evidence writes the whole pin closure down so
    # "what exactly was this" survives — every commit it names stays reachable
    # in the repo that owns it (normally a no-op: a tag reaches its closure).
    from . import gitkeep
    gitkeep.protect(
        [(rel["number"], doc["revision"]["tag_sha"],
          f"release {rel['id']} evidence")]
        + [(p["child"], p["commit"],
            f"release {rel['id']} evidence pin {p['parent']}:{p['path']}")
           for p in doc.get("pins") or []],
        where=f"evidence {rel['number']}")
    raw = _canon(doc)
    digest = hashlib.sha256(raw).hexdigest()
    path = evidence_path(rel["number"], rel["id"], rel["version"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"key": str(path), "sha256": digest, "bytes": len(raw),
            "document": doc}


def read_evidence(rel: dict) -> tuple[bytes, str]:
    """The stored bytes and their hash — never re-serialised on the way out.

    Serving a freshly rendered document would make `evidence_sha256` a claim
    about something nobody can check. This reads the file that was frozen.
    """
    path = Path(rel["evidence_key"]) if rel.get("evidence_key") else \
        evidence_path(rel["number"], rel["id"], rel["version"])
    # Stage 18: a release with no recorded hash has no evidence YET, whatever a
    # file at its path says. The path is `<number>/v<version>-release-<id>`,
    # and a backup tree keeps every file it ever saw (rsync, never --delete):
    # after a reseed renumbered releases, a restore could put a retired
    # document exactly where a pending release would look for its own.
    unfrozen = ("evidence_sha256" in rel and not rel.get("evidence_sha256")
                and not rel.get("evidence_key"))
    if unfrozen or not path.is_file():
        raise ReleaseError(
            f"the evidence for release {rel['id']} is not on disk at {path}. It "
            "is frozen at\napproval; a release that has not been approved has "
            "none yet.", 404)
    raw = path.read_bytes()
    return raw, hashlib.sha256(raw).hexdigest()


# -- the status note: the decision, mirrored into git -------------------------

def write_status_note(conn, rel: dict, *, actor: str, date: str = "") -> dict:
    """`[status]` on the release commit — §2.7, and promise 2 of three.

    This is the only thing a promotion writes into git, and it writes it as a
    NOTE: a tag's tree is immutable and stays that way, so a clone learns that
    v0.4.0 is now RELEASED without a single byte of the part changing.
    """
    number = rel["number"]
    repo = gitwrite.repo_path(number)
    status = {
        "state": state_for_maturity(rel["to_maturity"]),
        "maturity": (rel["to_maturity"] or "").lower(),
        "from_maturity": rel["from_maturity"] or "",
        "approver": rel["decided_by"] or actor,
        "approver_kind": rel["decided_kind"] or "",
        "date": date or time.strftime("%Y-%m-%d"),
        "release": rel["id"],
    }
    if rel.get("evidence_sha256"):
        status["evidence_sha256"] = rel["evidence_sha256"]

    def mutate(note: dict) -> None:
        note["status"] = status

    _, res = gitnotes.amend(
        repo, rel["tag_sha"], mutate, actor=actor or "plm",
        message=(f"{number} v{rel['version']} is "
                 f"{status['state']}\n\n"
                 f"Promoted {rel['from_maturity'] or '(none)'} → "
                 f"{rel['to_maturity']} by {status['approver']} "
                 f"({status['approver_kind'] or 'human'}), release "
                 f"{rel['id']}.\n"
                 "No tag moved and nothing was committed on `main`: a release "
                 "is a decision about\nan existing revision, not a change to it "
                 "(docs/PLM_REVIEW_MODEL.md §2.1b).\n"
                 + (f"Evidence sha256: {rel['evidence_sha256']}\n"
                    if rel.get("evidence_sha256") else "")))
    return res


def repo_state(number: str) -> dict:
    """`{main, tags}` — used before and after a promotion to PROVE nothing moved."""
    repo = gitwrite.repo_path(number)
    return {"main": gitwrite.tip(repo, "refs/heads/main"),
            "tags": dict(sorted(gitvalidate.releases(repo).items()))}


# -- verification: the STORED file is the evidence ----------------------------

def verify(conn, rel: dict) -> dict:
    """Is release `rel`'s evidence intact? Two answers, never confused.

    1. `stored` — THE CHECK. The frozen file's sha256 against
       `releases.evidence_sha256`, and against the `evidence_sha256` the
       release's `[status]` note carries in git (the copy a clone can see).
       This is what "the evidence is intact" means: the bytes recorded at the
       decision are the bytes on disk.
    2. `regenerated` — INFORMATIONAL. `evidence_document` run now over today's
       rows. Equal, or "would differ if regenerated today", with the top-level
       sections that differ and why each one can. It never fails a release:
       a re-run check or a child promoted since is the world moving on, not the
       evidence changing (stage 12, loose end 2 — 65 of 103 POC releases
       re-rendered differently and every one of their stored files was intact).
    """
    out = {"release": rel["id"], "number": rel["number"],
           "version": rel["version"], "recorded_sha256": rel["evidence_sha256"]}
    try:
        raw, digest = read_evidence(rel)
    except ReleaseError as exc:
        return {**out, "stored": "MISSING", "ok": False,
                "why": str(exc).split("\n")[0]}
    note_sha = ""
    try:
        st = gitnotes.status_of(gitwrite.repo_path(rel["number"]),
                                rel["tag_sha"])
        note_sha = str(st.get("evidence_sha256") or "")
    except Exception:                                       # noqa: BLE001
        note_sha = ""
    intact = digest == rel["evidence_sha256"]
    out.update({"stored_sha256": digest,
                "stored": "intact" if intact else "CHANGED",
                "note_sha256": note_sha,
                "note": ("agrees" if note_sha == digest else
                         "absent" if not note_sha else "DISAGREES"),
                "ok": intact and note_sha in ("", digest)})
    try:
        fresh = _canon(evidence_document(conn, rel))
    except Exception as exc:                                # noqa: BLE001
        out.update({"regenerated": "could not regenerate",
                    "regenerate_error": f"{type(exc).__name__}: {exc}"[:300]})
        return out
    if fresh == raw:
        out["regenerated"] = "identical today"
        return out
    stored_doc = json.loads(raw)
    now_doc = json.loads(fresh)
    diff = sorted(k for k in set(stored_doc) | set(now_doc)
                  if stored_doc.get(k) != now_doc.get(k))
    out.update({"regenerated": "would differ if regenerated today "
                               "(informational)",
                "differs_in": diff,
                "why": [DRIFT_WHY.get(k, "") for k in diff]})
    return out


#: Why a section of a frozen evidence file can differ from a re-render today.
#: Point-in-time facts by nature — not a defect in the stored file.
DRIFT_WHY = {
    "checks": "a check on the tag's commit was re-run since (its verdict "
              "row is per commit and the latest run wins)",
    "pins": "a pinned child's maturity changed since (promoted / obsoleted)",
    "approvals": "an approval decided AFTER this release (counted before "
                 "stage 12, which bounds them by the release's decided_at)",
    "item": "the item was renamed / reclassified / moved project since",
    "evidence_claims": "a referenced claim's recorded result changed since",
    "manifest": "the manifest-facts shape changed (stage 10 relations tuple)",
    "notes_md": "the release notes were edited after the freeze",
    "release": "the release row changed after the freeze",
    "signoffs": "a signoff was added or changed after the freeze",
    "checks_required": "absent from evidence frozen before it was recorded "
                       "(stage 7), or policy's required list changed since",
}


def _verify_main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m plm.release",
        description="Verify frozen release evidence: the STORED file against "
                    "its recorded sha256 (the check), and a re-render today "
                    "(informational).")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--backfill-notes", action="store_true",
                    help="write the missing [status] note for every APPROVED "
                         "release that has evidence but no note (stage 12's "
                         "hand-back: releases made through a proposal)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--release", type=int, action="append", default=[],
                    help="one release id (repeatable; default: every release "
                         "with evidence)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every release, not only the ones that differ")
    args = ap.parse_args(argv)
    if args.backfill_notes:
        return _backfill_notes(dry_run=args.dry_run)
    if not args.verify:
        ap.error("one of --verify / --backfill-notes")
    with db.connect() as conn:
        q = ("SELECT r.*, i.number FROM releases r JOIN items i "
             "ON i.id=r.item_id WHERE r.evidence_sha256<>''")
        if args.release:
            q += f" AND r.id IN ({','.join('?' * len(args.release))})"
        rels = [dict(x) for x in conn.execute(q + " ORDER BY r.id",
                                               args.release)]
        res = [verify(conn, r) for r in rels]
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        from collections import Counter
        print(f"plm.release --verify — {len(res)} release(s) with evidence")
        print("-" * 72)
        st = Counter(r["stored"] for r in res)
        nt = Counter(r.get("note", "") for r in res)
        rg = Counter(r.get("regenerated", "") for r in res)
        print("  STORED FILE (the check)")
        for k, v in sorted(st.items()):
            print(f"    {k:<30} {v}")
        print("  git note's evidence_sha256")
        for k, v in sorted(nt.items()):
            print(f"    {k or '-':<30} {v}")
        print("  REGENERATED TODAY (informational — never a failure)")
        for k, v in sorted(rg.items()):
            print(f"    {k:<44} {v}")
        why = Counter(k for r in res for k in r.get("differs_in") or [])
        for k, v in why.most_common():
            print(f"      {k:<16} {v:>4}  {DRIFT_WHY.get(k, '')}")
        for r in res:
            bad = not r["ok"]
            if bad or args.verbose:
                print(f"  {'✗' if bad else '·'} #{r['release']} {r['number']} "
                      f"v{r['version']}: stored {r['stored']}, note "
                      f"{r.get('note', '-')}, regenerated "
                      f"{r.get('regenerated', '-')}"
                      + (f" {r['differs_in']}" if r.get("differs_in") else ""))
        print(f"  ALL STORED EVIDENCE INTACT  {all(r['ok'] for r in res)}")
    sets = _verify_sets(args)
    return 0 if all(r["ok"] for r in res) and sets else 1


def _verify_sets(args) -> bool:
    """Stage 14: every approved RELEASE SET's manifest — the stored file vs its
    recorded sha256 (the check), the member evidence hashes it lists vs the
    rows, and a re-render from the rows (identical, by construction)."""
    from . import releasesets
    with db.connect() as conn:
        releasesets.ensure(conn)
        rows = [releasesets.row(conn, r[0]) for r in conn.execute(
            "SELECT id FROM release_sets WHERE manifest_sha256<>'' ORDER BY id")]
        res = [releasesets.verify(conn, s) for s in rows]
    if args.json:
        print(json.dumps({"release_sets": res}, indent=2))
        return all(r["ok"] for r in res)
    print("-" * 72)
    print(f"  RELEASE SET MANIFESTS — {len(res)} with a manifest")
    for r in res:
        print(f"  {'·' if r['ok'] else '✗'} set {r['release_set']}: stored "
              f"{r['stored']} ({(r.get('stored_sha256') or '')[:16]}…), "
              f"{r.get('members', 0)} members, member evidence "
              f"{r.get('member_evidence', '-')}, regenerated "
              f"{r.get('regenerated', '-')}")
    print(f"  ALL SET MANIFESTS INTACT  {all(r['ok'] for r in res)}")
    return all(r["ok"] for r in res)


def _backfill_notes(*, dry_run: bool) -> int:
    """Write `[status]` for approved releases that have evidence and no note.
    The note carries the release's own decided date, not today's."""
    n, skipped = 0, []
    with db.connect() as conn:
        rels = [dict(r) for r in conn.execute(
            "SELECT r.*, i.number FROM releases r JOIN items i ON i.id=r.item_id "
            "WHERE r.state='approved' AND r.evidence_sha256<>'' ORDER BY r.id")]
        for rel in rels:
            try:
                repo = gitwrite.repo_path(rel["number"])
                st = gitnotes.status_of(repo, rel["tag_sha"])
            except Exception as exc:                        # noqa: BLE001
                skipped.append((rel["id"], str(exc)[:80]))
                continue
            if st.get("evidence_sha256"):
                continue
            n += 1
            print(f"  #{rel['id']} {rel['number']} v{rel['version']} "
                  f"({rel['origin']}) — {'would write' if dry_run else 'writing'}"
                  " [status]")
            if dry_run:
                continue
            day = time.strftime("%Y-%m-%d",
                                time.gmtime(float(rel["decided_at"] or 0)))
            res = write_status_note(conn, rel, actor=rel["decided_by"] or "plm",
                                    date=day)
            conn.execute("UPDATE releases SET note_commit=? WHERE id=? AND "
                         "note_commit=''", (res.get("commit", ""), rel["id"]))
            conn.commit()
    print(f"{'would write' if dry_run else 'wrote'} {n} note(s); skipped "
          f"{len(skipped)}: {skipped[:5]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_verify_main())
