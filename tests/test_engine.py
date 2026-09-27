import io
from datetime import timedelta

import pytest
from openpyxl import Workbook

from conftest import NOW, OWNER, inbound, sent_to
from tradedesk.brain import BrainError, TurnResult
from tradedesk.db import iso
from tradedesk.engine import OPT_OUT_CONFIRMATION, WindowClosed, is_opt_out
from tradedesk.whatsapp import SendError

TOM = "31612345678"


def receive_and_process(engine, text, now=NOW, **kw):
    contact = engine.receive(inbound(text, **kw), now=now)
    return contact, engine.process(contact["id"], now=now)


# ---------------------------------------------------------------- approve vs auto
def test_approve_mode_drafts_every_reply_and_pings_owner(engine, wa, db):
    contact, out = receive_and_process(engine, "Hi Deepak, we're an ITAD in Rotterdam")
    assert out.action == "draft" and out.draft_id
    assert sent_to(wa, TOM) == []  # nothing to the prospect yet
    alert = sent_to(wa, OWNER)[-1]["body"]
    assert f"Draft #{out.draft_id}" in alert and f"send {out.draft_id}" in alert
    assert db.get_contact(contact["id"])["stage"] == "engaged"


def test_auto_mode_sends_safe_replies(engine, wa, db, brain):
    db.set_setting("mode", "auto")
    brain.results = [TurnResult(reply="Great to meet you Tom. What do you mostly handle, laptops?", stage="engaged",
                                followup_in_days=3)]
    contact, out = receive_and_process(engine, "Hi")
    assert out.action == "sent"
    assert sent_to(wa, TOM)[-1]["body"].startswith("Great to meet you")
    c = db.get_contact(contact["id"])
    assert c["disclosed"] == 1 and c["next_followup_at"] == iso(NOW + timedelta(days=3))
    assert brain.contexts[0].disclose_assistant is True  # first auto message says it's an assistant


def test_auto_mode_still_drafts_anything_commercial(engine, wa, db, brain):
    db.set_setting("mode", "auto")
    brain.results = [TurnResult(reply="Let me run it past the team.", needs_approval=True,
                                approval_reason="price requested", escalate=True,
                                escalation_reason="Asked what we'd pay", stage="engaged")]
    contact, out = receive_and_process(engine, "What would you pay for 200 Latitude 5420?")
    assert out.action == "draft"
    assert sent_to(wa, TOM) == []
    assert db.one("SELECT COUNT(*) AS n FROM escalations WHERE status = 'open'")["n"] == 1


def test_brain_failure_escalates_instead_of_going_silent(engine, wa, brain):
    def boom(ctx):
        raise BrainError("Claude declined to answer this one")
    brain.turn = boom
    _, out = receive_and_process(engine, "hello")
    assert out.action == "error"
    assert "Assistant couldn't reply" in sent_to(wa, OWNER)[-1]["body"]


# ---------------------------------------------------------------- conversation mechanics
def test_burst_of_messages_gets_one_reply(engine, brain):
    c1 = engine.receive(inbound("Hi Deepak"), now=NOW)
    engine.receive(inbound("we have 400 laptops"), now=NOW)
    engine.receive(inbound("list attached tomorrow"), now=NOW)
    engine.process(c1["id"], now=NOW)
    engine.process(c1["id"], now=NOW)  # second debounced call finds nothing new
    assert len(brain.contexts) == 1
    bodies = [m["body"] for m in brain.contexts[0].transcript]
    assert bodies == ["Hi Deepak", "we have 400 laptops", "list attached tomorrow"]


def test_webhook_retries_are_ignored(engine):
    msg = inbound("hello", message_id="wamid.same")
    assert engine.receive(msg, now=NOW)
    assert engine.receive(msg, now=NOW) is None


