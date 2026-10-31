"""What a file IS — declared, registered, and provenanced.

THE BUG THIS EXISTS TO KILL
---------------------------
plm used to decide what a file was by looking at its name. Three consumers ran
`path LIKE '%.stl'` and the importer wrote `files.role` from
`ROLE_BY_EXT = {".scad": "source", ".stl": "artifact", ...}.get(suffix,
"artifact")`. That last part is the poison: a GUESS was written into a field
that looks like a DECLARATION, so every downstream reader that dutifully
consulted `role` was still reading the filename, laundered. Nobody could tell a
fact from a guess, so nobody trusted the field, so the regex grew back.

Counted on prod before this module existed (837 files):

  - 60 STLs were NOT role='artifact'
  - 122 files that WERE role='artifact' were not geometry at all — .log,
    .blend, .tar.gz, .mp4, .png, .bin, .txt
  - 613 of 837 had NO media at all
  - 120 PNG renders were filed as `drawing`
  - `viz` (63 rows) was not in the declared vocabulary; `flat` was referenced in
    fab.py; storage.py kept a third partial list in PIN_ROLES

So the two sources of truth disagreed on roughly a fifth of the estate, and
whichever one you trusted you were wrong somewhere.

There was also a latent bug with a fuse in it: object-store rows are
CONTENT-ADDRESSED, so there is no extension in the path at all. Every
`path LIKE '%.stl'` predicate silently matches NOTHING for an offloaded file.
STORAGE_DEFAULT is 'vault' today, which is the only reason thumbnails still
work; flipping it would have quietly emptied the thumbnailer.

THE SEPARATION THAT FIXES IT
----------------------------
Two different questions were tangled into one string:

  FORMAT  — what these bytes ARE (model/stl, image/png). This is MEASURABLE.
            Sniff the magic numbers and you know, with no reference to what
            anybody named the file. Measurement is not guessing.

  PURPOSE — what the file is FOR (the geometry of record, versus a preview
            render of it, versus a drawing that gates an approval). This is NOT
            derivable from the bytes: a PNG can be a render, a drawing, or a
            photo of the real part on a bench, and only the author knows which.
            So it must be DECLARED.

`media` carries format and is filled by sniffing. `role` carries purpose and is
declared. READS NEVER LOOK AT THE FILENAME — `is_geometry()` asks `media`, which
works identically for a vault path and a content-addressed blob key.

PROVENANCE IS THE LOAD-BEARING IDEA
-----------------------------------
`role_source` and `media_source` say where each classification came from:
`declared` (a human or agent said so), `sniffed` (measured from the bytes),
`extension` (guessed from the name — honest about being a guess). A backfilled
guess is stored AS a guess and shown as one, and is never promoted to fact by
being written down. That single distinction is the whole difference between this
module and what it replaces.

Adding a role is one dict entry. Adding a format is one line in MEDIA_MAGIC or
EXT_MEDIA. Neither is a feature.
"""
from __future__ import annotations

import struct
from pathlib import Path

# -- provenance ---------------------------------------------------------------
# Ordered best-to-worst. `better_source` uses this so a backfill can never
# downgrade a fact somebody actually stated.
DECLARED = "declared"     # a human or agent said so. The only real answer.
SNIFFED = "sniffed"       # measured from the bytes. Trustworthy for FORMAT.
EXTENSION = "extension"   # guessed from the filename. Visible as a guess.
UNKNOWN = ""              # never classified (pre- rows before backfill)

SOURCE_RANK = {DECLARED: 3, SNIFFED: 2, EXTENSION: 1, UNKNOWN: 0}


def better_source(a: str, b: str) -> str:
    """The stronger of two provenances. A backfill pass runs this so it can
    fill a blank without ever overwriting something declared."""
    return a if SOURCE_RANK.get(a, 0) >= SOURCE_RANK.get(b, 0) else b


def is_guess(source: str) -> bool:
    """True when this classification was inferred rather than stated. Every UI
    that shows a role reads this, so a guess never renders as a fact."""
    return source in (EXTENSION, UNKNOWN)


# -- formats ------------------------------------------------------------------
# Geometry that the viewer and thumbnailer can actually render. THIS SET is what
# replaced `path LIKE '%.stl'`: a measured format, not a name.
#
# Only model/stl is renderable TODAY (thumbs.py drives OpenSCAD, the viewer uses
# a three.js STL loader). The others are registered because they are real
# geometry that plm already accepts and will need to answer for — a STEP file is
# geometry even when nothing here can draw it, and calling it "not geometry"
# because our renderer is limited would be a lie the data model has to carry.
RENDERABLE_MEDIA = frozenset({"model/stl"})
#: What OpenSCAD (2021.01) can `import()` — the physical-diff tiles' renderer
#: (plm/checks.py). Wider than RENDERABLE_MEDIA, which is what the VIEWER and
#: the thumbnailer draw: a 3MF diffs as a picture, but has no 3-D overlay.
SCAD_IMPORT_MEDIA = frozenset({"model/stl", "model/3mf"})

