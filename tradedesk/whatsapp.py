"""WhatsApp Cloud API (Meta): verify webhooks, parse inbound, send text/templates, fetch media."""

from __future__ import annotations

import hashlib
import hmac
import itertools
import logging
from dataclasses import dataclass, field

import httpx

from .config import Config

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com"


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check Meta's X-Hub-Signature-256 header against the raw request body."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@dataclass
class Inbound:
    wa_id: str
    message_id: str
    type: str
    text: str = ""
    profile_name: str = ""
    timestamp: int = 0
    media_id: str = ""
    mime_type: str = ""
    filename: str = ""
    voice: bool = False


@dataclass
class StatusUpdate:
    message_id: str
    status: str
    recipient: str
    error: str = ""


def parse_webhook(payload: dict) -> tuple[list[Inbound], list[StatusUpdate]]:
    messages: list[Inbound] = []
    statuses: list[StatusUpdate] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                mtype = m.get("type", "")
                msg = Inbound(wa_id=m.get("from", ""), message_id=m.get("id", ""), type=mtype,
                              profile_name=names.get(m.get("from"), ""), timestamp=int(m.get("timestamp") or 0))
                if mtype == "text":
                    msg.text = m.get("text", {}).get("body", "")
                elif mtype in ("image", "document", "audio", "video", "sticker"):
                    media = m.get(mtype, {})
                    msg.media_id = media.get("id", "")
                    msg.mime_type = media.get("mime_type", "")
                    msg.filename = media.get("filename", "")
                    msg.text = media.get("caption", "")
                    msg.voice = bool(media.get("voice"))
                elif mtype == "button":
                    msg.text = m.get("button", {}).get("text", "")
                elif mtype == "interactive":
                    inter = m.get("interactive", {})
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    msg.text = reply.get("title", "")
                elif mtype == "location":
                    loc = m.get("location", {})
                    msg.text = f"[location] {loc.get('name') or ''} {loc.get('address') or ''}".strip()
                elif mtype == "reaction":
                    msg.text = f"[reacted {m.get('reaction', {}).get('emoji', '')}]"
                elif mtype == "contacts":
                    shared = m.get("contacts", [])
                    msg.text = "[shared contact] " + ", ".join(
                        c.get("name", {}).get("formatted_name", "") for c in shared)
                messages.append(msg)
            for s in value.get("statuses", []):
                err = "; ".join(f"{e.get('code')}: {e.get('title')}" for e in s.get("errors", []))
                statuses.append(StatusUpdate(message_id=s.get("id", ""), status=s.get("status", ""),
                                             recipient=s.get("recipient_id", ""), error=err))
    return messages, statuses


class SendError(Exception):
    pass


class WhatsAppClient:
    def __init__(self, config: Config, http: httpx.Client | None = None):
        self.config = config
        self.http = http or httpx.Client(timeout=30)

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.wa_token}"}

    def _post(self, payload: dict) -> str:
        url = f"{GRAPH}/{self.config.wa_api_version}/{self.config.wa_phone_number_id}/messages"
        r = self.http.post(url, json={"messaging_product": "whatsapp", **payload}, headers=self._headers)
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                detail = f"{err.get('code')}: {err.get('message')}"
                if err.get("error_data", {}).get("details"):
                    detail += f" ({err['error_data']['details']})"
            except ValueError:
                detail = r.text[:300]
            raise SendError(detail)
        data = r.json()
        return (data.get("messages") or [{}])[0].get("id", "")

    def send_text(self, to: str, body: str) -> str:
        return self._post({"to": to, "type": "text", "text": {"body": body, "preview_url": False}})

    def send_template(self, to: str, name: str, params: list[str] | None = None, lang: str | None = None) -> str:
        template: dict = {"name": name, "language": {"code": lang or self.config.wa_template_lang}}
        if params:
            template["components"] = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
        return self._post({"to": to, "type": "template", "template": template})

    def mark_read(self, message_id: str) -> None:
        try:
            self._post({"status": "read", "message_id": message_id})
        except SendError as e:
            log.warning("mark_read failed: %s", e)

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        meta = self.http.get(f"{GRAPH}/{self.config.wa_api_version}/{media_id}", headers=self._headers)
        meta.raise_for_status()
        info = meta.json()
        blob = self.http.get(info["url"], headers=self._headers)
        blob.raise_for_status()
        return blob.content, info.get("mime_type", "")


@dataclass
class DryRunWhatsApp:
    """Stand-in used by the simulator, tests, and before Meta credentials are set.

    Records what would have been sent instead of sending it.
    """

    sent: list[dict] = field(default_factory=list)
    media: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    fail_with: str = ""
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def _record(self, item: dict) -> str:
        if self.fail_with:
            raise SendError(self.fail_with)
        mid = f"dryrun.{next(self._ids)}"
        self.sent.append({**item, "id": mid})
        return mid

    def send_text(self, to: str, body: str) -> str:
        return self._record({"to": to, "type": "text", "body": body})

    def send_template(self, to: str, name: str, params: list[str] | None = None, lang: str | None = None) -> str:
        return self._record({"to": to, "type": "template", "name": name, "params": params or []})

    def mark_read(self, message_id: str) -> None:
        return None

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        if media_id not in self.media:
            raise SendError(f"media {media_id} not available in dry-run")
        return self.media[media_id]