def test_stage_never_goes_backwards(engine, db, brain):
    contact, _ = receive_and_process(engine, "hi")
    db.update_contact(contact["id"], stage="trading")
    brain.results = [TurnResult(reply="ok", stage="engaged")]
    engine.receive(inbound("thanks"), now=NOW)
    engine.process(contact["id"], now=NOW)
    assert db.get_contact(contact["id"])["stage"] == "trading"


def test_contact_details_fill_blanks_only(engine, db, brain):
    brain.results = [TurnResult(reply="x", contact={"name": "Tomas", "company": "Reworked Tech", "type": "supplier",
                                                    "role": "", "country": "UK", "email": ""},
                                facts=[{"field": "monthly_volume", "value": "400-500 laptops"}])]
    contact, _ = receive_and_process(engine, "hi")
    c = db.get_contact(contact["id"])
    assert c["name"] == "Tom"  # WhatsApp profile name already there, not overwritten
    assert (c["company"], c["type"], c["country"]) == ("Reworked Tech", "supplier", "UK")
    assert db.profile(c)["monthly_volume"] == "400-500 laptops"


# ---------------------------------------------------------------- opt-out
@pytest.mark.parametrize("text", ["STOP", "stop.", "Unsubscribe", "please remove me from your list",
                                  "Don't contact me again", "abmelden"])
def test_opt_out_phrases(text):
    assert is_opt_out(text)


@pytest.mark.parametrize("text", ["Stock list attached", "We stopped selling Samsung", "non-stop busy week"])
def test_not_opt_out(text):
    assert not is_opt_out(text)


def test_stop_is_confirmed_once_and_respected(engine, wa, db, brain):
    contact, out = receive_and_process(engine, "STOP")
    assert out.action == "opted_out"
    assert sent_to(wa, TOM)[-1]["body"] == OPT_OUT_CONFIRMATION
    assert brain.contexts == []  # never asked the model
    c = db.get_contact(contact["id"])
    assert c["opted_out"] == 1 and c["stage"] == "opted_out"

    before = len(sent_to(wa, TOM))
    engine.receive(inbound("why?"), now=NOW)
    out2 = engine.process(contact["id"], now=NOW)
    assert out2.action == "opted_out" and len(sent_to(wa, TOM)) == before
    with pytest.raises(SendError, match="opted out"):
        engine.send_template(db.get_contact(contact["id"]), "followup", now=NOW)


