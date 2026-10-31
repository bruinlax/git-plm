"""The two git hooks, as one module.

`git receive-pack` runs these; the bare repo's `hooks/pre-receive` and
`hooks/post-receive` are three-line shims that exec this
(`extract.install_hooks` writes them on every rebuild, because a rebuild
recreates the repo from scratch).

    pre-receive   the cheap "no". Runs while the pushed objects are still in the
                  quarantine store git created for them (GIT_QUARANTINE_PATH),
                  and BEFORE any ref moves. A non-zero exit makes git throw the
                  quarantine away: a rejected push leaves nothing behind at all.

    post-receive  plm does the write. Cannot fail the push any more, so its job
                  is to do the plm calls and report honestly — including plm's own
                  refusal text, straight to git's stderr, if plm says no.

Everything printed to stderr here appears in the pusher's terminal prefixed
`remote:`. That is the only channel a git client gives us, so it is the whole
user interface of the write path, and it is written to be read.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

from . import config, extract, push
from .plmapi import Plm, PlmError

RULE = "─" * 68


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
    """The item number is the repo's name: <NUMBER>.git."""
    gd = Path(os.environ.get("GIT_DIR") or ".").resolve()
    name = gd.name
    return name[:-4] if name.endswith(".git") else name


def updates() -> list[tuple[str, str, str]]:
    out = []
    for line in sys.stdin.read().splitlines():
        bits = line.split()
        if len(bits) == 3:
            out.append((bits[0], bits[1], bits[2]))
    return out


def actor_from_env(plm: Plm) -> push.Actor:
    """The server verified the bearer token and put the result in the environment
    (REMOTE_USER is the CGI-standard name; the rest is detail plm-git adds). The
    hook does not see the token, only the identity it resolved to."""
    # ⛔ M2 — `PLMGIT_ACTOR` ONLY. `REMOTE_USER` was populated from the client
    # header `X-Plm-Actor` whenever the transport had not resolved a token, so
    # accepting it here made "the hook trusts nothing but a verified token"
    # false. The transport no longer sets it from a header; this no longer
    # reads it.
    name = os.environ.get("PLMGIT_ACTOR") or ""
    if not name:
        raise push.Refused(
            "this receive-pack was started without a verified actor, so plm-git cannot\n"
            "say who is proposing. Push over HTTP through plm-git (which authenticates\n"
            "the bearer token and sets the actor) rather than driving receive-pack\n"
            "directly.")
    kind = os.environ.get("PLMGIT_ACTOR_KIND") or ""
    roles = [r for r in (os.environ.get("PLMGIT_ACTOR_ROLES") or "").split(",") if r]
    if not kind or not roles:  # started outside the server: ask plm ourselves
        user = plm.user(name) or {}
        kind = kind or (user.get("kind") or "")
        roles = roles or [r.strip() for r in (user.get("roles") or "").split(",") if r.strip()]
    return push.Actor(name=name, kind=kind, roles=roles, via="token")


# --------------------------------------------------------------------------

def pre_receive() -> int:
    ups = updates()
    number = number_of_repo()
    try:
        push.gate()
        if len(ups) != 1:
            raise push.Refused(
                f"this push updates {len(ups)} refs at once. One push, one proposal —\n"
                "plm allocates one version per check-in and reviews one thing at a time.\n"
                f"\n  git push origin HEAD:{config.PUSH_REF}")
        old, new, ref = ups[0]
        plm = push.plm_client()
        actor = actor_from_env(plm)
        try:
            item = plm.item(number)
        except PlmError as exc:
            if exc.code == 404:
                raise push.Refused(
                    f"{number} is not an item in {config.PUSH_PLM_URL}. plm owns identity:\n"
                    "a part number exists because plm assigned it, and a push cannot create\n"
                    "one (a number carrying invented meaning is exactly what plm refuses).\n"
                    "\n"
                    "  register it first:  POST /api/items {name, kind, project, prefix}\n"
                    "  then check the first version in through plm, and propose the next\n"
                    "  one by push.") from None
            raise push.Refused(f"plm could not be asked about {number}: {exc}") from None
        if not item.get("folder"):
            raise push.Refused(
                f"{number} has no vault folder in plm — nothing has ever been checked in,\n"
                "so there is no revision to propose from.\n"
                "  check the first version in through plm, then push the next one.")
        prop = push.analyze(number, ref, old, new, actor, item)
        push.check_plm_state(prop, plm)
    except push.Refused as exc:
        block(f"plm-git refused this push ({number})", str(exc))
        err("Nothing was written: the pushed objects were in git's quarantine and")
        err("have been discarded. No ref moved, and plm was not touched.")
        err()
        return 1
    except Exception:  # noqa: BLE001 — never a bare traceback in a push
        block(f"plm-git hit an internal error validating this push ({number})",
              traceback.format_exc(limit=4))
        return 1
    err(f"plm-git: {number} v{prop.version} looks like a valid proposal "
        f"({prop.bump} from v{prop.latest}, {len(prop.files)} file(s)) — handing it to plm")
    return 0


