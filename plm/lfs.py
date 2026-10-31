"""Git LFS for plm — the pointer, the object store, and the policy (stage 17).

docs/PLM_GIT_FIRST.md §6 and docs/LARGE-FILES.md. "Clone a part, get no mesh"
was the POC's most surprising punt; LFS is the standard answer, and plm only has
to speak the batch API (`plm-git/plm_git/lfs.py`). Everything else is here, in
one module both containers import (stdlib + `plm.config` only — the git service
must not open the database, and the receive hook runs this):

  * THE POINTER. `version https://git-lfs.github.com/spec/v1` + `oid sha256:…`
    + `size …`, under 1 KiB. A tree blob that parses as one IS one;
    `parse_pointer` is the only reader and `pointer` the only writer.

  * THE STORE. Content-addressed by sha256, `objects/<aa>/<bb>/<oid>` under
    `config.LFS_ROOT` — git-lfs's own local layout, so an operator recognises
    it. An object is written to `tmp/`, hashed while it streams, and renamed
    into place only if the hash and size are what was promised: a partial or
    lying upload never becomes an object. Dedupe across items is free (same
    bytes, same name), which is also why nothing here is per-repo.

    WHY NOT AN EXTERNAL OBJECT STORE: the framework's rule is that design
    data is SERVICE data, backed up by the service's own sidecar. Local disk +
    the sidecar's DATA_TREES is that rule. This store is plm's ONE object
    store.

  * THE POLICY. `wants_lfs(path, size)`: meshes are LFS at ANY size (a
    `.gitattributes` pattern cannot say "above 1 MiB", and a mesh that is a
    pointer in one commit and a blob in the next makes `git status` lie in an
    LFS clone); sources, manifests and docs are NEVER LFS; anything else above
    `config.LFS_THRESHOLD` is. `gitattributes()` writes that down for a tree.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Iterator

from . import config

SPEC = "https://git-lfs.github.com/spec/v1"
#: A pointer file is ~130 bytes; the spec caps it at 1024. A blob bigger than
#: this is never read to ask "is this a pointer?".
POINTER_MAX = 1024
OID_RE = re.compile(r"^[0-9a-f]{64}$")
_PTR_RE = re.compile(
    rb"^version https://git-lfs\.github\.com/spec/v1\n"
    rb"(?:[a-z0-9.-]+ [^\n]*\n)*$")


class LfsError(RuntimeError):
    pass


# -- the pointer --------------------------------------------------------------

def parse_pointer(data: bytes | None) -> tuple[str, int] | None:
    """`(oid, size)` when `data` is an LFS pointer, else None."""
    if not data or len(data) > POINTER_MAX or not _PTR_RE.match(data):
        return None
    oid = size = None
    for line in data.decode("ascii", errors="replace").splitlines()[1:]:
        k, _, v = line.partition(" ")
        if k == "oid" and v.startswith("sha256:"):
            oid = v[len("sha256:"):]
        elif k == "size" and v.isdigit():
            size = int(v)
    if oid and OID_RE.match(oid) and size is not None:
        return oid, size
    return None


def pointer(oid: str, size: int) -> bytes:
    """The canonical pointer text — byte-identical to `git lfs pointer`'s, so a
    tree plm writes and the tree a client's clean filter writes are the same
    tree."""
    return (f"version {SPEC}\noid sha256:{check_oid(oid)}\nsize {int(size)}\n"
            ).encode()


def check_oid(oid: str) -> str:
    if not isinstance(oid, str) or not OID_RE.match(oid):
        raise LfsError(f"not an LFS object id (64 lowercase hex): {oid!r}")
    return oid


# -- the store ----------------------------------------------------------------

def root() -> Path:
    return Path(os.environ.get("PLM_LFS_ROOT") or config.LFS_ROOT)


def object_path(oid: str) -> Path:
    oid = check_oid(oid)
    return root() / "objects" / oid[:2] / oid[2:4] / oid


def has(oid: str, size: int | None = None) -> bool:
    try:
        st = object_path(oid).stat()
    except (OSError, LfsError):
        return False
    return size is None or st.st_size == int(size)


def size_of(oid: str) -> int | None:
    try:
        return object_path(oid).stat().st_size
    except (OSError, LfsError):
        return None


def stream(oid: str, chunk: int = 1 << 16) -> Iterator[bytes]:
    p = object_path(oid)
    if not p.is_file():
        raise LfsError(f"LFS object {oid[:12]}… is not in plm's store")
    with p.open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                return
            yield b


def read(oid: str, max_bytes: int | None = None) -> bytes:
    p = object_path(oid)
    if not p.is_file():
        raise LfsError(f"LFS object {oid[:12]}… is not in plm's store")
    if max_bytes is not None and p.stat().st_size > max_bytes:
        raise LfsError(f"LFS object {oid[:12]}… is {p.stat().st_size:,} bytes, "
                       f"over the {max_bytes:,}-byte ceiling for reading it "
                       "into memory")
    return p.read_bytes()


def put_stream(chunks: Iterable[bytes], *, oid: str | None = None,
               size: int | None = None) -> tuple[str, int]:
    """Store bytes; returns `(oid, size)`. With `oid`/`size` given, a mismatch
    is refused and nothing is kept — the content address is the promise."""
    if oid is not None:
        check_oid(oid)
    tmpdir = root() / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    digest, n = hashlib.sha256(), 0
    fd, tmpname = tempfile.mkstemp(dir=tmpdir, prefix="in-")
    tmp = Path(tmpname)
    try:
        with os.fdopen(fd, "wb") as fh:
            for b in chunks:
                if not b:
                    continue
                n += len(b)
                if n > config.LFS_MAX_OBJECT:
                    raise LfsError(
                        f"object is over the {config.LFS_MAX_OBJECT:,}-byte "
                        "limit (PLM_LFS_MAX_OBJECT_BYTES)")
                digest.update(b)
                fh.write(b)
            fh.flush()
            os.fsync(fh.fileno())
        got = digest.hexdigest()
        if oid is not None and got != oid:
            raise LfsError(f"the bytes hash to {got}, not to the promised "
                           f"{oid} — nothing was stored")
        if size is not None and n != int(size):
            raise LfsError(f"{n:,} bytes arrived, {int(size):,} were promised "
                           "— nothing was stored")
        dest = object_path(got)
        if dest.is_file() and dest.stat().st_size == n:
            return got, n                         # dedupe: already there
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        return got, n
    finally:
        if tmp.exists():
            tmp.unlink()


def put_bytes(data: bytes) -> tuple[str, int]:
    return put_stream([data])


def put_file(path: Path) -> tuple[str, int]:
    def chunks():
        with Path(path).open("rb") as fh:
            while True:
                b = fh.read(1 << 20)
                if not b:
                    return
                yield b
    return put_stream(chunks())


def adopt(path: Path, oid: str, size: int) -> tuple[str, int]:
    """Take a file whose sha256 and size the caller ALREADY measured (an
    arrival's Sink) into the store without reading it again: a hard link into
    `objects/aa/bb/<oid>` (the source path stays valid — the inbox removes its
    own copy later). Where a link is not possible the file is renamed into
    place, and across filesystems (EXDEV) it is copied. The size is checked
    against a stat; the hash is the caller's promise. Use put_stream when
    there is no verified oid."""
    oid, path = check_oid(oid), Path(path)
    if path.stat().st_size != int(size):
        raise LfsError(f"{path.name} is {path.stat().st_size:,} bytes, "
                       f"{int(size):,} were measured — nothing was stored")
    if has(oid, size):
        return oid, int(size)                     # dedupe: already there
    dest = object_path(oid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = root() / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp = tmpdir / f"adopt-{os.urandom(6).hex()}"
    try:
        try:
            os.link(path, tmp)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                os.replace(path, tmp)
            else:
                shutil.copyfile(path, tmp)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return oid, int(size)


# -- the policy ---------------------------------------------------------------

#: LFS at ANY size, by pattern in `.gitattributes`.
MESH_EXT = (".stl", ".3mf", ".obj", ".ply", ".off", ".amf", ".step", ".stp",
            ".f3d", ".glb")
#: NEVER LFS: the manifest, the docs, the sources — the files git diffs are for.
PLAIN_NAMES = frozenset({"plm.toml", "README.md", "INSTRUCTIONS.md",
                         ".gitattributes", ".gitmodules", ".gitignore",
                         "item.toml"})
PLAIN_EXT = frozenset({
    ".scad", ".py", ".md", ".txt", ".toml", ".json", ".yaml", ".yml", ".csv",
    ".svg", ".dxf", ".ini", ".cfg", ".c", ".h", ".cpp", ".hpp", ".ino", ".js",
    ".ts", ".sh", ".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_sym",
    ".kicad_mod", ".net", ".html", ".css", ".xml", ".fcmacro", ".tsv"})


def threshold() -> int:
    return int(os.environ.get("PLM_LFS_THRESHOLD_BYTES")
               or config.LFS_THRESHOLD)


def is_mesh(path: str) -> bool:
    return Path(path).suffix.lower() in MESH_EXT


def always_plain(path: str) -> bool:
    p = Path(path)
    return p.name in PLAIN_NAMES or p.suffix.lower() in PLAIN_EXT


def wants_lfs(path: str, size: int) -> bool:
    """Should this file be an LFS pointer in a plm tree?"""
    if always_plain(path):
        return False
    return is_mesh(path) or int(size) > threshold()


_ATTR_HEAD = b"""# plm: meshes are Git LFS objects, at any size (docs/LARGE-FILES.md).
# Sources, plm.toml and docs are plain text and stay that way. Binary CAD
# does not merge (docs/PLM_GIT_FIRST.md \xc2\xa75.3): a conflict means "rebase
# and regenerate". GIT_LFS_SKIP_SMUDGE=1 clones the text only.
"""
_LFS_ATTRS = "filter=lfs diff=lfs merge=binary -text"


def _attr_path(path: str) -> str:
    # gitattributes has no quoting; git-lfs writes a space as [[:space:]]
    return re.sub(r"\s", "[[:space:]]", path.lstrip("/"))


def gitattributes(extra: Iterable[str] = ()) -> bytes:
    """`.gitattributes` for a plm tree: every mesh pattern, plus one line per
    NON-mesh file the policy put in LFS (`extra`, the over-threshold images
    and binaries — a pattern would catch the small ones too)."""
    lines = []
    for ext in MESH_EXT:
        lines.append(f"*{ext} {_LFS_ATTRS}")
        lines.append(f"*{ext.upper()} {_LFS_ATTRS}")
    lines += [f"*{e} merge=binary -text" for e in (".png", ".jpg", ".pdf")]
    lines.append("plm.toml text eol=lf")
    for p in sorted(set(extra)):
        if not is_mesh(p):
            lines.append(f"/{_attr_path(p)} {_LFS_ATTRS}")
    return _ATTR_HEAD + ("\n".join(lines) + "\n").encode()


def tracks(attrs: bytes | str | None, path: str) -> bool:
    """Does this `.gitattributes` text put `path` through the LFS filter? A
    small, sufficient matcher for the lines plm writes and `git lfs track`
    writes (`*.ext`, `/path`, `path`), not a gitattributes engine."""
    if not attrs:
        return False
    text = attrs.decode(errors="replace") if isinstance(attrs, bytes) else attrs
    name = Path(path).name
    import fnmatch
    hit = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pat, _, rest = line.partition(" ")
        pat = pat.replace("[[:space:]]", " ")
        if pat.startswith("/"):
            m = fnmatch.fnmatchcase(path, pat[1:])
        elif "/" in pat:
            m = fnmatch.fnmatchcase(path, pat)
        else:
            m = fnmatch.fnmatchcase(name, pat)
        if m:
            if "filter=lfs" in rest:
                hit = True
            elif "-filter" in rest or "!filter" in rest:
                hit = False
    return hit


# -- reading a git blob that may be a pointer ---------------------------------

def blob_pointer(repo: Path, spec: str, size: int | None) -> tuple[str, int] | None:
    """`(oid, size)` if the blob at `spec` (`<commit>:<path>` or a blob sha) is
    an LFS pointer. Reads nothing above POINTER_MAX."""
    if size is None or size > POINTER_MAX:
        return None
    from . import safegit
    try:
        data = safegit.git(repo, "cat-file", "blob", spec, binary=True,
                           max_bytes=POINTER_MAX + 1)
    except safegit.SafeError:
        return None
    return parse_pointer(data)


def smudge_dir(top: Path) -> dict:
    """Replace every pointer file under `top` with its object — what a
    `git archive` extraction needs before OpenSCAD can `import()` an LFS mesh.
    Missing objects are left as pointers and counted."""
    out = {"smudged": 0, "missing": []}
    for p in Path(top).rglob("*"):
        try:
            if not p.is_file() or p.is_symlink() or p.stat().st_size > POINTER_MAX:
                continue
            ptr = parse_pointer(p.read_bytes())
        except OSError:
            continue
        if not ptr:
            continue
        src = object_path(ptr[0])
        if not src.is_file():
            out["missing"].append(str(p.relative_to(top)))
            continue
        shutil.copyfile(src, p)
        out["smudged"] += 1
    return out


def store_stats() -> dict:
    n = b = 0
    objs = root() / "objects"
    if objs.is_dir():
        for p in objs.rglob("*"):
            if p.is_file():
                n += 1
                b += p.stat().st_size
    return {"root": str(root()), "objects": n, "bytes": b}


def fsck() -> dict:
    """Every stored object hashes to its own name (what the drill asks)."""
    from .storage import sha256_file
    bad, n = [], 0
    objs = root() / "objects"
    for p in sorted(objs.rglob("*")) if objs.is_dir() else []:
        if not p.is_file():
            continue
        n += 1
        if sha256_file(p)[0] != p.name:
            bad.append(p.name)
    return {"objects": n, "bad": bad}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python -m plm.lfs",
                                 description="plm's LFS object store")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--fsck", action="store_true",
                    help="re-hash every object against its name")
    a = ap.parse_args(argv)
    rc = 0
    if a.fsck:
        r = fsck()
        print(f"lfs fsck: {r['objects']} objects, {len(r['bad'])} bad"
              + (f": {r['bad'][:5]}" if r["bad"] else ""))
        rc = 1 if r["bad"] else 0
    if a.stats or not a.fsck:
        print(json.dumps(store_stats(), indent=2))
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
