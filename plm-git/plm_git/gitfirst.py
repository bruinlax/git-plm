"""The git-first write path: the two receive hooks, and nothing else.

    git push origin HEAD:draft/taller-pillars
        │
        ├─ receive-pack lands the objects in a QUARANTINE store (git does this
        │  itself, via GIT_QUARANTINE_PATH). No served ref has moved.
        │
        ├─ pre-receive   plm.gitvalidate.check — the cheap, teachable "no".
        │                A non-zero exit makes git DELETE the quarantine, so a
        │                refused push leaves not one object behind.
        │
        ├─ (objects migrate; the draft branch now exists and is servable)
        │
        └─ post-receive  ask plm to INDEX the commit. plm keeps the SHA the
                         pusher pushed and upserts the registry rows from
                         `plm.toml` and the tree at that commit.

WHAT IS DIFFERENT FROM THE AUGUST WRITE PATH (`push.py`, still on branch
`main`): that one **translated** a push into `revise` / `put_file` / `submit`
API calls and plm re-authored the bytes into `versions/vX.Y.Z/`. The pusher got
back different SHAs than they pushed, and git was a costume. Here the commit is
kept and the database follows it. `push.py` is imported for the parts that were
always right — the token door, the `Refused` tone — and its `propose()` is the
thing being replaced.

THIS PROCESS NEVER OPENS THE DATABASE. The registry has one writer (plm), and a
git container with a read-write handle on the same SQLite file would make that
sentence false. `POST /api/git/index`, guarded by a shared token, is the whole
interface.

Everything printed to stderr appears in the pusher's terminal prefixed
`remote:`. That is the only channel git gives us, so it is the entire user
interface of the write path, and it is written to be read.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from plm import gitvalidate, webroutes

from . import config, push

RULE = "─" * 70
ZERO = "0" * 40


def err(line: str = "") -> None:
    print(line, file=sys.stderr, flush=True)


def block(title: str, body: str) -> None:
    err()
    err(f"✗ {title}")
    err(RULE)
    for line in body.splitlines():
        err(line)
    err(RULE)
    err()


def number_of_repo() -> str:
    """The item number is the repo's name: `<NUMBER>.git`."""
    gd = Path(os.environ.get("GIT_DIR") or ".").resolve()
    return gd.name[:-4] if gd.name.endswith(".git") else gd.name


def git_dir() -> Path:
    return Path(os.environ.get("GIT_DIR") or ".").resolve()


def updates() -> list[tuple[str, str, str]]:
    out = []
    for line in sys.stdin.read().splitlines():
        bits = line.split()
        if len(bits) == 3:
            out.append((bits[0], bits[1], bits[2]))
    return out


def push_options() -> dict[str, str]:
    """`git push -o title=… -o maturity=…` — Gitea's AGit convention (§6d).

    git hands them to the hook as GIT_PUSH_OPTION_COUNT / _0 / _1 …, and only
    when `receive.advertisePushOptions` is on, which is why the transport sets
    it. A bare option with no `=` is recorded as present-and-empty rather than
    dropped, so `-o draft` can mean something later without a format change.
    """
    n = int(os.environ.get("GIT_PUSH_OPTION_COUNT") or 0)
    out: dict[str, str] = {}
    for i in range(n):
        raw = os.environ.get(f"GIT_PUSH_OPTION_{i}", "")
        k, _, v = raw.partition("=")
        if k:
            out[k.strip()] = v.strip()
    # `-o change=` is the older spelling of `-o pr=` (stage 14 → 23): one key
    if "change" in out:
        out.setdefault("pr", out.pop("change"))
    return out


def pr_lines(ch: dict) -> list[str]:
    """The `remote:` lines for the pull request a push joined or opened
    (`changes.brief`) — empty when it joined none."""
    if not ch.get("number"):
        return []
    n = ch.get("members")
    return [f"plm: pull request {ch.get('ref') or ch['number']} "
            f"({ch['number']}, {ch.get('state')}, {n} item{'s' if n != 1 else ''})"
            f" — {ch.get('title', '')[:60]}",
            f"plm:           {ch.get('url', '')}",
            f"plm:           next pushes: git push -o pr={ch.get('id') or ch['number']} …"
            "   then submit_change when the work is whole"]


