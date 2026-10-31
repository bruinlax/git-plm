"""Swappable storage backends — plm/storage.py.

Every `files` row says where its bytes live via (backend, key):

  ''/'vault'  the git working tree (today's behavior). `path` is the
              DATA_ROOT-relative path; `key` is unused. All rows that
              existed before this module are implicitly vault.
  'lfs'       a Git LFS object in plm's content-addressed store (stage 17,
              plm/lfs.py): the tree holds the POINTER, `key` is the sha256,
              `path` is the tree path. In-tree for every purpose a manifest
              cares about — a clone with git-lfs gets the bytes. THE object
              store for new non-vault bytes (PLM_BLOBSTORE_DEFAULT=lfs).
  'git'       a blob in the item's own bare repo, because the revision ARRIVED
              as a commit (plm/gitindex.py). `key` is
              '<NUMBER>@<commit>:<path>' — a repo, an immutable commit and a
              path, which is the strongest promise any backend here makes:
              the bytes behind a released revision cannot change under it.
              Read-only by construction; new bytes get there by being
              committed, never by being PUT.

RETIRED_BACKENDS (`filedrop`, `local`) are refused wherever a row names one,
with the command that re-points it to 'lfs'.

Two operations, so callers never care which it is:

  choose_backend(name, role, size) -> where NEW bytes should go
  resolve(row) / local_copy(row)   -> get bytes back from any row

The default backend is PLM_BLOBSTORE_DEFAULT ('lfs' is the decision; = 'vault'
unless set, which makes this module a no-op: check-ins keep writing through
vault.write_checkin and nothing changes). Even when the default is flipped, three classes stay
PINNED to the vault, because the vault is where their value lives:

  - text/controlled files (extension list) — git diffs are the point,
  - 'source' files — regeneration provenance belongs in the repo,
  - 'viz' derivatives — cheap to keep beside the source that made them, and
    the thumbnailer renders them without a fetch. (It no longer *depends* on
    that: thumbs.py goes through local_copy, so an offloaded STL renders too.)
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterator

from . import config, roles

PIN_EXT = set(config.STORAGE_PIN_EXT)
# DERIVED FROM THE REGISTRY, not a list of its own. This was the third
# partial copy of the role vocabulary — the schema comment said
# source|artifact|doc|drawing, prod held `viz`, fab.py referenced `flat`, and
# this set knew about two of them. A role that must never be offloaded now
# declares `pin_vault` in roles.ROLES and every consumer agrees by construction.
PIN_ROLES = roles.PIN_ROLES


def choose_backend(name: str, role: str = "", size: int = 0) -> str:
    """Where NEW bytes go: 'vault', or PLM_BLOBSTORE_DEFAULT ('lfs' — plm's
    own content-addressed store). Pinned classes always stay in the vault."""
    if config.STORAGE_DEFAULT in ("", "vault"):
        return "vault"
    ext = Path(name).suffix.lower()
    if ext in PIN_EXT or role in PIN_ROLES:
        return "vault"
    if size < config.STORAGE_MIN_SIZE:
        return "vault"
    return config.STORAGE_DEFAULT


class StorageError(RuntimeError):
    pass


def sha256_file(path: Path) -> tuple[str, int]:
    """(sha256, size) of a local file, read a megabyte at a time — the one
    way plm hashes bytes already on its disk (vault, carry, lfs.fsck). Bytes
    ARRIVING are hashed as they stream, by staging.Sink."""
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _lfs_put(write) -> tuple[str, int]:
    """Run one `lfs` write; its refusals become StorageError."""
    from . import lfs
    try:
        return write(lfs)
    except lfs.LfsError as exc:
        raise StorageError(str(exc)) from None


def _lfs_row(stored: tuple[str, int], name: str, role: str) -> dict:
    """The files-row record for bytes now in plm's LFS store: the row
    gitindex writes for a pushed pointer — `key` is the oid, `path` the bare
    tree name (the tree is the revision; no `<number>/v<x>/` prefix)."""
    oid, size = stored
    return {"backend": "lfs", "key": oid, "path": name,
            "sha256": oid, "size": size, "role": role}


#: Backends plm no longer reads -> the command that re-points their rows to
#: 'lfs' by their bytes. resolve(), external_key() and readiness all say it.
_REPOINT = ("python -m plm.migrate_cleanup --apply --only filedrop-repoint "
            "[--project <p>]")
RETIRED_BACKENDS = {"filedrop": _REPOINT, "local": _REPOINT}

#: What every file link says about who can open it (api + MCP file_link).
LINK_NOTE = ("plm serves this link itself: it opens for anyone who can reach "
             "this plm instance, and for nobody else. To send the file further, "
             "download the bytes and share them another way.")


def _unknown(backend: str) -> str:
    if backend in RETIRED_BACKENDS:
        return (f"backend {backend!r} is retired — run "
                f"`{RETIRED_BACKENDS[backend]}`")
    return f"unknown storage backend: {backend}"


def _offload_backend() -> None:
    if config.STORAGE_DEFAULT != "lfs":
        raise StorageError(_unknown(config.STORAGE_DEFAULT))


def put(data: bytes, *, number: str, version: str, name: str,
        role: str = "") -> dict:
    """Write bytes to the configured non-vault backend and return a files-row
    shaped record. Vault writes do NOT come through here — they go through
    vault.write_checkin so the git commit covers them."""
    _offload_backend()
    return _lfs_row(_lfs_put(lambda lfs: lfs.put_bytes(data)),
                    Path(name).name, role)


def put_file(path: Path, *, number: str, version: str, name: str,
             role: str = "", sha256: str = "", size: int | None = None
             ) -> dict:
    """Same as put(), but the bytes are a local file — a URL arrival, a raw
    PUT or a staged upload in plm's inbox (plm/staging.py). The ONE write
    path for bytes that arrived by reference.

    With `sha256` and `size` — the numbers the arrival's Sink measured — the
    file is ADOPTED into the store (a hard link, no copy, no second hash).
    Without them it is streamed in and hashed on the way. Which backend this
    is, and whether the file goes to a backend at all rather than the git
    vault, was already decided by choose_backend.
    """
    _offload_backend()
    name = Path(name).name  # no nested paths from clients
    if sha256 and size is not None:
        return _lfs_row(_lfs_put(lambda lfs: lfs.adopt(path, sha256, size)),
                        name, role)
    return _lfs_row(_lfs_put(lambda lfs: lfs.put_file(path)), name, role)


# =============================================================================
# H1 — A PUSHED MANIFEST MAY NOT CHOOSE THE SERVER'S BACKEND OR ITS KEY
# =============================================================================
#
# `[[files.external]]` is how a revision says "this file is not in the tree".
# Before stage 7 `gitindex._index_files` copied its `backend` and `key` into
# the `files` row VERBATIM, and `/api/repo/{n}/external` handed both to
# `resolve()` — so a push could name any object in a store plm held a
# credential for, and `backend="git"` with a `../` in the number segment
# reached any repo on the host.
#
# The rule: `backend` is an allowlist, and it is `vault` alone. `vault`
# carries no key — `resolve()` opens it through `vault.safe_path`, which
# resolve+prefix-checks against the vault root and is already bounded. `git`
# is refused outright: a git-backed row is what the TREE produces and plm
# builds that key itself. A retired backend is refused with why: a binary
# belongs in the tree, where plm keeps it in Git LFS.
#
# Both the push validator (`gitvalidate._check_files`) and the indexer
# (`gitindex.external_location`) call this one function.

EXTERNAL_BACKENDS = ("vault",)


def external_key(number: str, version: str, name: str, *, backend: str = "",
                 declared: str = "") -> tuple[str, str]:
    """`(backend, key)` for one `[[files.external]]` row. Raises StorageError.
    `number`, `version` and `declared` keep the callers' shape; a `vault` row
    has no key, so nothing in them is used."""
    backend = (backend or "").strip().lower() or "vault"
    if not Path(str(name or "")).name:
        raise StorageError(
            "an [[files.external]] row needs a `name`: it is what the file is "
            "called and what\n/api/repo/<n>/external addresses it by.")
    if backend in RETIRED_BACKENDS:
        raise StorageError(
            f"{_unknown(backend)}.\n  In a manifest: commit the file into the "
            "tree instead — a binary\n  is kept in Git LFS "
            "(docs/LARGE-FILES.md) — and drop this [[files.external]] row.")
    if backend not in EXTERNAL_BACKENDS:
        raise StorageError(
            f"`backend = {backend!r}` is not a storage backend a manifest may "
            "choose.\n"
            f"  allowed: {', '.join(EXTERNAL_BACKENDS)}\n"
            "\n"
            "  `git` in particular is refused here on purpose: a git-backed "
            "file is one that is\n  IN THE TREE, and plm builds that row and "
            "its key itself from the commit it indexed.\n"
            "  Declaring one by hand would let a manifest name any object in "
            "any repository on\n  the host.")
    return "vault", ""


def resolve(row: dict) -> tuple[str, object]:
    """('file', Path) for bytes on this host, ('stream', iterator) for a
    blob in a git repo — /api/blob turns either into a response."""
    backend = row.get("backend") or "vault"
    if backend == "vault":
        from . import vault
        return "file", vault.safe_path(row["path"])
    if backend == "git":
        return "stream", _git_stream(row["key"])
    if backend == "lfs":
        # stage 17: `key` is the object's sha256 — content-addressed, so like
        # `git` it can never change under the row that names it
        from . import lfs
        try:
            p = lfs.object_path(row["key"])
        except lfs.LfsError as exc:
            raise StorageError(str(exc)) from None
        if not p.is_file():
            raise StorageError(
                f"LFS object {row['key'][:12]}… is not in plm's store "
                f"({lfs.root()}) — the pointer was indexed, the bytes never "
                "arrived (`git lfs push --all origin` from a clone that has "
                "them)")
        return "file", p
    raise StorageError(_unknown(backend))


def _git_stream(key: str) -> Iterator[bytes]:
    """`git cat-file blob <commit>:<path>` out of the item's bare repo.

    Streamed rather than read whole: the bytes are usually a mesh. This is the one line that makes the viewer, the
    thumbnailer, /api/blob and the MCP work on a pushed revision with no special
    cases anywhere else — a files row is a files row.
    """
    from . import gitindex, safegit
    # `parse_key` gates all three parts (H1); `repo_dir` gates the join (L2).
    number, commit, path = gitindex.parse_key(key)
    repo = safegit.repo_dir(gitindex.REPOS, number)
    if not (repo / "HEAD").exists():
        raise StorageError(f"no repository for {number} (key {key})")
    # stage 17: a blob that is an LFS POINTER serves the object it names, so
    # a `git`-backed row made before the indexer knew about LFS still opens
    from . import lfs
    try:
        size = safegit.git(repo, "cat-file", "-s", f"{commit}:{path}").strip()
        ptr = lfs.blob_pointer(repo, f"{commit}:{path}",
                               int(size) if size.isdigit() else None)
        if ptr:
            yield from lfs.stream(ptr[0])
            return
        yield from safegit.stream(repo, "cat-file", "blob", f"{commit}:{path}")
    except (safegit.SafeError, lfs.LfsError) as exc:
        raise StorageError(f"git could not read {key}: {exc}") from None


#: Extensions `local_copy` will put on a cache filename. Everything the
#: thumbnailer, the checks runner and the viewer can open, and nothing else —
#: OpenSCAD's `import()` dispatches on the suffix, so this list is also the
#: answer to "what may reach a generated .scad".
CACHE_EXT = frozenset({
    ".stl", ".3mf", ".obj", ".ply", ".off", ".amf", ".scad", ".dxf", ".svg",
    ".step", ".stp", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf",
    ".md", ".txt", ".csv", ".json", ".toml", ".yaml", ".yml", ".gcode",
})


def local_copy(row: dict) -> Path:
    """A real local Path for any row — streams a git blob into a sha-keyed
    cache, and gives an LFS object the extension it needs. For code (thumbs, diff) that needs a file on disk."""
    kind, obj = resolve(row)
    if kind == "file" and (row.get("backend") or "") != "lfs":
        return obj  # type: ignore[return-value]
    cache = config.CACHE_DIR / "objcache"
    cache.mkdir(parents=True, exist_ok=True)
    # Keep the real extension: OpenSCAD's import() dispatches on suffix and
    # renders an EMPTY scene (no error) for an extensionless path.
    # ⛔ L8 — THE EXTENSION IS ALLOWLISTED. `Path.suffix` keeps everything after
    # the last dot, and for an `[[files.external]]` row `path` is the manifest's
    # `name` — so a committed name could put `"` and `)` into a filename that
    # `plm/thumbs.py` then interpolates into `import("{p}", convexity=5);` in a
    # generated `.scad`. The quoting held by accident (a `/` would have made the
    # cache write fail, which was the only thing stopping a second `import()`).
    # An extension plm does not recognise gets none, which is safe and makes the
    # render fail visibly rather than silently.
    ext = Path(row.get("path") or row.get("key") or "").suffix.lower()
    if ext not in CACHE_EXT:
        ext = ""
    dest = cache / ((row.get("sha256") or hashlib.sha256(
        row["key"].encode()).hexdigest()) + ext)
    if not dest.is_file():
        if kind == "file":
            # an LFS object is a local file with no extension (it is named by
            # its sha256): a symlink gives OpenSCAD and the viewer the suffix
            # they dispatch on without copying an 85 MB mesh
            tmp = dest.with_suffix(".lnk")
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            tmp.symlink_to(obj)
            tmp.replace(dest)
            return dest
        tmp = dest.with_suffix(".part")
        with tmp.open("wb") as fh:
            for chunk in obj:  # type: ignore[union-attr]
                fh.write(chunk)
        tmp.replace(dest)
    return dest