# -- KINDS: what a format IS, and therefore how it can be SHOWN ---------------
#
# ⭐ THE SECOND CLASSIFIER, KILLED. `web/app.js` used to carry its own opinion —
# `previewKind()` over `EXT_IMAGE` / `EXT_TEXT` sets — and `gitrepo._kind_of()`
# carried a third, so a `.svg` was an image to one, a profile to another and a
# text file to the third. The answer to "what is this and how do we show it" now
# has ONE home, here, beside the sniffer that measures the format in the first
# place. Every file payload carries the answer; nothing downstream re-derives it.
#
# Four slots per kind, and they are deliberately different questions:
#
#   media    the formats that ARE this kind (measured; extension only as
#            fallback — see `kind_of`)
#   preview  which room opens when you click the name: '3d' the dark WebGL room,
#            'image'/'md'/'text'/'pdf' the lightbox, '' nothing (download)
#   view     the client widget that renders ONE of these (web/app.js WIDGETS)
#   diff     the FORMS a changed pair of this kind can be read in — the ids
#            of the forms web/diffwidgets.js registers for the kind (§2.9 of
#            docs/PLM_REVIEW_MODEL.md). The client registry is where a form is
#            implemented and where its DEFAULT is chosen (`defaultForm`, which
#            looks at the row: a mesh whose bytes did not change opens on
#            `pointer`, not `overlay`); this tuple is the same list as data for
#            agents and the API, and tests/test_diff_forms.py holds the two equal.
#
# Adding a G-code layer differ or a STEP viewer is one cell here plus one
# registration (a `WIDGETS` entry for a viewer, a `PLM_DIFF.register` form for
# a differ) — no edits to the tree, the compare view or the lightbox.
MEDIA_KINDS: dict[str, dict] = {
    "mesh": {
        "label": "3-D mesh",
        # (No `model/mesh`: it was listed here and nothing ever produced it —
        # no sniffer, no extension — and it leaked into GEOMETRY_MEDIA, so the
        # role-warning text grew a format plm cannot recognise. Stage 9's
        # compatibility harness found it as a wording difference against
        # master.)
        "media": ("model/stl", "model/3mf", "model/obj", "model/ply"),
        "preview": "3d", "view": "viewer3d",
        "diff": ("overlay", "stats", "pointer"),
        # Only STL parses today (three.js STLLoader). A 3MF is a mesh and is
        # honestly labelled one; the viewer says what it cannot do rather than
        # the registry pretending the file is something else.
        "viewable": ("model/stl",),
        "why": "the viewer reads binary and ASCII STL; other meshes download",
    },
    "cad": {
        "label": "CAD / profile",
        "media": ("model/step", "model/iges", "image/vnd.dwg",
                  "image/vnd.dxf", "application/dxf"),
        "preview": "", "view": "", "diff": ("text", "split", "size"),
        "why": "nothing here can open a solid model or a 2-D profile yet",
    },
    "image": {
        "label": "Image",
        "media": ("image/png", "image/jpeg", "image/gif", "image/webp",
                  "model/svg+xml"),
        "preview": "image", "view": "image",
        "diff": ("sidebyside", "swipe", "onion", "difference"),
    },
    "markdown": {
        "label": "Markdown",
        "media": ("text/markdown",),
        "preview": "md", "view": "markdown",
        "diff": ("text", "split", "rendered"),
    },
    "text": {
        "label": "Text",
        # The catch-all for anything `text/*`; this tuple only has to name the
        # formats that are NOT spelled `text/…`.
        "media": ("application/json", "text/x-scad", "text/x-python",
                  "text/x.gcode", "text/plain", "text/csv", "text/toml",
                  "text/yaml", "text/xml", "text/html", "text/css",
                  "text/javascript", "text/x-c", "text/x-c++", "text/x-diff",
                  "text/x-shellscript"),
        "preview": "text", "view": "text", "diff": ("text", "split"),
    },
    "manifest": {
        # plm.toml is text, and reading it as text is reading a diff of
        # bookkeeping. Narrowed BY NAME from `text` — the one place a name is
        # allowed to decide anything, because `plm.toml` is a name plm itself
        # writes (see NAME_KIND).
        "label": "plm manifest",
        "media": (), "preview": "text", "view": "text",
        "diff": ("fields", "design", "text"),
    },
    "submodule": {
        "label": "BOM child (pinned)",
        "media": (), "preview": "", "view": "pin", "diff": ("pin",),
        "why": "a gitlink is a pointer at a child repo, not bytes",
    },
    "pattern": {
        # A flat document (`*.flat.json`, format "plm.flat/1" — tools/flat in
        # the manufacturing workspace): the 2-D model of record a sewing pattern
        # or plasma profile is generated as. JSON by bytes, a pattern by
        # purpose, so it is its own media type (FLAT_MEDIA below, recognised by
        # its compound suffix in `sniff_media`) and its own kind here. Drawn by
        # web/flat.js as the `flat` widget; no pattern differ yet, so a pair is
        # read as text (`redline`) until one is worth building.
        "label": "Flat pattern",
        "media": ("application/vnd.plm.flat+json",),
        "preview": "flat", "view": "flat", "diff": ("text", "split"),
    },
    "pdf": {
        "label": "PDF",
        "media": ("application/pdf",),
        "preview": "pdf", "view": "pdf", "diff": ("text", "split", "size"),
    },
    "archive": {
        "label": "Archive",
        "media": ("application/zip", "application/gzip"),
        "preview": "", "view": "", "diff": ("text", "split", "size"),
        "why": "plm does not look inside an archive",
    },
    "binary": {
        "label": "Binary",
        "media": ("application/octet-stream", "application/x-blender",
                  "application/x.bgcode", "video/mp4", "video/quicktime",
                  "video/webm"),
        "preview": "", "view": "", "diff": ("text", "split", "size"),
        "why": "the size is all git honestly knows about these bytes",
    },
}