def actor() -> str:
    """The name the transport's bearer token resolved to. The hook never sees
    the token, only the identity it proved.

    ⛔ M2 — `PLMGIT_ACTOR` ONLY. `REMOTE_USER` used to be the fallback, and the
    transport populated `REMOTE_USER` from the CLIENT HEADER `X-Plm-Actor`
    whenever no token had been resolved — so on any request the `pushing`
    boolean missed (M1 proved it was escapable), an unauthenticated pusher could
    have been attributed to `geoff`. The transport no longer sets it from a
    header at all; dropping it here as well means the invariant does not depend
    on remembering both halves.
    """
    return os.environ.get("PLMGIT_ACTOR") or ""


def actor_is_human() -> bool:
    return os.environ.get("PLMGIT_ACTOR_KIND") == "human"


# -- talking to plm -----------------------------------------------------------

def plm_index(payload: dict) -> dict:
    """POST /api/git/index. Raises RuntimeError carrying plm's own sentence."""
    url = f"{config.GITFIRST_PLM_URL}/api/git/index"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config.GITFIRST_INDEX_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw).get("detail", "")
        except Exception:  # noqa: BLE001
            detail = raw.decode(errors="replace")[:600]
        raise RuntimeError(str(detail or exc.reason)) from None
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not reach plm at "
                           f"{config.GITFIRST_PLM_URL}: {exc}") from None


# -- pre-receive --------------------------------------------------------------

def _refuse_unless_write(number: str) -> None:
    """PLM_GIT_MODE (stage 9): a push is a WRITE, and only a project in `write`
    takes writes over git. In `read` the repository is a projection the
    registry regenerates after every legacy write — a pushed commit would be
    the only copy of something plm then overwrites, or (with the marker) a
    repo that stops following the registry at all. So it is refused here, in
    the quarantine, before any ref moves, with the way forward.

    The git container never opens the database, so it ASKS plm
    (`GET /api/gitmode?number=`). If plm cannot answer, the push is refused —
    fail closed: "I could not check" is not permission."""
    url = (f"{config.GITFIRST_PLM_URL}/api/gitmode?number="
           f"{urllib.request.quote(number)}")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            ans = json.loads(r.read() or b"{}")
    except Exception as exc:  # noqa: BLE001
        raise gitvalidate.Refused(
            f"plm could not be asked whether {number} accepts pushes "
            f"({config.GITFIRST_PLM_URL}: {exc}).\nRefusing rather than "
            "guessing. Try again in a minute.") from None
    if ans.get("push"):
        return
    mode = ans.get("mode") or "off"
    proj = ans.get("project") or "(no project)"
    base = config.PUSH_PLM_PUBLIC_URL
    raise gitvalidate.Refused(
        f"{number} is in project {proj}, which is READ-ONLY over git "
        f"(PLM_GIT_MODE={mode}).\n"
        "\n"
        "  Its repository is generated FROM the registry and regenerated after "
        "every write,\n  so a pushed commit would have nowhere to live. Clone "
        "it, read it, diff it —\n  but write through plm:\n"
        "\n"
        f"    MCP   checkin / put_file / revise / add_bom_line … on {number}\n"
        f"    API   {base}/api/items/{number}/checkin (and friends)\n"
        f"    UI    {base}{webroutes.item(number)}\n"
        "\n"
        "  To take writes over git, the project has to be moved to `write` "
        "mode — ask the operator:\n"
        f"    python -m plm.gitmode --project {proj} --mode write")


