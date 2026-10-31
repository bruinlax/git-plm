"""Is this commit a legal revision of this item? — asked once, answered once.

THE CHEAP, TEACHABLE "NO". In a git-first plm the repo is the record, so the
question "may these bytes become a revision?" has to be answered before the
bytes are kept. `git receive-pack` gives us exactly the right moment: the pushed
objects sit in a quarantine store (`GIT_QUARANTINE_PATH`) and no served ref has
moved, so a non-zero `pre-receive` makes git delete the whole push — a refusal
costs the pusher a message and nothing else.

ONE CODE PATH. The same questions have to be asked of a commit this
process is about to make itself (`plm/gitwrite.py`, when `set_attributes` or a
BOM edit arrives through the API or the MCP). So the checks live HERE, in a
module that:

  * imports nothing but the standard library and `plm.manifest`,
  * never opens the database — everything it needs is in the repo, and the git
    container must not be a second writer,
  * shells out to `git` against a `--git-dir`, so it works identically inside a
    receive-pack hook and inside the API process.

Every message is written to be read by whoever caused it, and says what to do
next. That tone is inherited from plm-git's August `Refused` and is the main
thing worth keeping from the translate-to-API write path it replaces.

`check(...)` returns a `Verdict`: refusals (fatal) and lint (warnings printed
but never blocking, §6c-1).
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import manifest, safegit

GIT = os.environ.get("PLM_GIT", "git")
ZERO = "0" * 40

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
DRAFT_REF_RE = re.compile(r"^refs/heads/draft/[A-Za-z0-9._/-]+$")

MODE_BLOB, MODE_EXEC, MODE_GITLINK, MODE_SYMLINK = (
    "100644", "100755", "160000", "120000")

# Size sanity, checked where saying no is free. Same names as plm-git's August
# knobs (PLMGIT_PUSH_MAX_*) so an operator who knew the old service still knows
# this one.
MAX_FILE_BYTES = int(os.environ.get("PLMGIT_PUSH_MAX_FILE_BYTES",
                                    str(64 * 1024 * 1024)))
MAX_TOTAL_BYTES = int(os.environ.get("PLMGIT_PUSH_MAX_TOTAL_BYTES",
                                     str(128 * 1024 * 1024)))
MAX_FILES = int(os.environ.get("PLMGIT_PUSH_MAX_FILES", "300"))

from . import config  # noqa: E402
REPOS = config.REPOS          # one home: plm/config.py


class Refused(Exception):
    """A refusal the author can act on: what is wrong, and the fix, in full.

    `needs_credential` decides 401-vs-403 at the HTTP door. It is a flag and not
    a phrase sniffed out of the message, because sniffing got it wrong once
    already: a "push is disabled here" message mentioning a tokens file was
    answered 401, and git went off asking for a password to a door that did not
    exist.
    """

    def __init__(self, msg: str, needs_credential: bool = False):
        super().__init__(msg)
        self.needs_credential = needs_credential


@dataclass
class Verdict:
    number: str
    commit: str
    version: str
    bump: str = ""
    previous: str = ""
    summary: str = ""
    body: str = ""
    manifest: dict = field(default_factory=dict)
    manifest_text: str = ""
    files: int = 0
    bytes: int = 0
    gitlinks: dict = field(default_factory=dict)   # path -> child commit
    lint: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)


# -- git plumbing -------------------------------------------------------------

def git(git_dir: Path | str, *args: str, check: bool = True,
        binary: bool = False, env: dict | None = None):
    """Inspect the repo being pushed to. `safegit.git` underneath, so the hook
    gets the same timeout and capture ceiling as everything else — a pre-receive
    that hangs is a push that never returns and a quarantine that never clears
    (M5)."""
    try:
        return safegit.git(git_dir, *args, check=check, binary=binary, env=env)
    except safegit.SafeError as exc:
        raise Refused(
            f"git plumbing failed inspecting this commit "
            f"({' '.join(args[:3])}): {exc}") from None


def _ok(git_dir: Path | str, *args: str) -> bool:
    return safegit.ok(git_dir, *args)


# `git receive-pack` exports the pushed objects' QUARANTINE through the
# environment (GIT_OBJECT_DIRECTORY + GIT_ALTERNATE_OBJECT_DIRECTORIES). That is
# exactly right for the repo being pushed to — it is how a hook can see objects
# no ref points at yet — and exactly wrong for any OTHER repo: object lookups in
# a child's repo would be answered out of the parent's quarantine, and every
# gitlink would come back "that commit does not exist here".
#
# Cost of learning this the hard way: one refused assembly push whose pin was
# perfectly good.
_QUARANTINE_VARS = ("GIT_DIR", "GIT_OBJECT_DIRECTORY", "GIT_INDEX_FILE",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_QUARANTINE_PATH",
                    "GIT_WORK_TREE", "GIT_NAMESPACE", "GIT_COMMON_DIR")


def _clean_env() -> dict:
    return {k: v for k, v in safegit.clean_env().items()
            if k not in _QUARANTINE_VARS}


def other(repo: Path | str, *args: str) -> tuple[int, str]:
    """Run git against a DIFFERENT repository than the one being pushed to."""
    import subprocess as _sp
    try:
        res = _sp.run([GIT, "--git-dir", str(repo), *args],
                      capture_output=True, env=_clean_env(),
                      timeout=safegit.TIMEOUT_S)
    except _sp.TimeoutExpired:
        return 128, ""
    return res.returncode, res.stdout.decode(errors="replace")


def other_ok(repo: Path | str, *args: str) -> bool:
    return other(repo, *args)[0] == 0


@dataclass
class Entry:
    path: str
    mode: str
    sha: str
    size: int

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


def tree_entries(git_dir: Path | str, rev: str) -> dict[str, Entry]:
    """Every entry in a commit's tree, keyed by path. `-l` gives blob sizes
    without reading the blobs; `-z` survives odd filenames; gitlinks come back
    with size `-`."""
    out = git(git_dir, "ls-tree", "-r", "-l", "-z", "--full-tree", rev)
    entries: dict[str, Entry] = {}
    for rec in out.split("\0"):
        if not rec.strip():
            continue
        meta, _, path = rec.partition("\t")
        bits = meta.split()
        if len(bits) < 4:
            continue
        mode, _type, sha, size = bits[:4]
        entries[path] = Entry(path=path, mode=mode, sha=sha,
                              size=int(size) if size.isdigit() else 0)
    return entries


def releases(git_dir: Path | str) -> dict[str, str]:
    """version -> commit, for every `v<x.y.z>` tag. Tags may be annotated, so
    `%(*objectname)` (the peeled target) is preferred over `%(objectname)`."""
    out: dict[str, str] = {}
    for line in git(git_dir, "for-each-ref", "refs/tags/",
                    "--format=%(refname:short) %(objectname) %(*objectname)"
                    ).splitlines():
        bits = line.split()
        if not bits:
            continue
        m = TAG_RE.match(bits[0])
        if m:
            out[bits[0][1:]] = (bits[2] if len(bits) > 2 else bits[1])
    return out


def draft_branches(git_dir: Path | str) -> dict[str, str]:
    """ref -> tip, for every `refs/heads/draft/*`."""
    out = {}
    for line in git(git_dir, "for-each-ref", "refs/heads/draft/",
                    "--format=%(refname) %(objectname)").splitlines():
        ref, _, sha = line.partition(" ")
        if ref:
            out[ref] = sha
    return out


def default_ref(git_dir: Path | str) -> str:
    """`main` where a release exists, else whatever HEAD points at. 59 items in
    this registry have never released anything , so a literal
    "main" would be a lie for a third of the repos."""
    if _ok(git_dir, "rev-parse", "--verify", "--quiet", "refs/heads/main"):
        return "refs/heads/main"
    head = git(git_dir, "symbolic-ref", "-q", "HEAD", check=False).strip()
    return head or "refs/heads/main"


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def newest_release(git_dir: Path | str) -> str:
    rels = releases(git_dir)
    return max(rels, key=_vkey) if rels else ""


def legal_bumps(latest: str) -> dict[str, str]:
    if not latest:
        return {"initial": "0.1.0"}
    a, b, c = _vkey(latest)
    return {"patch": f"{a}.{b}.{c + 1}", "minor": f"{a}.{b + 1}.0",
            "major": f"{a + 1}.0.0"}


def draft_versions(git_dir: Path | str) -> dict[str, str]:
    """ref -> the `version` its tip's `plm.toml` claims, for every open draft.

    An open draft has already taken a version out of the series even though no
    tag stands behind it yet, which is why the bump base below consults this.
    """
    out: dict[str, str] = {}
    for dref, tip in draft_branches(git_dir).items():
        doc, _ = _manifest_of(git_dir, tip)
        v = str((doc or {}).get("version") or "")
        if VERSION_RE.match(v):
            out[dref] = v
    return out


def bump_base(git_dir: Path | str, *, exclude: set[str] | None = None
              ) -> tuple[str, str]:
    """What a new version has to be a single bump over: (version, what it is).

    The newest RELEASE tag — and, when an item has never released anything, the
    newest OPEN DRAFT's version instead. 59 items in this registry are in that
    state , and under the release-only rule every legal next
    version for all of them was `0.1.0`, so the ones that already had a
    `draft/v0.2.0` could not be pushed to at all: the second draft of a part
    that has never shipped was unrepresentable.

    `exclude` is the refs that are THIS line of work (the ref being written and
    any draft this commit is built on). A draft is not a rival for the version
    it is itself advancing.
    """
    rels = releases(git_dir)
    if rels:
        return max(rels, key=_vkey), "release"
    others = [v for ref, v in draft_versions(git_dir).items()
              if ref not in (exclude or set())]
    if others:
        return max(others, key=_vkey), "draft"
    return "", ""


def bump_base_for(git_dir: Path | str, commit: str, ref: str, *,
                  strict: bool = False) -> tuple[str, str, str]:
    """THE bump base of a push (stage 16b, cold run 7): (version, kind, where).

    The newest version the item has — a release TAG or an open DRAFT's tip —
    that the pushed commit DESCENDS FROM. Run 7 branched off
    `origin/draft/v0.3.0` of LMB-PRT-0003 (drafts v0.1.0/0.2.0/0.3.0, nothing
    released) and pushed v0.3.1; the old rule set every draft the commit was
    built on aside as "this line of work", found no base, and offered only
    0.1.0 — a version that already existed. A commit built on draft/v0.3.0
    takes its next number from v0.3.0: 0.3.1 / 0.4.0 / 1.0.0.

    The ref being written is not its own base (its old tip is a re-push).
    `kind` is "release", "draft", "" (the repo has no version at all: 0.1.0),
    or "unrelated" — plm knows versions but the commit descends from none of
    them; `_check_version` refuses that, naming what it does know.

    `strict` (the REVIEW baseline, not the push door): a version whose tip IS
    `commit` is not a base of it. CHG-380's members were pushed to
    `draft/usb-correction` while `draft/v0.2.1` pointed at the same commit, so
    the inferred baseline was the head itself and the change page diffed every
    member against itself — "no diff" on a real fit correction."""
    cands: list[tuple[str, str, str]] = []
    for v, sha in releases(git_dir).items():
        if strict and sha == commit:
            continue
        if sha and _ok(git_dir, "merge-base", "--is-ancestor", sha, commit):
            cands.append((v, "release", f"v{v}"))
    tips = draft_branches(git_dir)
    for dref, v in draft_versions(git_dir).items():
        tip = tips.get(dref, "")
        if dref == ref or not tip or (strict and tip == commit):
            continue
        if _ok(git_dir, "merge-base", "--is-ancestor", tip, commit):
            cands.append((v, "draft", dref[len("refs/heads/"):]))
    if cands:
        return max(cands, key=lambda c: _vkey(c[0]))
    if releases(git_dir) or [d for d in draft_versions(git_dir) if d != ref]:
        return "", "unrelated", ""
    return "", "", ""


# -- the checks ---------------------------------------------------------------

def _load_manifest(git_dir: Path | str, commit: str, number: str) -> tuple[dict, str]:
    raw = git(git_dir, "cat-file", "blob", f"{commit}:plm.toml", check=False,
              binary=True)
    if not raw:
        raise Refused(
            "there is no `plm.toml` at the root of this tree, and plm.toml IS "
            "the revision:\nit carries the version, the file roles, the "
            "attributes and the BOM lines.\nWithout it a clone is a folder of "
            "files that nothing knows anything about.\n"
            "\n"
            "  get one the easy way — it is generated for every existing "
            "revision:\n"
            f"    git show main:plm.toml > plm.toml   # then edit the version\n")
    text = raw.decode(errors="replace")
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Refused(
            f"`plm.toml` does not parse as TOML: {exc}\n"
            "\n"
            "  check it locally before pushing — the same parser plm uses:\n"
            "    python3 -c 'import tomllib;tomllib.load(open(\"plm.toml\",\"rb\"))'"
        ) from None
    if doc.get("schema") != manifest.SCHEMA_VERSION:
        raise Refused(
            f"`plm.toml` says schema = {doc.get('schema')!r}; this plm speaks "
            f"schema {manifest.SCHEMA_VERSION}.\n"
            "The schema number is how a manifest written by a future plm is "
            "refused rather\nthan half-understood.\n"
            f"\n  put `schema = {manifest.SCHEMA_VERSION}` first in the file.")
    if doc.get("number") != number:
        raise Refused(
            f"`plm.toml` claims number = {doc.get('number')!r}, but this is the "
            f"repository for {number}.\n"
            "plm owns identity: a part number exists because plm allocated it, "
            "and a tree\nthat renames itself would give one design two numbers "
            "and one number two designs.\n"
            f"\n  fix the line:  number = \"{number}\"\n"
            "  (a genuinely new part is `create_item` in plm, which allocates "
            "the number\n   and creates its repo — never a push.)")
    # ⭐ LIFECYCLE FIELDS ARE PLM'S. `state`,
    # `maturity`, `author`, `date` and `summary` in a pushed `plm.toml` are NOT
    # refused and NOT believed: on a draft branch the indexer derives them (the
    # push is a DRAFT, the commit subject is the summary, the pusher is the
    # author) and the RELEASE commit writes the truth back into the file. Both
    # agents hand-edited `state = "DRAFT"`, deleted `maturity` and changed
    # `author` because this used to refuse a branch that still said what its
    # parent tag said — busywork about facts the server already knows, with no
    # safety in it: what makes `git push` mean "proposed" is that the indexer
    # never READS these on a draft (gitindex.index_commit), not that the file
    # agrees.
    _check_routing(doc)
    return doc, text


def _check_routing(doc: dict) -> None:
    """A routing is manufacturing work, and `plm/fab.py`'s registry is the one
    place that says which verbs exist and what units their parameters carry.
    Asked here so the pusher hears it while the objects are still in the
    quarantine, rather than from the indexer after the branch has landed."""
    ops_ = doc.get("routing") or []
    if not ops_:
        return
    try:
        from . import fab
    except Exception:  # pragma: no cover - the hook runs with plm importable
        return
    for o in ops_:
        try:
            fab._validate(str(o.get("op") or ""), dict(o.get("params") or {}))
        except Exception as exc:  # noqa: BLE001 — fab.FabError, reported as-is
            raise Refused(
                f"`[[routing]]` seq {o.get('seq')}: {exc}\n"
                "\n"
                "plm turns a routing into actionable work — a cut list, a nest, "
                "a build plan —\nso an operation it cannot read is work no shop "
                "surface will ever route.\n"
                "\n  the registered verbs and their parameters: GET "
                "/api/fab/registry") from None


def _my_line(git_dir: Path | str, commit: str, ref: str, old: str
             ) -> tuple[set[str], set[str]]:
    """(refs, versions) that are the SAME line of work as this push.

    A re-push of a draft, an amend, a `refs/for/main` submit of a branch you
    already pushed — all of them re-state a version that is already claimed, by
    you. The version series is about not colliding with somebody ELSE.
    """
    refs, versions = {ref}, set()
    if old and old != ZERO:
        doc, _ = _manifest_of(git_dir, old)
        v = str((doc or {}).get("version") or "")
        if v:
            versions.add(v)
    for dref, tip in draft_branches(git_dir).items():
        if dref != ref and not _ok(git_dir, "merge-base", "--is-ancestor",
                                   tip, commit):
            continue
        refs.add(dref)
        # ⚠️ only the ref being written, or a draft AT this very commit (the
        # `refs/for/main` submit of a branch already pushed), re-states ITS
        # version. A draft this commit merely descends from is the BASE of a
        # bump (bump_base_for), not a version this push may re-claim — before
        # stage 16b a commit built on draft/v0.3.0 could "update" v0.1.0.
        if dref != ref and tip != commit:
            continue
        doc, _ = _manifest_of(git_dir, tip)
        v = str((doc or {}).get("version") or "")
        if v:
            versions.add(v)
    return refs, versions


def _check_version(git_dir: Path | str, doc: dict, number: str, commit: str,
                   ref: str, old: str = "") -> tuple[str, str, str]:
    version = str(doc.get("version") or "")
    if not VERSION_RE.match(version):
        raise Refused(
            f"`version = {version!r}` is not a version. plm versions are "
            "`<major>.<minor>.<patch>`,\nwith no leading `v` (the TAG carries "
            "the v; the file carries the number).\n"
            "\n  e.g.  version = \"0.4.0\"")
    rels = releases(git_dir)
    mine_refs, mine_versions = _my_line(git_dir, commit, ref, old)
    latest, base_is, base_at = bump_base_for(git_dir, commit, ref)
    legal = legal_bumps(latest)
    if base_is == "unrelated" and version not in mine_versions:
        known = sorted(set(rels) | set(draft_versions(git_dir).values()),
                       key=_vkey)
        raise Refused(
            f"this commit descends from none of {number}'s versions, so there "
            "is nothing to bump\nfrom. plm knows "
            + ", ".join(f"v{v}" for v in known)
            + " — a new version is a single bump over the one\nyour work is "
            "built on.\n"
            "\n  start from one of them and push again:\n"
            "    git fetch origin && git rebase origin/main      # or "
            "origin/draft/<the draft you build on>")
    if version in rels:
        raise Refused(
            f"v{version} is already a release of {number} — tag "
            f"`v{version}` points at {rels[version][:10]}.\n"
            "A revision is immutable: whatever was approved under that number "
            "is a record,\nand this push would mint it a second time.\n"
            "\n"
            f"  the next free versions from v{latest} are\n"
            + "".join(f"    {v}   ({k})\n" for k, v in legal.items())
            + "\n  edit `version` in plm.toml, amend, and push again.")
    if (base_is == "draft" and version == latest
            and not ref.startswith("refs/heads/")):
        # `refs/for/main` built on draft/v<latest>, claiming <latest>: the
        # proposal ADVANCES that draft (it lands on draft/v<version>) — the
        # create_item checkout + one commit + `refs/for/main` flow. Only the
        # draft it is built on, never an older one (stage 16b).
        return version, "update", latest
    if version in mine_versions:
        # The same draft, advanced: a re-push, an amend, or the `refs/for/main`
        # submit of a branch that is already there. It re-states a version that
        # is already claimed — by this line of work, which is not a collision.
        return version, "update", latest
    bump = next((k for k, v in legal.items() if v == version), "")
    if not bump:
        raise Refused(
            f"v{version} is not a single bump over the version this commit is "
            f"built on: "
            + (f"v{latest} ({'release tag' if base_is == 'release' else 'open draft'}"
               f" `{base_at}`)" if latest else f"none — {number} has no "
               "version yet")
            + ".\n"
            "plm allocates versions and a repo is not allowed to skip one: a "
            "gap in the\nversion series is a revision nobody can find.\n"
            "\n  the three legal next versions are\n"
            "    " + f"{legal.get('patch', legal.get('initial'))}"
            "   (patch — geometry-equivalent / fit tweak)\n"
            + (f"    {legal['minor']}   (minor — a real design change)\n"
               f"    {legal['major']}   (major — not interchangeable with what "
               "came before)\n" if latest else "")
            + "\n  set one of those as `version` in plm.toml and amend the commit.")
    # Two drafts claiming one version is THE git-visible PLM conflict (§3).
    # `_my_line` has already set aside the drafts that ARE this line of work —
    # the ref being written and any draft this commit is built on — so that
    # `git push origin HEAD:refs/for/main` after `git push origin
    # HEAD:draft/<topic>` does not accuse the branch you just pushed.
    for dref, tip in draft_branches(git_dir).items():
        if dref in mine_refs or tip == commit:
            continue
        other, _ = _manifest_of(git_dir, tip)
        if other and str(other.get("version") or "") == version:
            raise Refused(
                f"another open draft already claims v{version}: `{dref}` at "
                f"{tip[:10]}.\n"
                "Two drafts that both say they are v" + version + " is the "
                "PLM conflict git was\nalways going to surface — one number, "
                "one answer.\n"
                "\n"
                "  bump yours to a version no other draft claims, or rebase "
                "onto that\n  draft if you meant to build on it:\n"
                f"    git fetch origin && git rebase origin/{dref[len('refs/heads/'):]}")
    return version, bump, latest


def _manifest_of(git_dir: Path | str, commit: str) -> tuple[dict, str]:
    raw = git(git_dir, "cat-file", "blob", f"{commit}:plm.toml", check=False,
              binary=True)
    if not raw:
        return {}, ""
    try:
        return tomllib.loads(raw.decode(errors="replace")), raw.decode(
            errors="replace")
    except tomllib.TOMLDecodeError:
        return {}, ""


def _check_ancestry(git_dir: Path | str, commit: str, number: str) -> None:
    base = default_ref(git_dir)
    if base != "refs/heads/main":
        # nothing released: HEAD is merely the newest DRAFT, not "released
        # history" — building on an older draft is legal, and a commit that
        # descends from no version at all is refused by `_check_version`
        # (bump_base_for), which can say what plm knows (stage 16b)
        return
    base_sha = git(git_dir, "rev-parse", "--verify", "--quiet", base,
                   check=False).strip()
    if not base_sha:
        return  # a repo with no history at all: anything is an improvement
    if _ok(git_dir, "merge-base", "--is-ancestor", base_sha, commit):
        return
    raise Refused(
        f"this commit is not built on {number}'s released history "
        f"({base} at {base_sha[:10]}).\n"
        "Released revisions are records. A draft that forks off an older state "
        "would\nsilently drop whatever was approved in between, and approving "
        "it would rewrite\nhistory rather than extend it.\n"
        "\n"
        "  bring yourself up to date and try again:\n"
        "    git fetch origin && git rebase origin/"
        + base[len("refs/heads/"):] + "\n"
        "\n"
        "  (this is also what you see when two drafts race: the second rebases "
        "onto\n   whatever landed first.)")


def _check_files(doc: dict, entries: dict[str, Entry], number: str) -> None:
    # ⛔ H1 — THE MANIFEST DOES NOT CHOOSE THE BACKEND OR THE KEY. Asked FIRST,
    # of the same function `gitindex.external_location` uses to write the row,
    # so "what the indexer would store" and "what the hook allows" cannot drift.
    # Before this, `[files]` role paths were checked against the tree or an
    # external NAME and `backend`/`key` were never looked at at all.
    from . import storage
    version = str(doc.get("version") or "")
    for e in ((doc.get("files") or {}).get("external") or []):
        try:
            storage.external_key(number, version, e.get("name") or "",
                                 backend=e.get("backend") or "",
                                 declared=e.get("key") or "")
        except storage.StorageError as exc:
            raise Refused(
                f"`[[files.external]]` row {e.get('name') or '(unnamed)'!r} is "
                f"not one plm will store:\n\n{exc}") from None
    external = {e.get("name", "") for e in (doc.get("files") or {})
                .get("external", [])}
    files_tbl = {k: v for k, v in (doc.get("files") or {}).items()
                 if k not in ("primary", "external")}
    missing: list[tuple[str, str]] = []
    for role, val in files_tbl.items():
        for p in ([val] if isinstance(val, str) else list(val or [])):
            if p not in entries and p not in external:
                missing.append((role, p))
    if missing:
        raise Refused(
            "`[files]` names paths that are not in this tree and not declared "
            "external:\n"
            + "".join(f"    {role} = {p!r}\n" for role, p in missing[:8])
            + "\n"
            "A file role is a promise about where the bytes are. plm's viewer, "
            "thumbnailer\nand release checks all follow it, and a dangling role "
            "breaks all three.\n"
            "\n"
            "  either `git add` the file, or drop the line, or — if it lives in "
            "the object\n  store rather than the tree — declare it under "
            "`[[files.external]]`\n  (name, role, backend, key, sha256, size).")
    primary = (doc.get("files") or {}).get("primary") or ""
    if primary:
        if primary not in entries and primary not in external:
            raise Refused(
                f"`[files] primary = {primary!r}` is not in this tree.\n"
                "`primary` is the geometry plm renders, thumbnails and diffs in "
                "3-D; naming a\nfile that is not there means the part has no "
                "picture anywhere in the system.\n"
                "\n  point it at a mesh that exists, or remove the line.")
        if Path(primary).suffix.lower() not in manifest.MESH_EXT:
            raise Refused(
                f"`[files] primary = {primary!r}` is not geometry "
                f"({', '.join(manifest.MESH_EXT)}).\n"
                "`primary` answers 'which mesh IS this part' — it is what the "
                "viewer loads and\nwhat the 3-D diff overlays. A drawing or a "
                "README cannot answer that.\n"
                "\n"
                "  name the mesh under `primary` and keep the document under "
                "its own role\n  (`drawing`, `doc`).")


def _check_bom(git_dir: Path | str, doc: dict, entries: dict[str, Entry],
               number: str) -> dict[str, str]:
    """`[[bom]]` lines and gitlinks must be the same set of facts (§5.1: the
    gitlink IS the pin). Returns path -> child commit for the caller."""
    from . import requirements
    links = {p: e.sha for p, e in entries.items() if e.mode == MODE_GITLINK}
    bad = requirements.validate(doc)
    if bad:
        raise Refused("\n".join(dict.fromkeys(bad)) + "\n\n"
                      "A purchase REQUIREMENT is a `[[bom]]` line with "
                      "`requirement = \"…\"` and no `path`\n(nothing selected "
                      "yet); a SELECTED purchase is `path = "
                      "\"catalog/<N>.toml\"`. docs/PLM_GIT_FIRST.md §5.6.")
    # stage 19: requirement lines (no path) select nothing and pin nothing —
    # they are not part of the gitlink <-> line agreement below
    lines = [b for b in (doc.get("bom") or [])
             if not requirements.is_requirement(b)]
    line_paths = [str(b.get("path") or "") for b in lines]

    # every gitlink under parts/ (a BOM child) or lib/ (a shared SOURCE —
    # stage 15) has to resolve in the child's own repo
    for path, sha in sorted(links.items()):
        if path == "catalog":
            continue
        child = (path.split("/", 1)[1]
                 if path.startswith("parts/") or path.startswith("lib/") else "")
        if not child or "/" in child:
            raise Refused(
                f"`{path}` is a submodule in a place plm does not read.\n"
                "BOM children live at `parts/<CHILD-NUMBER>`, a shared source "
                "at `lib/<NUMBER>`\n(docs/SHARED-SOURCES.md) and the purchased "
                "catalog at `catalog/` — those\nthree paths are the whole "
                "convention (§5.1, §5.4).\n"
                f"\n  git mv {path} parts/<CHILD-NUMBER>   # or lib/<NUMBER>")
        # ⛔ I1 — GATED HERE, not by the fact that `_check_shape` happened to
        # run first. `child` comes from a tree path in a PUSHED commit; the
        # review noted this was "correct today, fragile to reordering", so the
        # ordering is no longer what makes it correct.
        try:
            child_dir = safegit.repo_dir(REPOS, child)
        except safegit.SafeError as exc:
            raise Refused(
                f"`{path}` names a BOM child plm cannot look up: {exc}\n"
                "A submodule under `parts/` is addressed by ITEM NUMBER, and an "
                "item number is\nwhat plm says it is.") from None
        if not (child_dir / "HEAD").exists():
            raise Refused(
                f"`{path}` points at {child}, which has no repository here.\n"
                "plm owns identity: a BOM child is an item plm allocated, and "
                "its repo is\ncreated with it.\n"
                f"\n  check the number, or register {child} in plm first.")
        if not other_ok(child_dir, "cat-file", "-e", f"{sha}^{{commit}}"):
            raise Refused(
                f"`{path}` is pinned at {sha[:10]}, and {child}'s repository "
                "does not have\nthat commit. A gitlink IS the pin (§5.1), so a "
                "pin nobody can fetch is a BOM\nline that resolves to nothing — "
                "`git clone --recurse-submodules` would fail for\neverybody who "
                "cloned this revision.\n"
                "\n"
                "  the usual cause is a child commit that only exists in your "
                "working copy.\n"
                f"  Pin a release instead:\n"
                f"    git -C {path} fetch origin --tags\n"
                f"    git -C {path} checkout v<x.y.z> && git add {path}\n"
                "\n"
                f"  or push that commit to {child} first (to a draft branch). "
                "Once it exists there,\n  plm keeps any commit a gitlink pins "
                "reachable (refs/plm/keep/<sha> in the\n  child's repo), so "
                "deleting or force-pushing that draft later cannot break\n  "
                "this BOM — only a commit plm never had can.")

    # …and the two lists must agree, in both directions. A `lib/<N>` pin is a
    # SOURCE dependency, never a BOM line (stage 15): it has no `[[bom]]` row,
    # no quantity, and is invisible to rolled BOMs and build plans.
    lib_lines = [p for p in line_paths if p.startswith("lib/")]
    if lib_lines:
        raise Refused(
            "these `[[bom]]` lines point at `lib/` — a shared SOURCE, which is "
            "not product\nstructure:\n"
            + "".join(f"    path = {p!r}\n" for p in sorted(lib_lines)[:8])
            + "\nA `lib/<NUMBER>` submodule says \"this revision is generated "
              "from that source\";\nit is never built, bought or counted. "
              "Delete the line and keep the submodule.")
    only_line = [p for p in line_paths
                 if not p.startswith("catalog/") and p not in links]
    only_link = [p for p in links if p not in line_paths and p != "catalog"
                 and not p.startswith("lib/")]
    if only_line:
        raise Refused(
            "these `[[bom]]` lines have no submodule to pin them:\n"
            + "".join(f"    path = {p!r}\n" for p in sorted(only_line)[:8])
            + "\n"
            "§5.1: the gitlink IS the pin, which is why a BOM line carries no "
            "version.\nA line with no gitlink is a quantity of nothing in "
            "particular.\n"
            "\n"
            "  add the child as a submodule with a RELATIVE url so the "
            "structure survives\n  a host move:\n"
            f"    git submodule add ../<CHILD>.git {sorted(only_line)[0]}\n"
            f"    git -C {sorted(only_line)[0]} checkout v<x.y.z>")
    if only_link:
        raise Refused(
            "these submodules are pinned but appear in no `[[bom]]` line:\n"
            + "".join(f"    {p}\n" for p in sorted(only_link)[:8])
            + "\n"
            "A gitlink says WHICH revision; the manifest line says how many, "
            "what find\nnumber, whether it is suppressed. A child with no line "
            "would silently drop out\nof the rolled BOM, the build plan and the "
            "purchase list.\n"
            "\n"
            "  add the line to plm.toml:\n"
            "    [[bom]]\n"
            f"    path = \"{sorted(only_link)[0]}\"\n"
            "    qty  = 1\n"
            "    unit = \"EA\"")

    # catalog lines must name a file that exists at the pinned catalog commit
    cat_lines = [p for p in line_paths if p.startswith("catalog/")]
    if cat_lines:
        pin = links.get("catalog")
        if not pin:
            raise Refused(
                "this revision buys things (`catalog/…` BOM lines) but pins no "
                "catalog:\n"
                + "".join(f"    {p}\n" for p in sorted(cat_lines)[:8])
                + "\n"
                "Purchased and hardware items are not repo-per-screw (§5.4) — "
                "they are one\nTOML each inside `catalog.git`, which an assembly "
                "pulls in as ONE submodule.\n"
                "\n    git submodule add ../catalog.git catalog")
        cat_dir = REPOS / "catalog.git"
        present = set()
        if (cat_dir / "HEAD").exists() and other_ok(
                cat_dir, "cat-file", "-e", f"{pin}^{{commit}}"):
            present = set(
                other(cat_dir, "ls-tree", "-r", "--name-only", pin)[1]
                .splitlines())
        else:
            raise Refused(
                f"the `catalog` submodule is pinned at {pin[:10]}, which "
                "catalog.git does not have.\n"
                "\n  git -C catalog fetch origin && git -C catalog checkout "
                "origin/main && git add catalog")
        gone = [p for p in cat_lines if p.split("/", 1)[1] not in present]
        if gone:
            raise Refused(
                f"these catalog lines name a file that does not exist at the "
                f"pinned catalog\ncommit ({pin[:10]}):\n"
                + "".join(f"    {p}\n" for p in sorted(gone)[:8])
                + "\n"
                "A purchased line points at `catalog/<NUMBER>.toml`, and that "
                "file is the part.\n"
                "\n"
                "  bump the catalog pin so it includes the item:\n"
                "    git -C catalog fetch origin && git -C catalog checkout "
                "origin/main\n"
                "    git add catalog")
    return links


def _check_size(entries: dict[str, Entry]) -> tuple[int, int]:
    blobs = [e for e in entries.values()
             if e.mode in (MODE_BLOB, MODE_EXEC)]
    total = sum(e.size for e in blobs)
    if len(blobs) > MAX_FILES:
        raise Refused(
            f"{len(blobs)} tracked files in one revision is over the "
            f"{MAX_FILES} limit.\n"
            "  if a part genuinely needs that many the cap moves — say so. More "
            "likely a\n  build directory got committed; add it to .gitignore.")
    over = [e for e in blobs if e.size > MAX_FILE_BYTES]
    if over:
        e = max(over, key=lambda x: x.size)
        raise Refused(
            f"{e.path} is {e.size:,} bytes, over the {MAX_FILE_BYTES:,} byte "
            "per-file limit for a\nplain git blob.\n"
            "\n"
            "**git is the wrong transport above this size and plm will not "
            "pretend\notherwise.** A binary this big belongs in Git LFS "
            "(docs/LARGE-FILES.md):\n"
            f"  git lfs track '{e.path}' && git rm --cached -q '{e.path}' "
            f"&& git add '{e.path}' .gitattributes\n"
            "  git commit --amend --no-edit   # then push again\n"
            "\n"
            "  or, if it is generated: don't commit it. Commit the `[source]` "
            "that makes it.")
    if total > MAX_TOTAL_BYTES:
        raise Refused(
            f"this tree carries {total:,} bytes, over the {MAX_TOTAL_BYTES:,} "
            "byte total limit.\n"
            "  drop the derived artifacts that can be regenerated from "
            "`[source]`, or raise\n  PLMGIT_PUSH_MAX_TOTAL_BYTES if they are "
            "genuinely all controlled.")
    return len(blobs), total


def _cat_batch(git_dir: Path | str, shas: list[str]) -> dict[str, bytes]:
    """sha -> content for many SMALL blobs, in one `cat-file --batch`."""
    want = sorted(set(shas))
    if not want:
        return {}
    try:
        raw = safegit.git(git_dir, "cat-file", "--batch", binary=True,
                          stdin=("\n".join(want) + "\n").encode())
    except safegit.SafeError as exc:
        raise Refused(f"git plumbing failed reading small blobs: {exc}") from None
    out: dict[str, bytes] = {}
    i = 0
    while i < len(raw):
        nl = raw.index(b"\n", i)
        head = raw[i:nl].decode(errors="replace").split()
        i = nl + 1
        if len(head) < 3 or head[1] == "missing":
            continue
        n = int(head[2])
        out[head[0]] = raw[i:i + n]
        i += n + 1
    return out


def _parent_blobs(git_dir: Path | str, commit: str) -> set[str]:
    """Every blob sha in the trees of `commit`'s parents — what this commit
    did NOT introduce. A file that was already there is grandfathered."""
    out: set[str] = set()
    for p in git(git_dir, "rev-parse", f"{commit}^@", check=False).split():
        out.update(e.sha for e in tree_entries(git_dir, p).values()
                   if e.mode != MODE_GITLINK)
    return out


def _check_lfs(git_dir: Path | str, commit: str,
               entries: dict[str, Entry]) -> tuple[list[str], list[str]]:
    """Stage 17 (docs/LARGE-FILES.md): Git LFS pointers and the policy.

    Refused: a pointer whose object is not in plm's store (or is the wrong
    size); a pointer standing in for a source / manifest / doc; a NEW non-text
    blob above the LFS threshold (or a mesh above it) that arrived as a plain
    blob. Linted: a new small mesh that `.gitattributes` says is LFS arrived
    plain (the pusher has no git-lfs). Returns (lint, info).
    """
    from . import lfs
    blobs = {p: e for p, e in entries.items() if e.mode in (MODE_BLOB, MODE_EXEC)}
    small = {p: e for p, e in blobs.items() if e.size <= lfs.POINTER_MAX}
    body = _cat_batch(git_dir, [e.sha for e in small.values()])
    pointers = {p: lfs.parse_pointer(body.get(e.sha)) for p, e in small.items()}
    pointers = {p: v for p, v in pointers.items() if v}

    plain = sorted(p for p in pointers if lfs.always_plain(p))
    if plain:
        raise Refused(
            "these are Git LFS pointers, and plm keeps them as plain text:\n"
            + "".join(f"    {p}\n" for p in plain[:8])
            + "\nSources, plm.toml and docs are what `git diff` and review are "
              "FOR; as a pointer\nnobody could read them without the object "
              "store. Untrack the pattern and re-add:\n"
            + "".join(f"  git lfs untrack '{p}' && git rm --cached -q '{p}' "
                      f"&& git add '{p}' .gitattributes\n" for p in plain[:3])
            + "  git commit --amend --no-edit   # then push again")

    missing = sorted((p, oid, size) for p, (oid, size) in pointers.items()
                     if not lfs.has(oid, size))
    if missing:
        oids = sorted({oid for _, oid, _ in missing})
        raise Refused(
            "these files are Git LFS pointers whose objects plm does not have:\n"
            + "".join(f"    {p} -> sha256:{oid[:12]}… ({size:,} bytes"
                      + (f"; plm has {lfs.size_of(oid):,}" if lfs.size_of(oid)
                         is not None else "") + ")\n"
                      for p, oid, size in missing[:8])
            + "\nA pointer is a promise that the bytes are in plm's LFS store; "
              "a revision that\npoints at nothing cannot be viewed, checked or "
              "printed. git-lfs uploads the\nobjects BEFORE the refs (its "
              "pre-push hook) — that did not happen here.\n"
              "\n"
              f"  git lfs push --object-id origin {' '.join(oids[:6])}\n"
              "  then push again. (No git-lfs on this machine? Run them "
              "through tools/git/git.sh —\n  the manufacturing workspace's "
              "git + git-lfs container; docs/LARGE-FILES.md.)")

    thr = lfs.threshold()
    big = {p: e for p, e in blobs.items() if p not in pointers
           and e.size > thr and lfs.wants_lfs(p, e.size)}
    old: set[str] | None = None
    if big:
        old = _parent_blobs(git_dir, commit)
        new = sorted((p for p, e in big.items() if e.sha not in old),
                     key=lambda p: -big[p].size)
        if new:
            p = new[0]
            pat = (f"*{Path(p).suffix.lower()}" if lfs.is_mesh(p) else p)
            raise Refused(
                f"{p} is {big[p].size:,} bytes and arrived as a plain git blob"
                + (f" (and {len(new) - 1} more)" if len(new) > 1 else "")
                + ".\n"
                f"Above {thr:,} bytes plm keeps non-text files in Git LFS — "
                "meshes at any size —\nso a clone carries a 130-byte pointer "
                "and `GIT_LFS_SKIP_SMUDGE=1` clones the text\nonly "
                "(docs/LARGE-FILES.md). The one fix, in this clone:\n"
                "\n"
                f"  git lfs install --local && git lfs track '{pat}'\n"
                + "".join(f"  git rm --cached -q '{q}' && git add '{q}'\n"
                          for q in new[:4])
                + "  git add .gitattributes && git commit --amend --no-edit\n"
                "  then push again — git-lfs uploads the object first. (No "
                "git-lfs here? Run every\n  line through tools/git/git.sh, "
                "the manufacturing workspace's git + git-lfs container.)")

    lint: list[str] = []
    attrs = b""
    if ".gitattributes" in blobs:
        attrs = body.get(blobs[".gitattributes"].sha) or git(
            git_dir, "cat-file", "blob", blobs[".gitattributes"].sha,
            binary=True, check=False) or b""
    plain_mesh = [p for p, e in blobs.items()
                  if p not in pointers and lfs.is_mesh(p) and lfs.tracks(attrs, p)]
    if plain_mesh:
        old = old if old is not None else _parent_blobs(git_dir, commit)
        fresh = sorted(p for p in plain_mesh if blobs[p].sha not in old)
        if fresh:
            lint.append(
                f"{', '.join(fresh[:4])}{' …' if len(fresh) > 4 else ''}: "
                "`.gitattributes` makes this an LFS file but it arrived as a "
                "plain blob —\n  this clone has no git-lfs, so an LFS clone "
                "will show it modified. Use git-lfs\n  "
                "(tools/git/git.sh in the manufacturing workspace), then `git "
                "add --renormalize . && git commit`.\n  Accepted: it is under the "
                f"{thr:,}-byte threshold.")
    info: list[str] = []
    if pointers:
        total = sum(sz for _, sz in pointers.values())
        info.append(f"{len(pointers)} Git LFS file(s), {total:,} bytes — every "
                    "object is in plm's store")
    return lint, info


#: A submodule path plm reads: a BOM child, or the purchased catalog. The same
#: two-path convention `_check_bom` enforces for the GITLINK half.
_SUBMODULE_PATH = re.compile(
    r"^((parts|lib)/[A-Za-z0-9][A-Za-z0-9_.-]{0,64}|catalog)$")
#: A relative URL, and nothing else. `../<CHILD>.git` resolves against whatever
#: URL the parent was cloned from, which is what keeps the structure intact
#: through a host move or a GitHub mirror (the design docs "Repo shape").
_SUBMODULE_URL = re.compile(r"^\.\.?/[A-Za-z0-9][A-Za-z0-9_./-]{0,128}$")


def _check_gitmodules(git_dir: Path | str, commit: str,
                      entries: dict[str, Entry]) -> None:
    """⛔ M3 — `.gitmodules` is validated. It never was.

    `_check_bom` checks the gitlinks thoroughly and never opened this file, so
    the URL and PATH half of a submodule was entirely unchecked while
    the design docs, `plm-git/docs/AGENT-INSTRUCTIONS.md` and the MCP's
    instructions all tell humans and agents to run
    `git clone --recurse-submodules`.

    An attacker pushes a tree whose `.gitmodules` points `parts/X` at
    `https://attacker/` — or `ssh://`, or `ext::` — and every cloner fetches it.
    Git 2.43 blocks `ext::` and `file://` for submodules by default and rejects
    path escapes, so today that is credential-leak / SSRF-from-the-cloner rather
    than RCE — but that mitigation is GIT's, not this code's, and it is one
    `protocol.allow` setting on a developer's machine away from being RCE.

    The rule, which is the convention the repo shape already had: every `url`
    relative, every `path` either `parts/<NUMBER>` or exactly `catalog`, every
    declared path actually a gitlink in this tree, and every gitlink declared.
    """
    if ".gitmodules" not in entries:
        # A tree with gitlinks and no .gitmodules is half a submodule — the
        # clone has nothing to fetch. `_check_bom` is the one that cares about
        # the BOM; this is the clone's half, and it is a refusal because
        # `--recurse-submodules` is the documented way to get this part.
        links = [p for p, e in entries.items() if e.mode == MODE_GITLINK]
        if links:
            raise Refused(
                "this tree has "
                + (f"{len(links)} submodules" if len(links) != 1
                   else "a submodule")
                + " and no `.gitmodules`, so a\n"
                "`git clone --recurse-submodules` of it fetches nothing:\n"
                + "".join(f"    {p}\n" for p in sorted(links)[:8])
                + "\n  plm writes the stanzas itself when you add a BOM line "
                  "through the API;\n  by hand it is one block per child:\n"
                  "\n    [submodule \"parts/<CHILD>\"]\n"
                  "        path = parts/<CHILD>\n"
                  "        url = ../<CHILD>.git")
        return
    raw = git(git_dir, "cat-file", "blob",
              f"{commit}:.gitmodules", check=False, binary=True)
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
    if len(text) > 256 * 1024:
        raise Refused("`.gitmodules` is implausibly large; plm will not parse "
                      "it.")
    declared: dict[str, str] = {}
    name = ""
    section = re.compile(r'^\s*\[submodule\s+"(.*)"\]\s*$')
    keyval = re.compile(r"^\s*([A-Za-z0-9-]+)\s*=\s*(.*?)\s*$")
    stanza: dict[str, str] = {}
    stanzas: list[tuple[str, dict]] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0]
        m = section.match(line)
        if m:
            if name:
                stanzas.append((name, stanza))
            name, stanza = m.group(1), {}
            continue
        kv = keyval.match(line)
        if kv and name:
            stanza[kv.group(1).lower()] = kv.group(2)
    if name:
        stanzas.append((name, stanza))

    for sub_name, st in stanzas:
        path = st.get("path", "")
        url = st.get("url", "")
        if not _SUBMODULE_PATH.match(path):
            raise Refused(
                f"`.gitmodules` declares submodule {sub_name!r} at "
                f"path {path!r}.\n"
                "plm reads submodules in exactly three places: "
                "`parts/<CHILD-NUMBER>` for a BOM\nchild, `lib/<NUMBER>` for a "
                "shared source, and `catalog` for the purchased catalog\n"
                "(§5.1, §5.4, docs/SHARED-SOURCES.md). Anything else is a "
                "submodule nothing in plm\nwill ever look at, and one every "
                "cloner still fetches.")
        if not _SUBMODULE_URL.match(url):
            raise Refused(
                f"`.gitmodules` points {path} at {url!r}.\n"
                "\n"
                "A submodule URL here must be RELATIVE — `../<CHILD>.git` — so "
                "it resolves against\nwhatever URL the parent was cloned from "
                "and the structure survives a host move\nor a mirror "
                "(the design docs, \"Repo shape\").\n"
                "\n"
                "An absolute URL in a pushed tree is worse than wrong: "
                "the design docs's acceptance\ntest, the agent instructions and "
                "the MCP all tell people to run\n`git clone "
                "--recurse-submodules`, so it is a fetch every one of them makes "
                "to a host\nyou chose, with whatever credentials their git "
                "helper offers.\n"
                "\n"
                f"    [submodule \"{path}\"]\n"
                f"        path = {path}\n"
                "        url = ../<CHILD-NUMBER>.git")
        if st.get("update", "") not in ("", "checkout", "none"):
            raise Refused(
                f"`.gitmodules` sets `update = {st['update']}` on {path}. "
                "`!command` and `rebase`\nare instructions to the CLONER's git, "
                "and a pushed tree does not get to give\nthose.")
        declared[path] = url

    links = {p for p, e in entries.items() if e.mode == MODE_GITLINK}
    missing = sorted(links - set(declared))
    extra = sorted(set(declared) - links)
    if missing:
        raise Refused(
            "these gitlinks have no `.gitmodules` stanza, so a "
            "`--recurse-submodules` clone\ncannot fetch them:\n"
            + "".join(f"    {p}\n" for p in missing[:8]))
    if extra:
        raise Refused(
            "`.gitmodules` declares submodules this tree has no gitlink for:\n"
            + "".join(f"    {p} -> {declared[p]}\n" for p in extra[:8])
            + "\nA stanza with nothing pinned behind it is a fetch instruction "
              "for a child that\nis not part of this revision.")


def _check_shape(entries: dict[str, Entry]) -> None:
    weird = [e for e in entries.values()
             if e.mode not in (MODE_BLOB, MODE_EXEC, MODE_GITLINK)]
    if weird:
        e = weird[0]
        raise Refused(
            f"`{e.path}` is a symlink (mode {e.mode}) and plm does not check "
            "symlinks into a\nrevision: a released tree has to mean the same "
            "thing on every machine that\nchecks it out, and a link that "
            "resolves differently (or escapes the repo) does\nnot.\n"
            "\n  commit the real file, or reference it through `[source]`.")
    for path in entries:
        parts = Path(path).parts
        if ".." in parts or path.startswith("/") or ".git" in parts:
            raise Refused(
                f"`{path}` escapes the repository (or hides inside .git).\n"
                "\n  remove it and amend.")


def _lint(doc: dict, entries: dict[str, Entry], previous: dict[str, Entry],
          number: str, version: str) -> list[str]:
    """Warnings. §6c-1 and §3 — things that will hurt later but are nobody's
    business to refuse today. A lint that blocked would be a lint nobody
    could fix on a Friday."""
    out: list[str] = []
    # STAGE 16b (cold run 7): a file both IN THE TREE and declared under
    # `[[files.external]]`. The tree is the revision: the in-tree file
    # SUPERSEDES the external row (the indexer ignores the row), and the line
    # left in plm.toml is the author's to delete — it points at an OLD copy.
    for e in ((doc.get("files") or {}).get("external") or []):
        n = e.get("name") or ""
        if n and n in entries and entries[n].mode != MODE_GITLINK:
            out.append(
                f"{n}: is in this tree AND declared under `[[files.external]]` "
                f"({e.get('backend') or 'vault'}). The tree file supersedes "
                "it — plm indexes\n  the committed bytes and ignores the "
                "external row, which points at an older copy.\n  Delete that "
                "`[[files.external]]` block from plm.toml (the role line stays)"
                " and push again.")
    new_names = [p for p in entries
                 if p not in previous and entries[p].mode != MODE_GITLINK]
    for p in sorted(new_names):
        name = Path(p).name
        if re.search(r"[_-]v\d+\.\d+\.\d+", name) or number in name:
            out.append(
                f"{p}: a NEW tracked filename carries the version or the item "
                f"number. In a\n  git-first repo the tag carries the version "
                f"and the repo carries the number,\n  so the file is just "
                f"`{re.sub(r'([_-]v\\d+\\.\\d+\\.\\d+|' + re.escape(number) + r'[_-]?)', '', name) or name}` "
                "— then `git log` and `git blame` work on it\n  and a revision "
                "is a modification instead of a delete plus an add "
                "(§6c-1).")
    # A `source` on a column-backed attribute is a fact the registry has nowhere
    # to put: `mass_g` and `material` are revision COLUMNS (so build plans can
    # roll them up in SQL) and columns carry no provenance. Saying so beats
    # dropping it silently and letting the next export "lose" it.
    try:
        from . import attributes as _attrs
        backed = set(_attrs.COLUMN_BACKED) | set(_attrs.ELSEWHERE)
    except Exception:  # pragma: no cover
        backed = set()
    for name, a in (doc.get("attributes") or {}).items():
        if name in backed and isinstance(a, dict) and a.get("source"):
            out.append(
                f"[attributes] {name} carries `source = {a['source'][:40]!r}`, "
                f"and {name} lives in a revision COLUMN, which has no "
                "provenance\n  field. plm will index the value and drop the "
                "source; put the instrument in\n  the commit message, or "
                "record it under a name of its own.")
    src = doc.get("source") or {}
    if not src.get("file"):
        out.append(
            "no `[source] file`: nothing records how this geometry is "
            "regenerated, so the\n  release check cannot prove the mesh matches "
            "the model that made it (§6b).")
    return out


#: The fields plm owns on a draft (stage 15): derived by the indexer, written
#: by the release commit. A pushed tree may carry stale values; they are ignored.
LIFECYCLE_FIELDS = ("state", "maturity", "author", "date", "summary")


def _lifecycle_info(doc: dict, subject: str) -> list[str]:
    """ONE info line when the pushed file carries lifecycle values plm will not
    use — never a refusal, never a warning (there is nothing to fix)."""
    stale = [k for k in LIFECYCLE_FIELDS
             if str(doc.get(k) or "") and not (k == "state"
                                               and doc.get(k) == "DRAFT")
             and not (k == "summary" and doc.get(k) == subject)]
    if not stale:
        return []
    return [f"plm.toml's {', '.join(stale)} {'is' if len(stale) == 1 else 'are'} "
            "plm's on a draft and ignored — the commit subject is the summary, "
            "the release commit writes them. Edit `version` and design facts "
            "only."]


def item_name(git_dir: Path | str) -> str:
    """The item's name from `refs/plm/item` (its home, §3), or ""."""
    import tomllib
    txt = git(git_dir, "cat-file", "-p", "refs/plm/item:item.toml", check=False)
    try:
        return str(tomllib.loads(txt or "").get("name") or "")
    except Exception:                                          # noqa: BLE001
        return ""


