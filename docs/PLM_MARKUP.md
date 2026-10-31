# Markup — a redline with coordinates

**STATUS: DESIGNED, NOT BUILT.** *2026-08-13. The maintainer's ask, verbatim: "the ability
to Comment on a part — with the ability to go into a 3d viewer of the stl and
then highlight a face or add some points / lines — e.g. I'd like to say 'bring
this face in, add holes here' and be able to select / circle where things should
go. It'd then save metadata on the object and let us also screenshot the markup
itself. Probably a new 'markup object' that links to an itemversion / file or
something, and then it'd have a thumb as well that could be added to the comment
so you could quickly see that I commented and what it was about. This could be
part of the workflow / approval (pull request process)."*

The viewer half — pick handling, drawing tools, the screenshot capture — is
scoped separately in `docs/PLM_MARKUP_UX.md`. This document owns the object, the
anchor, the payload, the API/MCP surface and the workflow. Where the two meet is
§7, "the contract with the viewer".

---

## 0. What this is, in one paragraph

A **markup** is a comment that carries geometry. It names the exact mesh it was
drawn on, one or more anchored entities (a face, a point, a polyline, a lassoed
region), a note per entity, and the camera pose the author was looking through.
It is *not* a design change and not a lifecycle state: it is the review comment
in plm's pull-request story — `plm-git` push → DRAFT → a human approving is the
merge (`plm-git/docs/AGENT-INSTRUCTIONS.md`), and a markup is the line comment
with a 3D coordinate attached. **The payoff is not the picture.** The payoff is
that `get_markup(12)` hands an agent `"bring this face in 2 mm"` *together with*
`p: [12.5, 0.0, 8.0]`, `n: [1, 0, 0]` and the sha256 of the mesh that sentence
was true of. A screenshot a human can read is the second-order benefit.

---

## 1. The object: a table, and one dict entry

### Why `markups` is its own table

The four cheaper options were considered and each fails on a specific fact:

| Reuse | Why not |
|---|---|
| **a `context` entry** with JSON in `body` | context is *append-only prose*, by design and in its docstring (`plm/context.py`). A markup has **mutable lifecycle state** (open → addressed → resolved) and a mutable screenshot. Putting a state machine inside an append-only log means either lying about append-only or re-deriving current state by folding the log. Also: `search.reindex` treats `context.body` as text to stem; a 4 KB JSON blob in the FTS index is pure noise |
| **a `files` row**, `role='markup'` | a `files` row is a **controlled record of a revision** — it lands in the vault, in the git commit, in `versions/v<x>/`, in `MANIFEST.sha256`, and `revise()` carries it forward forever (`plm/ops.py:572`). A review PNG is evidence attached to a comment, not part of the product dataset. Worse, it would collide with `ops.supersede_inherited`: a markup PNG and a real render PNG share `(role, extension)` at best and `.png` at worst, and the retirement rule would start retiring evidence |
| **an `attributes` row** | attributes are scalar measured facts with a unit (`plm/attributes.py`). A polyline is not a scalar |
| **a `relations` row** | `relations` is item↔item *by construction*: `item_id` and `target_item_id` are both FKs to `items` (`plm/db.py:135`). A markup is not an item. Making it one side of a relation means nullable polymorphic columns on the one table that is currently clean |

A markup is a **new noun with its own fields and its own mutable state**. That is
a table. What it must *not* get is its own comment log, its own event vocabulary
or its own UI card — those come free:

- **`context.TARGETS['markup']` — one dict entry.** `plm/context.py`'s docstring
  already reserves this ("a link someone questions, an approval someone
  explains, later a change order: each of those is a thing"). A markup is the
  most obvious target yet: the whole point is that people discuss it. The
  resolver returns the markup's `(item_id, revision_id)` anchor, so FTS and
  events keep working with **zero per-type code** — the exact payoff the anchor
  column was kept for (`docs/PLM_DATA_MODEL.md`, "Context is GENERIC").
- **`db.add_event`** for `markup.create` / `markup.shot` / `markup.state`, with
  the item and revision ids the rest of the log already uses.

If you find yourself writing `if target_type == "markup"` anywhere outside
`plm/markups.py`, the design has slipped — the same rule `plm/relations.py`
states about itself.

### Schema

```sql
-- Markup (plm/markups.py, docs/PLM_MARKUP.md): a comment that carries geometry.
-- Bound to the REVISION it was drawn on and to the exact mesh bytes
-- (mesh_sha256) it was anchored against, because a triangle index means nothing
-- on a mesh it was not drawn on. Lifecycle is a state, not a log: bots may claim
-- `addressed` with the revision that addressed it; only a human resolves.
CREATE TABLE IF NOT EXISTS markups (
  id INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES items(id),      -- the anchor, as context uses it
  revision_id INTEGER NOT NULL REFERENCES revisions(id),  -- the revision it was drawn ON
  file_id     INTEGER,                  -- the PRIMARY files row it was drawn on (see below)
  mesh_sha256 TEXT NOT NULL DEFAULT '', -- identity of those bytes; survives revise()
  title       TEXT NOT NULL DEFAULT '', -- REQUIRED, one line: "what I commented about"
  payload     TEXT NOT NULL DEFAULT '{}',   -- the annotation JSON (§3), versioned
  state       TEXT NOT NULL DEFAULT 'open', -- open|addressed|resolved|obsolete
  addressed_by_rev_id INTEGER REFERENCES revisions(id),  -- the revision claiming to fix it
  shot_key    TEXT NOT NULL DEFAULT '',    -- filedrop key of the screenshot, or ''
  shot_sha256 TEXT NOT NULL DEFAULT '',
  shot_size   INTEGER NOT NULL DEFAULT 0,
  shot_w      INTEGER NOT NULL DEFAULT 0,
  shot_h      INTEGER NOT NULL DEFAULT 0,
  actor       TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  decided_by  TEXT NOT NULL DEFAULT '',   -- who moved it to resolved/obsolete
  decided_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_markups_rev  ON markups(revision_id, state);
CREATE INDEX IF NOT EXISTS idx_markups_item ON markups(item_id, state);
CREATE INDEX IF NOT EXISTS idx_markups_mesh ON markups(mesh_sha256);
```

Notes on the fields that are not obvious:

- **`title` is required.** Same reasoning as plm-git's refusal of `wip` as a
  commit subject: the title *becomes* the line a future engineer reads in the
  queue, and it is the "quickly see that I commented and what it was about" half
  of the maintainer's ask. `"markup"`, `"fix"`, `"see image"` should be refused.
- **`file_id` + `mesh_sha256` are the PRIMARY body only, and they are a
  denormalization.** The truth is per-entity, inside the payload, because a
  markup on an assembly can legitimately touch two children ("this boss and that
  hole must line up"). The columns exist so the "does this still apply" verdict
  (§2) can be computed in one indexed query for a *list* of markups instead of
  parsing N JSON blobs. **If the column and the payload ever disagree, the
  payload wins**, and a repair is a rewrite of the column.
- **`file_id` is nullable and is a weak pointer on purpose.** `revise()` inserts
  *new* `files` rows with the same path/sha/key (`plm/ops.py:572`), so a `file_id`
  identifies a row on one revision and nothing else. It is kept for provenance
  and for the blob URL; **`mesh_sha256` is what identity is actually done with.**
  No FK, so a `remove_file` / supersession does not cascade a markup out of
  existence.
- **No `body` column.** The markup's prose is `context` on
  `target_type='markup'`, which gives it the same append-only, searchable,
  multi-author thread every other object has. Per-*entity* notes live in the
  payload, because they are a property of the geometry, not of the discussion.

### Lifecycle: bots propose, humans decide

```
 open ──(bot or human: addressed_by=<rev>)──▶ addressed ──(human)──▶ resolved
   │                                              │
   └──────────────(human)─────────────────────────┴──────────────▶ obsolete
```

| state | meaning | who may set it |
|---|---|---|
| `open` | raised, not answered | created this way; anyone with `editor` |
| `addressed` | **a claim**: revision X says it fixes this. Requires `addressed_by_rev_id` | **bots may** — this is the legwork half |
| `resolved` | **a verdict**: the person who raised it is satisfied | the markup's `actor`, or an `approver` |
| `obsolete` | it no longer applies (the geometry it pointed at is gone, or the design went another way) | same as `resolved` |

A bot calling `resolve_markup(id, 'resolved')` gets the plm-git-shaped refusal:

> *a markup is resolved by the person who raised it. Mark it `addressed` with
> `addressed_by_version=` — the revision you claim fixes it — and a human decides.*

That asymmetry is the same one `api_decide` / `decide_approval` already owns
(`plm/api.py:1675`, `plm_mcp/server.py:1084`), and `resolved`/`obsolete` must go
through `_require_verified_actor` for the same reason `withdraw` does: it is a
decision of record, so a body-supplied `actor` is not good enough once OIDC is
configured.

**Nothing transitions a markup automatically.** Not a revise, not an approval,
not a mesh whose sha changed. Staleness is *computed and displayed* (§2); state
is only ever *written by somebody*. An auto-resolve is the failure that makes
people stop trusting the list.

### The screenshot rides the storage seam

The PNG arrives as a **staged handle or a URL**, sha256 and size verified
server-side, exactly like every other binary in this service
(`plm/staging.py`, `docs/PLM_UPLOADS.md`). Never base64 through a tool call —
and for a markup that is not merely a style rule: a 1600×1200 canvas capture is
~600 KB, which is ~840 KB of base64 in a tool call, and it would be the single
largest thing an agent ever pushed through plm's context for no reason.

It lands at `plm/markups/<id>/shot.png` in the filedrop bucket, with sha/size on
the row, and is served by `GET /api/markups/{id}/shot` streaming through
`storage.resolve({'backend': 'filedrop', 'key': ...})` — **byte-for-byte the
snippet pattern** (`plm/snippets.py:311`, `plm/api.py:1037`). It is deliberately
*not* a `files` row, for the reasons in the table above.

Two consequences to accept out loud:

1. The object is not covered by `plm-backup` (which snapshots the SQLite index,
   not filedrop) — the same exposure every offloaded `files` row already has.
   The markup *row* is backed up; the picture might not be. Since the row carries
   the geometry, a lost screenshot loses convenience, not meaning.
2. `shot_key` may be `''` forever. A markup created by an agent has no browser
   and therefore no screenshot, and a human may skip it. So **`shot_url` is a
   URL or `null`** — presence is data, the law `plm/thumbs.py` established. A
   client must never discover there is no picture by fetching one and eating a
   404 (`docs/PLM_DATA_MODEL.md`, "Presence is data, not a guess").

**Field name: `shot_url`, not `thumb`.** `thumb` means one thing everywhere in
this service — "the render of this revision's geometry" — and a row that carried
both would make a reader guess. The UI displaying the shot at thumbnail size is
CSS, not a second object.

---

## 2. The hard problem: anchoring across revisions

> A face index only means something on the mesh it was drawn on. Get this wrong
> and a redline silently points at the wrong triangle, which is worse than
> losing it.

### The options

| Anchor | Precision | Survives a re-export? | Failure mode |
|---|---|---|---|
| **triangle index + mesh sha256** | exact | **no** — any re-export changes the sha, and facet order is not stable across OpenSCAD runs | **loud**. The sha guards the index, so a mismatch is detectable. This is its virtue |
| **barycentric coords on a named triangle** | sub-triangle | no, same as above | loud, same guard, more maths for precision nobody asked for. "Bring this face in" does not need a point *within* a triangle |
| **model-space point + surface normal** | ~mesh resolution | **yes, when the design did not move** — coordinates are the design's own frame, not the tessellation's | **quiet-ish**: if the geometry *did* move, the point is now in solid or in air. Detectable by re-raycasting, but not by a hash |
| **small point cloud** (a fan of surface samples) | good, robust to re-projection | yes | complexity: N points to store, N to validate, and an ambiguous verdict when 3 of 5 land |
| **bbox-relative parametric point** | poor | "yes" — and that is the trap | **catastrophic and silent**. A bbox change *moves* the point while keeping it valid-looking. This is precisely the failure the maintainer named. **Rejected outright** |

### The recommendation

> **Coordinates are the anchor. The triangle index and the mesh sha256 are the
> witness. The witness decides whether you may believe the anchor — the witness
> is never itself the anchor.**

Every geometric entity carries `p` (model-space point, mm) and `n` (unit surface
normal), *and* `tri` (the triangle index) marked in the schema and the docs as a
witness. Every entity also names its `mesh` by `sha256`. The markup row
denormalizes the primary mesh's sha for cheap list queries.

Concretely:

- **`p`/`n` are what an agent acts on.** "Offset the face at (12.5, 0, 8.0) whose
  normal is +X inward by 2 mm" is executable against the `.scad` source, which is
  where the fix actually happens. A triangle index is useless there — the source
  has no triangles.
- **`tri` is what tells you whether the anchor is trustworthy.** Combined with
  the sha it is a bit-exact statement: *this point was on triangle 4471 of these
  exact bytes.* Keep it, cite it in the viewer, never reason from it alone.
- **Derived measurements are recorded at pick time** (`area_mm2` for a face
  patch, `length_mm` for a polyline). These are the durable half: they survive a
  re-export that scrambles facet order, and they let a bot sanity-check that the
  184 mm² face it found on the new mesh is the 184 mm² face that was marked. Same
  philosophy as attributes — the instrument records what it measured, plm never
  recomputes it (`docs/PLM_DATA_MODEL.md`, "Attributes").

### What happens when the part is revised

**A markup is bound to the revision it was drawn on, permanently.**
`markups.revision_id` is written once and never changes. There is no migration
job, no re-anchoring pass, and **no re-projection, ever**.

Reading a *later* revision does not copy markups forward — it lists the item's
markups **by reference**, each stamped with a computed `applies` verdict:

| `applies` | Condition | What it means | What the reader is told |
|---|---|---|---|
| `current` | a `files` row on **this** revision has the same `mesh_sha256` | the mesh is byte-identical: the point *and* the triangle index are both still exactly right | nothing special. This is the common case, because `revise()` copies file rows verbatim until someone re-exports |
| `stale` | the item has a newer mesh, different sha | the point/normal are *probably* still meaningful (design coordinates rarely jump); the triangle index is *definitely* not | **"made on v0.3.0"** plus the version it was made on. The coordinates are still handed over; the `tri` is suppressed from the agent payload |
| `gone` | that sha is on no revision of this item any more | the bytes the markup names have left the registry (row superseded, file removed) | say so. The note and the coordinates survive; the mesh to check them against does not |

This is the same answer GitHub reached for line comments on a diff that moved:
mark it **outdated**, keep it readable, do not guess where it went. The maintainer is
already thinking in the pull-request idiom, so the idiom's own answer is the
least surprising one.

**Why not re-project.** Re-projection means raycasting the old `p` against the
new mesh and adopting whatever triangle it hits. It is ten lines of code and it
is exactly the failure being guarded against: when the design changed *at the
marked feature* — which is the whole reason the markup existed — the re-projected
hit is a *different feature*, and nothing in the record says so. A `stale` flag
loses nothing (the coordinates are still there) and lies about nothing.

The honest version of "carry it forward" is an explicit act, deferred to §6's
build order: `reanchor_markup(id)` — a human or bot re-picks the point on the new
mesh, which creates a **new markup** carrying `supersedes: <old id>` in its
payload and closes the old one as `addressed`. Re-anchoring is a new statement by
a named actor with a timestamp, not an inference.

### The `viz` composite problem

`thumbs.geometry_bodies` (`plm/thumbs.py:49`) returns 1..N bodies: either one
`viz` STL representing a whole assembly, or one primary STL per unsuppressed BOM
occurrence, recursing through sub-assemblies. So the STL a markup is drawn on
**may be a composite of an entire assembly, not one part.** Three consequences:

1. **The frame is unambiguous, and that is lucky.** `vPickAt` returns `local` =
   `hit.object.worldToLocal(hit.point)` (`web/app.js:2185`), and each body mesh is
   added to `V.group` at identity — the only transform is on the group
   (`V.group.rotation.x = -π/2`, the Z-up → Y-up display flip, `web/app.js:1729`).
   So body-local coordinates **are** the STL's own coordinates **are** the shared
   CAD frame. One triple is meaningful as both "a point on this mesh file" and "a
   point in the assembly's design frame". Nothing needs to be transformed, and
   nothing may be: **an agent must never see the display frame.** The payload
   states this explicitly as `"frame": "model_mm_z_up"` so a future reader cannot
   guess wrong.
2. **Mesh identity is per-entity.** A markup on an assembly may anchor entity 1
   to `KML-PRT-1012`'s artifact and entity 2 to `KML-PRT-1013`'s. The row-level
   `file_id`/`mesh_sha256` name the primary (first entity's) body; the payload
   names each.
3. **Markups on `viz` meshes go stale faster, and that is unavoidable.** `viz` is
   derived geometry, regenerated freely and attachable to frozen revisions
   (`plm/api.py:1431`) — so its sha churns for reasons that have nothing to do
   with the design. A redline on an assembly composite will read `stale` after the
   next viz regeneration even though nothing moved. **Mitigation is a nudge, not
   a rule**: the viewer knows which body was picked, so when every entity of a
   markup lands on one child it can offer *"you marked KML-PRT-1012 — file this on
   the part instead?"*. Filed on the part, the anchor tracks the part's own
   artifact and survives assembly-level churn. That nudge belongs in
   `docs/PLM_MARKUP_UX.md`; the data model supports either choice.

---

## 3. The annotation payload

One JSON shape, versioned, with a **typed entity list** — the registry
discipline. `markups.ENTITIES` is the dict; `spec()` degrades an unknown type to
a readable label rather than dropping it, copied from `relations.spec()`
(`plm/relations.py:84`) so a payload written by newer code never vanishes from an
older reader's UI. Adding `dimension`, `section_plane` or `arrow` later is a dict
entry.

```jsonc
{
  "v": 1,
  "frame": "model_mm_z_up",          // never the viewer's Y-up display frame

  "camera": {                        // so reopening shows what the author saw
    "eye":    [x, y, z],             // camera.position, WORLD (viewer) coords
    "target": [x, y, z],             // controls.target
    "up":     [x, y, z],
    "fov": 45,
    "aspect": 1.78,
    "explode": 60,                   // V.explode — a redline made at 60% is
    "xray": false,                   //   unreadable at 0%
    "hidden": ["KML-PRT-1013"]       // bodies the author had switched off
  },

  "entities": [
    { "id": "e1", "type": "face",
      "mesh": { "file_id": 812, "sha256": "9f3c…", "path": "…/KML-PRT-1012_v0.3.0.stl",
                "number": "KML-PRT-1012", "role": "artifact" },
      "p": [12.5, 0.0, 8.0],         // ANCHOR: model-space point, mm
      "n": [1.0, 0.0, 0.0],          // ANCHOR: unit surface normal
      "tri": 4471,                   // WITNESS ONLY — meaningless off this sha
      "tris": [4471, 4472, 4489],    // WITNESS ONLY — the coplanar patch
      "area_mm2": 184.2,             // measured at pick time; survives re-export
      "snapped": false,              // true = snapped to a vertex (vSnapVertex)
      "note": "bring this face in 2 mm" },

    { "id": "e2", "type": "point",
      "mesh": { … }, "p": [ … ], "n": [ … ], "tri": 991, "snapped": true,
      "note": "M3 through-hole here, ⌀3.4 clearance" },

    { "id": "e3", "type": "polyline",
      "mesh": { … },
      "pts": [ { "p": [ … ], "n": [ … ], "tri": 12, "snapped": true }, … ],
      "closed": false,
      "length_mm": 41.7,
      "note": "0.8 mm chamfer along this edge" },

    { "id": "e4", "type": "region",            // the circled / lassoed area
      "mesh": { … },
      "loop": [ { "p": [ … ], "n": [ … ] }, … ],  // closed loop, model space
      "centroid": [ … ], "n": [ … ],
      "area_mm2": 612.0,
      "screen_loop": [[0.41, 0.22], … ],       // normalized 0..1 in the CAPTURED
                                               //   image — display only, so the
                                               //   lasso redraws over the PNG
                                               //   without a 3D scene
      "note": "add holes here — 3 off, evenly on this pad" }
  ]
}
```

Rules the writer enforces (`markups.validate`), each with a reason:

- **`v` is required and must be `1`.** A payload without a version is a payload
  nobody can grow.
- **Every entity needs `p` (or `pts`/`loop`) and `note`.** An entity with only a
  `tri` is refused: it is unactionable the moment the mesh changes, which is the
  whole thing this design exists to prevent. An entity without a note is a
  decoration — the maintainer's ask is *sentences plus places*, and the sentence is the
  half a bot can act on.
- **`n` is required for `face` and `point`.** "Bring this face in" has no meaning
  without a direction, and the direction is the normal.
- **`tri`/`tris` are optional and are stripped from the agent payload when
  `applies != 'current'`.** Handing a bot a triangle index for a mesh it cannot
  have is an invitation to use it.
- **`frame` must be `model_mm_z_up`.** One value today; the field exists so the
  day a second frame appears, nothing silently reinterprets the first.
- **Cap it.** ≤ 64 entities, ≤ 512 points per polyline/loop, ≤ 64 KB serialized.
  A markup is a comment, not a mesh format; something that needs more is a new
  part or a real CAD file.

---

## 4. Surfaces

### HTTP

| Route | What |
|---|---|
| `POST /api/markups` | create. `{number\|revision_id, version?, file_id?, title, payload, actor}`. Resolves the anchor through `db.resolve_item` / `db.find_revision` like every other write, validates the payload, verifies each entity's `mesh.sha256` **is actually on that revision** (a 400 otherwise: an anchor to bytes the revision does not carry is a bug, not a record), writes the row + a `markup.create` event |
| `GET /api/markups` | list. `?number=&version=&revision_id=&state=open&mesh_sha256=&limit=`. Rows carry title/state/actor/dates, `applies`, the mesh identity, the entity count, the per-entity notes (they are short), `shot_url` (or `null`), and the comment count. **Never the payload** |
| `GET /api/markups/{id}` | one, in full: payload, `applies`, `mesh` (with `blob_url` so a caller can fetch the *exact* bytes it was drawn on), `shot_url`, `context` (its thread, via `context.entries('markup', id)`) |
| `POST /api/markups/{id}/shot` | attach or replace the screenshot: `{filedrop_key\|url, sha256, size, width, height}` through `staging.claim` / `staging.fetch_url`. Separate from create because the capture happens *after* the geometry settles, and because a markup without a picture is still a valid markup |
| `GET /api/markups/{id}/shot` | stream the PNG, `image/png` inline (`.png` is already in `api.INLINE_MEDIA`), `Cache-Control: private, max-age=300` — the snippet-blob shape |
| `POST /api/markups/{id}/state` | the transition: `{state, addressed_by_version?\|addressed_by_rev_id?, note?, actor}`. On its own endpoint, not folded into a PATCH, so the identity rule (`_require_verified_actor` for `resolved`/`obsolete`) lives in exactly one place — the same reason `decide` and `withdraw` are their own routes |
| `PATCH /api/markups/{id}` | the author corrects `title`, adds an entity note, or replaces the `payload` **while `state='open'` and only as its `actor`**. A frozen markup (anything past `open`) refuses: raise a new one |
| `GET /api/markups/entities` | the ENTITIES registry — type, label, required fields, hint. Clients render and validate from this, never from a hard-coded list (`GET /api/relations/types`' precedent) |

**No `DELETE`.** `obsolete` is the delete. Comments in plm are append-only
(`plm/context.py`), BOM lines on released structure are suppressed rather than
removed (`plm/api.py:1573`), and files rows are pointers whose removal never
loses bytes. A markup someone drew and regretted is a fact about the review; the
state that says "ignore this" is `obsolete`, and it keeps the trail.

### MCP — this is the payoff

Four tools (44 → 48). Every one exists so an agent can *act on* a redline, not
merely notice it.

```python
list_markups(number="", version="", state="open", revision_id=0, limit=50)
```
> The redlines on a part. **Default `state='open'`** — the question an agent asks
> is "what am I supposed to fix". Each row: id, title, actor, when, `state`,
> `applies` (`current` / `stale` / `gone`, plus the version it was drawn on), the
> mesh it anchors to, one line per entity (`type`, `note`, `p`), `shot_url` (or
> `null` — do not ask), and the comment count. Call `get_markup(id)` for the
> geometry.

```python
get_markup(markup_id)
```
> One redline, in full and machine-readable. Returns every entity with its
> **model-space point `p` (mm, CAD Z-up), surface normal `n`, and note** — so
> "bring this face in 2 mm" arrives *with the coordinates it is about*. Also: the
> mesh identity + a `blob_url` for the exact bytes it was drawn on, the camera
> pose, `applies`, and the comment thread.
>
> `applies='stale'` means the part has been re-exported since: the coordinates
> are still the author's, the **triangle indices are not returned**, and you
> should re-check the feature against the current mesh before changing it.
> `applies='gone'` means the mesh is no longer in the registry at all — read the
> note and treat it as prose.

```python
resolve_markup(markup_id, state="addressed", addressed_by_version="", note="")
```
> Move a redline's state. **What a bot does: `state='addressed'` with
> `addressed_by_version=` — the revision you claim fixes it.** That is a claim,
> and it is the honest one; the human who raised the redline decides whether it
> is satisfied. A bot asking for `resolved` is refused by name, the same way
> `decide_approval` refuses a bot: *"a markup is resolved by the person who
> raised it."* `note` is appended as a comment on the markup, so the reasoning
> lands where the next reader looks.

```python
create_markup(number, title, entities, version="", file_id=0, actor="mcp")
```
> Raise a redline yourself. An agent reading geometry that finds a problem — a
> 0.8 mm wall below the nozzle's two-perimeter minimum at (12, 0, 8) — should be
> able to put it in front of the maintainer *with the coordinate*, not bury it in a
> context entry. No screenshot (you have no viewer), which is fine: `shot_url`
> is `null` and that means "there is no picture", not "fetch and find out".
> One redline per problem, and a real title.

**How a bot links the revision that addressed it: a column, not a relation.**
`markups.addressed_by_rev_id`, set by `resolve_markup(..., 'addressed',
addressed_by_version=...)`, plus a `markup.addressed` event. Not a `relations`
row, because `relations` is item↔item by construction and a markup is not an
item (§1). The reverse read — "which redlines does this revision claim to
address?" — is one indexed query, surfaced on `get_revision` / the item payload
as `addressed_markups`.

### Where markups show up in reads that already exist

- **`get_item` / `GET /api/items/{n}`** gain a `markups` block. See §6 for the
  cost rule: **counts per revision, plus the open ones as title-only rows. Never
  a payload.**
- **`search`** — `search.reindex` appends the markup title and every entity note
  to the revision's indexed text, prefixed `[markup 12 open] …` so a snippet says
  what it was about. Roughly six lines in `plm/search.py`, and it is what makes
  *"find the part where I said the boss was too thin"* work. The markup's
  **comments** are already indexed for free, through the context anchor.
- **`GET /api/approvals` / `pending_approvals()`** gain
  `markups: {open: n, total: n}` plus the open titles for the submitted revision.
  That is the entire workflow integration (§5).

---

## 5. Workflow: information, not enforcement

### An open markup does NOT block release

Recommended firmly, and this is a decision the maintainer should confirm (§8).

1. **The gate is already a human who can see them.** Adding a second, automatic
   gate in front of a human gate produces the "why won't this submit" mystery,
   and the mystery produces an override flag, and the override flag is the thing
   that rots. plm's own posture is that judgement is a person's
   (`docs/PLM_IDENTITY.md`).
2. **An approver may legitimately ship over a redline** — "yes, that boss is
   thin; it prints fine, next rev." Blocking makes that decision unrecordable
   except by lying about the markup's state.
3. **A blocking check makes people stop creating markups.** The whole feature
   depends on the maintainer being willing to scribble a note; anything that turns a
   scribble into a release obstacle is a tax on scribbling.

### But it must be impossible to approve without seeing them

- `submit_for_release` counts the open markups on the revision, puts the count
  and their titles in the `submit` event `data`, and **returns them in its own
  response** — so the agent that submitted is told, immediately, "3 open redlines
  on this revision; they are shown in the queue."
- Every approval-queue row (`GET /api/approvals`, `pending_approvals()`) carries
  `markups: {open, total}` and the open titles, with `shot_url` where a picture
  exists. The queue row reads *"3 open redlines"*; the approver clicks through to
  the viewer.

That is the whole thing. It costs one grouped query on a queue that is
deliberately short enough to ship comments inline already (`plm/api.py:1631`).

### "Changes requested" is reject + markups. No fourth decision.

Do **not** add a `changes_requested` decision to `approvals`. The existing
`rejected` already means "back to DRAFT for more work" and `withdrawn` already
means "I looked — tell the bot to revise" (`docs/PLM_IDENTITY.md`). What was
missing was never a state; it was *where the specifics go*. Now they go in
markups.

The documented loop, which belongs in the MCP `instructions` SOP:

```
bot:    checkin(...) → submit_for_release(...)
Maintainer:  opens the revision in the viewer, draws 2 markups, then
        decide_approval(id, 'rejected', note='see markups 41, 42')
bot:    list_markups(number)                → 2 open, with coordinates
        get_markup(41)                      → "bring this face in 2 mm", p, n
        revise(); edit the .scad; put_file()
        resolve_markup(41, 'addressed', addressed_by_version='0.3.1')
        resolve_markup(42, 'addressed', addressed_by_version='0.3.1')
        submit_for_release(...)             → response says 0 open, 2 addressed
Maintainer:  sees "2 addressed" in the queue, checks them, resolves both, approves
```

`POST /api/approvals/{id}/decide` may optionally accept `markup_ids: []`, which
writes a context entry on each named markup citing the decision and puts the ids
in the `release.rejected` event. Nice, cheap, additive — and it is a convenience,
not the mechanism. The mechanism is that both objects hang off the same revision.

### Is a markup part of the approval record?

**No new join table.** The link is markup → revision → approval, and
`markups.created_at` versus `approvals.requested_at` already tells you whether a
redline was written *during* review (which is, by construction, review feedback).
The event log records both sides. If "the redlines that killed v0.4.0" ever needs
to be a first-class list, it is `markup_ids` in the `decide` event `data` plus a
context entry — additive, no schema change, and available the day it is wanted.

### The plm-git mapping (the maintainer's mental model, stated so nobody has to guess)

| GitHub | plm |
|---|---|
| pull request | a DRAFT revision + its pending `approvals` row |
| `git push` | `checkin` + `submit_for_release`, or a `refs/for/main` push (`plm-git`) |
| merge | a human `decide_approval('approved')` — **bots never approve** |
| review comment on a line | **a markup**: a note anchored to a place |
| "outdated" on a moved line | `applies: 'stale'` (§2) |
| "changes requested" | `rejected` + the markups that say what |
| resolving a review thread | `resolve_markup(id, 'resolved')` — the raiser's call |

### What this deliberately does NOT do

- **No assignment, ownership, due dates or severity.** A markup has an author, a
  state and a note. Everything else is project management, and plm is not one.
- **No notification.** "Text me when the maintainer redlines something" is one
  `integrations` row with `on_events='markup'` — the framework already exists
  (`plm/db.py:207`), and PLM stays PLM.
- **No editing an anchor.** Correcting a title or a note on an `open` markup is
  fine; moving where it points is a **new markup** (optionally `supersedes` the
  old). An anchor that can be edited is an anchor whose history is a lie.
- **No threading beyond context's flat, append-only log.** No replies-to-replies.
- **No markups on snippets.** Tempting — "do you like this?" / "no, bring this
  face in" is a natural exchange — but a snippet's bytes delete themselves, so
  the anchor dies with them, and the whole point of `docs/PLM_SNIPPETS.md` is
  that anything worth keeping is worth a number. The right move is: snippet →
  "yes, roughly" → `checkin` → markup.
- **No geometric interpretation.** plm records "offset this face 2 mm"; it never
  performs it. The fix happens in the `.scad`, by the agent, and lands as a
  normal revision.

---

## 6. Migration and cost

### Additive only

| Change | Where |
|---|---|
| `CREATE TABLE IF NOT EXISTS markups (…)` + three indexes | `db.SCHEMA` — a new table needs no `ALTER`; `executescript(SCHEMA)` runs it at every boot (`plm/db.py:294`) |
| `TARGETS['markup'] = {…}` + `_r_markup` resolver | `plm/context.py` — one dict entry, one function. Returns the markup's `(item_id, revision_id)` anchor, so FTS and events need **zero** new code |
| `TABLES += ["markups"]` | `plm/backup.py:62` — **non-negotiable.** Unlike `snippets` (deliberately excluded: "restoring a week-old disposable link is not a thing anyone wants"), a redline is primary data that exists **only** in the DB. Leaving it out is how the feature gets lost in a restore |
| markup titles + entity notes into the revision's indexed text | `plm/search.py:82` — ~6 lines in the `parts` list |
| nothing in `gc.py` | a markup is permanent. An orphaned filedrop object can only happen if `POST /shot` dies between the PUT and the row write; the staging janitor already sweeps the unclaimed stage, and the markup's own object is written only on success |

No `ALTER TABLE`, no data migration, no backfill. Every existing row keeps its
meaning because no existing row is touched.

### Keeping the hot payloads cheap — the `Presence` precedent

`plm/thumbs.py`'s `Presence` is the pattern: **one memo for the span of one
payload**, one field name with one meaning, and a cheap DB-only answer that
short-circuits. Mirror it.

**`get_item`** gains:

```python
m = markups.Counts(conn)          # ONE grouped query for the whole payload
for rev in item["revisions"]:
    rev["markups"] = m.for_rev(rev["id"])     # {"open": 2, "total": 5}
item["markups"] = m.open_rows(item["id"])     # title-only rows, typically 0–3
```

- **One query, not N.** `SELECT revision_id, state, COUNT(*) FROM markups WHERE
  item_id=? GROUP BY revision_id, state` over `idx_markups_item`. A 3-revision
  item costs one indexed read, same order as the 76 reads `Presence` costs on
  GL-ASM-0000.
- **`open_rows` ships `id`, `title`, `actor`, `created_at`, `state`, `applies`,
  `entity_count`, `shot_url` — and never `payload`.** The rule, stated so nobody
  relaxes it later: *the summary is a badge and a title; the geometry is a second
  call.* A 40 KB payload × 5 markups on a page nobody asked to redline is exactly
  the bloat that made `thumb` presence necessary in the first place.
- The `applies` verdict is one memoized lookup per markup:
  `SELECT 1 FROM files WHERE revision_id=? AND sha256=? LIMIT 1` over
  `idx_files_rev`, memoized on `(revision_id, sha256)` for the payload's span. It
  is computed only for markups actually being shipped — ~3 on a normal item.

**BOM rows get nothing.** A BOM line is an *occurrence of a child*; a markup is
not a property of an occurrence, and a count per line would be N extra queries
per page load for a signal nobody asked for. Explicitly rejected. If it is ever
wanted, it goes through a `Counts` memo like `Presence.bom()` does — never
per-row.

**`GET /api/approvals`** gains the same `Counts` object: one grouped query for
the whole queue.

---

## 7. The contract with the viewer

Specified here as what this side *needs*; the interaction design, tool palette
and capture flow are `docs/PLM_MARKUP_UX.md`'s. Five items, and the last three
are where it will go wrong if nobody says so:

1. **Per pick** — `file_id` (already in `/api/items/{n}/geometry`'s payload as
   `id`, `plm/api.py:789`), model-space `p` (this is exactly `vPickAt`'s `local`,
   `web/app.js:2185` — **pre**-`V.group` rotation), `n`, `tri`
   (`hit.faceIndex`, currently unused by the measure tool), and `snapped`.
   `hit.face.normal` is in **geometry** space and must be transformed by the
   mesh's normal matrix before it is a model-space normal — for these meshes
   that is identity, but write it down or the first assembly with a placement
   transform breaks it silently.

2. **A face patch and its area** must be computed viewer-side — only the viewer
   has the parsed geometry. **The trap:** `THREE.STLLoader` produces a
   **non-indexed** `BufferGeometry`, so `geometry.index` is `null` and a
   flood-fill over shared *indices* finds nothing. "Shared vertex" means
   position-equal within an epsilon. This is the single most likely place the
   viewer side ships something that silently returns a one-triangle "face".

3. **Camera pose** — `camera.position`, `controls.target`, `camera.up`,
   `camera.fov`, `camera.aspect`, plus `V.explode`, `V.xray` and which bodies
   were hidden (solo / per-body checkboxes). A redline drawn at 60% explode is
   unreadable at 0%, so the explode value is part of the pose, not a UI
   preference.

4. **The PNG.** `WebGLRenderer` is constructed **without
   `preserveDrawingBuffer`** (`web/app.js:1588`), so `canvas.toBlob()` /
   `toDataURL()` will usually return a blank image once the frame is composited.
   The capture must either turn that flag on or call `renderer.render()`
   immediately before the capture, in the same task. Then it goes through the
   staged flow — `POST /api/uploads` → `PUT` the bytes → `POST
   /api/markups/{id}/shot` with `{filedrop_key, sha256, size}` — **not** as a
   data URL in the create body. Same law as every other binary
   (`docs/PLM_UPLOADS.md`).

5. **A permalink.** The viewer is deliberately *not* a routed state any more —
   `#/item/N/view/V` was un-routed and now redirects to the item page
   (`/N` since stage 22's real paths, `web/routes.js` `fromHash`). A markup that cannot be linked to is a markup nobody
   cites. The data side hands out a stable integer `id`, so
   `/<n>/markup/<id>` is available the moment the UX side decides to
   reverse that; whether to is a UX call, and it needs to be made, not inherited.

---

## 8. Build order — what I would build first

Steps 1–4 have **no browser dependency** and are verifiable with `curl` and a
hand-written payload. That ordering is deliberate: if the drawing UI slips, the
feature still delivers *"the maintainer dictates a redline and an agent acts on the
coordinates."*

1. **`plm/markups.py` + the table.** The module (create / get / list / state, the
   `ENTITIES` registry, `validate`, the `applies` verdict, `Counts`),
   `db.SCHEMA`, `context.TARGETS['markup']`, `backup.TABLES`. **No surfaces
   yet.** This is where every decision in this document lives; get it reviewed
   before any pixel exists.
2. **HTTP: create / list / get / state / shot (POST + GET) / entities.** Then
   prove the anchoring verdicts for real: create a markup, `revise()`, re-read →
   `current` (files carried forward verbatim); `put_file` a new STL → `stale`;
   `remove_file` the old row → `gone`. **If that test does not pass, nothing
   after this step is worth building.**
3. **MCP: `list_markups`, `get_markup`, `resolve_markup`, `create_markup`, and
   the SOP paragraph in the `instructions` string.** This is the payoff, and it
   comes **before** the drawing UI: the moment an agent can read "bring this face
   in 2 mm at (12.5, 0, 8.0), normal +X", a markup written by hand-rolled `curl`
   already earns its keep — and it tells you whether the payload shape is right
   while it is still cheap to change.
4. **The read surfaces:** `get_item` counts + open rows, approvals counts,
   `search` indexing, the `submit_for_release` response line. Small, and this is
   what makes markups *findable* rather than merely storable.
5. **The viewer** (`docs/PLM_MARKUP_UX.md`): pick → entity, the four drawing
   tools, camera capture, screenshot upload, the markup list card on the item
   page and in the approval queue. The expensive, fiddly half — and correctly
   last, because by now the server contract is fixed and proven.
6. **Later, and only on demand:** `reanchor_markup` (explicit re-pick creating a
   new markup that `supersedes` the old), `markup` events wired into
   `integrations` for notification, a `region` → hole-pattern helper that turns a
   lassoed pad plus "3 off" into candidate coordinates.

---

## 9. What I deliberately reject

| Rejected | Why |
|---|---|
| **bbox-relative parametric anchors** | they *move* the point when the bbox changes while still looking valid. Silent wrongness is the one failure mode worse than losing the markup |
| **automatic re-projection onto a new mesh** | when the design changed *at the marked feature* — the reason the markup existed — the re-projected hit is a different feature and nothing in the record says so. `stale` loses nothing and lies about nothing |
| **an open markup blocking release** | a second automatic gate in front of a human gate breeds an override flag, and the override flag rots. Inform the approver instead (§5) |
| **a `changes_requested` approval decision** | `rejected` already means "back to DRAFT". What was missing was never a state, it was where the specifics go |
| **a markup↔revision join table for the approval record** | markup → revision → approval already connects them, and the timestamps already say whether a redline was review feedback |
| **the screenshot as a `files` row** | it would enter the vault, the git commit, every future version folder, and `supersede_inherited`'s retirement rule. Evidence attached to a comment is not product data |
| **`DELETE /api/markups/{id}`** | `obsolete` is the delete, and it keeps the trail. Consistent with append-only context and with suppression-over-removal on released structure |
| **markup counts on BOM lines** | N queries per page for a signal nobody asked for. A markup is not a property of an occurrence |
| **markups on snippets** | a snippet's bytes delete themselves, so the anchor dies with them. Snippet → "yes, roughly" → check-in → markup |
| **`thumb` as the field name for the screenshot** | `thumb` means "the render of this revision's geometry" everywhere in this service. Two meanings on one row is a guess a reader should never have to make |
| **plm performing the geometric edit** | plm records "offset this face 2 mm". The fix happens in the `.scad` and lands as a normal revision through the normal gate |

---

## 10. Open questions for the maintainer

1. **Does an open markup block release?** I recommend **no** — inform the
   approver, never refuse the submit (§5). One line either way; the difference is
   whether an override flag eventually exists.
2. **Who may `resolve`?** I recommend **the markup's author, or an approver** —
   the same rule `withdraw_approval` uses. Alternatives: approver-only (simpler,
   matches the gate) or anyone with `editor` (too loose — a bot would resolve its
   own homework).
3. **What does "highlight a face" mean when there is no flat face?** Designed as
   *point + normal is the anchor, the coplanar patch is the witness*. On a fillet
   there is no flat patch, and the honest answer there is a point plus a `region`
   loop. If "face" should mean *the CAD face* rather than *the coplanar
   tessellation patch*, that needs CAD that exports faces (STEP, not STL) — worth
   knowing whether that is on the horizon.
4. **Assembly composites versus parts.** A redline on an assembly's `viz` mesh
   goes `stale` on the next viz regeneration even though nothing moved (§2).
   Should the viewer *insist* that a markup whose entities all land on one child
   gets filed on the child?
5. **May a bot raise a markup?** I recommend **yes** (`create_markup`). It changes
   the feature's character — from "the maintainer's redlines" to a two-way review channel
   where an agent can put "this wall is 0.8 mm at (12, 0, 8)" in front of a human
   with the coordinate attached.
