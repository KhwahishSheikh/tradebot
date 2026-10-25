"""Signal engine: stored bars -> features -> strategy setups -> gated, funding-adjusted Signals.

Gates applied to every setup (in order):
  1. regime must be one the strategy trades in; HIGH_VOL / UNKNOWN always off
  2. direction must not fight the regime (no longs in TREND_DOWN, no shorts in TREND_UP)
  3. confidence (after funding adjustment) >= strategies.min_confidence
  4. reward:risk after round-trip costs + expected funding >= strategies.min_net_rr
A setup failing a gate is still returned (passed=False, with reasons) so research and the
audit log can see what was filtered and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from tradebot.config import AppConfig, StrategyEntry
from tradebot.core.models import Instrument, Market, Side, Signal
from tradebot.data.quality import RESOLUTION_SECONDS
from tradebot.data.store import MarketStore
from tradebot.news.score import modifier as news_modifier
from tradebot.strategies.base import Setup, Strategy, clip01
from tradebot.strategies.crypto import REGISTRY
from tradebot.strategies.features import Regime, build_features
from tradebot.strategies.funding import FundingImpact, funding_impact

STORED_RES = {"5m": "5m", "15m": "5m", "30m": "5m", "1h": "1h", "4h": "1h", "1d": "1h"}


@dataclass
class Evaluation:
    strategy: str
    symbol: str
    ts: pd.Timestamp
    regime: str
    setup: Setup
    confidence: float
    net_rr: float
    funding: FundingImpact
    passed: bool
    blocked_by: list[str] = field(default_factory=list)
    news_notes: list[str] = field(default_factory=list)
    news_score: float | None = None
    signal: Signal | None = None
    enabled: bool = True             # False = shadow: recorded for forward-testing, never proposed


def resample_bars(bars: pd.DataFrame, timeframe: str, base: str) -> pd.DataFrame:
    """Aggregate base bars to timeframe; drop incomplete buckets (incl. the still-forming one)."""
    if timeframe == base:
        return bars
    rule = pd.Timedelta(seconds=RESOLUTION_SECONDS[timeframe])
    need = RESOLUTION_SECONDS[timeframe] // RESOLUTION_SECONDS[base]
    g = bars.set_index("ts").resample(rule, label="left", closed="left")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    out = out[g.size() == need].dropna(subset=["close"])
    return out.reset_index()


def bars_per_day(timeframe: str) -> int:
    return 86400 // RESOLUTION_SECONDS[timeframe]


class SignalEngine:
    def __init__(self, cfg: AppConfig, store: MarketStore, news=None):
        self.cfg = cfg
        self.store = store
        self.news = news            # NewsScorer | None: confidence modifier only
        self.scfg = cfg.strategies
        self.round_trip_pct = cfg.costs.crypto.round_trip_pct

    @staticmethod
    def build(entry: StrategyEntry) -> Strategy:
        if entry.name not in REGISTRY:
            raise ValueError(f"unknown strategy {entry.name}; have {sorted(REGISTRY)}")
        strat = REGISTRY[entry.name](timeframe=entry.timeframe,
                                     max_hold_bars=entry.max_hold_bars, **entry.params)
        if entry.regimes is not None:
            strat.regimes = frozenset(Regime(r) for r in entry.regimes)
        return strat

    def enabled(self) -> list[tuple[Strategy, list[str]]]:
        return [(self.build(e), e.symbols) for e in self.scfg.crypto if e.enabled]

    # ---------- features ----------

    def features(self, symbol: str, timeframe: str, start=None, end=None) -> pd.DataFrame:
        base = STORED_RES[timeframe]
        bars = self.store.candles(Market.CRYPTO_DELTA, symbol, base, start, end)
        if bars.empty:
            raise LookupError(f"no {base} bars for {symbol}; run `tradebot data backfill`")
        bars = resample_bars(bars, timeframe, base)
        funding = self.store.candles(Market.CRYPTO_DELTA, f"FUNDING:{symbol}", "1h", start, end)
        days = 90 if RESOLUTION_SECONDS[timeframe] >= 3600 else 20
        return build_features(bars, funding, vol_window=days * bars_per_day(timeframe))

    # ---------- evaluation ----------

    def evaluate(self, strat: Strategy, f: pd.DataFrame, i: int, symbol: str) -> Evaluation | None:
        if i < strat.warmup:
            return None
        setup = strat.decide(f, i)
        if setup is None or setup.risk <= 0:
            return None
        r = f.iloc[i]
        regime = r["regime"]
        hold_hours = strat.max_hold_bars * RESOLUTION_SECONDS[strat.timeframe] / 3600 / 2
        fi = funding_impact(setup.side, r["funding"], setup.entry, setup.risk, hold_hours)
        cost_r = self.round_trip_pct / 100 * setup.entry / setup.risk
        net_rr = setup.rr - cost_r - fi.cost_r
        news_delta, news_notes, news_score = 0.0, [], None
        if self.news is not None:
            bar_close = r["ts"] + pd.Timedelta(seconds=RESOLUTION_SECONDS[strat.timeframe])
            nctx = self.news.context(symbol, bar_close.to_pydatetime())
            news_delta, news_notes = news_modifier(nctx, setup.side)
            news_score = round(nctx.score, 3)
        conf = clip01(setup.confidence + fi.confidence_delta + news_delta)

        blocked = []
        if regime in (Regime.HIGH_VOL, Regime.UNKNOWN) or regime not in strat.regimes:
            blocked.append(f"regime {regime} not traded by {strat.name}")
        if (setup.side is Side.BUY and regime == Regime.TREND_DOWN) or (
                setup.side is Side.SELL and regime == Regime.TREND_UP):
            blocked.append(f"{setup.side.value} against {regime}")
        if conf < self.scfg.min_confidence:
            blocked.append(f"confidence {conf:.2f} < {self.scfg.min_confidence}")
        if net_rr < self.scfg.min_net_rr:
            blocked.append(f"net R:R {net_rr:.2f} < {self.scfg.min_net_rr} "
                           f"(fees {cost_r:.2f}R, funding {fi.cost_r:+.2f}R)")

        ev = Evaluation(strat.name, symbol, r["ts"], regime, setup, conf, net_rr, fi,
                        not blocked, blocked, news_notes=news_notes, news_score=news_score)
        if ev.passed:
            ev.signal = self._to_signal(strat, symbol, ev)
        return ev

    def _to_signal(self, strat: Strategy, symbol: str, ev: Evaluation) -> Signal:
        s = ev.setup
        bar_close = ev.ts + pd.Timedelta(seconds=RESOLUTION_SECONDS[strat.timeframe])
        reasoning = "; ".join(s.reasons) + f". Regime {ev.regime}. {ev.funding.note}."
        if ev.news_notes:
            reasoning += " " + "; ".join(ev.news_notes) + "."
        return Signal(
            instrument=Instrument(Market.CRYPTO_DELTA, symbol),
            side=s.side, strategy=strat.name, horizon=strat.horizon,
            entry=_dec(s.entry), stop_loss=_dec(s.stop), target=_dec(s.target),
            confidence=round(ev.confidence, 2), reasoning=reasoning,
            created_at=bar_close.to_pydatetime(), timeframe=strat.timeframe, regime=ev.regime,
            max_hold=timedelta(seconds=RESOLUTION_SECONDS[strat.timeframe] * strat.max_hold_bars),
            features={**s.features, "net_rr": round(ev.net_rr, 2), "news_score": ev.news_score})

    def latest(self, include_disabled: bool = False) -> list[Evaluation]:
        """Evaluate strategies on the most recent closed bar. include_disabled -> shadow mode."""
        out = []
        for entry in self.scfg.crypto:
            if not entry.enabled and not include_disabled:
                continue
            strat = self.build(entry)
            for sym in entry.symbols:
                f = strat.prepare(self.features(sym, strat.timeframe))
                ev = self.evaluate(strat, f, len(f) - 1, sym)
                if ev:
                    ev.enabled = entry.enabled
                    out.append(ev)
        return out


def _dec(x: float):
    from decimal import Decimal
    return Decimal(f"{x:.2f}")
