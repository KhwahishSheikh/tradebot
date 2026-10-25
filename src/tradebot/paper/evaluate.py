"""Give every forward signal an outcome with the backtest's own 5m execution model, so paper,
shadow and rejected signals are all measured the same way (net R after fees, GST, slippage,
funding). Runs hourly in the runtime; only signals whose max-hold window has fully passed."""

from __future__ import annotations

import logging
from datetime import timedelta

from tradebot.backtest.execution import ExecData, simulate_trade
from tradebot.config import AppConfig
from tradebot.core.models import Side
from tradebot.data.store import MarketStore
from tradebot.paper.ledger import ForwardLedger
from tradebot.timeutil import now_utc

log = logging.getLogger(__name__)
SIM_DELAY = timedelta(minutes=3)   # same approval delay the backtests assume


def evaluate_outcomes(cfg: AppConfig, ledger: ForwardLedger, store: MarketStore, now=None) -> int:
    now = now or now_utc()
    rows = ledger.awaiting_outcome(now)
    done = 0
    for r in rows:
        start = r.bar_close - timedelta(hours=1)
        end = r.bar_close + timedelta(hours=r.max_hold_h + 2)
        try:
            ex = ExecData.load(store, r.symbol, start, end)
        except LookupError:
            continue                              # 5m data not there yet; retry next hour
        x = cfg.exits
        trail = x.trail_atr_mult * r.atr_pct * r.entry if x.trailing_enabled else 0.0
        sim = simulate_trade(ex, Side(r.side), r.bar_close, r.entry, r.stop, r.target,
                             timedelta(hours=r.max_hold_h), cfg.costs.crypto, SIM_DELAY,
                             cfg.risk.max_price_drift_pct, trail, x.trail_activate_r)
        if sim.status == "filled" and sim.reason == "open_at_data_end":
            continue                              # data ends before the trade did: wait
        ledger.set_outcome(r.signal_id, sim.status, sim.reason,
                           round(sim.net_r, 4) if sim.status == "filled" else None)
        done += 1
    return done
