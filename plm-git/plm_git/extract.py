"""Carve one part out of the monorepo vault, WITH its real history.

The vault is one git repo holding every project. A part is a folder in it. To
`git clone` a part you need that folder promoted to the root of its own repo,
carrying the commits that actually touched it — the plm check-in messages, the
authors, the dates. That is a subtree extraction.

Why not `git subtree split`
---------------------------
`git subtree split -P <prefix>` is the obvious tool and it does work, but it is
prefix-literal: it only sees history in which THAT prefix existed. The vault has
real folder renames in its past (`parts/KML-PRT-1009` -> `..._integral-core-
diffuser-spider` -> `..._integrated-light-engine-spider`), and plm will produce
more of them every time a slug changes or an item is renumbered. Split against
the current prefix silently truncates the history at the rename. Stitching the
pieces back with `--onto` means one split per historical prefix plus a graft,
and it needs a work tree.

So we do the extraction with plain plumbing instead, which is both smaller and
rename-aware:

  for each vault commit, oldest first:
      find this item's folder IN THAT COMMIT (anchored on the item's number,
          not on a literal path)
      tree = <commit>:<that folder>
      if tree == last emitted tree: skip (the commit didn't touch this part)
      commit-tree tree -p <last emitted commit>  with the original
          author/committer/date/message, plus trailers pointing back at the
          monorepo commit it came from

`git commit-tree` is the same primitive subtree uses. The result is a normal
linear history whose blobs are the SAME objects as the vault's, so the extraction
is byte-identical to what plm checked in.

Safety: this never runs against the live vault working tree. Everything happens
in a --no-hardlinks mirror clone under the cache dir, so the extraction shares no
object files with the vault and cannot write to it even by accident.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .resolve import Item

STAMP = "plm-git.json"


class ExtractError(Exception):
    pass


def _run(cwd: Path | None, *args: str, input: bytes | None = None,
         env: dict | None = None, check: bool = True) -> str:
    """`check=False` for the plumbing whose failure is a legitimate answer —
    deleting a ref that is not there, verifying a ref that may not exist."""
    res = subprocess.run([config.GIT, *(["-C", str(cwd)] if cwd else []), *args],
                         capture_output=True, input=input,
                         env={**os.environ, **(env or {})})
    if res.returncode != 0 and check:
        raise ExtractError((res.stderr or res.stdout).decode(errors="replace").strip()[:2000])
    return res.stdout.decode(errors="replace")


# --------------------------------------------------------------------------
# the read-only mirror of the vault
# --------------------------------------------------------------------------

_last_fetch = 0.0


def ensure_mirror(refresh: bool = True, force: bool = False) -> Path:
    """Clone (or fetch) the vault into the cache. Read-only w.r.t. the vault.

    --no-hardlinks on purpose: the cache must not share object files with the
    vault's .git. It costs one copy of a ~150 MB repo, once.

    The fetch is rate-limited by PLMGIT_FRESHNESS_TTL. That TTL deliberately
    covers only THIS — the one network/disk-heavy step — and never the staleness
    comparison, so a cache that is known-stale is never served just because it
    was checked a moment ago.
    """
    global _last_fetch
    m = config.MIRROR
    if not (m / "HEAD").exists():
        m.parent.mkdir(parents=True, exist_ok=True)
        if m.exists():
            shutil.rmtree(m)
        _run(None, "clone", "--mirror", "--no-hardlinks", str(config.VAULT), str(m))
        _last_fetch = time.time()
        return m
    # The same cache dir gets used from a host shell (vault at a host path)
    # and from the container (vault at /vault). Re-point origin every time so
    # whichever process is running can still fetch.
    if _run(m, "remote", "get-url", "origin").strip() != str(config.VAULT):
        _run(m, "remote", "set-url", "origin", str(config.VAULT))
    if refresh and (force or time.time() - _last_fetch >= config.FRESHNESS_TTL):
        # A --mirror clone's refspec is +refs/*:refs/*, so this tracks branches,
        # tags and deletions. Fetching mid-commit is safe: worst case we see the
        # previous ref and refresh again on the next request.
        _run(m, "fetch", "--prune", "--quiet", "origin")
        _last_fetch = time.time()
    return m


def vault_head(m: Path) -> str:
    for ref in ("HEAD", "refs/heads/master", "refs/heads/main"):
        try:
            return _run(m, "rev-parse", ref).strip()
        except ExtractError:
            continue
    raise ExtractError("mirror has no HEAD/master/main")


# --------------------------------------------------------------------------
# lineage: where did this item's folder live, commit by commit
# --------------------------------------------------------------------------

def _candidates(m: Path, commit: str, pattern: re.Pattern) -> list[str]:
    out = _run(m, "ls-tree", "-r", "-d", "--name-only", commit)
    return [l for l in out.splitlines() if pattern.search(l)]


def _pick(cands: list[str], current: str) -> str:
    """Choose the candidate that is really this item's folder.

    An item number can appear in more than one directory path in a commit —
    the vault keeps an archived copy of the pre-plm registry under
    `projects/<p>/old/local-registry/parts/<NUMBER>_<slug>/`, which matches the
    number just as well as the live folder does. Rank by how much of the
    CURRENT (plm-authoritative) folder path the candidate shares: the live
    lineage shares `projects/<p>/parts`, the archive diverges one level higher.
    """
    cur = current.split("/")

    def common(p: str) -> int:
        n = 0
        for a, b in zip(cur, p.split("/")):
            if a != b:
                break
            n += 1
        return n

    return max(cands, key=lambda p: (p == current, common(p), -len(p)))


def lineage(m: Path, item: Item) -> list[tuple[str, str]]:
    """[(commit, folder-in-that-commit)] oldest first, only commits where the
    item's folder existed. Anchored on the item's numbers (current + former),
    which is what survives a slug rename or a renumber."""
    alt = "|".join(re.escape(n) for n in item.anchors)
    pattern = re.compile(rf"(^|/)({alt})(_[^/]*)?$")
    commits = _run(m, "rev-list", "--reverse", "--topo-order",
                   vault_head(m)).split()
    out = []
    for c in commits:
        cands = _candidates(m, c, pattern)
        if cands:
            out.append((c, _pick(cands, item.folder)))
    return out


def folder_head(m: Path, prefixes: list[str], head: str = "HEAD") -> str:
    """The newest vault commit touching any of these paths — the staleness key.

    One `rev-list -1` over the item's known historical prefixes. Cheap, and it
    stays correct across a FUTURE rename: a rename commit necessarily deletes
    files under the old prefix, so it still shows up here, the rebuild runs, and
    the rebuild rediscovers the lineage from scratch.
    """
    if not prefixes:
        return ""
    return _run(m, "rev-list", "-1", head, "--", *prefixes).strip()


def _tree(m: Path, commit: str, folder: str) -> str:
    return _run(m, "rev-parse", f"{commit}:{folder}").strip()


# --------------------------------------------------------------------------
# the replay
# --------------------------------------------------------------------------

_FMT = "%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B"


def _meta(m: Path, commit: str) -> tuple[dict, str]:
    raw = _run(m, "show", "-s", f"--format={_FMT}", commit)
    an, ae, ad, cn, ce, cd, body = raw.split("\0", 6)
    env = {"GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae, "GIT_AUTHOR_DATE": ad,
           "GIT_COMMITTER_NAME": cn, "GIT_COMMITTER_EMAIL": ce,
           "GIT_COMMITTER_DATE": cd}
    return env, body


def replay(m: Path, item: Item) -> tuple[str, int, list[str]]:
    """Build the per-part history inside the mirror.
    Returns (tip sha, n commits, historical prefixes seen)."""
    lin = lineage(m, item)
    if not lin:
        raise ExtractError(f"no vault history for {item.number} "
                           f"(folder {item.folder!r} never appears)")
    tip, last_tree = "", None
    n = 0
    for c, folder in lin:
        tree = _tree(m, c, folder)
        if tree == last_tree:
            continue  # this vault commit didn't touch the part
        env, body = _meta(m, c)
        body = body.rstrip("\n")
        # Trailers: the extracted repo stays traceable to the monorepo it came
        # from, which is what makes a later push -> plm check-in reconcilable.
        msg = (f"{body}\n\n"
               f"Plm-Item: {item.number}\n"
               f"Plm-Vault-Commit: {c}\n"
               f"Plm-Vault-Path: {folder}\n")
        args = ["commit-tree", tree]
        if tip:
            args += ["-p", tip]
        tip = _run(m, *args, input=msg.encode(), env=env).strip()
        last_tree, n = tree, n + 1
    ref = f"refs/plm-git/{item.number}"
    _run(m, "update-ref", ref, tip)
    return tip, n, sorted({f for _, f in lin})


# --------------------------------------------------------------------------
# the served bare repo
# --------------------------------------------------------------------------

def repo_path(number: str) -> Path:
    return config.REPOS / f"{number}.git"


# --------------------------------------------------------------------------
# the write path's furniture: hooks, proposal refs, draft refs
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent.parent


def _dirty_flag(number: str) -> Path:
    d = config.CACHE / "dirty"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{number}.moved"


def mark_dirty(number: str) -> None:
    """"plm-git asked plm to write the vault, so plm-git KNOWS the vault moved."

    The freshness TTL exists to stop every object request re-fetching the vault,
    and it is right for reads. It is wrong immediately after a push: the pusher's
    very next `git fetch` would consult a mirror that predates the commit plm just
    made on plm-git's own instruction, and the DRAFT ref would appear to be
    missing. A marker file rather than a variable, because the hook is a separate
    process from the server — nothing else is shared between them.
    """
    _dirty_flag(number).write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")


def take_dirty(number: str) -> bool:
    p = _dirty_flag(number)
    if p.exists():
        p.unlink(missing_ok=True)
        return True
    return False


def install_hooks(repo: Path) -> None:
    """Write the receive hooks into a freshly generated repo.

    They have to be (re)installed on every build because a build creates the repo
    from scratch — the same reason the draft refs are re-derived from the ledger
    rather than kept in the repo.

    The shims are three lines each: git's hook protocol is stdin + exit status, and
    all of the thinking lives in plm_git.hook where it can be read and tested.
    """
    hooks = repo / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for name in ("pre-receive", "post-receive"):
        p = hooks / name
        p.write_text(
            "#!/bin/sh\n"
            f"# plm-git {name}: a push is a PROPOSAL, and plm decides. See plm_git/push.py.\n"
            f"PYTHONPATH={APP_DIR}${{PYTHONPATH:+:$PYTHONPATH}} \\\n"
            f"  exec {sys.executable} -m plm_git.hook {name}\n")
        p.chmod(0o755)
    # An `update` hook is the belt to pre-receive's braces: even if a future
    # change let a bad ref through pre-receive, the served branch cannot move.
    upd = hooks / "update"
    upd.write_text(
        "#!/bin/sh\n"
        "# The served branch is a projection of the vault. Nothing pushes to it.\n"
        'case "$1" in\n'
        f'  refs/heads/*) echo "plm-git: $1 is written by plm, never by a push" >&2; exit 1;;\n'
        f'  refs/plm/drafts/*) echo "plm-git: draft refs are plm-git\'s; approval is not a push" >&2; exit 1;;\n'
        "esac\n"
        "exit 0\n")
    upd.chmod(0o755)


def carry_proposal_refs(src: Path, dst: Path) -> int:
    """Copy refs/plm/incoming/* (and their objects) into a rebuilt repo.

    Those refs are the receipts for what was actually pushed — the one thing in
    here that cannot be re-derived from the vault, because plm rewrites the bytes
    into its own layout. Losing them on a refresh would lose the only record of
    the tree a human or agent handed over.
    """
    if not (src / "HEAD").exists():
        return 0
    try:
        refs = [l.split()[1] for l in
                _run(src, "for-each-ref", "--format=%(objectname) %(refname)",
                     "refs/plm/incoming").splitlines() if l.strip()]
    except ExtractError:
        return 0
    if not refs:
        return 0
    _run(dst, "fetch", "--quiet", str(src),
         *[f"+{r}:{r}" for r in refs])
    return len(refs)


def apply_draft_refs(number: str, repo: Path) -> list[str]:
    """Advertise every open proposal as refs/plm/drafts/<NUMBER>/v<version>.

    This is the "pull request" view, and it costs nothing to provide: plm told us
    the vault commit its check-in produced, and every replayed commit carries a
    `Plm-Vault-Commit:` trailer, so the extracted commit for a DRAFT is a grep.
    A reviewer then gets `git diff main...FETCH_HEAD` on a part they have never
    touched, using tooling nobody had to write.

    Refs for revisions that are no longer open (approved, rejected, withdrawn) are
    dropped: the point of the ref is to review something pending.
    """
    from . import push
    entries = push.ledger(number)
    if not entries:
        return []
    # vault commit -> extracted commit, from the trailers, in one log pass.
    mapping: dict[str, str] = {}
    try:
        out = _run(repo, "log", "--format=%H%x00%B%x01",
                   f"refs/heads/{config.BRANCH}")
    except ExtractError:
        return []
    for rec in out.split("\x01"):
        if "\0" not in rec:
            continue
        sha, _, body = rec.strip().partition("\0")
        for line in body.splitlines():
            if line.startswith("Plm-Vault-Commit:"):
                mapping[line.split(":", 1)[1].strip()] = sha.strip()
    live = _open_versions(number)
    made = []
    for e in entries:
        ref = push.draft_ref(number, e.get("version", ""))
        target = mapping.get(e.get("vault_commit") or "")
        if target and e.get("version") in live:
            _run(repo, "update-ref", ref, target)
            made.append(ref)
        else:
            _run(repo, "update-ref", "-d", ref, check=False)
    return made


def _open_versions(number: str) -> set[str]:
    """Versions of this item that are still awaiting a decision, per plm."""
    from .plmapi import Plm, PlmError
    from . import push
    try:
        item = Plm(config.PUSH_PLM_URL).item(number)
    except PlmError:
        return {e.get("version", "") for e in push.ledger(number)}
    return {r["version"] for r in (item.get("revisions") or [])
            if r.get("state") in ("DRAFT", "IN_REVIEW")}


def read_stamp(number: str) -> dict:
    p = repo_path(number) / STAMP
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def is_stale(item: Item, m: Path) -> tuple[bool, str, str]:
    """(stale?, cached staleness key, current staleness key).

    Three tiers, cheapest first:
      1. no repo / no stamp                     -> stale
      2. vault HEAD unchanged since the build   -> fresh, zero git calls
      3. one rev-list over the cached prefixes  -> compare
    """
    st = read_stamp(item.number)
    if not st or not repo_path(item.number).exists():
        return True, st.get("folder_head", ""), ""
    head = vault_head(m)
    cached = st.get("folder_head", "")
    if st.get("vault_head") == head:
        return False, cached, cached
    prefixes = sorted({*(st.get("prefixes") or []), item.folder})
    cur = folder_head(m, prefixes, head)
    if cur == cached:
        # Vault moved but not this part: remember that so the next check is free.
        st["vault_head"] = head
        (repo_path(item.number) / STAMP).write_text(json.dumps(st, indent=2) + "\n")
    return cur != cached, cached, cur


def build(item: Item, force: bool = False, refresh_mirror: bool = True) -> dict:
    """Ensure {CACHE}/repos/{NUMBER}.git is a bare repo holding the part's
    current content and real history. Idempotent; cheap when already fresh."""
    t0 = time.time()
    moved = take_dirty(item.number)
    m = ensure_mirror(refresh=refresh_mirror, force=moved)
    stale, cached, cur = is_stale(item, m)
    if moved:
        force = True
    if not stale and not force:
        # A proposal's state can change in plm without the vault moving (an
        # approval is a state transition, not a commit), so the PR refs are
        # re-evaluated even on the cheap path — but only when this part has ever
        # had a proposal, so an ordinary clone still costs zero extra calls.
        drafts: list[str] = []
        if config.ALLOW_PUSH:
            from . import push as _push
            if _push.ledger(item.number):
                try:
                    drafts = apply_draft_refs(item.number, repo_path(item.number))
                except Exception:  # noqa: BLE001
                    drafts = []
        return {**read_stamp(item.number), "action": "cached",
                "draft_refs": drafts,
                "elapsed_s": round(time.time() - t0, 2)}

    tip, ncommits, prefixes = replay(m, item)
    head = vault_head(m)
    cur = folder_head(m, prefixes, head)
    dst = repo_path(item.number)
    tmp = dst.with_suffix(".git.tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    _run(None, "init", "--bare", "--quiet", f"--initial-branch={config.BRANCH}",
         str(tmp))
    # Fetching one ref pulls exactly the objects that ref reaches: the part's
    # blobs and nothing else from the 150 MB monorepo.
    _run(tmp, "fetch", "--quiet", str(m), f"refs/plm-git/{item.number}:refs/heads/{config.BRANCH}")
    _run(tmp, "symbolic-ref", "HEAD", f"refs/heads/{config.BRANCH}")
    _run(tmp, "config", "http.uploadpack", "true")
    # The write path, per-repo. Both halves of the switch are honest: with push
    # off, receive-pack is refused at the router AND disabled here, so there are
    # two independent reasons a push cannot land.
    _run(tmp, "config", "http.receivepack",
         "true" if config.ALLOW_PUSH else "false")
    # Structural protections that do not depend on a hook being correct.
    _run(tmp, "config", "receive.denyDeletes", "true")
    _run(tmp, "config", "receive.denyNonFastForwards", "true")
    _run(tmp, "config", "receive.denyCurrentBranch", "refuse")
    # The proposal ref is transient plumbing, not something to serve; the draft
    # and incoming refs ARE for reviewers, so they stay advertised.
    _run(tmp, "config", "uploadpack.hideRefs", "refs/for")
    install_hooks(tmp)
    carried = carry_proposal_refs(dst, tmp) if dst.exists() else 0
    _run(tmp, "gc", "--quiet", "--aggressive", "--prune=all")
    (tmp / "description").write_text(
        f"{item.number} — {item.name} (extracted from the plm CAD vault)\n")
    stamp = {
        "number": item.number, "name": item.name, "folder": item.folder,
        "former_numbers": item.former_numbers, "resolver": item.source,
        "branch": config.BRANCH, "tip": tip, "commits": ncommits,
        "prefixes": prefixes,
        "folder_head": cur, "previous_folder_head": cached,
        "vault_head": head,
        "push_enabled": config.ALLOW_PUSH,
        "incoming_refs_carried": carried,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (tmp / STAMP).write_text(json.dumps(stamp, indent=2) + "\n")
    if dst.exists():
        old = dst.with_suffix(".git.old")
        if old.exists():
            shutil.rmtree(old)
        dst.rename(old)
        tmp.rename(dst)
        shutil.rmtree(old)
    else:
        tmp.rename(dst)
    # The "pull request" refs, re-derived from the proposal ledger now that the
    # replayed history (and its Plm-Vault-Commit trailers) exists again.
    drafts: list[str] = []
    if config.ALLOW_PUSH:
        try:
            drafts = apply_draft_refs(item.number, dst)
        except Exception as exc:  # noqa: BLE001 — a missing PR ref must not fail a clone
            drafts = [f"(draft refs unavailable: {exc})"]
    return {**stamp, "action": "rebuilt", "draft_refs": drafts,
            "elapsed_s": round(time.time() - t0, 2)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .resolve import NotFound, resolve

    ap = argparse.ArgumentParser(
        prog="python -m plm_git.extract",
        description="Generate the bare per-part git repo for an item number.")
    ap.add_argument("number", nargs="+")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if the cache is already up to date")
    ap.add_argument("--no-refresh", action="store_true",
                    help="skip the vault fetch (use the mirror as-is)")
    ap.add_argument("--lineage", action="store_true",
                    help="print the folder lineage instead of building")
    a = ap.parse_args(argv)
    rc = 0
    for num in a.number:
        try:
            item = resolve(num)
        except NotFound as e:
            print(f"404 {num}: {e}")
            rc = 1
            continue
        if a.lineage:
            m = ensure_mirror(refresh=not a.no_refresh)
            prev = None
            print(f"{item.number}  anchors={item.anchors}")
            for c, f in lineage(m, item):
                if f != prev:
                    print(f"  {c[:10]}  {f}")
                    prev = f
            continue
        info = build(item, force=a.force, refresh_mirror=not a.no_refresh)
        print(f"{item.number}: {info['action']} "
              f"commits={info.get('commits','?')} "
              f"folder_head={info.get('folder_head','')[:10]} "
              f"({info['elapsed_s']}s)  -> {repo_path(item.number)}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
