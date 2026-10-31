"""Split the registry into REAL per-item git repos (docs/PLM_GIT_FIRST.md §5.1,
the design docs "Repo shape").

One item, one bare repo. One revision, one commit whose tree IS the revision —
the version folder is gone, which is the whole point: `git diff v0.4.0 v0.4.1`
finds the changed line without `-M -C --find-copies-harder`, because nothing was
copied. Releases are annotated tags, drafts are branches, BOM children are
submodules, and purchased/hardware items are one TOML each inside `catalog.git`
rather than a repo per screw (§5.4).

PLUMBING, NOT A WORKING TREE. Every repo is written with `hash-object`,
`update-index --index-info` against a throw-away index, `write-tree`,
`commit-tree` and `update-ref`. Checking out 619 revisions to build them would
mean writing ~1.8 GB of meshes to disk to hash bytes git already has, and it
would make the result depend on the filesystem. This way a rebuild is a few
seconds and the SHAs are a function of the data alone.

DETERMINISM IS A FEATURE, NOT AN ACCIDENT. No wall clock reaches a commit: every
author/committer/tagger date comes from the vault commit the revision was
checked in as, else from `revisions.date`, else from `revisions.created_at`.
Entries are sorted. Run it twice and every ref, every commit and every tag object
has the same SHA — `--verify-deterministic` proves it by building a second copy
into a scratch root and diffing the ref listings.

  python -m plm.split --all
  python -m plm.split --item KML-ASM-1000        (children are built first)
  python -m plm.split --verify-deterministic
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import re

from . import config, db, gitrepo, gitwrite, lfs, manifest, safegit

# Where the bare repos live. LOCAL DISK, never the synced volume: git object
# directories on NFS is how a repository comes back corrupt for no reason.
REPOS = config.REPOS          # one home: plm/config.py

CATALOG_REPO = "catalog"

BOT_NAME = "plm-split"
BOT_EMAIL = config.BOT_EMAIL     # one home: plm/config.py

GIT = "git"

MODE_BLOB = "100644"
MODE_EXEC = "100755"
MODE_GITLINK = "160000"


class SplitError(RuntimeError):
    pass


# -- git plumbing -------------------------------------------------------------

def _git(repo: Path, *args: str, stdin: bytes | None = None,
         env: dict | None = None, check: bool = True) -> str:
    try:
        return safegit.git(repo, *args, stdin=stdin, env=env, check=check,
                           timeout=safegit.TIMEOUT_LONG_S)
    except safegit.SafeError as exc:
        raise SplitError(f"git {' '.join(args)[:120]} in {repo}: {exc}") from None


def _ident(name: str, email: str, when: int) -> dict:
    """The environment that makes a commit a pure function of its inputs."""
    stamp = f"{when} +0000"
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": stamp}


def init_bare(path: Path) -> Path:
    """An empty bare repo, or the existing one. Idempotent: a rebuild reuses the
    object store (objects are content-addressed, so re-adding them is free) and
    only moves refs."""
    if not (path / "HEAD").exists():
        path.mkdir(parents=True, exist_ok=True)
        safegit.git(None, "init", "--bare", "-q", "--initial-branch=main",
                    "--end-of-options", str(path))
        # Read-only service: the transport refuses receive-pack too (§E), but a
        # repo that cannot be pushed to is one fewer thing to get wrong.
        _git(path, "config", "http.receivepack", "false")
        _git(path, "config", "gc.auto", "0")
    return path


def hash_blob(repo: Path, data: bytes) -> str:
    return _git(repo, "hash-object", "-w", "--stdin", stdin=data).strip()


def hash_file(repo: Path, p: Path) -> str:
    return _git(repo, "hash-object", "-w", "--no-filters", "--",
                str(p)).strip()


def lfs_blob(repo: Path, oid: str, size: int, path: str, *,
             pointer_ok: bool = True) -> tuple[str, bool]:
    """`(blob sha, is_pointer)` for an object in plm's LFS store, at tree `path`.

    The ONE answer to "this file's bytes are in the LFS store — what goes in
    the tree?", for both places split asks it: an `[[files.external]]` row
    stage 17 pulls into the tree, and a files row that is ALREADY `lfs`
    (re-pointed by `migrate_cleanup --only filedrop-repoint`, or indexed from a
    pushed pointer). The policy decides: the pointer when `lfs.wants_lfs`
    says so, else the plain bytes. `pointer_ok=False` (a split without the
    LFS layout) always writes the bytes. Raises `lfs.LfsError` when the object
    is not in the store.
    """
    if pointer_ok and lfs.wants_lfs(path, size):
        return hash_blob(repo, lfs.pointer(oid, size)), True
    return hash_blob(repo, lfs.read(oid)), False


def rev_ref(version: str, state: str) -> str:
    """The ref a revision lives at in its item's repo: `draft/vX.Y.Z` (a
    branch) while it is DRAFT/IN_REVIEW, the tag `vX.Y.Z` once released. The
    one spelling `split` writes and `migrate_cleanup` reads back."""
    if state in manifest.UNRELEASED_STATES:
        return f"draft/v{version}"
    return f"v{version}"


_POINTERS: dict[str, bool] = {}


def _is_pointer(repo: Path, sha: str) -> bool:
    """Is this blob an LFS pointer? (cached: one small read per blob)"""
    if sha not in _POINTERS:
        size = _git(repo, "cat-file", "-s", sha).strip()
        _POINTERS[sha] = bool(lfs.blob_pointer(
            repo, sha, int(size) if size.isdigit() else None))
    return _POINTERS[sha]


def write_tree(repo: Path, entries: list[tuple[str, str, str]]) -> str:
    """entries = [(mode, sha, path)] -> the tree SHA.

    One temp index per revision. `update-index --index-info` takes the whole
    listing on stdin, so this is two git calls for a tree of any size, and
    nested paths (`stl/foo.stl`) become subtrees for free.
    """
    idx = repo / f"split-index-{os.getpid()}"
    body = "".join(f"{mode} {sha}\t{path}\n"
                   for mode, sha, path in sorted(entries, key=lambda e: e[2]))
    env = {"GIT_INDEX_FILE": str(idx)}
    try:
        if idx.exists():
            idx.unlink()
        _git(repo, "update-index", "--index-info", stdin=body.encode(), env=env)
        return _git(repo, "write-tree", env=env).strip()
    finally:
        if idx.exists():
            idx.unlink()


def commit_tree(repo: Path, tree: str, parents: list[str], message: str,
                ident: dict) -> str:
    args = ["commit-tree", tree]
    for p in parents:
        args += ["-p", p]
    return _git(repo, *args, stdin=message.encode(), env=ident).strip()


def annotated_tag(repo: Path, name: str, target: str, message: str,
                  ident: dict) -> None:
    _git(repo, "-c", f"user.name={ident['GIT_COMMITTER_NAME']}",
         "-c", f"user.email={ident['GIT_COMMITTER_EMAIL']}",
         "tag", "-a", "-f", "-F", "-", name, target,
         stdin=message.encode(), env=ident)


def update_ref(repo: Path, ref: str, sha: str) -> None:
    _git(repo, "update-ref", ref, sha)


def existing_refs(repo: Path) -> dict[str, str]:
    out = {}
    for line in _git(repo, "for-each-ref",
                     "--format=%(refname) %(objectname)").splitlines():
        ref, _, sha = line.partition(" ")
        out[ref] = sha
    return out


# -- the vault: where a revision's bytes come from ----------------------------

class Vault:
    """The seeded vault clone, read two ways.

    A revision's files are on disk in its version folder — unless the folder was
    later removed or renamed, in which case the bytes are still in git history
    and `git show <revisions.git_commit>:<path>` gets them. 28 prod file rows
    are in neither (no folder on disk, no git_commit on the revision); those are
    reported, not guessed at.
    """

    def __init__(self, root: Path):
        self.root = root
        self._authors: dict[str, tuple[str, str, int]] = {}
        self.misses: list[str] = []

    def read(self, rel: str, git_commit: str = "", *,
             quiet: bool = False) -> tuple[bytes | None, Path | None]:
        p = self.root / rel
        if p.is_file():
            return None, p
        if git_commit:
            try:
                # ⚠️ The VAULT is a normal worktree, not a bare repo, so it is
                # addressed with `cwd=` (git's `-C`) and not `--git-dir`.
                return safegit.git(None, "show",
                                   f"{safegit.safe_ref(git_commit)}:{rel}",
                                   binary=True, cwd=str(self.root),
                                   timeout=safegit.TIMEOUT_LONG_S), None
            except safegit.SafeError:
                pass
        if not quiet:
            self.misses.append(rel)
        return None, None

    def author(self, commit: str) -> tuple[str, str, int] | None:
        """(name, email, epoch) of a vault commit — the real author of the
        check-in, so a generated repo says who did the work rather than
        attributing 619 revisions to a bot."""
        if not commit:
            return None
        if commit in self._authors:
            return self._authors[commit]
        try:
            raw = safegit.git(None, "show", "-s",
                              "--format=%an%x00%ae%x00%at",
                              safegit.safe_ref(commit), cwd=str(self.root))
        except safegit.SafeError:
            self._authors[commit] = None  # type: ignore[assignment]
            return None
        name, email, at = raw.strip().split("\0")
        val = (name, email, int(at))
        self._authors[commit] = val
        return val


def _epoch(rev: dict, vault_author) -> int:
    if vault_author:
        return vault_author[2]
    if rev.get("date"):
        try:
            return int(datetime.strptime(rev["date"], "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    return int(rev.get("created_at") or 0)


# -- the registry, read once --------------------------------------------------

def load(conn) -> dict:
    """Everything split needs, in four queries. Reading per item would be 282
    round trips per pass and would make the topological sort read the DB twice."""
    items = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM items")}
    revs: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute(
            "SELECT * FROM revisions ORDER BY item_id, vmaj, vmin, vpat, id"):
        revs[r["item_id"]].append(dict(r))
    boms: dict[int, list[dict]] = defaultdict(list)
    for b in conn.execute("SELECT * FROM bom ORDER BY parent_rev_id, id"):
        boms[b["parent_rev_id"]].append(dict(b))
    files: dict[int, list[dict]] = defaultdict(list)
    for f in conn.execute("SELECT * FROM files ORDER BY revision_id, path, id"):
        files[f["revision_id"]].append(dict(f))
    approvals: dict[int, list[dict]] = defaultdict(list)
    for a in conn.execute(
            "SELECT * FROM approvals WHERE decision='approved' ORDER BY id"):
        approvals[a["revision_id"]].append(dict(a))
    relations: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM relations ORDER BY id"):
        relations[r["item_id"]].append(dict(r))
    return {"items": items, "revs": revs, "boms": boms, "files": files,
            "approvals": approvals, "relations": relations}


def design_children(reg: dict, item_id: int) -> set[int]:
    """Every design-kind item that appears under ANY revision of this item.

    Item-level, not revision-level, because the build order is per repo: a
    child that is needed by v0.2.0 must exist before the repo is written at all.
    """
    out = set()
    for rev in reg["revs"].get(item_id, []):
        for b in reg["boms"].get(rev["id"], []):
            child = reg["items"].get(b["child_item_id"])
            if child and child["kind"] not in manifest.CATALOG_KINDS:
                out.add(child["id"])
    return out


def claim_targets(reg: dict, item_id: int) -> set[int]:
    """The items this item's REVISION-home claims point at — the ones whose
    commits its trees pin (stage 10). Note-home claims are not here: a note is
    written after every tree exists (`run`'s last pass), so it orders nothing —
    and an asset that constrains a rack while the rack is installed on it is a
    real two-way fact, not a cycle."""
    from . import relations as relmod
    return {r["target_item_id"] for r in reg["relations"].get(item_id, [])
            if relmod.home(r["relation"]) == "revision"
            and r["target_item_id"] in reg["items"]}


def build_order(reg: dict, roots: list[int] | None = None) -> tuple[list[int], list[list[str]]]:
    """Children before parents. Returns (order, cycles).

    A BOM cycle is a real registry fact (an assembly that reaches itself), not
    an error to crash on: the cycle members are reported and then built in
    number order with the back-edge gitlink omitted, so everything else still
    gets a correct repo.
    """
    wanted: set[int] = set()

    def walk(i: int) -> None:
        if i in wanted:
            return
        wanted.add(i)
        for c in design_children(reg, i):
            walk(c)

    if roots is None:
        for i, it in reg["items"].items():
            if it["kind"] not in manifest.CATALOG_KINDS:
                wanted.add(i)
    else:
        for r in roots:
            walk(r)

    # Kahn, children first. Ties broken by item NUMBER so the order — and so
    # every SHA downstream of it — is a function of the data, not of dict order.
    # A revision-home claim pins its target's COMMIT (stage 10), so the target
    # is built first too — but only ordered, never pulled in: `--item N` does
    # not rebuild what N's coupons point at (the pin is read off that repo).
    deps = {i: {c for c in design_children(reg, i) | claim_targets(reg, i)
                if c in wanted and c != i}
            for i in wanted}
    parents_of: dict[int, set[int]] = defaultdict(set)
    for i, cs in deps.items():
        for c in cs:
            parents_of[c].add(i)
    remaining = {i: len(cs) for i, cs in deps.items()}
    ready = sorted((i for i, n in remaining.items() if n == 0),
                   key=lambda i: reg["items"][i]["number"])
    order: list[int] = []
    while ready:
        i = ready.pop(0)
        order.append(i)
        for p in sorted(parents_of.get(i, ()),
                        key=lambda p: reg["items"][p]["number"]):
            remaining[p] -= 1
            if remaining[p] == 0:
                ready.append(p)
        ready.sort(key=lambda i: reg["items"][i]["number"])

    stuck = sorted(wanted - set(order), key=lambda i: reg["items"][i]["number"])
    cycles = [[reg["items"][i]["number"] for i in stuck]] if stuck else []
    order += stuck
    return order, cycles


# -- README ------------------------------------------------------------------

def readme(reg: dict, item: dict, m: dict, vault: Vault) -> bytes:
    """ITEM.md if the vault has one (it is already the item page), else a
    generated one. GitHub's convention: README.md renders under the tree."""
    if item.get("folder"):
        data, path = vault.read(f"{item['folder']}/ITEM.md", quiet=True)
        if path is not None:
            return path.read_bytes()
        if data is not None:
            return data
    lines = [f"# {item['number']} — {item['name']}", ""]
    if m["summary"]:
        lines += [m["summary"], ""]
    lines += ["| | |", "|---|---|",
              f"| Kind | {item['kind']} |",
              f"| Project | {m['project'] or '—'} |",
              f"| This ref | v{m['version']} ({m['state']}) |"]
    if m["process"]:
        lines.append(f"| Process | {m['process']} |")
    if m["tags"]:
        lines.append(f"| Tags | {', '.join(m['tags'])} |")
    lines += ["",
              "Everything plm knows about this revision is in `plm.toml`.",
              "Releases are annotated tags (`git tag`); drafts are `draft/*`",
              "branches; BOM children are submodules under `parts/`.", ""]
    if m["bom"]:
        lines += ["## BOM", "", "| Find | Path | Qty | Unit |", "|---|---|---|---|"]
        for b in m["bom"]:
            where = (f"`{b['path']}`" if b.get("path") else
                     f"requirement — not selected: {b.get('requirement', '')}")
            lines.append(f"| {b.get('find', '')} | {where} | "
                         f"{float(b.get('qty') or 1):g} | {b.get('unit', 'EA')} |")
        lines.append("")
    lines += [README_MARKER, ""]
    return "\n".join(lines).encode()


#: The line that makes a README plm's: it restates the revision (summary,
#: version, state, BOM), so the RELEASE commit regenerates it
#: (`gitindex.approve_commit`, stage 16). Delete the line and it is yours.
README_MARKER = "<!-- generated by plm/split.py from the plm registry -->"


def readme_for_doc(number: str, doc: dict) -> bytes:
    """`readme()` for a released `plm.toml` — what the release commit writes."""
    item = {"number": number, "name": doc.get("name") or number,
            "kind": doc.get("kind") or "", "folder": ""}
    bom = []
    for b in doc.get("bom") or []:
        try:
            qty = float(b.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        bom.append({"find": b.get("find", ""), "path": b.get("path") or "",
                    "qty": qty, "unit": b.get("unit") or "EA",
                    **({"requirement": b["requirement"]}
                       if b.get("requirement") else {})})
    m = {"summary": doc.get("summary") or "", "project": doc.get("project") or "",
         "version": doc.get("version") or "", "state": doc.get("state") or "",
         "process": doc.get("process") or "", "tags": list(doc.get("tags") or []),
         "bom": bom}
    return readme(None, item, m, None)


def gitmodules(subs: list[tuple[str, str]]) -> bytes:
    """`.gitmodules` with RELATIVE urls (§5.1), so the structure survives a host
    move or a GitHub mirror: `../KML-PRT-1013.git` resolves against whatever URL
    the parent was cloned from."""
    out = []
    for path, url in sorted(subs):
        out += [f'[submodule "{path}"]', f"\tpath = {path}", f"\turl = {url}"]
    return ("\n".join(out) + "\n").encode()


# -- the catalog repo ---------------------------------------------------------

def build_catalog(conn, reg: dict, vault: Vault, *, force: bool = False) -> dict:
    """`catalog.git` — purchased + hardware, one TOML per item (§5.4).

    A LINEAR HISTORY, one commit per catalog-item revision, ordered by date.
    That is what makes `git log -- HDW-0012.toml` and `git blame` mean something
    for a screw; a single squashed commit would make the catalog a snapshot with
    no history, which is the gitplm partmaster problem this is supposed to fix.
    """
    repo = init_bare(REPOS / f"{CATALOG_REPO}.git")
    n_items = len([i for i in reg["items"].values()
                   if i["kind"] in manifest.CATALOG_KINDS])
    if (repo / gitwrite.MARKER).is_file() and not force:
        # ⛔ THE SAME GUARD build_item HAS, which this function never had. Once
        # `plm/catalog.py` has committed into catalog.git (stage 3c: a write to
        # a purchased item IS a commit there), regenerating `main` from the
        # registry rewrote that history on every `split` run — content-equal,
        # so `--catalog --rebuild --check` stayed IDENTICAL and nobody saw it,
        # but every catalog commit a parent had pinned became unreachable.
        # Found by stage 9, whose incremental sync would have run this after
        # every legacy write. A live catalog is ADOPTED: its own commits, in
        # order, are what parents pin.
        commits = []
        log = (_git(repo, "log", "--first-parent", "--reverse",
                    "--format=%ct %H", "refs/heads/main")
               if "refs/heads/main" in existing_refs(repo) else "")
        for line in log.splitlines():
            t, _, sha = line.partition(" ")
            if sha:
                commits.append((int(t), sha))
        return {"repo": repo, "commits": commits, "items": n_items,
                "skipped": "live"}
    rows = []
    for item_id, it in reg["items"].items():
        if it["kind"] not in manifest.CATALOG_KINDS:
            continue
        for rev in reg["revs"].get(item_id, []):
            m = manifest.build(conn, rev["id"])
            va = vault.author(rev["git_commit"])
            rows.append({"when": _epoch(rev, va), "number": it["number"],
                         "version": rev["version"], "m": m, "va": va,
                         "state": rev["state"]})
    rows.sort(key=lambda r: (r["when"], r["number"], r["version"]))

    entries: dict[str, tuple[str, str, str]] = {}
    parent: list[str] = []
    commits: list[tuple[int, str]] = []
    readme_sha = hash_blob(repo, (
        "# catalog\n\n"
        "Purchased and hardware items — the things you buy, one TOML each.\n\n"
        "Not repo-per-screw (docs/PLM_GIT_FIRST.md §5.4): an assembly pulls "
        "this\nin as ONE submodule at `catalog/`, and its `[[bom]]` lines point "
        "at\n`catalog/<NUMBER>.toml`. `[[source]]` blocks are the SUPPLIERS "
        "(IPN != MPN);\nthe rare generating source file is `[generator]`.\n\n"
        "<!-- generated by plm/split.py from the plm registry -->\n").encode())
    entries["README.md"] = (MODE_BLOB, readme_sha, "README.md")

    for r in rows:
        text = manifest.dumps(manifest.document(r["m"], catalog=True))
        sha = hash_blob(repo, text.encode())
        path = f"{r['number']}.toml"
        entries[path] = (MODE_BLOB, sha, path)
        tree = write_tree(repo, list(entries.values()))
        name, email = ((r["va"][0], r["va"][1]) if r["va"]
                       else (r["m"]["author"] or BOT_NAME, BOT_EMAIL))
        msg = (f"{r['number']} v{r['version']}: "
               f"{r['m']['summary'] or r['m']['name']}\n\n"
               f"Plm-Item: {r['number']}\nPlm-Version: {r['version']}\n"
               f"Plm-State: {r['state']}\n")
        sha_c = commit_tree(repo, tree, parent, msg,
                            _ident(name, email, r["when"]))
        parent = [sha_c]
        commits.append((r["when"], sha_c))
    if parent:
        update_ref(repo, "refs/heads/main", parent[0])
        _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    return {"repo": repo, "commits": commits, "items": len(
        [i for i in reg["items"].values() if i["kind"] in manifest.CATALOG_KINDS])}


def catalog_pin(catalog: dict, when: int) -> str:
    """The catalog commit a parent revision pins: the newest one dated at or
    before that revision.

    THE KNOWN LIMIT of one catalog submodule (§5.4's trade): a parent with two
    purchased children at two different versions gets ONE gitlink, so the pin is
    "the catalog as it stood when this revision was made" rather than a per-line
    pin. `[[bom]]` still names the exact file; the day a line needs its own pin
    is the day that item deserves a repo.
    """
    best = ""
    for w, sha in catalog["commits"]:
        if w <= when:
            best = sha
        else:
            break
    return best or (catalog["commits"][0][1] if catalog["commits"] else "")


# -- §6c-1: the version leaves the filename too -------------------------------
#
# `split` replays history with the filenames the folder era gave it, so
# `GSI-PRT-0004_v0.2.0_floor_plate.stl` becomes `…_v0.3.0_floor_plate.stl` at
# the next revision and git — with rename detection deliberately OFF — reports a
# delete and an add. There is then no `git log --follow`, no blame and no
# file-level diff without a heuristic. In a git-first repo the TAG carries the
# version and the REPO carries the number, so the file is just `floor_plate.stl`
# and every git tool works on it unaided.
#
# The rule is `gitrepo.stable_name`, which is the same `_stem_ext` the stage-5
# pairing normaliser is built on. ONE rule: a second regex here would be the
# "same rule, two homes" fault that every Critical in the security review turned
# out to be, and it would drift the first time somebody taught one of them about
# a new filename shape.


def _stamp_rank(name: str) -> tuple:
    """Sort key deciding WHICH file keeps the bare name when two collide.

    Unstamped first (a name with no version token in it was never generated —
    it IS the canonical name), then newest version token first, so the plain
    `sensor_node_assembled.stl` follows the CURRENT geometry and the stale copy
    carried forward from two revisions ago is the one that gets `~2`.
    """
    stem = Path(name).stem
    m = gitrepo._VERSION_TOKEN.search(stem)
    if not m:
        return (0, ())
    parts = re.split(r"[._]", m.group(0).lstrip("vV"))
    return (1, tuple(-int(x) for x in parts if x.isdigit()))


def stable_map(number: str, rows: list[dict], version: str,
               collisions: list[str]) -> dict[str, str]:
    """{tree path -> stable tree path} for ONE revision.

    Collisions are kept, never resolved by dropping bytes: both files stay, the
    loser gets `~2`, and the pair is appended to `collisions` so it reaches the
    report. Two are real in this registry (GSI-ASM-0002 and GSI-ASM-0003 each
    carry a stale copy of the same mesh in one tree) and stage 5 found the same
    two from the other direction.
    """
    paths = []
    for f in rows:
        if manifest.is_external(f):
            continue
        tp = manifest.tree_path(f["path"])
        if Path(tp).name in manifest.DROP_FROM_TREE:
            continue
        paths.append(tp)
    out: dict[str, str] = {}
    taken: dict[str, str] = {}
    for tp in sorted(dict.fromkeys(paths),
                     key=lambda p: (_stamp_rank(Path(p).name), p)):
        base = Path(tp).name
        parent = str(Path(tp).parent)
        d = "" if parent == "." else parent + "/"
        seq = 1
        while True:
            cand = d + gitrepo.stable_name(base, number, seq=seq)
            if cand not in taken:
                break
            if seq > 1 and cand == d + base:
                cand = tp          # nothing left to disambiguate with
                break
            seq += 1
        if seq > 1:
            collisions.append(
                f"{number} v{version}: {taken[d + gitrepo.stable_name(base, number)]}"
                f" and {tp} both normalise to "
                f"{d + gitrepo.stable_name(base, number)} — kept both, the "
                f"second as {cand}")
        taken[cand] = tp
        out[tp] = cand
    return out


def _remap(m: dict, ren: dict[str, str], ext_ren: dict[str, str]) -> None:
    """Point the manifest at the new names, IN PLACE.

    `[files]` roles, `primary`, `[[files.external]] name` and `[source] file`.
    Nothing else moves: a name is not a design fact, so the original is recorded
    once in the commit trailer (`Plm-Renamed:`) and nowhere in `plm.toml`. The
    storage KEY is deliberately untouched — the bytes did not move, only what
    the manifest calls them.
    """
    m["files"] = {role: sorted(dict.fromkeys(ren.get(p, p) for p in paths))
                  for role, paths in m["files"].items()}
    for e in m["files_external"]:
        e["name"] = ext_ren.get(e["name"], e["name"])
    m["files_external"] = sorted(m["files_external"],
                                 key=lambda e: (e["role"], e["name"], e["key"]))
    prim = m.get("primary") or ""
    if prim:
        m["primary"] = ren.get(prim, ext_ren.get(prim, prim))
    # `[source] file` is a bare generating-script name (`lantern_top_drive.scad`)
    # and `render-matches-source` looks it up in the tree, so a renamed source
    # file has to be followed or the check goes from green to "no such file".
    src = m["source"].get("file") or ""
    if src:
        by_base = {}
        for old, new in ren.items():
            by_base.setdefault(Path(old).name, set()).add(new)
        hit = by_base.get(Path(src).name) or set()
        if len(hit) == 1:
            m["source"]["file"] = next(iter(hit))


# -- one item -----------------------------------------------------------------

def adopt_existing(repo: Path, number: str) -> dict:
    """What a LIVE repo already holds, in `build_item`'s result shape.

    A repo that has been pushed to is not regenerated (see `build_item`), but a
    parent still has to pin its children — so the refs that are there are read
    back instead of rebuilt. Without this, skipping one child repo would turn
    every assembly above it into `submodule_child_not_built`.
    """
    result = {"number": number, "repo": repo, "commits": [], "tags": [],
              "drafts": [], "by_version": {}, "main": "", "skipped": True,
              "submodules": []}
    for line in _git(repo, "for-each-ref", "refs/tags/", "refs/heads/",
                     "--format=%(refname) %(objectname) %(*objectname) "
                     "%(creatordate:unix)").split("\n"):
        bits = line.split()
        if len(bits) < 3:
            continue
        ref, obj = bits[0], bits[1]
        sha = bits[2] if len(bits) > 3 and len(bits[2]) == 40 else obj
        when = int(bits[-1]) if bits[-1].isdigit() else 0
        if ref.startswith("refs/tags/v"):
            version = ref[len("refs/tags/v"):]
            result["tags"].append(f"v{version}")
            result["by_version"][version] = {
                "commit": sha, "state": "RELEASED", "when": when,
                "draft": False, "summary": "", "maturity": "released"}
        elif ref.startswith("refs/heads/draft/"):
            # A draft `split` wrote is named for its version; a draft somebody
            # PUSHED is named for its topic (`draft/taller-pillars`), and the
            # version it claims is in its own plm.toml. Reading it there is what
            # lets a parent pin that work at all — a name-only rule left every
            # pushed draft invisible to `split`, so a parent that wanted it fell
            # back to a dated pin and quietly changed its own BOM.
            version = version_at(repo, sha) or ref[len("refs/heads/draft/"):]
            result["drafts"].append((ref[len("refs/heads/"):], sha))
            result["by_version"].setdefault(version, {
                "commit": sha, "state": "DRAFT", "when": when, "draft": True,
                "summary": "", "maturity": ""})
        elif ref == "refs/heads/main":
            result["main"] = sha
    return result


def version_at(repo: Path, rev: str) -> str:
    """The `version` the manifest at a commit claims, or ''."""
    raw = _git(repo, "cat-file", "blob", f"{rev}:plm.toml", check=False)
    if not raw:
        return ""
    try:
        import tomllib
        return str(tomllib.loads(raw).get("version") or "")
    except Exception:  # noqa: BLE001 — an unreadable manifest is not a version
        return ""


def build_item(conn, reg: dict, item_id: int, vault: Vault, catalog: dict,
               built: dict, stats: dict, *, force: bool = False,
               normalize: bool = False, prune: bool = False,
               lfs_on: bool = False) -> dict:
    item = reg["items"][item_id]
    number = item["number"]
    repo = init_bare(REPOS / f"{number}.git")
    revs = reg["revs"].get(item_id, [])
    result = {"number": number, "repo": repo, "commits": [], "tags": [],
              "drafts": [], "by_version": {}, "main": ""}

    # ⛔ THE REPOS STOPPED BEING DISPOSABLE.
    # Stage 1 could delete /srv/.../repos and rebuild it byte-identically,
    # because every commit was a projection of the registry. The moment a repo
    # holds a commit somebody PUSHED — or one plm wrote through
    # plm/gitwrite.py — regenerating it would destroy the only copy of that
    # work, and `split` would be the tool that did it.
    if (repo / gitwrite.MARKER).is_file() and not force:
        stats["repos_skipped_because_they_hold_pushed_work"].append(
            f"{number} ({len(gitwrite.written_log(repo))} write(s); "
            f"--force to regenerate and LOSE them)")
        adopted = adopt_existing(repo, number)
        # …but `refs/plm/item` is plm's own ref, not the pushed work this guard
        # protects, so a live repo still gets one if it has none (§3).
        from . import itemref
        if not itemref.head(repo):
            revs_ = reg["revs"].get(item_id, [])
            adopted["item_ref"] = write_item_ref(
                conn, repo, item_id,
                _epoch(revs_[-1], None) if revs_ else int(time.time()))
        return adopted

    if not revs:
        stats["items_with_no_revisions"].append(number)
        return result

    blob_cache: dict[tuple, str] = {}
    main_tip = ""    # the newest RELEASED commit — this is what `main` becomes
    prev_tip = ""    # the previous revision in version order, released or not
    subs_seen: list[tuple[str, str]] = []
    prev_origin: dict[str, str] = {}   # stable path -> the name it came in as

    rel_pin = _pin_resolver(reg, built, stats)
    notes: dict[str, dict] = {}       # commit -> the note split writes on it
    for rev in revs:
        m = manifest.build(conn, rev["id"], pin=rel_pin)
        va = vault.author(rev["git_commit"])
        when = _epoch(rev, va)
        entries: list[tuple[str, str, str]] = []
        subs: list[tuple[str, str]] = []

        # §6c-1. `ren` is identity when normalisation is off, so there is one
        # code path below either way.
        ren: dict[str, str] = {}
        ext_ren: dict[str, str] = {}
        if normalize:
            ren = stable_map(number, reg["files"][rev["id"]], rev["version"],
                             stats["filename_collisions"])
            seen_ext: dict[str, str] = {}
            for e in m["files_external"]:
                nm, seq = e["name"], 1
                while True:
                    cand = gitrepo.stable_name(nm, number, seq=seq)
                    if cand not in seen_ext:
                        break
                    if seq > 1 and cand == nm:
                        cand = nm
                        break
                    seq += 1
                if seq > 1:
                    stats["filename_collisions"].append(
                        f"{number} v{rev['version']}: external {seen_ext[cand]}"
                        f" and {nm} normalise alike — kept both, the second as "
                        f"{cand}")
                seen_ext[cand] = nm
                ext_ren[nm] = cand
            _remap(m, ren, ext_ren)

        # -- the revision's own bytes, flattened out of versions/vX.Y.Z/ ------
        version_md = b""
        renamed_here: list[tuple[str, str]] = []
        for f in reg["files"][rev["id"]]:
            if manifest.is_external(f):
                continue          # object store: [[files.external]], not the tree
            tp = manifest.tree_path(f["path"])
            name = Path(tp).name
            if (f.get("backend") or "") == "lfs":
                # In the tree already, bytes in plm's LFS store by `key` (the
                # oid) — never in the vault, whose path this is not.
                out_path = ren.get(tp, tp)
                oid = str(f.get("key") or "").lower()
                ck = ("lfs", oid, out_path)
                if ck not in blob_cache:
                    real = lfs.size_of(oid) if lfs.OID_RE.match(oid) else None
                    if real is None:
                        stats["files_not_recoverable"].append(
                            f"{number} v{rev['version']} {f['path']} (lfs "
                            f"{oid[:12] or 'no oid'}: object not in plm's "
                            "LFS store)")
                        continue
                    sha, ptr = lfs_blob(repo, oid, real, out_path,
                                        pointer_ok=lfs_on)
                    stats["lfs_tree_files"] += int(ptr)
                    stats["lfs_tree_bytes"] += real if ptr else 0
                    blob_cache[ck] = (sha, MODE_BLOB)
                entries.append((MODE_BLOB, blob_cache[ck][0], out_path))
                prev_origin[out_path] = tp
                continue
            on_disk = (vault.root / f["path"]).is_file()
            key = (f["path"], "disk" if on_disk else rev["git_commit"])
            if name == "VERSION.md":
                data, p = vault.read(f["path"], rev["git_commit"])
                version_md = data if data is not None else (
                    p.read_bytes() if p else b"")
                continue
            if name in manifest.DROP_FROM_TREE:
                continue
            out_path = ren.get(tp, tp)
            if key in blob_cache:
                sha, mode = blob_cache[key]
            else:
                data, p = vault.read(f["path"], rev["git_commit"])
                size = (p.stat().st_size if p is not None
                        else len(data) if data is not None else 0)
                if (p is not None or data is not None) and lfs_on \
                        and lfs.wants_lfs(out_path, size):
                    # stage 17: the bytes go to plm's LFS store, the tree gets
                    # the POINTER — byte-identical to what `git lfs` writes
                    oid, n = lfs.put_file(p) if p is not None \
                        else lfs.put_bytes(data)
                    sha, mode = hash_blob(repo, lfs.pointer(oid, n)), MODE_BLOB
                    stats["lfs_tree_files"] += 1
                    stats["lfs_tree_bytes"] += n
                elif p is not None:
                    sha = hash_file(repo, p)
                    mode = MODE_EXEC if os.access(p, os.X_OK) else MODE_BLOB
                elif data is not None:
                    sha = hash_blob(repo, data)
                    mode = MODE_BLOB
                else:
                    stats["files_not_recoverable"].append(
                        f"{number} v{rev['version']} {f['path']}")
                    continue
                blob_cache[key] = (sha, mode)
            if out_path != tp:
                stats["filenames_normalised"] += 1
                # ONCE, not once per revision: the trailer records the name the
                # bytes ARRIVED under, so it is written the first time this
                # stable path sees it and again only when it changes (which is
                # exactly what a version-stamped name does every revision).
                if prev_origin.get(out_path) != tp:
                    renamed_here.append((tp, out_path))
            prev_origin[out_path] = tp
            entries.append((mode, sha, out_path))

        # -- BOM children as submodules (§5.1: the gitlink IS the pin) --------
        need_catalog = False
        for b in reg["boms"].get(rev["id"], []):
            child = reg["items"].get(b["child_item_id"])
            if not child:
                stats["bom_lines_with_missing_child"].append(
                    f"{number} v{rev['version']} -> item {b['child_item_id']}")
                continue
            if child["kind"] in manifest.CATALOG_KINDS:
                need_catalog = True
                continue
            cb = built.get(child["id"])
            if not cb or not cb["by_version"]:
                stats["submodule_child_not_built"].append(
                    f"{number} v{rev['version']} -> {child['number']}")
                continue
            pin, how = pin_child(cb, b["child_version"], when)
            if not pin:
                stats["submodule_child_unpinnable"].append(
                    f"{number} v{rev['version']} -> {child['number']}")
                continue
            stats["pin_" + how] += 1
            path = f"parts/{child['number']}"
            entries.append((MODE_GITLINK, pin, path))
            subs.append((path, f"../{child['number']}.git"))
        if need_catalog and catalog["commits"]:
            entries.append((MODE_GITLINK, catalog_pin(catalog, when), "catalog"))
            subs.append(("catalog", f"../{CATALOG_REPO}.git"))

        # -- the generated files ---------------------------------------------
        entries.append((MODE_BLOB, hash_blob(
            repo, manifest.dumps(manifest.document(m)).encode()), "plm.toml"))
        # README.md is GENERATED only when the revision has none of its own.
        # A revision that tracks a README.md (21 prod rows: firmware, docs)
        # lost it here: both entries went to `update-index`, the generated one
        # sorted last and won, and the revision's own bytes were in no tree —
        # invisible to --rebuild --check, which compares file NAMES per role.
        # Found by the stage-12 vault census (rows whose bytes are not at
        # their tag), not by any check.
        if not any(path == "README.md" for _, _, path in entries):
            entries.append((MODE_BLOB, hash_blob(
                repo, readme(reg, item, m, vault)), "README.md"))
        if subs:
            subs_seen = subs
            entries.append((MODE_BLOB, hash_blob(repo, gitmodules(subs)),
                            ".gitmodules"))
        if lfs_on:
            # every commit of an LFS-layout repo carries the template, so a
            # checkout of ANY ref smudges its meshes; non-mesh LFS files are
            # named one per line (a pattern would catch the small ones too)
            if any(path == ".gitattributes" for _, _, path in entries):
                stats["lfs_own_gitattributes_kept"].append(
                    f"{number} v{rev['version']}")
            else:
                ptr_paths = [path for mode, sha_, path in entries
                             if mode == MODE_BLOB and not lfs.is_mesh(path)
                             and not lfs.always_plain(path)
                             and _is_pointer(repo, sha_)]
                entries.append((MODE_BLOB, hash_blob(
                    repo, lfs.gitattributes(ptr_paths)), ".gitattributes"))

        tree = write_tree(repo, entries)
        ref_name = rev_ref(rev["version"], rev["state"])
        draft = ref_name.startswith("draft/")
        # A DRAFT sits on top of the revision it was revised FROM (the previous
        # revision in version order, which may itself be a draft); a RELEASED
        # revision continues `main`, which never carries a draft — that is the
        # POC punt this fixes by construction (§4, "main = released only").
        parents = [prev_tip if draft else main_tip] if (
            prev_tip if draft else main_tip) else []
        name, email = ((va[0], va[1]) if va
                       else (rev["author"] or BOT_NAME, BOT_EMAIL))
        subject = (rev["summary"] or f"{number} v{rev['version']}").strip()
        msg = (subject.split("\n")[0][:200] + "\n\n"
               + (subject[200:].strip() + "\n\n" if len(subject) > 200 else "")
               + f"Plm-Item: {number}\n"
                 f"Plm-Version: {rev['version']}\n"
                 f"Plm-State: {rev['state']}\n"
                 f"Plm-Vault-Commit: {rev['git_commit'] or '-'}\n"
               # §6c-1: the original name is recorded ONCE, here, and not in
               # `plm.toml` — what a file was called is history, and history is
               # what a commit message is for. `git log --grep=Plm-Renamed` is
               # the whole audit trail of the migration's renames.
               + "".join(f"Plm-Renamed: {old} -> {new}\n"
                         for old, new in sorted(renamed_here)))
        sha_c = commit_tree(repo, tree, parents, msg, _ident(name, email, when))
        prev_tip = sha_c
        # Stage 10: a NOTE-home fact (installed_on) about this revision is a
        # note on its commit, never a line in its tree — the tag below stays
        # exactly the tag it would be without it. Written in `run`'s last
        # pass, when every target it may pin has been built.
        notes[sha_c] = {"rev": rev, "when": when}
        result["commits"].append(sha_c)
        result["by_version"][rev["version"]] = {
            "commit": sha_c, "state": rev["state"], "when": when,
            "draft": draft, "summary": subject.split("\n")[0],
            "maturity": m["maturity"]}

        if draft:
            result["drafts"].append((ref_name, sha_c))
        else:
            tagname = ref_name
            approver = ", ".join(
                a["approver"] for a in reg["approvals"].get(rev["id"], [])
                if a["approver"])
            ann = [f"{number} v{rev['version']} — {rev['state']}", ""]
            if m["maturity"]:
                ann.append(f"Maturity: {m['maturity']}")
            ann.append(f"State: {rev['state']}")
            if approver:
                ann.append(f"Approved-By: {approver}")
            if rev["ecn"]:
                ann.append(f"ECN: {rev['ecn']}")
            if rev["verification"]:
                ann.append(f"Verification: {rev['verification']}")
            ann.append("")
            if subject:
                ann += [subject, ""]
            if version_md:
                ann += ["--- VERSION.md ---", "",
                        version_md.decode(errors="replace").rstrip(), ""]
            annotated_tag(repo, tagname, sha_c, "\n".join(ann),
                          _ident(BOT_NAME, BOT_EMAIL, when))
            result["tags"].append(tagname)
            main_tip = sha_c

    for ref, sha in result["drafts"]:
        update_ref(repo, f"refs/heads/{ref}", sha)
    if main_tip:
        update_ref(repo, "refs/heads/main", main_tip)
        _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        result["main"] = main_tip
    elif result["drafts"]:
        # Nothing releasable has ever existed for this item. Rather than invent
        # a `main` that no tag stands behind, HEAD points at the newest draft so
        # a clone still checks something out and says what it is.
        _git(repo, "symbolic-ref", "HEAD", f"refs/heads/{result['drafts'][-1][0]}")
        stats["items_with_no_released_revision"].append(number)
    result["_notes"] = notes
    if prune:
        result["pruned"] = prune_refs(repo, result)
    _git(repo, "config", "plm.number", number)
    _git(repo, "config", "plm.kind", item["kind"])
    (repo / "description").write_text(f"{number} — {item['name']}\n")
    # §3: the item manifest, generated for the historical items exactly as it is
    # written for a new one. Without it a rebuild would read the item's name out
    # of whichever tag it happened to be re-deriving, which for a renamed part is
    # the name it had in 2025.
    result["item_ref"] = write_item_ref(conn, repo, item_id, when)
    result["submodules"] = subs_seen
    return result


def _pin_resolver(reg: dict, built: dict, stats: dict):
    """`pin(number, version) -> commit` for a claim's target (stage 10).

    From this run's build when the target was built in it (it was built first:
    `claim_targets` orders it), else read off the target's repo on disk — the
    same refs `adopt_existing` reads for a live child. Cached per number."""
    by_number = {it["number"]: i for i, it in reg["items"].items()}
    cache: dict[str, dict] = {}

    def pin(number: str, version: str) -> str:
        b = built.get(by_number.get(number, -1))
        if not (b and b.get("by_version")):
            if number not in cache:
                repo = REPOS / f"{number}.git"
                cache[number] = (adopt_existing(repo, number)
                                 if (repo / "HEAD").exists() else {})
            b = cache[number]
        v = (b or {}).get("by_version", {}).get(version)
        if v:
            stats["relation_pins"] += 1
            return v["commit"]
        stats["relation_pins_unresolved"].append(f"{number} v{version}")
        return ""
    return pin


def _note_relations(conn, reg: dict, item_id: int, rev: dict, pin) -> dict:
    """The note-home claims whose claimant is this revision, as a note doc."""
    from . import gitnotes, relations as relmod
    doc: dict = {}
    for r in reg["relations"].get(item_id, []):
        sp = relmod.spec(r["relation"])
        if sp["home"] != "note" or not sp["note_key"]:
            continue
        if relmod.claimant(conn, r)["revision_id"] != rev["id"]:
            continue
        t = reg["items"].get(r["target_item_id"])
        if not t:
            continue
        e = {"item": t["number"]}
        if r["target_version"]:
            e["version"] = r["target_version"]
            c = r["target_commit"] or pin(t["number"], r["target_version"])
            if c:
                e["commit"] = c
        if r["note"]:
            e["note"] = r["note"]
        doc.setdefault(sp["note_key"], []).append(e)
    if doc:
        doc = {**gitnotes.empty(), **doc}
    return doc


def write_notes(repo: Path, notes: dict[str, dict]) -> str:
    """`refs/notes/plm` for a GENERATED repo — deterministic, like every ref
    split writes: one commit, dated by the newest annotated revision, holding
    one note per annotated commit. In a generated repo split owns the ref, so
    no note-home fact means no ref (it is deleted if a previous build left one).
    A LIVE repo never gets here — its notes are plm's record of later facts."""
    from . import gitnotes
    if not notes:
        if _git(repo, "rev-parse", "--verify", "--quiet", gitnotes.REF,
                check=False).strip():
            _git(repo, "update-ref", "-d", gitnotes.REF)
        return ""
    entries = [(MODE_BLOB, hash_blob(repo, gitnotes.document(n["doc"]).encode()),
                gitnotes.blob_path(c)) for c, n in sorted(notes.items())]
    tree = write_tree(repo, entries)
    when = max(n["when"] for n in notes.values())
    sha = commit_tree(
        repo, tree, [],
        "Post-release facts, as notes\n\n"
        "Facts that arrived after a revision was frozen (installed_on, ...) "
        "are notes on\nits commit, so the tag is unchanged "
        "(docs/PLM_GIT_FIRST.md §2). Generated by\nplm/split.py from the "
        "registry.\n",
        _ident(BOT_NAME, BOT_EMAIL, when))
    update_ref(repo, gitnotes.REF, sha)
    return sha


def prune_refs(repo: Path, result: dict) -> list[str]:
    """Delete the generated refs this build did NOT produce — the half of an
    INCREMENTAL rebuild a full one never needed (stage 9, plm/gitsync.py).

    `split` only ever adds and moves refs: on a fresh object store that is the
    whole truth, but re-run over an existing repo after a legacy write it leaves
    the past behind — `draft/v0.3.0` after v0.3.0 was approved, a `main` after
    the only release was obsoleted. A repo that keeps a ref the registry no
    longer has is not deterministic (a fresh build would not have it), so the
    sync prunes, in the three namespaces `split` owns and nowhere else:
    `refs/tags/v*`, `refs/heads/draft/*`, `refs/heads/main`. Only reached for a
    repo `split` generated — a live repo returned long before this.
    """
    want = {f"refs/tags/{t}" for t in result["tags"]}
    want |= {f"refs/heads/{ref}" for ref, _ in result["drafts"]}
    if result.get("main"):
        want.add("refs/heads/main")
    gone = []
    for ref in existing_refs(repo):
        owned = (ref.startswith("refs/tags/v") or ref == "refs/heads/main"
                 or ref.startswith("refs/heads/draft/"))
        if owned and ref not in want:
            _git(repo, "update-ref", "-d", ref)
            gone.append(ref)
    return gone


def _pin_refs(repo: Path) -> dict[str, str]:
    """The refs a pin can depend on — tags and branches — of a repo, or {}."""
    if not (repo / "HEAD").exists():
        return {}
    return {r: s for r, s in existing_refs(repo).items()
            if r.startswith(("refs/tags/", "refs/heads/"))}


def write_item_ref(conn, repo: Path, item_id: int, when: int) -> str:
    """`refs/plm/item` for a generated repo, dated like the rest of the replay.

    Deterministic on purpose: `split --all` twice has to produce identical SHAs,
    so the commit's identity and date come from the item's newest revision, never
    from the wall clock.
    """
    from . import itemref
    doc = itemref.build(conn, item_id)
    text = itemref.document(doc)
    blob = hash_blob(repo, text.encode())
    tree = write_tree(repo, [(MODE_BLOB, blob, itemref.PATH)])
    sha = commit_tree(
        repo, tree, [],
        f"{doc['number']} — item manifest\n\n"
        "Item-level facts (name, kind, project, product, item-level tags and\n"
        "relations) live on this ref, not in any one revision's tree\n"
        "(docs/PLM_GIT_FIRST.md §3). Generated by plm/split.py from the "
        "registry.\n",
        _ident(BOT_NAME, BOT_EMAIL, when))
    update_ref(repo, itemref.REF, sha)
    return sha


def pin_child(child: dict, child_version: str, when: int) -> tuple[str, str]:
    """Which commit of the child this line points at, and why.

    precise  — the line names a version and the child has it.
    dated    — imprecise line (§5.1 `track = "release"`): the newest child
               RELEASE dated at or before the parent revision, which is what
               "whatever was current when I built this" actually meant.
    newest   — imprecise and the child had no release that old.
    unreleased — the named/needed revision exists only as a draft. Git is happy
               to pin it; the RELEASE rule (§5.1) is what refuses to ship above
               your least-mature child, and that is a check, not a pin.
    """
    vs = child["by_version"]
    if child_version and child_version in vs:
        v = vs[child_version]
        return v["commit"], ("unreleased" if v["draft"] else "precise")
    released = sorted(((v["when"], k, v) for k, v in vs.items() if not v["draft"]))
    if released:
        older = [r for r in released if r[0] <= when]
        if older:
            return older[-1][2]["commit"], "dated"
        return released[-1][2]["commit"], "newest"
    allv = sorted(((v["when"], k, v) for k, v in vs.items()))
    if allv:
        return allv[-1][2]["commit"], "unreleased"
    return "", "none"


# -- the repos root's LAYOUT, recorded once (stage 9b, open question 4) --------
#
# `--normalize-names` decides every tracked filename in every generated repo, so
# it is a property of the REPOS ROOT, not of a run: a root built normalised and
# then re-split without the flag (`split --all` from a demo script, an operator
# who forgot it) has its stable filenames undone, and a `git pull` on a synced
# clone sees every mesh renamed back. So the first build writes the choice down
# — `<repos>/.plm-split.json` — and every later run, the read-mode sync
# included, follows it. A run that SAYS the opposite is refused; changing a
# root's layout is a deliberate act (`--force-layout`), not a forgotten flag.
# A dot-file is invisible to every `*.git` glob (the git server's, ours).

LAYOUT_FILE = ".plm-split.json"


def layout(repos: Path | None = None) -> dict:
    """The recorded layout of a repos root, or {} when none was recorded."""
    import json
    f = (repos or REPOS) / LAYOUT_FILE
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {}


def _has_repos(root: Path) -> bool:
    return any(root.glob("*.git"))


def _record_layout(root: Path, normalize: bool, *, why: str,
                   **extra) -> dict:
    import json
    # keys this call does not set (the LFS layout, stage 17) are kept
    doc = {**layout(root), "schema": 1, "normalize_names": bool(normalize),
           "recorded": why,
           "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           **extra}
    tmp = root / (LAYOUT_FILE + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(root / LAYOUT_FILE)
    return doc


def resolve_layout(root: Path, normalize: bool | None, *,
                   force_layout: bool = False) -> tuple[bool, dict]:
    """`(normalize, note)` for a run over `root` — or a SplitError that teaches.

    `normalize=None` means "the caller did not say": the marker governs. An
    explicit value must agree with the marker unless `force_layout`.
    """
    rec = layout(root)
    flag = {True: "--normalize-names", False: "--no-normalize-names"}
    if rec:
        have = bool(rec.get("normalize_names"))
        if normalize is None or normalize == have:
            return have, {}
        if not force_layout:
            raise SplitError(
                f"refusing: the repos under {root} were built "
                f"{'WITH' if have else 'WITHOUT'} normalised filenames "
                f"({LAYOUT_FILE}: normalize_names = {str(have).lower()}), and "
                f"this run says {flag[normalize]}.\n"
                "  Re-splitting in the other layout renames every tracked file "
                "in every generated repo — a\n  synced clone would see each "
                "mesh renamed back on its next pull.\n"
                f"  - drop the flag (the recorded layout is used), or say "
                f"{flag[have]};\n"
                "  - or change the root's layout on purpose: --force-layout "
                "(rewrites the marker; use --prune --all).")
        return normalize, _record_layout(root, normalize,
                                         why=f"--force-layout {flag[normalize]}")
    if normalize is None:
        if _has_repos(root):
            raise SplitError(
                f"refusing: {root} holds repos but no {LAYOUT_FILE}, so "
                "nothing says whether their filenames\n  were normalised, and "
                "guessing is how a root ends up half one way and half the "
                "other.\n  Say it once — --normalize-names (the migration's "
                "layout) or --no-normalize-names — and\n  it is recorded for "
                "every later run, the read-mode sync included.")
        normalize = False      # a NEW root, no flag: today's CLI default
    return normalize, _record_layout(
        root, normalize, why=f"first build {flag[normalize]}"
        + (" (root already held repos)" if _has_repos(root) else ""))


def resolve_lfs(root: Path, want: bool | None, normalize: bool, *,
                force_layout: bool = False) -> tuple[bool, dict]:
    """`(lfs_on, note)` — stage 17's half of the layout marker.

    Same rule as filenames: the first build of a root records whether meshes
    are LFS pointers (`lfs`) and at which threshold (`lfs_threshold` — it
    decides which non-mesh files are pointers, so it is part of every SHA);
    later runs follow it, a contradicting run is refused. A root recorded
    before stage 17 has no `lfs` key and is NOT LFS — which is what keeps the
    live POC's repos, and `gitsync`'s re-splits of them, exactly as they were.
    """
    rec = layout(root)
    have = bool(rec.get("lfs"))
    thr = lfs.threshold()
    flag = {True: "--lfs", False: "--no-lfs"}
    if want is None or (want == have and (not have
                                           or rec.get("lfs_threshold") == thr)):
        if have and rec.get("lfs_threshold") != thr:
            raise SplitError(
                f"refusing: the repos under {root} were built with LFS at "
                f"threshold {rec.get('lfs_threshold'):,} bytes ({LAYOUT_FILE}), "
                f"and PLM_LFS_THRESHOLD_BYTES is {thr:,} here.\n  The "
                "threshold decides which files are pointers, so it is part of "
                "every SHA. Set it back,\n  or re-split on purpose: "
                "--lfs --force-layout --prune --all.")
        return have, {}
    if not force_layout and ("lfs" in rec or _has_repos(root)):
        raise SplitError(
            f"refusing: the repos under {root} were built "
            f"{'WITH' if have else 'WITHOUT'} Git LFS pointers ({LAYOUT_FILE}: "
            f"lfs = {str(have).lower()}), and this run says {flag[want]}.\n"
            "  Switching rewrites every mesh in every generated repo (new "
            "SHAs; the guard pass keeps\n  what live parents pin). Do it on "
            "purpose: --force-layout --prune --all.")
    note = _record_layout(root, normalize,
                          why=f"{'--force-layout ' if 'lfs' in rec else ''}{flag[want]}"
                          + (f" (threshold {thr})" if want else ""),
                          lfs=bool(want), lfs_threshold=thr if want else None)
    return bool(want), note


# -- the whole registry -------------------------------------------------------

def run(conn, *, roots: list[int] | None = None, repos: Path | None = None,
        force: bool = False, normalize: bool | None = None,
        prune: bool = False, force_layout: bool = False,
        lfs_layout: bool | None = None, vault: Vault | None = None,
        catalog: dict | None = None) -> dict:
    """Build repos. `normalize=None` / `lfs_layout=None` follow the root's
    recorded layout (`resolve_layout`, `resolve_lfs`); an explicit value must
    agree with it.

    `vault` and `catalog` are for `plm/carry.py` (stage 20), which builds a
    project carried from ANOTHER instance: its bytes come from the carry
    bundle, not from this instance's vault, and on a target whose
    `catalog.git` is live the carried parents pin the catalog commit that
    first holds every entry they name (`{"commits": [(0, sha)]}`) — the
    dated pin of a fresh build would land on a commit older than the entries
    the carry just wrote."""
    global REPOS
    if repos is not None:
        REPOS = repos
    REPOS.mkdir(parents=True, exist_ok=True)
    normalize, layout_note = resolve_layout(REPOS, normalize,
                                            force_layout=force_layout)
    lfs_on, lfs_note = resolve_lfs(REPOS, lfs_layout, normalize,
                                   force_layout=force_layout)
    layout_note = lfs_note or layout_note
    vault = vault or Vault(config.DATA_ROOT)
    reg = load(conn)
    order, cycles = build_order(reg, roots)
    stats = defaultdict(list)
    for k in ("pin_precise", "pin_dated", "pin_newest", "pin_unreleased",
              "pin_none", "filenames_normalised", "relation_pins",
              "lfs_tree_files", "lfs_tree_bytes"):
        stats[k] = 0
    t0 = time.time()
    # Stage 11b: what every repo this run may touch pointed at BEFORE it, so
    # that a regenerated commit a LIVE parent (or a claim, or an evidence file)
    # still pins is kept rather than orphaned — see the guard pass below.
    touched = {CATALOG_REPO: REPOS / f"{CATALOG_REPO}.git"}
    touched.update({reg["items"][i]["number"]:
                    REPOS / f"{reg['items'][i]['number']}.git" for i in order})
    before = {n: _pin_refs(r) for n, r in touched.items()}
    if catalog is None:
        catalog = build_catalog(conn, reg, vault, force=force)
    built: dict[int, dict] = {}
    for item_id in order:
        built[item_id] = build_item(conn, reg, item_id, vault, catalog, built,
                                    stats, force=force, normalize=normalize,
                                    prune=prune, lfs_on=lfs_on)
    # The notes pass (stage 10): note-home facts pin their targets' commits,
    # and every target this run builds exists now.
    rel_pin = _pin_resolver(reg, built, stats)
    for item_id, b in built.items():
        pending = b.pop("_notes", None)
        if pending is None:
            continue          # a live repo: its notes are plm's record, not ours
        docs = {}
        for sha_c, n in pending.items():
            nd = _note_relations(conn, reg, item_id, n["rev"], rel_pin)
            if nd:
                docs[sha_c] = {"doc": nd, "when": n["when"]}
        b["notes"] = write_notes(b["repo"], docs)
    # THE GUARD PASS (stage 11b). A re-split gives a changed revision new SHAs
    # and moves its tag/branch; the old commit loses its last ref. If anything
    # still pins it — a live parent's gitlink, a claim, a release row, an
    # evidence file — `gitkeep.guard` keeps it; otherwise it is let go. Parents
    # last (reverse build order), so a kept old parent's closure can reach back
    # into a child whose own guard already ran.
    from . import gitkeep
    guarded: dict[str, dict] = {}
    for n in reversed(list(touched)):
        olds = [sha for ref, sha in before[n].items()
                if _pin_refs(touched[n]).get(ref) != sha]
        if olds:
            g = gitkeep.guard(conn, n, olds, why="re-split by plm.split",
                              root=REPOS)
            if g:
                guarded[n] = g
    summary = {
        "normalize_names": normalize,
        "lfs": lfs_on,
        **({"lfs_threshold": lfs.threshold(),
            "lfs_files_from_the_vault": stats["lfs_tree_files"],
            "lfs_bytes_from_the_vault": stats["lfs_tree_bytes"]}
           if lfs_on else {}),
        **({"layout_recorded": f"{LAYOUT_FILE}: {layout_note['recorded']}"}
           if layout_note else {}),
        "repos": len(built) + 1,
        "item_repos": len(built),
        "catalog_repo": 1,
        "catalog_items": catalog["items"],
        "catalog_commits": len(catalog["commits"]),
        "commits": sum(len(b["commits"]) for b in built.values()) + len(
            catalog["commits"]),
        "tags": sum(len(b["tags"]) for b in built.values()),
        "draft_branches": sum(len(b["drafts"]) for b in built.values()),
        "submodule_links": stats["pin_precise"] + stats["pin_dated"]
        + stats["pin_newest"] + stats["pin_unreleased"],
        "pins_precise": stats["pin_precise"],
        "pins_dated_imprecise": stats["pin_dated"],
        "pins_newest_fallback": stats["pin_newest"],
        "pins_at_an_unreleased_commit": stats["pin_unreleased"],
        "repos_skipped_live": sum(1 for b in built.values()
                                  if b.get("skipped")),
        "filenames_normalised": stats["filenames_normalised"],
        "relation_commit_pins": stats["relation_pins"],
        "notes_written": sum(1 for b in built.values() if b.get("notes")),
        "filename_collisions": len(stats["filename_collisions"]),
        "orphaned_by_resplit": sum(g.get("orphaned", 0)
                                   for g in guarded.values()),
        "kept_because_pinned": sum(len(g.get("kept") or [])
                                   for g in guarded.values()),
        "seconds": round(time.time() - t0, 1),
    }
    skipped = stats.pop("repos_skipped_because_they_hold_pushed_work", [])
    problems = {k: v for k, v in stats.items() if isinstance(v, list) and v}
    if cycles:
        problems["bom_cycles"] = cycles
    if vault.misses:
        problems["vault_paths_not_found"] = sorted(set(vault.misses))
    return {"summary": summary, "problems": problems, "built": built,
            "catalog": catalog, "skipped": skipped, "guarded": guarded}


def verify_pins(conn, built: dict, reg: dict) -> dict:
    """Does every gitlink agree with `bom.child_version`?

    The manifest deliberately drops the version from a BOM line (§5.1), so this
    is the check that the fact was not LOST, only MOVED: for every precise line,
    the submodule commit must be exactly the child's commit for that version.
    """
    ok = bad = skipped = 0
    misses = []
    for item_id, b in built.items():
        repo = b["repo"]
        for rev in reg["revs"].get(item_id, []):
            version = rev["version"]
            if version not in b["by_version"]:
                continue
            sha = b["by_version"][version]["commit"]
            links = {}
            for line in _git(repo, "ls-tree", "-r", sha).splitlines():
                meta, _, path = line.partition("\t")
                mode, kind, obj = meta.split()
                if kind == "commit":
                    links[path] = obj
            for row in reg["boms"].get(rev["id"], []):
                child = reg["items"].get(row["child_item_id"])
                if not child or child["kind"] in manifest.CATALOG_KINDS:
                    skipped += 1
                    continue
                if not row["child_version"]:
                    skipped += 1
                    continue
                path = f"parts/{child['number']}"
                cb = built.get(child["id"])
                want = (cb or {}).get("by_version", {}).get(
                    row["child_version"], {}).get("commit")
                if not want or path not in links:
                    skipped += 1
                    continue
                if links[path] == want:
                    ok += 1
                else:
                    bad += 1
                    if len(misses) < 10:
                        misses.append(
                            f"{b['number']} v{version} {path}: gitlink "
                            f"{links[path][:10]} != v{row['child_version']} "
                            f"{want[:10]}")
    return {"gitlinks_matching_bom_child_version": ok, "mismatched": bad,
            "not_checkable": skipped, "examples": misses}


def ref_fingerprint(root: Path) -> dict[str, dict[str, str]]:
    out = {}
    for p in sorted(root.glob("*.git")):
        out[p.name] = existing_refs(p)
    return out


# -- CLI ----------------------------------------------------------------------

def _print_report(res: dict) -> None:
    print("plm.split — per-item bare repos under", REPOS)
    print("-" * 72)
    for k, v in res["summary"].items():
        print(f"  {k:<30} {v:>10}")
    if res.get("skipped"):
        print("-" * 72)
        print("  NOT REGENERATED — these repos hold pushed work, which exists")
        print("  nowhere else. This is correct: a repo stops being disposable")
        print("  the moment somebody pushes to it.")
        for line in res["skipped"]:
            print(f"    - {line}")
    if res["problems"]:
        print("-" * 72)
        print("  PROBLEMS (registry facts, reported not swallowed):")
        for k, v in sorted(res["problems"].items()):
            print(f"    {k}: {len(v)}")
            for line in v[:5]:
                print(f"      - {line}")
            if len(v) > 5:
                print(f"      … and {len(v) - 5} more")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m plm.split",
        description="Generate real per-item bare git repos from the registry.")
    ap.add_argument("--all", action="store_true", help="every item")
    ap.add_argument("--item", action="append", default=[],
                    help="one item number (repeatable); children are built too")
    ap.add_argument("--verify-deterministic", action="store_true",
                    help="build a second copy into a scratch root and diff refs")
    ap.add_argument("--verify-pins", action="store_true",
                    help="check every gitlink against bom.child_version")
    ap.add_argument("--repos", help="override the output root")
    ap.add_argument("--force", action="store_true",
                    help="regenerate even a repo that holds pushed work — this "
                         "DESTROYS commits that exist nowhere else")
    ap.add_argument("--normalize-names", dest="normalize", action="store_true",
                    help="§6c-1: strip the item number and the version token "
                         "from tracked filenames, so a revision is a "
                         "MODIFICATION and `git log --follow` works. THE "
                         "MIGRATION RUNS WITH THIS ON (the rollout runbook). The "
                         "first build of a repos root RECORDS the choice "
                         f"({LAYOUT_FILE}); later runs follow it and a run "
                         "that contradicts it is refused")
    ap.add_argument("--force-layout", action="store_true",
                    help="change a repos root's recorded filename layout on "
                         "purpose (rewrites the marker; pair with --all "
                         "--prune)")
    ap.add_argument("--prune", action="store_true",
                    help="delete generated refs (v* tags, draft/* branches, "
                         "main) the registry no longer has — what an "
                         "incremental rebuild over an existing repo needs "
                         "(plm/gitsync.py always passes it)")
    ap.add_argument("--no-normalize-names", dest="normalize",
                    action="store_false",
                    help="version-tokened filenames (a NEW root's default "
                         "when neither flag is given)")
    ap.add_argument("--lfs", dest="lfs_layout", action="store_true",
                    help="stage 17 (docs/LARGE-FILES.md): meshes and files "
                         "over PLM_LFS_THRESHOLD_BYTES become Git LFS pointers "
                         "(bytes into plm's LFS store), every commit carries "
                         "the .gitattributes template, and [[files.external]] "
                         "rows whose bytes the store holds move INTO the tree. "
                         "Recorded in the layout marker like --normalize-names")
    ap.add_argument("--no-lfs", dest="lfs_layout", action="store_false",
                    help="plain blobs (a root recorded before stage 17)")
    ap.set_defaults(normalize=None, lfs_layout=None)
    args = ap.parse_args(argv)

    global REPOS
    if args.repos:
        REPOS = Path(args.repos)
    if not (args.all or args.item or args.verify_deterministic):
        ap.print_help()
        return 2

    with db.connect() as conn:
        roots = None
        if args.item:
            roots = []
            for ref in args.item:
                it = db.resolve_item(conn, ref)
                if not it:
                    print(f"unknown item: {ref}", file=sys.stderr)
                    return 2
                roots.append(it["id"])
        try:
            res = run(conn, roots=roots, force=args.force,
                      normalize=args.normalize, prune=args.prune,
                      force_layout=args.force_layout,
                      lfs_layout=args.lfs_layout)
        except SplitError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        _print_report(res)
        if args.verify_pins:
            reg = load(conn)
            print("-" * 72)
            for k, v in verify_pins(conn, res["built"], reg).items():
                print(f"  {k}: {v}")
        if args.verify_deterministic:
            first = ref_fingerprint(REPOS)
            # Inside the repos root, not beside it: the container mounts
            # /srv/repos and nothing else under /srv, so a sibling directory is
            # a PermissionError. A dot-directory is invisible to every `*.git`
            # glob (the server's, ref_fingerprint's) and is removed below.
            scratch = Path(os.environ.get("PLM_SPLIT_VERIFY_ROOT")
                           or REPOS / ".verify")
            if scratch.exists():
                shutil.rmtree(scratch)
            keep = REPOS
            run(conn, roots=roots, repos=scratch, force=True,
                normalize=res["normalize_names"], lfs_layout=res["lfs"])
            second = ref_fingerprint(scratch)
            REPOS = keep
            same = first == second
            print("-" * 72)
            print(f"  determinism: built twice into separate object stores")
            print(f"    repos compared      {len(first)}")
            print(f"    refs compared       {sum(len(v) for v in first.values())}")
            print(f"    IDENTICAL           {same}")
            if not same:
                for name in sorted(set(first) | set(second)):
                    a, b = first.get(name, {}), second.get(name, {})
                    for ref in sorted(set(a) | set(b)):
                        if a.get(ref) != b.get(ref):
                            print(f"    DIFF {name} {ref}: "
                                  f"{a.get(ref)} != {b.get(ref)}")
            shutil.rmtree(scratch)
            return 0 if same else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
