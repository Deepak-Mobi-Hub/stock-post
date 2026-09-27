"""FastAPI app: WhatsApp webhook, dashboard, JSON API, scheduler.

Run locally:  uvicorn tradedesk.main:app --reload
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import csv
import io
import json
import logging
import math
import secrets
from dataclasses import replace
from pathlib import Path

from fastapi import (BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Query, Request,
                     UploadFile)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .brain import Brain, BrainError, ClaudeBrain
from .config import Config, normalize_wa_id
from .db import Database, utcnow
from .engine import Engine, WindowClosed, contact_label
from .matcher import SETTING_KEYS, find_opportunities, money
from .stock import save_offer, save_requirement
from .whatsapp import DryRunWhatsApp, Inbound, SendError, WhatsAppClient, parse_webhook, verify_signature

log = logging.getLogger("tradedesk")
STATIC = Path(__file__).resolve().parent / "static"


class State:
    def __init__(self, config: Config, db: Database, brain: Brain, wa):
        self.config = config
        self.db = db
        self.brain = brain
        self.wa = wa
        self.engine = Engine(config, db, brain, wa)
        self.generation: dict[int, int] = {}
        self._sim: Engine | None = None

    @property
    def sim(self) -> Engine:
        """Simulator: same brain and settings, separate database, nothing is really sent."""
        if self._sim is None:
            path = ":memory:" if self.config.database_path == ":memory:" else \
                str(Path(self.config.database_path).with_suffix(".sim.db"))
            cfg = replace(self.config, database_path=path, owner_wa_id="")
            self._sim = Engine(cfg, Database(path), self.brain, DryRunWhatsApp(), notify_owner=False)
        for key in SETTING_KEYS:
            value = self.db.get_setting(key)
            if value is not None:
                self._sim.db.set_setting(key, value)
        return self._sim


def create_app(config: Config | None = None, *, brain: Brain | None = None, wa=None,
               run_scheduler: bool = True) -> FastAPI:
    config = config or Config.from_env()
    db = Database(config.database_path)
    brain = brain or ClaudeBrain(config)
    if wa is None:
        wa = WhatsAppClient(config) if config.whatsapp_enabled else DryRunWhatsApp()
    state = State(config, db, brain, wa)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(_scheduler(state)) if run_scheduler else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="RePocket Trade Desk", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.td = state
    security = HTTPBasic(auto_error=False)

    def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        if config.dev_mode and not config.dashboard_password:
            return
        if not config.dashboard_password:
            raise HTTPException(503, "Set DASHBOARD_PASSWORD to use the dashboard.")
        ok = creds is not None and secrets.compare_digest(creds.username, config.dashboard_user) \
            and secrets.compare_digest(creds.password, config.dashboard_password)
        if not ok:
            raise HTTPException(401, "Login required", headers={"WWW-Authenticate": 'Basic realm="Trade Desk"'})

    engine = state.engine

    # ------------------------------------------------------------ webhook
    @app.get("/webhook")
    def verify(mode: str = Query("", alias="hub.mode"), token: str = Query("", alias="hub.verify_token"),
               challenge: str = Query("", alias="hub.challenge")):
        if mode == "subscribe" and config.wa_verify_token and secrets.compare_digest(token, config.wa_verify_token):
            return PlainTextResponse(challenge)
        raise HTTPException(403, "Verification failed")

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks):
        raw = await request.body()
        if config.wa_app_secret:
            if not verify_signature(config.wa_app_secret, raw, request.headers.get("x-hub-signature-256")):
                raise HTTPException(401, "Bad signature")
        elif not config.dev_mode:
            raise HTTPException(503, "Set WHATSAPP_APP_SECRET so webhook calls can be verified.")
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid JSON")
        messages, statuses = parse_webhook(payload)
        pending: list[tuple[int, int]] = []
        for msg in messages:
            contact = await asyncio.to_thread(engine.receive, msg)
            if contact:
                gen = state.generation.get(contact["id"], 0) + 1
                state.generation[contact["id"]] = gen
                pending.append((contact["id"], gen))
        if pending:
            # runs after Meta gets its 200; different contacts are handled in parallel
            background.add_task(_debounce_all, state, pending)
        for st in statuses:
            await asyncio.to_thread(_apply_status, state, st)
        return {"ok": True}

    # ------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def dashboard():
        return HTMLResponse((STATIC / "dashboard.html").read_text(encoding="utf-8"))

    @app.get("/healthz")
    def health():
        return {"ok": True}

    api = dict(dependencies=[Depends(auth)])

    @app.get("/api/summary", **api)
    def summary():
        one = db.one
        econ = engine.econ()
        ops = find_opportunities(db, econ)
        return {
            "mode": engine.mode,
            "whatsapp_live": config.whatsapp_enabled,
            "owner_alerts": bool(config.owner_wa_id),
            "model": config.claude_model,
            "contacts": one("SELECT COUNT(*) AS n FROM contacts WHERE opted_out = 0")["n"],
            "by_stage": {r["stage"]: r["n"] for r in db.all("SELECT stage, COUNT(*) AS n FROM contacts GROUP BY stage")},
            "drafts": one("SELECT COUNT(*) AS n FROM drafts WHERE status = 'pending'")["n"],
            "escalations": one("SELECT COUNT(*) AS n FROM escalations WHERE status = 'open'")["n"],
            "offers": one("SELECT COUNT(*) AS n FROM offers WHERE status = 'available'")["n"],
            "requirements": one("SELECT COUNT(*) AS n FROM requirements WHERE status = 'open'")["n"],
            "opportunities_good": sum(1 for o in ops if o.status == "good"),
            "opportunities_bid": sum(1 for o in ops if o.status == "needs_buy_price"),
            "potential_profit": round(sum(o.total_profit or 0 for o in ops if o.status == "good"), 2),
            "outreach_queued": one("SELECT COUNT(*) AS n FROM outreach WHERE status = 'queued'")["n"],
            "economics": econ.__dict__,
        }

    # contacts
    @app.get("/api/contacts", **api)
    def contacts(stage: str = "", type: str = "", q: str = ""):
        sql = ("SELECT c.*, (SELECT COUNT(*) FROM offers o WHERE o.contact_id = c.id AND o.status = 'available') "
               "AS n_offers, (SELECT COUNT(*) FROM requirements r WHERE r.contact_id = c.id AND r.status = 'open') "
               "AS n_requirements, (SELECT COUNT(*) FROM drafts d WHERE d.contact_id = c.id AND d.status = 'pending') "
               "AS n_drafts, (SELECT body FROM messages m WHERE m.contact_id = c.id ORDER BY m.id DESC LIMIT 1) "
               "AS last_message FROM contacts c WHERE 1=1")
        params: list = []
        if stage:
            sql += " AND c.stage = ?"
            params.append(stage)
        if type:
            sql += " AND c.type = ?"
            params.append(type)
        if q:
            sql += " AND (lower(c.name) LIKE ? OR lower(c.company) LIKE ? OR c.wa_id LIKE ?)"
            params += [f"%{q.lower()}%"] * 2 + [f"%{q}%"]
        sql += " ORDER BY c.score DESC, c.updated_at DESC LIMIT 500"
        rows = db.all(sql, params)
        for r in rows:
            r["profile"] = db.profile(r)
        return rows

    def _contact_or_404(cid: int) -> dict:
        c = db.get_contact(cid)
        if not c:
            raise HTTPException(404, "Contact not found")
        return c

    @app.get("/api/contacts/{cid}", **api)
    def contact_detail(cid: int):
        c = _contact_or_404(cid)
        return {
            "contact": c, "profile": db.profile(c), "label": contact_label(c),
            "within_window": engine.within_window(c),
            "messages": db.messages_for(cid, limit=200),
            "offers": db.all("SELECT * FROM offers WHERE contact_id = ? ORDER BY id DESC LIMIT 300", (cid,)),
            "requirements": db.all("SELECT * FROM requirements WHERE contact_id = ? ORDER BY id DESC", (cid,)),
            "drafts": db.all("SELECT * FROM drafts WHERE contact_id = ? AND status = 'pending'", (cid,)),
            "escalations": db.all("SELECT * FROM escalations WHERE contact_id = ? AND status = 'open'", (cid,)),
        }

    @app.post("/api/contacts", **api)
    def create_contact(data: dict = Body(...)):
        wa_id = normalize_wa_id(data.get("phone", ""))
        if not wa_id:
            raise HTTPException(400, "Phone must be in international format, e.g. +44 7…")
        ctype = data.get("type") if data.get("type") in ("supplier", "buyer", "both") else None
        c = db.upsert_contact(wa_id, name=data.get("name", ""), company=data.get("company", ""),
                              country=data.get("country", ""), notes=data.get("notes", ""), type=ctype,
                              source="manual")
        if data.get("consent"):
            db.update_contact(c["id"], consent=1)
        return db.get_contact(c["id"])

    @app.patch("/api/contacts/{cid}", **api)
    def update_contact(cid: int, data: dict = Body(...)):
        _contact_or_404(cid)
        allowed = {"name", "company", "role", "country", "email", "type", "stage", "notes", "bot_paused",
                   "opted_out", "consent"}
        values = {k: v for k, v in data.items() if k in allowed}
        if "opted_out" in values:
            values["opted_out"] = int(bool(values["opted_out"]))
            if values["opted_out"]:
                values.update(stage="opted_out", next_followup_at=None)
            elif data.get("stage") is None:
                values["stage"] = "engaged"
        db.update_contact(cid, **values)
        engine.refresh_score(cid)
        return db.get_contact(cid)

    @app.post("/api/contacts/{cid}/erase", **api)
    def erase_contact(cid: int):
        _contact_or_404(cid)
        engine.erase(cid)
        return {"ok": True}

    @app.post("/api/contacts/{cid}/message", **api)
    def send_message(cid: int, data: dict = Body(...)):
        body = (data.get("body") or "").strip()
        if not body:
            raise HTTPException(400, "Message is empty")
        _contact_or_404(cid)
        try:
            engine.send_manual(cid, body)
        except WindowClosed as e:
            raise HTTPException(409, str(e))
        except SendError as e:
            raise HTTPException(502, f"WhatsApp refused it: {e}")
        return {"ok": True}

    @app.post("/api/contacts/{cid}/template", **api)
    def send_template(cid: int, data: dict = Body(...)):
        c = _contact_or_404(cid)
        which = data.get("which", "followup")
        if which not in ("intro", "followup"):
            raise HTTPException(400, "Unknown template")
        if which == "intro" and not c["consent"]:
            raise HTTPException(409, "Mark this contact as having agreed to WhatsApp contact first.")
        try:
            engine.send_template(c, which)
        except SendError as e:
            raise HTTPException(502, f"WhatsApp refused it: {e}")
        return {"ok": True}

    @app.post("/api/stocklist", **api)
    async def upload_stocklist(file: UploadFile = File(...), contact_id: int | None = Form(None),
                               kind: str = Form("auto")):
        c = _contact_or_404(contact_id) if contact_id else None
        data = await file.read()
        as_req = None if kind == "auto" else kind == "requirements"
        offers, reqs = await asyncio.to_thread(engine.ingest_file, c, file.filename or "upload",
                                               file.content_type or "", data, as_requirements=as_req)
        if not offers and not reqs:
            raise HTTPException(422, "No stock lines found in that file.")
        return {"offers": offers, "requirements": reqs}

    # drafts & escalations
    @app.get("/api/drafts", **api)
    def drafts():
        rows = db.all("SELECT d.*, c.name, c.company, c.wa_id, c.type, c.last_inbound_at FROM drafts d "
                      "JOIN contacts c ON c.id = d.contact_id WHERE d.status = 'pending' ORDER BY d.id DESC")
        for r in rows:
            r["within_window"] = engine.within_window(r) or r["kind"] == "template"
        return rows

    @app.post("/api/drafts/{did}/send", **api)
    def send_draft(did: int, data: dict = Body(default={})):
        try:
            return engine.send_draft(did, body=(data or {}).get("body") or None)
        except ValueError as e:
            raise HTTPException(404, str(e))
        except WindowClosed as e:
            raise HTTPException(409, str(e))
        except SendError as e:
            raise HTTPException(502, f"WhatsApp refused it: {e}")

    @app.post("/api/drafts/{did}/discard", **api)
    def discard_draft(did: int):
        engine.discard_draft(did)
        return {"ok": True}

    @app.get("/api/escalations", **api)
    def escalations():
        return db.all("SELECT e.*, c.name, c.company, c.wa_id FROM escalations e LEFT JOIN contacts c "
                      "ON c.id = e.contact_id WHERE e.status = 'open' ORDER BY e.id DESC")

    @app.post("/api/escalations/{eid}/done", **api)
    def escalation_done(eid: int):
        db.update("escalations", eid, {"status": "done"})
        return {"ok": True}

    # stock & demand
    @app.get("/api/offers", **api)
    def offers(status: str = "available", q: str = ""):
        sql = ("SELECT o.*, c.name AS contact_name, c.company AS contact_company FROM offers o "
               "LEFT JOIN contacts c ON c.id = o.contact_id WHERE o.status = ?")
        params: list = [status]
        if q:
            sql += " AND (lower(o.model) LIKE ? OR lower(o.spec) LIKE ?)"
            params += [f"%{q.lower()}%"] * 2
        return db.all(sql + " ORDER BY o.id DESC LIMIT 2000", params)

    @app.get("/api/requirements", **api)
    def requirements(status: str = "open"):
        return db.all("SELECT r.*, c.name AS contact_name, c.company AS contact_company FROM requirements r "
                      "LEFT JOIN contacts c ON c.id = r.contact_id WHERE r.status = ? ORDER BY r.id DESC", (status,))

    @app.post("/api/offers", **api)
    def add_offer(data: dict = Body(...)):
        econ = engine.econ()
        oid = save_offer(db, econ, data.get("contact_id") or None, _clean_line(data), source="manual")
        if not oid:
            raise HTTPException(400, "Model is required")
        engine.alert_matches(offer_ids=[oid])
        return {"id": oid}

    @app.post("/api/requirements", **api)
    def add_requirement(data: dict = Body(...)):
        econ = engine.econ()
        line = _clean_line(data)
        line["grades"] = data.get("grades") or []
        line["target_price"] = _num(data.get("target_price"))
        rid = save_requirement(db, econ, data.get("contact_id") or None, line, source="manual")
        if not rid:
            raise HTTPException(400, "Model is required")
        engine.alert_matches(requirement_ids=[rid])
        return {"id": rid}

    @app.patch("/api/offers/{oid}", **api)
    def update_offer(oid: int, data: dict = Body(...)):
        if data.get("status") not in ("available", "sold", "expired"):
            raise HTTPException(400, "status must be available, sold or expired")
        db.update("offers", oid, {"status": data["status"]})
        return {"ok": True}

    @app.patch("/api/requirements/{rid}", **api)
    def update_requirement(rid: int, data: dict = Body(...)):
        if data.get("status") not in ("open", "filled", "closed"):
            raise HTTPException(400, "status must be open, filled or closed")
        db.update("requirements", rid, {"status": data["status"]})
        return {"ok": True}

    @app.get("/api/opportunities", **api)
    def opportunities(status: str = ""):
        ops = find_opportunities(db, engine.econ())
        if status:
            wanted = set(status.split(","))
            ops = [o for o in ops if o.status in wanted]
        return [o.to_dict() for o in ops[:500]]

    @app.post("/api/blast", **api)
    def blast(data: dict = Body(...)):
        """Buyer-facing stock list with our sell prices. Never includes supplier names or buy prices."""
        ids = [int(i) for i in data.get("offer_ids", [])]
        if not ids:
            raise HTTPException(400, "Pick some stock lines first")
        econ = engine.econ()
        marks = ",".join("?" for _ in ids)
        rows = db.all(f"SELECT * FROM offers WHERE id IN ({marks})", ids)
        items, lines = [], []
        for r in rows:
            price = math.ceil(econ.sell_price(r["price_base"])) if r["price_base"] is not None else None
            storage = f"{r['storage_gb']}GB" if r["storage_gb"] else ""
            grade = f"Grade {r['grade_norm']}" if r["grade_norm"] and len(r["grade_norm"]) <= 2 else (r["grade_norm"] or r["grade"])
            qty = str(r["qty"]) if r["qty"] else ""
            lines.append(" · ".join(x for x in [f"{r['model']} {storage}".strip(), grade,
                                                 f"{qty} pcs" if qty else "", money(price, econ.base_currency)
                                                 if price is not None else "price on request"] if x))
            items.append({"model": r["model"][:40], "spec": storage, "grade": grade, "qty": qty,
                          "price": str(price) if price is not None else ""})
        headline = (data.get("headline") or "STOCK BLAST")[:26]
        text = ("📦 RePocket stock update\n\n" + "\n".join(lines) +
                "\n\nPrices ex-VAT, subject to availability. Reply here to reserve.")
        symbol = {"EUR": "€", "GBP": "£", "USD": "$"}.get(econ.base_currency, econ.base_currency + " ")
        payload = {"headline": headline, "currency": symbol, "items": items[:8]}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return {"text": text, "studio_url": f"{config.studio_url}#data={encoded}",
                "note": "Stock Blast Studio shows up to 8 lines." if len(items) > 8 else ""}

    # outreach
    @app.post("/api/outreach/import", **api)
    async def outreach_import(file: UploadFile = File(...), campaign: str = Form("outreach"),
                              consent_confirmed: bool = Form(False), queue: bool = Form(True)):
        raw = (await file.read()).decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.DictReader(io.StringIO(raw), dialect=dialect))
        if not rows:
            raise HTTPException(400, "The CSV is empty")
        return engine.import_contacts(rows, campaign=campaign, consent_confirmed=consent_confirmed, queue=queue)

    @app.get("/api/outreach", **api)
    def outreach():
        return db.all("SELECT o.*, c.name, c.company, c.wa_id, c.stage FROM outreach o JOIN contacts c "
                      "ON c.id = o.contact_id ORDER BY o.id DESC LIMIT 500")

    @app.post("/api/jobs/run", **api)
    def run_jobs():
        return engine.run_due()

    @app.get("/api/digest", **api)
    def digest():
        return {"text": engine.digest_text(utcnow())}

    # settings
    @app.get("/api/settings", **api)
    def get_settings():
        econ = engine.econ()
        return {"mode": engine.mode, "target_margin_pct": econ.target_margin_pct,
                "cost_per_unit": econ.cost_per_unit, "cost_pct": econ.cost_pct,
                "fx_rates": econ.fx_rates, "base_currency": econ.base_currency}

    @app.put("/api/settings", **api)
    def put_settings(data: dict = Body(...)):
        if "mode" in data:
            if data["mode"] not in ("approve", "auto"):
                raise HTTPException(400, "mode must be approve or auto")
            db.set_setting("mode", data["mode"])
        for key in ("target_margin_pct", "cost_per_unit", "cost_pct"):
            if key in data:
                value = _num(data[key])
                if value is None or value < 0:
                    raise HTTPException(400, f"{key} must be a positive number")
                db.set_setting(key, str(value))
        if "fx_rates" in data:
            fx = {str(k).upper(): float(v) for k, v in (data["fx_rates"] or {}).items() if _num(v)}
            db.set_setting("fx_rates", json.dumps(fx))
            _reprice(db, engine)
        if "base_currency" in data and data["base_currency"]:
            db.set_setting("base_currency", str(data["base_currency"]).upper()[:3])
            _reprice(db, engine)
        return get_settings()

    # simulator
    @app.post("/api/simulate", **api)
    def simulate(data: dict = Body(...)):
        sim = state.sim
        text = (data.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "Type a message")
        wa_id = normalize_wa_id(data.get("phone") or "+447000000001") or "447000000001"
        msg = Inbound(wa_id=wa_id, message_id=f"sim.{secrets.token_hex(6)}", type="text", text=text,
                      profile_name=data.get("name") or "")
        contact = sim.receive(msg)
        if not contact:
            raise HTTPException(400, "Duplicate message")
        outcome = sim.process(contact["id"])
        c = sim.db.get_contact(contact["id"])
        if outcome.action == "draft" and outcome.draft_id:
            # in the simulator, show the conversation as if Deepak approved the draft
            sim.db.add_message(c["id"], "out", "bot", outcome.reply, status="simulated")
            sim.discard_draft(outcome.draft_id)
        return {"outcome": outcome.to_dict(), "contact": c, "profile": sim.db.profile(c),
                "messages": sim.db.messages_for(c["id"], 100),
                "offers": sim.db.all("SELECT * FROM offers WHERE contact_id = ?", (c["id"],)),
                "requirements": sim.db.all("SELECT * FROM requirements WHERE contact_id = ?", (c["id"],))}

    @app.post("/api/simulate/reset", **api)
    def simulate_reset(data: dict = Body(default={})):
        sim = state.sim
        wa_id = normalize_wa_id((data or {}).get("phone") or "+447000000001") or "447000000001"
        c = sim.db.contact_by_wa(wa_id)
        if c:
            for table in ("messages", "drafts", "offers", "requirements", "escalations", "outreach"):
                sim.db.execute(f"DELETE FROM {table} WHERE contact_id = ?", (c["id"],))
            sim.db.execute("DELETE FROM contacts WHERE id = ?", (c["id"],))
        return {"ok": True}

    @app.exception_handler(BrainError)
    async def brain_error(_: Request, exc: BrainError):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    return app


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _clean_line(data: dict) -> dict:
    return {
        "brand": data.get("brand", ""), "model": data.get("model", ""), "spec": data.get("spec", ""),
        "storage_gb": int(data["storage_gb"]) if _num(data.get("storage_gb")) else None,
        "grade": data.get("grade", ""), "qty": int(data["qty"]) if _num(data.get("qty")) else None,
        "unit_price": _num(data.get("unit_price")), "currency": data.get("currency", ""),
        "location": data.get("location", ""), "destination": data.get("destination", ""),
        "notes": data.get("notes", ""),
    }


def _reprice(db: Database, engine: Engine) -> None:
    """Recompute base-currency prices after FX or base currency changes."""
    from .normalize import to_base
    econ = engine.econ()
    for table, col in (("offers", "unit_price"), ("requirements", "target_price")):
        for r in db.all(f"SELECT id, {col} AS p, currency FROM {table}"):
            db.update(table, r["id"], {"price_base": to_base(r["p"], r["currency"], econ.base_currency, econ.fx_rates)})


def _apply_status(state: State, st) -> None:
    db = state.db
    row = db.one("SELECT m.id, m.contact_id FROM messages m WHERE m.wa_message_id = ?", (st.message_id,))
    if not row:
        return
    db.update("messages", row["id"], {"status": st.status})
    if st.status == "failed":
        contact = db.get_contact(row["contact_id"])
        if contact:
            state.engine.escalate(contact, f"WhatsApp couldn't deliver our message: {st.error or 'unknown error'}")


async def _debounce_all(state: State, pending: list[tuple[int, int]]) -> None:
    await asyncio.gather(*(_debounced(state, cid, gen) for cid, gen in pending))


async def _debounced(state: State, contact_id: int, gen: int) -> None:
    """Wait for a pause in the conversation so a burst of messages gets one reply."""
    await asyncio.sleep(state.config.debounce_seconds)
    if state.generation.get(contact_id) != gen:
        return
    try:
        await asyncio.to_thread(state.engine.process, contact_id)
    except Exception:  # noqa: BLE001 — never let one conversation crash the worker
        log.exception("processing contact %s failed", contact_id)
        contact = state.db.get_contact(contact_id)
        if contact:
            state.engine.escalate(contact, "Something went wrong handling their last message. Please reply yourself.")


async def _scheduler(state: State) -> None:
    while True:
        try:
            await asyncio.to_thread(state.engine.run_due)
        except Exception:  # noqa: BLE001
            log.exception("scheduled jobs failed")
        await asyncio.sleep(60)
