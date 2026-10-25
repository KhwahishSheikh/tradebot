"""Configuration: layered YAML for settings, .env for secrets, guarded paper/live switch.

Load order (later wins): config/default.yaml -> config/local.yaml -> $TRADEBOT_CONFIG.
Field bounds below are hard safety ceilings: no YAML file can configure past them.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tradebot.timing.windows import TimeWindow, Weekday, hhmm_to_minutes

LIVE_CONFIRM_PHRASE = "I ACCEPT REAL MONEY RISK"


class Mode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typos in YAML fail loudly


class AccountConfig(_Strict):
    capital_inr: float = Field(100_000, gt=0)


class LiveConfig(_Strict):
    confirm: str = ""
    max_capital_inr: float = Field(25_000, gt=0)


class RiskConfig(_Strict):
    max_risk_per_trade_pct: float = Field(1.0, gt=0, le=2.0)
    max_daily_loss_pct: float = Field(2.5, gt=0, le=5.0)
    max_drawdown_pct: float = Field(10.0, gt=0, le=20.0)
    max_open_positions: int = Field(3, ge=1, le=10)
    max_consecutive_losses: int = Field(3, ge=1, le=10)
    loss_cooldown_hours: float = Field(24, ge=1)
    max_portfolio_heat_pct: float = Field(3.0, gt=0, le=6.0)     # sum of open risk-at-stop
    max_gross_leverage: float = Field(2.0, gt=0, le=5.0)          # total notional / equity
    max_positions_per_symbol: int = Field(1, ge=1, le=3)
    min_liq_buffer: float = Field(2.0, ge=1.5)     # liq distance >= this x stop distance
    max_data_age_bars: int = Field(2, ge=1)        # block if latest bar is older than this
    max_price_drift_pct: float = Field(0.5, gt=0, le=2.0)  # signal entry vs live price
    events_file: Path = Path("config/calendars/events.yaml")


class CryptoConfig(_Strict):
    enabled: bool = True
    base_url: str = "https://api.india.delta.exchange"
    symbols: list[str] = ["BTCUSD", "ETHUSD"]
    max_leverage: float = Field(3, ge=1, le=5)
    usd_inr: float = Field(88.0, gt=50, lt=150)    # Delta India INR<->USD rate; check your wallet


class EquityConfig(_Strict):
    enabled: bool = True
    broker: Literal["angel_one"] = "angel_one"
    watchlist: list[str] = ["NIFTY", "BANKNIFTY"]


class ExpiryConfig(_Strict):
    intraday: int = Field(5, ge=1, le=30)
    swing: int = Field(60, ge=5, le=240)


class AutoApproveConfig(_Strict):
    enabled: bool = False
    max_risk_inr: float = Field(0, ge=0)


class ApprovalConfig(_Strict):
    expiry_minutes: ExpiryConfig = ExpiryConfig()
    max_price_drift_pct: float = Field(0.3, gt=0, le=2.0)
    exit_on_target: Literal["auto", "ask"] = "auto"
    auto_approve: AutoApproveConfig = AutoApproveConfig()


class TelegramConfig(_Strict):
    allowed_user_ids: list[int] = []
    require_pin_live: bool = True


class ExpiryRule(_Strict):
    underlying: str                     # NIFTY, BANKNIFTY, or STOCKS for single-stock F&O
    cycle: Literal["weekly", "monthly"]
    weekday: Weekday                    # monthly = last <weekday> of the month


class NseTimingConfig(_Strict):
    holidays_file: Path = Path("config/calendars/nse_holidays.yaml")
    avoid_open_minutes: int = Field(15, ge=0, le=60)
    avoid_close_minutes: int = Field(15, ge=0, le=60)
    no_new_intraday_after: str = "14:45"
    square_off_at: str = "15:10"
    expiries: list[ExpiryRule] = []

    @field_validator("no_new_intraday_after", "square_off_at")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not (9 * 60 + 15 <= hhmm_to_minutes(v) < 15 * 60 + 30):
            raise ValueError(f"{v} must be inside the NSE session 09:15-15:30")
        return v


class CryptoTimingConfig(_Strict):
    funding_interval_hours: int = Field(8, ge=1, le=24)
    funding_anchor_utc: str = "00:00"
    funding_blackout_minutes: int = Field(5, ge=0, le=60)
    low_liquidity_utc: list[TimeWindow] = []
    block_new_entries_in_low_liquidity: bool = False

    @field_validator("funding_anchor_utc")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        hhmm_to_minutes(v)
        return v


class TimingConfig(_Strict):
    nse: NseTimingConfig = NseTimingConfig()
    crypto: CryptoTimingConfig = CryptoTimingConfig()
    availability_ist: list[TimeWindow] = []   # empty = always available


class DataConfig(_Strict):
    db_path: Path = Path("data/market.duckdb")
    delta_rate_per_sec: float = Field(5, gt=0, le=20)
    angel_rate_per_sec: float = Field(3, gt=0, le=3)     # Angel historical API limit
    angel_master_cache: Path = Path("data/angel_master.json")
    angel_client_local_ip: str = "127.0.0.1"
    angel_client_public_ip: str = "127.0.0.1"            # set to VPS static IP before going live
    angel_mac_address: str = "00:00:00:00:00:00"
    lookback_days: dict[str, int] = {"1m": 7, "5m": 30, "15m": 60, "1h": 365, "1d": 1500}


Timeframe = Literal["5m", "15m", "30m", "1h", "4h", "1d"]


class StrategyEntry(_Strict):
    name: str
    enabled: bool = True
    symbols: list[str] = ["BTCUSD", "ETHUSD"]
    timeframe: Timeframe = "1h"
    max_hold_bars: int = Field(48, ge=1)
    regimes: list[Literal["trend_up", "trend_down", "ranging", "neutral"]] | None = None  # None = strategy default
    params: dict[str, float] = {}


class StrategiesConfig(_Strict):
    min_confidence: float = Field(0.6, ge=0.5, le=1.0)
    min_net_rr: float = Field(1.5, ge=1.0)          # reward:risk after fees + expected funding
    crypto: list[StrategyEntry] = []


class CryptoCosts(_Strict):
    """Delta India BTCUSD product spec: maker 0.02%, taker 0.05%; 18% GST on fees (India)."""
    taker_fee_pct: float = 0.05
    maker_fee_pct: float = 0.02
    gst_pct: float = 18.0
    slippage_pct: float = 0.02

    @property
    def round_trip_pct(self) -> float:
        """Taker in + taker out, incl. GST, + slippage both sides (worst case: market orders)."""
        return 2 * (self.taker_fee_pct * (1 + self.gst_pct / 100) + self.slippage_pct)


class CostsConfig(_Strict):
    crypto: CryptoCosts = CryptoCosts()


class NewsConfig(_Strict):
    enabled: bool = True
    db_path: Path = Path("data/news.duckdb")
    feeds: dict[str, str] = {}                 # empty = built-in crypto feeds
    classifier: Literal["claude", "rules"] = "claude"   # falls back to rules without a key
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high"] = "low"
    batch_size: int = Field(25, ge=1, le=60)
    max_items_per_run: int = Field(150, ge=1, le=1000)  # cost cap per classify run
    classify_window_hours: int = Field(48, ge=1)
    lookback_hours: int = Field(72, ge=6)
    use_in_signals: bool = True


class ExecutionConfig(_Strict):
    db_path: Path = Path("data/execution.db")
    paper_state_file: Path = Path("data/paper_exchange.json")
    max_entry_slippage_pct: float = Field(0.10, gt=0, le=0.5)   # IOC limit cap vs live price
    stop_trigger: Literal["last_traded_price", "mark_price"] = "last_traded_price"
    order_retries: int = Field(3, ge=0, le=5)
    bracket_retries: int = Field(3, ge=1, le=5)
    reconcile_seconds: int = Field(120, ge=15, le=900)


class ExitsConfig(_Strict):
    trailing_enabled: bool = False                      # decided by backtest evidence (Ticket 10)
    trail_activate_r: float = Field(1.0, ge=0.5, le=3)  # start trailing once trade is +1R
    trail_atr_mult: float = Field(2.0, ge=0.5, le=5)    # trail distance = mult x ATR at entry
    extend_min_r: float = Field(1.0, ge=0)              # at max-hold, offer extension only if >= +1R
    extend_hours: int = Field(48, ge=1, le=240)
    max_extensions: int = Field(1, ge=0, le=3)
    decision_timeout_min: int = Field(30, ge=5, le=240) # no answer -> the SAFE default (close)
    adverse_funding_pct_day: float = Field(0.10, gt=0)  # review alert if paying more than this/day
    review_time_ist: str = "09:00"


class MonitoringConfig(_Strict):
    heartbeat_file: Path = Path("data/heartbeat.json")
    state_file: Path = Path("data/runtime_state.json")
    tick_seconds: int = Field(60, ge=10, le=600)          # exits + reconcile cadence
    signal_delay_seconds: int = Field(75, ge=10, le=600)  # wait after bar close before evaluating
    news_minutes: int = Field(30, ge=5, le=240)
    summary_time_ist: str = "21:30"
    stale_data_hours: float = Field(2.5, gt=0)            # -> safe mode
    max_tick_failures: int = Field(3, ge=1)               # -> safe mode
    watchdog_stale_seconds: int = Field(300, ge=60)
    alert_cooldown_min: int = Field(10, ge=1)


class PaperConfig(_Strict):
    ledger_path: Path = Path("data/forward.db")
    shadow_disabled_strategies: bool = True   # forward-test disabled strategies without trading them
    min_weeks: int = Field(4, ge=1)
    target_weeks: int = Field(8, ge=1)
    min_forward_trades: int = Field(30, ge=5)  # forward sample needed before trusting the edge
    uptime_target_pct: float = Field(98.0, ge=50, le=100)
    max_avg_r_gap: float = Field(0.30, gt=0)   # paper vs backtest, per trade
    min_decided_pct: float = Field(80.0, ge=0, le=100)   # proposals you answered before expiry


class LoggingConfig(_Strict):
    dir: Path = Path("logs")
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class AppConfig(_Strict):
    mode: Mode = Mode.PAPER
    account: AccountConfig = AccountConfig()
    live: LiveConfig = LiveConfig()
    risk: RiskConfig = RiskConfig()
    crypto: CryptoConfig = CryptoConfig()
    equity: EquityConfig = EquityConfig()
    approval: ApprovalConfig = ApprovalConfig()
    telegram: TelegramConfig = TelegramConfig()
    timing: TimingConfig = TimingConfig()
    data: DataConfig = DataConfig()
    strategies: StrategiesConfig = StrategiesConfig()
    costs: CostsConfig = CostsConfig()
    news: NewsConfig = NewsConfig()
    execution: ExecutionConfig = ExecutionConfig()
    exits: ExitsConfig = ExitsConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    paper: PaperConfig = PaperConfig()
    logging: LoggingConfig = LoggingConfig()

    @property
    def is_live(self) -> bool:
        return self.mode is Mode.LIVE


class Secrets(BaseSettings):
    """Read from environment / .env only. Never logged or dumped in clear text."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    delta_api_key: SecretStr | None = None
    delta_api_secret: SecretStr | None = None
    angel_api_key: SecretStr | None = None
    angel_client_code: SecretStr | None = None
    angel_pin: SecretStr | None = None
    angel_totp_secret: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    telegram_approval_pin: SecretStr | None = None     # required for approvals in live mode
    tradebot_allow_live: bool = False

    def values(self) -> list[str]:
        """Raw secret strings, used by the log redactor."""
        return [
            v.get_secret_value()
            for v in self.__dict__.values()
            if isinstance(v, SecretStr) and v.get_secret_value()
        ]

    def status(self) -> dict[str, bool]:
        return {k: bool(v) for k, v in self.__dict__.items() if k != "tradebot_allow_live"}