def _name_info(git_dir: Path | str, doc: dict) -> list[str]:
    """Stage 16: `name` in plm.toml is a COPY
    of the item's name (home: `refs/plm/item`, written by rename_item). On a
    draft it is ignored like the lifecycle fields and the release commit
    restates it — say so when a push changed it, so nobody believes they
    renamed a part by editing the file."""
    home = item_name(git_dir)
    mine = str(doc.get("name") or "")
    if not home or not mine or mine == home:
        return []
    return [f"plm.toml's `name` is a copy of the item's ({home!r}) and is "
            "ignored here — rename an item with rename_item; the release "
            "commit restates the item's name."]


def _check_relations(doc: dict) -> list[str]:
    """`[[relations]]` — the claims this revision makes (stage 10).

    ONE REFUSAL, and it is the BOM gitlink's: a line that pins its target at a
    `commit` the target's repository does not have. A pin nobody can fetch is a
    claim about nothing — "this coupon verified commit X" where X exists only in
    somebody's working copy cannot be checked by anyone, ever.

    Everything else is a LINT, because the registry glosses rather than gates:
      · the version and the commit disagree (the version's tag/branch is now a
        different commit — the claim is about the commit, and it may be stale);
      · a line with a version and no commit (plm pins the one it resolves);
      · `scope = "item"` — the pre-stage-10 shape, ignored;
      · an item-home or note-home type written here, where it is not its home;
      · a `result` that is not pass/fail/pending, or on a type with no results.
    """
    from . import relations as relmod
    out: list[str] = []
    for r in doc.get("relations") or []:
        type_ = str(r.get("type") or "")
        target = str(r.get("item") or "")
        version = str(r.get("version") or "")
        commit = str(r.get("commit") or "")
        where = (f"[[relations]] {type_} {target}"
                 + (f" v{version}" if version else ""))
        h = relmod.home(type_)
        if "scope" in r:
            out.append(
                f"{where}: `scope` is the pre-stage-10 shape and is ignored — "
                "where a claim lives is\n  decided by its TYPE (relations."
                "HOMES), not by a field. Delete the line's `scope`.")
        if h == "item":
            out.append(
                f"{where}: {type_} is an ITEM-level claim; its home is "
                "item.toml on refs/plm/item,\n  which plm writes (link_items). "
                "A copy here is ignored when the item ref exists.")
            continue
        if h == "note":
            out.append(
                f"{where}: {type_} is a post-release FACT; its home is the "
                "revision's refs/notes/plm note\n  (link_items on the released "
                "revision). In a tree it would revise the part to record\n  "
                "where the part is — read tolerantly, but write it as a note.")
        res = str(r.get("result") or "")
        if (res or r.get("measured")) and not relmod.spec(type_)["results"]:
            out.append(f"{where}: `result`/`measured` mean nothing on a "
                       f"{type_} line and are ignored.")
        elif res and res not in relmod.RESULTS:
            out.append(f"{where}: result = {res!r} — the registry knows "
                       f"{', '.join(relmod.RESULTS)}.")
        if not target or not version:
            continue
        try:
            trepo = safegit.repo_dir(REPOS, target)
        except safegit.SafeError:
            continue
        if not (trepo / "HEAD").exists():
            if commit:
                raise Refused(
                    f"{where} pins commit {commit[:10]}, and {target} has no "
                    "repository here — it is\na catalog item or not an item at "
                    "all. Drop `commit`: a claim about something with\nno "
                    "repository is pinned by its version alone.")
            continue
        if commit:
            try:
                commit = safegit.safe_sha(commit)
            except safegit.SafeError as exc:
                raise Refused(f"{where}: `commit = {commit!r}` is not a "
                              f"commit id: {exc}") from None
            if not other_ok(trepo, "cat-file", "-e", f"{commit}^{{commit}}"):
                raise Refused(
                    f"{where} pins commit {commit[:10]}, and {target}'s "
                    "repository does not have that\ncommit. The commit IS what "
                    "the claim is about — a coupon that verified a commit\n"
                    "nobody can fetch verified nothing anyone can check.\n"
                    "\n  pin the commit the version names:\n"
                    f"    git ls-remote ../{target}.git 'refs/tags/v{version}*' "
                    "'refs/heads/draft/*'\n"
                    "  or drop `commit` and plm will pin the one the version "
                    "resolves to.\n"
                    "\n  Once a pinned commit exists there, plm keeps it "
                    f"reachable in {target}'s repository\n  (refs/plm/keep/"
                    "<sha>), whatever later happens to the branch it was on.")
        now = relmod.pin_commit(target, version)
        if commit and now and not relmod.same_design(target, commit, now):
            out.append(
                f"{where} pins commit {commit[:10]}, but v{version} of {target} "
                f"is now {now[:10]}.\n  The claim is about the commit it pins — "
                "if the part changed since, re-test\n  and re-pin; if it did "
                "not, re-pin to say so.")
        elif not commit:
            out.append(
                f"{where} has no `commit`. plm pins the one v{version} names "
                f"now{' (' + now[:10] + ')' if now else ''} — write it\n  into "
                "the line so the claim says which bytes it was about.")
    return out