# ---------------------------------------------------------------- stock
def xlsx_bytes(rows):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_stock_list_on_whatsapp_is_saved_and_matched(engine, wa, db, brain):
    buyer = db.upsert_contact("33600000000", name="Elena", company="Refurb SA", type="buyer")
    from tradedesk.stock import save_requirement
    save_requirement(db, engine.econ(), buyer["id"], {"model": "Latitude 5420", "grades": ["B"], "qty": 50,
                                                      "target_price": 300, "currency": "EUR"})
    wa.media["media-1"] = (xlsx_bytes([["Model", "Grade", "Qty", "Price EUR"], ["Dell Latitude 5420", "B", 40, 240]]),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    contact = engine.receive(inbound("Here's our list", type="document", media_id="media-1",
                                     filename="stock.xlsx"), now=NOW)
    out = engine.process(contact["id"], now=NOW)
    assert out.offers_saved == 1
    offer = db.one("SELECT * FROM offers WHERE contact_id = ?", (contact["id"],))
    assert offer["price_base"] == 240 and offer["source"] == "file:stock.xlsx"
    # the model saw a note that the list was saved
    assert any("stock lines saved" in m["body"] for m in brain.contexts[0].transcript)
    alerts = " ".join(m["body"] for m in sent_to(wa, OWNER))
    assert "New trade match" in alerts and "Elena (Refurb SA)" in alerts


def test_new_list_replaces_previous_list(engine, db):
    contact = db.upsert_contact(TOM, name="Tom", type="supplier")
    engine.ingest_file(contact, "a.csv", "text/csv", b"Model,Qty,Price\niPhone 12,10,200\n", NOW)
    engine.ingest_file(contact, "b.csv", "text/csv", b"Model,Qty,Price\niPhone 13,5,300\n", NOW)
    live = db.all("SELECT model FROM offers WHERE contact_id = ? AND status = 'available'", (contact["id"],))
    assert [r["model"] for r in live] == ["iPhone 13"]


def test_buyer_want_list_becomes_requirements(engine, db):
    contact = db.upsert_contact(TOM, name="Tom", type="buyer")
    offers, reqs = engine.ingest_file(contact, "wants.csv", "text/csv",
                                      b"Model,Grade,Qty,Target price\niPhone 13 128GB,A,100,330\n", NOW)
    assert (offers, reqs) == (0, 1)
    r = db.one("SELECT * FROM requirements")
    assert r["target_price"] == 330 and r["grades"] == "A" and r["storage_gb"] == 128


def test_unreadable_sheet_falls_back_to_claude(engine, db, brain):
    contact = db.upsert_contact(TOM, name="Tom", type="supplier")
    brain.extracted = {"stock_offers": [{"category": "laptop", "brand": "HP", "model": "EliteBook 840 G8",
                                         "storage_gb": 256, "spec": "", "grade": "A", "qty": 12, "unit_price": 310,
                                         "currency": "EUR", "location": "NL", "notes": ""}],
                       "requirements": [], "notes": ""}
    offers, _ = engine.ingest_file(contact, "odd.csv", "text/csv", b"HP EliteBook 840 G8 256GB A 12pcs 310\n", NOW)
    assert offers == 1 and brain.extract_calls


def test_voice_note_goes_to_owner(engine, wa, db):
    contact = engine.receive(inbound("", type="audio", media_id="m-voice", voice=True), now=NOW)
    engine.process(contact["id"], now=NOW)
    assert "voice note" in sent_to(wa, OWNER)[0]["body"]


def test_chat_stock_is_not_duplicated(engine, db, brain):
    line = {"category": "phone", "brand": "Apple", "model": "iPhone 13", "storage_gb": 128, "spec": "", "grade": "A",
            "qty": 20, "unit_price": 330, "currency": "EUR", "location": "", "notes": ""}
    brain.results = [TurnResult(reply="a", stock_offers=[line]), TurnResult(reply="b", stock_offers=[line])]
    contact, _ = receive_and_process(engine, "20x iPhone 13 128 A at 330")
    receive_and_process(engine, "any interest?")
    assert db.one("SELECT COUNT(*) AS n FROM offers WHERE contact_id = ?", (contact["id"],))["n"] == 1


def test_market_signals_are_anonymous(engine, db, brain):
    other = db.upsert_contact("4900000000", name="Secret Supplier", company="HiddenCo", type="supplier")
    from tradedesk.stock import save_offer
    save_offer(db, engine.econ(), other["id"], {"model": "iPhone 14", "qty": 80, "unit_price": 410})
    receive_and_process(engine, "Do you have iPhones?")
    signals = " ".join(brain.contexts[0].market_signals)
    assert "iPhone 14" in signals and "80" in signals
    assert "HiddenCo" not in signals and "Secret" not in signals and "410" not in signals


# ---------------------------------------------------------------- owner commands
def test_owner_send_edit_skip(engine, wa, db):
    contact, out = receive_and_process(engine, "hi")
    engine.receive(inbound(f"send {out.draft_id}", wa_id=OWNER, name="Deepak"), now=NOW)
    assert sent_to(wa, TOM)[-1]["body"] == "Thanks, good to hear from you."
    assert "Sent draft" in sent_to(wa, OWNER)[-1]["body"]

    _, out2 = receive_and_process(engine, "we sell laptops")
    engine.receive(inbound(f"edit {out2.draft_id} Brilliant, thanks Tom. Mostly Dell and HP?", wa_id=OWNER),
                   now=NOW)
    assert sent_to(wa, TOM)[-1]["body"] == "Brilliant, thanks Tom. Mostly Dell and HP?"
    ex = db.one("SELECT * FROM style_examples")
    assert ex["draft"] == "Thanks, good to hear from you." and ex["final"].startswith("Brilliant")

    _, out3 = receive_and_process(engine, "ok")
    engine.receive(inbound(f"skip {out3.draft_id}", wa_id=OWNER), now=NOW)
    assert db.one("SELECT status FROM drafts WHERE id = ?", (out3.draft_id,))["status"] == "discarded"


def test_owner_edits_teach_the_next_turn(engine, brain, db):
    db.insert("style_examples", {"draft": "Dear Sir, kindly revert.", "final": "Hi Tom, any update?",
                                 "created_at": iso(NOW)})
    receive_and_process(engine, "hi")
    assert brain.contexts[0].style_examples[0]["final"] == "Hi Tom, any update?"


def test_owner_pause_and_say(engine, wa, db):
    contact, _ = receive_and_process(engine, "hi")
    engine.receive(inbound("pause Tom", wa_id=OWNER), now=NOW)
    assert db.get_contact(contact["id"])["bot_paused"] == 1
    engine.receive(inbound("say Tom: I'll call you at 3pm", wa_id=OWNER), now=NOW)
    assert sent_to(wa, TOM)[-1]["body"] == "I'll call you at 3pm"
    engine.receive(inbound("still there?"), now=NOW)
    out = engine.process(contact["id"], now=NOW)
    assert out.action == "paused"


def test_new_draft_replaces_stale_one(engine, db):
    _, first = receive_and_process(engine, "hi")
    _, second = receive_and_process(engine, "hello?")
    assert db.one("SELECT status FROM drafts WHERE id = ?", (first.draft_id,))["status"] == "discarded"
    assert db.one("SELECT status FROM drafts WHERE id = ?", (second.draft_id,))["status"] == "pending"


def test_24h_window_blocks_free_text(engine, db):
    _, out = receive_and_process(engine, "hi")
    with pytest.raises(WindowClosed):
        engine.send_draft(out.draft_id, now=NOW + timedelta(hours=25))


# ---------------------------------------------------------------- follow-ups
def test_followup_outside_window_uses_template(engine, wa, db):
    contact = db.upsert_contact(TOM, name="Tom Baker")
    db.update_contact(contact["id"], stage="contacted", next_followup_at=iso(NOW - timedelta(minutes=1)))
    engine.run_due(NOW)
    draft = db.one("SELECT * FROM drafts WHERE contact_id = ?", (contact["id"],))
    assert draft["kind"] == "template" and "Hi Tom," in draft["body"]  # approve mode: you OK it first

    engine.send_draft(draft["id"], now=NOW)
    msg = sent_to(wa, TOM)[-1]
    assert msg["type"] == "template" and msg["params"] == ["Tom"]
    c = db.get_contact(contact["id"])
    assert c["followups_sent"] == 1 and c["next_followup_at"] == iso(NOW + timedelta(days=7))


def test_auto_followups_stop_and_go_dormant(engine, wa, db):
    db.set_setting("mode", "auto")
    contact = db.upsert_contact(TOM, name="Tom")
    db.update_contact(contact["id"], stage="contacted", next_followup_at=iso(NOW))
    t = NOW
    for _ in range(3):
        engine.process_followups(t)
        t = t + timedelta(days=15)
        while t.weekday() >= 5:
            t += timedelta(days=1)
    engine.process_followups(t)
    c = db.get_contact(contact["id"])
    assert len([m for m in sent_to(wa, TOM) if m["type"] == "template"]) == 3
    assert c["stage"] == "dormant" and c["next_followup_at"] is None


def test_no_followups_at_weekends(engine, db):
    contact = db.upsert_contact(TOM, name="Tom")
    db.update_contact(contact["id"], stage="contacted", next_followup_at=iso(NOW))
    saturday = NOW + timedelta(days=4)
    assert engine.process_followups(saturday) == 0


# ---------------------------------------------------------------- outreach
def test_outreach_needs_consent_and_respects_limits(engine, wa, db, config):
    rows = [
        {"phone": "+44 7700 900111", "name": "Anna Smith", "company": "A Ltd", "type": "buyer", "consent": "yes"},
        {"phone": "+44 7700 900112", "name": "Ben", "company": "B Ltd", "consent": "no"},
        {"phone": "07700 900113", "name": "Local Number"},
        {"phone": "+44 7700 900114", "name": "Cara", "consent": "yes"},
    ]
    res = engine.import_contacts(rows, campaign="EBM", consent_confirmed=False)
    assert (res["added"], res["queued"], res["skipped"]) == (3, 2, 1)

    config.outreach_daily_limit = 1
    assert engine.process_outreach(NOW + timedelta(days=4)) == 0  # Saturday: nothing goes out
    assert engine.process_outreach(NOW) == 1
    assert engine.process_outreach(NOW + timedelta(hours=1)) == 0  # daily limit reached
    first = sent_to(wa, "447700900111")[0]
    assert first["type"] == "template" and first["params"] == ["Anna"]
    anna = db.contact_by_wa("447700900111")
    assert anna["stage"] == "contacted" and anna["type"] == "buyer"


def test_outreach_skips_people_already_talking_or_opted_out(engine, db):
    receive_and_process(engine, "hi", wa_id="447700900200")
    receive_and_process(engine, "STOP", wa_id="447700900201")
    res = engine.import_contacts([{"phone": "+447700900200"}, {"phone": "+447700900201"}], campaign="x",
                                 consent_confirmed=True)
    assert res["queued"] == 0


def test_digest(engine, db):
    receive_and_process(engine, "hi")
    text = engine.digest_text(NOW)
    assert "Trade Desk" in text and "Drafts waiting: 1" in text
    assert engine.maybe_digest(NOW.replace(hour=7)) is True
    assert engine.maybe_digest(NOW.replace(hour=7, minute=30)) is False  # once a day


def test_model_detected_opt_out_is_confirmed_not_drafted(engine, wa, db, brain):
    brain.results = [TurnResult(reply="Understood, sorry to bother you.", opt_out=True, stage="opted_out")]
    contact, out = receive_and_process(engine, "Please take us off whatever list this is, not relevant for us")
    assert out.action == "opted_out"
    assert sent_to(wa, TOM)[-1]["body"] == OPT_OUT_CONFIRMATION
    assert db.one("SELECT COUNT(*) AS n FROM drafts WHERE status = 'pending'")["n"] == 0
    assert db.get_contact(contact["id"])["opted_out"] == 1


def test_owner_forwards_stock_list_for_a_supplier(engine, wa, db):
    markus = db.upsert_contact("4915100000001", name="Markus", company="Nord ITAD", type="supplier")
    wa.media["fwd-1"] = (b"Model,Grade,Qty,Price EUR\nLatitude 5420,B,140,228\n", "text/csv")
    engine.receive(inbound("from Markus", wa_id=OWNER, type="document", media_id="fwd-1", filename="nord.csv"),
                   now=NOW)
    offer = db.one("SELECT * FROM offers")
    assert offer["contact_id"] == markus["id"] and offer["price_base"] == 228
    assert "1 stock lines" in sent_to(wa, OWNER)[-1]["body"] and "Markus" in sent_to(wa, OWNER)[-1]["body"]


def test_owner_forwards_buyer_wants(engine, wa, db):
    elena = db.upsert_contact("33600000002", name="Elena", type="buyer")
    wa.media["fwd-2"] = (b"Model,Grade,Qty,Target price\niPhone 13 128GB,A,100,330\n", "text/csv")
    engine.receive(inbound("wants Elena", wa_id=OWNER, type="document", media_id="fwd-2", filename="w.csv"), now=NOW)
    req = db.one("SELECT * FROM requirements")
    assert req["contact_id"] == elena["id"] and req["target_price"] == 330
