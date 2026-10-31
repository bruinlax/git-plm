# Disposable 3D snippets — "hey, do you like this?"

*2026-08-11. The maintainer's ask: "a disposable stl viewer 'snippet' — that lets a llm
just upload a temporary file through filedrop etc that gives a way to view the
stl in 3d… just for quick stuff — not a tracked part — like 'hey do you like
this' / basically like a 3d artifact."*

A **snippet** is a temporary STL an agent uploads to get back a link a human can
open and spin in 3D — the 3D equivalent of pasting a screenshot into chat.
`share_3d(name, content_b64, note, ttl_days)` on the plm MCP (or
`POST /api/snippets` for non-MCP callers) stores the bytes on plm's own disk
under `CACHE_DIR/snippets/<token>/<filename>` with an unguessable
`secrets.token_urlsafe(12)` token, writes one row in the `snippets` table
(token, name, note, key, sha256, size, actor, created_at, expires_at), and
returns `https://plm.example.com/snip/<token>` plus the expiry. That page shows
the filename, the note (the question being asked), the size and *when it
disappears*, and opens the existing three.js overlay — same explode / x-ray /
fit / dimensions readout the registry viewer has, fed by
`GET /api/snippets/<token>/blob`, which serves the local file with the same
headers `/api/blob` uses (`snippets.path`). A big mesh
arrives by reference — `url` (a filedrop MCP `get_url`) is fetched straight into
the snippet's directory, and a plm `upload()` handle's inbox file is MOVED
there (same disk, no copy). Critically it is **not a part**: no item, no revision, no `files` row, no
git commit, no approval — because a tracked part is a promise to keep the bytes
and their history forever, and most show-and-tell geometry does not deserve that
promise. Pushing "do you like this shape?" through create-item/checkin would
fill the registry with numbers nobody will ever order, and every future search
would pay for the noise. So: quick look → `share_3d()`; anything that should
still exist next month → `checkin()`. The MCP instructions say exactly that, and
the snippet page repeats it in plain English so a human can't mistake a snippet
for a record.

## Expiry and GC

TTL defaults to **7 days** and is **capped at 30** (a longer request is silently
clamped, never rejected; `ttl_seconds` exists as an internal/testing knob so the
janitor can be exercised in seconds). There is deliberately no "extend"
operation — if a snippet mattered enough to keep, it mattered enough to check in.

Expiry is enforced in two layers. **Reads check the clock**: once `expires_at`
passes, `/api/snippets/<token>` and `.../blob` return **410 Gone** with a
message that says it expired and to ask for a fresh one, whether or not the
bytes are still on disk. **A janitor then reclaims**: `snippets.start_janitor()`
runs from the API's `@app.on_event("startup")` (the same daemon-thread idiom as
the search warmer) — one sweep immediately, then every 6 hours
(`PLM_SNIPPET_GC_SECONDS`) — deleting the snippet's directory first and the
row second, so a failed delete leaves the row for the next pass to retry rather
than orphaning bytes forever. Because the sweep also runs on boot, nothing
survives its TTL just because the process was restarting when it fell due. After
collection the token reads **404**. `POST /api/snippets/gc` (and
`python -c "from plm import snippets; print(snippets.janitor_once())"`) runs a
sweep on demand; `DELETE /api/snippets/<token>` / `unshare_3d(token)` revokes
early. Snippet rows are intentionally **not** in `plm/backup.py`'s table list:
restoring a week-old disposable link is not a thing anyone wants.

## Access

`/api/snippets*` sits on the machine plane (`/api/` is in
`identity.PUBLIC_PREFIXES`), which is what lets an agent create one with no
session; the private network plus the unguessable token is the control. The human link
is the **path** `/snip/<token>`, not a `#/snip/<token>` fragment, for a specific reason: a
fragment never reaches the server, so the hash form would be dropped by the OIDC
login round-trip the first time the maintainer clicks a link on a new device. `/snip/` is
outside `PUBLIC_PREFIXES`, so it is OIDC-gated like every other human page, and
since stage 22 (real paths) it IS the page: the server serves the UI shell
there and the SPA takes over (`renderSnip` + `openSnipViewer` in `web/app.js`;
`/snip/<token>/info` is the card without the overlay, and where Close lands). An
old `/#/snip/<token>` link still lands — the router maps it. Default-private,
intentional.

Every instance's janitor collects only the snippets in **its own** database, so
prod and the sandbox can share the `plm` bucket safely — keys are token-scoped
and tokens are unique.