# -- the entry point ----------------------------------------------------------

def check(git_dir: Path | str, number: str, commit: str, *,
          ref: str = "", old: str = "", actor: str = "") -> Verdict:
    """Everything that can be decided from the repo alone. Raises `Refused`.

    `ref` is the ref being written (so a re-push of the same draft does not
    accuse itself of claiming its own version); `old` is its previous tip, or
    zeros for a creation.
    """
    git_dir = Path(git_dir)
    doc, text = _load_manifest(git_dir, commit, number)
    version, bump, latest = _check_version(git_dir, doc, number, commit, ref,
                                           old)
    _check_ancestry(git_dir, commit, number)
    entries = tree_entries(git_dir, commit)
    _check_shape(entries)
    _check_gitmodules(git_dir, commit, entries)
    _check_files(doc, entries, number)
    links = _check_bom(git_dir, doc, entries, number)
    rel_lint = _check_relations(doc)
    lfs_lint, lfs_info = _check_lfs(git_dir, commit, entries)
    nfiles, nbytes = _check_size(entries)

    subject = git(git_dir, "log", "-1", "--format=%s", commit).strip()
    body = git(git_dir, "log", "-1", "--format=%b", commit).strip()
    prev_entries: dict[str, Entry] = {}
    if old and old != ZERO:
        prev_entries = tree_entries(git_dir, old)
    else:
        base = git(git_dir, "rev-parse", "--verify", "--quiet",
                   default_ref(git_dir), check=False).strip()
        if base:
            prev_entries = tree_entries(git_dir, base)

    return Verdict(number=number, commit=commit, version=version, bump=bump,
                   previous=latest, summary=subject, body=body, manifest=doc,
                   manifest_text=text, files=nfiles, bytes=nbytes,
                   gitlinks=links,
                   lint=_lint(doc, entries, prev_entries, number, version)
                   + rel_lint + lfs_lint,
                   info=_lifecycle_info(doc, subject)
                   + _name_info(git_dir, doc) + lfs_info)


