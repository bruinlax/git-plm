"""Links from a plm payload into the git-first surfaces (clone, browse, compare).

One module, one job: given an item number, say WHERE the part lives — the repo
you can clone, the page a human opens, and the "what changed" link that compares
the previous release with the latest. The MCP calls this so a `get_item` answer
carries the git block; nothing else in plm depends on it.

Why it is its own file and not three f-strings in the MCP: the route shape had
to be in ONE place. Since stage 22 that place is plm/webroutes.py (real paths,
GitHub's shape); `browse_url` and `compare_url` below prefix it with WEB_BASE.

Read-only, no database, no writes. `git_block` shells out through
plm/gitrepo.py's `refs()` (a couple of git calls); `git_brief` is one stat, for
list results where a full `refs()` per row would not be cheap.
"""
from __future__ import annotations

import os
from . import config, gitrepo, webroutes

# Where the web UI is, for the links handed to agents and humans. Host identity
# comes from the environment like every other address in this codebase;
# PLM_PUBLIC_URL is the existing knob and is used when nothing more specific is
# set, so a deployment that already configured its public URL configures this.
WEB_BASE = (os.environ.get("PLM_WEB_BASE")
            or os.environ.get("PLM_PUBLIC_URL")
            or config.PUBLIC_URL).rstrip("/")


def browse_url(number: str, ref: str = "") -> str:
    """The item's repo page in the web UI, at a ref when one is known.

    Real paths since stage 22 (plm/webroutes.py): `/<N>` and
    `/<N>/tree/<ref>` — a ref's own slash (`draft/v0.17.0`) stays literal."""
    if ref:
        return f"{WEB_BASE}{webroutes.tree(number, ref)}"
    return f"{WEB_BASE}{webroutes.item(number)}"


def compare_url(number: str, base: str, head: str) -> str:
    """The compare view: base…head, the same pair `/api/repo/{n}/compare` takes."""
    return f"{WEB_BASE}{webroutes.compare(number, base, head)}"


def git_brief(number: str) -> dict | None:
    """Clone + browse links only — one stat, safe to call per row in a list.

    None when this item has no repo (`plm.split` has not run for it, or it is a
    purchased/hardware item, which lives in `catalog.git` rather than owning a
    repo of its own).
    """
    try:
        gitrepo.repo_path(number)
    except gitrepo.RepoError:
        return None
    return {"clone_url": gitrepo.clone_url(number),
            "browse_url": browse_url(number)}


def git_block(number: str) -> dict | None:
    """The full git block for one item, as `get_item` returns it.

    `default_ref` is what a bare clone checks out and is NOT always `main` —
    items with no release have no `main` at all. `compare_url` is the previous
    release against the latest, and is None when there are fewer than two
    releases to compare.
    """
    try:
        r = gitrepo.refs(number)
    except gitrepo.RepoError:
        return None
    releases = r["releases"]
    latest = r["default_release"]
    previous = releases[1]["ref"] if len(releases) > 1 else ""
    return {
        "clone_url": r["clone_url"],
        "default_ref": r["default_ref"],
        "default_release": latest or None,
        "releases_count": r["counts"]["releases"],
        "drafts_count": r["counts"]["drafts"],
        "browse_url": browse_url(number, latest or r["default_ref"]),
        "compare_url": compare_url(number, previous, latest) if previous else None,
        # Stage 3: the drafts an agent can actually act on. A git-origin DRAFT
        # is a branch, and the two things you want next are its name (to clone
        # and push to) and the compare against the release it would supersede.
        "drafts": _drafts(number, r, latest),
        # stage 17: does a clone of the default ref need git-lfs to have its
        # meshes? (docs/LARGE-FILES.md) — and the fast text-only alternative
        "lfs": lfs_summary(number, r["default_ref"]),
    }


def lfs_summary(number: str, ref: str) -> dict:
    """`{needs_git_lfs, files, bytes, say}` for one ref's tree: the LFS
    pointers in it (one `ls-tree` + one `cat-file --batch`)."""
    from . import gitvalidate, lfs
    out = {"needs_git_lfs": False, "files": 0, "bytes": 0}
    try:
        repo = gitrepo.repo_path(number)
        entries = gitvalidate.tree_entries(repo, ref)
        small = [e for e in entries.values()
                 if e.mode in (gitvalidate.MODE_BLOB, gitvalidate.MODE_EXEC)
                 and e.size <= lfs.POINTER_MAX and not lfs.always_plain(e.path)]
        body = gitvalidate._cat_batch(repo, [e.sha for e in small])
    except Exception:                                          # noqa: BLE001
        return {**out, "say": "could not read the tree"}
    ptrs = [p for p in (lfs.parse_pointer(body.get(e.sha)) for e in small) if p]
    out.update({"needs_git_lfs": bool(ptrs), "files": len(ptrs),
                "bytes": sum(sz for _, sz in ptrs)})
    out["say"] = (
        f"{len(ptrs)} file(s) at {ref} are Git LFS ({out['bytes']:,} bytes): a "
        "clone with git-lfs gets them (in the manufacturing workspace: "
        "tools/git/git.sh clone … — `tools/git/git.sh --check` first says "
        "whether the wrapper, the ws-git image and your token are ready); "
        "plain git gets the text and 130-byte pointers, which is normal"
        if ptrs else
        f"no Git LFS files at {ref}: plain git gets everything")
    return out


def _drafts(number: str, r: dict, latest: str) -> list[dict]:
    """The drafts an agent can act on, each with the thing to compare it against.

    "And what changed?" is the newest RELEASE for a draft — except on an item
    that has never released anything (59 of them), where it is the draft before
    this one, and on the oldest draft of such an item, where there is honestly
    nothing to compare and the answer is None rather than a link that diffs a
    ref against itself.
    """
    drafts = r.get("drafts") or []
    out = []
    for i, d in enumerate(drafts):
        base = latest or (drafts[i + 1]["ref"] if i + 1 < len(drafts) else "")
        out.append({
            "ref": d["ref"], "branch": d["ref"],
            "version": d.get("version") or "",
            "commit": d.get("commit") or "",
            "state": d.get("state") or "",
            "summary": d.get("summary") or "",
            "browse_url": browse_url(number, d["ref"]),
            "compare_url": (compare_url(number, base, d["ref"])
                            if base and base != d["ref"] else None),
            "push_url": gitrepo.clone_url(number),
        })
    return out
