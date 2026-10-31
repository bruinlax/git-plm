# 3D markup — the viewer half: interaction, ink, capture

*2026-08-13. The maintainer's ask: "the ability to Comment on a part — with the ability to
go into a 3d viewer of the stl and then highlight a face or add some points /
lines — e.g. 'bring this face in, add holes here' — and be able to select /
circle where things should go… and let us also screenshot the markup itself…
it'd have a thumb as well that could be added to the comment so you could
quickly see that I commented and what it was about."*

Two documents, one feature. **This one is the viewer**: what the tools feel like,
what the ink looks like, what the browser hands the server, how the screenshot is
made, and how a markup is re-opened honestly on a mesh that has moved on.
`docs/PLM_MARKUP.md` is the other half — the data model, the tables, the API
verbs, the anchoring policy. Where the two meet is §3, the payload; that section
is written as a contract and nothing else here may contradict it.

The one-line summary of the design: **a markup is a redline drawn in model
coordinates, plus the camera it was drawn from, plus a baked PNG of the result.**
The coordinates are the record, the camera is the courtesy, the PNG is the thumb.

---

## 1. What is already built (and is not being replaced)

The measure tool is the proof that this feature is mostly assembly rather than
invention. Everything below already ships in `web/app.js`:

| Need | What exists |
|---|---|
| Pick a point on a body | `vPickAt(px,py)` — raycast against *visible* meshes only, returns `{mesh, part, local, snapped}` |
| Mean the corner, not the sliver | `vSnapVertex` — nearest vertex within `V_SNAP_PX` (16 CSS px), world-radius pre-reject, surface-pick fallback |
| A point that rides explode | picks are stored `(mesh, local)`; `vPtWorld` pushes them back out through their own mesh |
| Click vs orbit | `vOnDown/vOnMove/vOnUp` + the 5 px travel test, listeners registered **before** OrbitControls so a grab can kill the orbit dead |
| Drag an existing mark | `vMarkerAt` (screen-space, `V_GRAB_PX`), `V.mDrag`, pointer capture, `vDragEnd(commit)` |
| One raycast per frame | `V.mAt` + `V.mNeed`, served by the render loop in `vInit` |
| Lines, labels, marks | `vLine` (with the occluded + ghosted double pass from `vAxes`), `vSprite`/`vSpriteSet` (constant screen size, dark halo), `vDot`/`vDotTex` (shared 2-entry texture cache) |
| Teardown without leaks | `vKill` — disposes our geometries and canvas textures, spares Sprite's singleton geometry and anything flagged `vShared` |
| Scene, framing, furniture | `vBegin`/`vBuild`/`vFit`/`vDispose`, `vAxes`, the mm graticule, explode / x-ray / solo |

Markup adds **one tool family, one panel section, one capture path, one restore
path**. It adds no second scene builder, no second raycaster, no second gesture
model, and no route (the viewer stopped being routed on 2026-08-12; markup does
not bring routing back — see §6).

Two facts about the geometry that the whole design leans on, both worth stating
because they are not obvious:

- **`STLLoader` produces a non-indexed triangle soup** (position + normal, three
  vertices per facet, no index buffer). So *facet index = `hit.faceIndex`*, which
  three.js r128 does set for non-indexed geometry (`Math.floor(n/3)` in
  `Mesh.raycast`), and facet `t` is the `t`-th facet **in the STL file** — a
  stable name for a triangle as long as the bytes are the same bytes.
- **`geo.computeVertexNormals()` in `vBuild` yields facet normals, not smoothed
  ones.** On non-indexed geometry vertices are unshared, so each triangle writes
  its own cross product into its three normals. That means `geo.attributes.normal`
  at index `3t` *is* facet `t`'s normal, for free, already computed — which is
  exactly what face-region growth needs. (It also means the shading is flat by
  construction, and that the file's own normals — which some exporters get
  wrong — were discarded on purpose.)

---

## 2. The ink: four tools, one active at a time

### 2.1 Point — "add a hole here"