def check_ref_namespace(ref: str, old: str, new: str, *, draft_ref: str,
                        for_ref: str) -> str:
    """Which of the two accepted namespaces this is — or a refusal that teaches.

    Accepted: `refs/heads/draft/<topic>` (create / fast-forward / delete) and
    `refs/for/main` (the AGit "propose" ref). Everything else is refused, and
    each refusal names the ref that WOULD have worked.
    """
    if ref == for_ref:
        return "propose"
    if DRAFT_REF_RE.match(ref):
        return "draft"
    if ref == "refs/heads/main" or ref.startswith("refs/heads/main/"):
        raise Refused(
            "`main` carries released revisions only, and plm is the only thing "
            "that moves it.\n"
            "A release tag and the commit `main` points at are what an approver "
            "signed off;\nif a push could fast-forward `main`, 'released' would "
            "mean 'whoever pushed last'.\n"
            "\n"
            "  push your work as a draft — same commit, different ref:\n"
            f"    git push origin HEAD:{draft_ref}/<topic>\n"
            "\n"
            "  and when it is ready for a human to decide:\n"
            f"    git push origin HEAD:{for_ref}\n"
            "\n"
            "**Approval is the merge.** A human approving in plm is what moves "
            "`main` and\nwrites the tag.")
    if ref.startswith("refs/tags/"):
        raise Refused(
            f"`{ref}`: a tag IS a release, and plm writes them.\n"
            "The annotation on `v0.4.0` records who approved it, at what "
            "maturity, against\nwhich approval id — facts a pushed tag would "
            "invent. Tags here are immutable and\nserver-authored, which is the "
            "whole reason a clone of a release can be trusted.\n"
            "\n"
            f"  push the work:      git push origin HEAD:{draft_ref}/<topic>\n"
            f"  then submit it:     git push origin HEAD:{for_ref}\n"
            "  the tag follows the approval.")
    if ref.startswith("refs/plm/"):
        raise Refused(
            f"`{ref}` is plm's own namespace — review refs and item identity "
            "live there and\nare written by the server.\n"
            + ("\n**`refs/plm/item` is the item manifest** (docs §3): the name, "
               "the kind, the project,\nitem-level tags and relations. plm owns "
               "identity, which is the same reason a tree\nmay not rename its "
               "own number — so this ref is READ-ONLY to you:\n"
               "\n    git fetch origin refs/plm/item:refs/plm/item   # yes\n"
               "    git show refs/plm/item:item.toml\n"
               "\n  to CHANGE any of it, ask plm: PATCH /api/items/<NUMBER> "
               "(name, project),\n  PATCH /api/items/<NUMBER>/kind, "
               "/product — each is one commit on this ref.\n"
               if ref == "refs/plm/item" else "")
            + f"\n  push your work to {draft_ref}/<topic> instead.")
    if ref.startswith("refs/notes/"):
        raise Refused(
            f"`{ref}` is plm's notes namespace (docs §5) and the server writes "
            "it.\n"
            "A note records a fact that arrived AFTER a revision was frozen — a "
            "measured mass,\na caveat, a suppression, the lifecycle decision "
            "itself — and the release gate READS\nit, so a note anybody could "
            "write would be a gate anybody could open.\n"
            "\n  reading it is yours, and encouraged:\n"
            "    git fetch origin refs/notes/plm:refs/notes/plm\n"
            "    git log --notes=plm main\n"
            "\n  to WRITE one, use the verb that means it — the API and MCP "
            "surfaces are\n  unchanged, and on a frozen revision they land "
            "here:\n"
            "    set_attributes(...)        the mass you just measured\n"
            "    tag_item(version=...)      a caveat about THAT geometry\n"
            "    suppress_bom_line(...)     absent in this configuration\n"
            "    request_release(...)       the lifecycle decision itself")
    if ref.startswith("refs/heads/"):
        branch = ref[len("refs/heads/"):]
        raise Refused(
            f"`{branch}` is not a draft branch, so plm has nowhere to put it.\n"
            "A branch in a part repo means one thing: a proposed revision. "
            "(§5.2 — a\nDIVERGENT PURPOSE is a fork, which is a new number, not "
            "a long-lived branch.)\n"
            "\n"
            f"  rename it:  git push origin HEAD:{draft_ref}/{branch}\n")
    raise Refused(
        f"`{ref}` is not a ref this service accepts.\n"
        "\n  the two that are:\n"
        f"    git push origin HEAD:{draft_ref}/<topic>   # a draft — visible, "
        "diffable\n"
        f"    git push origin HEAD:{for_ref}             # submit it for "
        "approval")
