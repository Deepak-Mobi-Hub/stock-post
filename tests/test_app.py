import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from conftest import sent_to
from tradedesk.app import create_app
from tradedesk.brain import TurnResult

AUTH = ("deepak", "secret")


@pytest.fixture
def client(config, brain, wa):
    app = create_app(config, brain=brain, wa=wa, run_scheduler=False)
    with TestClient(app) as c:
        c.td = app.state.td
        yield c


def signed(body: dict, secret="app-secret"):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def wa_payload(text, wa_id="4915112345678", name="Markus", mid="wamid.1"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "123"},
        "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
        "messages": [{"from": wa_id, "id": mid, "timestamp": "1790000000", "type": "text", "text": {"body": text}}],
    }}]}]}


def test_webhook_verification(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                        "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
    assert bad.status_code == 403


def test_webhook_rejects_unsigned_calls(client):
    raw, headers = signed(wa_payload("hi"), secret="wrong")
    assert client.post("/webhook", content=raw, headers=headers).status_code == 401


def test_webhook_message_flows_to_a_draft(client, brain):
    brain.results = [TurnResult(reply="Hi Markus, thanks for getting in touch.", stage="engaged",
                                contact={"name": "", "company": "ITAD GmbH", "role": "", "country": "Germany",
                                         "email": "", "type": "supplier"})]
    raw, headers = signed(wa_payload("Hello, we're an ITAD in Hamburg"))
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200
    drafts = client.get("/api/drafts", auth=AUTH).json()
    assert len(drafts) == 1 and drafts[0]["company"] == "ITAD GmbH"
    # same webhook delivered twice (Meta retries) is ignored
    client.post("/webhook", content=raw, headers=headers)
    assert len(brain.contexts) == 1


def test_delivery_failure_is_escalated(client, wa, brain):
    raw, headers = signed(wa_payload("hi"))
    client.post("/webhook", content=raw, headers=headers)
    [draft] = client.get("/api/drafts", auth=AUTH).json()
    client.post(f"/api/drafts/{draft['id']}/send", auth=AUTH, json={})
    wamid = sent_to(wa, "4915112345678")[-1]["id"]
    status = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": wamid, "status": "failed", "recipient_id": "4915112345678",
         "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}
    raw, headers = signed(status)
    client.post("/webhook", content=raw, headers=headers)
    esc = client.get("/api/escalations", auth=AUTH).json()
    assert "131047" in esc[0]["reason"]


def test_dashboard_needs_login(client):
    assert client.get("/").status_code == 401
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", auth=("deepak", "wrong")).status_code == 401
    assert client.get("/", auth=AUTH).status_code == 200
    s = client.get("/api/summary", auth=AUTH).json()
    assert s["mode"] == "approve" and s["whatsapp_live"] is False


def test_manual_contact_message_and_edit(client, wa):
    c = client.post("/api/contacts", auth=AUTH, json={"phone": "+44 7700 900500", "name": "Anna", "type": "buyer"}).json()
    # no inbound yet, so WhatsApp's 24h window is closed for free text
    r = client.post(f"/api/contacts/{c['id']}/message", auth=AUTH, json={"body": "Hi Anna"})
    assert r.status_code == 409
    r = client.post(f"/api/contacts/{c['id']}/template", auth=AUTH, json={"which": "intro"})
    assert r.status_code == 409  # intro template needs consent first
    client.patch(f"/api/contacts/{c['id']}", auth=AUTH, json={"consent": 1})
    assert client.post(f"/api/contacts/{c['id']}/template", auth=AUTH, json={"which": "intro"}).status_code == 200
    assert sent_to(wa, "447700900500")[-1]["type"] == "template"


def test_simulator_is_isolated(client, brain):
    brain.results = [TurnResult(reply="Nice to meet you Tom! What do you mainly sell?", stage="engaged",
                                facts=[{"field": "business_type", "value": "ITAD"}])]
    r = client.post("/api/simulate", auth=AUTH, json={"text": "Hi, we're an ITAD", "name": "Tom"}).json()
    assert r["outcome"]["reply"].startswith("Nice to meet you")
    assert r["profile"] == {"business_type": "ITAD"}
    assert [m["direction"] for m in r["messages"]] == ["in", "out"]
    # nothing leaked into the real pipeline
    assert client.get("/api/contacts", auth=AUTH).json() == []
    assert client.get("/api/drafts", auth=AUTH).json() == []
    client.post("/api/simulate/reset", auth=AUTH, json={})


def test_stock_upload_opportunities_and_blast(client):
    buyer = client.post("/api/contacts", auth=AUTH, json={"phone": "+33600000001", "name": "Elena", "type": "buyer"}).json()
    supplier = client.post("/api/contacts", auth=AUTH, json={"phone": "+4915100000002", "name": "Markus",
                                                             "company": "ITAD GmbH", "type": "supplier"}).json()
    client.post("/api/requirements", auth=AUTH, json={"model": "iPhone 13 Pro", "storage_gb": 128, "grades": ["A"],
                                                      "qty": 30, "target_price": 345, "currency": "EUR",
                                                      "contact_id": buyer["id"]})
    csv_data = b"Model,Grade,Qty,Price EUR\niPhone 13 Pro 128GB,A,50,300\n"
    up = client.post("/api/stocklist", auth=AUTH, files={"file": ("list.csv", csv_data, "text/csv")},
                     data={"contact_id": str(supplier["id"]), "kind": "offers"})
    assert up.json() == {"offers": 1, "requirements": 0}

    [op] = client.get("/api/opportunities?status=good", auth=AUTH).json()
    assert op["supplier"] == "Markus (ITAD GmbH)" and op["margin_pct"] > 8

    b = client.post("/api/blast", auth=AUTH, json={"offer_ids": [op["offer_id"]]}).json()
    assert "iPhone 13 Pro 128GB" in b["text"] and "Markus" not in b["text"] and "ITAD" not in b["text"]
    assert "€300" not in b["text"]  # our buy price never goes to buyers
    encoded = b["studio_url"].split("#data=")[1]
    data = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert data["items"][0]["price"] and int(data["items"][0]["price"]) > 300


def test_settings_roundtrip_reprices(client):
    s = client.post("/api/contacts", auth=AUTH, json={"phone": "+447700900900", "name": "UK Supplier"}).json()
    client.post("/api/offers", auth=AUTH, json={"model": "iPhone 12", "unit_price": 100, "currency": "GBP",
                                                "contact_id": s["id"]})
    client.put("/api/settings", auth=AUTH, json={"fx_rates": {"GBP": 1.2}, "target_margin_pct": 10, "mode": "auto"})
    got = client.get("/api/settings", auth=AUTH).json()
    assert got["target_margin_pct"] == 10 and got["mode"] == "auto" and got["fx_rates"] == {"GBP": 1.2}
    [offer] = client.get("/api/offers", auth=AUTH).json()
    assert offer["price_base"] == 120
    assert client.put("/api/settings", auth=AUTH, json={"mode": "yolo"}).status_code == 400


def test_csv_outreach_import(client):
    csv_data = "phone,name,company,type\n+447700900301,Anna,A Ltd,buyer\n0770090030,Bad,B,buyer\n"
    r = client.post("/api/outreach/import", auth=AUTH, files={"file": ("p.csv", csv_data, "text/csv")},
                    data={"campaign": "EBM", "consent_confirmed": "true"}).json()
    assert r["added"] == 1 and r["queued"] == 1 and r["skipped"] == 1
    assert client.get("/api/outreach", auth=AUTH).json()[0]["status"] == "queued"


def test_erase_keeps_suppression(client, brain):
    raw, headers = signed(wa_payload("hi, here is our stock"))
    client.post("/webhook", content=raw, headers=headers)
    [c] = client.get("/api/contacts", auth=AUTH).json()
    assert client.post(f"/api/contacts/{c['id']}/erase", auth=AUTH).status_code == 200
    d = client.get(f"/api/contacts/{c['id']}", auth=AUTH).json()
    assert d["messages"] == [] and d["contact"]["name"] == "" and d["contact"]["opted_out"] == 1
    r = client.post("/api/outreach/import", auth=AUTH, files={"file": ("p.csv", "phone\n+4915112345678\n", "text/csv")},
                    data={"campaign": "x", "consent_confirmed": "true"}).json()
    assert r["queued"] == 0  # can't be re-imported into outreach