def _repo_lock():
    """⛔ M9 — THE SAME LOCK THE APPROVER TAKES. `plm/gitwrite.py::lock` is a
    cross-process flock on `<repo>/plm-write.lock` and `gitindex.approve_commit`
    holds it; the git-first hooks held NOTHING, so a push landing between
    `approve_commit` reading the branch tip and the database write left the
    registry describing a commit that was no longer the tip. It is
    `safegit.flock` underneath now, so it gives up after a bounded wait and
    says who has it rather than parking receive-pack forever.
    """
    from plm import gitwrite
    return gitwrite.lock(git_dir())


def pre_receive() -> int:
    ups = updates()
    number = number_of_repo()
    gd = git_dir()
    try:
        if not actor():
            raise gitvalidate.Refused(
                "this receive-pack was started without a verified actor, so "
                "plm cannot say who\nis proposing. Push over HTTP through this "
                "service (which checks the bearer token\nand sets the actor) "
                "rather than driving receive-pack directly.")
        _refuse_unless_write(number)
        if len(ups) != 1:
            raise gitvalidate.Refused(
                f"this push updates {len(ups)} refs at once. One push, one "
                "proposal — a change that\nmeant two revisions cannot be "
                "reviewed or approved as one thing.\n"
                f"\n  git push origin HEAD:{config.GITFIRST_DRAFT_PREFIX}/<topic>")
        old, new, ref = ups[0]
        kind = gitvalidate.check_ref_namespace(
            ref, old, new, draft_ref=config.GITFIRST_DRAFT_PREFIX,
            for_ref=config.PUSH_REF)

        if new == ZERO:
            # Deleting your own draft is how you say "never mind". Allowed for
            # a draft branch; `check_ref_namespace` already refused everything
            # else, and refs/for/main is transient anyway.
            if kind != "draft":
                raise gitvalidate.Refused(
                    f"{ref} is not yours to delete.")
            err(f"plm-git: {ref} will be deleted; plm will retire the DRAFT it "
                "indexed from it.")
            return 0

        # Held only for the read — `gitvalidate.check` talks to git and to no
        # other process, so this serialises two concurrent pushes to the same
        # part against each other and against an in-flight approval, and is
        # released long before anything calls plm.
        with _repo_lock():
            verdict = gitvalidate.check(gd, number, new, ref=ref, old=old,
                                        actor=actor())
    except gitvalidate.Refused as exc:
        block(f"plm refused this push ({number})", str(exc))
        err("Nothing was written. The pushed objects were in git's quarantine "
            "and have been")
        err("discarded: no ref moved, no revision was created, and plm's "
            "registry was not")
        err("touched.")
        err()
        return 1
    except Exception:  # noqa: BLE001 — never a bare traceback in a push
        block(f"plm hit an internal error validating this push ({number})",
              traceback.format_exc(limit=4))
        return 1

    for warn in verdict.lint:
        err(f"plm-git: ⚠ {warn}")
    for line in verdict.info:
        err(f"plm-git: ℹ {line}")
    err(f"plm-git: {number} v{verdict.version} looks like a valid revision "
        f"({verdict.bump} from v{verdict.previous or '—'}, "
        f"{verdict.files} file(s), {len(verdict.gitlinks)} gitlink(s)) — "
        "indexing it")
    return 0


# -- post-receive -------------------------------------------------------------

def _draft_ref_for(ref: str, opts: dict, number: str, version: str) -> str:
    """Where a `refs/for/main` proposal's branch lives.

    AGit's convention is that the proposal ref is transient and the real branch
    is derived; `-o topic=` names it, else the version does, which is the name
    somebody reading `git branch -r` would have chosen anyway.
    """
    topic = opts.get("topic") or f"v{version}"
    return f"refs/heads/{config.GITFIRST_DRAFT_PREFIX}/{topic}"


