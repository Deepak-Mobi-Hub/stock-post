"""Prompt building, schema shape, and the real SDK request (over a mock transport, no API calls)."""

import json

import anthropic
import httpx2
import pytest

from tradedesk.brain import (EXTRACT_SCHEMA, TURN_SCHEMA, BrainError, ClaudeBrain, TurnContext, TurnResult,
                             user_prompt)
from tradedesk.config import Config


def walk_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for v in schema.values():
            yield from walk_objects(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from walk_objects(v)


@pytest.mark.parametrize("schema", [TURN_SCHEMA, EXTRACT_SCHEMA])
def test_schemas_are_strict(schema):
    for obj in walk_objects(schema):
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def ctx(**kw):
    base = dict(
        contact={"name": "Markus", "company": "ITAD GmbH", "type": "supplier", "stage": "engaged", "wa_id": "49",
                 "followups_sent": 0, "notes": "Met at EBM"},
        profile={"categories": "laptops"},
        transcript=[
            {"direction": "out", "author": "bot", "body": "Hi Markus 👋", "created_at": "2026-09-24T09:00:00+00:00"},
            {"direction": "in", "author": "contact", "body": "What would you pay for 200 Latitudes?",
             "created_at": "2026-09-24T10:00:00+00:00"},
        ],
    )
    base.update(kw)
    return TurnContext(**base)


def test_user_prompt_has_what_the_model_needs():
    p = user_prompt(ctx(market_signals=["Buyers we talk to are looking for: Latitude 5420 (~100 units)"]))
    assert "Markus: What would you pay for 200 Latitudes?" in p
    assert "Deepak: Hi Markus 👋" in p
    assert "still worth learning: monthly_volume, grades_condition, stock_location, sales_method" in p
    assert "Never name the other party" in p
    assert "Met at EBM" in p
    assert "assistant" not in p.split("<task>")[1]  # no disclosure line unless asked


def test_user_prompt_disclosure_and_followup():
    p = user_prompt(ctx(disclose_assistant=True, task="followup"))
    task = p.split("<task>")[1]
    assert "hasn't replied" in task and "Deepak's assistant" in task


def test_turn_result_ignores_unknown_keys():
    r = TurnResult.from_json({"reply": "hi", "stage": "engaged", "surprise": 1})
    assert r.reply == "hi" and r.stage == "engaged"


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def fake_stream(text, stop_reason="end_turn"):
    return sse([
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                              "model": "claude-opus-5", "content": [], "stop_reason": None,
                                              "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ])


def make_brain(text, stop_reason="end_turn", model="claude-opus-5"):
    captured = {}

    def handler(request: httpx2.Request):
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=fake_stream(text, stop_reason).encode())

    client = anthropic.Anthropic(api_key="test-key", base_url="https://api.anthropic.test",
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return ClaudeBrain(Config(claude_model=model), client=client), captured


def turn_json(**over):
    data = {"reply": "Thanks Markus, let me run it past our buyers.", "needs_approval": True,
            "approval_reason": "price requested", "contact": {"name": "", "company": "", "role": "", "country": "",
                                                              "email": "", "type": "supplier"},
            "facts": [{"field": "monthly_volume", "value": "200 laptops"}], "stage": "engaged", "stock_offers": [],
            "requirements": [], "escalate": True, "escalation_reason": "Asked what we'd pay", "opt_out": False,
            "followup_in_days": 0, "summary_for_owner": "Wants an offer on 200 Latitudes"}
    data.update(over)
    return json.dumps(data)


def test_real_sdk_request_shape():
    brain, captured = make_brain(turn_json())
    result = brain.turn(ctx())
    assert result.escalate and result.needs_approval and result.facts[0]["value"] == "200 laptops"

    body, headers = captured["body"], captured["headers"]
    assert body["model"] == "claude-opus-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Deepak from Mobi-Hub" in body["system"][0]["text"]  # the playbook is in the cached system prompt
    assert body["stream"] is True
    assert "<transcript>" in body["messages"][0]["content"]


def test_no_fallback_param_for_models_without_it():
    brain, captured = make_brain(turn_json(), model="claude-sonnet-5")
    brain.turn(ctx())
    assert "fallbacks" not in captured["body"]
    assert "server-side-fallback" not in captured["headers"].get("anthropic-beta", "")


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_unusable_answers_raise(stop):
    brain, _ = make_brain("{}", stop_reason=stop)
    with pytest.raises(BrainError):
        brain.turn(ctx())


def test_extract_sends_document_blocks():
    brain, captured = make_brain(json.dumps({"stock_offers": [], "requirements": [], "notes": ""}))
    block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0="}}
    brain.extract([block], "hint")
    content = captured["body"]["messages"][0]["content"]
    assert content[0]["type"] == "document" and captured["body"]["max_tokens"] == 64000


def test_missing_api_key_is_a_clear_error(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    client = anthropic.Anthropic(base_url="https://api.anthropic.test",
                                 http_client=anthropic.DefaultHttpxClient(
                                     transport=httpx2.MockTransport(lambda r: httpx2.Response(500))))
    brain = ClaudeBrain(Config(), client=client)
    with pytest.raises(BrainError, match="ANTHROPIC_API_KEY"):
        brain.turn(ctx())
