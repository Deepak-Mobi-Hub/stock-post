"""Runtime configuration, read from environment variables.

Secrets (API keys, WhatsApp token) live only in the environment. Business
settings that change often (margin, costs, FX, mode) have env defaults but can
be overridden from the dashboard; those overrides are stored in the database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def parse_fx(raw: str) -> dict[str, float]:
    """Parse "GBP=1.17,USD=0.92" into {"GBP": 1.17, "USD": 0.92}.

    Each rate is how many units of the base currency one unit is worth.
    """
    rates: dict[str, float] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        code, value = part.split("=", 1)
        code = code.strip().upper()
        if code:
            rates[code] = float(value)
    return rates


@dataclass
class Config:
    # Claude
    claude_model: str = "claude-opus-5"
    claude_effort: str = "medium"

    # WhatsApp Cloud API (Meta)
    wa_token: str = ""
    wa_phone_number_id: str = ""
    wa_verify_token: str = ""
    wa_app_secret: str = ""
    wa_api_version: str = "v24.0"
    wa_template_intro: str = "repocket_intro"
    wa_template_followup: str = "repocket_followup"
    wa_template_owner_alert: str = ""
    wa_template_lang: str = "en"

    # Owner (Deepak)
    owner_wa_id: str = ""
    owner_name: str = "Deepak"

    # Behaviour
    mode: str = "approve"  # "approve": every reply is a draft; "auto": safe replies go out
    ai_disclosure: bool = True  # in auto mode, first auto-sent message says it's an assistant
    debounce_seconds: float = 20.0
    mark_read: bool = False
    followup_days: tuple[int, ...] = (3, 7, 14)
    outreach_daily_limit: int = 30
    send_window_utc: tuple[int, int] = (7, 17)  # outreach/follow-ups only in this hour range, Mon–Fri
    digest_hour_utc: int = 7

    # Trading economics (dashboard can override)
    base_currency: str = "EUR"
    fx_rates: dict[str, float] = field(default_factory=lambda: {"GBP": 1.17, "USD": 0.92, "AED": 0.25})
    target_margin_pct: float = 8.0
    cost_per_unit: float = 3.0
    cost_pct: float = 1.5

    # App
    database_path: str = "data/tradedesk.db"
    dashboard_user: str = "deepak"
    dashboard_password: str = ""
    dev_mode: bool = False
    playbook_path: str = str(PACKAGE_DIR / "persona" / "playbook.md")
    studio_url: str = "https://deepak-mobi-hub.github.io/stock-post/"

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.wa_token and self.wa_phone_number_id)

    @classmethod
    def from_env(cls) -> "Config":
        followups = tuple(int(x) for x in _env("FOLLOWUP_DAYS", "3,7,14").split(",") if x.strip())
        window = [int(x) for x in _env("SEND_WINDOW_UTC", "7-17").split("-")]
        return cls(
            claude_model=_env("CLAUDE_MODEL", cls.claude_model),
            claude_effort=_env("CLAUDE_EFFORT", cls.claude_effort),
            wa_token=_env("WHATSAPP_TOKEN"),
            wa_phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
            wa_verify_token=_env("WHATSAPP_VERIFY_TOKEN"),
            wa_app_secret=_env("WHATSAPP_APP_SECRET"),
            wa_api_version=_env("WHATSAPP_API_VERSION", cls.wa_api_version),
            wa_template_intro=_env("WA_TEMPLATE_INTRO", cls.wa_template_intro),
            wa_template_followup=_env("WA_TEMPLATE_FOLLOWUP", cls.wa_template_followup),
            wa_template_owner_alert=_env("WA_TEMPLATE_OWNER_ALERT"),
            wa_template_lang=_env("WA_TEMPLATE_LANG", cls.wa_template_lang),
            owner_wa_id=normalize_wa_id(_env("OWNER_WHATSAPP")) or "",
            owner_name=_env("OWNER_NAME", cls.owner_name),
            mode=_env("MODE", cls.mode).lower(),
            ai_disclosure=_env_bool("AI_DISCLOSURE", True),
            debounce_seconds=_env_float("DEBOUNCE_SECONDS", 20.0),
            mark_read=_env_bool("MARK_READ", False),
            followup_days=followups or (3, 7, 14),
            outreach_daily_limit=_env_int("OUTREACH_DAILY_LIMIT", 30),
            send_window_utc=(window[0], window[1]) if len(window) == 2 else (7, 17),
            digest_hour_utc=_env_int("DIGEST_HOUR_UTC", 7),
            base_currency=_env("BASE_CURRENCY", "EUR").upper(),
            fx_rates=parse_fx(_env("FX_RATES", "GBP=1.17,USD=0.92,AED=0.25")),
            target_margin_pct=_env_float("TARGET_MARGIN_PCT", 8.0),
            cost_per_unit=_env_float("COST_PER_UNIT", 3.0),
            cost_pct=_env_float("COST_PCT", 1.5),
            database_path=_env("DATABASE_PATH", cls.database_path),
            dashboard_user=_env("DASHBOARD_USER", cls.dashboard_user),
            dashboard_password=_env("DASHBOARD_PASSWORD"),
            dev_mode=_env_bool("DEV_MODE", False),
            playbook_path=_env("PLAYBOOK_PATH", cls.playbook_path),
            studio_url=_env("STUDIO_URL", cls.studio_url),
        )


def normalize_wa_id(raw: str | None) -> str | None:
    """Turn a phone number into a WhatsApp ID (digits only, international format).

    Returns None when the number can't be made international with confidence
    (e.g. a local number starting with a single 0).
    """
    if not raw:
        return None
    s = str(raw).strip().replace("(0)", "")  # "+49 (0) 151…": the (0) is a trunk prefix, not part of the number
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    if s.startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:]
    if digits.startswith("0"):
        return None
    return digits if len(digits) >= 8 else None
