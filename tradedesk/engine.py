"""The trade desk engine: inbound → understand → reply/draft → record → match → tell Deepak.

Modes
- approve (default): every reply becomes a draft Deepak sends from WhatsApp or the dashboard.
- auto: routine relationship/qualifying replies go out on their own; anything commercial,
  escalated or uncertain still becomes a draft.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .brain import Brain, BrainError, TurnContext, TurnResult
from .config import Config, normalize_wa_id
from .db import Database, iso, parse_iso, utcnow
from .matcher import Opportunity, describe, economics, find_opportunities
from .normalize import model_key
from .stock import (currency_for, import_offer_lines, read_table_file, rows_to_lines, save_offer,
                    save_requirement, sheets_to_text)
from .whatsapp import Inbound, SendError

log = logging.getLogger(__name__)

PROGRESS = ["new", "contacted", "engaged", "qualified", "trading"]
WINDOW = timedelta(hours=24) - timedelta(minutes=5)

OPT_OUT_EXACT = {"stop", "unsubscribe", "stop promotions", "opt out", "optout", "remove", "stopp", "abmelden",
                 "désabonner", "desabonner", "baja", "stop please", "please stop"}
OPT_OUT_PHRASE = re.compile(
    r"\b(unsubscribe|remove me from|take me off|stop (messaging|contacting|sending|texting)|"
    r"do not (contact|message|text)|don'?t (contact|message|text) me|no more messages|"
    r"nicht mehr kontaktieren|ne plus me contacter)\b", re.I)
OPT_OUT_CONFIRMATION = "Understood. I've removed you from our list and won't contact you again."

TEMPLATE_PREVIEWS = {
    "intro": ("Hi {name} 👋 I'm Deepak from Mobi-Hub. We're building RePocket, our laptops, MacBooks & tablets "
              "line across Europe, and we both buy and sell. Would you be open to swapping stock lists to see "
              "where there's a good fit?"),
    "followup": ("Hi {name}, just floating my note back up in case it slipped past. Happy to share our current "
                 "stock or take a look at yours whenever suits. Deepak"),
}

OWNER_HELP = (
    "Trade Desk commands:\n"
    "drafts: list drafts waiting for you\n"
    "send 12: send draft 12 as written\n"
    "edit 12 <text>: send your version instead\n"
    "skip 12: discard draft 12\n"
    "opps: best trades right now\n"
    "stock <model>: who has it and at what price\n"
    "say <name or number>: <text>: message a contact\n"
    "pause <name> / resume <name>: stop/start the assistant for a contact\n"
    "digest: today's summary\n"
    "Forward a stock list file with caption 'from <name>' (their stock) or 'wants <name>' (their wants)"
)


def is_opt_out(text: str) -> bool:
    t = re.sub(r"[^\w\s'’-]", "", (text or "").strip().lower()).strip()
    return t in OPT_OUT_EXACT or bool(OPT_OUT_PHRASE.search(text or ""))


def first_name(contact: dict) -> str:
    return ((contact.get("name") or "").split() or ["there"])[0]


def contact_label(contact: dict) -> str:
    name = contact.get("name") or f"+{contact['wa_id']}"
    extra = ", ".join(x for x in [contact.get("company"), contact.get("type") if contact.get("type") != "unknown" else ""] if x)
    return f"{name} ({extra})" if extra else name


class WindowClosed(Exception):
    """WhatsApp only allows free-form messages within 24h of the contact's last message."""