Click. `vPickAt` gives the model coordinate, snapped to a vertex when the cursor
is within 16 px of one. The mark is drawn with `vDot` in redline (§2.6), gets the
next balloon number, and the panel scrolls its note field into view. Drag to
adjust (reuse `vMarkerAt` / `V.mDrag` verbatim, widened to search all entities'
handles rather than the measure tool's two). This is the measure tool's first
half with the second click removed, and it is the cheapest useful thing in the
feature.

### 2.2 Polyline — "trim to this line", "the crack runs like this"

Repeated picks. Between clicks the existing rubber band shows the segment you
would get; `Enter` or a double-click finishes, `Esc` pops the last vertex (not
the whole line — losing four good points to a fifth bad one is the failure mode
that makes people stop using a tool). Segments are drawn with `vLine` in the
occluded + ghosted double pass, so a line that runs behind the solid stays
readable instead of vanishing — the drawing-office answer to a hidden line, and
`vAxes` already proved it works here.

No per-segment dimension chips. The measure tool exists for numbers, and a
polyline with five chips on it is a pile, not a drawing. The panel shows the
total length for the selected polyline; that is enough.

### 2.3 Circle — "bring this face in, add a Ø12 boss here"

**A 3D disc placed on the surface, not a screen-space lasso.** Press, and the
pick point becomes the disc centre; its facet normal becomes the disc axis; drag
outward and the radius follows the cursor, in millimetres, live, in the HUD;
release to set. The gesture reuses `V.mDrag`'s pointer capture and
`controls.enabled = false` so the orbit never starts. It is drawn as a ring of
`vLine` segments plus a short normal stub from the surface to the ring plane,
offset off the face by 0.4 % of the part's extent for the same reason `vAxes`
nudges the triad off the bbox corner: coincident coplanar lines z-fight and
disappear.

Why the disc and not the lasso, argued properly, because the lasso is the thing
people ask for:

- **What the maintainer means by "circle where things should go" is a place and a size.**
  "Put a Ø12 clearance hole about here" is a point, a normal and a radius. The
  disc records exactly those three things and no more.
- **A lasso is a screen gesture with no 3D meaning of its own.** Its interior is
  a region of the *viewport*. To make it an entity you must either store screen
  coordinates plus the camera (in which case it is a doodle on a photograph, and
  orbiting one degree makes it a lie), or resolve it to the set of triangles
  whose projection falls inside it — which on a thin wall silently grabs the far
  side, and which cannot be honestly re-drawn on a mesh whose triangles changed.
- **The lasso's only unique power** — designating many disjoint triangles at once
  — is what the face tool does for the case that actually occurs (one face,
  possibly curved).
- A disc survives orbit, explode, a re-export and a re-anchor, because it is
  three numbers in CAD space. A lasso survives none of them.

So the lasso is **cut**, and if it ever comes back it comes back as a *derived*
facet set with a boundary loop in model space, never as viewport coordinates.

### 2.4 Face highlight — the marquee ask, and the hard one

A single triangle is useless to a human: on a 40k-facet STL one facet is a
fraction of a millimetre. So a face pick **grows a connected, near-coplanar
region** from the seed facet.

The mechanics, and each one's cost:

1. **Adjacency has to be built, because the STL has none.** A triangle soup does
   not know its neighbours; vertices are duplicated per facet and only
   *approximately* equal across a shared edge. So: quantize each vertex to a grid
   of `1e-4 × bbox diagonal`, hash it, and bucket facets by welded edge. Built
   lazily on the first face pick, cached beside `V.geos` under the same body URL
   key, thrown away with the scene.
2. **Growth is a BFS across welded edges with two gates.** Crossing an edge
   requires (a) the dihedral angle between the two facets ≤ `tol` (default 15°,
   a panel slider 1–45° that only appears while the face tool is active), and
   (b) the angle between the candidate facet and the **seed** facet ≤ a drift
   budget (default `3 × tol`). Gate (a) alone leaks: on a well-tessellated
   casting it will walk around a fillet and off across half the part. Gate (b)
   alone stops dead one facet into any curvature.
3. **On a flat face this is exact** — the region is the face, the boundary is the
   face's outline, done.
4. **On a curved surface it is a patch, and the UI says so.** A cylinder wall
   grows a band and stops when the drift budget runs out; the panel row reads
   `face · 62 facets · curved, 41° swept` and the HUD offers *press G to grow
   again* — another drift budget, one deliberate keystroke at a time. There is no
   silent flood fill of a bore, and a human who wants the whole bore knows after
   two presses that this tool is telling them the truth about the geometry.
5. **Caps, with an honest refusal.** Growth stops at 20 000 facets or 25 % of the
   body, whichever is smaller, and says which; adjacency is not built at all for
   bodies over ~300 000 facets (the maintainer prints DEM relief maps — `MAX_STL_BYTES` in
   `plm/thumbs.py` is 120 MB for exactly that reason). The face tool disables
   itself on such a body with the count in the tooltip, and the circle and point
   tools still work. A five-second freeze would be a worse answer than "not
   here".

**Rendering the region** without cloning a 40k-facet mesh: build one small
`BufferGeometry` of just the selected facets' vertices, copied out of the source
`position` attribute, with a translucent redline material (`polygonOffset: true`,
`depthWrite: false`) — and **add it as a child of the body's own mesh**. That one
decision buys explode, solo, per-body visibility and the Z-up→Y-up group
transform for free, because the ink is parented to the thing it describes. Then
draw the region's **boundary**: the edges owned by exactly one selected facet,
chained into loops, as `vLine` in the occluded + ghosted pass. Flat translucent
fill with a firm outline reads as redline over a print; a glow or a gradient
reads as a video game.

The boundary loop is not decoration — it is the only part of a face region that
can be re-drawn honestly on a changed mesh (§5), so it is computed here and
stored (§3).

**The fused `viz` blob is the genuine limitation.** `thumbs.geometry_bodies`
prefers a revision's own primary STL, and for an assembly that is usually a
single fused `viz` body. On that mesh "which part is this face on" has no answer,
and coplanar growth will happily cross from one part's face onto the touching
part's coplanar face. So on a single-body view the panel labels the entity with
the *body* name (the viz filename), never a part number, and the HUD says
`assembled view — one body, faces are surface patches`. The fix — a
`?bodies=occurrences` flag on `/api/items/{number}/geometry` that skips the
own-viz shortcut and returns the per-occurrence bodies — is a small backend ask
with a clear payoff, and it is **cut from v1**. Labelling honestly costs nothing;
silently claiming a part number would be the bug.

### 2.5 Notes, without a modal that hides the model

Every entity carries an optional text note, and the markup as a whole carries a
title and a body (the latter *is* the comment). None of it is entered in a
dialog, because the whole point of typing here is that the part is in front of
you.

The right-hand `#vpanel` is 300 px of column that is already beside the canvas
rather than over it. Markup mode gives it a **Markup** section: the title field,
then the numbered entity list. The selected entity's row expands **in place**
into a one-line-growing textarea; `Enter` commits and collapses, `Shift+Enter`
adds a line, `Esc` collapses without losing what was typed. Clicking a row
selects its entity in the scene (its balloon expands to show the note as a chip,
and the camera does *not* move — nothing is more disorienting than a list that
flies your view around); clicking the balloon in the scene selects the row. The
model never gets covered, and there is no state where the thing you are
describing is behind the thing you are typing into.

Balloon numbers are the whole reason this stays legible: **the ink carries a
number, the words live in the panel.** One `vSprite` per entity, at fixed screen
size, showing `1`, `2`, `3` — the drawing-office callout. Only the *selected*
entity renders its text as a chip. Fifteen text chips at once is an unreadable
pile and fifteen canvas textures being re-lettered on every edit; fifteen numbers
is a drawing.

### 2.6 The palette — redline over a print

Two vocabularies, never confused:

| Ink | Colour | Means |
|---|---|---|
| Measure | cream `#f4e7c4` / grey `#8d939b` (existing `V_SNAP_C` / `V_SURF_C`) | **a fact** — this is how big it is |
| Markup | redline `#d2503b`, fill same hue at 0.18 | **a request** — this should change |
| Stale / unanchored markup | the HUD's caveat amber `#c2b48a`, dashed, 0.7 opacity | **as drawn, not re-found** (§5) |
| Balloon chip | existing `V_CHIP` shape, border `#a63f2e` | the callout |

Markup points reuse `vDot` with a redline hex — one more entry in the shared
`V_DOT_TEX` cache, which is `vShared` and therefore correctly spared by `vKill`.
No new marker artwork: the surveyor's ring-crosshair-dot already has a centre you
can trust, and using a different mark for markup points would imply a difference
that does not exist.

---

## 3. The contract: what the viewer hands the backend

This is the section `docs/PLM_MARKUP.md` consumes. The shapes below are what the
browser can produce cheaply and correctly; the storage decisions are the other
document's.

### 3.1 Coordinate frames — store model space, and only model space

The viewer stacks three transforms:

```
world  ←  V.group (rotation.x = -π/2 ; position = (-c.x, -union.min.z, c.y))
       ←  mesh    (position = dir × explode/100)
       ←  geometry = the STL's own coordinates, mm, Z-up
```

`V.group`'s offset is derived from the **union bbox of whichever bodies happened
to load**, and `mesh.position` is the explode offset. Both are staging, not data:
add a BOM child, or re-export the assembly, and the group re-centres; move the
explode slider and every body slides. So:

> **Everything geometric is stored in the mesh's LOCAL frame — the STL's own mm
> Z-up coordinates.** That is what `vPickAt` already returns in `local` (the
> measure tool's readout is "as modelled" for the same reason), it is invariant to
> the group transform and to explode, and it is the frame a re-export preserves.
> World coordinates would encode the union bbox of one particular body list, which
> is the one thing guaranteed not to survive.

**The camera is stored in model space too**, by pushing `camera.position` and
`controls.target` through `V.group.worldToLocal`. Storing the camera in world
space would look right until a revision gained a part: the union bbox moves, the
group offset moves with it, and the same world camera now frames somewhere else.
`up` in model space is CAD `[0,0,1]` (the group applies rotation only, so this is
constant — record it anyway, so the restore is not relying on a fact that is only
true today).

### 3.2 The payload

One POST when the author saves. `body`/`title` are the comment; everything else
is the drawing.

```json
{
  "number": "KML-PRT-1012",
  "version": "0.3.0",
  "actor": "alice",
  "title": "bring this face in, add two holes",
  "body": "The carrier face is 2 mm proud of the shelf...",

  "view": {
    "frame": "model",
    "camera": { "position": [148.2, -96.4, 71.0], "target": [12.0, 8.5, 22.4],
                "up": [0, 0, 1], "fov": 45, "aspect": 1.63 },
    "explode": 0, "xray": false, "axes": true,
    "hidden_bodies": [1]
  },

  "bodies": [
    { "ord": 0, "number": "KML-PRT-1012", "name": "bottom insert carrier",
      "file_id": 8213, "sha256": "9f2c…", "path": "…/KML-PRT-1012_v0.3.0_carrier.stl",
      "role": "viz", "facets": 41822 },
    { "ord": 1, "number": "KML-PRT-1031", "…": "…" }
  ],

  "entities": [
    { "n": 1, "kind": "point", "body": 0,
      "p": [12.4, -3.1, 8.0], "tri": 20194, "normal": [0, 0, 1],
      "snapped": true, "note": "start the boss here" },

    { "n": 2, "kind": "polyline", "body": 0, "closed": false,
      "points": [ { "p": [0,0,0], "tri": 101, "normal": [0,0,1], "snapped": true },
                  { "p": [10,0,0], "tri": 233, "normal": [0,0,1], "snapped": false } ],
      "length_mm": 41.7, "note": "trim to this line" },

    { "n": 3, "kind": "circle", "body": 0,
      "p": [24.0, -3.1, 8.0], "normal": [0, 0, 1], "tri": 8891,
      "radius_mm": 6.0, "note": "Ø12 clearance, thru" },

    { "n": 4, "kind": "face", "body": 0,
      "seed_tri": 3310, "seed_p": [31.2, 4.0, 12.0], "normal": [1, 0, 0],
      "tol_deg": 15, "drift_deg": 45, "grow_passes": 1,
      "facet_count": 62, "area_mm2": 412.6, "curved": true, "swept_deg": 41,
      "boundary": [ [[31.2,0,0],[31.2,8,0], "…"] ],
      "note": "bring this face in 2 mm" }
  ],

  "capture": {
    "png":   { "handle": "staging/x7Kd…/markup.png",       "sha256": "…", "size": 612344, "w": 1600, "h": 981 },
    "thumb": { "handle": "staging/Qm2p…/markup-thumb.png", "sha256": "…", "size": 41210, "w": 560,  "h": 420 }
  }
}
```

### 3.3 The rules that make that payload trustworthy

- **`p` is the ink; `tri` is a claim.** Every entity carries its own model-space
  geometry, sufficient to draw it with no mesh at all. The triangle index is a
  *validated optimisation*: it is honoured only while the body's `sha256` matches,
  and it is never the sole source of a mark's position. Drawing from a triangle
  index on changed bytes is the silent-wrong-triangle failure, and it is the one
  outcome this design refuses outright.
- **A body is identified by `sha256` first, occurrence second.** `files.id`
  changes when a revision is created (revise copies file rows forward — see the
  supersession section of `docs/PLM_UPLOADS.md`), so the id is a convenience for
  the server, not an identity. `sha256` is what says *these are the same bytes*;
  the item number is what says *this is the same part in the structure* when the
  bytes have changed. Both travel; the geometry endpoint already returns both, so
  this costs no extra request.
- **`normal` is the facet normal**, read from `geo.attributes.normal` at `3t`
  (equivalently `hit.face.normal`, which `Mesh.raycast` computes geometrically).
  It is what makes a stale point re-findable at all: a coordinate plus a normal
  can be matched against a changed mesh; a bare coordinate cannot be told from
  the same coordinate on the other side of a wall.
- **A face region is stored as a recipe plus a boundary, never as a facet list.**
  Growth is a deterministic BFS ordered by facet index, so
  `{seed_tri, seed_p, normal, tol_deg, drift_deg, grow_passes}` re-derives exactly
  the same set on identical bytes — and `facet_count` + `area_mm2` are the
  checksum that proves it. A 20 000-int facet list would be 120 kB of JSON that is
  merely a cache of six numbers. If a re-grow on *matching* bytes yields a
  different count, that is a code-version difference and the panel says so out
  loud rather than quietly drawing a different patch. `boundary` (model-space
  loops) is the fallback ink for the non-matching case.
- **`hidden_bodies` is part of the view.** "What the author saw" includes what
  they had soloed away. Restoring the camera without restoring visibility puts the
  redline behind a part that was not there when it was drawn.
- **`explode` is part of the view.** An author who marks up at 80 % explode stores
  invariant model coordinates (correct) but captures an exploded picture. Restore
  at 0 % and the PNG and the scene disagree about where the ink is. Cheap to
  store; easy to forget; embarrassing when it happens.
- **The capture arrives as handles, never as bytes in the JSON.** See §4.3.

### 3.4 What the viewer needs back from a read

Two requirements only, both to match conventions plm already committed to:

- **`thumb` is a URL or `null`, and `null` means do not ask.** Same rule as
  `plm/thumbs.py`'s presence-is-data (a page of BOM rows firing doomed 404s is
  the bug that rule exists to prevent). A markup whose capture failed to upload
  is a legal markup — the ink is the record — so the comment row has to render
  without a picture. The existing `thumbImg` / `retryThumb` helpers handle the
  rest, including the "still uploading" case.
- **The capture is not a `files` row on the revision.** Push-back for the data
  model: revise carries file rows forward, so a screenshot of v0.3.0 would appear
  in v0.4.0's Files card and in the vault's git history forever. A markup capture
  is an attachment to a comment about one revision, owned by the markup row,
  deleted with it. Same reasoning as `plm/snippets.py`: not everything that has
  bytes deserves to be a controlled document.

---

## 4. The screenshot

### 4.1 `preserveDrawingBuffer` vs a readback, and why the answer is neither

`vInit` creates the renderer with `antialias: true` and default
`preserveDrawingBuffer: false`, and the render loop draws every frame the overlay
is open. Three ways to get pixels out:

1. **`preserveDrawingBuffer: true`** — pay a full-frame buffer copy on every
   frame, forever, on every part anybody views, for a feature used once per
   markup. **Rejected**; the viewer is the human's inspection tool and it should
   not get slower to enable a capture button.
2. **Render to a `WebGLRenderTarget` and `readRenderTargetPixels`** — deterministic,
   resolution-independent, and it is in the vendored r128. But a WebGL1 render
   target has **no MSAA** (r128's multisample target is WebGL2-only), and the
   obvious fix — supersample at 2× and downsample — **destroys the ink**:
   `LineBasicMaterial` ignores `linewidth`, so every redline is exactly one device
   pixel, and one pixel of a 2× buffer is half a pixel after downsampling. The
   part would come out smooth and the redline would come out as a ghost. **Rejected
   for a line-heavy drawing.**
3. **Render, then read the live canvas in the same task.** `V.renderer.render()`
   followed immediately by `canvas.toBlob(...)` — no yield between them, so the
   compositor has not cleared the buffer yet. This is the standard three.js
   capture idiom and it is reliable precisely *because* we control the render
   loop; the documented failure is capturing at some arbitrary later time. It
   keeps MSAA, keeps 1 px lines at 1 px, and costs nothing when unused. **Chosen.**

### 4.2 Resolution, and the devicePixelRatio trap

`vInit` sets `setPixelRatio(Math.min(devicePixelRatio, 2))`. On the maintainer's retina
laptop a 1400×860 CSS canvas has a 2800×1720 backing store — 4.8 Mpx; on a 1×
external monitor the same markup captures at 1.2 Mpx. Naively grabbing the canvas
makes **the artifact's size and legibility depend on which screen drew it**, which
is not a property a record is allowed to have.

So the capture pins its own pixel size:

```
capture:  setPixelRatio(target_long_edge / css_long_edge)   // long edge → 1600
          render()  →  toBlob('image/png')
restore:  setPixelRatio(Math.min(devicePixelRatio, 2))
```

Because `camera.aspect` derives from the CSS size, not the backing store, the
framing is bit-for-bit what was on screen — **the capture cannot surprise you by
reframing.** Long edge 1600 (≈ 1600×980 at the overlay's usual shape) is big
enough to read a balloon number and a Ø on a laptop, small enough to be one
attachment rather than a download.

**Sizes, measured expectations:** a dark flat-shaded render with a graticule and
a dozen marks compresses to roughly **250–700 kB** at 1600 px, up to ~1.2 MB for
a busy multi-body assembly with x-ray on. The 560×420 thumb is **30–60 kB**.
Well under `INLINE_MAX_HTTP` (16 MiB) — which is exactly the argument someone
will make for base64-ing it into the JSON, and it is still the wrong answer
(§4.3).

### 4.3 Two products from one render, and how they reach the server

**Annotations are baked in. That is the entire point** — the thumb has to make
"the maintainer commented, and it was about the shelf face" readable at 84×58 in the
dashboard feed. What gets baked:

- bodies as shaded, the grid, the axes/scale bar **if the author had them on** (the
  capture shows what the author saw), any committed measurement (it is part of
  what they meant), all markup ink and its balloon numbers;
- **not** the hover layer — call `vLiveApply(null)` first, a cursor is not ink;
- **not** the panel or the HUD (they are chrome, and the HUD is DOM anyway);
- **plus a burnt-in caption**, drawn onto the 2D canvas after the pixels land:
  `KML-PRT-1012 v0.3.0 · alice · 2026-08-13 · 4 marks`, bottom-left, mono, the
  panel's grey. A drawing that will be pasted into a chat window and read out of
  context needs a title block, or it is a rumour.

The **thumb** is the same pixels drawn into a 560×420 2D canvas — 560×420 because
that is `thumbs.SIZE`, and having one thumbnail geometry in the system is worth
more than a perfect fit — letterboxed on the viewer's own `#101314` so nothing is
cropped into a lie. It exists because ten markups on an item page at 600 kB each
is a 6 MB page load; `.feedrow .thumbbox` and `.revthumb` already `object-fit:
cover` a small box, so a small file is all they can use.

**A clean second capture without annotations is not worth it in v1.** The
revision already has one (`/api/items/{number}/thumb`, OpenSCAD, same 560×420),
and the viewer can reproduce a clean view on demand from the stored camera pose,
which is *better* than a stale baked one. Doubling the storage and the upload
failure surface for something rebuildable is the wrong trade. If a before/after
pair is ever wanted it is three lines — `ink.visible = false`, render, capture,
restore — so the decision can wait for someone to actually want it.

**Getting the bytes there — by reference, the way everything else in plm does it:**

```
1. sha256   crypto.subtle.digest('SHA-256', buf)        // https ⇒ secure context ⇒ available
2. mint     POST /api/uploads {name, size, sha256}      // plm/staging.py mint()
                → {put_url, handle}
3. PUT      fetch(put_path, {method:'PUT', body: buf})  // browser → plm's inbox, same origin
4. save     POST the markup with {handle, sha256, size} // staging.claim() compares against
                                                       // the sha256 the PUT measured
```

Twice, once per PNG. **Never `content_b64` in the JSON body** — not because 600 kB
would break anything, but because plm has exactly one intake road
(`docs/PLM_UPLOADS.md`) and a second one that exists only for the web UI is how a
service ends up with two of everything. The declared-then-verified hash is also
the only reason a capture can be trusted at all: the declared sha256 is held to
what the PUT measured when the bytes landed, and a mismatch keeps nothing.

The PUT goes to plm itself (`PUT /api/uploads/<token>`, the minted `put_path`), so
it is same-origin: no CORS preflight to arrange with another service. Send the
body as an `ArrayBuffer` rather than a `Blob` so fetch sets no `Content-Type`.
What the viewer needs from the backend is exactly that *one door that takes raw
bytes*.

---

## 5. Re-opening a markup, honestly

### 5.1 The restore order

`vBegin` → geometry for the target revision → `vBuild` → **then**, in this order,
because each step depends on the previous one's matrices:

1. view state: per-body visibility from `hidden_bodies`, `V.explode` + `vApplyExplode`,
   x-ray, axes;
2. camera: `V.group.localToWorld` on the stored model-space position and target,
   `fov`, `up`, then `controls.update()`. If the stored `aspect` differs from the
   window's by more than ~15 %, scale the camera distance by the same
   narrower-fov ratio `vFit` uses — otherwise a markup drawn on a wide monitor
   crops on a laptop, which is the bug `vFit`'s fov-aware framing was written to
   kill;
3. entities, each one anchor-checked (below).

### 5.2 Three anchor states, three ways of drawing

Per body, compare the stored `sha256` against the `sha256` of the body that
actually loaded (`/api/items/{number}/geometry` already returns it):

| State | Test | How it is drawn | What the panel says |
|---|---|---|---|
| **anchored** | `sha256` matches | full-strength redline; face regions re-grown from the recipe and checked against `facet_count` | `on this mesh` |
| **as drawn** | body present (same occurrence), `sha256` differs | amber, dashed, 0.7 opacity, balloon gets a `?`; points/circles at their stored coordinates; **face regions drawn as their stored `boundary` loop only — never as facets, never re-grown** | `body changed 9f2c… → 4b71… · shown as drawn, not re-found` |
| **orphaned** | occurrence not in this view | nothing drawn | greyed row: `body not in this revision` |

The middle row is the whole reason §3.3 insists every entity carries its own
coordinates. A stale anchor is drawn **as the author drew it**, in a colour that
says "this is a claim about an older mesh", and it is never re-projected onto a
triangle index that now names some other facet. The companion document decides
whether re-anchoring (snap to the nearest facet with a compatible normal within a
tolerance) is attempted at all; if it is, its result must arrive as a *third*
state with its own styling and its own words (`re-anchored, 0.4 mm moved`) —
because a re-found anchor and an original one being drawn identically is the same
lie in a nicer costume.

### 5.3 Saying which revision this is

Two places, both unmissable:

- **The HUD's second line** (`vHud` is rebuilt from state, so it is exactly where
  this belongs) in the caveat amber it already uses for hints:
  `markup #7 · drawn on v0.3.0 — you are looking at v0.4.0 · 3 of 4 anchors stale`.
- **The panel's Markup header**: the title, author, date, `drawn on v0.3.0`, and a
  button **Open v0.3.0** that rebuilds the scene at the authored revision (the
  geometry endpoint already takes `?version=`). One click to see the redline on
  the mesh it was actually drawn on — the escape hatch that makes the whole
  staleness story honest rather than merely apologetic.

**Opening a markup from the comment stream defaults to the revision it was drawn
on**, with a *Compare on v0.4.0* button offering the other reading. A markup is a
statement about one revision; showing it against a newer mesh by default would
silently relocate the redline, and the default should be the one that cannot
mislead. (When the author's revision *is* the current one, which is the common
case, none of this shows at all.)

---

## 6. Where it lives in the UI

### 6.1 Getting in

**From the item page**: the item-shot card's `.shotbar` has `View 3D`; it gains
**Comment in 3D**, gated by the same `selected.thumb` presence test (no
geometry ⇒ nothing to draw on ⇒ the button is disabled with the reason in its
tooltip, exactly as `View 3D` already does). It opens the viewer with markup mode
armed and the Point tool selected, because the first thing anybody does is drop a
mark.

**From inside the viewer**: the panel's Markup section has *Start markup*, and
`M` does the same. Somebody who came in to spin the part and then found something
wrong should not have to leave and come back.

**Not from a snippet.** `/snip/<token>` shares `vBuild`, but a snippet is a
self-deleting file with no revision to anchor to; markup there would be ink on
something designed to disappear. One `if` gates markup mode on having a revision,
and the snippet panel keeps saying what it says today: if it matters, check it
in.

### 6.2 The panel, without mode soup

The panel today mixes display toggles and a tool (`axes` and `measure` are both
checkboxes, but only one of them steals the click). Adding four more checkboxes
would be the soup. So `vPanel` gets restructured — reusing its own row/checkbox
idiom, not replaced:

```
VIEW      explode ▁▁▁▂▃  0%
          ☐ x-ray — check fits
          ☑ axes — X/Y/Z + scale
          [ Fit view ]

TOOLS     ◉ orbit        ← the default; the canvas is just the camera
          ○ measure — point to point
          ○ point — mark a spot
          ○ line — mark a path
          ○ face — highlight a region        tolerance ▁▁▃▁▁ 15°   (only while active)
          ○ circle — mark a place and a size

MARKUP    title  [ bring this face in…            ]
          1 ● point   start the boss here
          2 ● line    trim to this line
          3 ● circle  Ø12 clearance, thru      ← selected: expands to a textarea
          4 ● face    62 facets · curved
          [ Capture & save ]   [ Discard ]

BODIES    ☑ ▪ carrier.stl        (unchanged)
          ☑ ▪ shelf.stl
```

**Tools are a radio set, one active.** Measure becomes its first member: same
`vMeasureToggle` entry point, same behaviour, the checkbox becomes a radio. That
is the change that keeps this from becoming five independent modes that can all
be on at once. `V.aBox` / `V.mBox` (the panel's own references to its controls,
kept because the tools can also be driven from the keyboard) extend to the radio
group unchanged.

Display toggles stay display toggles — explode, x-ray and axes are orthogonal to
every tool and must remain usable *while* drawing. Marking up at 60 % explode and
then rotating to check a fit is the actual workflow, and the ink follows because
it is parented to the mesh (§2.4).

The HUD keeps one hint line per active tool, extending the `if`-chain `vHud`
already has for measure. Rebuilt from state, so a tool switch cannot leave a
stale instruction behind — that property is worth preserving deliberately.

### 6.3 Getting out, without losing ink

The Escape ladder gives up one thing at a time, which is the existing rule; it
grows two rungs: **drag → in-progress polyline vertex → selected entity → tool →
the room.** And one deliberate departure: **while a markup has unsaved marks,
neither Esc-on-the-room nor a backdrop click closes the viewer.** They toast
`4 marks unsaved — Capture & save, or Discard`. The existing behaviour is right
for a viewer you can reopen for free; it is wrong for the only copy of something
a human just drew.

### 6.4 In the comment stream

Context *is* comments (`plm/context.py`), comments already attach to anything in
a `TARGETS` registry, and the maintainer's ask — "a thumb… added to the comment so you
could quickly see that I commented and what it was about" — is a comment with a
picture. So there is **no separate markup feed to keep in sync**: a markup is a
row that a context comment points at (`target_type = 'markup'`), and the item
page's Context card renders such a row with its thumb inline:

```
┌──────────┐  v0.3.0   alice   2026-08-13   markup · 4 marks
│  [thumb] │  bring this face in, add two holes
└──────────┘  The carrier face is 2 mm proud of the shelf…
```

`.ctx` gains a thumb column (84×58, the `.feedrow .thumbbox` treatment, lazy,
`thumb: null` renders nothing and requests nothing). Clicking the thumb opens the
PNG in the existing lightbox — **a human on a phone reads the redline with no
WebGL at all**, which is a large part of what baking the annotations buys.
Clicking the row's *Open in 3D* opens the viewer with the markup restored at its
authored revision.

No dedicated Markups card. If a busy part's context log ever drowns in redlines,
the fix is a filter chip on the card, not a second list that can disagree with
the first.

---

## 7. Effort, risk, and what I would cut

### 7.1 Straightforward, because the primitives exist

Point, polyline, circle, notes, the entity list, balloon numbers, camera
store/restore, the capture, both uploads, the panel restructure, the HUD hints,
the Esc ladder, the comment-stream thumb. Roughly **450–600 lines** in
`web/app.js` — one new section of about the size the measure tool already is —
plus ~40 lines of CSS and the `vPanel` rearrangement. Everything in it is a
recombination of `vPickAt`, `vLine`, `vSprite`, `vDot`, `V.mDrag` and `vKill`.

One teardown obligation, easy to forget and expensive to forget: **markup ink must
be torn down through `vKill` from `vDispose`**, or every reopen leaks a few
hundred kB of canvas textures. (`vKill` already gets the shared-texture and
sprite-geometry rules right; the only requirement is to call it.)

### 7.2 Genuinely hard

1. **Face-region growth on a big mesh.** The adjacency build is the cost: a
   500k-facet relief map is 1.5 M quantized vertex hashes, hundreds of
   milliseconds and ~100 MB of JS `Map`. Hence lazy, cached, and refused above
   ~300k facets. The subtler failure is the **weld tolerance**: quantize too fine
   and growth stops at a tessellation seam (a highlighted face that ends in the
   middle of nowhere for no visible reason); too coarse and it welds across a thin
   gap. Expect to tune, and make a single-facet result say `no connected coplanar
   region — mesh may be unwelded here` instead of highlighting a sliver.
2. **The fused `viz` blob** (§2.4). Not solvable in the viewer; solvable in one
   backend flag; labelled honestly meanwhile.
3. **Curved surfaces.** The drift budget is a judgement call disguised as a
   number. It will be re-tuned after the first real part.
4. **Sprite/label density.** Handled by numbered balloons plus text-in-the-panel;
   what is *not* handled is balloon-on-balloon collision at certain angles.
   Collision avoidance is real work for small benefit — cut, and note that
   balloons are at fixed screen size so orbiting a few degrees separates them.
5. ~~**Browser → filedrop CORS** (§4.3).~~ Moot: the capture PUT goes to plm's
   own inbox, same origin.
6. **Touch.** The gestures work (the 5 px threshold, pointer capture and
   `pointercancel` are already handled), but drawing a polyline on glass with a
   finger is unloved. Not cut, not polished.

### 7.3 Cut from v1, deliberately

- **The screen-space lasso** (§2.3) — the disc says the same thing and survives.
- **A second clean capture** (§4.3) — rebuildable from the pose.
- **Freehand ink.** It is a doodle with no model coordinates; the polyline is the
  disciplined version of the same gesture.
- **Editing an existing markup's ink.** Markups are **immutable, like every other
  context row** — delete-and-redraw, or reply with a new one. That is consistent
  with the append-only doctrine, it means the baked PNG can never disagree with
  the entities, and it removes an entire anchor-editing UI. (Delete stays: the
  markup owns its PNGs, so deletion is clean.)
- **Per-occurrence body override** on assemblies with a `viz` blob.
- **Per-segment dimension chips**, balloon collision avoidance, markup on
  snippets.

### 7.4 The smallest useful version

**v1 — Point, Line, Circle, notes, capture, restore.** No face tool. Full
payload including `bodies[].sha256` and the model-space camera; the capture and
both uploads; the comment row with its thumb; re-open with the camera and the
three anchor states. This is the version the maintainer can use to say "add a hole here,
Ø12, and trim to this line", and it is a few days rather than a week because the
face tool is where all the adjacency work lives.

**v1.1 — the face tool.** Adjacency, growth with the two gates, the tolerance
slider, `press G to grow again`, the boundary loop, the facet cap, the honest
fused-body labelling.

**v1.2 — polish with evidence.** Whatever the first ten real markups prove is
missing: probably the per-occurrence body flag, possibly re-anchoring with its
own third anchor state, possibly a before/after capture pair.

The ordering rule behind that split: **v1 ships the contract, v1.1 ships the hard
tool.** Every coordinate frame, every anchor state and the entire payload shape
are settled by the point tool alone — so they get settled and used early, on
something small, rather than discovered late underneath the one feature most
likely to need rework.
