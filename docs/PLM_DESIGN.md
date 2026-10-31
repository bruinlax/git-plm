# plm — a part registry first, a viewer second

*2026-08-11. Ground-up redesign of print-plm per the maintainer's direction: "database
driven… a github for parts… I want to know what the most recent version of
parts are, and THEN I can choose to see the STLs."*

## What v1 got wrong

1. **Slow by construction.** Every `/api/projects` call `rglob`s the whole
   projects tree — on a network-synced volume. The UI polls the printer every
   3 s. Nothing is indexed.
2. **The slicer is welded into the core.** OpenSCAD/Orca/Moonraker are the
   center of the app; parts and versions barely exist as concepts.
3. **The viewer is the app.** Geometry should be something you open *from* a
   revision, not the organizing principle.

Meanwhile the lantern project hand-built the real thing in files:
`parts/KML-PRT-####_slug/versions/vX.Y.Z/`, `plm/versions.csv` (211 revision
records with ECN + lifecycle), `plm/bom.csv` (568 versioned BOM lines with find
numbers), `plm/sources.csv` (purchased-part sourcing), release manifests binding
version → `.scad` + selector → artifact. **That convention is the product.**
plm turns it into a service.

## Shape

```
   agents / processes                    browser
        │  check in / query                │  browse registry, revisions,
        ▼                                  ▼  BOMs — then open the viewer
  ┌───────────────────────── plm (FastAPI, :8097) ─────────────────────────┐
  │  SQLite index (named volume, OFF the synced tree — rebuildable)         │
  │  git vault   (the 3d-printing repo — files are still the truth)         │
  └──────────────────────────────────────────────────────────────────────────┘
        downstream, NOT core: slicing/printing (v1 stays on :8095 for now)
```

- **Files + git remain the source of truth.** The DB is an index built by
  check-ins and by the importer; `rm` the DB and reimport and you lose nothing.
  This also honors the synced-volume rule: SQLite lives in a Docker named
  volume, never on a synced folder.
- **Check-in is the only write path.** A check-in writes the vault convention
  (same folders the lantern processes already produce), makes one git commit,
  and updates the index. Agents move from "edit CSVs by hand" to one API call.
- **git is the diff engine.** Each check-in is a commit; revision-to-revision
  diff is `git diff` scoped to the item folder (with a text-diff fallback for
  imported history that predates per-revision commits).

## Data model (SQLite, WAL)

```
items      id, number UNIQ, name, kind, project, folder, status, meta
           latest_rev_id           -- denormalized at check-in: "what's current"
                                   -- is one indexed read, never a scan
revisions  id, item_id, version, vmaj/vmin/vpat (sortable), state,
           date, ecn, summary, verification, author, git_commit
           UNIQ(item_id, version)
files      id, revision_id, role(source|artifact|drawing|doc), path,
           sha256, size, media
bom        id, parent_rev_id, find_number, child_item_id, child_version,
           qty, unit, alt_group, notes
sources    id, item_id, supplier, manufacturer, mpn, url, status, notes
events     id, item_id, revision_id, type, at, actor, data
```

Item kinds map from number prefixes: `PRT` part, `ASM` assembly, `PUR`
purchased, `HDW` hardware, `FW` firmware, `DOC` document. Lifecycle states:
`DRAFT → PROTOTYPE → RELEASED → OBSOLETE` (+ `SUPERSEDED`).

## API

Registry (all served straight from the index — the speed contract):

    GET  /api/items?q=&kind=&project=&state=     list w/ latest version
    GET  /api/items/{number}                     revisions, files, sources
    GET  /api/items/{number}/bom?version=        one level, as authored
    GET  /api/items/{number}/bom?rolled=1        recursive rollup (pBOM, CTE)
    GET  /api/items/{number}/where-used
    GET  /api/items/{number}/diff?a=0.3.0&b=0.3.1
    GET  /api/blob?path=                          vault file (STL for viewer…)
    GET  /api/stats

Check-in (the github-for-parts part):

    POST /api/items                     register an item (number or auto-next)
    POST /api/items/{number}/checkin    {version, state, summary, ecn, files[],
                                         bom[]} → vault write + git commit + index
    POST /api/items/{number}/state      lifecycle transition (records an event)

Import:

    python -m plm.importer <project-folder>    lantern-style vault → index

## UI (small, fast, no framework)

- **Registry** — the landing page. A dense table: number, name, kind, latest
  version, state, project, updated. Instant client-side filter. No polling.
- **Item page** — revision timeline (version, state, ECN, summary), BOM with
  links, where-used, files, sourcing, diff picker.
- **Viewer** — opened from a revision. Three.js; for assemblies it resolves the
  BOM to child artifacts and reuses the explode / x-ray / solo interactions
  from the v1 Assembly tab.

## Disposable 3D snippets (docs/PLM_SNIPPETS.md)

The one place geometry is allowed in without becoming a record. `share_3d()` on
the MCP (or `POST /api/snippets`) takes a temporary STL, parks it in plm's own storage
under `plm/snippets/<token>/`, and returns `https://plm.example.com/snip/<token>`
— the same three.js overlay, reached by a token instead of a part number, so an
agent can ask "do you like this shape?" without polluting the registry. No item,
no revision, no `files` row, no git commit: it expires (7 days default, 30 max)
and a startup + 6-hourly janitor deletes the object and the row. The rule the
MCP instructions state plainly: quick look → snippet; anything that should exist
next month → check it in as a real part.

## Rollout plan

plm runs at :8097 beside v1 (:8095, which keeps slicing/printing working).
Once the lantern processes are revised to check in via the API and the other
projects are imported, v1's design/prepare surfaces shrink into a "send to
printer" appendage of plm — or a separate print service that consumes released
artifacts.
