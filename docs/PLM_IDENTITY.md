# Identity and the release gate

*2026-08-11. Per the maintainer: "versioning should be a release process… approvals
built in for users like me… users should be like me or bot — and really should
be driven by our OIDC."*

## The release process (built)

Versions are minted by workflow, not by fiat:

```
 DRAFT ──submit──▶ IN_REVIEW ──approve──▶ PROTOTYPE / RELEASED
   ▲    (freezes)      │       (the workflow runs; the
   │                   │        version becomes real)
   └──reject───────────┤
   └──withdraw─────────┘  (submitter or approver pulls it back:
                           "I looked — tell the bot to revise")
```

- **DRAFT** is the only mutable state (working uploads, BOM edits).
- **Submit** freezes the revision at IN_REVIEW and opens an `approvals` row
  with a target state and a note for the approver.
- **Approve** runs the workflow: the revision moves to its target state and is
  now a released record. **Reject** sends it back to DRAFT with a note.
- Every step is an event, and `submit` / `release` / `release.rejected`
  dispatch to integration webhooks — so "text the maintainer when something needs
  approval" is one integration row away.

## Users (built) and OIDC (the wiring plan)

`users`: username, kind (**human | bot**), roles (`approver`, `editor`), and a
`sub` column reserved for the OIDC subject. Seeded: `admin` (human, the `PLM_ADMIN` name;
approver+editor), `agent` / `mcp` (bots, editor), `import` (bot). Bots can do
all the legwork — revise, upload, submit — but **deciding an approval requires
the approver role**, which no bot has unless explicitly granted.

## OIDC — BUILT 2026-08-11 (`plm/identity.py`)

**Status: humans are done; bots and the UI cleanup are not.** 
1. **Humans — DONE, but not the way this doc first planned it.** The plan below
   was to front plm with an OIDC gatekeeper / oauth2-proxy that passes
   `X-Auth-Request-User`. ***That proxy does not exist.*** It is Phase 2 of the
   unbuilt Phase 2 of the operator's auth plan; consolidated services
   instead do **in-app authorization-code** against
   `auth.example.com`, resolving identity at **`/oauth/userinfo`** — access
   tokens are opaque, so there is no JWT to validate locally.

   So plm does that too: `/auth/login` → provider → `/auth/callback` →
   server-side session. A verified identity maps to `users.sub` (matched on
   `sub`, then username, so the seeded `admin` row *adopts* its subject and
   keeps its `approver` role), auto-provisioning unknown humans as
   `kind=human` with **no roles**. One middleware overrides any
   client-supplied `actor` with the verified username, and `decide`/`withdraw`
   **refuse** a body-only actor outright.

   The trusted-header path *is* implemented (`PLM_OIDC_TRUSTED_HEADERS=1`) so
   that if the Phase 2 proxy ever lands, adopting it is configuration rather
   than code. It is off by default: a forgeable header must never be a
   credential.

   Switchover switch: `PLM_REQUIRE_OIDC_APPROVALS`, defaulting to on whenever
   OIDC is configured — so the sandbox and local dev, which have no client
   credentials, keep the old trusted-username behavior untouched.

   Critical gotcha, learned the hard way and worth repeating: the
   authorization server does **not** enforce a client's `required_group` (it
   only filters portal tiles), and the `groups` claim is **scope-gated**. Every
   service must request `groups` *and* re-check membership itself. plm does,
   in `identity.user_allowed()`.

2. **Bots — NOT DONE.** Still as before: body `actor` on the private network, able to
   do all the legwork, never able to decide an approval. The intended shape is
   unchanged: service tokens introspected against comms-auth, exactly how
   photos-mcp accepts `COMMS_SERVICE_TOKEN` today, resolving to a bot user row
   with roles in the users table — so granting a bot `approver` stays a
   deliberate, auditable row change and not a config file.
   (`identity.identity_of()` already accepts a provider access token as a
   Bearer, which is the hook this will hang from.)

3. **UI cleanup — NOT DONE.** web still sends `actor` from JavaScript. The
   server now ignores it whenever a session exists, so this is cosmetic, but
   the field should come out.
