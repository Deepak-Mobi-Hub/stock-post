"""The assistant's brain: one Claude call per turn.

Each call gets the playbook (cached system prompt), what we know about the
contact, the recent transcript and a task. It returns JSON with the reply to
send plus everything worth recording: contact details, qualification facts,
stock offers, buyer requirements, stage, escalation and opt-out.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import anthropic

from .config import Config

log = logging.getLogger(__name__)

STAGES = ["new", "contacted", "engaged", "qualified", "trading", "dormant", "opted_out"]
CONTACT_TYPES = ["", "supplier", "buyer", "both", "unknown"]
CATEGORIES = ["", "phone", "tablet", "laptop", "desktop", "server", "wearable", "accessory", "parts", "other"]

FACT_FIELDS = [
    # who they are
    "business_type", "categories", "brands_models", "monthly_volume", "decision_maker",
    # supplier side
    "grades_condition", "data_wipe_certification", "locks_status", "stock_location", "shipping_terms",
    "vat_scheme", "payment_terms", "sales_method", "list_frequency",
    # buyer side
    "target_models", "target_grades", "spec_requirements", "markets_served", "price_expectations",
    "delivery_destination", "certifications_needed",
    # relationship
    "events_meetings", "rapport_notes", "next_step",
]

# What we want to know before calling someone "qualified".
QUALIFY_KEYS = {
    "supplier": ["categories", "monthly_volume", "grades_condition", "stock_location", "sales_method"],
    "buyer": ["target_models", "target_grades", "monthly_volume", "delivery_destination", "price_expectations"],
    "both": ["categories", "monthly_volume", "grades_condition", "stock_location", "target_models"],
    "unknown": ["business_type", "categories", "monthly_volume"],
    "": ["business_type", "categories", "monthly_volume"],
}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


OFFER_ITEM = _obj({
    "category": {"type": "string", "enum": CATEGORIES},
    "brand": {"type": "string"},
    "model": {"type": "string"},
    "storage_gb": _nullable({"type": "integer"}),
    "spec": {"type": "string"},
    "grade": {"type": "string"},
    "qty": _nullable({"type": "integer"}),
    "unit_price": _nullable({"type": "number"}),
    "currency": {"type": "string"},
    "location": {"type": "string"},
    "notes": {"type": "string"},
})

REQUIREMENT_ITEM = _obj({
    "category": {"type": "string", "enum": CATEGORIES},
    "brand": {"type": "string"},
    "model": {"type": "string"},
    "storage_gb": _nullable({"type": "integer"}),
    "spec": {"type": "string"},
    "grades": {"type": "array", "items": {"type": "string"}},
    "qty": _nullable({"type": "integer"}),
    "target_price": _nullable({"type": "number"}),
    "currency": {"type": "string"},
    "destination": {"type": "string"},
    "notes": {"type": "string"},
})

TURN_SCHEMA = _obj({
    "reply": {"type": "string"},
    "needs_approval": {"type": "boolean"},
    "approval_reason": {"type": "string"},
    "contact": _obj({
        "name": {"type": "string"},
        "company": {"type": "string"},
        "role": {"type": "string"},
        "country": {"type": "string"},
        "email": {"type": "string"},
        "type": {"type": "string", "enum": CONTACT_TYPES},
    }),
    "facts": {"type": "array", "items": _obj({
        "field": {"type": "string", "enum": FACT_FIELDS},
        "value": {"type": "string"},
    })},
    "stage": {"type": "string", "enum": STAGES},
    "stock_offers": {"type": "array", "items": OFFER_ITEM},
    "requirements": {"type": "array", "items": REQUIREMENT_ITEM},
    "escalate": {"type": "boolean"},
    "escalation_reason": {"type": "string"},
    "opt_out": {"type": "boolean"},
    "followup_in_days": {"type": "integer"},
    "summary_for_owner": {"type": "string"},
})

EXTRACT_SCHEMA = _obj({
    "stock_offers": {"type": "array", "items": OFFER_ITEM},
    "requirements": {"type": "array", "items": REQUIREMENT_ITEM},
    "notes": {"type": "string"},
})

OUTPUT_GUIDE = """
## Output

Return one JSON object (the schema is enforced). Fields:

- reply: the WhatsApp message to send now, in Deepak's voice. Empty string if nothing should be sent
  (e.g. they just said "thanks 👍" and a reply would be needy, or the contact has opted out and was
  already confirmed).
- needs_approval: true if the reply touches anything commercial (prices, offers, availability
  promises, terms, meetings at a set time) or you're not confident it's right. Deepak reviews it first.
- approval_reason: short reason when needs_approval is true, else "".
- contact: details you learned for sure (name, company, role, country, email, type). Use "" for
  anything you don't know; never guess. type is supplier / buyer / both / unknown, or "" if no change.
