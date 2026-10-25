import json
import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tradebot.audit import AuditEvent, AuditLog
from tradebot.config import (
    LIVE_CONFIRM_PHRASE,
    AppConfig,
    LiveModeError,
    Mode,
    Secrets,
    load_config,
    validate_live,
)
from tradebot.execution.exchange import PaperExchange
from tradebot.execution.factory import make_exchange
from tradebot.logging_setup import RedactSecrets

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ["TRADEBOT_CONFIG", "TRADEBOT_ALLOW_LIVE", "DELTA_API_KEY", "DELTA_API_SECRET",
              "TELEGRAM_BOT_TOKEN"]:
        monkeypatch.delenv(k, raising=False)


def write_cfg(tmp_path, data, local=None):
    d = tmp_path / "config"
    d.mkdir()
    (d / "default.yaml").write_text((ROOT / "config/default.yaml").read_text())
    if data:
        (d / "local.yaml").write_text(yaml.safe_dump(data))
    return d


def full_live_secrets(**over):
    base = {"tradebot_allow_live": True, "delta_api_key": "dk-123456", "delta_api_secret": "ds-123456",
                "telegram_bot_token": "tg-123456", "telegram_approval_pin": "482916"}
    return Secrets(_env_file=None, **(base | over))


def live_cfg(**over):
    data = {"mode": "live", "account": {"capital_inr": 20000},
            "live": {"confirm": LIVE_CONFIRM_PHRASE, "max_capital_inr": 25000},
            "equity": {"enabled": False}, "telegram": {"allowed_user_ids": [111]}} | over
    return AppConfig.model_validate(data)


def test_defaults_are_paper(tmp_path):
    cfg, _ = load_config(write_cfg(tmp_path, None), env_file=None)
    assert cfg.mode is Mode.PAPER
    assert cfg.risk.max_risk_per_trade_pct == 1.0
    assert cfg.approval.auto_approve.enabled is False


def test_local_override_deep_merges(tmp_path):
    cfg, _ = load_config(write_cfg(tmp_path, {"risk": {"max_open_positions": 2}}), env_file=None)
    assert cfg.risk.max_open_positions == 2
    assert cfg.risk.max_daily_loss_pct == 2.5  # untouched sibling survives


@pytest.mark.parametrize("override", [
    {"risk": {"max_risk_per_trade_pct": 5}},
    {"risk": {"max_drawdown_pct": 50}},
    {"crypto": {"max_leverage": 25}},
    {"risk": {"max_risk_pct_typo": 1}},
])
def test_safety_ceilings_and_typos_rejected(tmp_path, override):
    with pytest.raises(ValidationError):
        load_config(write_cfg(tmp_path, override), env_file=None)


def test_live_requires_every_key():
    validate_live(live_cfg(), full_live_secrets())  # all present -> ok

    with pytest.raises(LiveModeError, match="TRADEBOT_ALLOW_LIVE"):
        validate_live(live_cfg(), full_live_secrets(tradebot_allow_live=False))
    with pytest.raises(LiveModeError, match="live.confirm"):
        validate_live(live_cfg(live={"confirm": "yes"}), full_live_secrets())
    with pytest.raises(LiveModeError, match="exceeds"):
        validate_live(live_cfg(account={"capital_inr": 500000}), full_live_secrets())
    with pytest.raises(LiveModeError, match="TELEGRAM"):
        validate_live(live_cfg(telegram={"allowed_user_ids": []}), full_live_secrets())
    with pytest.raises(LiveModeError, match="TELEGRAM_APPROVAL_PIN"):
        validate_live(live_cfg(), full_live_secrets(telegram_approval_pin=None))
    with pytest.raises(LiveModeError, match="auto_approve"):
        validate_live(live_cfg(approval={"auto_approve": {"enabled": True}}), full_live_secrets())


def test_live_yaml_alone_is_refused(tmp_path):
    d = write_cfg(tmp_path, {"mode": "live", "live": {"confirm": LIVE_CONFIRM_PHRASE}})
    with pytest.raises(LiveModeError):
        load_config(d, env_file=None)


def test_paper_mode_always_gets_paper_exchange(tmp_path):
    cfg = AppConfig.model_validate({"execution": {"paper_state_file": str(tmp_path / "p.json")}})
    assert isinstance(make_exchange(cfg, Secrets(_env_file=None), {}), PaperExchange)


def test_factory_rechecks_live():
    cfg = live_cfg()
    with pytest.raises(LiveModeError):
        make_exchange(cfg, full_live_secrets(tradebot_allow_live=False), {})


def test_audit_writes_jsonl_by_ist_date(tmp_path):
    audit = AuditLog(tmp_path, Mode.PAPER)
    entry = audit.record(AuditEvent.PROPOSAL_APPROVED, proposal_id="abc", by=111)
    files = list((tmp_path / "audit").glob("audit-*.jsonl"))
    assert len(files) == 1 and entry["ts_ist"][:10] in files[0].name
    row = json.loads(files[0].read_text().splitlines()[0])
    assert row["event"] == "proposal_approved" and row["mode"] == "paper"
    assert row["data"] == {"proposal_id": "abc", "by": 111}
    assert row["ts_ist"].endswith("+05:30")


def test_secrets_redacted_from_logs():
    rec = logging.LogRecord("x", logging.INFO, "", 0, "key=%s ok", ("sk-SUPERSECRET",), None)
    RedactSecrets(["sk-SUPERSECRET"]).filter(rec)
    assert rec.getMessage() == "key=*** ok"