#: media -> kind, built from the table so the table is the only place to edit.
KIND_OF_MEDIA: dict[str, str] = {
    m: k for k, spec in MEDIA_KINDS.items() for m in spec["media"]}

#: ⚠️ THE ONLY NAME TEST IN THE SYSTEM, and it narrows rather than decides: a
#: file that is already `text` and is called `plm.toml` is the manifest. The
#: brief's escape hatch, used once, for the one filename plm writes itself.
NAME_KIND = {"plm.toml": "manifest"}

# Derived, so "what is geometry" and "what is a mesh" cannot drift apart.
# A STEP file is geometry (the data model has to carry that) and is not a mesh
# anything here can draw — which is exactly the mesh/cad split above.
GEOMETRY_MEDIA = frozenset(MEDIA_KINDS["mesh"]["media"]) | frozenset(
    m for m in MEDIA_KINDS["cad"]["media"] if m.startswith("model/"))
# 2-D cut profiles — the plasma/laser lane (fab.py's `flat` role). Geometry in
# the sense of "shape of record", but never a 3-D body, so they are deliberately
# NOT in GEOMETRY_MEDIA: a DXF must not make a part look 3-D-viewable.
FLAT_MEDIA = MEDIA_KINDS["pattern"]["media"][0]
PROFILE_MEDIA = frozenset({"image/vnd.dxf", "application/dxf", "model/svg+xml",
                           FLAT_MEDIA})
# The flat document (tools/flat in the manufacturing workspace, format
# "plm.flat/1"): the 2-D model of record a sewing pattern or plasma profile is
# generated as — pieces, cut and finished outlines, notches, seams, nest. The
# item page draws it natively (web/flat.js); the DXF/SVG/PDFs beside it are its
# derivatives. Recognised by its compound suffix, because `.json` alone says
# nothing about what a file is FOR.
FLAT_SUFFIX = ".flat.json"

# Magic-number sniffing. Format from BYTES — the honest half of classification.
# Each entry is (media, test) where test(head, size) -> bool.


def _stl_binary(head: bytes, size: int) -> bool:
    """Binary STL: 80-byte header, uint32 facet count, then exactly 50 bytes per
    facet. The size arithmetic IS the signature — there is no magic string — so
    this is a strong test that a stray .stl-named text file cannot pass."""
    if size < 84 or len(head) < 84:
        return False
    (n,) = struct.unpack("<I", head[80:84])
    return size == 84 + 50 * n


def _stl_ascii(head: bytes, size: int) -> bool:
    if not head[:80].lstrip()[:5].lower().startswith(b"solid"):
        return False
    # 'solid' alone is weak (an SCAD file could start with it), so require a
    # facet keyword in the first block.
    return b"facet" in head[:4096].lower() or b"endsolid" in head[:4096].lower()


def _zip_3mf(head: bytes, size: int) -> bool:
    # 3MF is an OPC zip. A bare .zip is not a 3MF, so this only fires when the
    # caller's name/extension also said 3mf — see sniff_media's ordering.
    return head[:4] in (b"PK\x03\x04", b"PK\x05\x06")


def _step(head: bytes, size: int) -> bool:
    h = head[:2048].upper()
    return b"ISO-10303" in h or h.lstrip().startswith(b"ISO-10303")


MEDIA_MAGIC: tuple[tuple[str, object], ...] = (
    ("model/stl", _stl_binary),
    ("model/stl", _stl_ascii),
    ("model/step", _step),
    ("image/png", lambda h, s: h[:8] == b"\x89PNG\r\n\x1a\n"),
    ("image/jpeg", lambda h, s: h[:3] == b"\xff\xd8\xff"),
    ("application/pdf", lambda h, s: h[:5] == b"%PDF-"),
    ("image/gif", lambda h, s: h[:6] in (b"GIF87a", b"GIF89a")),
    ("video/mp4", lambda h, s: len(h) > 12 and h[4:8] == b"ftyp"),
    ("application/gzip", lambda h, s: h[:2] == b"\x1f\x8b"),
)

# Extension → media. The GUESS path, used only when the bytes are unavailable
# or inconclusive, and always recorded as EXTENSION so it stays visibly a guess.
EXT_MEDIA = {
    ".stl": "model/stl", ".3mf": "model/3mf", ".step": "model/step",
    ".stp": "model/step", ".obj": "model/obj", ".ply": "model/ply",
    ".iges": "model/iges", ".igs": "model/iges",
    ".dxf": "image/vnd.dxf", ".svg": "model/svg+xml",
    ".scad": "text/x-scad", ".py": "text/x-python",
    ".gcode": "text/x.gcode", ".bgcode": "application/x.bgcode",
    ".md": "text/markdown", ".txt": "text/plain", ".csv": "text/csv",
    ".json": "application/json", ".yaml": "text/yaml", ".yml": "text/yaml",
    ".toml": "text/toml", ".ini": "text/plain", ".log": "text/plain",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
    ".pdf": "application/pdf", ".dwg": "image/vnd.dwg",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".zip": "application/zip", ".gz": "application/gzip",
    ".tgz": "application/gzip", ".bin": "application/octet-stream",
    ".blend": "application/x-blender",
    # Firmware sources and the vault's own bookkeeping. These were the residue
    # after the first backfill pass: 102 rows with no format, 65 of them
    # MANIFEST.sha256 files written by vault.write_checkin itself. Adding a line
    # here is the whole cost of teaching plm a new format.
    ".sha256": "text/plain", ".patch": "text/x-diff", ".sh": "text/x-shellscript",
    ".h": "text/x-c", ".c": "text/x-c", ".hpp": "text/x-c++",
    ".cpp": "text/x-c++", ".ino": "text/x-c++",
    ".html": "text/html", ".css": "text/css", ".js": "text/javascript",
    ".cfg": "text/plain", ".conf": "text/plain", ".lock": "text/plain",
    ".xml": "text/xml", ".dockerfile": "text/plain",
}