def post_receive() -> int:
    ups = updates()
    number = number_of_repo()
    if not ups:
        return 0
    old, new, ref = ups[0]
    rep = push.Report()
    plm = push.plm_client()
    result = None
    prop = None
    try:
        with push.lock(number):
            actor = actor_from_env(plm)
            item = plm.item(number)
            prop = push.analyze(number, ref, old, new, actor, item)
            err()
            err(f"plm-git: proposing {number} v{prop.version} to {config.PUSH_PLM_URL}")
            err(RULE)
            result = push.propose(prop, plm, rep)
            # File the accepted commit as the quarantine receipt, then drop the
            # transient ref so the next push starts from a clean creation.
            inc = push.incoming_ref(prop)
            push.git("update-ref", inc, new, check=False)
            push.record(number, {
                "number": number, "version": prop.version,
                "actor": actor.name, "actor_kind": actor.kind,
                "pushed_commit": new, "based_on": prop.base_sha,
                "incoming_ref": inc, "summary": prop.summary,
                "vault_commit": result.get("vault_commit", ""),
                "revision_id": (result.get("revision") or {}).get("id"),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
            # plm just wrote the vault on our instruction, so bypass the freshness
            # TTL on the next request: the pusher's `git fetch` must see the DRAFT
            # ref, not a mirror that predates the commit we asked for.
            extract.mark_dirty(number)
            dref = push.draft_ref(number, prop.version)
            err(RULE)
            err(f"proposed:  {number} v{prop.version}  DRAFT -> IN_REVIEW "
                f"(awaiting a human approver)")
            err(f"review it: {config.PUSH_PLM_PUBLIC_URL}/item/{number}")
            err(f"receipt:   {inc}")
            err()
            err("the pull-request view — `git fetch` refreshes the mirror on the way:")
            err("  git fetch origin '+refs/plm/*:refs/plm/*'")
            err(f"  git diff -M -C --find-copies-harder {prop.base_sha[:10]}..{dref}")
            err("      ^ the copy-detection flags matter: a revision is a NEW version")
            err("        folder, so without them git reports adds instead of the change.")
            err(f"  git diff {dref} {inc}   # what plm changed on the way in")
            err()
            err(f"note: {config.BRANCH} will already carry this DRAFT. plm commits the")
            err("vault at CHECK-IN, not at release, so the mirror sees it immediately —")
            err("the release gate is the revision's STATE, not the branch. Diff against")
            err("the base commit above, not against main.")
            err()
            err("git push means PROPOSED. A human approving in plm is the merge.")
            err()
    except push.Refused as exc:
        block(f"plm refused this proposal ({number})", str(exc))
        return 1
    except PlmError as exc:
        block(f"plm refused this proposal ({number})",
              f"{exc}\n\n"
              "That message is plm's own, not a translation. plm is the authority on\n"
              "whether a revision may be written; plm-git only carried the bytes.\n"
              + (f"\nA DRAFT v{prop.version} may already exist — check\n"
                 f"{config.PUSH_PLM_URL}/item/{number} and withdraw it if it is not wanted."
                 if prop else ""))
        return 1
    except Exception:  # noqa: BLE001
        block(f"plm-git hit an internal error while proposing ({number})",
              traceback.format_exc(limit=6))
        return 1
    finally:
        # Always retire the transient proposal ref, success or failure. Its objects
        # are kept by the incoming ref when the proposal was accepted, and are
        # unreachable (and gc-able) when it was not — which is the correct outcome
        # for a proposal plm did not take.
        #
        # Deliberately NOT rebuilding the per-part repo here: a rebuild swaps the
        # repo directory by atomic rename, and this hook is a child of the
        # receive-pack that is running INSIDE that directory. The vault moved, so
        # the existing freshness check (extract.is_stale, on the next ref
        # advertisement) picks it up on the pusher's very next fetch — which is
        # exactly the command the output above tells them to run.
        push.git("update-ref", "-d", config.PUSH_REF, check=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    which = args[0] if args else ""
    if which == "pre-receive":
        return pre_receive()
    if which == "post-receive":
        return post_receive()
    err(f"usage: python -m plm_git.hook pre-receive|post-receive (got {which!r})")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