def post_receive() -> int:
    ups = updates()
    number = number_of_repo()
    if not ups:
        return 0
    old, new, ref = ups[0]
    who = actor()
    opts = push_options()
    gd = git_dir()

    try:
        if new == ZERO:
            res = plm_index({"number": number, "ref": ref, "commit": old,
                             "action": "withdraw", "actor": who})
            err()
            err(f"plm: {ref} deleted — "
                + (f"DRAFT v{', v'.join(res.get('removed') or [])} withdrawn"
                   if res.get("removed") else res.get("note", "nothing to do")))
            err()
            return 0

        proposing = ref == config.PUSH_REF
        merged_by_policy = False
        branch = ref
        # What this push REPLACED (stage 11b): a force-pushed draft's old tip,
        # or the branch a re-proposal overwrote. plm keeps whatever that leaves
        # with no ref IF something still pins it (plm/gitkeep.guard).
        replaced = old if old != ZERO else ""
        if proposing:
            # `refs/for/main` = "make the draft branch and submit it". The
            # branch is the durable thing; the for-ref is a request.
            doc_version = gitvalidate.check(gd, number, new, ref=ref,
                                            old=old).version
            branch = _draft_ref_for(ref, opts, number, doc_version)
            # ⛔ M9 — CAS, NOT A BLIND WRITE, UNDER THE SAME LOCK THE APPROVER
            # TAKES. This was `update-ref <branch> <new>` with no old value, so
            # two `refs/for/main` pushes deriving the same topic clobbered each
            # other silently. The expected old value is read inside the lock.
            #
            # ⚠️ THE LOCK IS RELEASED BEFORE `plm_index`. It is a cross-PROCESS
            # flock and `plm.gitindex.approve_commit` takes the same one in the
            # API process; holding it across the HTTP call would deadlock a
            # policy auto-merge against its own push. The ref move is the part
            # that needs serialising; the index is plm's own transaction.
            with _repo_lock():
                old_branch = push.git("rev-parse", "--verify", "--quiet",
                                      branch, check=False).strip()
                push.git("update-ref", "--end-of-options", branch, new,
                         old_branch or ZERO, check=False)
                replaced = old_branch if old_branch != new else ""

        err()
        err(f"plm: indexing {number} {new[:10]} from {branch}")
        err(RULE)
        pr_opt = opts.get("pr") or ""
        res = plm_index({"number": number, "commit": new, "ref": branch,
                         "actor": who, "origin": "push",
                         **({"old": replaced} if replaced else {}),
                         # stage 19: `-o base=<ref|sha>` = the review baseline
                         **({"base": opts["base"]} if opts.get("base") else {}),
                         # stage 14: `-o pr=new -o title="…"` opens a PULL
                         # REQUEST (#n, CHG-n) around this proposal; `-o
                         # pr=<n>` puts it into an existing one (`-o change=`
                         # is folded into `pr` by push_options).
                         **({"change": pr_opt,
                             "title": opts.get("title") or ""}
                            if pr_opt else {})})
        version = res.get("version", "?")
        err(f"plm: DRAFT v{version} (revision id {res.get('revision_id')}) — "
            f"the commit you pushed IS the revision, {new[:10]}")
        err(f"plm:   summary   {res.get('summary', '')[:80]}")
        err(f"plm:   files     {res.get('files')} (the tree's blobs, and LFS "
            "objects by pointer — nothing was copied anywhere)")
        err(f"plm:   attrs {res.get('attributes')}  routing "
            f"{res.get('routing')}  tags {res.get('tags')}  relations "
            f"{res.get('relations')}")
        for line in res.get("bom") or []:
            err(f"plm:   bom       {line['path']} -> {line['child']} "
                f"v{line.get('resolved') or '?'}"
                + ("   [track=release: recorded imprecise, the gitlink is the "
                   "pin]" if line.get("track") else
                   f"   [pinned: child_version={line.get('version') or '—'}]")
                + ("" if line.get("released") else
                   "   ⚠ not a release — §5.1 blocks releasing above it"))
        for line in res.get("requirements") or []:
            # stage 19: a purchase REQUIREMENT — nothing is selected yet
            err(f"plm:   requires  {line['qty']:g} {line['unit']} "
                f"{line['requirement'][:60]}   [requirement — not selected"
                + (f"; candidates {', '.join(line['candidates'][:3])}"
                   if line.get("candidates") else "") + "]")
        for line in res.get("sources") or []:
            err(f"plm:   source    {line['path']} -> {line['source']} "
                f"v{line.get('version') or '?'}   [a SOURCE pin — not a BOM "
                "line]" + ("" if line.get("released") else
                           "   (not on a release tag)"))
        left = res.get("left_review") or {}
        if left:
            err(f"plm:   ⚠ LEFT REVIEW  {left.get('change')} — {left.get('why')}")
            err(f"plm:              every signoff on the old head is dismissed; "
                f"{left.get('next')}")
        prop = res.get("proposal") or {}
        if prop.get("id"):
            err(f"plm: proposal  #{prop['id']} ({prop.get('state')}) — "
                f"{prop.get('url', '')}")
        rb = prop.get("review_base") or {}
        if rb.get("sha"):
            err(f"plm: baseline  {rb['sha'][:10]} ({rb.get('label')}, "
                f"{rb.get('how')}) — what a reviewer diffs this against; "
                "-o base=<ref> to say otherwise")
        elif prop.get("id"):
            err("plm: baseline  none — nothing this commit descends from; "
                "the review shows every file as new")
        if res.get("base_error"):
            err(f"plm: ⚠ base    {res['base_error']}")
        for line in pr_lines(res.get("change") or {}):
            err(line)
        if res.get("change_error"):
            err(f"plm: ⚠ pull request  {res['change_error'].splitlines()[0]}")
        keep = res.get("keep") or {}
        for k in keep.get("kept") or []:
            if k.get("created"):
                err(f"plm:   kept      {k['number']} {k['sha'][:10]} "
                    "(refs/plm/keep — this commit pins it)")
        if (keep.get("orphans") or {}).get("kept"):
            err(f"plm:   kept      {len(keep['orphans']['kept'])} commit(s) "
                "your push left unreachable that something still pins")
        err(RULE)

        if proposing:
            note = opts.get("title") or ""
            target = (opts.get("maturity") or config.PUSH_TARGET_STATE).upper()
            # stage 16b: the one-repo shortcut submits too, so it settles the
            # checks first, like submit_change — a reviewer (or a policy
            # auto-merge) must not act on a board nobody has measured yet
            for line in _settle_checks(number, new):
                err(line)
            sub = _submit(number, version, who, target, note or
                          f"Proposed by git push {new[:10]} ({who})",
                          explode_waiver=opts.get("explode_waiver") or "")
            state = sub.get("state", "IN_REVIEW")
            err(f"submitted: {number} v{version}  DRAFT -> {state}  (approval "
                f"{sub.get('approval_id')}, target {target})")
            # ⚠️ H4 — SAY WHICH OF THE TWO THINGS JUST HAPPENED. This line used
            # to read "a human approving in plm is the merge; bots never
            # approve", which is false for three of the four maturities: the
            # policy (plm/policy.py) auto-merges CONCEPT, DESIGN and PROTOTYPE,
            # so for those the push output was promising a review that would
            # never happen. The registry knows which it was — `approver_kind`
            # is `policy` on an auto-merge — so the push output says it.
            merged_by_policy = bool(sub.get("approved_by_policy"))
            for why in sub.get("held_by_policy") or []:
                # stage 16b: "just build it" would have merged it, but did not
                err(f"held:      just build it did not merge it — {why}")
            sch = sub.get("change") or {}
            if sch.get("number"):
                err(f"review:    pull request {sch.get('ref') or sch['number']} "
                    f"({sch.get('state')}) — "
                    f"{sch.get('url', '')}")
                if sch.get("note"):
                    err(f"           {sch['note']}")
            if merged_by_policy:
                err(f"MERGED BY POLICY — nobody reviewed this. {target} "
                    "auto-merges on this instance")
                err("(GET /api/policy for the rule and where it comes from); "
                    "the tag annotation")
                err("and the History stamp both record approver = policy.")
            else:
                err("now WAIT — a human approving the pull request in plm is "
                    "the merge.")
        else:
            err(f"draft:     {number} v{version} on {branch}")
            err("one piece of work = one pull request: push every repo it "
                "touches with -o pr=<n>,")
            err("then submit the pull request (MCP submit_change / POST "
                "/api/pulls/<n>/submit). A lone draft:")
            err(f"  git push origin HEAD:{config.PUSH_REF}"
                "          # -o title=… -o maturity=… -o pr=new|<n>")
        err(f"review:    {config.PUSH_PLM_PUBLIC_URL}{webroutes.item(number)}")
        # stage 19 (handoff finding 1): the compare is the REVIEW BASELINE ...
        # the pushed head — never `main...` on an item that has no main
        if rb.get("sha"):
            err(f"compare:   {rb['sha'][:10]}...{new[:10]}   "
                f"{config.PUSH_PLM_PUBLIC_URL}"
                f"{webroutes.compare(number, rb['sha'][:12], new[:12])}")
        else:
            err(f"browse:    {config.PUSH_PLM_PUBLIC_URL}{webroutes.item(number)}  "
                "(no baseline to compare against)")
        err()
        if merged_by_policy:
            err("`main` HAS MOVED and the tag is written — the policy approved "
                "it, not a person.")
        else:
            err("git push means PROPOSED. Approval is what moves `main` and "
                "writes the tag.")
        err()
    except SubmitRefused as exc:
        # The DRAFT landed and was indexed; only the request for a decision was
        # refused (the explode gate, an unknown actor, …). Say exactly that —
        # "could not index" would send the pusher hunting for a problem with
        # their commit that does not exist.
        block(f"your draft is in plm, but the SUBMISSION was refused ({number})",
              f"{exc}\n\n"
              "The branch and the DRAFT revision are there. Fix the cause and "
              "push to refs/for/main\nagain — or pass the option the message "
              "names (e.g. -o explode_waiver='<why>').")
        return 1
    except RuntimeError as exc:
        block(f"plm could not index this commit ({number})",
              f"{exc}\n\n"
              "That message is plm's own, not a translation. The branch is "
              "there — your commit\nwas not lost — but plm does not know about "
              "it. Fix the cause and push again;\nre-pushing the same branch "
              "re-indexes the same DRAFT.")
        return 1
    except Exception:  # noqa: BLE001
        block(f"plm hit an internal error indexing this push ({number})",
              traceback.format_exc(limit=6))
        return 1
    finally:
        if ref == config.PUSH_REF:
            push.git("update-ref", "-d", config.PUSH_REF, check=False)
    return 0


