# Bruno collection -- auth notes

Read this before running anything under `chat/` or `patient/`. Everything in
those two folders requires a real, logged-in OpenEMR session -- there is no
more anonymous/shared-credential access (Phase 1,
`../CLAUDE_CODE_BUILD_INSTRUCTIONS.md`). A saved request with no valid
session cookie gets a clean 401, not data:

```
{"detail": "Not logged in. Open /login (or /ui, which will prompt) to start an OpenEMR session first."}
```

`health/` and `auth/01 Start Login` and `auth/02 Callback` don't need a
session (the latter two can't succeed standalone regardless -- see their own
`docs` for why).

## Getting a session cookie into Bruno

This app's login is a real `authorization_code` OAuth round trip through
OpenEMR's own sign-in + consent screens -- there is no way to script or
curl your way through it end to end (the consent screen's CSRF token and
the login form's `user_role` field are enough to make a raw HTTP client
painful, and OpenEMR's own state/PKCE handling expects a real browser
session throughout). The practical path is: log in with a real browser
once, copy the cookie it gets, and reuse that cookie in Bruno.

1. Open `<base_url>/login` in an actual browser (not Bruno) --
   `http://localhost:8420/login` for `local`,
   `http://<deployed-host>:8420/login` for `droplet`. It redirects to
   OpenEMR's sign-in page.
2. Log in with a real account (`test_clinician` -- see the root `README.md` for
   the current credential -- or any real resident account), then accept the
   consent screen.
3. You land on `/ui`, now logged in. Open your browser's dev tools ->
   Application (Chrome) or Storage (Firefox) -> Cookies -> the app's origin,
   and copy the value of the `copilot_session` cookie.
4. In Bruno, open the environment you're using (`local` or `droplet`, top
   right of the Bruno window) and paste that value into the `copilot_session`
   variable. **This variable is declared `vars:secret` (2026-09-24)** -- it
   shows as a masked input in Bruno's UI, and Bruno stores the value in its
   own local secret store, never in this `.bru` file, so a real cookie
   never ends up committed to the repo even by accident. Every request in
   `chat/` and `patient/` already sends `Cookie: copilot_session=
   {{copilot_session}}` -- no per-request edits needed.
5. Run `auth/03 Me` to confirm it took: `{"authenticated": true, "resident":
   "<uuid>"}` means you're set. `{"authenticated": false}` means the cookie
   is missing, stale, or wasn't saved to the right environment.

**The session is server-side, in-memory, single-process** (`app/main.py`) --
it does not survive a restart/redeploy of the service, and it can also just
expire on its own. If requests that were working start 401ing again, that's
almost always this: re-run `auth/03 Me` first, and if it now shows
`authenticated: false`, repeat steps 1-4 to get a fresh cookie rather than
assuming something else broke.

## Requests added 2026-09-20

`auth/` and `patient/` didn't exist in this collection before tonight --
`/chat` was the only real request here, and its 7 saved scenarios predate
Phase 1's login requirement entirely (they were built against an earlier,
anonymous-access version of `/chat`, which is why they needed the
`copilot_session` header added along with this note). `/login`, `/callback`,
`/me`, and `/patient` all exist in `app/main.py` and are real, working
endpoints on the live droplet as of 2026-09-20 -- they just never had saved
requests here.

| Folder | Request | Needs `copilot_session`? |
|---|---|---|
| `auth/` | `01 Start Login` | No |
| `auth/` | `02 Callback` | No (can't succeed standalone either way -- see its own docs) |
| `auth/` | `03 Me` | Optional -- tests both the logged-in and logged-out response shapes depending on whether it's set |
| `patient/` | `01 Patient Header (pid1)` | Yes |
| `chat/` | `01`-`07` | Yes |
| `feedback/` | `01 Submit Feedback (pid1)` | Yes -- added 2026-09-20, closing a same-night gap: `POST /feedback` shipped this same night but never got a saved request here until now |
| `documents/` | `01 Upload Document (pid1)` | Yes -- added 2026-09-24: `POST /documents`, the one-shot ingestion entry point (real OCR + real Anthropic structuring). There's no in-UI upload control (`/ui`'s `chat.html` has no file input), so this is currently the only way to exercise this path outside a live demo click-through. Set the environment's `upload_patient_id` variable and the request's `doc_type` field before running -- see its own `docs`. |

## How long a session cookie lasts

The cookie itself has no independent expiry -- it's a key into an in-memory
map (`app/main.py`'s `_sessions`) pointing at your real OAuth access +
refresh tokens, auto-refreshed on each use as long as the refresh token
stays valid (`app/auth.py`'s `OAuthTokenProvider`, access tokens are
short-lived, ~1 hour, refreshed transparently). In practice that means
hours at minimum, likely longer, bounded by OpenEMR's own refresh-token
TTL, not anything this app sets.

**The one hard cutoff: any restart or redeploy of the service wipes every
session instantly**, cookie value and all -- `_sessions` is in-process
memory, nothing is persisted. If you get a cookie, then the service gets
redeployed for any reason, that cookie is dead even though it hasn't
"expired" in the normal sense -- `auth/03 Me` will flip from
`{"authenticated": true, ...}` to `{"authenticated": false}` with no
warning. Get your cookie *after* any planned deploy, not before, or budget
for repeating steps 1-4 above once more after it lands.