- facts: new or updated qualification facts only, each as {field, value}; concise values in English.
- stage: the stage after this message (see Stages).
- stock_offers: stock the contact is offering (they sell it to us), line by line, only for items
  in their newest messages that aren't already in their_stock_on_file. Prices are per unit; put their currency code (EUR,
  GBP, USD, AED) in currency, "" if not stated.
- requirements: devices the contact wants to buy from us, when they said so concretely in their
  newest messages and it isn't already in what_they_want.
  grades is the list of grades they'd accept (e.g. ["A+", "A"]); [] if any.
- escalate: true when the playbook says hand over to Deepak. escalation_reason: one line why.
- opt_out: true only if they asked not to be contacted again.
- followup_in_days: when to nudge if they go quiet after this message (e.g. 3); 0 for no follow-up
  (they opted out, or the ball is in our court).
- summary_for_owner: one or two lines for Deepak: what happened and what matters commercially.
"""


@dataclass
class TurnContext:
    """Everything the brain needs for one turn with one contact."""

    contact: dict
    profile: dict
    transcript: list[dict]           # messages rows, oldest first
    task: str = "reply"              # reply | followup
    their_offers: list[dict] = field(default_factory=list)
    their_requirements: list[dict] = field(default_factory=list)
    market_signals: list[str] = field(default_factory=list)
    style_examples: list[dict] = field(default_factory=list)
    owner_name: str = "Deepak"
    disclose_assistant: bool = False
    now: datetime | None = None


@dataclass
class TurnResult:
    reply: str = ""
    needs_approval: bool = False
    approval_reason: str = ""
    contact: dict = field(default_factory=dict)
    facts: list[dict] = field(default_factory=list)
    stage: str = ""
    stock_offers: list[dict] = field(default_factory=list)
    requirements: list[dict] = field(default_factory=list)
    escalate: bool = False
    escalation_reason: str = ""
    opt_out: bool = False
    followup_in_days: int = 0
    summary_for_owner: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "TurnResult":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


class BrainError(Exception):
    """The model couldn't produce a usable answer (refusal, truncation, API error)."""


class Brain(Protocol):
    def turn(self, ctx: TurnContext) -> TurnResult: ...

    def extract(self, content: list[dict], hint: str) -> dict: ...