class SubmitRefused(RuntimeError):
    """plm answered the submit with a refusal (4xx) — the draft itself landed."""


#: seconds a `refs/for/main` push waits for its commit's checks before it
#: submits (stage 16b); 0 turns the wait off
SUBMIT_WAIT_S = float(os.environ.get("PLMGIT_SUBMIT_WAIT_S", "60"))


def _settle_checks(number: str, sha: str) -> list[str]:
    """Wait (bounded) for plm's checks on the pushed commit; the push output
    lines that say how they came out. Never raises — a submit is not blocked
    by a board it could not read; it says so instead."""
    if SUBMIT_WAIT_S <= 0:
        return []
    url = (f"{config.GITFIRST_PLM_URL}/api/repo/{urllib.request.quote(number)}"
           f"/checks?sha={sha}&wait={SUBMIT_WAIT_S:g}")
    try:
        with urllib.request.urlopen(url, timeout=SUBMIT_WAIT_S + 30) as r:
            d = json.loads(r.read() or b"{}")
    except Exception as exc:                                # noqa: BLE001
        return [f"plm: checks    (could not read the board: {exc}) — "
                "submitting anyway"]
    rows = d.get("checks") or []
    busy = [c["name"] for c in rows if c.get("status") in ("queued", "running")]
    amber = [f"{c['name']} {c['status']}" for c in rows
             if c.get("status") in ("warn", "fail", "error", "skipped")]
    out = [f"plm: checks    {(d.get('rollup') or {}).get('state', '?')}"
           + (f" — {', '.join(amber[:6])}" if amber else "")]
    if busy:
        out.append(f"plm:           still running after {SUBMIT_WAIT_S:g} s: "
                   f"{', '.join(busy)} — get_checks('{number}', "
                   f"ref='{sha[:12]}', wait=120)")
    return out


