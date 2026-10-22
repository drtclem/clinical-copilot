"""Resident-scoped `authorization_code` session management (Phase 1 of
CLAUDE_CODE_BUILD_INSTRUCTIONS.md), replacing the shared password-grant
credential for live `/chat`/`/ui` traffic with a token bound to whichever
resident actually logs in via OpenEMR's own login page, per ARCHITECTURE.md
1.3.

State is in-memory, single-process -- the same simplicity level as the
existing `_conversations` dict in app/main.py (see that module's docstring):
fine for this demo, not for more than one server instance. A session dies
with the process; the resident just logs in again.

PKCE (S256 only -- OpenEMR's CustomAuthCodeGrant forbids the `plain`
method) uses only stdlib primitives (`secrets`, `hashlib`, `base64`), not a
hand-rolled cipher -- the standard construction from RFC 7636.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from app.auth import OAuthTokenProvider, TokenAcquisitionError
from app.config import Settings
from app.fhir_client import FhirClient

_PENDING_LOGIN_TTL_S = 600  # generous enough for a real login page interaction
SESSION_COOKIE_NAME = "copilot_session"

_DEFAULT_NEXT_URL = "/ui"
_ALLOWED_NEXT_PATH = "/ui"
_MAX_NEXT_URL_LENGTH = 2048


def sanitize_next_url(next_url: str) -> str:
    """The post-login redirect target: `/ui`, optionally with a query string.

    `next` arrives on GET /login from whoever built the link, and is
    redirected to verbatim once the resident has really authenticated -- so
    an absolute URL, a scheme-only `https:host`, or a protocol-relative
    `//host` would bounce a freshly-logged-in resident to an attacker's page.

    Allow-list, and deliberately narrower than "any same-origin path": the
    only page anything here ever sends a resident back to is the chat page
    (`/ui?patient=...`), so that is the only path accepted. Anything else
    -- another route on this app, `/ui/`, `/ui#x`, a value with a control
    character or over the length cap -- falls back to `/ui` rather than
    erroring: the resident still gets logged in, and a crafted link learns
    nothing from the response. Because the kept value is always exactly
    `/ui` followed by nothing or `?...`, its origin cannot differ from this
    app's, by construction; there is no scheme, authority, or backslash/tab
    normalization case to enumerate (browsers rewrite `\\` to `/` and strip
    tab/CR/LF, which is what makes deny-lists over `//` and `http:` leaky).
    Widen `_ALLOWED_NEXT_PATH` deliberately if a second landing page is
    ever added.
    """
    path, _, _query = next_url.partition("?")
    if (
        path != _ALLOWED_NEXT_PATH
        or len(next_url) > _MAX_NEXT_URL_LENGTH
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in next_url)
    ):
        return _DEFAULT_NEXT_URL
    return next_url


class LoginError(RuntimeError):
    """Raised for any failure in the authorization_code exchange -- message
    is safe to log, not to echo verbatim to the browser (app/auth.py's same
    failure-surfacing rule)."""


@dataclass
class _PendingLogin:
    code_verifier: str
    next_url: str
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class ResidentSession:
    session_id: str
    tokens: OAuthTokenProvider
    fhir: FhirClient
    resident_username: str | None
    created_at: float = field(default_factory=time.monotonic)


class SessionStore:
    """Owns pending-login state (the PKCE verifier between /login and
    /callback) and established resident sessions (the cookie -> token
    mapping /chat and /ui rely on)."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self._settings = settings
        self._client = client or httpx.Client(
            timeout=settings.request_timeout_seconds, verify=settings.verify_tls
        )
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingLogin] = {}
        self._sessions: dict[str, ResidentSession] = {}

    def build_authorize_url(self, next_url: str) -> str:
        """Starts a login: generates PKCE + state, stores the pending
        verifier, returns the URL to redirect the browser to. `next_url` is
        sanitized here -- the one place it enters the store -- so nothing
        unvalidated can ever come back out of complete_login()."""
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        state = secrets.token_urlsafe(32)

        with self._lock:
            self._evict_expired_pending()
            self._pending[state] = _PendingLogin(
                code_verifier=code_verifier, next_url=sanitize_next_url(next_url)
            )

        s = self._settings
        params = {
            "response_type": "code",
            "client_id": s.oauth_client_id,
            "redirect_uri": f"{s.copilot_base_url}/callback",
            "scope": s.oauth_scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{s.oauth_authorize_url}?{urlencode(params)}"

    def complete_login(self, *, code: str, state: str) -> tuple[str, str]:
        """Exchanges `code` for tokens, creates a session. Returns
        (session_id, next_url) for the caller to set the cookie and
        redirect. Raises LoginError on any failure -- an unrecognized/
        expired `state` (CSRF or a stale link) or a rejected exchange."""
        with self._lock:
            self._evict_expired_pending()
            pending = self._pending.pop(state, None)
        if pending is None:
            raise LoginError("Unrecognized or expired login attempt (bad/stale state).")

        s = self._settings
        response = self._client.post(
            s.oauth_token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{s.copilot_base_url}/callback",
                "client_id": s.oauth_client_id,
                "client_secret": s.oauth_client_secret,
                "code_verifier": pending.code_verifier,
            },
        )
        if response.status_code != 200:
            raise LoginError(
                f"OpenEMR token endpoint rejected the authorization_code exchange: "
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
        body = response.json()

        tokens = OAuthTokenProvider.from_authorization_code_tokens(
            s,
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=float(body.get("expires_in", 3600)),
            client=self._client,
        )
        resident_username = self._extract_username(body)
        fhir = FhirClient(s, tokens, client=self._client)

        session_id = str(uuid.uuid4())
        with self._lock:
            self._sessions[session_id] = ResidentSession(
                session_id=session_id, tokens=tokens, fhir=fhir, resident_username=resident_username
            )
        return session_id, pending.next_url

    def get(self, session_id: str | None) -> ResidentSession | None:
        """Returns the session only if its token is still live/refreshable
        -- a dead session (refresh exhausted/revoked) is treated the same
        as no session at all, never silently kept around with a stale
        token (app/auth.py's from_authorization_code_tokens never falls
        back to the shared credential, so a dead session truly can't fetch
        anything -- there's no reason to keep it)."""
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return None
        try:
            session.tokens.get_token()
        except TokenAcquisitionError:
            with self._lock:
                self._sessions.pop(session_id, None)
            return None
        return session

    def _evict_expired_pending(self) -> None:
        now = time.monotonic()
        expired = [s for s, p in self._pending.items() if now - p.created_at > _PENDING_LOGIN_TTL_S]
        for s in expired:
            del self._pending[s]

    @staticmethod
    def _extract_username(token_response: dict) -> str | None:
        """Best-effort resident identity for display (`GET /me`) only --
        decodes the `id_token`'s payload claims WITHOUT verifying its
        signature. Never used for authorization: the bearer token itself
        (not this string) is what every FHIR call is scoped by, so a
        forged/garbled id_token can make `/me` show the wrong name, never
        widen what data is reachable."""
        id_token = token_response.get("id_token")
        if not id_token or id_token.count(".") != 2:
            return None
        try:
            payload_b64 = id_token.split(".")[1]
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded))
        except (ValueError, TypeError):
            return None
        return claims.get("preferred_username") or claims.get("sub")
