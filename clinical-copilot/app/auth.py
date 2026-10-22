"""OAuth2 token acquisition against OpenEMR.

Simplification, stated honestly (see README "Known gaps"): ARCHITECTURE.md calls
for a `user/`-scoped token bound to the resident's own authenticated
authorization_code session. That flow needs a real browser redirect through a
logged-in OpenEMR session, which the Early Submission timeline didn't allow for.
This module instead uses the OAuth2 **password** grant (also `user/`-scoped,
also centrally authorized/audited the same way per architecture-audit.md
6.1-6.4) with a single configured service credential. It is not the resident's
own session token, so the "access ceiling no higher than what the resident
could reach directly" property ARCHITECTURE.md 1.3 relies on is NOT yet true
of this build -- that's an open item for the authorization_code migration.
"""

from __future__ import annotations

import threading
import time

import httpx

from app.config import Settings


class TokenAcquisitionError(RuntimeError):
    """Raised when OpenEMR rejects a token request. Message is safe to log
    but should not be echoed verbatim to a resident (see verification.py's
    failure-surfacing rule)."""


class OAuthTokenProvider:
    """Fetches and caches a bearer token, refreshing shortly before expiry."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self._settings = settings
        self._client = client or httpx.Client(
            timeout=settings.request_timeout_seconds, verify=settings.verify_tls
        )
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._allow_password_login: bool = True

    @classmethod
    def from_authorization_code_tokens(
        cls,
        settings: Settings,
        *,
        access_token: str,
        refresh_token: str | None,
        expires_in: float,
        client: httpx.Client | None = None,
    ) -> "OAuthTokenProvider":
        """Seeds a provider directly from an `authorization_code` token
        response (app/oauth_session.py) -- bound to whichever resident
        actually logged in, per ARCHITECTURE.md 1.3. Deliberately never
        calls `_login()`: if `refresh_token` is later exhausted/revoked,
        `get_token()` raises instead of falling back to password-grant --
        the caller's session is simply dead, never silently replaced by
        the shared service credential.
        """
        provider = cls(settings, client)
        provider._access_token = access_token
        provider._refresh_token = refresh_token
        provider._expires_at = time.monotonic() + float(expires_in)
        provider._allow_password_login = False
        return provider

    def get_token(self) -> str:
        with self._lock:
            if self._access_token and time.monotonic() < self._expires_at - 30:
                return self._access_token
            if self._refresh_token:
                try:
                    self._refresh()
                    return self._access_token  # type: ignore[return-value]
                except TokenAcquisitionError:
                    if not self._allow_password_login:
                        raise
                    pass  # fall through to a fresh password-grant login
            if not self._allow_password_login:
                raise TokenAcquisitionError(
                    "Resident session's token expired and could not be refreshed; a fresh login is required."
                )
            self._login()
            return self._access_token  # type: ignore[return-value]

    def _login(self) -> None:
        s = self._settings
        response = self._client.post(
            s.oauth_token_url,
            data={
                "grant_type": "password",
                "client_id": s.oauth_client_id,
                "client_secret": s.oauth_client_secret,
                "scope": s.oauth_scope,
                "user_role": "users",
                "username": s.oauth_username,
                "password": s.oauth_password,
            },
        )
        self._store_token_response(response)

    def _refresh(self) -> None:
        s = self._settings
        response = self._client.post(
            s.oauth_token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": s.oauth_client_id,
                "client_secret": s.oauth_client_secret,
                "refresh_token": self._refresh_token,
            },
        )
        self._store_token_response(response)

    def _store_token_response(self, response: httpx.Response) -> None:
        if response.status_code != 200:
            raise TokenAcquisitionError(
                f"OpenEMR token endpoint returned {response.status_code}: {response.text[:500]}"
            )
        body = response.json()
        self._access_token = body["access_token"]
        self._refresh_token = body.get("refresh_token", self._refresh_token)
        self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