@dataclass
class Outcome:
    action: str = "none"        # sent | draft | none | escalated | opted_out | paused | error
    reply: str = ""
    reason: str = ""
    draft_id: int | None = None
    result: dict | None = None
    offers_saved: int = 0
    requirements_saved: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class Engine:
    def __init__(self, config: Config, db: Database, brain: Brain, wa, *, notify_owner: bool = True):
        self.config = config
        self.db = db
        self.brain = brain
        self.wa = wa
        self.notify_enabled = notify_owner
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------ settings
    @property
    def mode(self) -> str:
        return (self.db.get_setting("mode") or self.config.mode or "approve").lower()

    def econ(self):
        return economics(self.db, self.config)

    def _lock(self, contact_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(contact_id, threading.Lock())

    # ------------------------------------------------------------ inbound
    def receive(self, msg: Inbound, now: datetime | None = None) -> dict | None:
        """Store an inbound message. Returns the contact to process, or None (duplicate / owner command)."""
        now = now or utcnow()
        wa_id = normalize_wa_id("+" + msg.wa_id) or msg.wa_id
        if self.config.owner_wa_id and wa_id == self.config.owner_wa_id:
            self.db.set_setting("owner_last_inbound", iso(now))
            if msg.media_id and msg.type in ("document", "image"):
                self.handle_owner_file(msg, now)
            else:
                self.handle_owner_command(msg.text, now=now)
            return None

        contact = self.db.upsert_contact(wa_id, name=msg.profile_name, source="whatsapp")
        body = msg.text or ""
        media = None
        if msg.media_id:
            media = {"id": msg.media_id, "type": msg.type, "mime": msg.mime_type,
                     "filename": msg.filename, "voice": msg.voice}
            label = "voice note" if msg.voice or msg.type == "audio" else msg.type
            body = f"[sent {label}{': ' + msg.filename if msg.filename else ''}] {body}".strip()
        elif msg.type not in ("text", "button", "interactive") and not body:
            body = f"[sent a {msg.type} message]"

        mid = self.db.add_message(contact["id"], "in", "contact", body, kind="media" if media else "text",
                                  wa_message_id=msg.message_id or None, media=media, at=now)
        if mid is None:
            return None  # webhook retry of a message we already have
        updates = {"last_inbound_at": iso(now), "followups_sent": 0, "next_followup_at": None}
        if contact["stage"] in ("new", "contacted", "dormant"):
            updates["stage"] = "engaged"
        self.db.update_contact(contact["id"], **updates)
        if self.config.mark_read and msg.message_id:
            self.wa.mark_read(msg.message_id)
        return self.db.get_contact(contact["id"])

    def process(self, contact_id: int, now: datetime | None = None) -> Outcome:
        """Handle everything the contact sent since we last replied (one reply for a burst of messages)."""
        now = now or utcnow()
        with self._lock(contact_id):
            contact = self.db.get_contact(contact_id)
            if not contact:
                return Outcome(action="error", reason="unknown contact")
            new = self.db.all(
                "SELECT * FROM messages WHERE contact_id = ? AND direction = 'in' AND author = 'contact' "
                "AND id > ? ORDER BY id", (contact_id, contact["last_processed_msg_id"] or 0))
            if not new:
                return Outcome(reason="nothing new")
            self.db.update_contact(contact_id, last_processed_msg_id=new[-1]["id"])
            texts = " ".join(m["body"] for m in new)

            if contact["opted_out"]:
                self.notify(f"📭 {contact_label(contact)} (opted out) wrote: {texts[:300]}")
                return Outcome(action="opted_out", reason="contact had opted out")
            if any(m["kind"] == "text" and is_opt_out(m["body"]) for m in new):
                self.opt_out(contact, confirm=True, now=now)
                return Outcome(action="opted_out", reply=OPT_OUT_CONFIRMATION, reason="asked to stop")

            outcome = Outcome()
            for m in new:
                if m["media_json"]:
                    saved = self._handle_media(contact, m, now)
                    outcome.offers_saved += saved[0]
                    outcome.requirements_saved += saved[1]
            contact = self.db.get_contact(contact_id) or contact

            if contact["bot_paused"]:
                self.notify(f"💬 {contact_label(contact)}: {texts[:500]}\n(assistant paused for this contact)")
                outcome.action, outcome.reason = "paused", "assistant paused for this contact"
                return outcome

            try:
                result = self.brain.turn(self.build_context(contact, "reply", now))
            except BrainError as e:
                self.escalate(contact, f"Assistant couldn't reply: {e}. Last message: {texts[:300]}", now)
                outcome.action, outcome.reason = "error", str(e)
                return outcome
            applied = self.apply(contact, result, now, kind="reply")
            applied.offers_saved += outcome.offers_saved
            applied.requirements_saved += outcome.requirements_saved
            return applied

    # ------------------------------------------------------------ context
    def build_context(self, contact: dict, task: str, now: datetime) -> TurnContext:
        cid = contact["id"]
        return TurnContext(
            contact=contact,
            profile=self.db.profile(contact),
            transcript=self.db.messages_for(cid, limit=40),
            task=task,
            their_offers=self.db.all("SELECT * FROM offers WHERE contact_id = ? AND status = 'available' "
                                     "ORDER BY id DESC LIMIT 30", (cid,)),
            their_requirements=self.db.all("SELECT * FROM requirements WHERE contact_id = ? AND status = 'open' "
                                           "ORDER BY id DESC LIMIT 30", (cid,)),
            market_signals=self.market_signals(cid),
            style_examples=list(reversed(self.db.all(
                "SELECT draft, final FROM style_examples ORDER BY id DESC LIMIT 6"))),
            owner_name=self.config.owner_name,
            disclose_assistant=(self.mode == "auto" and self.config.ai_disclosure and not contact["disclosed"]),
            now=now,
        )

    def market_signals(self, contact_id: int) -> list[str]:
        def grouped(sql: str) -> list[dict]:
            return self.db.all(sql, (contact_id,))

        have = grouped("SELECT model, storage_gb, SUM(COALESCE(qty,0)) AS q, COUNT(*) AS n FROM offers "
                       "WHERE status = 'available' AND COALESCE(contact_id, 0) != ? AND model != '' "
                       "GROUP BY model_key, storage_gb ORDER BY q DESC LIMIT 8")
        want = grouped("SELECT model, storage_gb, SUM(COALESCE(qty,0)) AS q, COUNT(*) AS n FROM requirements "
                       "WHERE status = 'open' AND COALESCE(contact_id, 0) != ? AND model != '' "
                       "GROUP BY model_key, storage_gb ORDER BY q DESC LIMIT 8")

        def fmt(r: dict) -> str:
            return f"{r['model']}{' ' + str(r['storage_gb']) + 'GB' if r['storage_gb'] else ''}" + \
                   (f" (~{r['q']} units)" if r["q"] else "")
        out = []
        if have:
            out.append("We currently have access to: " + "; ".join(fmt(r) for r in have))
        if want:
            out.append("Buyers we talk to are looking for: " + "; ".join(fmt(r) for r in want))
        return out

    # ------------------------------------------------------------ apply a turn
    def apply(self, contact: dict, result: TurnResult, now: datetime, *, kind: str) -> Outcome:
        cid = contact["id"]
        econ = self.econ()
        outcome = Outcome(result=asdict(result))

        # contact details: fill blanks, never overwrite; type can be refined
        details = {k: (result.contact or {}).get(k, "") for k in ("name", "company", "role", "country", "email")}
        updates = {k: v for k, v in details.items() if v and not contact.get(k)}
        new_type = (result.contact or {}).get("type") or ""
        if new_type and new_type != "unknown" and new_type != contact["type"]:
            updates["type"] = "both" if contact["type"] in ("supplier", "buyer") and new_type != contact["type"] else new_type
        if updates:
            self.db.update_contact(cid, **updates)
        if result.facts:
            self.db.merge_profile(cid, {f["field"]: f["value"] for f in result.facts if f.get("value")})
        contact = self.db.get_contact(cid) or contact

        if result.stage and contact["stage"] != "opted_out":
            cur, new = contact["stage"], result.stage
            if new in ("dormant", "opted_out") or cur not in PROGRESS or new not in PROGRESS \
                    or PROGRESS.index(new) > PROGRESS.index(cur):
                if new != "opted_out":
                    self.db.update_contact(cid, stage=new)

        default_ccy = currency_for(contact, econ.base_currency)
        offer_ids = [i for i in (save_offer(self.db, econ, cid, o, source="chat", default_currency=default_ccy)
                                 for o in result.stock_offers if not self._known_line("offers", cid, o)) if i]
        req_ids = [i for i in (save_requirement(self.db, econ, cid, r, source="chat", default_currency=default_ccy)
                               for r in result.requirements if not self._known_line("requirements", cid, r)) if i]
        outcome.offers_saved, outcome.requirements_saved = len(offer_ids), len(req_ids)

        if result.opt_out:
            # same fixed confirmation as the STOP keyword path; nothing else goes to them
            self.opt_out(contact, confirm=True, now=now)
            self.refresh_score(cid)
            outcome.action, outcome.reply, outcome.reason = "opted_out", OPT_OUT_CONFIRMATION, "asked to stop"
            return outcome

        if result.escalate:
            self.escalate(contact, result.escalation_reason or "Needs your input", now,
                          summary=result.summary_for_owner, notify=not result.reply)

        reply = (result.reply or "").strip()
        if reply:
            auto_ok = (self.mode == "auto" and not result.needs_approval and not result.escalate
                       and self.within_window(contact, now))
            followup = result.followup_in_days
            if auto_ok:
                try:
                    self.send_text(contact, reply, author="bot", now=now, followup_days=followup)
                    if self.config.ai_disclosure and not contact["disclosed"]:
                        self.db.update_contact(cid, disclosed=1)
                    outcome.action, outcome.reply = "sent", reply
                except (SendError, WindowClosed) as e:
                    outcome.draft_id = self.create_draft(contact, reply, f"auto-send failed: {e}", kind=kind,
                                                         followup_days=followup, summary=result.summary_for_owner)
                    outcome.action, outcome.reply, outcome.reason = "draft", reply, str(e)
            else:
                reason = (result.approval_reason or result.escalation_reason
                          or ("approve mode" if self.mode == "approve" else "needs your OK"))
                outcome.draft_id = self.create_draft(contact, reply, reason, kind=kind, followup_days=followup,
                                                     summary=result.summary_for_owner)
                outcome.action, outcome.reply, outcome.reason = "draft", reply, reason
        elif result.escalate:
            outcome.action, outcome.reason = "escalated", result.escalation_reason

        self.refresh_score(cid)
        if offer_ids or req_ids:
            self.alert_matches(offer_ids=offer_ids, requirement_ids=req_ids)
        return outcome

    def _known_line(self, table: str, contact_id: int, line: dict) -> bool:
        """Is this chat-mentioned line already on file for the contact? (The model sees the whole
        transcript, so it can re-report lines from earlier messages.)"""
        key = model_key(line.get("brand"), line.get("model"))
        status = "available" if table == "offers" else "open"
        price_col = "unit_price" if table == "offers" else "target_price"
        rows = self.db.all(f"SELECT storage_gb, qty, {price_col} AS price FROM {table} WHERE contact_id = ? "
                           f"AND model_key = ? AND status = ?", (contact_id, key, status))
        storage = line.get("storage_gb")
        price = line.get(price_col)
        return any((r["storage_gb"] or None) == (storage or None) and (r["qty"] or None) == (line.get("qty") or None)
                   and (r["price"] if r["price"] is not None else None) == price for r in rows)

    # ------------------------------------------------------------ media / stock lists
    def _handle_media(self, contact: dict, message: dict, now: datetime) -> tuple[int, int]:
        media = json.loads(message["media_json"])
        if media.get("voice") or media.get("type") == "audio":
            self.escalate(contact, "Sent a voice note. Please listen and reply yourself.", now)
            return 0, 0
        if media.get("type") not in ("document", "image"):
            return 0, 0
        filename = media.get("filename") or f"{media.get('type')}"
        try:
            data, mime = self.wa.download_media(media["id"])
        except Exception as e:  # noqa: BLE001 — any download failure goes to Deepak
            self.escalate(contact, f"Couldn't download {filename}: {e}", now)
            return 0, 0
        return self.ingest_file(contact, filename, mime or media.get("mime") or "", data, now)

    def ingest_file(self, contact: dict | None, filename: str, mime: str, data: bytes,
                    now: datetime | None = None, *, as_requirements: bool | None = None) -> tuple[int, int]:
        """Read a stock list / want list file and save it. Returns (offers_saved, requirements_saved)."""
        now = now or utcnow()
        econ = self.econ()
        cid = contact["id"] if contact else None
        if as_requirements is None:
            as_requirements = bool(contact and contact.get("type") == "buyer")
        default_ccy = currency_for(contact, econ.base_currency)
        hint = (f"File '{filename}' sent by {contact_label(contact) if contact else 'Deepak'} "
                f"(a {contact.get('type') if contact else 'unknown'} contact).")
        offers: list[dict] = []
        reqs: list[dict] = []
        try:
            sheets = read_table_file(filename, mime, data)
        except Exception as e:  # noqa: BLE001 — corrupt files happen
            sheets = None
            if contact:
                self.escalate(contact, f"Couldn't open {filename}: {e}", now)
            return 0, 0

        try:
            if sheets is not None:
                lines = [line for name, rows in sheets for line in rows_to_lines(rows, name)]
                if lines:
                    if as_requirements:
                        reqs = [{**{k: v for k, v in ln.items() if k != "unit_price"},
                                 "target_price": ln.get("unit_price"),
                                 "grades": [ln["grade"]] if ln.get("grade") else []} for ln in lines]
                    else:
                        offers = lines
                else:
                    text, truncated = sheets_to_text(sheets)
                    if truncated:
                        if contact:
                            self.escalate(contact, f"{filename} is too large to read automatically "
                                                   "and its columns weren't recognised. Please check it.", now)
                        return 0, 0
                    extracted = self.brain.extract([{"type": "text", "text": text}], hint)
                    offers, reqs = extracted.get("stock_offers", []), extracted.get("requirements", [])
            elif mime == "application/pdf" or filename.lower().endswith(".pdf"):
                block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                        "data": base64.standard_b64encode(data).decode()}}
                extracted = self.brain.extract([block], hint)
                offers, reqs = extracted.get("stock_offers", []), extracted.get("requirements", [])
            elif mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                block = {"type": "image", "source": {"type": "base64", "media_type": mime,
                                                     "data": base64.standard_b64encode(data).decode()}}
                extracted = self.brain.extract([block], hint)
                offers, reqs = extracted.get("stock_offers", []), extracted.get("requirements", [])
            else:
                if contact:
                    self.escalate(contact, f"Can't read {filename} ({mime or 'unknown type'}) automatically. "
                                           "If it's a stock list, save it as .xlsx and upload it in the dashboard.", now)
                return 0, 0
        except BrainError as e:
            if contact:
                self.escalate(contact, f"Couldn't extract stock from {filename}: {e}", now)
            return 0, 0

        offer_ids = import_offer_lines(self.db, econ, cid, offers, source=f"file:{filename}",
                                       default_currency=default_ccy) if offers else []
        req_ids = [i for i in (save_requirement(self.db, econ, cid, r, source=f"file:{filename}",
                                                default_currency=default_ccy) for r in reqs) if i]
        if contact:
            what = []
            if offer_ids:
                what.append(f"{len(offer_ids)} stock lines")
            if req_ids:
                what.append(f"{len(req_ids)} wanted lines")
            note = f"[{filename}: {' and '.join(what) if what else 'no stock lines found'} saved to the trade desk]"
            self.db.add_message(cid, "in", "system", note, kind="note", at=now)
            if offer_ids or req_ids:
                self.refresh_score(cid)
                self.notify(f"📥 {contact_label(contact)} sent {filename}: {' and '.join(what)} saved.")
        if offer_ids or req_ids:
            self.alert_matches(offer_ids=offer_ids, requirement_ids=req_ids)
        return len(offer_ids), len(req_ids)

    # ------------------------------------------------------------ sending
    def within_window(self, contact: dict, now: datetime | None = None) -> bool:
        last = parse_iso(contact.get("last_inbound_at"))
        return bool(last and (now or utcnow()) - last < WINDOW)

    def send_text(self, contact: dict, body: str, *, author: str, now: datetime | None = None,
                  followup_days: int | None = None) -> int:
        now = now or utcnow()
        if contact.get("opted_out"):
            raise SendError("contact has opted out")
        if not self.within_window(contact, now):
            raise WindowClosed("It's been over 24h since they last wrote, so WhatsApp only allows a template message.")
        wa_msg_id = self.wa.send_text(contact["wa_id"], body)
        mid = self.db.add_message(contact["id"], "out", author, body, wa_message_id=wa_msg_id or None,
                                  status="sent", at=now)
        self._after_outbound(contact, now, followup_days)
        return mid or 0

    def send_template(self, contact: dict, which: str, *, now: datetime | None = None) -> int:
        now = now or utcnow()
        if contact.get("opted_out"):
            raise SendError("contact has opted out")
        name = self.config.wa_template_intro if which == "intro" else self.config.wa_template_followup
        fname = first_name(contact)
        wa_msg_id = self.wa.send_template(contact["wa_id"], name, [fname])
        preview = TEMPLATE_PREVIEWS[which].format(name=fname)
        mid = self.db.add_message(contact["id"], "out", "bot", preview, kind="template",
                                  wa_message_id=wa_msg_id or None, status="sent", at=now)
        self._after_outbound(contact, now, None)
        return mid or 0

    def _after_outbound(self, contact: dict, now: datetime, followup_days: int | None) -> None:
        updates: dict = {"last_outbound_at": iso(now)}
        if contact["stage"] == "new":
            updates["stage"] = "contacted"
        if followup_days is None:
            idx = min(contact.get("followups_sent") or 0, len(self.config.followup_days) - 1)
            followup_days = self.config.followup_days[idx]
        updates["next_followup_at"] = iso(now + timedelta(days=followup_days)) if followup_days > 0 else None
        self.db.update_contact(contact["id"], **updates)

    # ------------------------------------------------------------ drafts
    def create_draft(self, contact: dict, body: str, reason: str, *, kind: str = "reply",
                     followup_days: int | None = None, summary: str = "") -> int:
        # a newer draft replaces older unsent ones for the same contact
        self.db.execute("UPDATE drafts SET status = 'discarded', decided_at = ? WHERE contact_id = ? "
                        "AND status = 'pending'", (iso(), contact["id"]))
        did = self.db.insert("drafts", {"contact_id": contact["id"], "body": body, "reason": reason,
                                        "kind": kind, "followup_days": followup_days, "created_at": iso()})
        head = f"✍️ Draft #{did} → {contact_label(contact)}"
        if summary:
            head += f"\n{summary}"
        self.notify(f"{head}\n\n{body}\n\nWhy: {reason}\n→ send {did} · edit {did} <text> · skip {did}")
        return did

    def send_draft(self, draft_id: int, body: str | None = None, now: datetime | None = None) -> dict:
        now = now or utcnow()
        draft = self.db.one("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        if not draft or draft["status"] != "pending":
            raise ValueError(f"Draft {draft_id} isn't pending")
        contact = self.db.get_contact(draft["contact_id"])
        if not contact:
            raise ValueError("Contact not found")
        final = (body or draft["body"]).strip()
        followup = draft["followup_days"]
        if draft["kind"] == "template":
            self.send_template(contact, "followup", now=now)
            self._bump_followups(contact, now)
        else:
            edited = bool(body) and final != draft["body"].strip()
            self.send_text(contact, final, author="owner" if edited else "bot", now=now, followup_days=followup)
            if draft["kind"] == "followup":
                self._bump_followups(contact, now)
            if edited:
                self.db.insert("style_examples", {"contact_id": contact["id"], "draft": draft["body"],
                                                  "final": final, "created_at": iso(now)})
        self.db.update("drafts", draft_id, {"status": "sent", "decided_at": iso(now), "body": final})
        return {"ok": True, "draft_id": draft_id}

    def discard_draft(self, draft_id: int) -> None:
        self.db.execute("UPDATE drafts SET status = 'discarded', decided_at = ? WHERE id = ? AND status = 'pending'",
                        (iso(), draft_id))

    def send_manual(self, contact_id: int, body: str, now: datetime | None = None) -> int:
        """Deepak typing a message himself (dashboard or 'say' command). Also teaches the assistant his style."""
        contact = self.db.get_contact(contact_id)
        if not contact:
            raise ValueError("Contact not found")
        mid = self.send_text(contact, body, author="owner", now=now)
        self.db.insert("style_examples", {"contact_id": contact_id, "draft": "", "final": body, "created_at": iso(now)})
        return mid

    # ------------------------------------------------------------ escalation / opt-out
    def escalate(self, contact: dict, reason: str, now: datetime | None = None, *, summary: str = "",
                 notify: bool = True) -> int:
        eid = self.db.insert("escalations", {"contact_id": contact["id"], "reason": reason, "created_at": iso(now)})
        if notify:
            text = f"⚠️ {contact_label(contact)}: {reason}"
            if summary:
                text += f"\n{summary}"
            self.notify(text)
        return eid

    def opt_out(self, contact: dict, *, confirm: bool, now: datetime | None = None) -> None:
        now = now or utcnow()
        self.db.update_contact(contact["id"], opted_out=1, stage="opted_out", next_followup_at=None)
        self.db.execute("UPDATE outreach SET status = 'skipped', error = 'opted out' WHERE contact_id = ? "
                        "AND status = 'queued'", (contact["id"],))
        self.db.execute("UPDATE drafts SET status = 'discarded', decided_at = ? WHERE contact_id = ? "
                        "AND status = 'pending'", (iso(now), contact["id"]))
        if confirm:
            try:
                wa_msg_id = self.wa.send_text(contact["wa_id"], OPT_OUT_CONFIRMATION)
                self.db.add_message(contact["id"], "out", "bot", OPT_OUT_CONFIRMATION,
                                    wa_message_id=wa_msg_id or None, status="sent", at=now)
            except SendError as e:
                log.warning("opt-out confirmation failed for %s: %s", contact["wa_id"], e)
        self.notify(f"🚫 {contact_label(contact)} asked not to be contacted. Removed from all outreach.")

    def erase(self, contact_id: int) -> None:
        """GDPR erasure: delete everything about the person but keep their number suppressed,
        so a later import can never message them again."""
        for table in ("messages", "drafts", "offers", "requirements", "escalations", "outreach", "style_examples"):
            self.db.execute(f"DELETE FROM {table} WHERE contact_id = ?", (contact_id,))
        self.db.update_contact(contact_id, name="", company="", role="", country="", email="", notes="",
                               profile_json="{}", score=0, opted_out=1, stage="opted_out", consent=0,
                               next_followup_at=None, source="erased")

    # ------------------------------------------------------------ owner
    def notify(self, text: str) -> None:
        if not self.notify_enabled or not self.config.owner_wa_id:
            return
        try:
            last = parse_iso(self.db.get_setting("owner_last_inbound"))
            if last and utcnow() - last < WINDOW:
                self.wa.send_text(self.config.owner_wa_id, text[:4000])
            elif self.config.wa_template_owner_alert:
                # template parameters can't contain newlines
                flat = re.sub(r"\s*\n+\s*", " · ", text)[:1000]
                self.wa.send_template(self.config.owner_wa_id, self.config.wa_template_owner_alert, [flat])
        except SendError as e:
            log.warning("owner notification failed: %s", e)

    def find_contact(self, query: str) -> dict | None:
        q = query.strip()
        wa = normalize_wa_id(q if q.startswith("+") else "+" + q) if re.fullmatch(r"\+?[\d\s-]{8,}", q) else None
        if wa:
            return self.db.contact_by_wa(wa)
        like = f"%{q.lower()}%"
        return self.db.one("SELECT * FROM contacts WHERE lower(name) LIKE ? OR lower(company) LIKE ? "
                           "ORDER BY updated_at DESC LIMIT 1", (like, like))

    def handle_owner_file(self, msg: Inbound, now: datetime) -> str:
        """Deepak forwards a stock list (e.g. one that came by email). Caption "from Markus" or
        "wants Elena" files it under that contact; no caption keeps it as unassigned stock."""
        caption = (msg.text or "").strip()
        m = re.match(r"^(from|for|wants?|needs?)\s+(.+)$", caption, re.I)
        contact = self.find_contact(m.group(2)) if m else (self.find_contact(caption) if caption else None)
        as_reqs = bool(m and m.group(1).lower().startswith(("want", "need")))
        try:
            data, mime = self.wa.download_media(msg.media_id)
        except Exception as e:  # noqa: BLE001
            reply = f"Couldn't download that file: {e}"
        else:
            offers, reqs = self.ingest_file(contact, msg.filename or msg.type, mime or msg.mime_type, data, now,
                                            as_requirements=as_reqs if m else None)
            who = contact_label(contact) if contact else "unassigned (no matching contact)" if caption else "unassigned"
            reply = (f"📥 {msg.filename or 'File'}: {offers} stock lines, {reqs} wanted lines saved → {who}."
                     if offers or reqs else f"Couldn't find stock lines in {msg.filename or 'that file'}.")
        self.notify(reply)
        return reply

    def handle_owner_command(self, text: str, now: datetime | None = None) -> str:
        now = now or utcnow()
        t = (text or "").strip()
        low = t.lower()
        reply = OWNER_HELP
        try:
            if m := re.match(r"^(send|ok|yes)\s+#?(\d+)$", low):
                self.send_draft(int(m.group(2)), now=now)
                reply = f"✅ Sent draft {m.group(2)}."
            elif m := re.match(r"^edit\s+#?(\d+)\s+(.+)$", t, re.S | re.I):
                self.send_draft(int(m.group(1)), body=m.group(2), now=now)
                reply = f"✅ Sent your version of draft {m.group(1)}."
            elif m := re.match(r"^(skip|discard|no)\s+#?(\d+)$", low):
                self.discard_draft(int(m.group(2)))
                reply = f"🗑️ Discarded draft {m.group(2)}."
            elif low == "drafts":
                rows = self.db.all("SELECT d.id, d.body, c.name, c.company, c.wa_id FROM drafts d JOIN contacts c "
                                   "ON c.id = d.contact_id WHERE d.status = 'pending' ORDER BY d.id DESC LIMIT 10")
                reply = "\n".join(f"#{r['id']} {r['name'] or r['wa_id']}: {r['body'][:90]}" for r in rows) \
                    or "No drafts waiting."
            elif low in ("opps", "opportunities", "deals"):
                reply = self.opportunities_text(limit=8)
            elif low == "digest":
                reply = self.digest_text(now)
            elif m := re.match(r"^stock\s+(.+)$", t, re.I):
                like = f"%{m.group(1).lower()}%"
                rows = self.db.all("SELECT o.*, c.name AS cname, c.company AS ccomp FROM offers o LEFT JOIN contacts c "
                                   "ON c.id = o.contact_id WHERE o.status = 'available' AND lower(o.model) LIKE ? "
                                   "ORDER BY o.price_base IS NULL, o.price_base LIMIT 12", (like,))
                ccy = self.econ().base_currency
                reply = "\n".join(
                    f"{r['model']} {str(r['storage_gb']) + 'GB ' if r['storage_gb'] else ''}{r['grade']} "
                    f"x{r['qty'] or '?'} @ {r['price_base'] if r['price_base'] is not None else '?'} {ccy} "
                    f"({r['cname'] or r['ccomp'] or 'unknown'})" for r in rows) or "Nothing on file for that."
            elif m := re.match(r"^say\s+(.+?):\s*(.+)$", t, re.S | re.I):
                contact = self.find_contact(m.group(1))
                if not contact:
                    reply = f"Couldn't find '{m.group(1)}'."
                else:
                    self.send_manual(contact["id"], m.group(2).strip(), now=now)
                    reply = f"✅ Sent to {contact_label(contact)}."
            elif m := re.match(r"^(pause|resume)\s+(.+)$", t, re.I):
                contact = self.find_contact(m.group(2))
                if not contact:
                    reply = f"Couldn't find '{m.group(2)}'."
                else:
                    paused = m.group(1).lower() == "pause"
                    self.db.update_contact(contact["id"], bot_paused=int(paused))
                    reply = f"{'⏸️ Paused' if paused else '▶️ Resumed'} the assistant for {contact_label(contact)}."
        except (ValueError, SendError, WindowClosed) as e:
            reply = f"Couldn't do that: {e}"
        self.notify(reply)
        return reply

    # ------------------------------------------------------------ matching
    def alert_matches(self, *, offer_ids: list[int] | None = None, requirement_ids: list[int] | None = None) -> list[Opportunity]:
        econ = self.econ()
        found: list[Opportunity] = []
        if offer_ids:
            found += find_opportunities(self.db, econ, offer_ids=offer_ids)
        if requirement_ids:
            found += find_opportunities(self.db, econ, requirement_ids=requirement_ids)
        worth = [op for op in found if op.status in ("good", "needs_buy_price")]
        if worth:
            lines = "\n".join(f"• {describe(op)}" for op in worth[:6])
            more = f"\n…and {len(worth) - 6} more on the dashboard" if len(worth) > 6 else ""
            self.notify(f"💰 New trade match{'es' if len(worth) > 1 else ''}:\n{lines}{more}")
        return found

    def opportunities_text(self, limit: int = 8) -> str:
        econ = self.econ()
        ops = [op for op in find_opportunities(self.db, econ) if op.status in ("good", "needs_buy_price", "thin")]
        if not ops:
            return "No matches yet. More stock lists and buyer requirements will fill this up."
        return f"Top trades (target {econ.target_margin_pct:g}% margin):\n" + \
            "\n".join(f"{i}. {describe(op)}" for i, op in enumerate(ops[:limit], 1))

    # ------------------------------------------------------------ scoring
    def refresh_score(self, contact_id: int) -> int:
        c = self.db.get_contact(contact_id)
        if not c:
            return 0
        if c["opted_out"]:
            score = 0
        else:
            profile = self.db.profile(c)
            n_off = self.db.one("SELECT COUNT(*) AS n FROM offers WHERE contact_id = ? AND status = 'available'",
                                (contact_id,))["n"]
            n_req = self.db.one("SELECT COUNT(*) AS n FROM requirements WHERE contact_id = ? AND status = 'open'",
                                (contact_id,))["n"]
            score = (10 if c["type"] in ("supplier", "buyer", "both") else 0) + min(len(profile), 12) * 4
            score += (20 if n_off else 0) + (20 if n_req else 0)
            score += {"engaged": 5, "qualified": 10, "trading": 15}.get(c["stage"], 0)
            score = min(score, 100)
        self.db.update_contact(contact_id, score=score)
        return score

    # ------------------------------------------------------------ scheduled jobs
    def in_send_window(self, now: datetime) -> bool:
        start, end = self.config.send_window_utc
        return now.weekday() < 5 and start <= now.hour < end

    def run_due(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        return {"followups": self.process_followups(now), "outreach": self.process_outreach(now),
                "digest": self.maybe_digest(now)}

    def _bump_followups(self, contact: dict, now: datetime) -> None:
        sent = (contact.get("followups_sent") or 0) + 1
        gaps = self.config.followup_days
        nxt = iso(now + timedelta(days=gaps[sent])) if sent < len(gaps) else iso(now + timedelta(days=gaps[-1]))
        self.db.update_contact(contact["id"], followups_sent=sent, next_followup_at=nxt)

    def process_followups(self, now: datetime) -> int:
        due = self.db.all(
            "SELECT * FROM contacts WHERE next_followup_at IS NOT NULL AND next_followup_at <= ? AND opted_out = 0 "
            "AND bot_paused = 0 AND stage != 'opted_out'", (iso(now),))
        handled = 0
        for c in due:
            if (c["followups_sent"] or 0) >= len(self.config.followup_days):
                self.db.update_contact(c["id"], stage="dormant", next_followup_at=None)
                continue
            if not self.in_send_window(now):
                continue
            if self.db.one("SELECT id FROM drafts WHERE contact_id = ? AND status = 'pending'", (c["id"],)):
                self.db.update_contact(c["id"], next_followup_at=iso(now + timedelta(days=1)))
                continue
            try:
                if self.within_window(c, now):
                    result = self.brain.turn(self.build_context(c, "followup", now))
                    if not (result.reply or "").strip():
                        self._bump_followups(c, now)
                        continue
                    if self.mode == "auto" and not result.needs_approval and not result.escalate:
                        self.send_text(c, result.reply.strip(), author="bot", now=now)
                        self._bump_followups(c, now)
                    else:
                        self.create_draft(c, result.reply.strip(), result.approval_reason or "follow-up",
                                          kind="followup", summary=result.summary_for_owner)
                        self.db.update_contact(c["id"], next_followup_at=None)
                elif self.mode == "auto":
                    self.send_template(c, "followup", now=now)
                    self._bump_followups(c, now)
                else:
                    preview = TEMPLATE_PREVIEWS["followup"].format(name=first_name(c))
                    self.create_draft(c, preview, "follow-up (WhatsApp template, as approved by Meta)", kind="template")
                    self.db.update_contact(c["id"], next_followup_at=None)
                handled += 1
            except (BrainError, SendError, WindowClosed) as e:
                log.warning("follow-up for %s failed: %s", c["wa_id"], e)
                self.db.update_contact(c["id"], next_followup_at=iso(now + timedelta(hours=6)))
        return handled

    def import_contacts(self, rows: list[dict], *, campaign: str, consent_confirmed: bool,
                        queue: bool = True) -> dict:
        """Add prospects from a CSV. Only people who agreed to WhatsApp contact are queued for outreach."""
        added = queued = skipped = 0
        problems: list[str] = []
        for row in rows:
            lower = {str(k).strip().lower(): (str(v).strip() if v is not None else "") for k, v in row.items()}
            phone = lower.get("phone") or lower.get("whatsapp") or lower.get("mobile") or lower.get("number") or ""
            wa_id = normalize_wa_id(phone)
            if not wa_id:
                skipped += 1
                problems.append(f"'{phone}' isn't in international format (+44…, +49…)")
                continue
            ctype = lower.get("type", "").lower()
            consent = consent_confirmed or lower.get("consent", "").lower() in ("yes", "y", "true", "1")
            existing = self.db.contact_by_wa(wa_id)
            contact = self.db.upsert_contact(
                wa_id, name=lower.get("name", ""), company=lower.get("company", ""),
                country=lower.get("country", ""), email=lower.get("email", ""), notes=lower.get("notes", ""),
                type=ctype if ctype in ("supplier", "buyer", "both") else None, source=campaign)
            if not existing:
                added += 1
            if consent and not contact["consent"]:
                self.db.update_contact(contact["id"], consent=1)
            if not queue or not consent or contact["opted_out"]:
                continue
            talking = self.db.one("SELECT id FROM messages WHERE contact_id = ? LIMIT 1", (contact["id"],))
            already = self.db.one("SELECT id FROM outreach WHERE contact_id = ? AND status IN ('queued', 'sent')",
                                  (contact["id"],))
            if talking or already:
                continue
            self.db.insert("outreach", {"contact_id": contact["id"], "campaign": campaign, "created_at": iso()})
            queued += 1
        return {"added": added, "queued": queued, "skipped": skipped, "problems": problems[:20]}

    def process_outreach(self, now: datetime) -> int:
        if not self.in_send_window(now):
            return 0
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = self.db.one("SELECT COUNT(*) AS n FROM outreach WHERE status = 'sent' AND sent_at >= ?",
                                 (iso(day_start),))["n"]
        room = max(self.config.outreach_daily_limit - sent_today, 0)
        if not room:
            return 0
        queued = self.db.all("SELECT o.id AS oid, c.* FROM outreach o JOIN contacts c ON c.id = o.contact_id "
                             "WHERE o.status = 'queued' ORDER BY o.id LIMIT ?", (room,))
        sent = 0
        for row in queued:
            if row["opted_out"]:
                self.db.update("outreach", row["oid"], {"status": "skipped", "error": "opted out"})
                continue
            try:
                self.send_template(row, "intro", now=now)
                self.db.update("outreach", row["oid"], {"status": "sent", "sent_at": iso(now)})
                sent += 1
            except SendError as e:
                self.db.update("outreach", row["oid"], {"status": "failed", "error": str(e)[:300]})
        return sent

    def digest_text(self, now: datetime) -> str:
        since = iso(now - timedelta(days=1))
        one = self.db.one
        new_convos = one("SELECT COUNT(DISTINCT contact_id) AS n FROM messages WHERE direction = 'in' "
                         "AND author = 'contact' AND created_at >= ?", (since,))["n"]
        drafts = one("SELECT COUNT(*) AS n FROM drafts WHERE status = 'pending'")["n"]
        escalations = one("SELECT COUNT(*) AS n FROM escalations WHERE status = 'open'")["n"]
        due = one("SELECT COUNT(*) AS n FROM contacts WHERE next_followup_at IS NOT NULL AND next_followup_at <= ? "
                  "AND opted_out = 0", (iso(now + timedelta(days=1)),))["n"]
        new_offers = one("SELECT COUNT(*) AS n, COUNT(DISTINCT contact_id) AS s FROM offers WHERE created_at >= ?",
                         (since,))
        new_reqs = one("SELECT COUNT(*) AS n FROM requirements WHERE created_at >= ?", (since,))["n"]
        hot = self.db.all("SELECT * FROM contacts WHERE opted_out = 0 AND stage IN ('qualified', 'trading') "
                          "ORDER BY score DESC, updated_at DESC LIMIT 3")
        parts = [
            f"☀️ Trade Desk, {now.strftime('%a %d %b')}",
            f"Conversations in the last 24h: {new_convos} · Drafts waiting: {drafts} · Needs you: {escalations}",
            f"New stock: {new_offers['n']} lines from {new_offers['s']} suppliers · New buyer wants: {new_reqs}",
            f"Follow-ups due: {due}",
        ]
        if hot:
            parts.append("Warmest leads: " + "; ".join(contact_label(c) for c in hot))
        parts += ["", self.opportunities_text(limit=5), "", "Reply 'drafts', 'opps' or 'help'."]
        return "\n".join(parts)

    def maybe_digest(self, now: datetime) -> bool:
        today = now.strftime("%Y-%m-%d")
        if now.hour != self.config.digest_hour_utc or self.db.get_setting("last_digest") == today:
            return False
        self.db.set_setting("last_digest", today)
        self.notify(self.digest_text(now))
        return True