def _submit(number: str, version: str, who: str, target: str,
            note: str, explode_waiver: str = "") -> dict:
    """Submit through plm's own API — `ops.submit_for_release`, the same one
    code path the UI and the MCP use. There is no git-specific submit — which
    is also why the EXPLODE GATE applies to a push with no code here: an
    assembly that does not explode is refused by ops, and the refusal names
    each child. `git push -o explode_waiver='<why>'` is the waiver."""
    url = (f"{config.GITFIRST_PLM_URL}/api/items/"
           f"{urllib.request.quote(number)}/submit")
    body = json.dumps({"version": version, "actor": who,
                       "target_state": target if target in
                       ("PROTOTYPE", "RELEASED") else "PROTOTYPE",
                       "note": note,
                       "explode_waiver": explode_waiver}).encode()
    # ⭐ THE SERVICE CREDENTIAL (stage 7, C2). plm refuses an anonymous write
    # now, and this IS a write — it is the submit that a `refs/for/main` push
    # means. The hook presents the same index token `plm_index` above uses;
    # `plm/identity.py::service_identity` explains why that token, and only that
    # token, may name the actor it authenticated itself (`who`, resolved from
    # the pusher's bearer by `plm_git.push.identify`).
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config.GITFIRST_INDEX_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw).get("detail", "")
        except Exception:  # noqa: BLE001
            detail = raw.decode(errors="replace")[:400]
        raise SubmitRefused(f"plm refused the submission: {detail}") from None


