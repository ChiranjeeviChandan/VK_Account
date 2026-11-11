"""WhatsApp Business Cloud API (Meta's official API) - send only.

Felix uses it for one thing: delivering briefs and alerts to the owner's own
number from a business number the owner controls. It cannot read personal
chats. Outside WhatsApp's 24-hour service window Meta only accepts
pre-approved templates, so a template can be configured as the fallback.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from ..util import clip, squash

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

OUTSIDE_WINDOW = {131047, 131026, 470}


class CloudError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class CloudApi:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self.base = "https://graph.facebook.com"     # tests point this at a local fake

    def config(self) -> dict[str, Any]:
        return self.app.config.get("whatsapp.cloud", {}) or {}

    def ready(self) -> bool:
        config = self.config()
        return bool(config.get("enabled") and config.get("phone_number_id")
                    and self.app.secrets.get("whatsapp_cloud_token") and self.app.net.online)

    def send(self, number: str, text: str) -> str:
        """Send a text message; fall back to the configured template when
        Meta says the service window is closed. Returns the message id."""
        to = re.sub(r"\D", "", number or "")
        if not to:
            raise CloudError("no destination number")
        try:
            return self._post({"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
                               "type": "text", "text": {"preview_url": False, "body": text[:4096]}})
        except CloudError as exc:
            template = self.config().get("template")
            if exc.code not in OUTSIDE_WINDOW or not template:
                raise
        # Template parameters may not contain newlines or long runs of spaces.
        summary = clip(squash(text.replace("\n", " | ")), 900)
        return self._post({
            "messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {"name": template, "language": {"code": self.config().get("template_lang", "en")},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": summary}]}]},
        })

    def _post(self, body: dict[str, Any]) -> str:
        config = self.config()
        url = f"{self.base}/{config.get('api_version', 'v21.0')}/{config['phone_number_id']}/messages"
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
            "Authorization": f"Bearer {self.app.secrets.get('whatsapp_cloud_token')}",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                error = json.loads(raw).get("error", {})
                raise CloudError(error.get("message") or raw[:200], error.get("code")) from exc
            except ValueError:
                raise CloudError(raw[:200] or f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise CloudError(f"cannot reach WhatsApp Cloud API ({getattr(exc, 'reason', exc)})") from exc
        messages = data.get("messages") or [{}]
        return str(messages[0].get("id", ""))
