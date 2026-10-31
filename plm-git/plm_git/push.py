"""`git push` == "propose a revision".

The thesis, in one paragraph. A push into the CAD vault would bypass the release
gate, so a push must not fast-forward anything: it must become a *proposal* and
plm must decide. git already has every primitive needed to say that — a
quarantine object store, a hook that runs before any ref moves, and a hook that
runs after — so the pipeline is:

    git push origin HEAD:refs/for/main
        │
        ├─ receive-pack lands the objects in a QUARANTINE dir (git does this
        │  itself, via GIT_QUARANTINE_PATH). No served ref has moved.
        │
        ├─ pre-receive  = the cheap "no". Ref namespace, tree shape, version
        │                 arithmetic, identity, size sanity, plm's own state.
        │                 Exit non-zero and git DELETES the quarantine: a
        │                 rejected push leaves not one object behind.
        │
        ├─ (objects migrate; refs/for/main now exists, and is hidden from
        │   upload-pack so it is never served)
        │
        └─ post-receive = plm does the write. revise -> DRAFT, put_files with
                          the changed bytes, add_context for provenance,
                          submit_for_release. Then the accepted commit is filed
                          as refs/plm/incoming/<N>/<actor>/<ts> and refs/for/main
                          is deleted.

What plm-git never does: write the vault, compute a MANIFEST, allocate a version,
or decide a state. Those are plm's, and plm-git reaches them only through
`plm/api.py`. The vault therefore stays linear and plm-authored — every commit in
it still comes from plm's own check-in code — which is what keeps the registry and
the vault from ever disagreeing.

And `git push` never means "released". It means "proposed". The merge is a human
approving in plm.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .plmapi import Plm, PlmError

ZERO = "0" * 40
VER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
VERSION_PATH_RE = re.compile(r"^versions/(v\d+\.\d+\.\d+)/(.+)$")

# role, per extension — plm uses these to decide where bytes live
# (storage.choose_backend pins 'source' and text to the vault, offloads bulk
# binaries to the object store). Getting the role right is how a .scad stays diffable in
# git while a 20 MB STL goes to the object store.
ROLE_SOURCE = {".scad", ".py", ".f3d", ".fcstd", ".ipynb", ".ino"}
ROLE_DRAWING = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".pdf", ".dxf"}
ROLE_DOC = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini",
            ".sha256", ".cfg"}


class Refused(Exception):
    """A refusal the pusher can act on.

    The message is the WHOLE thing printed to git's stderr — what is wrong, and
    the command that fixes it. Same house style as plm's own OpsError text: teach,
    do not emit a code.

    `needs_credential` decides 401-vs-403, and it is an explicit flag rather than
    a keyword sniffed out of the message. Sniffing got this wrong the first time:
    the "push is disabled here" text mentions PLMGIT_PUSH_TOKENS_FILE, so it was
    answered 401, and git dutifully went off asking for a password to a service
    that had told it the door does not exist.
    """

    def __init__(self, msg: str, needs_credential: bool = False):
        super().__init__(msg)
        self.needs_credential = needs_credential


# --------------------------------------------------------------------------
# the gate: is the write path even allowed to exist right now
# --------------------------------------------------------------------------

def push_target() -> str:
    return config.PUSH_PLM_URL


def looks_like_sandbox(url: str) -> bool:
    return "sandbox" in url.lower()


def gate() -> None:
    """Three independent conditions, all of which must hold. Any one of them is a
    refusal, and each refusal names the exact knob and what flipping it means."""
    if not config.ALLOW_PUSH:
        raise Refused(
            "push is disabled on this plm-git instance.\n"
            "plm is the release gate: a push has to become a DRAFT revision through\n"
            "plm's own check-in pipeline, and that pipeline is switched off here.\n"
            "\n"
            "  to enable it (and read 'Enabling push' in plm-git's README first):\n"
            "    PLMGIT_ALLOW_PUSH=1\n"
            "    PLMGIT_PUSH_PLM_URL=<the plm instance that should receive DRAFTs>\n"
            "    PLMGIT_PUSH_TOKENS_FILE=<token=actor map>\n"
            "\n"
            "  the sandbox instance (plm-git-sandbox, port 8199) has this on already.")
    if not looks_like_sandbox(config.PUSH_PLM_URL) and not config.PUSH_ALLOW_PROD:
        raise Refused(
            f"push is enabled, but its target plm is {config.PUSH_PLM_URL} — which is\n"
            "not a sandbox, and writing DRAFT revisions into the operator's system of record\n"
            "has to be a deliberate act rather than a default.\n"
            "\n"
            "  point it at the sandbox:   PLMGIT_PUSH_PLM_URL=http://plm-sandbox:8097\n"
            "  or say you meant prod:     PLMGIT_PUSH_ALLOW_PROD=1\n"
            "\n"
            "Nothing was written.")
    if not tokens():
        raise Refused(
            "push is enabled but no push tokens are configured, so no push could ever\n"
            "be attributed to a plm actor — and an unattributable revision is worse\n"
            "than a refused one.\n"
            "\n"
            "  set PLMGIT_PUSH_TOKENS_FILE=/run/secrets/plm-git-tokens with lines like\n"
            "    <token>=<plm username>\n"
            "  the username must already exist in plm (GET /api/users).")


# --------------------------------------------------------------------------
# identity: a bearer token, consumed the standard git way
# --------------------------------------------------------------------------

def tokens() -> dict[str, str]:
    """token -> plm username.

    ⭐ ONE STORE (stage 7). This used to parse the file itself; the parsing —
    including the per-line comment rule learned here — moved to `plm/tokens.py`
    so the API, the MCP and this transport cannot disagree about who a token is.
    The env names stay `PLMGIT_*`, because that is what the git container is
    configured with and renaming a knob to share an implementation would be the
    wrong half of the change.
    """
    from plm import tokens as store
    return store.load(path=config.PUSH_TOKENS_FILE or "",
                      env_raw=config.PUSH_TOKENS or "")


@dataclass
class Actor:
    name: str
    kind: str = ""
    roles: list[str] = field(default_factory=list)
    via: str = ""

    @property
    def is_human(self) -> bool:
        return self.kind == "human"


def bearer_of(headers: dict) -> str:
    auth = ""
    for k, v in headers.items():
        if k.lower() == "authorization":
            auth = v or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth.lower().startswith("basic "):
        # git's credential helper / `git push https://user:token@host/...` path.
        import base64
        try:
            userpass = base64.b64decode(auth[6:].strip()).decode()
        except Exception:  # noqa: BLE001
            return ""
        user, _, pw = userpass.partition(":")
        return pw or user
    return ""


def identify(token: str, plm: Plm) -> Actor:
    """Map a token to a plm actor, and refuse anything plm does not vouch for.

    Two layers on purpose. plm-git's token map says *who is holding this token*;
    plm's `users` table says *whether that person may edit*. plm-git never invents
    an actor and never grants a role — it only carries a name to plm, which is
    already the authority on what that name can do.
    """
    if not token:
        raise Refused(
            "this push carries no credential, and a push has to be attributable:\n"
            "plm records the pushing actor as the DRAFT's author, and 'anonymous'\n"
            "is not a person who can be asked about a design decision.\n"
            "\n"
            "  push with a token, the ordinary git way:\n"
            "    git -c http.extraHeader=\"Authorization: Bearer $PLM_TOKEN\" \\\n"
            "        push origin HEAD:refs/for/main\n"
            "\n"
            "  or store it once:\n"
            "    git config --local http.extraHeader \"Authorization: Bearer $PLM_TOKEN\"\n"
            "\n"
            "Reading stays anonymous on the private network; only writing needs a name.",
            needs_credential=True)
    # ⛔ L6 — `hmac.compare_digest`, over every entry, with no early exit. A
    # dict lookup is not the byte-by-byte oracle a bare `==` is, but
    # `POST /api/git/index` next door already did this and the two should not
    # differ about how carefully they compare a credential.
    from plm import tokens as store
    name = store.actor_in(tokens(), token)
    if not name:
        raise Refused(
            "that token is not one plm-git knows, so the push cannot be attributed\n"
            "to a plm actor. Nothing was written.\n"
            "\n"
            "  check you exported the right one, and that it matches a line in\n"
            "  PLMGIT_PUSH_TOKENS_FILE (<token>=<plm username>).",
            needs_credential=True)
    try:
        user = plm.user(name)
    except PlmError as exc:
        raise Refused(f"could not ask plm who '{name}' is: {exc}") from None
    if not user:
        raise Refused(
            f"the token maps to plm actor '{name}', but {push_target()} has no such\n"
            f"user — so plm would refuse the submission after the bytes had already\n"
            f"moved. Refusing here instead.\n"
            f"\n"
            f"  register the actor in plm first, then push again.")
    roles = [r.strip() for r in (user.get("roles") or "").split(",") if r.strip()]
    if "editor" not in roles:
        raise Refused(
            f"plm actor '{name}' has roles {roles or ['(none)']} and proposing a\n"
            f"revision is editing. plm owns who may do what; plm-git will not grant\n"
            f"a role it was not given.\n"
            f"\n"
            f"  ask an approver to add the 'editor' role to {name} in plm.")
    return Actor(name=name, kind=user.get("kind") or "", roles=roles, via="token")


# --------------------------------------------------------------------------
# git plumbing, inside the bare repo the hook is running in
# --------------------------------------------------------------------------

def git(*args: str, git_dir: str | None = None, check: bool = True) -> str:
    gd = git_dir or os.environ.get("GIT_DIR") or "."
    res = subprocess.run([config.GIT, "--git-dir", gd, *args],
                         capture_output=True, env=os.environ.copy())
    if check and res.returncode != 0:
        raise Refused(
            "git plumbing failed while inspecting the push "
            f"({' '.join(args[:3])}): "
            f"{(res.stderr or res.stdout).decode(errors='replace').strip()[:400]}")
    return res.stdout.decode(errors="replace")


@dataclass
class Entry:
    path: str          # path relative to the repo root (== the item folder)
    mode: str
    sha: str
    size: int

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def ext(self) -> str:
        return ("." + self.name.rsplit(".", 1)[-1].lower()) if "." in self.name else ""

    @property
    def is_text(self) -> bool:
        return self.ext in config.PUSH_TEXT_EXT

    @property
    def role(self) -> str:
        if self.ext in ROLE_SOURCE:
            return "source"
        if self.ext in ROLE_DRAWING:
            return "drawing"
        if self.ext in ROLE_DOC:
            return "doc"
        return "artifact"


def tree_entries(rev: str, git_dir: str | None = None) -> dict[str, Entry]:
    """Every blob in a commit's tree, keyed by path. `-l` gives sizes without
    reading the blobs, `-z` survives odd filenames."""
    out = git("ls-tree", "-r", "-l", "-z", "--full-tree", rev, git_dir=git_dir,
              check=False)
    entries: dict[str, Entry] = {}
    for rec in out.split("\0"):
        if not rec.strip():
            continue
        meta, _, path = rec.partition("\t")
        bits = meta.split()
        if len(bits) < 4:
            continue
        mode, _type, sha, size = bits[0], bits[1], bits[2], bits[3]
        entries[path] = Entry(path=path, mode=mode, sha=sha,
                              size=int(size) if size.isdigit() else 0)
    return entries


# --------------------------------------------------------------------------
# the proposal
# --------------------------------------------------------------------------

@dataclass
class Proposal:
    number: str
    actor: Actor
    ref: str
    old_sha: str
    new_sha: str
    base_sha: str
    version: str                 # "0.3.1" — no leading v, plm's form
    bump: str                    # major | minor | patch
    latest: str                  # plm's current latest version
    summary: str
    body: str
    commits: list[str]
    files: list[Entry]
    flattened: list[tuple[str, str]]
    item: dict


def _parse_version(v: str) -> tuple[int, int, int]:
    m = VER_RE.match(v)
    if not m:
        raise Refused(f"{v} is not a version folder name")
    return int(m[1]), int(m[2]), int(m[3])


def _bump_of(latest: str, target: str) -> str | None:
    la, li, lp = (int(x) for x in latest.split("."))
    for bump, want in (("major", (la + 1, 0, 0)),
                       ("minor", (la, li + 1, 0)),
                       ("patch", (la, li, lp + 1))):
        if tuple(int(x) for x in target.split(".")) == want:
            return bump
    return None


def analyze(number: str, ref: str, old_sha: str, new_sha: str, actor: Actor,
            item: dict, git_dir: str | None = None) -> Proposal:
    """Everything that can be decided from git alone. Raises Refused with the fix.

    Runs inside pre-receive, where the objects are still in the quarantine and no
    served ref has moved — so every `raise` here costs the pusher nothing but a
    message, and leaves the repo byte-for-byte as it was.
    """
    branch = config.BRANCH

    # --- 1. the ref namespace -------------------------------------------
    if new_sha == ZERO:
        raise Refused(
            f"this push deletes {ref}. plm-git's refs are projections of the vault —\n"
            "deleting one would not delete anything in plm, it would only make the\n"
            "mirror lie until the next refresh.\n"
            "\n"
            "  to retire a revision, do it in plm (it is a lifecycle state, not a\n"
            "  missing ref).")
    if ref != config.PUSH_REF:
        if ref.startswith("refs/heads/"):
            raise Refused(
                f"{ref} belongs to plm, not to you.\n"
                f"'{branch}' is a read-only projection of the CAD vault: every commit on\n"
                "it was written by plm's own check-in code, which is the only reason the\n"
                "registry and the vault can never disagree. A push that fast-forwarded it\n"
                "would put uncontrolled bytes into a controlled record.\n"
                "\n"
                "  propose the change instead — same commit, different ref:\n"
                f"    git push origin HEAD:{config.PUSH_REF}\n"
                "\n"
                "  or make it the default for this clone, once:\n"
                f"    git config remote.origin.push HEAD:{config.PUSH_REF}\n"
                "    git push\n"
                "\n"
                "What lands is a DRAFT revision plus a submitted approval. A human\n"
                "approving in plm is the merge.")
        if ref.startswith("refs/plm/drafts/"):
            raise Refused(
                f"{ref} is the review view of a DRAFT — the 'pull request' ref. It is\n"
                "plm-git's to write and yours to fetch.\n"
                "\n"
                "Pushing to it looks like an attempt to merge, and merging IS approval:\n"
                "a human with a verified identity decides that in plm, and bots never do\n"
                f"(plm refuses it — see PLM_REQUIRE_OIDC_APPROVALS). Push your work to\n"
                f"{config.PUSH_REF} and let the approval queue do its job.")
        if ref.startswith("refs/tags/"):
            raise Refused(
                f"{ref}: plm owns release tags (revisions.git_tag), and a tag pushed here\n"
                "would claim a release that no approver granted.\n"
                "\n"
                f"  push the work to {config.PUSH_REF}; the tag follows the approval.")
        raise Refused(
            f"{ref} is not a ref this service accepts.\n"
            f"\n  the one that is:  git push origin HEAD:{config.PUSH_REF}")

    # --- 2. one proposal per push ---------------------------------------
    base_sha = git("rev-parse", "--verify", f"refs/heads/{branch}",
                   git_dir=git_dir, check=False).strip()
    if not base_sha:
        raise Refused(
            f"this repo has no {branch} yet, so there is nothing to propose against.\n"
            "Check the part in through plm first (it is a new item, not a new commit).")
    if new_sha == base_sha:
        raise Refused(
            f"the pushed commit is already {branch} — there is nothing to propose.\n"
            "\n  commit your change first, then push.")
    if subprocess.run([config.GIT, "--git-dir", os.environ.get("GIT_DIR", git_dir or "."),
                       "merge-base", "--is-ancestor", base_sha, new_sha],
                      capture_output=True, env=os.environ.copy()).returncode != 0:
        raise Refused(
            f"the pushed commit is not built on the current {branch} ({base_sha[:10]}).\n"
            "plm's vault is linear and plm-authored; a proposal that forks off an older\n"
            "state would silently drop whatever landed in between.\n"
            "\n"
            "  bring yourself up to date and try again:\n"
            f"    git fetch origin && git rebase origin/{branch}\n"
            "\n"
            "  (this is also what you will see if two proposals race: the second one\n"
            "   rebases onto the first.)")

    # --- 3. the commit message becomes the revision summary -------------
    subject = git("log", "-1", "--format=%s", new_sha, git_dir=git_dir).strip()
    body = git("log", "-1", "--format=%b", new_sha, git_dir=git_dir).strip()
    commits = [l for l in git("log", "--format=%h %s", f"{base_sha}..{new_sha}",
                              git_dir=git_dir).splitlines() if l.strip()]
    if len(subject) < 12 or subject.lower() in {"wip", "update", "fix", "changes"}:
        raise Refused(
            f"the commit subject {subject!r} becomes the plm revision summary — the one\n"
            "line a future engineer reads in `git blame` and in the approval queue when\n"
            "deciding whether this change is safe to fabricate.\n"
            "\n"
            "  say what changed and why, then:\n"
            "    git commit --amend -m 'Bore opened 0.15 mm so 608 bearings press by hand'")

    # --- 4. the tree must look like a part folder -----------------------
    new_tree = tree_entries(new_sha, git_dir=git_dir)
    base_tree = tree_entries(base_sha, git_dir=git_dir)

    weird = [e for e in new_tree.values() if e.mode not in ("100644", "100755")]
    if weird:
        kinds = {"120000": "a symlink", "160000": "a submodule"}
        bad = ", ".join(f"{e.path} ({kinds.get(e.mode, 'mode ' + e.mode)})"
                        for e in weird[:5])
        raise Refused(
            f"this tree contains entries plm cannot check in: {bad}.\n"
            "A symlink checked into a version folder is a path escape waiting to happen,\n"
            "and a submodule is a promise plm has no way to keep (assemblies are BOMs,\n"
            "not submodules — yet).\n"
            "\n  replace them with real files and push again.")
    for path in new_tree:
        parts = Path(path).parts
        if ".." in parts or path.startswith("/") or ".git" in parts:
            raise Refused(
                f"{path} escapes the part folder (or hides inside .git). Every path in a\n"
                "pushed tree has to stay under the part, because plm writes it relative to\n"
                f"{item.get('folder')} in the vault.\n"
                "\n  remove it and push again.")

    version_dirs_new = {m[1] for p in new_tree
                        if (m := VERSION_PATH_RE.match(p))}
    version_dirs_base = {m[1] for p in base_tree
                         if (m := VERSION_PATH_RE.match(p))}
    if not version_dirs_new:
        raise Refused(
            "this does not look like a part folder: there is no versions/v<x.y.z>/\n"
            "directory in the pushed tree. plm stores a revision's dataset as\n"
            "  versions/v<major>.<minor>.<patch>/<files>\n"
            "and a proposal is a NEW one of those.\n"
            "\n"
            "  the usual move, from a fresh clone:\n"
            "    cp -r versions/v0.3.0 versions/v0.3.1   # next version\n"
            "    <edit>  &&  git add -A  &&  git commit")
    fresh = sorted(version_dirs_new - version_dirs_base)
    if not fresh:
        raise Refused(
            "every versions/ directory in this push already exists, so nothing here is a\n"
            "new revision. plm versions are IMMUTABLE — editing v"
            f"{sorted(version_dirs_base)[-1][1:] if version_dirs_base else '?'} in place is\n"
            "exactly what a PLM exists to prevent.\n"
            "\n"
            "  create the next version folder instead:\n"
            f"    cp -r versions/{sorted(version_dirs_new)[-1]} versions/v<next>\n"
            "\n"
            "  (iterating on an existing DRAFT is a plm operation — put_file onto the\n"
            "   DRAFT — not a push.)")
    if len(fresh) > 1:
        raise Refused(
            f"this push adds {len(fresh)} new version folders ({', '.join(fresh)}).\n"
            "One push, one revision: plm allocates one version per check-in, and a proposal\n"
            "that meant two of them cannot be reviewed or approved as one thing.\n"
            "\n  split it into one commit and one push per version.")
    vdir = fresh[0]
    version = vdir[1:]
    _parse_version(vdir)

    # nothing outside the new version folder may change
    changed_elsewhere = sorted(
        p for p, e in new_tree.items()
        if not p.startswith(f"versions/{vdir}/")
        and (p not in base_tree or base_tree[p].sha != e.sha))
    removed = sorted(p for p in base_tree if p not in new_tree)
    if changed_elsewhere:
        raise Refused(
            "this push also changes files outside versions/"
            f"{vdir}/:\n    " + "\n    ".join(changed_elsewhere[:8]) + "\n"
            "\n"
            "A revision's dataset is its own version folder; earlier version folders are\n"
            "immutable records, and ITEM.md is item-level master data plm maintains.\n"
            "\n"
            "  revert those paths and keep the push to the new version:\n"
            f"    git checkout origin/{branch} -- " + " ".join(changed_elsewhere[:3]) + "\n"
            "  (for ITEM.md-style facts, use plm: PATCH /api/items/<n> or add_context.)")
    if removed:
        raise Refused(
            "this push deletes controlled files:\n    " + "\n    ".join(removed[:8]) + "\n"
            "\n"
            "Released bytes are records — git keeps them and so does plm. Removing a file\n"
            "from a revision is a plm operation with a reason attached, not a tree edit.\n"
            "\n  restore them (git checkout origin/" + branch + " -- <paths>), and if a file\n"
            "  really must go, use plm: DELETE /api/files/<id>.")

    # --- 5. the payload -------------------------------------------------
    files = [e for p, e in sorted(new_tree.items())
             if p.startswith(f"versions/{vdir}/")
             and e.name != "MANIFEST.sha256"]
    if not files:
        raise Refused(
            f"versions/{vdir}/ holds nothing but MANIFEST.sha256. plm computes the\n"
            "manifest itself from the bytes it wrote — it is a receipt, not an input.\n"
            "\n  put the actual files (the .scad, the .stl, the notes) in the folder.")
    if len(files) > config.PUSH_MAX_FILES:
        raise Refused(
            f"{len(files)} files in one revision is over the {config.PUSH_MAX_FILES} limit.\n"
            "  if a part really needs that many, say so and the cap moves; more likely a\n"
            "  build directory got committed.")
    total = sum(e.size for e in files)
    over = [e for e in files if e.size > config.PUSH_MAX_FILE_BYTES]
    if over:
        e = over[0]
        raise Refused(
            f"{e.name} is {e.size:,} bytes, over the {config.PUSH_MAX_FILE_BYTES:,} byte\n"
            "per-file limit (plm's own PLM_RAW_UPLOAD_MAX).\n"
            "\n"
            "  git is the wrong transport for objects this size and plm-git will not\n"
            "  pretend otherwise — no git-lfs here. Attach it to the revision directly\n"
            "  through plm's staged upload flow instead.")
    if total > config.PUSH_MAX_TOTAL_BYTES:
        raise Refused(
            f"this push carries {total:,} bytes, over the "
            f"{config.PUSH_MAX_TOTAL_BYTES:,} byte total limit.\n"
            "  drop the derived artifacts that can be regenerated, or raise\n"
            "  PLMGIT_PUSH_MAX_TOTAL_BYTES if they are genuinely all controlled.")

    # plm writes version folders FLAT (plm/vault.py write_checkin takes the
    # basename). Nested paths still work, but they move — say so rather than
    # letting the pusher discover it in the next clone.
    flattened = [(e.path[len(f"versions/{vdir}/"):], e.name) for e in files
                 if "/" in e.path[len(f"versions/{vdir}/"):]]
    seen: dict[str, str] = {}
    for e in files:
        if e.name in seen:
            raise Refused(
                f"two files in versions/{vdir}/ are called {e.name}:\n"
                f"    {seen[e.name]}\n    {e.path}\n"
                "\n"
                "plm writes a version folder flat, so those two would collide and one\n"
                "would silently win.\n"
                "\n  rename one of them.")
        seen[e.name] = e.path

    # --- 6. version arithmetic, against plm's latest --------------------
    revisions = item.get("revisions") or []
    if not revisions:
        raise Refused(
            f"{number} has no revisions in plm yet, so there is nothing to revise from.\n"
            "  check the first version in through plm, then propose the next one by push.")
    existing = {r["version"] for r in revisions}
    latest = revisions[0]["version"]
    if version in existing:
        r = next(r for r in revisions if r["version"] == version)
        la, li, lp = (int(x) for x in latest.split("."))
        raise Refused(
            f"{number} v{version} already exists in plm ({r['state']}) — versions are\n"
            "IMMUTABLE. Whatever is in that folder is a record, and this push would try to\n"
            "mint it a second time.\n"
            "\n"
            f"  plm's latest is v{latest}, so the next version is one of\n"
            f"    v{la}.{li}.{lp + 1}   v{la}.{li + 1}.0   v{la + 1}.0.0\n"
            f"\n"
            f"    git mv versions/{vdir} versions/v{la}.{li}.{lp + 1}\n"
            f"    git commit --amend --no-edit && git push origin HEAD:{config.PUSH_REF}")
    latest_state = revisions[0]["state"]
    if latest_state in ("DRAFT", "IN_REVIEW"):
        raise Refused(
            f"{number} v{latest} is already {latest_state} — there is an open proposal on\n"
            "this part and plm allocates one DRAFT at a time.\n"
            "\n"
            "  resolve it first: approve or reject it in plm, or withdraw it\n"
            "  (POST /api/approvals/<id>/withdraw), then push again.\n"
            "  to add to that same DRAFT, put_file onto it through plm — a push mints a\n"
            "  new version by definition.")
    bump = _bump_of(latest, version)
    if not bump:
        la, li, lp = (int(x) for x in latest.split("."))
        raise Refused(
            f"v{version} is not the next version after plm's latest (v{latest}).\n"
            "plm allocates versions; plm-git only checks that the folder you created\n"
            "agrees with what plm would allocate. From v"
            f"{latest} the three legal next versions are\n"
            f"    v{la}.{li}.{lp + 1}   (patch — geometry-equivalent / fit tweak)\n"
            f"    v{la}.{li + 1}.0   (minor — a real design change)\n"
            f"    v{la + 1}.0.0   (major — not interchangeable with what came before)\n"
            "\n"
            f"  rename the folder to one of those:\n"
            f"    git mv versions/{vdir} versions/v{la}.{li}.{lp + 1} && git commit --amend --no-edit")

    return Proposal(number=number, actor=actor, ref=ref, old_sha=old_sha,
                    new_sha=new_sha, base_sha=base_sha, version=version,
                    bump=bump, latest=latest, summary=subject, body=body,
                    commits=commits, files=files, flattened=flattened, item=item)


def check_plm_state(prop: Proposal, plm: Plm) -> None:
    """The last cheap "no": plm's own concurrency control, asked before any bytes
    move. plm would refuse this later anyway (ops.revise raises 409) — asking now
    means the pusher hears it before a 20 MB upload rather than after."""
    try:
        co = plm.checkout(prop.number)
    except PlmError:
        return  # plm answered oddly; let the real call be the authority
    if co and co.get("actor") and co["actor"] != prop.actor.name:
        raise Refused(
            f"{prop.number} is checked out by {co['actor']} in plm"
            + (f" ({co.get('note')})" if co.get("note") else "") + ".\n"
            "That lock exists so two people do not revise the same part at once, and it\n"
            "is not plm-git's to override.\n"
            "\n"
            f"  wait for {co['actor']}, or ask them to release it\n"
            f"  (POST /api/items/{prop.number}/checkout/release).")


# --------------------------------------------------------------------------
# post-receive: plm does the write
# --------------------------------------------------------------------------

class Report:
    """Lines destined for git's stderr, i.e. `remote: …` in the push output."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def say(self, line: str = "") -> None:
        self.lines.append(line)
        print(line, file=sys.stderr, flush=True)


