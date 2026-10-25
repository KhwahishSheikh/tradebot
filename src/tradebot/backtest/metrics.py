"""Backtest metrics: equity-curve stats (daily, marked to market), trade stats, breakdowns, tax."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from tradebot.backtest.portfolio import BacktestResult

CESS = 0.04


def trades_frame(res: BacktestResult) -> pd.DataFrame:
    if not res.trades:
        return pd.DataFrame()
    return pd.DataFrame([{
        "strategy": t.strategy, "symbol": t.symbol, "side": t.side, "regime": t.regime,
        "signal_ts": t.signal_ts, "entry_ts": t.sim.entry_ts, "exit_ts": t.sim.exit_ts,
        "entry_px": t.sim.entry_px, "exit_px": t.sim.exit_px, "exit_reason": t.sim.reason,
        "contracts": t.contracts, "qty": t.qty, "pnl_inr": t.pnl_inr, "risk_inr": t.risk_inr,
        "r": t.r, "gross_inr": t.qty * t.sim.gross_pu * (t.pnl_inr / t.pnl_usd if t.pnl_usd else 0),
        "fees_inr": t.qty * t.sim.fees_pu * (t.pnl_inr / t.pnl_usd if t.pnl_usd else 0),
        "funding_inr": t.qty * t.sim.funding_pu * (t.pnl_inr / t.pnl_usd if t.pnl_usd else 0),
        "hold_h": (t.sim.exit_ts - t.sim.entry_ts).total_seconds() / 3600,
    } for t in res.trades])


def daily_equity(res: BacktestResult, closes: dict[str, pd.Series], start, end,
                 usd_inr: float) -> pd.Series:
    """Equity at each UTC day end = realised + unrealised of positions open at that moment."""
    days = pd.date_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(),
                         freq="1D", tz="UTC") + pd.Timedelta(days=1)
    eq = pd.Series(res.start_equity, index=days, dtype=float)
    for t in res.trades:
        exit_ts, entry_ts = pd.Timestamp(t.sim.exit_ts), pd.Timestamp(t.sim.entry_ts)
        eq[eq.index >= exit_ts] += t.pnl_inr
        open_days = eq.index[(eq.index > entry_ts) & (eq.index < exit_ts)]
        if len(open_days):
            px = closes[t.symbol].reindex(open_days, method="ffill")
            sgn = 1 if t.side == "buy" else -1
            entry_fee_inr = t.qty * t.sim.entry_px * 0.0006 * usd_inr
            eq[open_days] += (t.qty * sgn * (px - t.sim.entry_px) * usd_inr - entry_fee_inr).fillna(0).to_numpy()
    return eq


def curve_stats(eq: pd.Series) -> dict:
    if len(eq) < 2:
        return {}
    ret = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365)
    dd = eq / eq.cummax() - 1
    sd = ret.std()
    down = ret[ret < 0].std()
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    return {
        "start_equity": round(eq.iloc[0]), "end_equity": round(eq.iloc[-1]),
        "total_return_%": round((eq.iloc[-1] / eq.iloc[0] - 1) * 100, 2),
        "CAGR_%": round(cagr * 100, 2), "max_DD_%": round(dd.min() * 100, 2),
        "sharpe": round(ret.mean() / sd * math.sqrt(365), 2) if sd else 0.0,
        "sortino": round(ret.mean() / down * math.sqrt(365), 2) if down and not np.isnan(down) else 0.0,
        "calmar": round(cagr / abs(dd.min()), 2) if dd.min() < 0 else 0.0,
        "years": round(years, 2),
    }


def trade_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"trades": 0}
    pnl = df["pnl_inr"]
    wins, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    streak = (pnl <= 0).astype(int)
    return {
        "trades": len(df), "win_%": round(100 * (pnl > 0).mean(), 1),
        "profit_factor": round(wins / losses, 2) if losses else float("inf"),
        "avg_R": round(df["r"].mean(), 3), "expectancy_inr": round(pnl.mean(), 0),
        "net_pnl_inr": round(pnl.sum(), 0), "fees_inr": round(df["fees_inr"].sum(), 0),
        "funding_inr": round(df["funding_inr"].sum(), 0),
        "max_loss_streak": int(streak.groupby((streak == 0).cumsum()).sum().max()),
        "avg_hold_h": round(df["hold_h"].mean(), 1),
    }


def breakdown(df: pd.DataFrame, by: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    rows = [{by: k, **trade_stats(g)} for k, g in df.groupby(by)]
    return pd.DataFrame(rows)


def tax_estimate(df: pd.DataFrame) -> dict:
    """Two readings of Indian tax on crypto derivatives (unsettled; ask a CA):
    VDA (s.115BBH): 30% + cess on each gain, losses can't offset gains.
    Business income: slab rate (30% assumed) + cess on NET profit."""
    if df.empty:
        return {}
    gains = df.loc[df["pnl_inr"] > 0, "pnl_inr"].sum()
    net = df["pnl_inr"].sum()
    rate = 0.30 * (1 + CESS)
    vda, biz = gains * rate, max(net, 0) * rate
    return {"net_pnl_inr": round(net), "tax_if_VDA_inr": round(vda),
            "after_tax_if_VDA_inr": round(net - vda),
            "tax_if_business_inr": round(biz), "after_tax_if_business_inr": round(net - biz)}


def buy_and_hold(closes: pd.Series) -> dict:
    eq = closes.resample("1D").last().dropna()
    return curve_stats(eq / eq.iloc[0] * 100)
