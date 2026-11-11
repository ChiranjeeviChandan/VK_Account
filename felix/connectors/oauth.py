"""OAuth 2.0 sign-in for Google and Microsoft accounts.

Google uses the installed-app loopback flow with PKCE; Microsoft uses the
device-code flow. Tokens are kept in the secret store and refreshed on demand.
Felix ships no client credentials: the owner registers their own OAuth client
(docs/SETUP.md), so tokens are issued to them and nobody else.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets as pysecrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

from ..util import bind_without_name_lookup

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

log = logging.getLogger("felix.oauth")

GOOGLE = {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "scopes": {
        "mail": "https://mail.google.com/",
        "calendar": "https://www.googleapis.com/auth/calendar",
    },
}
MICROSOFT = {
    "device_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode",
    "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    "scopes": {
        "mail": "https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send",
    },
}


class OAuthError(Exception):
    pass


class NeedsSignIn(OAuthError):
    """No usable token: the owner has to sign in (again)."""


def xoauth2(user: str, token: str) -> bytes:
    """SASL XOAUTH2 initial response used by IMAP and SMTP."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode("utf-8")


class OAuth:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._lock = threading.RLock()
        # Overridable so tests can point at a local fake identity provider.
        self.google = dict(GOOGLE)
        self.microsoft = dict(MICROSOFT)

    # -- storage -------------------------------------------------------------
    def _key(self, provider: str, account: str) -> str:
        return f"oauth:{provider}:{account}"

    def _load(self, provider: str, account: str) -> dict[str, Any] | None:
        raw = self.app.secrets.get(self._key(provider, account))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _save(self, provider: str, account: str, token: dict[str, Any]) -> None:
        self.app.secrets.set(self._key(provider, account), json.dumps(token))

    def client(self, provider: str) -> dict[str, str]:
        raw = self.app.secrets.get(f"oauth:{provider}:client")
        if not raw:
            raise NeedsSignIn(
                f"No {provider.title()} OAuth client is configured. Run: felix connect {provider}"
            )
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise OAuthError(f"The stored {provider} OAuth client is not valid JSON") from exc

    def set_client(self, provider: str, client_id: str, client_secret: str = "", tenant: str = "common") -> None:
        self.app.secrets.set(f"oauth:{provider}:client", json.dumps(
            {"client_id": client_id.strip(), "client_secret": client_secret.strip(), "tenant": tenant.strip() or "common"}))

    def signed_in(self, provider: str, account: str) -> bool:
        token = self._load(provider, account)
        return bool(token and token.get("refresh_token"))

    def sign_out(self, provider: str, account: str) -> None:
        self.app.secrets.delete(self._key(provider, account))

    # -- access tokens ---------------------------------------------------------
    def access_token(self, provider: str, account: str) -> str:
        """A valid access token, refreshed if needed. Raises NeedsSignIn."""
        with self._lock:
            token = self._load(provider, account)
            if not token:
                raise NeedsSignIn(f"Sign in to {provider.title()} first: felix connect {provider}")
            if token.get("access_token") and token.get("expires_at", 0) - 90 > time.time():
                return token["access_token"]
            if not token.get("refresh_token"):
                raise NeedsSignIn(f"The {provider.title()} sign-in expired. Run: felix connect {provider}")
            if not self.app.net.online:
                raise OAuthError("Felix is offline and the saved sign-in needs refreshing.")
            client = self.client(provider)
            fields = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                      "client_id": client["client_id"]}
            if provider == "google":
                url = self.google["token_url"]
                if client.get("client_secret"):
                    fields["client_secret"] = client["client_secret"]
            else:
                url = self.microsoft["token_url"].format(tenant=client.get("tenant") or "common")
                if token.get("scope"):
                    fields["scope"] = token["scope"]
            try:
                fresh = _post_form(url, fields)
            except OAuthError as exc:
                if "invalid_grant" in str(exc):
                    self.sign_out(provider, account)
                    raise NeedsSignIn(
                        f"{provider.title()} no longer accepts the saved sign-in. Run: felix connect {provider}") from exc
                raise
            token.update(_normalise(fresh, previous=token))
            self._save(provider, account, token)
            return token["access_token"]

    # -- Google: loopback + PKCE -------------------------------------------------
    def google_sign_in(self, account: str, scopes: list[str], open_browser: bool = True,
                       timeout: float = 300, on_url=None) -> dict[str, Any]:
        """Run the browser sign-in. Blocks until the redirect arrives."""
        client = self.client("google")
        verifier = pysecrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = pysecrets.token_urlsafe(24)
        result: dict[str, Any] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if query.get("state", [""])[0] != state:
                    self.send_error(400, "state mismatch")
                    return
                result["code"] = query.get("code", [""])[0]
                result["error"] = query.get("error", [""])[0]
                body = (b"<html><body style='font-family:sans-serif;padding:3em'>"
                        b"<h2>Felix is connected.</h2><p>You can close this tab.</p></body></html>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        class Loopback(http.server.HTTPServer):
            def server_bind(self) -> None:
                bind_without_name_lookup(self)

        server = Loopback(("127.0.0.1", 0), Handler)
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_address[1]}"
        url = self.google["auth_url"] + "?" + urllib.parse.urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(scopes), "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent", "state": state, "login_hint": account,
        })
        if on_url:
            on_url(url)
        if open_browser:
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:
                pass
        deadline = time.time() + timeout
        try:
            while "code" not in result and time.time() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if result.get("error"):
            raise OAuthError(f"Google sign-in was refused: {result['error']}")
        if not result.get("code"):
            raise OAuthError("Google sign-in timed out.")
        fields = {"grant_type": "authorization_code", "code": result["code"], "code_verifier": verifier,
                  "client_id": client["client_id"], "redirect_uri": redirect}
        if client.get("client_secret"):
            fields["client_secret"] = client["client_secret"]
        token = _normalise(_post_form(self.google["token_url"], fields))
        if not token.get("refresh_token"):
            raise OAuthError("Google did not return a refresh token; remove Felix under "
                             "myaccount.google.com/permissions and sign in again.")
        self._save("google", account, token)
        return {"account": account, "scopes": token.get("scope", "")}

    # -- Microsoft: device code ---------------------------------------------------
    def microsoft_begin(self, scopes: str) -> dict[str, Any]:
        client = self.client("microsoft")
        url = self.microsoft["device_url"].format(tenant=client.get("tenant") or "common")
        return _post_form(url, {"client_id": client["client_id"], "scope": "offline_access " + scopes})

    def microsoft_finish(self, account: str, device: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        client = self.client("microsoft")
        url = self.microsoft["token_url"].format(tenant=client.get("tenant") or "common")
        interval = float(device.get("interval", 5))
        deadline = time.time() + (timeout or float(device.get("expires_in", 900)))
        while time.time() < deadline:
            time.sleep(interval)
            try:
                fresh = _post_form(url, {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": client["client_id"], "device_code": device["device_code"]})
            except OAuthError as exc:
                text = str(exc)
                if "authorization_pending" in text:
                    continue
                if "slow_down" in text:
                    interval += 5
                    continue
                raise
            token = _normalise(fresh)
            self._save("microsoft", account, token)
            return {"account": account, "scopes": token.get("scope", "")}
        raise OAuthError("Microsoft sign-in timed out.")


def _normalise(response: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    token: dict[str, Any] = {
        "access_token": response.get("access_token", ""),
        "expires_at": time.time() + float(response.get("expires_in", 3600)),
    }
    refresh = response.get("refresh_token") or (previous or {}).get("refresh_token")
    if refresh:
        token["refresh_token"] = refresh
    scope = response.get("scope") or (previous or {}).get("scope")
    if scope:
        token["scope"] = scope
    return token


def _post_form(url: str, fields: dict[str, str], timeout: float = 30) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body)
            code = detail.get("error", "")
            message = f"{code}: {detail.get('error_description', '')}".strip(": ")
        except ValueError:
            message = body[:200]
        raise OAuthError(message or f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError(f"Could not reach the sign-in service: {exc}") from exc