def _extract_blobs(prop: Proposal, dest: Path, git_dir: str | None = None) -> dict[str, Path]:
    """Write the pushed blobs to a temp dir so binaries can be streamed to
    plm's upload inbox without ever being held in memory or in a JSON body."""
    out: dict[str, Path] = {}
    for e in prop.files:
        if e.is_text:
            continue
        p = dest / e.name
        with p.open("wb") as fh:
            res = subprocess.run([config.GIT, "--git-dir",
                                  git_dir or os.environ.get("GIT_DIR", "."),
                                  "cat-file", "blob", e.sha],
                                 stdout=fh, stderr=subprocess.PIPE,
                                 env=os.environ.copy())
        if res.returncode != 0:
            raise Refused(f"could not read {e.path} out of the pushed objects: "
                          f"{res.stderr.decode(errors='replace')[:200]}")
        out[e.path] = p
    return out


def _blob_text(e: Entry, git_dir: str | None = None) -> str:
    raw = subprocess.run([config.GIT, "--git-dir",
                          git_dir or os.environ.get("GIT_DIR", "."),
                          "cat-file", "blob", e.sha],
                         capture_output=True, env=os.environ.copy()).stdout
    return raw.decode()


def _sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def propose(prop: Proposal, plm: Plm, rep: Report,
            git_dir: str | None = None) -> dict:
    """revise -> put_files -> add_context -> submit. Four plm calls, no vault write.

    Every failure surfaces plm's own sentence. If plm refuses halfway, the DRAFT it
    already created stays — visible, unsubmitted, and named in the output — because
    silently deleting a revision to tidy up would be a worse lie than an untidy
    registry.
    """
    number, actor = prop.number, prop.actor.name
    staged: list[str] = []

    # 1. open the DRAFT ---------------------------------------------------
    rep.say(f"plm: revise {number} {prop.bump} (v{prop.latest} -> v{prop.version}) as {actor}")
    rev = plm.revise(number, bump=prop.bump, summary=prop.summary, actor=actor)
    got = (rev.get("revision") or {}).get("version")
    if got != prop.version:
        raise Refused(
            f"plm allocated v{got}, not the v{prop.version} this push proposed — something\n"
            f"else revised {number} in between. The DRAFT v{got} exists and is yours to\n"
            "either use or withdraw; nothing was uploaded to it.\n"
            f"\n  git fetch origin && git rebase origin/{config.BRANCH}, rename the folder\n"
            "  to the version plm now expects, and push again.")
    rep.say(f"plm: DRAFT v{got} open (revision id {(rev.get('revision') or {}).get('id')}), "
            "files/BOM/attributes carried forward from v" + prop.latest)

    # 2. the bytes --------------------------------------------------------
    wire: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="plm-git-push-") as tmp:
        blobs = _extract_blobs(prop, Path(tmp), git_dir=git_dir)
        transport_used: set[str] = set()
        try:
            for e in prop.files:
                if e.is_text:
                    wire.append({"name": e.name, "role": e.role,
                                 "text": _blob_text(e, git_dir=git_dir)})
                    transport_used.add("inline text")
                    continue
                path = blobs[e.path]
                sha, size = _sha256(path), path.stat().st_size
                if config.PUSH_BINARY_TRANSPORT == "raw":
                    # bytes as the request body straight onto the DRAFT
                    plm.put_raw(number, path=path, name=e.name,
                                version=prop.version, role=e.role,
                                actor=actor,
                                message=f"plm-git: {prop.summary}")
                    transport_used.add("raw PUT to plm")
                    continue
                mint = plm.mint_upload(name=e.name, size=size, sha256=sha,
                                       actor=actor)
                staged.append(mint["handle"])
                plm.put_upload(mint["put_path"], path)
                wire.append({"name": e.name, "role": e.role,
                             "handle": mint["handle"], "sha256": sha,
                             "size": size})
                transport_used.add("staged upload handle")

            # 3. one working upload onto the DRAFT ------------------------
            res = {"files": [], "git_commit": ""}
            if wire:
                rep.say(f"plm: put_files {len(wire)} file(s) onto "
                        f"v{prop.version} ({', '.join(sorted(transport_used))})")
                res = plm.put_files(number, wire, version=prop.version,
                                    actor=actor,
                                    message=f"plm: {prop.summary}")
        except PlmError:
            for h in staged:
                plm.drop_upload(h, actor)
            raise

    for fr in res.get("files") or []:
        rep.say(f"  {fr.get('backend') or 'vault':<9} {fr.get('size', 0):>10,} B  "
                f"{fr.get('path')}")
    if prop.flattened:
        rep.say("plm: version folders are flat — these moved:")
        for src, dst in prop.flattened:
            rep.say(f"  {src}  ->  {dst}")
    vault_commit = res.get("git_commit") or ""
    if vault_commit:
        rep.say(f"plm: vault commit {vault_commit[:10]} (written by plm, not by git)")

    # 4. provenance -------------------------------------------------------
    ctx = (
        f"**Arrived by `git push`.** Proposed by `{actor}` "
        f"({prop.actor.kind or 'unknown kind'}) through plm-git.\n\n"
        f"- pushed commit: `{prop.new_sha}`\n"
        f"- based on: `{prop.base_sha}` (mirror `{config.BRANCH}`)\n"
        f"- quarantine ref: `{incoming_ref(prop)}`\n"
        f"- files: {', '.join(e.name for e in prop.files)}\n"
        + (f"- flattened: {', '.join(f'{s} -> {d}' for s, d in prop.flattened)}\n"
           if prop.flattened else "")
        + f"\n{prop.summary}\n"
        + (f"\n{prop.body}\n" if prop.body else "")
        + ("\nCommits in this push:\n"
           + "".join(f"- `{c}`\n" for c in prop.commits) if prop.commits else ""))
    try:
        plm.add_context(number, ctx, version=prop.version, actor=actor)
        rep.say("plm: context entry recorded (who pushed, which commit, what moved)")
    except PlmError as exc:
        rep.say(f"plm-git: WARNING could not record provenance context: {exc}")

    # 5. submit = open the pull request -----------------------------------
    sub = plm.submit(number, version=prop.version, actor=actor,
                     target_state=config.PUSH_TARGET_STATE,
                     note=f"Proposed by git push {prop.new_sha[:10]} "
                          f"({actor}); {prop.summary}")
    rep.say(f"plm: submit_for_release -> {sub.get('state')} "
            f"(target {config.PUSH_TARGET_STATE})")
    return {"revision": rev.get("revision") or {}, "files": res.get("files") or [],
            "vault_commit": vault_commit, "submitted": sub}


