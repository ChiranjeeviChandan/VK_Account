"""OAuth flows against a local fake identity provider."""

import base64
import hashlib
import http.server
import json
import threading
import time
import urllib.parse
import urllib.request

import pytest

from felix.connectors.oauth import NeedsSignIn, OAuthError, xoauth2


class Provider:
    """Just enough of an OAuth 2.0 token service to check Felix's requests."""

    def __init__(self):
        self.requests = []
        self.challenge = None
        self.device_polls_before_ok = 2
        self.revoked = False
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                fields = {k: v[0] for k, v in urllib.parse.parse_qs(
                    self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                provider.requests.append((self.path, fields))
                status, body = provider.answer(self.path, fields)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def answer(self, path, fields):
        if path.endswith("/devicecode"):
            return 200, {"device_code": "dev-1", "user_code": "ABCD-1234", "interval": 0.01, "expires_in": 30,
                         "verification_uri": "https://example.invalid/device"}
        grant = fields.get("grant_type")
        if grant == "authorization_code":
            digest = hashlib.sha256(fields["code_verifier"].encode()).digest()
            expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if fields.get("code") != "good-code" or expected != self.challenge:
                return 400, {"error": "invalid_grant", "error_description": "bad code or verifier"}
            return 200, {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600, "scope": "mail"}
        if grant == "refresh_token":
            if self.revoked or fields.get("refresh_token") != "refresh-1":
                return 400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
            return 200, {"access_token": "access-2", "expires_in": 3600}
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            if self.device_polls_before_ok > 0:
                self.device_polls_before_ok -= 1
                return 400, {"error": "authorization_pending"}
            return 200, {"access_token": "ms-access", "refresh_token": "ms-refresh", "expires_in": 3600, "scope": "imap smtp"}
        return 400, {"error": "unsupported_grant_type"}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def provider():
    p = Provider()
    yield p
    p.stop()


@pytest.fixture
def oapp(app, net, provider):
    net.up = True
    app.oauth.google["auth_url"] = provider.url + "/auth"
    app.oauth.google["token_url"] = provider.url + "/token"
    app.oauth.microsoft["device_url"] = provider.url + "/{tenant}/devicecode"
    app.oauth.microsoft["token_url"] = provider.url + "/{tenant}/token"
    return app


def browser(provider, code="good-code", tamper_state=False):
    """Stand in for the user's browser: follow the consent URL to the redirect."""
    def visit(url):
        query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        provider.challenge = query["code_challenge"]
        assert query["code_challenge_method"] == "S256" and query["access_type"] == "offline"
        state = "wrong" if tamper_state else query["state"]

        def redirect():
            time.sleep(0.05)
            target = f"{query['redirect_uri']}/?code={code}&state={state}"
            try:
                urllib.request.urlopen(target, timeout=5).read()
            except Exception:
                pass

        threading.Thread(target=redirect, daemon=True).start()
    return visit


def test_google_sign_in_with_pkce_then_refresh(oapp, provider):
    with pytest.raises(NeedsSignIn, match="felix connect google"):
        oapp.oauth.google_sign_in("vk@gmail.com", ["mail"], open_browser=False, timeout=1)
    oapp.oauth.set_client("google", "client-123", "shh")
    result = oapp.oauth.google_sign_in("vk@gmail.com", ["mail"], open_browser=False, timeout=5,
                                       on_url=browser(provider))
    assert result == {"account": "vk@gmail.com", "scopes": "mail"}
    assert oapp.oauth.signed_in("google", "vk@gmail.com")
    assert oapp.oauth.access_token("google", "vk@gmail.com") == "access-1"
    exchange = provider.requests[-1][1]
    assert exchange["client_id"] == "client-123" and exchange["client_secret"] == "shh"
    assert exchange["redirect_uri"].startswith("http://127.0.0.1:")

    # expire the access token: the next call refreshes and keeps the refresh token
    stored = json.loads(oapp.secrets.get("oauth:google:vk@gmail.com"))
    stored["expires_at"] = time.time() - 10
    oapp.secrets.set("oauth:google:vk@gmail.com", json.dumps(stored))
    assert oapp.oauth.access_token("google", "vk@gmail.com") == "access-2"
    assert json.loads(oapp.secrets.get("oauth:google:vk@gmail.com"))["refresh_token"] == "refresh-1"


def test_forged_redirect_is_rejected(oapp, provider):
    oapp.oauth.set_client("google", "client-123")
    with pytest.raises(OAuthError, match="timed out"):
        oapp.oauth.google_sign_in("vk@gmail.com", ["mail"], open_browser=False, timeout=1.5,
                                  on_url=browser(provider, tamper_state=True))
    assert not oapp.oauth.signed_in("google", "vk@gmail.com")


def test_revoked_refresh_token_asks_for_sign_in_again(oapp, provider):
    oapp.oauth.set_client("google", "client-123")
    oapp.secrets.set("oauth:google:vk@gmail.com", json.dumps(
        {"access_token": "old", "refresh_token": "refresh-1", "expires_at": time.time() - 5}))
    provider.revoked = True
    with pytest.raises(NeedsSignIn, match="no longer accepts"):
        oapp.oauth.access_token("google", "vk@gmail.com")
    assert not oapp.oauth.signed_in("google", "vk@gmail.com")


def test_expired_token_while_offline_is_explained(oapp, net):
    oapp.oauth.set_client("google", "client-123")
    oapp.secrets.set("oauth:google:vk@gmail.com", json.dumps(
        {"access_token": "old", "refresh_token": "refresh-1", "expires_at": time.time() - 5}))
    net.up = False
    with pytest.raises(OAuthError, match="offline"):
        oapp.oauth.access_token("google", "vk@gmail.com")


def test_microsoft_device_code(oapp, provider):
    oapp.oauth.set_client("microsoft", "ms-client", tenant="contoso")
    device = oapp.oauth.microsoft_begin("imap smtp")
    assert device["user_code"] == "ABCD-1234"
    assert provider.requests[-1][0] == "/contoso/devicecode"
    assert provider.requests[-1][1]["scope"] == "offline_access imap smtp"
    result = oapp.oauth.microsoft_finish("vk@outlook.com", device, timeout=5)
    assert result["account"] == "vk@outlook.com"
    assert oapp.oauth.access_token("microsoft", "vk@outlook.com") == "ms-access"
    polls = [f for path, f in provider.requests if f.get("grant_type", "").endswith("device_code")]
    assert len(polls) == 3          # two "pending" answers, then success


def test_xoauth2_string():
    assert xoauth2("vk@gmail.com", "tok") == b"user=vk@gmail.com\x01auth=Bearer tok\x01\x01"