class LiveModeError(RuntimeError):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("Live mode refused:\n  - " + "\n  - ".join(reasons))


def validate_live(cfg: AppConfig, secrets: Secrets) -> None:
    """Raise LiveModeError unless every live-trading precondition holds. No-op in paper mode."""
    if not cfg.is_live:
        return
    reasons = []
    if cfg.live.confirm != LIVE_CONFIRM_PHRASE:
        reasons.append(f'live.confirm must be exactly "{LIVE_CONFIRM_PHRASE}"')
    if not secrets.tradebot_allow_live:
        reasons.append("TRADEBOT_ALLOW_LIVE=true is not set in the environment")
    if cfg.account.capital_inr > cfg.live.max_capital_inr:
        reasons.append(
            f"account.capital_inr ({cfg.account.capital_inr:,.0f}) exceeds "
            f"live.max_capital_inr ({cfg.live.max_capital_inr:,.0f})"
        )
    if not cfg.telegram.allowed_user_ids or not secrets.telegram_bot_token:
        reasons.append("approvals need TELEGRAM_BOT_TOKEN and telegram.allowed_user_ids")
    if cfg.telegram.require_pin_live and not secrets.telegram_approval_pin:
        reasons.append("telegram.require_pin_live is on but TELEGRAM_APPROVAL_PIN is not set")
    if cfg.approval.auto_approve.enabled:
        reasons.append("auto_approve must stay off until paper + live track record exists")
    if cfg.crypto.enabled and not (secrets.delta_api_key and secrets.delta_api_secret):
        reasons.append("crypto enabled but DELTA_API_KEY / DELTA_API_SECRET missing")
    if cfg.equity.enabled and not all(
        [secrets.angel_api_key, secrets.angel_client_code, secrets.angel_pin, secrets.angel_totp_secret]
    ):
        reasons.append("equity enabled but ANGEL_* credentials incomplete")
    if reasons:
        raise LiveModeError(reasons)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(config_dir: Path | str = "config", env_file: Path | str | None = ".env"
                ) -> tuple[AppConfig, Secrets]:
    config_dir = Path(config_dir)
    layers = [config_dir / "default.yaml", config_dir / "local.yaml"]
    if extra := os.environ.get("TRADEBOT_CONFIG"):
        layers.append(Path(extra))

    merged: dict[str, Any] = {}
    for path in layers:
        if path.exists():
            merged = _deep_merge(merged, yaml.safe_load(path.read_text()) or {})

    cfg = AppConfig.model_validate(merged)
    secrets = Secrets(_env_file=env_file)
    validate_live(cfg, secrets)
    return cfg, secrets