# Files whose NAME is the whole signal because they carry no extension at all.
# A short, explicit list beats a heuristic: these are the only extensionless
# names plm has ever seen, and an unknown one honestly reporting no format is
# better than a guess dressed up as knowledge.
NAME_MEDIA = {
    "dockerfile": "text/plain", "makefile": "text/plain",
    "manifest.sha256": "text/plain", "license": "text/plain",
    ".gitignore": "text/plain", ".dockerignore": "text/plain",
}


def sniff_media(data: bytes | None, name: str = "",
                size: int = 0) -> tuple[str, str]:
    """(media, provenance). Bytes win; the name is the fallback.

    Returns SNIFFED when a magic number matched — that is a measurement and it
    holds regardless of what the file is called or whether it is stored at a
    content-addressed key. Returns EXTENSION when we had to fall back to the
    name, so the caller can record that it is a guess.
    """
    ext = Path(name or "").suffix.lower()
    if data:
        head = data[:8192]
        n = size or len(data)
        for media, test in MEDIA_MAGIC:
            try:
                if test(head, n):            # type: ignore[operator]
                    return media, SNIFFED
            except (struct.error, IndexError, ValueError):
                continue
        # A zip is only a 3MF if the name says so — the container is generic, so
        # this stays a name-assisted guess rather than a measurement.
        if _zip_3mf(head, n):
            return ("model/3mf" if ext == ".3mf" else "application/zip"), (
                EXTENSION if ext == ".3mf" else SNIFFED)
    base = Path(name or "").name.lower()
    if base.endswith(FLAT_SUFFIX):
        return FLAT_MEDIA, EXTENSION
    if ext in EXT_MEDIA:
        return EXT_MEDIA[ext], EXTENSION
    if base in NAME_MEDIA:
        return NAME_MEDIA[base], EXTENSION
    return "", UNKNOWN


def kind_of(name: str = "", media: str = "", *, type_: str = "") -> str:
    """THE kind of a file: mesh / image / text / manifest / submodule / …

    Measured format first, extension only as the fallback — the same order
    `sniff_media` uses, and the reason this is not a filename regex: an STL and
    a `.stl`-named text file are different kinds, a content-addressed object
    has no extension at all, and a `.json` can be anything.

    `type_` is git's word for a tree entry (`submodule`), which is a kind no
    bytes can report because there are none.
    """
    if type_ == "submodule":
        return "submodule"
    m = (media or "").lower()
    if not m:
        m = sniff_media(None, name)[0]
    kind = KIND_OF_MEDIA.get(m) or ("text" if m.startswith("text/")
                                    else "binary" if m else "")
    # The narrowing pass: a name may only refine a kind, never invent one.
    base = Path(name or "").name.lower()
    if kind == "text" and base in NAME_KIND:
        kind = NAME_KIND[base]
    return kind or "binary"


def media_block(name: str = "", media: str = "", *, kind: str = "",
                type_: str = "") -> dict:
    """`{kind, preview, view, diff}` — what every file payload carries so that
    no client ever decides for itself how to show a file.

    One call per file row, in `plm/gitapi.py`, `plm/gitrepo.compare()` and
    `plm/db._file_out`. `diff` is a LIST of form ids (web/diffwidgets.js);
    the client renders a toggle per form and picks the default per row.
    """
    # The measured format when we have it, the extension's guess when we do not
    # — resolved ONCE here so every slot below agrees about what this file is.
    m = (media or "").lower() or sniff_media(None, name)[0]
    k = kind or kind_of(name, m, type_=type_)
    spec = MEDIA_KINDS.get(k) or MEDIA_KINDS["binary"]
    out = {"kind": k, "preview": spec["preview"], "view": spec["view"],
           "diff": list(spec["diff"])}
    # A kind with no room to open in owes an explanation, and the explanation is
    # registered next to the fact rather than written out at each call site.
    if not spec["preview"] and spec.get("why"):
        out["why"] = spec["why"]
    # A kind whose renderer only handles some of its formats says so HERE, once,
    # with the reason — rather than the client discovering it as a broken view.
    viewable = spec.get("viewable")
    if viewable and m not in viewable:
        out.update({"preview": "", "view": "",
                    "why": f"{m or 'this format'}: {spec.get('why', '')}"})
    out["media"] = m
    return out


def describe_kinds() -> list[dict]:
    """The kind table as data, for `GET /api/actions` and the MCP."""
    return [{"kind": k, "label": v["label"], "media": list(v["media"]),
             "preview": v["preview"], "view": v["view"],
             "diff": list(v["diff"]), "why": v.get("why", "")}
            for k, v in MEDIA_KINDS.items()]


