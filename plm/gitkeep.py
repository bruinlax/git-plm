"""Every commit plm relies on is reachable from a ref in the repo that owns it.

THE HOLE THIS CLOSES (stage 11b). plm pins things to commits of OTHER repos: a
parent's gitlink `parts/<CHILD>`, a `[[relations]] commit = …`, a
`revisions.git_commit`, a release's `tag_sha`, a note's relation entry, the pin
closure an evidence file writes down. Nothing made those commits reachable in
the repo that owns them. A draft branch is deleted after a merge or a
withdrawal, a draft is force-pushed, a regenerated repo re-points its refs —
and the pinned commit is still there only because nothing has run `git gc`.
The stage-11 restore drill restored every ref exactly and
`git clone --recurse-submodules KML-ASM-1000` STILL failed: a gitlink pinned a
KML-ASM-1100 commit no ref reached.

ONE RULE, ONE HELPER. `keep(number, sha, why)` writes `refs/plm/keep/<sha>` in
the owning repo — under that repo's write lock, as a compare-and-swap create,
idempotent — and appends a line to `plm-keep.jsonl` saying why.
`ensure_reachable(number, sha)` does it only when no ref at all reaches the
commit yet. That is the whole mechanism; every pin site calls one of the two,
and `python -m plm.gitkeep --audit` proves the rule holds over everything.

WHEN, AND WHY IT IS TWO MOMENTS NOT ONE.

  * At PIN time, in the TARGET repo (`protect` / `ensure_reachable`): a gitlink
    a commit writes, a relation commit a claim records. Cheap — a commit that
    some ref already reaches costs one `rev-list -n1` and writes nothing.
  * At ORPHAN time (`guard`): a draft branch deleted, a draft force-pushed, a
    generated repo re-split. At pin time the child commit was reachable — from
    a draft branch — so nothing was kept; deleting that branch is what orphans
    it. `guard` asks git which commits just lost their last ref
    (`rev-list <old> --not --all`) and keeps exactly those that something still
    pins, by the same scan the audit runs. Everything else is let go: a
    withdrawn draft nobody pins is exactly what gc is for.

  The guard runs AFTER the ref moved (the post-receive hook has no choice: git
  has already deleted the branch when it runs). That is safe because nothing
  prunes an unreachable object younger than `gc.pruneExpire` (two weeks by
  default), and these repos run with `gc.auto = 0` besides.

WHAT COUNTS AS "PINNED" (`scan`), every source the audit reports on:

  gitlink      `parts/<N>` / `catalog` in EVERY commit some ref of a repo
               reaches (keep-refs included — a kept parent pins its children)
  tree-claim   any `{item, commit}` entry in `plm.toml` at those commits
  note         any `{item, commit}` entry in the `refs/notes/plm` tip's notes
  item-ref     any `{item, commit}` entry in `refs/plm/item`'s item.toml
  revision     `revisions.git_commit` (a vault sha from the replay is not a
               repo pin and is skipped — it is checked against the vault)
  relation     `relations.target_commit`
  release      `releases.tag_sha`, `releases.note_commit`
  signoff      `release_signoffs.(item, tag_sha)`
  evidence     each frozen evidence file's `revision.tag_sha` and `pins[]`

KEEP-REFS ARE HIDDEN, NOT SECRET. The git service sets
`transfer.hideRefs=refs/plm/keep` (deploy compose): a clone does not download
hundreds of refs it has no use for, and a push cannot touch them (receive-pack
refuses an update to a hidden ref — on top of `check_ref_namespace`'s refusal of
all of `refs/plm/`). Their objects stay fetchable BY SHA because the service
also sets `uploadpack.allowAnySHA1InWant`, which is what
`git submodule update` asks for. Proven with a real recursive clone
(the keep demo script).

NO REPOS, NO-OP. Every entry point returns quietly when the repos root has no
repo for the number (PLM_GIT_MODE=off, a catalog item, a vault sha) — the
compat harness (`off` == `master`) depends on it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import config, safegit

KEEP_PREFIX = "refs/plm/keep/"
LEDGER = "plm-keep.jsonl"
ZERO = "0" * 40
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
MODE_GITLINK = b"160000"
MODE_TREE = b"40000"


class KeepError(RuntimeError):
    pass


def _root() -> Path:
    return config.REPOS


# -- which repo owns a number ---------------------------------------------------

def repo_for(number: str, root: Path | None = None) -> Path | None:
    """The bare repo for `number` (`catalog` included), following a renumber's
    `<OLD>.git.redirect`, or None when there is none — which is an answer
    (off mode, a catalog item), not an error."""
    root = root or _root()
    try:
        p = safegit.repo_dir(root, number)
    except safegit.SafeError:
        return None
    for _ in range(8):
        if (p / "HEAD").exists():
            return p
        red = p.with_name(p.name + ".redirect")
        if not red.is_file():
            return None
        nxt = red.read_text().strip()
        if not nxt.endswith(".git") or "/" in nxt:
            return None
        p = root / nxt
    return None


def _number_of(repo: Path) -> str:
    return repo.name[:-len(".git")]


def present(repo: Path, sha: str) -> bool:
    return safegit.ok(repo, "cat-file", "-e", f"{sha}^{{commit}}")


def reachable(repo: Path, sha: str) -> bool:
    """True when SOME ref of the repo reaches `sha` — a head, a tag, the notes,
    the item ref, a fork marker, or a keep-ref. One bounded walk: `rev-list`
    prints `sha` back exactly when no ref reaches it."""
    out = safegit.git(repo, "rev-list", "-n", "1", sha, "--not", "--all",
                      check=False).strip()
    return not out


# -- the one write --------------------------------------------------------------

def _lock(repo: Path):
    from . import gitwrite
    return gitwrite.lock(repo)


def keep(number: str, sha: str, why: str, *, closure: bool = True,
         root: Path | None = None, _seen: set | None = None) -> dict:
    """`refs/plm/keep/<sha>` in `number`'s repo. Idempotent; a CAS create under
    the repo's write lock. With `closure`, the kept commit's own gitlinks are
    made reachable in THEIR repos too — a kept parent is a parent somebody may
    still `git checkout` and `submodule update`."""
    repo = repo_for(number, root)
    if repo is None:
        raise KeepError(f"{number} has no repository to keep {sha[:10]} in")
    sha = safegit.safe_sha(sha).lower()
    if not _HEX40.match(sha):
        raise KeepError(f"keep needs a full commit id, not {sha!r}")
    if not present(repo, sha):
        raise KeepError(
            f"{_number_of(repo)} does not have commit {sha[:10]} — it is "
            "MISSING, and a keep-ref cannot\nbring back an object that is "
            "gone. That is a finding, not something to paper over.")
    ref = KEEP_PREFIX + sha
    created = False
    with _lock(repo):
        cur = safegit.git(repo, "rev-parse", "--verify", "--quiet", ref,
                          check=False).strip()
        if cur != sha:
            # CAS: create only if absent (or repair one pointing elsewhere,
            # which `update-ref` with its old value also makes atomic).
            safegit.git(repo, "update-ref", "-m", f"plm keep: {why}"[:200],
                        ref, sha, cur or ZERO)
            created = True
            with (repo / LEDGER).open("a") as fh:
                fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                     "sha": sha, "why": why}) + "\n")
    out = {"number": _number_of(repo), "sha": sha, "kept": True,
           "created": created, "why": why}
    if closure:
        _seen = _seen if _seen is not None else set()
        _seen.add((out["number"], sha))
        sub = []
        for path, child_sha in gitlinks(repo, sha).items():
            child = link_target(path)
            if not child or (child, child_sha) in _seen:
                continue
            _seen.add((child, child_sha))
            r = ensure_reachable(child, child_sha,
                                 f"pinned by kept {out['number']}@{sha[:10]}:{path}",
                                 root=root, _seen=_seen, strict=False)
            if r.get("created") or r.get("error"):
                sub.append(r)
        if sub:
            out["closure"] = sub
    return out


def ensure_reachable(number: str, sha: str, why: str = "", *,
                     closure: bool = True, root: Path | None = None,
                     strict: bool = True, _seen: set | None = None) -> dict:
    """Keep `sha` only when no ref reaches it. `{"reachable": True}` and no
    write when one does. With `strict=False` a missing repo or object is
    returned as `{"error": …}` instead of raised."""
    repo = repo_for(number, root)
    if repo is None or not sha:
        return {"number": number, "sha": sha, "skipped": "no repository"}
    try:
        sha = safegit.safe_sha(sha).lower()
        if not present(repo, sha):
            raise KeepError(f"{_number_of(repo)} does not have commit "
                            f"{sha[:10]} (MISSING)")
        if reachable(repo, sha):
            return {"number": _number_of(repo), "sha": sha, "reachable": True,
                    "created": False}
        return keep(number, sha, why or "pinned", closure=closure, root=root,
                    _seen=_seen)
    except (KeepError, safegit.SafeError) as exc:
        if strict:
            raise
        return {"number": number, "sha": sha, "error": str(exc)}


def protect(pins, *, where: str = "") -> list[dict]:
    """Pin-time keeps for a write that already happened — NEVER fatal.

    `pins` is `[(number, sha, why)]`. The write that made the pin is already in
    git (a pushed branch, a commit plm wrote), so a keep that fails must not
    turn into a registry that disagrees with git; it is logged, returned, and
    the standing audit (`--audit`) is what catches it afterwards."""
    out = []
    for number, sha, why in pins:
        if not number or not sha:
            continue
        try:
            r = ensure_reachable(number, sha, why, strict=False)
        except Exception as exc:  # noqa: BLE001 — never fails the write
            r = {"number": number, "sha": sha,
                 "error": f"{type(exc).__name__}: {exc}"}
        if r.get("error"):
            print(f"[gitkeep] {where}: could not keep {number}@{sha[:10]}: "
                  f"{r['error'][:200]}", file=sys.stderr, flush=True)
        if r.get("created") or r.get("error"):
            out.append(r)
    return out


# -- reading pins out of a tree -------------------------------------------------

def link_target(path: str) -> str:
    """`parts/KML-PRT-1013` -> `KML-PRT-1013`; `catalog` -> `catalog`;
    `lib/CCR-DOC-0001` -> `CCR-DOC-0001` (stage 15: a shared SOURCE pin keeps
    the same rule as a BOM child — what a tree pins stays fetchable)."""
    if path == "catalog":
        return "catalog"
    if (path.startswith("parts/") or path.startswith("lib/")) \
            and path.count("/") == 1:
        return path.split("/", 1)[1]
    return ""


def gitlinks(repo: Path, commit: str) -> dict[str, str]:
    """path -> pinned sha for the gitlinks plm reads (`parts/*`, `lib/*`,
    `catalog`)."""
    out = {}
    raw = safegit.git(repo, "ls-tree", "-r", commit, "--", "parts", "lib",
                      "catalog", check=False)
    for line in raw.splitlines():
        meta, _, path = line.partition("\t")
        bits = meta.split()
        if len(bits) == 3 and bits[0] == "160000" and link_target(path):
            out[path] = bits[2]
    return out


def tree_pins(repo: Path, commit: str) -> list[tuple[str, str, str]]:
    """(target, sha, kind) for every pin one commit makes: gitlinks and
    `{item, commit}` claims in its plm.toml. What a pin site protects."""
    out = [(link_target(p), s, "gitlink") for p, s in gitlinks(repo, commit).items()]
    raw = safegit.git(repo, "cat-file", "blob", f"{commit}:plm.toml",
                      check=False)
    out += [(n, s, "tree-claim") for n, s in _claims(raw)]
    return out


def _claims(text: str) -> list[tuple[str, str]]:
    """Every `{item = "N", commit = "<sha>"}` table anywhere in a TOML document
    — `[[relations]]` in a plm.toml, a note's `[[installed]]`, item.toml.
    Generic on purpose: a new relation home is a new array, not new code."""
    if not text or "commit" not in text:
        return []
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    out = []

    def walk(v):
        if isinstance(v, dict):
            it, c = v.get("item"), v.get("commit")
            if isinstance(it, str) and isinstance(c, str) and _HEX40.match(c):
                out.append((it, c))
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(doc)
    return out


# -- the batch reader the scan uses ---------------------------------------------

def _batch(repo: Path, names: list[str]) -> dict[str, tuple[str, bytes]]:
    """`git cat-file --batch` over many names: name -> (type, content)."""
    if not names:
        return {}
    res = subprocess.run(
        [safegit.GIT, "--git-dir", str(repo), "cat-file", "--batch"],
        input=("\n".join(names) + "\n").encode(), capture_output=True,
        env=safegit.clean_env(), timeout=safegit.TIMEOUT_LONG_S)
    out, buf, i = {}, res.stdout, 0
    for name in names:
        nl = buf.index(b"\n", i)
        head = buf[i:nl].decode(errors="replace").split()
        i = nl + 1
        if len(head) == 3 and head[1] in ("blob", "tree", "commit", "tag"):
            size = int(head[2])
            out[name] = (head[1], buf[i:i + size])
            i += size + 1
    return out


def _tree_entries(raw: bytes) -> list[tuple[bytes, str, str]]:
    out, i = [], 0
    while i < len(raw):
        sp = raw.index(b" ", i)
        nul = raw.index(b"\0", sp)
        mode, name = raw[i:sp], raw[sp + 1:nul].decode(errors="replace")
        sha = raw[nul + 1:nul + 21].hex()
        out.append((mode, name, sha))
        i = nul + 21
    return out


# -- the scan: every pin, from every source -------------------------------------

def _repos(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*.git") if (p / "HEAD").exists())


def _rev_list(repo: Path, *args: str) -> set[str]:
    return set(safegit.git(repo, "rev-list", *args, check=False,
                           timeout=safegit.TIMEOUT_LONG_S).split())


def reach_sets(repo: Path) -> tuple[set[str], set[str]]:
    """(reached by any ref, reached by a ref that is NOT a keep-ref)."""
    allc = _rev_list(repo, "--all")
    anchored = _rev_list(repo, f"--exclude={KEEP_PREFIX}*", "--all")
    return allc, anchored


def scan_repo(repo: Path, commits: set[str]) -> list[dict]:
    """The pins one repo's reachable history makes (gitlinks, tree claims),
    its notes and its item ref."""
    me = _number_of(repo)
    pins: list[dict] = []
    commits = sorted(commits)
    roots = _batch(repo, [f"{c}^{{tree}}" for c in commits])
    want_parts, want_toml = {}, {}
    for c in commits:
        t = roots.get(f"{c}^{{tree}}")
        if not t:
            continue
        for mode, name, sha in _tree_entries(t[1]):
            if name == "catalog" and mode == MODE_GITLINK:
                pins.append({"target": "catalog", "sha": sha, "kind": "gitlink",
                             "source": f"{me}@{c[:10]}:catalog"})
            elif name == "parts" and mode == MODE_TREE:
                want_parts.setdefault(sha, []).append(c)
            elif name == "plm.toml":
                want_toml.setdefault(sha, []).append(c)
    got = _batch(repo, sorted(want_parts) + sorted(want_toml))
    for tsha, cs in want_parts.items():
        for mode, name, sha in _tree_entries((got.get(tsha) or ("", b""))[1]):
            if mode == MODE_GITLINK:
                for c in cs:
                    pins.append({"target": name, "sha": sha, "kind": "gitlink",
                                 "source": f"{me}@{c[:10]}:parts/{name}"})
    for bsha, cs in want_toml.items():
        for n, s in _claims((got.get(bsha) or ("", b""))[1].decode(errors="replace")):
            for c in cs:
                pins.append({"target": n, "sha": s, "kind": "tree-claim",
                             "source": f"{me}@{c[:10]}:plm.toml"})
    # the notes TIP (older note commits say nothing current) and the item ref
    for ref, kind in (("refs/notes/plm", "note"), ("refs/plm/item", "item-ref")):
        tip = safegit.git(repo, "rev-parse", "--verify", "--quiet", ref,
                          check=False).strip()
        if not tip:
            continue
        listing = safegit.git(repo, "ls-tree", "-r", tip, check=False)
        blobs = [ln.split()[2] for ln in listing.splitlines()
                 if len(ln.split()) >= 3 and ln.split()[1] == "blob"]
        for b, (_, raw) in _batch(repo, blobs).items():
            for n, s in _claims(raw.decode(errors="replace")):
                pins.append({"target": n, "sha": s, "kind": kind,
                             "source": f"{me}:{ref}"})
    return pins


def db_pins(conn) -> list[dict]:
    """The registry's own pins, and the frozen evidence files'."""
    pins: list[dict] = []
    if conn is None:
        return pins

    def q(sql):
        try:
            return conn.execute(sql).fetchall()
        except Exception:  # noqa: BLE001 — an older schema lacks a column
            return []
    for r in q("SELECT i.number, r.version, r.git_commit FROM revisions r "
               "JOIN items i ON i.id=r.item_id WHERE r.git_commit!=''"):
        pins.append({"target": r[0], "sha": r[2], "kind": "revision",
                     "source": f"revisions {r[0]} v{r[1]}"})
    for r in q("SELECT f.number, t.number, r.target_commit, r.id FROM relations r "
               "JOIN items f ON f.id=r.item_id JOIN items t ON "
               "t.id=r.target_item_id WHERE r.target_commit!=''"):
        pins.append({"target": r[1], "sha": r[2], "kind": "relation",
                     "source": f"relations #{r[3]} {r[0]}->{r[1]}"})
    for r in q("SELECT i.number, r.version, r.tag_sha, r.note_commit, r.id "
               "FROM releases r JOIN items i ON i.id=r.item_id"):
        for col, sha in (("tag_sha", r[2]), ("note_commit", r[3])):
            if sha:
                pins.append({"target": r[0], "sha": sha, "kind": "release",
                             "source": f"releases #{r[4]} {r[0]} v{r[1]} {col}"})
    # a signoff's `item` is the checklist LINE it signs ("Instructions
    # reviewed"); the commit it signed is the release's own tag.
    for r in q("SELECT i.number, s.tag_sha, s.release_id, s.id FROM "
               "release_signoffs s JOIN releases rl ON rl.id=s.release_id "
               "JOIN items i ON i.id=rl.item_id WHERE s.tag_sha!=''"):
        pins.append({"target": r[0], "sha": r[1], "kind": "signoff",
                     "source": f"release_signoffs #{r[3]} (release {r[2]})"})
    # The review model (stage 13): a thread is anchored AT a commit and a
    # review records the head it looked at. Each is already a keep-ref
    # (`proposals.keep_sha`); listing them here is what lets `--audit` say
    # WHY such a commit is pinned, and catches a keep that failed.
    for r in q("SELECT i.number, t.anchor_sha, t.id FROM threads t JOIN items i "
               "ON i.id=t.item_id WHERE t.anchor_sha!=''"):
        pins.append({"target": r[0], "sha": r[1], "kind": "thread",
                     "source": f"threads #{r[2]}"})
    for r in q("SELECT i.number, v.head_sha, v.id FROM reviews v JOIN proposals p "
               "ON p.id=v.proposal_id JOIN items i ON i.id=p.item_id "
               "WHERE v.head_sha!=''"):
        pins.append({"target": r[0], "sha": r[1], "kind": "review",
                     "source": f"reviews #{r[2]}"})
    ev = Path(os.environ.get("PLM_EVIDENCE_DIR", "/var/evidence"))
    for f in sorted(ev.glob("*/*.json")) if ev.is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        num = (d.get("item") or {}).get("number", "")
        tag = (d.get("revision") or {}).get("tag_sha", "")
        if num and tag:
            pins.append({"target": num, "sha": tag, "kind": "evidence",
                         "source": f"evidence {f.parent.name}/{f.name}"})
        for p in d.get("pins") or []:
            if p.get("child") and p.get("commit"):
                pins.append({"target": p["child"], "sha": p["commit"],
                             "kind": "evidence",
                             "source": f"evidence {f.parent.name}/{f.name} pin "
                                       f"{p.get('parent')}:{p.get('path')}"})
    return pins


def _present_many(git_args: list[str], shas: list[str]) -> set[str]:
    """Which of `shas` are commits in one object store — ONE `cat-file
    --batch-check`, not a process per sha (the replay's ~600 vault shas)."""
    if not shas:
        return set()
    try:
        res = subprocess.run(
            [safegit.GIT, *git_args, "cat-file", "--batch-check"],
            input=("\n".join(f"{s}^{{commit}}" for s in shas) + "\n").encode(),
            capture_output=True, env=safegit.clean_env(),
            timeout=safegit.TIMEOUT_LONG_S)
    except Exception:  # noqa: BLE001
        return set()
    out = set()
    for want, line in zip(shas, res.stdout.decode(errors="replace").splitlines()):
        bits = line.split()
        if len(bits) == 3 and bits[1] == "commit":
            out.add(want)
    return out


def _vault_args() -> list[str] | None:
    v = config.DATA_ROOT
    if (v / ".git").exists():
        return ["-C", str(v)]
    if (v / "HEAD").exists():
        return ["--git-dir", str(v)]
    return None


def audit(conn=None, *, fix: bool = False, root: Path | None = None,
          rounds: int = 6) -> dict:
    """Classify every pinned commit: reachable / kept / UNREACHABLE-BUT-PRESENT
    / MISSING. With `fix`, keep the third class and re-scan until nothing new
    is kept (a kept parent's gitlinks are pins too)."""
    root = root or _root()
    created: list[dict] = []
    for _ in range(rounds):
        rep = _classify(conn, root)
        todo = rep["unreachable"]
        if not fix or not todo:
            rep["kept_now"] = created
            return rep
        for p in todo:
            try:
                created.append(keep(p["target"], p["sha"],
                                    f"{p['kind']}: {p['source']}", root=root,
                                    closure=False))
            except (KeepError, safegit.SafeError) as exc:
                created.append({"number": p["target"], "sha": p["sha"],
                                "error": str(exc)})
    rep = _classify(conn, root)
    rep["kept_now"] = created
    return rep


def _classify(conn, root: Path) -> dict:
    t0 = time.time()
    pins: list[dict] = []
    sets: dict[str, tuple[set, set]] = {}
    for repo in _repos(root):
        allc, anchored = reach_sets(repo)
        sets[repo.name] = (allc, anchored)
        pins += scan_repo(repo, allc)
    pins += db_pins(conn)
    by: dict[tuple[str, str], dict] = {}
    for p in pins:
        k = (p["target"], p["sha"])
        e = by.setdefault(k, {"target": p["target"], "sha": p["sha"],
                              "kind": p["kind"], "source": p["source"],
                              "sources": 0})
        e["sources"] += 1
    out = {"reachable": [], "kept": [], "unreachable": [], "missing": [],
           "vault": [], "pins": len(pins)}
    loose: dict[str, list[dict]] = {}       # repo name -> pins no ref reaches
    novault: list[dict] = []
    for (target, sha), e in sorted(by.items()):
        repo = repo_for(target, root)
        if repo is None:
            e["why"] = f"{target} has no repository"
            novault.append(e)
            continue
        e["repo"] = repo.name
        allc, anchored = sets.get(repo.name) or reach_sets(repo)
        if sha in anchored:
            out["reachable"].append(e)
        elif sha in allc:
            out["kept"].append(e)
        else:
            loose.setdefault(repo.name, []).append(e)
    for name, es in sorted(loose.items()):
        have = _present_many(["--git-dir", str(root / name)],
                             [e["sha"] for e in es])
        for e in es:
            (out["unreachable"] if e["sha"] in have else novault).append(e)
    # A replayed revision's `git_commit` is a VAULT commit (split, stage 1), not
    # a commit of its item repo: not a repo pin, and not missing either.
    va = _vault_args()
    in_vault = _present_many(va, [e["sha"] for e in novault
                                  if e["kind"] == "revision"]) if va else set()
    for e in novault:
        if e["kind"] == "revision" and e["sha"] in in_vault:
            e.pop("why", None)
            out["vault"].append(e)
        else:
            out["missing"].append(e)
    out["seconds"] = round(time.time() - t0, 2)
    out["repos"] = len(sets)
    return out


# -- orphan time ------------------------------------------------------------------

def orphaned(number: str, olds, root: Path | None = None) -> set[str]:
    """Commits reachable from `olds` that no ref of the repo reaches now."""
    repo = repo_for(number, root)
    olds = [o for o in (olds or []) if o and o != ZERO]
    if repo is None or not olds:
        return set()
    olds = [o for o in olds if present(repo, o)]
    if not olds:
        return set()
    return _rev_list(repo, *olds, "--not", "--all")


def _where_used(conn, number: str, root: Path) -> list[Path]:
    """The repos that could pin `number`: the parents and claimants the
    registry knows (every revision's BOM rows and relations, so a parent's OLD
    revision counts), the item itself, and for `catalog` every assembly that
    buys anything. A scoped scan keeps a withdraw at well under a second; the
    standing `--audit` is the global backstop for anything this misses."""
    names = {number}
    if conn is not None:
        try:
            from . import manifest
            if number == "catalog":
                kinds = tuple(sorted(manifest.CATALOG_KINDS))
                rows = conn.execute(
                    "SELECT DISTINCT p.number FROM bom b JOIN revisions r ON "
                    "r.id=b.parent_rev_id JOIN items p ON p.id=r.item_id JOIN "
                    "items c ON c.id=b.child_item_id WHERE c.kind IN "
                    f"({','.join('?' * len(kinds))})", kinds).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT p.number FROM bom b JOIN revisions r ON "
                    "r.id=b.parent_rev_id JOIN items p ON p.id=r.item_id JOIN "
                    "items c ON c.id=b.child_item_id WHERE c.number=? "
                    "UNION SELECT DISTINCT f.number FROM relations x JOIN items "
                    "f ON f.id=x.item_id JOIN items t ON t.id=x.target_item_id "
                    "WHERE t.number=?", (number, number)).fetchall()
            names |= {r[0] for r in rows}
        except Exception:  # noqa: BLE001 — no registry: scan everything
            return _repos(root)
    else:
        return _repos(root)
    return [r for r in (repo_for(n, root) for n in sorted(names)) if r]


