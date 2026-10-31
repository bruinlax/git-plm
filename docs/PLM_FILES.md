# File backend strategy — and the file-sharing service question

> **2026-10-16:** plm no longer uses filedrop as a storage backend — its
> object store is its own Git LFS store (docs/LARGE-FILES.md decision 0). An external file-sharing service can still
> be the file BUS (links), which plm consumes through the `url` wire form. This
> page is an early design record.

*2026-08-11. Question: version-controlled assembly instructions with files;
is there a universal file service for this, like S3?*

## Findings: what exists today

- **No S3-like service was in the stack at the time.** No MinIO, no object store.
- A capability-scoped *publisher* (sanitized snapshots pushed to an isolated
  viewer with bearer view/edit-draft tokens, expiry and revocation) is a
  different job — don't overload it.
- plm's current backend: the **git vault** (the 3d-printing repo) for
  controlled files + the SQLite index. `files` rows already carry `sha256`.

## Assessment of the git vault

Fine at today's scale, with three known pressures:

1. **Binary growth is forever.** Every checked-in STL lives in git history
   permanently; relief-map-class artifacts (100 MB+) would bloat the repo fast.
2. **The synced volume** is slow for bulk reads (sandbox copy took ~6 min for
   350 MB) and is the reason the DB and thumb cache already live elsewhere.
3. **No dedupe across items.** Revise carries file *rows* (no copies), but
   forks and re-checkins write identical bytes into new version folders.

## Direction: two tiers, one lookup

Keep git for what git is good at; add a content-addressed blob tier for bulk:

- **Tier 1 — git vault (now):** sources, markdown (instructions, context
  docs), manifests, small derivatives. Diffable, blame-able, the controlled
  record.
- **Tier 2 — CAS blob store (next):** big binaries stored once by sha256
  (`blobs/ab/cd/<sha256>`), referenced from `files.sha256` which already
  exists. Identical STLs across revisions/forks cost one copy. Git keeps a
  small pointer file (sha + size), so commits stay meaningful.
- `/api/blob` becomes the single resolver: path-in-vault or sha-in-CAS,
  callers never know the difference. Migration is lazy — new check-ins above
  a size threshold go to CAS; history stays where it is.

**The universal filedrop is worth building as its own service** — a thin CAS
HTTP service (PUT bytes → sha; GET sha → bytes; auth via service tokens), on local disk (a VM or a NAS via NFS —
*not* a synced tree). One backend for plm artifacts and other attachments. MinIO is the heavier alternative if S3 API
compatibility matters; a ~200-line FastAPI CAS is likely enough and is a small
separate service. plm is ready to consume it the day it exists (swap the blob resolver).

## Assembly instructions, version-controlled (works today)

Instructions are **document items** — the lantern's `KML-DOC-####` numbers,
already imported. The full lifecycle applies unchanged: DRAFT → working
uploads (photos, PDFs, markdown — files land in the doc item's version
folder) → context for the why → submit → approve → released record.

Conventions:

- One document item per assembly's instructions (`KML-DOC-1100` for
  ASM-1000's, as the lantern already numbered them).
- **Bind the doc onto the assembly's BOM** (qty 1, notes "work instructions",
  imprecise version during drafting; precise at release). It then rides
  where-used, rolled BOMs and diffs like any other occurrence, and releasing
  the assembly can propagate-pin the exact instruction revision.
- The item page renders the primary markdown of a document revision inline
  (with attachments listed beside it) — the registry is readable, not just a
  file index.