# --------------------------------------------------------------------------
# refs and the ledger
# --------------------------------------------------------------------------

def incoming_ref(prop: Proposal) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", prop.actor.name)
    return (f"refs/plm/incoming/{prop.number}/{safe}/"
            f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")


def draft_ref(number: str, version: str) -> str:
    return f"refs/plm/drafts/{number}/v{version}"


def ledger_path(number: str) -> Path:
    config.PROPOSALS.mkdir(parents=True, exist_ok=True)
    return config.PROPOSALS / f"{number}.jsonl"


def record(number: str, entry: dict) -> None:
    """Append a proposal to the ledger. It lives OUTSIDE the generated repo on
    purpose: a repo is rebuilt from the vault whenever the vault moves, and the
    draft ref has to survive that. Given the vault commit, the ref can always be
    re-derived, because every replayed commit carries a Plm-Vault-Commit trailer."""
    with ledger_path(number).open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def ledger(number: str) -> list[dict]:
    p = ledger_path(number)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class lock:
    """One push at a time per part, across processes.

    In-process locks are not enough here: each hook is a fresh child of
    git-http-backend, so the only thing two concurrent pushes share is the
    filesystem.
    """

    def __init__(self, number: str):
        config.LOCKS.mkdir(parents=True, exist_ok=True)
        self.path = config.LOCKS / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', number)}.push"
        self.fh = None

    def __enter__(self):
        self.fh = self.path.open("w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def plm_client() -> Plm:
    return Plm(config.PUSH_PLM_URL)