def guard(conn, number: str, olds, why: str = "", *,
          root: Path | None = None) -> dict:
    """After a ref moved or went away: keep what just lost its last ref IF
    something still pins it; let the rest go. NEVER fatal (see `protect`).

    HOW IT DECIDES. `orphaned` asks git which commits no ref reaches any more.
    For those only, the pins are gathered — the registry's (`db_pins`: a
    revision row still pointing at it, a relation, a release, a signoff, an
    evidence file) and the trees of every repo that could pin this item
    (`_where_used`). Pinned → `keep` (with its gitlink closure). Not pinned →
    left for gc: a withdrawn draft nobody pinned is exactly what gc is for."""
    root = root or _root()
    try:
        gone = orphaned(number, olds, root)
        if not gone:
            return {}
        repo = repo_for(number, root)
        names = {number, _number_of(repo)}
        pins = db_pins(conn)
        for r in _where_used(conn, number, root):
            pins += scan_repo(r, _rev_list(r, "--all"))
        hit: dict[str, dict] = {}
        for p in pins:
            if p["target"] in names and p["sha"] in gone:
                hit.setdefault(p["sha"], p)
        kept, errs = [], []
        for sha, p in sorted(hit.items()):
            try:
                kept.append(keep(number, sha, f"{why}; {p['kind']}: "
                                 f"{p['source']}", root=root))
            except (KeepError, safegit.SafeError) as exc:
                errs.append({"sha": sha, "error": str(exc)})
        out = {"orphaned": len(gone), "kept": [k["sha"] for k in kept],
               "let_go": len(gone) - len(kept), "why": why}
        if errs:
            out["errors"] = errs
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"[gitkeep] guard {number} ({why}) failed: {exc}",
              file=sys.stderr, flush=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


