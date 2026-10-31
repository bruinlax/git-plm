"""Read the generated bare repos — the data layer under `/api/repo/*`.

Everything the repo page needs (§6c: ref dropdown, file tree with last-commit
per row, commits, compare) comes straight out of `git ls-tree`, `cat-file`,
`log` and `diff` on the bare repo. There is no second index to maintain and
nothing to invalidate: the repo IS the record, so a page rendered at a ref and a
`git clone` of that ref can never disagree.

Only two things here are more than a thin wrapper, and both exist because git
answers a slightly different question than the UI asks:

  pin_label()      a gitlink is a SHA. A human wants "v0.3.1", or, when the pin
                   is not a release, the Go-style pseudo-version the plan asks
                   for (`0.3.2-0.20260917-a1b2c3d`: after 0.3.1, not a release).
  compare()        git's diff is per file. A PLM diff is also per attribute, per
                   BOM line and per submodule pin — computed from the two
                   `plm.toml` blobs, which is why the manifest exists.

No shell strings anywhere: every call is an argv list, and a ref or path that
came off the wire is validated before it reaches git (see `safe_ref`).
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from urllib.parse import quote

from . import config, manifest, roles, safegit

REPOS = config.REPOS          # one home: plm/config.py

# The URL a `git clone` line should print. Host-agnostic: set it in the
# environment, exactly like every other address in this codebase.
# Default: `<PLM_PUBLIC_URL>/git`, the path a reverse proxy in front of both
# doors serves it on. The stock compose file sets it to the git port.
CLONE_BASE = (os.environ.get("PLM_GIT_CLONE_BASE", "").strip()
              or f"{config.PUBLIC_URL}/git").rstrip("/")

CATALOG_REPO = "catalog"

US = "\x1f"   # field separator inside a --format line; never appears in our data

class RepoError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def _gated(fn, value):
    """Run one `plm/safegit.py` gate and re-raise its refusal as a `RepoError`.

    ⭐ THE RULES MOVED, THE DOOR DID NOT (stage 7). These three were the only
    correct implementations in the codebase and the review said so; they now
    live in `plm/safegit.py` so `gitindex`, `gitwrite`, `storage` and `ops` can
    use the SAME ones (L2/H1), and these stay as the names every call site here
    already spells — including the exception type its callers catch.
    """
    try:
        return fn(value)
    except safegit.SafeError as exc:
        raise RepoError(str(exc), exc.code) from None


def safe_ref(ref: str) -> str:
    return _gated(safegit.safe_ref, ref)


def safe_path(path: str) -> str:
    return _gated(safegit.safe_path, path)


def safe_number(number: str) -> str:
    return _gated(safegit.safe_number, number)


def repo_path(number: str) -> Path:
    p = REPOS / f"{safe_number(number)}.git"
    if not (p / "HEAD").exists():
        raise RepoError(f"no repo for {number} — has plm.split run?", 404)
    return p


def git(repo: Path, *args: str, binary: bool = False, check: bool = True,
        timeout: float | None = None, max_bytes: int | None = None):
    """Every read this module makes. A thin name over `safegit.git` so the
    timeout, the capture ceiling and `GIT_CONFIG_NOSYSTEM` are the same ones the
    write path gets — one helper, not six (M5)."""
    try:
        return safegit.git(repo, *args, binary=binary, check=check,
                           timeout=timeout, max_bytes=max_bytes)
    except safegit.SafeError as exc:
        raise RepoError(str(exc), exc.code) from None


def clone_url(number: str) -> str:
    return f"{CLONE_BASE}/{number}.git"


# -- refs ---------------------------------------------------------------------

_REF_FMT = US.join([
    "%(refname)", "%(objectname)", "%(*objectname)",
    "%(creatordate:iso8601-strict)", "%(*creatordate:iso8601-strict)",
    "%(subject)", "%(*subject)",
])

# WHY THE TRAILERS ARE NOT IN THAT FORMAT (git 2.43, measured): `for-each-ref`
# parses the trailers ONCE per ref and reuses it for every `%(trailers:...)`
# atom in the same format string, so a second atom with a different `key=`
# silently returns the FIRST atom's selection — two atoms produced
# "0.16.3,PROTOTYPE" in both fields. `git show -s` has no such quirk, so the
# version and state of every listed ref come from ONE batched `show` over the
# commits instead. (`separator=` must also be given as `%x2C`, not `%20`:
# an unrecognised escape is emitted literally and the default separator is a
# NEWLINE, which would break a line-oriented format.)
_TRAILER_FMT = US.join([
    "%H",
    "%(trailers:key=Plm-Version,valueonly,separator=%x2C)",
    "%(trailers:key=Plm-State,valueonly,separator=%x2C)",
])


def _trailers(repo: Path, shas: list[str]) -> dict[str, tuple[str, str]]:
    """{commit: (version, state)} for many commits in one call."""
    out: dict[str, tuple[str, str]] = {}
    shas = [s for s in dict.fromkeys(shas) if s]
    for i in range(0, len(shas), 200):
        raw = git(repo, "show", "-s", f"--format={_TRAILER_FMT}",
                  *shas[i:i + 200], check=False)
        for line in raw.splitlines():
            f = (line.split(US) + ["", "", ""])[:3]
            if f[0]:
                out[f[0]] = (f[1].strip(), f[2].strip())
    return out

# ONE HOME: the state -> maturity projection is the manifest's, and a tag
# annotation that disagreed with the plm.toml in the same tree would be a bug
# nobody could see. gitrepo never touches the database; this is the only thing
# it borrows from the writer side.
_MATURITY = manifest.MATURITY_BY_STATE


_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _branch_version(repo: Path, name: str, commit: str) -> str:
    """What version a draft BRANCH is, when the commit carries no trailer.

    `split` names its drafts `draft/vX.Y.Z`, so the name answered it. A pushed
    draft is named by its author — `draft/vent-slot-rework` — and the name then
    answers nothing; `plm.toml` at the tip is the fact. (Reading it here is one
    `cat-file` per DRAFT branch, and an item has a handful at most.)
    """
    guess = name.rsplit("/v", 1)[-1]
    if _SEMVER.match(guess):
        return guess
    try:
        doc = tomllib.loads(git(repo, "cat-file", "blob", f"{commit}:plm.toml",
                                check=False) or "")
    except (tomllib.TOMLDecodeError, ValueError):
        return ""
    v = str(doc.get("version") or "")
    return v if _SEMVER.match(v) else ""


def _version_key(v: str) -> tuple:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in (parts + ["0", "0", "0"])[:3])


def refs(number: str) -> dict:
    repo = repo_path(number)
    out_releases, out_drafts = [], []
    rows = []
    for line in git(repo, "for-each-ref", f"--format={_REF_FMT}",
                    "refs/tags", "refs/heads").splitlines():
        f = line.split(US)
        if len(f) < 7:
            continue
        rows.append(f[:7])
    trailers = _trailers(repo, [(r[2] or r[1]) for r in rows])
    main = None
    for refname, obj, deref, cdate, dcdate, subj, dsubj in rows:
        annotated = bool(deref)
        commit = deref or obj
        date = dcdate or cdate
        summary = dsubj or subj
        version, state = trailers.get(commit, ("", ""))
        if refname.startswith("refs/tags/"):
            name = refname[len("refs/tags/"):]
            out_releases.append({
                "ref": name, "kind": "tag", "commit": commit, "date": date,
                "version": version or name.lstrip("v"),
                "state": state, "maturity": _MATURITY.get(state, state.lower()),
                "summary": summary, "annotated": annotated,
            })
        elif refname.startswith("refs/heads/"):
            name = refname[len("refs/heads/"):]
            if name == "main":
                # THE DEFAULT BRANCH, as a fact the page can show (GitHub lists
                # it first under Branches). Released work only: plm moves it on
                # approval, a push cannot. `at` (the tag on its tip) is filled
                # once the tags are in.
                main = {"ref": "main", "kind": "branch", "commit": commit,
                        "date": date, "version": version, "state": state,
                        "maturity": "", "summary": summary, "annotated": False,
                        "default": True, "at": ""}
                continue
            out_drafts.append({
                "ref": name, "kind": "branch", "commit": commit, "date": date,
                "version": version or _branch_version(repo, name, commit),
                "state": state or "DRAFT", "maturity": "",
                "summary": summary, "annotated": False,
            })
    # WHAT A RELEASE IS *NOW*, not what it was when it was tagged (§2.7). A
    # promotion writes `[status]` into `refs/notes/plm` and never touches the
    # tag, so a tag annotated PROTOTYPE may today be RELEASED — and the Releases
    # rail, the release gate and the "Latest" mark all have to agree about that
    # or the UI will contradict a refusal. One read of the notes tree, then a
    # blob only for the commits that actually have one.
    _overlay_status(repo, out_releases)
    out_releases.sort(key=lambda r: _version_key(r["version"]), reverse=True)
    if main:
        main["at"] = next((r["ref"] for r in out_releases
                           if r["commit"] == main["commit"]), "")
    out_drafts.sort(key=lambda r: _version_key(r["version"]), reverse=True)
    latest_released = next((r["ref"] for r in out_releases if r.get("released")),
                           "")

    head = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    default = "main" if head == "main" else (head or "main")
    return {
        "number": number,
        "description": (repo / "description").read_text().strip()
        if (repo / "description").exists() else "",
        "kind": git(repo, "config", "--get", "plm.kind", check=False).strip(),
        "clone_url": clone_url(number),
        "default_ref": default,
        "default_release": out_releases[0]["ref"] if out_releases else "",
        # "Latest" in the Teamcenter sense: the newest revision that is actually
        # RELEASED, which is what a BOM line marked `track = "release"` should
        # follow. `default_release` stays the newest TAG, because that is what
        # the page opens at.
        "latest_released": latest_released,
        "releases": out_releases,
        "drafts": out_drafts,
        # None when the repo has no `main` yet (nothing released): the client
        # lists exactly the branches the server reports, never an invented one.
        "main": main,
        "counts": {"releases": len(out_releases), "drafts": len(out_drafts),
                   "branches": len(out_drafts) + (1 if main else 0),
                   # not the keep-refs (stage 11b): a commit kept only because
                   # something pins it is not part of anybody's history here
                   "commits": int(git(repo, "rev-list", "--count",
                                      "--exclude=refs/plm/keep/*", "--all")
                                  .strip() or 0)},
    }


def _overlay_status(repo: Path, releases: list[dict]) -> None:
    """Fold `refs/notes/plm` `[status]` onto the tag rows, in place."""
    from . import gitnotes
    try:
        have = gitnotes.notes_at(repo)
    except Exception:  # noqa: BLE001 — no notes ref is the normal case
        have = {}
    for r in releases:
        r["status_source"] = "tag"
        r["released"] = (r.get("state") or "").upper() == "RELEASED"
        if r["commit"] not in have:
            continue
        st = (gitnotes.read(repo, r["commit"]) or {}).get("status") or {}
        if not (st.get("state") or st.get("maturity")):
            continue
        r["state"] = str(st.get("state") or r["state"])
        r["maturity"] = str(st.get("maturity")
                            or _MATURITY.get(r["state"], r["state"].lower()))
        r["status_source"] = "note"
        r["released"] = (r["state"].upper() == "RELEASED"
                         or r["maturity"].lower() == "released")
        r["promoted"] = {"approver": st.get("approver", ""),
                         "approver_kind": st.get("approver_kind", ""),
                         "date": st.get("date", ""),
                         "release": st.get("release"),
                         "from_maturity": st.get("from_maturity", ""),
                         "evidence_sha256": st.get("evidence_sha256", "")}


def resolve(repo: Path, ref: str) -> str:
    sha = git(repo, "rev-parse", "--verify", f"{safe_ref(ref)}^{{commit}}",
              check=False).strip()
    if not sha:
        raise RepoError(f"unknown ref: {ref}", 404)
    return sha


# -- the submodule pin label --------------------------------------------------

def pin_label(child_number: str, commit: str) -> dict:
    """What a gitlink at `commit` of `child_number` should be CALLED.

    A release gets its tag. Anything else gets the Go pseudo-version form the
    plan asks for — `<next patch>-0.<yyyymmdd>-<short sha>` — which sorts after
    the release it follows and reads as "after 0.3.1, not a release". Getting
    this wrong is how a draft pin ends up looking like a release in a BOM.
    """
    out = {"commit": commit, "ref": "", "released": False,
           "pseudo_version": "", "label": commit[:10], "known": False}
    try:
        repo = repo_path(child_number)
    except RepoError:
        return out
    out["known"] = True
    tag = git(repo, "describe", "--tags", "--exact-match", commit,
              check=False).strip()
    if tag:
        out.update({"ref": tag, "released": True, "label": tag})
        return out
    near = git(repo, "describe", "--tags", "--abbrev=7", commit,
               check=False).strip()
    base = near.split("-")[0].lstrip("v") if near else "0.0.0"
    maj, mnr, pat = (_version_key(base) if near else (0, 0, 0))
    when = git(repo, "show", "-s", "--format=%cd", "--date=format:%Y%m%d",
               commit, check=False).strip()
    out["pseudo_version"] = f"{maj}.{mnr}.{pat + 1}-0.{when}-{commit[:7]}"
    out["label"] = out["pseudo_version"]
    return out


# -- the tree -----------------------------------------------------------------

def _last_commit(repo: Path, ref: str, path: str) -> dict:
    line = git(repo, "log", "-1", f"--format=%H{US}%s{US}%cI", safe_ref(ref),
               "--", path, check=False).strip()
    if not line:
        return {}
    sha, subject, date = (line.split(US) + ["", "", ""])[:3]
    return {"sha": sha, "short": sha[:10], "subject": subject, "date": date}


def tree(number: str, ref: str = "HEAD", path: str = "", *,
         last_commit: bool = True) -> dict:
    repo = repo_path(number)
    commit = resolve(repo, ref)
    path = safe_path(path)
    spec = f"{commit}:{path}" if path else f"{commit}^{{tree}}"
    raw = git(repo, "ls-tree", "--long", "-z", spec, check=False)
    if not raw:
        # Either an empty tree or a path that is a blob, not a tree.
        kind = git(repo, "cat-file", "-t", f"{commit}:{path}",
                   check=False).strip() if path else "tree"
        if kind == "blob":
            raise RepoError(f"{path} is a file, not a directory — use /blob", 400)
        if path and not kind:
            raise RepoError(f"no such path at {ref}: {path}", 404)
    entries = []
    for rec in raw.split("\0"):
        if not rec.strip():
            continue
        meta, _, name = rec.partition("\t")
        bits = meta.split()
        mode, otype, osha = bits[0], bits[1], bits[2]
        size = bits[3] if len(bits) > 3 else "-"
        full = f"{path}/{name}" if path else name
        e = {
            "name": name, "path": full, "sha": osha, "mode": mode,
            "type": {"blob": "blob", "tree": "tree",
                     "commit": "submodule"}[otype],
            "size": int(size) if size.isdigit() else None,
        }
        if (e["type"] == "blob" and e["size"] is not None
                and e["size"] <= _lfs().POINTER_MAX
                and not _lfs().always_plain(full)):
            ptr = _lfs().blob_pointer(repo, osha, e["size"])
            if ptr:
                # stage 17: the row is the FILE; the pointer is transport
                e.update({"size": ptr[1], "lfs": {
                    "oid": ptr[0], "present": _lfs().has(ptr[0], ptr[1])}})
        if e["type"] == "submodule":
            child = Path(name).name
            e["submodule"] = {"number": child,
                              "url": f"../{child}.git",
                              **pin_label(child, osha)}
        if last_commit:
            e["last_commit"] = _last_commit(repo, commit, full)
        entries.append(e)
    entries.sort(key=lambda e: (e["type"] == "blob", e["name"]))
    return {"number": number, "ref": ref, "commit": commit, "path": path,
            "entries": entries,
            "readme": "README.md" if any(
                e["name"] == "README.md" for e in entries) else ""}


#: A blob bigger than this is never held in memory by `blob()`; the route has to
#: stream it. 8 MB covers every manifest, README, drawing and source file in the
#: registry and stops well short of the 85 MB mesh the review measured (H5) —
#: one unauthenticated GET of which cost 170-250 MB RSS, twice over, because the
#: bytes were read into a `bytes` and then copied into the `Response`.
BLOB_INLINE_MAX = int(os.environ.get("PLM_BLOB_INLINE_MAX", str(8 * 1024 * 1024)))


def blob_info(number: str, ref: str, path: str) -> dict:
    """`{repo, commit, path, size}` for a blob — WITHOUT reading it.

    `cat-file -t` and `-s` are two cheap calls that let a route decide whether
    to hold the bytes or stream them, which is the whole fix for H5: the size is
    knowable before the memory is spent.
    """
    repo = repo_path(number)
    commit = resolve(repo, ref)
    path = safe_path(path)
    if not path:
        raise RepoError("path= is required", 400)
    spec = f"{commit}:{path}"
    kind = git(repo, "cat-file", "-t", spec, check=False).strip()
    if kind != "blob":
        raise RepoError(f"no such file at {ref}: {path}", 404)
    raw = git(repo, "cat-file", "-s", spec, check=False).strip()
    size = int(raw) if raw.isdigit() else None
    # stage 17: a POINTER is read as the object it names — every route that
    # serves, previews, diffs or measures a file goes through here, so none of
    # them needs to know a mesh moved into LFS
    from . import lfs
    ptr = lfs.blob_pointer(repo, spec, size)
    if ptr:
        return {"repo": repo, "commit": commit, "path": path, "size": ptr[1],
                "lfs": {"oid": ptr[0], "size": ptr[1],
                        "present": lfs.has(ptr[0], ptr[1])},
                "pointer_size": size}
    return {"repo": repo, "commit": commit, "path": path, "size": size}


def _lfs_missing(number: str, ref: str, path: str, info: dict) -> RepoError:
    return RepoError(
        f"{path} at {ref} is a Git LFS pointer to {info['lfs']['oid'][:12]}… "
        f"({info['lfs']['size']:,} bytes), and that object is not in plm's "
        "store.\nThe commit was accepted without it only if it predates the "
        "store; push it from a clone that has it:\n"
        f"  git lfs push --object-id origin {info['lfs']['oid']}", 404)


_RELEASE_REF = re.compile(r"^(?:refs/tags/)?v(\d+\.\d+\.\d+)$")


def download_name(number: str, ref: str, path: str) -> str:
    """What `Content-Disposition` calls this blob (§6c-1, last bullet).

    **The TREE is stable; the DOWNLOAD is labelled.** Once the migration has
    taken the version out of the filename, a mesh pulled off a release tag
    arrives in somebody's Downloads folder as `floor_plate.stl` — next to the
    four other `floor_plate.stl`s they pulled off four other parts. So a blob
    fetched *at a release tag* is stamped on the way out, and only there:

        v0.3.0 + stl/floor_plate.stl  ->  GSI-PRT-0004_v0.3.0_floor_plate.stl
        main   + stl/floor_plate.stl  ->  floor_plate.stl
        a draft branch                ->  floor_plate.stl

    A branch is a moving target and a stamped name would be a lie about which
    bytes these are; a tag is the one ref that can honestly carry a version.
    Re-stamping a name that already carries the number is skipped, so this is
    idempotent against a repo that has NOT been normalised.
    """
    name = Path(path).name
    m = _RELEASE_REF.match(str(ref or ""))
    if not m or not name:
        return name
    stem, ext = _stem_ext(name, number)
    if not stem:
        return name
    return f"{number}_v{m.group(1)}_{stem}{ext}"


def _capped(it, cap: int, label: str):
    """`safegit.stream`'s byte ceiling, for an iterator that is not a git
    process (an LFS object): stop BEFORE the chunk that would cross it."""
    sent = 0
    for buf in it:
        sent += len(buf)
        if sent > cap:
            raise safegit.GitTooBig(
                f"{label} exceeded the {cap:,}-byte ceiling for a single "
                "response.")
        yield buf


def blob_stream(number: str, ref: str, path: str, *, max_bytes: int | None = None):
    """`(iterator, commit, size)` — the bytes, in chunks, with a ceiling.

    `storage._git_stream` has always done it this way and the review named it
    the pattern to copy; `safegit.stream` is now that one implementation, and
    both callers use it.
    """
    info = blob_info(number, ref, path)
    if info.get("lfs"):
        from . import lfs
        if not info["lfs"]["present"]:
            raise _lfs_missing(number, ref, path, info)
        # Stage 18: the ceiling applies to an LFS object too (found when the
        # live repos were re-split with --lfs and security-smoke's H5 probe
        # streamed an 8.9 MB pointer-backed mesh past a 1 MiB cap). No
        # explicit cap = the largest object the store accepts.
        cap = config.LFS_MAX_OBJECT if max_bytes is None else max_bytes
        return (_capped(lfs.stream(info["lfs"]["oid"]), cap, path),
                info["commit"], info["size"])
    it = safegit.stream(info["repo"], "cat-file", "blob",
                        f"{info['commit']}:{info['path']}", max_bytes=max_bytes)
    return it, info["commit"], info["size"]


def blob(number: str, ref: str, path: str, *,
         max_bytes: int | None = None) -> tuple[bytes, str]:
    """The whole blob in memory. ⛔ Bounded — a caller that might be handed a
    mesh wants `blob_stream`."""
    info = blob_info(number, ref, path)
    cap = BLOB_INLINE_MAX if max_bytes is None else max_bytes
    if info["size"] is not None and info["size"] > cap:
        raise RepoError(
            f"{path} is {info['size']:,} bytes at {ref}, over the "
            f"{cap:,}-byte ceiling for reading a blob into memory.\n"
            "Fetch it as a download instead — GET "
            f"/api/repo/{number}/blob?ref={ref}&path={path} streams it.", 413)
    if info.get("lfs"):
        from . import lfs
        if not info["lfs"]["present"]:
            raise _lfs_missing(number, ref, path, info)
        return lfs.read(info["lfs"]["oid"]), info["commit"]
    return git(info["repo"], "cat-file", "blob",
               f"{info['commit']}:{info['path']}", binary=True,
               max_bytes=cap), info["commit"]


def manifest_at(number: str, ref: str) -> dict:
    data, commit = blob(number, ref, "plm.toml")
    text = data.decode(errors="replace")
    return {"number": number, "ref": ref, "commit": commit,
            "manifest": tomllib.loads(text), "toml": text}


def commits(number: str, ref: str = "HEAD", limit: int = 50,
            path: str = "") -> dict:
    repo = repo_path(number)
    commit = resolve(repo, ref)
    limit = max(1, min(int(limit or 50), 500))
    args = ["log", f"-{limit}",
            f"--format=%H{US}%s{US}%an{US}%aI{US}%cI{US}"
            f"%(trailers:key=Plm-Version,valueonly,separator=%x2C){US}"
            f"%(trailers:key=Plm-State,valueonly,separator=%x2C)", commit]
    if path:
        args += ["--", safe_path(path)]
    out = []
    for line in git(repo, *args).splitlines():
        f = (line.split(US) + [""] * 7)[:7]
        out.append({"sha": f[0], "short": f[0][:10], "subject": f[1],
                    "author": f[2], "authored": f[3], "date": f[4],
                    "version": f[5].strip(), "state": f[6].strip()})
    return {"number": number, "ref": ref, "commit": commit, "path": path,
            "commits": out}


# -- compare ------------------------------------------------------------------

# What the UI can SHOW a file with, and whether git can say anything useful
# about a change to it. Both answers come from `plm.roles.MEDIA_KINDS` — this
# module used to keep a THIRD opinion here (`_MESH_EXT` / `_IMAGE_EXT` /
# `_BINARY_EXT`, three extension sets), which disagreed with `web/app.js`'s
# `previewKind()` about `.svg` and with `roles.py` about everything else.

#: The kinds a unified diff is honest for. Everything else gets a size delta:
#: an ASCII STL's patch is 90 000 lines that mean nothing.
DIFFABLE_KINDS = frozenset({"text", "markdown", "manifest", "pattern"})


def _kind_of(path: str) -> str:
    """This repo's answer to "what is this file", which is `plm.roles`'s answer.
    A bare path is all git gives us here, so the extension fallback inside
    `roles.kind_of` is doing the work — the point is that it is the SAME table
    the sniffed-from-bytes path uses, not a second one."""
    return roles.kind_of(path)


def _blob_url(number: str, ref: str, path: str) -> str:
    return (f"/api/repo/{quote(number, safe='')}/blob"
            f"?ref={quote(ref, safe='')}&path={quote(path, safe='')}")


def _ext_url(number: str, ref: str, name: str) -> str:
    return (f"/api/repo/{quote(number, safe='')}/external"
            f"?ref={quote(ref, safe='')}&name={quote(name, safe='')}")


def _numstat(repo: Path, a: str, b: str) -> list[dict]:
    """Per-file add/delete counts. `--no-renames` on purpose: the whole point of
    killing `versions/vX/` is that a diff between two revisions is a REAL diff,
    so nothing here is allowed to lean on similarity detection to look good."""
    out = []
    raw = git(repo, "diff", "--no-renames", "--numstat", "-z", a, b)
    fields = [x for x in raw.split("\0") if x != ""]
    i = 0
    while i < len(fields):
        rec = fields[i]
        adds, dels, name = (rec.split("\t") + ["", "", ""])[:3]
        i += 1
        binary = adds == "-" or dels == "-"
        out.append({"path": name,
                    "additions": 0 if binary else int(adds or 0),
                    "deletions": 0 if binary else int(dels or 0),
                    "binary": binary})
    return out


def _status(repo: Path, a: str, b: str) -> dict[str, str]:
    raw = git(repo, "diff", "--no-renames", "--name-status", "-z", a, b)
    fields = [x for x in raw.split("\0") if x != ""]
    out = {}
    i = 0
    while i + 1 < len(fields):
        code, path = fields[i], fields[i + 1]
        out[path] = {"A": "added", "D": "deleted", "M": "modified",
                     "T": "typechange"}.get(code[0], code[0].lower())
        i += 2
    return out


def _patch(repo: Path, a: str, b: str, path: str, max_bytes: int) -> str:
    """The unified diff for one path, STREAMED and stopped at `max_bytes`.

    ⚠️ It used to CAPTURE the whole diff and slice it afterwards, which meant
    plm built an 88 MB string in order to show 60 KB of it. Found in the stage-8
    migration rehearsal: `KML-PRT-1011 v0.1.0..v0.2.0` is a pair of large ASCII
    STLs, `git diff` produced 88,274,719 bytes, and `safegit`'s 64 MB capture
    ceiling did its job and refused — so the compare view for that item raised
    instead of rendering. The ceiling was right; the caller was wrong.

    `max_patch` is capped at 400 KB (`MAX_PATCH_CAP`), so this reads at most
    that much and kills git. Nothing is truncated that was going to be shown.
    """
    want = max(0, int(max_bytes))
    if not want:
        return ""
    buf = bytearray()
    try:
        for chunk in safegit.stream(repo, "diff", "--no-renames", "--unified=3",
                                    a, b, "--", path,
                                    max_bytes=want + (1 << 16)):
            buf += chunk
            if len(buf) > want:
                break
    except safegit.SafeError:
        # A diff git itself refuses (a missing object, a broken pack) is a row
        # with no patch, not a 500 for the whole compare.
        return ""
    if len(buf) > want:
        return (bytes(buf[:want]).decode(errors="replace")
                + f"\n… truncated at {want} bytes\n")
    return bytes(buf).decode(errors="replace")


def _tree_sizes(repo: Path, rev: str) -> dict[str, int]:
    """Every blob size at `rev`, keyed by path.

    A binary file's patch is withheld on purpose (an ASCII STL yields 90 000
    "added lines" that mean nothing), which leaves the compare view with a row
    that says a file changed and nothing about HOW. Its size is the one honest
    number available without parsing the mesh, and one `ls-tree -r -l` per side
    answers it for every file at once — cheaper than a `cat-file` per row."""
    raw = git(repo, "ls-tree", "-r", "-l", "-z", rev, check=False)
    out: dict[str, int] = {}
    for rec in raw.split("\0"):
        if not rec:
            continue
        info, _, path = rec.partition("\t")
        f = info.split()
        if len(f) < 4 or f[1] != "blob":
            continue
        try:
            out[path] = int(f[3])
        except ValueError:
            continue
        # stage 17: an LFS pointer's honest size is the OBJECT's — the size
        # delta of a mesh is the one number this row has
        if out[path] <= _lfs().POINTER_MAX and not _lfs().always_plain(path):
            ptr = _lfs().blob_pointer(repo, f[2], out[path])
            if ptr:
                out[path] = ptr[1]
    return out


def _lfs():
    from . import lfs
    return lfs


def _manifest_or_empty(repo: Path, commit: str) -> dict:
    raw = git(repo, "cat-file", "blob", f"{commit}:plm.toml",
              binary=True, check=False)
    if not raw:
        return {}
    try:
        return tomllib.loads(raw.decode(errors="replace"))
    except tomllib.TOMLDecodeError:
        return {}


def _bom_key(line: dict) -> str:
    # stage 19: a purchase REQUIREMENT line has no path — keyed by its text
    return str(line.get("path", "")) or \
        "requirement:" + str(line.get("requirement", ""))


def _manifest_delta(a: dict, b: dict) -> dict:
    """The PLM half of a compare: what CHANGED about the part, not about bytes."""
    fields = []
    for k in ("version", "state", "maturity", "process", "name", "kind",
              "project", "summary", "verification", "ecn", "author"):
        if a.get(k, "") != b.get(k, ""):
            fields.append({"name": k, "from": a.get(k, ""), "to": b.get(k, "")})

    aa, ba = a.get("attributes") or {}, b.get("attributes") or {}
    attrs = {"added": [], "removed": [], "changed": []}
    for n in sorted(set(aa) | set(ba)):
        x, y = aa.get(n), ba.get(n)
        if x is None:
            attrs["added"].append({"name": n, "to": y})
        elif y is None:
            attrs["removed"].append({"name": n, "from": x})
        elif x != y:
            attrs["changed"].append({"name": n, "from": x, "to": y})

    ab = {_bom_key(l): l for l in (a.get("bom") or [])}
    bb = {_bom_key(l): l for l in (b.get("bom") or [])}
    bom = {"added": [], "removed": [], "qty_changed": [], "changed": [],
           "unchanged": 0}
    for k in sorted(set(ab) | set(bb)):
        x, y = ab.get(k), bb.get(k)
        if x is None:
            bom["added"].append(y)
        elif y is None:
            bom["removed"].append(x)
        elif x.get("qty") != y.get("qty"):
            bom["qty_changed"].append({"path": k, "from": x.get("qty"),
                                       "to": y.get("qty"),
                                       "unit": y.get("unit", "EA")})
        elif x != y:
            bom["changed"].append({"path": k, "from": x, "to": y})
        else:
            bom["unchanged"] += 1

    af = a.get("files") or {}
    bf = b.get("files") or {}
    # `external` is the one [files] key that is an array of TABLES, not of
    # paths — it is the external rows, diffed separately below.
    def _roles(d):
        return {p: r for r, v in d.items()
                if r != "external" and isinstance(v, list) for p in v}

    role_of_a, role_of_b = _roles(af), _roles(bf)
    files = {"added": [], "removed": [], "role_changed": [],
             "primary_changed": None}
    for p in sorted(set(role_of_a) | set(role_of_b)):
        ra, rb = role_of_a.get(p), role_of_b.get(p)
        if ra is None:
            files["added"].append({"path": p, "role": rb})
        elif rb is None:
            files["removed"].append({"path": p, "role": ra})
        elif ra != rb:
            files["role_changed"].append({"path": p, "from": ra, "to": rb})
    if af.get("primary", "") != bf.get("primary", ""):
        files["primary_changed"] = {"from": af.get("primary", ""),
                                    "to": bf.get("primary", "")}

    ext_a = {(e.get("name"), e.get("key")): e
             for e in (af.get("external") or [])}
    ext_b = {(e.get("name"), e.get("key")): e
             for e in (bf.get("external") or [])}
    files["external_added"] = [ext_b[k] for k in sorted(set(ext_b) - set(ext_a))]
    files["external_removed"] = [ext_a[k] for k in sorted(set(ext_a) - set(ext_b))]

    def _set(rows, keys):
        return {tuple(str(r.get(k, "")) for k in keys): r for r in (rows or [])}

    ar = _set(a.get("relations"), ("type", "item", "version"))
    br = _set(b.get("relations"), ("type", "item", "version"))
    rel = {"added": [br[k] for k in sorted(set(br) - set(ar))],
           "removed": [ar[k] for k in sorted(set(ar) - set(br))]}
    ao = _set(a.get("routing"), ("seq", "op"))
    bo = _set(b.get("routing"), ("seq", "op"))
    routing = {"added": [bo[k] for k in sorted(set(bo) - set(ao))],
               "removed": [ao[k] for k in sorted(set(ao) - set(bo))],
               "changed": [{"from": ao[k], "to": bo[k]}
                           for k in sorted(set(ao) & set(bo)) if ao[k] != bo[k]]}
    at, bt = set(a.get("tags") or []), set(b.get("tags") or [])
    return {"fields": fields, "attributes": attrs, "bom": bom, "files": files,
            "relations": rel, "routing": routing,
            "tags": {"added": sorted(bt - at), "removed": sorted(at - bt)}}


def _submodule_changes(repo: Path, a: str, b: str) -> list[dict]:
    """Every gitlink that moved, with the CHILD's commit subjects between the
    two pins — `git log old..new` run inside the child repo, which is the BOM
    delta with reasons that §5.1 says nobody has to build."""
    out = []
    for line in git(repo, "diff", "--no-renames", "--raw", "--abbrev=40",
                    a, b).splitlines():
        # :160000 160000 <old> <new> M\tpath
        if not line.startswith(":"):
            continue
        meta, _, path = line.partition("\t")
        bits = meta.split()
        if len(bits) < 5 or "160000" not in (bits[0], bits[1]):
            continue
        old, new = bits[2], bits[3]
        child = Path(path).name if path != "catalog" else CATALOG_REPO
        entry = {"path": path, "number": child,
                 "from": {"commit": old} if old.strip("0") else None,
                 "to": {"commit": new} if new.strip("0") else None,
                 "commits": [], "direction": "unknown", "note": ""}
        if entry["from"]:
            entry["from"].update(pin_label(child, old))
        if entry["to"]:
            entry["to"].update(pin_label(child, new))
        try:
            crepo = repo_path(child)
        except RepoError:
            entry["note"] = f"no repo for {child}"
            out.append(entry)
            continue
        if entry["from"] and entry["to"]:
            for direction, rng in (("forward", f"{old}..{new}"),
                                   ("backward", f"{new}..{old}")):
                raw = git(crepo, "log", f"--format=%H{US}%s{US}%cI", rng,
                          check=False).strip()
                if raw:
                    entry["direction"] = direction
                    entry["commits"] = [
                        {"sha": f[0], "short": f[0][:10], "subject": f[1],
                         "date": f[2]}
                        for f in (l.split(US) for l in raw.splitlines())
                        if len(f) >= 3]
                    break
        out.append(entry)
    return out


# -- pairing: the same file under a new name ----------------------------------
#
# A generated filename carries the item number and the version it was cut at —
# `GSI-PRT-0004_v0.2.0_floor_plate.stl` becomes `…_v0.3.0_floor_plate.stl`. Git,
# with rename detection deliberately OFF (see _numstat), therefore reports the
# one mesh you most want to diff as an unrelated delete and an unrelated add,
# and the UI has nothing to overlay. Both of those tokens are ceremony, not
# identity: strip them and the same file at two revisions has the same name.

# ⚠️ AN ITEM NUMBER, NOT SOMETHING SHAPED LIKE ONE. This was
# `^[a-z]{2,5}-(?:[a-z]{2,5}-)?\d+[-_]*`, which is fine for a pairing key that
# nobody reads but destroys meaning the moment it renames a FILE: measured over
# the registry, it turned `Study-002-Slow-Pulse-complete-lit-room.blend` into
# `Slow-Pulse-complete-lit-room.blend` and `study-003-middle-ripple.json` into
# `middle-ripple.json` — 20 filenames across KML-DOC-2200 / KML-PRT-9014 /
# KML-PRT-9015 whose leading token is a STUDY number, not a part number. It also
# made the rule non-idempotent (`deck-spacer-20mm.stl` -> `mm.stl` on a second
# pass), which is a trap waiting for whoever runs the migration twice.
#
# So the prefix must carry a real CLASS TOKEN, and the list of those lives in
# `plm/config.py` and nowhere else.
_ITEM_PREFIX = re.compile(
    r"^[a-z][a-z0-9]{0,7}-(?:"
    + "|".join(sorted(config.KIND_BY_CLASS, key=len, reverse=True))
    + r")-\d+[-_.]*", re.I)
_VERSION_TOKEN = re.compile(r"(?<![0-9a-z])v?\d+[._]\d+[._]\d+(?![0-9])", re.I)

# The disambiguator `split --normalize-names` appends when two files in ONE
# revision normalise to the same name (`floor_plate.stl`, `floor_plate~2.stl`).
# Recognised here so the key a pair is matched on, and the check that a declared
# storage key still belongs to its declared name, both see through it.
_DISAMBIG = re.compile(r"~(\d+)$")

# When one file is filed under several roles, this is the one it pairs on, so
# the answer does not depend on dict order.
_ROLE_PREF = ("mesh", "viz", "flat", "drawing", "render", "photo", "source",
              "doc", "artifact", "archive", "log")


# ⭐ ONE RULE, TWO USES (docs/PLM_GIT_FIRST.md §6c-1). What is ceremony in a
# generated filename — the item number and the version it was cut at — is
# decided exactly once, in `_stem_ext`. Two things are built on it:
#
#   stable_name()  the FILENAME the migration writes into the tree. Keeps the
#                  descriptive stem verbatim, case and all, because this is a
#                  name a human reads and a `git log --follow` walks.
#   _norm_key()    the MATCHING key `_pair_files` compares two sides on. Case-
#                  folded and squashed to `[a-z0-9_]`, because this one is never
#                  shown to anybody — it exists to make two spellings equal.
#
# Before stage 8 only the second existed. A second, separate regex for the
# migration would have been the "rule with more than one home" that every
# Critical in the security review turned out to be.

def _stem_ext(name: str, number: str) -> tuple[str, str]:
    """(descriptive stem, extension) with the number and version stripped.

    An empty stem means the name was NOTHING BUT ceremony
    (`KML-PRT-1001_v0.4.0.png`, 28 rows in the prod registry). Callers must not
    invent a name for those: `stable_name` leaves them alone and `_pairable`
    keeps them out of the pool.
    """
    base = Path(name).name
    ext = Path(base).suffix
    stem = base[: len(base) - len(ext)] if ext else base
    if number and stem.lower().startswith(number.lower()):
        stem = stem[len(number):]
    stem = _ITEM_PREFIX.sub("", stem)
    stem = _VERSION_TOKEN.sub("", stem)
    # Separators the stripped tokens left behind, e.g. `foo__bar` or a trailing
    # `_`. Collapsed, not deleted: `SWITCH-19MM-ASSEMBLED` keeps its hyphens.
    stem = re.sub(r"[-_.\s]{2,}", "_", stem).strip("-_. ")
    return stem, ext


def stable_name(name: str, number: str, *, seq: int = 1) -> str:
    """`GSI-PRT-0004_v0.3.0_floor_plate.stl` -> `floor_plate.stl` (§6c-1).

    The name the file keeps across every revision, so git sees a MODIFICATION
    and `git log --follow -- floor_plate.stl` walks the whole history. Returns
    the original basename unchanged when there is nothing left to call it.

    `seq > 1` appends the collision disambiguator (`floor_plate~2.stl`). Two
    files in one revision CAN normalise to the same name — the registry has two
    real cases — and the answer there is to keep both bytes under two names and
    say so, never to pick one.
    """
    stem, ext = _stem_ext(name, number)
    if not stem:
        return Path(name).name
    return f"{stem}~{seq}{ext}" if seq > 1 else f"{stem}{ext}"


def _norm_key(name: str, number: str) -> str:
    """The pairing key. `GSI-PRT-0004_v0.2.0_Floor Plate.STL` -> `floor_plate.stl`."""
    stem, ext = _stem_ext(name, number)
    stem = _DISAMBIG.sub("", stem)
    stem = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    return f"{stem}{ext.lower()}"


def _pairable(name: str, number: str) -> str:
    """The normalised key, or `""` when there is nothing distinctive left.

    Nothing left after removing the item number and the version means there is
    nothing to match ON — which is exactly what keeps SUBMODULE gitlinks out of
    the pool: `parts/KML-PRT-1008` is an item number and nothing else. A pin
    appearing while a different pin disappears is not one file renamed, and it
    is already reported, with the child's own commit subjects, in the manifest
    delta. (Measured: this alone took the registry-wide ambiguous count from 6
    to 2, and the 2 that remain are genuinely ambiguous.)
    """
    key = _norm_key(name, number)
    ext = Path(name).suffix.lower()
    stem = key[: -len(ext)] if ext and key.endswith(ext) else key
    return key if stem else ""


def _role_of(files_tbl: dict, path: str) -> str:
    hits = {r for r, v in files_tbl.items()
            if r != "external" and isinstance(v, list) and path in v}
    for pref in _ROLE_PREF:
        if pref in hits:
            return pref
    return sorted(hits)[0] if hits else ""


def _pair_files(number: str, base_ref: str, head_ref: str, man_a: dict,
                man_b: dict, files: list[dict], size_a: dict,
                size_b: dict) -> tuple[list[dict], list[dict]]:
    """Which delete+add pairs are actually one file renamed by its version.

    The rule, and it is deliberately narrow: **same manifest role, and the same
    name once the item number and any `vX.Y.Z` token are removed, same
    extension.** External rows (`[[files.external]]`) join the same pool,
    so a mesh that moved between the tree and an object store still pairs.

    Two candidates on either side is NOT a match — it is reported as ambiguous
    and both rows stay exactly as git described them. Guessing here would put a
    wrong red/green overlay in front of somebody about to print a part.
    """
    fa = man_a.get("files") or {}
    fb = man_b.get("files") or {}
    prim_a, prim_b = fa.get("primary", ""), fb.get("primary", "")
    ext_a = {e["name"]: e for e in (fa.get("external") or []) if e.get("name")}
    ext_b = {e["name"]: e for e in (fb.get("external") or []) if e.get("name")}

    def tree_side(path, ref, sizes, tbl, idx):
        return {"path": path, "source": "tree", "role": _role_of(tbl, path),
                "size": sizes.get(path), "index": idx,
                "url": _blob_url(number, ref, path)}

    def ext_side(row, ref):
        return {"path": row["name"], "source": "external",
                "role": row.get("role", ""), "size": row.get("size"),
                "index": None, "sha256": row.get("sha256", ""),
                "key": row.get("key", ""),
                "url": _ext_url(number, ref, row["name"])}

    def mk(key, a, b, reason):
        return {"key": key, "kind": _kind_of((b or a)["path"]),
                "role": (b or a)["role"] or (a or b)["role"],
                "reason": reason,
                "is_primary": bool((a and a["path"] == prim_a)
                                   or (b and b["path"] == prim_b)),
                "base": a, "head": b,
                "base_index": a["index"] if a else None,
                "head_index": b["index"] if b else None}

    pairs: list[dict] = []
    # An external whose NAME did not change but whose bytes did is invisible to
    # git entirely — it never appears in the diff. It is still a change.
    for n in sorted(set(ext_a) & set(ext_b)):
        ra, rb = ext_a[n], ext_b[n]
        if (ra.get("sha256"), ra.get("key")) == (rb.get("sha256"), rb.get("key")):
            continue
        pairs.append(mk(_norm_key(n, number), ext_side(ra, base_ref),
                        ext_side(rb, head_ref), "same name, new bytes"))

    side_a, side_b = [], []
    for i, row in enumerate(files):
        if row["status"] == "deleted":
            side_a.append(tree_side(row["path"], base_ref, size_a, fa, i))
        elif row["status"] == "added":
            side_b.append(tree_side(row["path"], head_ref, size_b, fb, i))
    for n in sorted(set(ext_a) - set(ext_b)):
        side_a.append(ext_side(ext_a[n], base_ref))
    for n in sorted(set(ext_b) - set(ext_a)):
        side_b.append(ext_side(ext_b[n], head_ref))

    def bucket(side):
        out: dict[tuple, list] = {}
        for e in side:
            key = _pairable(e["path"], number)
            if not key:
                continue
            out.setdefault((e["role"], key), []).append(e)
        return out

    ba, bb = bucket(side_a), bucket(side_b)
    ambiguous = []
    for key in sorted(set(ba) & set(bb)):
        ca, cb = ba[key], bb[key]
        if len(ca) == 1 and len(cb) == 1:
            pairs.append(mk(key[1], ca[0], cb[0], "renamed by version"))
        else:
            ambiguous.append(
                {"key": key[1], "role": key[0],
                 "base_paths": sorted(e["path"] for e in ca),
                 "head_paths": sorted(e["path"] for e in cb),
                 "note": "more than one candidate on a side — left as a separate "
                         "add and delete rather than guessed"})
    return pairs, ambiguous


def _mesh_choices(number: str, base_ref: str, head_ref: str, man_a: dict,
                  man_b: dict, files: list[dict], pairs: list[dict],
                  size_a: dict, size_b: dict) -> list[dict]:
    """Every mesh the 3-D overlay could show, primary first.

    §6c's overlay used to be a fixed answer — `[files] primary`, which on
    GSI-PRT-0004 is the POSED VIZ copy, so the printable mesh could not be
    looked at at all. This is the chooser behind it, and it says out loud which
    one is the default and why.
    """
    fa = man_a.get("files") or {}
    fb = man_b.get("files") or {}
    out, seen, used = [], set(), set()

    def add(entry):
        a = (entry["base"] or {}).get("path", "")
        b = (entry["head"] or {}).get("path", "")
        if not (a or b) or (a, b) in seen:
            return
        # A file already listed as half of a pair must not come back as a
        # one-sided entry: that is the SAME mesh offered twice, once with its
        # counterpart and once without.
        if (a and a in used) or (b and b in used):
            return
        seen.add((a, b))
        used.update(p for p in (a, b) if p)
        out.append(entry)

    # 1. the primary at each ref. Paired by ROLE ALONE, which is sound only
    #    because `[files] primary` is single-valued: there is exactly one per
    #    ref, so there is nothing to be ambiguous about.
    ma, mb = _mesh_ref(number, base_ref, man_a), _mesh_ref(number, head_ref, man_b)
    def side(m, role, sizes):
        if m["source"] == "none":
            return None
        return {"path": m["path"], "url": m.get("url", ""),
                "source": m["source"], "role": role,
                "size": m.get("size") or sizes.get(m["path"])}
    role_a, role_b = _role_of(fa, fa.get("primary", "")), _role_of(fb, fb.get("primary", ""))
    if ma["source"] != "none" or mb["source"] != "none":
        add({"key": Path((mb if mb["source"] != "none" else ma)["path"]).name,
             "role": role_b or role_a, "is_primary": True,
             "reason": "primary at each ref — first of viz > mesh > artifact "
                       "in plm.toml",
             "base": side(ma, role_a, size_a), "head": side(mb, role_b, size_b)})

    # 2. every mesh pair the renaming rule found
    for p in pairs:
        if p["kind"] == "mesh":
            add({"key": p["key"], "role": p["role"], "is_primary": p["is_primary"],
                 "reason": p["reason"], "base": p["base"], "head": p["head"]})

    # 3. meshes that kept their path, and single-sided ones
    for row in files:
        if _kind_of(row["path"]) != "mesh":
            continue
        p = row["path"]
        base = ({"path": p, "url": _blob_url(number, base_ref, p),
                 "source": "tree", "size": size_a.get(p), "role": _role_of(fa, p)}
                if row["status"] != "added" else None)
        head = ({"path": p, "url": _blob_url(number, head_ref, p),
                 "source": "tree", "size": size_b.get(p), "role": _role_of(fb, p)}
                if row["status"] != "deleted" else None)
        add({"key": _norm_key(p, number),
             "role": _role_of(fb, p) or _role_of(fa, p),
             "is_primary": p in (fa.get("primary", ""), fb.get("primary", "")),
             "reason": "same path" if row["status"] == "modified"
                       else f"{row['status']} at {head_ref}",
             "base": base, "head": head})
    return out


def _mesh_ref(number: str, ref: str, man: dict) -> dict:
    """Where the UI gets the primary mesh for a 3-D overlay at one ref.

    In the tree when the revision committed it; otherwise the external row
    named in `[[files.external]]` — which the browser reaches through plm's
    own `/external` route, never through a storage backend directly.
    """
    files = man.get("files") or {}
    primary = files.get("primary", "")
    if not primary:
        return {"path": "", "url": "", "source": "none"}
    in_tree = any(primary in v for k, v in files.items()
                  if k != "external" and isinstance(v, list))
    if in_tree:
        return {"path": primary, "source": "tree",
                "url": _blob_url(number, ref, primary)}
    for e in files.get("external") or []:
        if e.get("name") == primary:
            return {"path": primary, "source": "external",
                    "backend": e.get("backend", ""), "key": e.get("key", ""),
                    "sha256": e.get("sha256", ""), "size": e.get("size", 0),
                    "url": _ext_url(number, ref, primary)}
    return {"path": primary, "url": "", "source": "missing"}


#: Server-side ceilings on a compare (M8). `commits()` has always clamped
#: `limit` to 500; `max_files` and `max_patch` were client query parameters with
#: no ceiling at all, so `?max_files=1000000&max_patch=1000000000` ran `_patch()`
#: per file across a whole history on an unauthenticated GET. Clamped in the
#: DATA layer rather than the route, because `/api/repo/{n}/compare`, the
#: release-notes draft and the MCP all call this function.
MAX_FILES_CAP = int(os.environ.get("PLM_COMPARE_MAX_FILES", "500"))
MAX_PATCH_CAP = int(os.environ.get("PLM_COMPARE_MAX_PATCH", str(400_000)))


def compare(number: str, base: str, head: str, *, max_patch: int = 60000,
            max_files: int = 300) -> dict:
    max_files = max(0, min(int(max_files or 0), MAX_FILES_CAP))
    max_patch = max(0, min(int(max_patch or 0), MAX_PATCH_CAP))
    repo = repo_path(number)
    a, b = resolve(repo, base), resolve(repo, head)
    status = _status(repo, a, b)
    stats = _numstat(repo, a, b)
    size_a, size_b = _tree_sizes(repo, a), _tree_sizes(repo, b)
    files = []
    adds = dels = 0
    for st in stats[:max_files]:
        p = st["path"]
        kind = _kind_of(p)
        # An UNKNOWN kind (`.gitattributes`, an extensionless script) that git
        # itself reads as text is diffed as text — the registry's fallback rule
        # (§2.9). A known binary kind (a mesh, an image) never is, even when its
        # bytes are an LFS pointer, which git also calls text.
        binary = st["binary"] or (kind not in DIFFABLE_KINDS and kind != "binary")
        row = {"path": p, "status": status.get(p, "modified"),
               "additions": st["additions"], "deletions": st["deletions"],
               "binary": binary, "patch": "",
               "size_base": size_a.get(p), "size_head": size_b.get(p)}
        if not binary:
            row["patch"] = _patch(repo, a, b, p, max_patch)
        adds += st["additions"]
        dels += st["deletions"]
        files.append(row)
    man_a, man_b = _manifest_or_empty(repo, a), _manifest_or_empty(repo, b)

    # Everything a row needs to be SHOWN rather than counted: what it is, what
    # it is for, and where to fetch each side of it.
    fa, fb = man_a.get("files") or {}, man_b.get("files") or {}
    prim = {fa.get("primary", ""), fb.get("primary", "")} - {""}
    for row in files:
        p = row["path"]
        row["kind"] = _kind_of(p)
        row["role"] = _role_of(fb, p) or _role_of(fa, p)
        row["is_primary"] = p in prim
        row["base_url"] = (None if row["status"] == "added"
                           else _blob_url(number, base, p))
        row["head_url"] = (None if row["status"] == "deleted"
                           else _blob_url(number, head, p))
        row["pair"] = None
        row["paired_with"] = None

    # The Files-changed forms (web/diffwidgets.js, PLM_REVIEW_MODEL §2.9) need
    # three facts git has and the row did not carry: which rows are BOM pins
    # (a gitlink is not "binary"), what each side of a binary IS to git (blob
    # sha, LFS pointer) — so a mesh whose bytes did not change says so instead
    # of opening an overlay of two identical bodies — and plm.toml's changed
    # keys, flat ("version 0.2.0 → 0.2.1; bom[20].qty 2 → 4").
    subs = _submodule_changes(repo, a, b)
    sub_paths = {x["path"] for x in subs}
    for row in files:
        p = row["path"]
        if p in sub_paths:
            row["kind"], row["binary"], row["patch"] = "submodule", True, ""
        elif row["binary"]:
            row["ident_base"] = (None if row["status"] == "added"
                                 else _side_ident(repo, a, p))
            row["ident_head"] = (None if row["status"] == "deleted"
                                 else _side_ident(repo, b, p))
            row["same_content"] = _same_content(repo, row["ident_base"],
                                                row["ident_head"])
        if row["kind"] == "manifest":
            row["fields"] = toml_field_delta(man_a, man_b)

    pairs, ambiguous = _pair_files(number, base, head, man_a, man_b, files,
                                   size_a, size_b)
    for i, pr in enumerate(pairs):
        for side, other in (("base_index", "head"), ("head_index", "base")):
            idx = pr[side]
            if idx is not None:
                files[idx]["pair"] = i
                files[idx]["paired_with"] = (pr[other] or {}).get("path")

    return {
        "number": number,
        "base": {"ref": base, "commit": a, "version": man_a.get("version", ""),
                 "state": man_a.get("state", ""),
                 "maturity": man_a.get("maturity", "")},
        "head": {"ref": head, "commit": b, "version": man_b.get("version", ""),
                 "state": man_b.get("state", ""),
                 "maturity": man_b.get("maturity", "")},
        "stats": {"files_changed": len(stats), "additions": adds,
                  "deletions": dels, "files_reported": len(files),
                  "truncated": len(stats) > len(files)},
        "files": files,
        "pairs": pairs,
        "pairs_ambiguous": ambiguous,
        "meshes": _mesh_choices(number, base, head, man_a, man_b, files, pairs,
                                size_a, size_b),
        "manifest_delta": _manifest_delta(man_a, man_b),
        "submodules": subs,
        "mesh": {"base": _mesh_ref(number, base, man_a),
                 "head": _mesh_ref(number, head, man_b)},
        "commits": [] if a == b else _range_commits(repo, a, b),
    }


def _side_ident(repo: Path, commit: str, path: str) -> dict | None:
    """What one side of a binary row IS to git: the blob, and — when that blob
    is a Git LFS pointer — the object it names. Two cheap plumbing calls; no
    content is read above the pointer size."""
    spec = f"{commit}:{path}"
    blob = git(repo, "rev-parse", "--verify", "--quiet", spec, check=False).strip()
    if not blob:
        return None
    raw = git(repo, "cat-file", "-s", spec, check=False).strip()
    size = int(raw) if raw.isdigit() else None
    out: dict = {"blob": blob, "size": size}
    if size is not None and size <= _lfs().POINTER_MAX:
        ptr = _lfs().blob_pointer(repo, spec, size)
        if ptr:
            out["lfs"] = {"oid": ptr[0], "size": ptr[1]}
    return out


#: A raw side bigger than this is not hashed to compare it with an LFS oid.
SAME_CONTENT_HASH_MAX = 64 * 1024 * 1024


def _same_content(repo: Path, x: dict | None, y: dict | None) -> bool | None:
    """True when both sides are the same BYTES even though git's blobs differ —
    the case that matters is a file moved into LFS (raw blob → pointer, same
    content) or a pointer rewritten with the same oid. None = not knowable
    cheaply (one side missing, or a raw side too big to hash)."""
    if not x or not y:
        return None
    if x["blob"] == y["blob"]:
        return True
    lx, ly = x.get("lfs"), y.get("lfs")
    if lx and ly:
        return lx["oid"] == ly["oid"]
    if not lx and not ly:
        return False
    raw, ptr = (y, lx) if lx else (x, ly)
    if raw.get("size") is None:
        return None
    if raw["size"] != ptr["size"]:
        return False
    if raw["size"] > SAME_CONTENT_HASH_MAX:
        return None
    import hashlib
    data = git(repo, "cat-file", "blob", raw["blob"], binary=True, check=False)
    return hashlib.sha256(data or b"").hexdigest() == ptr["oid"]


def _flat(v, key: str, out: dict) -> None:
    if isinstance(v, dict):
        if not v and key:
            out[key] = {}
        for k in v:
            _flat(v[k], f"{key}.{k}" if key else str(k), out)
    elif isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v):
        for i, x in enumerate(v):
            _flat(x, f"{key}[{i}]", out)
    else:
        out[key] = v if isinstance(v, (str, int, float, bool, list)) or v is None \
            else str(v)


def toml_field_delta(a: dict, b: dict) -> list[dict]:
    """plm.toml's changed keys as flat paths, in the order they first appear:
    `[{key, from, to, op}]` with op added | removed | changed. A list of tables
    (`[[bom]]`) is indexed (`bom[20].qty`); a list of scalars is one value.
    This is the WORDS of the change — what the `manifestdelta` reading groups
    by meaning, flat, for the Fields form."""
    fa, fb = {}, {}
    _flat(a or {}, "", fa)
    _flat(b or {}, "", fb)
    out = []
    for k in list(fa) + [k for k in fb if k not in fa]:
        x, y = fa.get(k, _MISSING), fb.get(k, _MISSING)
        if x == y:
            continue
        op = ("added" if x is _MISSING else "removed" if y is _MISSING
              else "changed")
        out.append({"key": k, "op": op,
                    "from": None if x is _MISSING else x,
                    "to": None if y is _MISSING else y})
    return out


_MISSING = object()


def _range_commits(repo: Path, a: str, b: str) -> list[dict]:
    raw = git(repo, "log", f"--format=%H{US}%s{US}%an{US}%cI", f"{a}..{b}",
              check=False).strip()
    out = []
    for line in raw.splitlines():
        f = (line.split(US) + [""] * 4)[:4]
        out.append({"sha": f[0], "short": f[0][:10], "subject": f[1],
                    "author": f[2], "date": f[3]})
    return out


def known_repos() -> list[str]:
    if not REPOS.is_dir():
        return []
    return sorted(p.stem for p in REPOS.glob("*.git"))


# -- drafts: a branch, read as a PROPOSAL (stage 4) ----------------------------
#
# The Drafts tab and the Approvals queue both ask the same three questions about
# a `draft/*` branch — how far is it from `main`, what does it change, and is
# there anything to decide — and neither of them wants the full `compare()`
# payload (patches, meshes, pairing) to answer them. So: one cheap summary per
# branch, from three plumbing calls, and the compare view stays the place you go
# for the detail.

MODE_GITLINK = "160000"


def _raw_modes(repo: Path, a: str, b: str) -> list[dict]:
    """`git diff --raw`: the file MODES, which is the only way to tell a
    submodule pin bump from an ordinary file change without re-reading both
    trees. Format per record: `:<old> <new> <sha> <sha> <status>\\0<path>\\0`."""
    raw = git(repo, "diff", "--no-renames", "--raw", "-z", a, b, check=False)
    fields = [x for x in raw.split("\0") if x != ""]
    out, i = [], 0
    while i + 1 < len(fields):
        bits = fields[i].lstrip(":").split()
        if len(bits) >= 5:
            out.append({"old_mode": bits[0], "new_mode": bits[1],
                        "status": bits[4][0], "path": fields[i + 1]})
        i += 2
    return out


def range_stats(repo: Path, a: str, b: str) -> dict:
    """What changed between two commits, in the numbers a review row shows.

    `pins` counts gitlink rows separately from `files_changed`: a BOM pin bump
    is not a file edit, and a row that reported "1 file changed, +0 −0" for a
    child revision bump would be telling the truth in a way nobody can read.
    """
    rows = _raw_modes(repo, a, b)
    pins = [r for r in rows if MODE_GITLINK in (r["old_mode"], r["new_mode"])]
    pin_paths = {r["path"] for r in pins}
    adds = dels = 0
    changed = 0
    for st in _numstat(repo, a, b):
        if st["path"] in pin_paths:
            continue
        changed += 1
        adds += st["additions"]
        dels += st["deletions"]
    return {"files_changed": changed, "additions": adds, "deletions": dels,
            "pins_changed": len(pins),
            "pins": [{"path": r["path"], "status": r["status"].lower(),
                      "child": r["path"].split("/", 1)[1]
                      if r["path"].startswith("parts/") else ""}
                     for r in pins]}


def _ahead_behind(repo: Path, base: str, head: str) -> dict:
    """`git rev-list --left-right --count base...head` — GitHub's "N commits
    ahead, M behind", which is the one number that says whether a draft has been
    left behind by the releases that happened under it."""
    raw = git(repo, "rev-list", "--left-right", "--count", f"{base}...{head}",
              check=False).split()
    if len(raw) != 2:
        return {"ahead": None, "behind": None}
    return {"behind": int(raw[0]), "ahead": int(raw[1])}


def pins_at(number: str, ref: str) -> list[dict]:
    """Every `parts/*` gitlink at a ref, with whether the pin is a release.

    This is §5.1 made visible: an approver at RELEASED is refused while any of
    these is false, and `plm/gitindex.release_gate` asks git exactly this
    question at the moment of the click. Showing it BEFORE the click is the
    whole point — a rule you only meet as a refusal is a trap.
    """
    repo = repo_path(number)
    commit = resolve(repo, ref)
    out = []
    raw = git(repo, "ls-tree", "-r", "-t", "-z", commit, check=False)
    for rec in raw.split("\0"):
        if not rec.strip():
            continue
        meta, _, path = rec.partition("\t")
        bits = meta.split()
        if len(bits) < 3 or bits[0] != MODE_GITLINK:
            continue
        child = path.split("/", 1)[1] if path.startswith("parts/") else path
        out.append({"path": path, "child": child, **pin_label(child, bits[2])})
    out.sort(key=lambda p: p["path"])
    return out


def drafts(number: str) -> dict:
    """Every `draft/*` branch as a proposal row (no database — see gitapi).

    `base` is what "what does it change?" diffs against: the newest release, or
    on an item that has never released anything (59 of them) the draft before
    this one. `ahead`/`behind` are counted against `main` where there is one,
    because "3 commits ahead of main" and "what changed since the last release"
    are two different questions and a row that conflates them is lying about one.
    """
    info = refs(number)
    repo = repo_path(number)
    main = "main" if git(repo, "rev-parse", "--verify", "--quiet",
                         "refs/heads/main", check=False).strip() else ""
    rows = []
    for i, d in enumerate(info["drafts"]):
        base = info["default_release"] or (
            info["drafts"][i + 1]["ref"] if i + 1 < len(info["drafts"]) else "")
        row = {**d, "base": base, "main": main,
               "ahead": None, "behind": None, "stats": None,
               "author": "", "committed": d.get("date") or ""}
        log = git(repo, "log", "-1", f"--format=%an{US}%aI{US}%cI",
                  d["commit"], check=False).strip()
        if log:
            f = (log.split(US) + ["", "", ""])[:3]
            row.update({"author": f[0], "authored": f[1], "committed": f[2]})
        cnt = main or base
        if cnt:
            row.update(_ahead_behind(repo, cnt, d["commit"]))
            row["counted_against"] = cnt
        if base:
            try:
                row["stats"] = range_stats(repo, resolve(repo, base), d["commit"])
            except RepoError:
                row["stats"] = None
        rows.append(row)
    return {"number": number, "clone_url": info["clone_url"],
            "default_ref": info["default_ref"],
            "default_release": info["default_release"],
            "main": main, "drafts": rows,
            "counts": info["counts"]}
