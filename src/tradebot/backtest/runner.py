"""Backtest orchestration: signals -> execution sim -> portfolio (live risk engine) -> metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from tradebot.backtest import metrics as M
from tradebot.backtest.execution import ExecData
from tradebot.backtest.portfolio import BacktestResult, SignalEvent, run_portfolio
from tradebot.config import AppConfig, StrategyEntry
from tradebot.core.models import Market
from tradebot.data.quality import RESOLUTION_SECONDS
from tradebot.data.store import MarketStore
from tradebot.risk.engine import CryptoRiskEngine
from tradebot.risk.events import EventCalendar
from tradebot.risk.sizing import ContractSpec
from tradebot.strategies.engine import SignalEngine
from tradebot.timing.clock import TradingClock


@dataclass
class Context:
    cfg: AppConfig
    store: MarketStore
    signals: SignalEngine
    risk: CryptoRiskEngine
    exec_data: dict[str, ExecData]
    closes: dict[str, pd.Series]
    _features: dict

    def features(self, symbol: str, timeframe: str) -> pd.DataFrame:
        key = (symbol, timeframe)
        if key not in self._features:
            self._features[key] = self.signals.features(symbol, timeframe)
        return self._features[key]


def build_context(cfg: AppConfig, store: MarketStore, specs: dict[str, ContractSpec],
                  ignore_quiet_hours: bool = False) -> Context:
    if ignore_quiet_hours:
        cfg = cfg.model_copy(deep=True)
        cfg.timing.availability_ist = []
    symbols = sorted({s for e in cfg.strategies.crypto if e.enabled for s in e.symbols})
    exec_data = {s: ExecData.load(store, s) for s in symbols}
    closes = {}
    for s in symbols:
        h = store.candles(Market.CRYPTO_DELTA, s, "1h")
        closes[s] = h.set_index("ts")["close"]
    risk = CryptoRiskEngine(cfg, specs, TradingClock(cfg), EventCalendar([]))
    return Context(cfg, store, SignalEngine(cfg, store), risk, exec_data, closes, {})


def generate_events(ctx: Context, entry: StrategyEntry, start: datetime | None = None,
                    end: datetime | None = None) -> list[SignalEvent]:
    """Every signal that passes the strategy engine's gates (regime, confidence, net R:R)."""
    strat = ctx.signals.build(entry)
    step = pd.Timedelta(seconds=RESOLUTION_SECONDS[strat.timeframe])
    out = []
    for sym in entry.symbols:
        f = strat.prepare(ctx.features(sym, strat.timeframe))
        ts = f["ts"]
        lo = 0 if start is None else int(ts.searchsorted(pd.Timestamp(start)))
        hi = len(f) - 1 if end is None else int(ts.searchsorted(pd.Timestamp(end)))
        for i in range(max(lo, strat.warmup), hi):
            ev = ctx.signals.evaluate(strat, f, i, sym)
            if ev and ev.passed:
                out.append(SignalEvent(ev.signal, (ts.iat[i] + step).to_pydatetime(), ev.regime))
    return out


def evaluate(ctx: Context, res: BacktestResult, start, end) -> dict:
    df = M.trades_frame(res)
    eq = M.daily_equity(res, ctx.closes, start, end, ctx.cfg.crypto.usd_inr)
    blocked = pd.DataFrame(res.blocked, columns=["ts", "strategy", "stage", "reason"])
    if not blocked.empty:
        blocked["reason_kind"] = blocked["reason"].str.split(":").str[0].str.slice(0, 40)
    btc = ctx.closes.get("BTCUSD")
    return {
        "curve": M.curve_stats(eq), "trades": M.trade_stats(df),
        "by_strategy": M.breakdown(df, "strategy"), "by_symbol": M.breakdown(df, "symbol"),
        "by_regime": M.breakdown(df, "regime"), "by_exit": M.breakdown(df, "exit_reason"),
        "tax": M.tax_estimate(df),
        "blocked": blocked.groupby(["stage", "reason_kind"]).size().rename("count").reset_index()
        if not blocked.empty else blocked,
        "benchmark_btc": M.buy_and_hold(btc[(btc.index >= pd.Timestamp(start)) & (btc.index < pd.Timestamp(end))])
        if btc is not None else {},
        "halts": [(h.kind.value, h.created_at, h.reason) for h in res.halts],
        "equity": eq, "trades_df": df, "blocked_df": blocked,
    }


def run_backtest(ctx: Context, entries: list[StrategyEntry], start: datetime, end: datetime,
                 delay: timedelta) -> tuple[BacktestResult, dict]:
    events = [e for en in entries for e in generate_events(ctx, en, start, end)]
    res = run_portfolio(ctx.cfg, ctx.risk, events, ctx.exec_data, delay)
    return res, evaluate(ctx, res, start, end)


def data_window(ctx: Context) -> tuple[datetime, datetime]:
    """Usable period: every symbol has 5m execution data AND 1h features past warmup."""
    starts, ends = [], []
    for ex in ctx.exec_data.values():
        starts.append(pd.Timestamp(ex.ts[0]).tz_localize("UTC"))
        ends.append(pd.Timestamp(ex.ts[-1]).tz_localize("UTC"))
    return max(starts).to_pydatetime(), min(ends).to_pydatetime()


def write_report(out_dir: Path, title: str, r: dict, notes: list[str]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    r["trades_df"].to_csv(out_dir / "trades.csv", index=False)
    r["equity"].rename("equity_inr").to_csv(out_dir / "equity_daily.csv")
    if not r["blocked_df"].empty:
        r["blocked_df"].to_csv(out_dir / "blocked.csv", index=False)
    md = [f"# {title}", "", "## Equity curve (daily, marked to market)", "",
          _kv(r["curve"]), "", "## BTC buy & hold (same period)", "", _kv(r["benchmark_btc"]),
          "", "## Trades", "", _kv(r["trades"]), ""]
    for k in ("by_strategy", "by_symbol", "by_regime", "by_exit", "blocked"):
        if len(r[k]):
            md += [f"## {k.replace('_', ' ')}", "", r[k].to_markdown(index=False), ""]
    md += ["## Tax estimate (India; NOT advice)", "", _kv(r["tax"]), ""]
    if r["halts"]:
        md += ["## Risk halts triggered", ""] + [f"- {k} at {t:%Y-%m-%d %H:%M}: {why}" for k, t, why in r["halts"]] + [""]
    md += ["## Assumptions", ""] + [f"- {n}" for n in notes]
    path = out_dir / "summary.md"
    path.write_text("\n".join(md))
    return path


def _kv(d: dict) -> str:
    return "\n".join(f"- **{k}**: {v}" for k, v in d.items()) if d else "- (none)"