# -- CLI ----------------------------------------------------------------------------

def _print(rep: dict, verbose: bool) -> None:
    print(f"plm.gitkeep audit — {rep['repos']} repos, {rep['pins']} pin "
          f"references, {rep['seconds']}s")
    for k, label in (("reachable", "reachable"), ("kept", "kept (keep-ref)"),
                     ("unreachable", "UNREACHABLE-BUT-PRESENT"),
                     ("missing", "MISSING"), ("vault", "vault sha (not a repo pin)")):
        print(f"  {label:<28} {len(rep[k])}")
    for k in ("unreachable", "missing"):
        for e in rep[k][: (None if verbose else 40)]:
            print(f"    {k.upper():<11} {e['target']} {e['sha'][:12]}  "
                  f"({e['kind']}: {e['source']}"
                  + (f", +{e['sources'] - 1} more" if e['sources'] > 1 else "")
                  + (f"; {e['why']}" if e.get("why") else "") + ")")
    if rep.get("kept_now"):
        made = [k for k in rep["kept_now"] if k.get("created")]
        errs = [k for k in rep["kept_now"] if k.get("error")]
        print(f"  keep-refs created now        {len(made)}"
              + (f"   errors {len(errs)}" if errs else ""))
        for e in errs[:20]:
            print(f"    ERROR {e['number']} {e['sha'][:12]}: {e['error'][:160]}")


