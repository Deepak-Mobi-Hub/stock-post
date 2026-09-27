import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradedesk.brain import TurnResult  # noqa: E402
from tradedesk.config import Config  # noqa: E402
from tradedesk.db import Database, iso  # noqa: E402
from tradedesk.engine import Engine  # noqa: E402
from tradedesk.whatsapp import DryRunWhatsApp, Inbound  # noqa: E402

OWNER = "447700900001"
# A Tuesday, 10:00 UTC — inside the default weekday send window.
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


class FakeBrain:
    """Scripted stand-in for Claude."""

    def __init__(self):
        self.results: list[TurnResult] = []
        self.contexts = []
        self.extracted = {"stock_offers": [], "requirements": [], "notes": ""}
        self.extract_calls = []

    def turn(self, ctx):
        self.contexts.append(ctx)
        if self.results:
            return self.results.pop(0)
        return TurnResult(reply="Thanks, good to hear from you.", stage="engaged", followup_in_days=3)

    def extract(self, content, hint):
        self.extract_calls.append((content, hint))
        return self.extracted


@pytest.fixture
def config(tmp_path):
    return Config(database_path=":memory:", owner_wa_id=OWNER, debounce_seconds=0, mode="approve",
                  dashboard_password="secret", wa_verify_token="verify-me", wa_app_secret="app-secret")


@pytest.fixture
def db(config):
    return Database(config.database_path)


@pytest.fixture
def brain():
    return FakeBrain()


@pytest.fixture
def wa():
    return DryRunWhatsApp()


@pytest.fixture
def engine(config, db, brain, wa):
    eng = Engine(config, db, brain, wa)
    db.set_setting("owner_last_inbound", iso())  # owner chatted recently, so alerts go out as text
    return eng


_counter = iter(range(1, 10_000))


def inbound(text="", wa_id="31612345678", name="Tom", **kw) -> Inbound:
    return Inbound(wa_id=wa_id, message_id=kw.pop("message_id", f"wamid.{next(_counter)}"), type=kw.pop("type", "text"),
                   text=text, profile_name=name, **kw)


def sent_to(wa, number):
    return [m for m in wa.sent if m["to"] == number]


os.environ.setdefault("TRADEDESK_TESTING", "1")
