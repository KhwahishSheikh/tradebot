"""The ONLY place an exchange connection that can trade is constructed.
Paper unless validate_live() passes (checked again here, never trusting the caller)."""

from __future__ import annotations

from tradebot.audit import AuditLog
from tradebot.config import AppConfig, Secrets, validate_live
from tradebot.execution.engine import ExecutionEngine
from tradebot.execution.exchange import DeltaExchange, Exchange, PaperExchange
from tradebot.execution.orders import ExecutionStore
from tradebot.markets.delta.client import DeltaClient
from tradebot.risk.sizing import ContractSpec
from tradebot.risk.state import RiskStateStore


def make_exchange(cfg: AppConfig, secrets: Secrets, specs: dict[str, ContractSpec]) -> Exchange:
    public = DeltaClient(cfg.crypto.base_url, rate_per_sec=cfg.data.delta_rate_per_sec)
    if not cfg.is_live:
        return PaperExchange(public, specs, cfg.execution.paper_state_file)
    validate_live(cfg, secrets)
    signed = DeltaClient(cfg.crypto.base_url, secrets.delta_api_key.get_secret_value(),
                         secrets.delta_api_secret.get_secret_value(), cfg.data.delta_rate_per_sec)
    return DeltaExchange(signed, specs, cfg.execution.stop_trigger)


def build_execution(cfg: AppConfig, secrets: Secrets, specs: dict[str, ContractSpec],
                    risk_store: RiskStateStore, audit: AuditLog | None, notify=print) -> ExecutionEngine:
    return ExecutionEngine(cfg, make_exchange(cfg, secrets, specs),
                           ExecutionStore(cfg.execution.db_path), specs, risk_store, audit, notify)