def lfs_audit(root: Path | None = None) -> dict:
    """Stage 17: every LFS POINTER in every repo's object store resolves to an
    object in plm's LFS store, at its size.

    The repos are backed up as git (bundles) and the store as files (the
    sidecar's DATA_TREES); this is the join between the two — what a restore
    has to satisfy for a pointer to mean anything. Every blob object counts,
    reachable or not (`--batch-all-objects`): a bundle carries what a ref
    reaches, and a pointer nobody reaches costs nothing to also check."""
    from . import lfs
    root = root or _root()
    rep = {"repos": 0, "pointers": 0, "objects": set(), "bytes": 0,
           "missing": []}
    for repo in sorted(root.glob("*.git")):
        if not (repo / "HEAD").exists():
            continue
        rep["repos"] += 1
        try:
            listing = safegit.git(
                repo, "cat-file", "--batch-all-objects",
                "--batch-check=%(objectname) %(objecttype) %(objectsize)")
        except safegit.SafeError:
            continue
        small = []
        for ln in listing.splitlines():
            f = ln.split()
            if len(f) == 3 and f[1] == "blob" and f[2].isdigit() \
                    and 0 < int(f[2]) <= lfs.POINTER_MAX:
                small.append(f[0])
        if not small:
            continue
        for name, (kind, body) in _batch(repo, small).items():
            ptr = lfs.parse_pointer(body if isinstance(body, bytes)
                                    else str(body).encode())
            if not ptr:
                continue
            rep["pointers"] += 1
            if ptr[0] not in rep["objects"]:
                rep["objects"].add(ptr[0])
                rep["bytes"] += ptr[1]
            if not lfs.has(*ptr):
                rep["missing"].append(f"{repo.name[:-4]} blob {name[:12]} -> "
                                      f"{ptr[0][:12]}… ({ptr[1]:,} bytes)")
    rep["objects"] = len(rep["objects"])
    rep["store"] = lfs.store_stats()
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m plm.gitkeep",
        description="Every commit plm pins must be reachable from a ref in the "
                    "repo that owns it. --audit reports; --fix keeps.")
    ap.add_argument("--audit", action="store_true", help="classify every pin")
    ap.add_argument("--fix", action="store_true",
                    help="create refs/plm/keep/<sha> for UNREACHABLE-BUT-PRESENT")
    ap.add_argument("--repos", help="repos root (default: PLM_REPOS)")
    ap.add_argument("--no-db", action="store_true",
                    help="repo pins only (a copy with no matching registry)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--lfs", action="store_true",
                    help="stage 17: every LFS pointer in every repo resolves "
                         "to an object in plm's LFS store")
    args = ap.parse_args(argv)
    if args.lfs:
        r = lfs_audit(Path(args.repos) if args.repos else None)
        print(f"plm.gitkeep --lfs: {r['repos']} repos, {r['pointers']} LFS "
              f"pointer blob(s) -> {r['objects']} distinct object(s), "
              f"{r['bytes']:,} bytes; store {r['store']['objects']} object(s), "
              f"{r['store']['bytes']:,} bytes; {len(r['missing'])} MISSING")
        for m in r["missing"][:20]:
            print(f"    MISSING {m}")
        return 1 if r["missing"] else 0
    if not (args.audit or args.fix):
        ap.print_help()
        return 2
    root = Path(args.repos) if args.repos else _root()
    conn = None
    if not args.no_db:
        from . import db
        conn = db.connect()
    try:
        rep = audit(conn, fix=args.fix, root=root)
    finally:
        if conn is not None:
            conn.close()
    if args.json:
        print(json.dumps({k: (len(v) if isinstance(v, list) and k not in
                              ("missing", "unreachable", "kept_now") else v)
                          for k, v in rep.items()}, indent=2, default=str))
    else:
        _print(rep, args.verbose)
    bad = len(rep["unreachable"]) + len(rep["missing"])
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