# ------------------------------------------------------------------ prompts
def load_playbook(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def system_prompt(playbook: str) -> str:
    return (
        "You are the WhatsApp assistant for Deepak's trading desk. You write his messages to "
        "suppliers and buyers in the used-device and ITAD trade and keep his records straight.\n\n"
        + playbook.strip() + "\n" + OUTPUT_GUIDE
    )


def _fmt_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d %b %H:%M")
    except ValueError:
        return value


def render_transcript(messages: list[dict], contact_name: str, owner_name: str) -> str:
    lines = []
    for m in messages:
        who = contact_name if m["direction"] == "in" else owner_name
        if m["author"] == "system":
            who = "note"
        body = (m.get("body") or "").strip()
        lines.append(f"[{_fmt_time(m.get('created_at'))}] {who}: {body}")
    return "\n".join(lines) if lines else "(no messages yet)"


def _offer_line(o: dict) -> str:
    bits = [o.get("model") or "", f"{o['storage_gb']}GB" if o.get("storage_gb") else "", o.get("spec") or "",
            o.get("grade") or "", f"x{o['qty']}" if o.get("qty") else "",
            f"{o['unit_price']} {o.get('currency') or ''}".strip() if o.get("unit_price") is not None else ""]
    return " ".join(b for b in bits if b)


def _req_line(r: dict) -> str:
    bits = [r.get("model") or "", f"{r['storage_gb']}GB" if r.get("storage_gb") else "", r.get("spec") or "",
            f"grades {r['grades']}" if r.get("grades") else "", f"x{r['qty']}" if r.get("qty") else "",
            f"target {r['target_price']} {r.get('currency') or ''}".strip() if r.get("target_price") is not None else ""]
    return " ".join(b for b in bits if b)


def user_prompt(ctx: TurnContext) -> str:
    c = ctx.contact
    name = c.get("name") or "the contact"
    ctype = c.get("type") or "unknown"
    missing = [k for k in QUALIFY_KEYS.get(ctype, QUALIFY_KEYS["unknown"]) if not ctx.profile.get(k)]
    known = "\n".join(f"- {k}: {v}" for k, v in ctx.profile.items()) or "- (nothing yet)"
    parts = [
        f"<today>{(ctx.now or datetime.now()).strftime('%A %d %B %Y, %H:%M UTC')}</today>",
        "<contact>",
        f"name: {c.get('name') or '(unknown)'}; company: {c.get('company') or '(unknown)'}; "
        f"role: {c.get('role') or '(unknown)'}; country: {c.get('country') or '(unknown)'}",
        f"type: {ctype}; stage: {c.get('stage') or 'new'}; follow-ups already sent: {c.get('followups_sent') or 0}",
        f"notes from Deepak: {c.get('notes') or '(none)'}",
        "known facts:", known,
        f"still worth learning: {', '.join(missing) if missing else 'nothing essential; deepen the relationship'}",
        "</contact>",
    ]
    if ctx.their_offers:
        parts += ["<their_stock_on_file>", *(f"- {_offer_line(o)}" for o in ctx.their_offers[:15]),
                  f"({len(ctx.their_offers)} lines in total)" if len(ctx.their_offers) > 15 else "",
                  "</their_stock_on_file>"]
    if ctx.their_requirements:
        parts += ["<what_they_want>", *(f"- {_req_line(r)}" for r in ctx.their_requirements[:15]), "</what_they_want>"]
    if ctx.market_signals:
        parts += ["<market_signals>",
                  "Anonymised view of our other conversations. You may say we have or are looking for these "
                  "models in general terms. Never name the other party or mention prices.",
                  *(f"- {s}" for s in ctx.market_signals), "</market_signals>"]
    if ctx.style_examples:
        parts += ["<how_deepak_rewrote_drafts>",
                  "Recent drafts Deepak edited before sending. Match his final versions.",
                  *(f"- draft: {e['draft']!r}\n  sent: {e['final']!r}" if e.get("draft") else f"- sent: {e['final']!r}"
                    for e in ctx.style_examples),
                  "</how_deepak_rewrote_drafts>"]
    parts += ["<transcript>", render_transcript(ctx.transcript, name, ctx.owner_name), "</transcript>"]

    if ctx.task == "followup":
        task = (f"{name} hasn't replied since Deepak's last message. Write a short, friendly nudge in "
                "Deepak's voice that adds something useful (a question, a reason to talk) instead of just "
                "chasing. If a nudge would be pushy given the transcript, return an empty reply.")
    else:
        task = (f"Write Deepak's next WhatsApp reply to {name}, following the playbook. Keep building the "
                "relationship, ask at most one or two natural qualifying questions from 'still worth "
                "learning' if they fit, and record everything you learned.")
    if ctx.disclose_assistant:
        task += (" This will be the first message the assistant sends to this person without Deepak "
                 "reviewing it: add one short, natural line saying you're Deepak's assistant and he sees "
                 "every message.")
    parts += ["<task>", task, "</task>"]
    return "\n".join(p for p in parts if p != "")


# ------------------------------------------------------------------ Claude
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")


class ClaudeBrain:
    def __init__(self, config: Config, client: anthropic.Anthropic | None = None):
        self.config = config
        self._client = client
        self.system = system_prompt(load_playbook(config.playbook_path))

    @property
    def client(self) -> anthropic.Anthropic:
        # Created on first use so the dashboard runs before an API key is configured.
        if self._client is None:
            try:
                self._client = anthropic.Anthropic()
            except anthropic.AnthropicError as e:
                raise BrainError("Claude isn't configured: set ANTHROPIC_API_KEY") from e
        return self._client

    def _call(self, content: list[dict] | str, schema: dict, *, system: str | None = None,
              max_tokens: int = 16000) -> dict:
        kwargs: dict = {}
        if self.config.claude_model.startswith(FALLBACK_MODELS):
            # If a safety classifier declines, retry server-side on Anthropic's recommended model.
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            # Streamed so long extractions (big stock lists) don't hit HTTP timeouts.
            with self.client.beta.messages.stream(
                model=self.config.claude_model,
                max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": self.config.claude_effort,
                               "format": {"type": "json_schema", "schema": schema}},
                system=[{"type": "text", "text": system or self.system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                **kwargs,
            ) as stream:
                response = stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise BrainError(f"Claude rate limit: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise BrainError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise BrainError("Couldn't reach the Claude API") from e
        except TypeError as e:
            # the SDK raises this at request time when no API key / credentials are configured
            if "authentication method" in str(e):
                raise BrainError("Claude isn't configured: set ANTHROPIC_API_KEY") from e
            raise

        if response.stop_reason == "refusal":
            raise BrainError("Claude declined to answer this one")
        if response.stop_reason == "max_tokens":
            raise BrainError("Claude's answer was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise BrainError("Claude returned invalid JSON") from e

    def turn(self, ctx: TurnContext) -> TurnResult:
        return TurnResult.from_json(self._call(user_prompt(ctx), TURN_SCHEMA))

    def extract(self, content: list[dict], hint: str) -> dict:
        """Pull stock lines / requirements out of a document, image or pasted table."""
        system = (
            "You extract structured trade data for a used-device and ITAD trading desk. Read the "
            "material and return every stock line (devices offered for sale) and every buy requirement. "
            "One entry per distinct model/spec/grade line. Don't invent anything: use \"\" or null for "
            "missing values. Prices are per unit; use the currency code shown (EUR, GBP, USD, AED)."
        )
        blocks = list(content) + [{"type": "text", "text": f"Context: {hint}\nExtract the trade data."}]
        return self._call(blocks, EXTRACT_SCHEMA, system=system, max_tokens=64000)