def media_sql(col: str = "media", kinds: str = "geometry") -> tuple[str, list]:
    """A SQL predicate over the MEASURED format, and the one place it is spelled.

    This is the literal replacement for `path LIKE '%.stl'` in db.activity and
    thumbs._pick_stl / has_own_stl. Returns ("<col> IN (?,?,…)", [media…]) so
    callers interpolate only the column name they own.

    Why a set of media instead of a name pattern: an object-store row's key
    is a content hash with no extension in it, so the old predicate matched
    nothing for offloaded files and would have silently emptied the thumbnailer
    the day STORAGE_DEFAULT flipped.
    """
    sets = {"geometry": GEOMETRY_MEDIA, "renderable": RENDERABLE_MEDIA,
            "profile": PROFILE_MEDIA}
    vals = sorted(sets[kinds])
    return f"{col} IN ({','.join('?' * len(vals))})", vals


def is_geometry_media(media: str) -> bool:
    """THE geometry predicate. Replaces `path LIKE '%.stl'` everywhere.

    Works for a content-addressed blob key exactly as well as for a vault path,
    which is the bug it was written to close."""
    return (media or "").lower() in GEOMETRY_MEDIA


def is_renderable_media(media: str) -> bool:
    return (media or "").lower() in RENDERABLE_MEDIA


def is_scad_importable(path) -> bool:
    """Can OpenSCAD import() this file? By its format (EXT_MEDIA)."""
    return EXT_MEDIA.get(Path(str(path or "")).suffix.lower(), "") in SCAD_IMPORT_MEDIA


def is_profile_media(media: str) -> bool:
    return (media or "").lower() in PROFILE_MEDIA


# -- roles: what a file is FOR ------------------------------------------------
# ONE DICT ENTRY PER ROLE. Each declares what the role MEANS and what the system
# is entitled to conclude from it — never what extension it has.
#
#   label       human name
#   help        what it is for, in the terms an author would use when choosing
#   carries     'geometry' | 'profile' | 'document' | 'image' | 'code' | 'data'
#               — the EXPECTED format class. Advisory: it powers a mismatch
#               warning, never a refusal, because the author knows more than we
#               do and a refusal here would just teach people to lie.
#   pin_vault   bytes stay in the git vault (never offloaded) — storage.py reads
#               this instead of keeping its own PIN_ROLES list
#   primary     a candidate for "the" representative geometry of a revision
#   approval    presence/absence is meaningful to a release decision
#   legacy      registered so old rows stay valid, but not offered for new work
ROLES: dict[str, dict] = {
    "source": {
        "label": "Source",
        "help": "The definition the geometry is GENERATED from — a .scad, a "
                "script, a parametric model. Editing this is how the part "
                "changes; editing an artifact is how the record gets a lie in "
                "it.",
        "carries": "code",
        "pin_vault": True,
        "primary": False,
        "approval": False,
    },
    "mesh": {
        "label": "Mesh (geometry of record)",
        "help": "THE 3-D geometry this revision is defined by — what gets "
                "printed, measured and viewed. Prefer this over `artifact` for "
                "anything that is actually a body.",
        "carries": "geometry",
        "pin_vault": False,
        "primary": True,
        "approval": True,
    },
    "viz": {
        "label": "Visualization mesh",
        "help": "A deliberately-assembled representation for LOOKING at — an "
                "assembly posed in shared coordinates. Beats a print-oriented "
                "mesh for anything visual, which is why the thumbnailer picks "
                "it first.",
        "carries": "geometry",
        "pin_vault": True,
        "primary": True,
        "approval": False,
    },
    "flat": {
        "label": "Flat profile (2-D cut)",
        "help": "A 2-D outline to be cut — DXF/SVG for the plasma table or "
                "laser. Shape of record, but never a 3-D body, so it does not "
                "make a part 3-D-viewable.",
        "carries": "profile",
        "pin_vault": True,
        "primary": False,
        "approval": True,
    },
    "drawing": {
        "label": "Engineering drawing",
        "help": "A DIMENSIONED drawing that somebody could build or inspect "
                "from — the controlled document a release decision rests on. "
                "NOT a pretty render: use `render` for that.",
        "carries": "drawing",
        "pin_vault": False,
        "primary": False,
        "approval": True,
    },
    "render": {
        "label": "Render / preview image",
        "help": "A picture OF the geometry — a thumbnail, a marketing shot, a "
                "viewport grab. Derived and disposable. 120 of these were "
                "filed as `drawing` before this role existed, which would have "
                "made renders gate approvals.",
        "carries": "image",
        "pin_vault": False,
        "primary": False,
        "approval": False,
    },
    "photo": {
        "label": "Photograph",
        "help": "A picture of the REAL thing — the printed part, the weld, the "
                "failure. Evidence, not geometry. A photo of a cracked bracket "
                "is the most important file on a revision and must not be "
                "filed with the renders.",
        "carries": "image",
        "pin_vault": False,
        "primary": False,
        "approval": True,
    },
    "doc": {
        "label": "Document",
        "help": "Prose or tabular material about the part — notes, an SOP, a "
                "test report, a BOM export.",
        "carries": "document",
        # DELIBERATELY FALSE, and not an oversight. Text docs are already pinned
        # to the vault by EXTENSION (config.STORAGE_PIN_EXT covers .md/.txt/.csv
        # and friends), so pinning the ROLE would change nothing for them — but
        # it WOULD newly pin PDFs, dragging scanned drawings into git. Storage
        # policy is not this ticket's business, so the pinning behaviour is left
        # byte-for-byte where it was.
        "pin_vault": False,
        "primary": False,
        "approval": True,
    },
    "toolpath": {
        "label": "Toolpath / machine program",
        "help": "Machine-ready output: gcode for a printer, a cut program for "
                "the table. Bound to ONE machine and one set of parameters, so "
                "it is never the geometry of record.",
        "carries": "data",
        "pin_vault": False,
        "primary": False,
        "approval": False,
    },
    "log": {
        "label": "Log / run output",
        "help": "What a tool said while it ran — a slicer log, a render log. "
                "Kept for diagnosis; means nothing to the design.",
        "carries": "data",
        "pin_vault": False,
        "primary": False,
        "approval": False,
    },
    "archive": {
        "label": "Archive / bundle",
        "help": "A container of other files — a .zip, a .tar.gz, a project "
                "bundle. plm does not look inside, so an archive is opaque to "
                "every other surface.",
        "carries": "data",
        "pin_vault": False,
        "primary": False,
        "approval": False,
    },
    "video": {
        "label": "Video",
        "help": "A recording — a print timelapse, a motion check, a failure "
                "caught on camera.",
        "carries": "data",
        "pin_vault": False,
        "primary": False,
        "approval": False,
    },
    "artifact": {
        "label": "Artifact (unclassified)",
        "help": "LEGACY, AND A JUNK DRAWER. This was the DEFAULT, so it means "
                "'nobody said' — on prod it held STLs, gcode, logs, .blend "
                "files, tarballs, PNGs, .bin and .mp4 all at once. Registered "
                "so 228 existing rows stay valid; not offered for new work. "
                "Pick `mesh`, `toolpath`, `log`, `archive` or `video` instead.",
        "carries": "",
        "pin_vault": False,
        "primary": True,       # it still holds real meshes, so it must be
        "approval": False,     # searched for geometry — by MEDIA, not by name
        "legacy": True,
    },
}