# -- installing the hooks -----------------------------------------------------

HOOK = """#!/bin/sh
# Written by plm_git.gitfirst.ensure_hooks — do not edit; it is rewritten.
exec python3 -m plm_git.gitfirst {name}
"""


def ensure_hooks(repo: Path) -> None:
    """Write the two hook shims into a bare repo, and turn receive-pack on.

    Lazily, on the receive-pack request, for the same reason the August service
    refreshed a repo on the ref advertisement: it is the one moment we know the
    repo is about to be written to, and it means `plm/split.py` can keep
    generating repos without knowing the write path exists.
    """
    hooks = repo / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-receive", "post-receive"):
        p = hooks / name
        want = HOOK.format(name=name)
        if not p.is_file() or p.read_text() != want:
            p.write_text(want)
            p.chmod(0o755)
    # split.py sets http.receivepack=false on a fresh repo (it was a read-only
    # service). Turning it on HERE rather than there keeps split's output
    # byte-identical and keeps the decision with the thing that authenticates.
    # ⚠️ `receive.fsckObjects`, `transfer.fsckObjects` and
    # `receive.maxInputSize` are deliberately NOT written here (M3, L9). They
    # arrive through `GIT_CONFIG_COUNT/KEY_n/VALUE_n` in the compose file,
    # the same env-scoped mechanism `uploadpack.allowFilter` already used — so
    # they apply to EVERY served repo including ones this function has never
    # touched, `plm/split.py`'s output stays byte-identical, and an operator can
    # see the whole hardening posture in one place instead of hunting for
    # `config` calls. These four are here because they are per-repo facts about
    # what this repo IS, not policy about how it is served.
    from plm import safegit
    for k, v in (("http.receivepack", "true"),
                 ("receive.advertisePushOptions", "true"),
                 ("receive.denyDeletes", "false"),
                 ("receive.denyNonFastForwards", "false")):
        safegit.git(repo, "config", "--end-of-options", k, v, check=False)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    which = args[0] if args else ""
    if which == "pre-receive":
        return pre_receive()
    if which == "post-receive":
        return post_receive()
    err(f"usage: python -m plm_git.gitfirst pre-receive|post-receive "
        f"(got {which!r})")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