# What storage.py used to hardcode as PIN_ROLES. Derived now, so adding a role
# that must not be offloaded is still one dict entry.
PIN_ROLES = frozenset(k for k, v in ROLES.items() if v.get("pin_vault"))
# Roles whose files are candidates for a revision's representative geometry.
PRIMARY_ROLES = frozenset(k for k, v in ROLES.items() if v.get("primary"))
LEGACY_ROLES = frozenset(k for k, v in ROLES.items() if v.get("legacy"))
# Offered in pickers and MCP docs: everything a NEW file should be able to be.
CHOOSABLE = tuple(k for k in ROLES if k not in LEGACY_ROLES)

# The format class each `carries` value accepts, for the advisory mismatch
# warning. A role that declares no expectation ('' — artifact) never warns.
_IMAGE_MEDIA = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

CARRIES_MEDIA: dict[str, frozenset] = {
    "geometry": GEOMETRY_MEDIA,
    "profile": PROFILE_MEDIA,
    "image": _IMAGE_MEDIA,
    "document": frozenset({"text/markdown", "text/plain", "text/csv",
                           "application/pdf", "image/vnd.dwg", "text/yaml"}),
    # A CLASS OF ITS OWN, held only by `drawing`, and the interesting case in the
    # whole registry. A dimensioned engineering drawing is legitimately exported
    # as a PDF, a PNG or an SVG — so THE BYTES CANNOT TELL a drawing from a
    # render. That is not a gap in the sniffer; it is precisely why `role` has to
    # be declared at all, and why `render` is a separate role rather than a
    # format rule.
    #
    # Concretely: 120 prod rows are role='drawing' holding a PNG, every one of
    # them from ROLE_BY_EXT calling all images drawings. Flagging those as a
    # format MISMATCH would be wrong, because a PNG drawing is a real thing.
    # They are flagged UNCONFIRMED instead, which is the true statement: nobody
    # has ever said which of the two they are.
    #
    # Kept out of `document` so that a PNG does not offer `doc` as a plausible
    # candidate — the confirm control should list the three answers somebody
    # might actually mean, not every role the format squeaks past.
    "drawing": frozenset({"application/pdf", "image/vnd.dwg", "model/svg+xml",
                          "image/vnd.dxf"}) | _IMAGE_MEDIA,
    # Source is source whatever language it is in. The first cut of this listed
    # four formats and consequently flagged the ENTIRE firmware tree — .h, .c,
    # .html, .css, .js — as a role/format mismatch, which is a registry being
    # wrong about the estate rather than the estate being wrong.
    #
    # text/markdown is deliberately ABSENT: it belongs to `doc` alone, which is
    # what makes a markdown file unambiguous and keeps 198 rows out of the review
    # queue (see roles.ambiguous).
    "code": frozenset({"text/x-scad", "text/x-python", "text/x-c", "text/x-c++",
                       "text/html", "text/css", "text/javascript", "text/xml",
                       "text/x-diff", "text/x-shellscript", "application/json",
                       "text/yaml", "text/toml", "text/plain"}),
    "data": frozenset(),   # deliberately open: logs, archives, gcode, video
}


def ambiguous(media: str) -> list[str]:
    """The registered, non-legacy roles that this FORMAT could plausibly be —
    counting only roles whose consequences differ.

    This is what decides whether a declaration is worth asking a human for, and
    computing it from the registry rather than hardcoding a list means it stays
    right as roles are added.

      image/png   -> ['drawing', 'photo', 'render']   AMBIGUOUS, and expensively
                     so: a drawing is release evidence, a render is disposable,
                     a photo of a cracked weld is the most important file on the
                     revision. Same bytes, three meanings.
      model/stl   -> ['mesh', 'viz']                  ambiguous: which one is THE
                     geometry of record versus a pretty assembly.
      text/markdown -> ['doc']                        NOT ambiguous. Nobody needs
                     to confirm that a markdown file is a document, and asking
                     198 times would empty the queue of meaning.
    """
    if not media:
        return []
    hits = [k for k, v in ROLES.items()
            if k not in LEGACY_ROLES
            and media in CARRIES_MEDIA.get(v.get("carries") or "", frozenset())]
    if len(hits) < 2:
        return []
    # Only ambiguous if the choice CHANGES something.
    sigs = {(bool(ROLES[k].get("approval")), bool(ROLES[k].get("primary")))
            for k in hits}
    return sorted(hits) if len(sigs) > 1 else []


class RoleError(ValueError):
    """An unregistered role. Refused at the write, which is the one place a
    typo can still be cheaply rejected — a role nobody registered is a purpose
    no surface will ever act on, so accepting it would only bury the mistake."""


def known(role: str) -> bool:
    return role in ROLES


def require(role: str) -> str:
    """Validate a declared role, or refuse with the vocabulary in the message.
    The error names CHOOSABLE, not every key, so nobody is invited into the
    legacy junk drawer by an error message."""
    if not known(role):
        raise RoleError(
            f"unknown file role {role!r}. Registered roles for new files: "
            + ", ".join(sorted(CHOOSABLE))
            + f". (Legacy, still readable: {', '.join(sorted(LEGACY_ROLES))}.) "
            "A role says what the file is FOR; add an entry to plm/roles.py "
            "ROLES if this is a genuinely new purpose.")
    return role


def mismatch(role: str, media: str) -> str:
    """A human warning when a declared role and a measured format disagree —
    'you called it a drawing and it is a PNG'. ADVISORY ONLY, never a refusal:
    the author knows more than the registry does, and refusing here would teach
    people to declare whatever passes.

    Returns '' when there is nothing to say."""
    spec = ROLES.get(role) or {}
    carries = spec.get("carries") or ""
    if not carries or not media:
        return ""
    allowed = CARRIES_MEDIA.get(carries)
    if not allowed or media in allowed:
        return ""
    return (f"role {role!r} expects {carries} ({', '.join(sorted(allowed))}) "
            f"but the bytes are {media}")


def infer_role(name: str, media: str = "") -> str:
    """The GUESS, kept in exactly one place and never presented as a fact.

    This is the descendant of importer.py's ROLE_BY_EXT — the function whose
    output used to be written into `role` as though somebody had declared it.
    It still exists because 837 pre-existing rows need SOME value and refusing
    to guess would just leave them blank, but every caller is required to record
    EXTENSION provenance alongside, so the guess stays visibly a guess.

    Prefers the measured media over the name, because format is the one half we
    can actually know.
    """
    m = (media or "").lower()
    ext = Path(name or "").suffix.lower()
    if m in GEOMETRY_MEDIA:
        return "mesh"
    if m in PROFILE_MEDIA:
        return "flat"
    if ext in (".scad", ".py"):
        return "source"
    if ext in (".gcode", ".bgcode"):
        return "toolpath"
    if ext == ".log":
        return "log"
    if m in ("application/zip", "application/gzip"):
        return "archive"
    if m.startswith("video/"):
        return "video"
    if m.startswith("image/"):
        # A render, not a drawing. The old table said `drawing` for every PNG,
        # which is how 120 preview images became candidate approval evidence.
        return "render"
    if m in ("text/markdown", "text/plain", "text/csv", "application/pdf"):
        return "doc"
    if ext in (".md", ".csv", ".txt", ".pdf"):
        return "doc"
    return "artifact"


def classify(name: str, role: str = "", data: bytes | None = None,
             size: int = 0, media: str = "") -> dict:
    """THE single classification seam. Every write path calls this.

    Returns {role, role_source, media, media_source, warning}.

    The contract, and the reason this function is the whole ticket in miniature:

      - A role the caller DECLARED is validated (RoleError on an unregistered
        one) and recorded as `declared`. It is never second-guessed from the
        bytes, because the author knows things the bytes do not.
      - A role the caller OMITTED is inferred and recorded as `extension` — a
        guess, marked as one. This is the behaviour importer.py used to have
        while pretending otherwise.
      - Media is sniffed from the bytes when we have them (`sniffed`), else
        guessed from the name (`extension`). A media the caller stated wins and
        is `declared`.

    `warning` is advisory: a declared role whose format class disagrees with the
    measured bytes. Surfaced, never enforced — refusing here would just teach
    people to declare whatever passes.
    """
    m, m_src = (media, DECLARED) if media else sniff_media(data, name, size)
    if role:
        require(role)
        r, r_src = role, DECLARED
    else:
        r, r_src = infer_role(name, m), EXTENSION
    return {"role": r, "role_source": r_src, "media": m, "media_source": m_src,
            "warning": mismatch(r, m) if r_src == DECLARED else ""}


def describe() -> list[dict]:
    """The registry as data, for the MCP/API so an agent can read the
    vocabulary instead of guessing at it — the same shape relations and
    attributes expose."""
    return [{"role": k, "label": v["label"], "help": v["help"],
             "carries": v.get("carries") or "",
             "pin_vault": bool(v.get("pin_vault")),
             "primary": bool(v.get("primary")),
             "approval": bool(v.get("approval")),
             "legacy": bool(v.get("legacy")),
             "choose_for_new_files": k in CHOOSABLE}
            for k, v in ROLES.items()]


# =============================================================================
# HOW BYTES ARE SERVED — H2 / M11 / L7. One table, because there were two.
# =============================================================================
#
# ⛔ WHAT WENT WRONG. `plm/api.py` had `INLINE_MEDIA` with a comment explaining,
# correctly, that `.svg` is deliberately absent: "an SVG is a document that can
# carry script, and serving one inline from plm's own origin would run it with
# plm's session". `plm/gitapi.py` then grew a SECOND, divergent extension table
# for the git-first blob route — with `".svg": "image/svg+xml"` in it, and
# `image/` in its INLINE set. The newest surface in the system re-opened the
# exact hole the older one had closed on purpose, because the rule lived in a
# table instead of in a function.
#
# So the rule lives here, once, next to the registry that already decides what
# a file IS:
#
#   serve_media(name)   the content type, from EXT_MEDIA — the SAME table
#                       `sniff_media` falls back to, so a file cannot be one
#                       thing to the classifier and another to the wire.
#   may_preview(media)  may this be rendered in a browser AT ALL?
#   preview_media(m)    what content type the PREVIEW route serves it as.
#
# ⭐ AND THE DEFAULT IS `attachment`. Every blob route serves
# `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`.
# Rendering is a separate, sandboxed route that opts IN per media type. That
# ordering is the fix: a format nobody has thought about is a download.

DEFAULT_MEDIA = "application/octet-stream"

#: What the PREVIEW route will render, and as what. Everything here is either
#: inert (a raster image) or served into a sandboxed document with a CSP that
#: forbids script (`plm/websec.py`). A type that is not here is not previewable,
#: whatever its extension says.
PREVIEW_MEDIA: dict[str, str] = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
    "application/pdf": "application/pdf",
    # ⚠️ SVG IS HERE AND IT IS THE ONE THAT NEEDS THE SANDBOX. `EXT_MEDIA` maps
    # `.svg` to `model/svg+xml` on purpose — a type no browser will execute — so
    # the ordinary blob route cannot render it. The preview route serves it as
    # `image/svg+xml` so it draws, under `Content-Security-Policy: sandbox;
    # script-src 'none'`, which is what stops the script it may carry. The
    # capability without the hole, which is what `web/app.js` was already doing
    # by hand with a client-side blob URL.
    "model/svg+xml": "image/svg+xml",
    "image/svg+xml": "image/svg+xml",
    # Text renders as text/plain, never as its own type: `text/html` in a
    # preview would be a document with plm's origin in the address bar.
    "text/markdown": "text/plain; charset=utf-8",
    "text/plain": "text/plain; charset=utf-8",
    "text/csv": "text/plain; charset=utf-8",
    "text/toml": "text/plain; charset=utf-8",
    "text/yaml": "text/plain; charset=utf-8",
    "text/x-scad": "text/plain; charset=utf-8",
    "text/x-python": "text/plain; charset=utf-8",
    "text/x.gcode": "text/plain; charset=utf-8",
    "application/json": "text/plain; charset=utf-8",
}

#: Types the 3-D viewer and the mesh differ fetch with `fetch()`. They are NOT
#: previewable — nothing renders them — but they must arrive as themselves and
#: not as `text/plain`, or the parser silently mangles the bytes.
FETCH_MEDIA = frozenset(MEDIA_KINDS["mesh"]["media"]) | {"image/vnd.dxf"}


def serve_media(name: str) -> str:
    """The content type to put on the wire for a file called `name`.

    From `EXT_MEDIA`, which is the one extension table in this codebase. An
    unknown extension is `application/octet-stream`, which with `nosniff` and
    `attachment` is the safest thing a server can say.
    """
    return EXT_MEDIA.get(Path(name or "").suffix.lower(), DEFAULT_MEDIA)


def may_preview(media: str) -> bool:
    return (media or "") in PREVIEW_MEDIA


def preview_media(media: str) -> str:
    return PREVIEW_MEDIA.get(media or "", "text/plain; charset=utf-8")


def content_disposition(disp: str, name: str) -> str:
    """RFC 6266 `Content-Disposition`, quoted and escaped — L7.

    ⛔ WHY NOT AN f-STRING. `f'{disp}; filename="{name}"'` with `name` from a
    git tree: a `"` in a committed filename appends attacker-chosen parameters,
    and a non-latin-1 filename raises on header encoding and 500s the endpoint
    mid-response. Git allows both. So: the quoted form carries an ASCII-safe
    fallback with the dangerous characters escaped, and `filename*` carries the
    real name as RFC 5987 UTF-8 — which is what every browser since IE9 prefers
    anyway, so nobody loses the accented filename either.
    """
    from urllib.parse import quote
    raw = Path(name or "").name or "file"
    ascii_name = raw.encode("ascii", "replace").decode("ascii")
    ascii_name = ascii_name.replace("\\", "_").replace('"', "_")
    ascii_name = "".join(c if c.isprintable() else "_" for c in ascii_name)
    out = f'{disp}; filename="{ascii_name}"'
    if raw != ascii_name:
        out += f"; filename*=UTF-8''{quote(raw, safe='')}"
    return out
