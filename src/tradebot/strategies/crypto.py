"""Crypto perpetual strategies (Delta BTCUSD / ETHUSD). Long and short, ATR-based brackets.

Confidence scores are hand-set heuristics (0.5 base + evidence). They are NOT probabilities;
Ticket 14 calibrates them against your approve/reject history and realised outcomes.
"""

from __future__ import annotations

from tradebot.core.models import Horizon, Side
from tradebot.strategies import indicators as ta
from tradebot.strategies.base import Setup, Strategy, clip01
from tradebot.strategies.features import TRENDING, Regime


def _crossed_above(a, b, i) -> bool:
    return a.iat[i - 1] <= b.iat[i - 1] and a.iat[i] > b.iat[i]


def _crossed_below(a, b, i) -> bool:
    return a.iat[i - 1] >= b.iat[i - 1] and a.iat[i] < b.iat[i]


class EmaTrend(Strategy):
    """EMA20/50 cross in the direction of the EMA200 trend, confirmed by ADX/DI."""

    name = "ema_trend"
    horizon = Horizon.SWING
    regimes = TRENDING | {Regime.NEUTRAL}
    defaults = {"min_adx": 20, "stop_atr": 2.0, "rr": 2.0}

    def decide(self, f, i):
        r = f.iloc[i]
        long_ = _crossed_above(f["ema20"], f["ema50"], i) and r.close > r.ema200 and r.plus_di > r.minus_di
        short = _crossed_below(f["ema20"], f["ema50"], i) and r.close < r.ema200 and r.minus_di > r.plus_di
        if not (long_ or short) or r.adx < self.p["min_adx"]:
            return None
        side = Side.BUY if long_ else Side.SELL
        stop, target = self._bracket(side, r.close, r.atr)
        sgn = 1 if long_ else -1
        conf = 0.5 + 0.15 * (r.adx >= 30) + 0.1 * (r.volume > r.vol_ma) + 0.1 * (sgn * r.macd_hist > 0)
        return Setup(side, r.close, stop, target, clip01(conf), (
            (f"EMA20 crossed {'above' if long_ else 'below'} EMA50, price "
            f"{'above' if long_ else 'below'} EMA200"),
            (f"ADX {r.adx:.0f}, {'+DI>-DI' if long_ else '-DI>+DI'}, "
            f"volume {'above' if r.volume > r.vol_ma else 'below'} 20-bar avg")),
            {"adx": r.adx, "atr_pct": r.atr_pct})


class DonchianBreakout(Strategy):
    """Close beyond the prior N-bar high/low with a rising ADX (trend starting)."""

    name = "donchian_breakout"
    horizon = Horizon.SWING
    regimes = TRENDING | {Regime.NEUTRAL, Regime.RANGING}
    defaults = {"lookback": 48, "stop_atr": 2.5, "rr": 2.5}

    def prepare(self, f):
        return f.join(ta.donchian(f, self.p["lookback"]))

    def decide(self, f, i):
        r, prev = f.iloc[i], f.iloc[i - 1]
        adx_rising = r.adx > f["adx"].iat[i - 5]
        long_ = r.close > r.dc_high and prev.close <= prev.dc_high and r.close > r.ema200
        short = r.close < r.dc_low and prev.close >= prev.dc_low and r.close < r.ema200
        if not (long_ or short) or not adx_rising:
            return None
        side = Side.BUY if long_ else Side.SELL
        stop, target = self._bracket(side, r.close, r.atr)
        conf = 0.5 + 0.1 * (r.volume > 1.5 * r.vol_ma) + 0.1 * (r.adx >= 25) + 0.05 * (
            f["regime"].iat[i - 1] == Regime.RANGING)
        lvl = r.dc_high if long_ else r.dc_low
        return Setup(side, r.close, stop, target, clip01(conf), (
            f"close broke {self.p['lookback']}-bar {'high' if long_ else 'low'} {lvl:,.1f}",
            f"ADX rising to {r.adx:.0f}, volume x{r.volume / r.vol_ma:.1f} of avg"),
            {"adx": r.adx, "atr_pct": r.atr_pct})


class RsiPullback(Strategy):
    """Buy the dip / sell the rip inside an established trend: RSI recovers from an extreme
    while MACD histogram turns."""

    name = "rsi_pullback"
    horizon = Horizon.SWING
    regimes = TRENDING | {Regime.NEUTRAL}
    defaults = {"dip": 40, "recover": 45, "lookback": 6, "stop_atr": 1.5, "rr": 2.0}

    def decide(self, f, i):
        r = f.iloc[i]
        lb = f["rsi"].iloc[i - self.p["lookback"]:i]
        hist_up = r.macd_hist > f["macd_hist"].iat[i - 1]
        uptrend = r.ema50 > r.ema200 and r.close > r.ema200
        downtrend = r.ema50 < r.ema200 and r.close < r.ema200
        long_ = uptrend and lb.min() < self.p["dip"] and f["rsi"].iat[i - 1] < self.p["recover"] <= r.rsi and hist_up
        hi, rec = 100 - self.p["dip"], 100 - self.p["recover"]
        short = downtrend and lb.max() > hi and f["rsi"].iat[i - 1] > rec >= r.rsi and not hist_up
        if not (long_ or short):
            return None
        side = Side.BUY if long_ else Side.SELL
        stop, target = self._bracket(side, r.close, r.atr)
        conf = 0.55 + 0.1 * (r.adx >= 25) + 0.05 * (abs(r.close - r.ema50) / r.atr < 1.5)
        return Setup(side, r.close, stop, target, clip01(conf), (
            (f"{'uptrend' if long_ else 'downtrend'} (EMA50 {'>' if long_ else '<'} EMA200), "
            f"RSI {'dipped to' if long_ else 'spiked to'} {lb.min() if long_ else lb.max():.0f} "
            f"and recovered to {r.rsi:.0f}"),
            f"MACD histogram turning {'up' if long_ else 'down'}"),
            {"rsi": r.rsi, "adx": r.adx, "atr_pct": r.atr_pct})


class VwapIntraday(Strategy):
    """Intraday (UTC-day VWAP): in a short-term trend, buy a pullback that holds VWAP."""

    name = "vwap_intraday"
    horizon = Horizon.INTRADAY
    regimes = TRENDING | {Regime.NEUTRAL}
    defaults = {"touch_bars": 3, "slope_bars": 12, "stop_atr": 1.5, "rr": 1.5}
    warmup = 300

    def prepare(self, f):
        f = f.copy()
        f["vwap"] = ta.session_vwap(f)
        return f

    def decide(self, f, i):
        r = f.iloc[i]
        k = self.p["touch_bars"]
        slope = r.ema50 - f["ema50"].iat[i - self.p["slope_bars"]]
        recent = f.iloc[i - k:i]
        long_ = (slope > 0 and r.close > r.vwap and r.close > r.ema20
                 and (recent["low"] <= recent["vwap"]).any())
        short = (slope < 0 and r.close < r.vwap and r.close < r.ema20
                 and (recent["high"] >= recent["vwap"]).any())
        if not (long_ or short):
            return None
        side = Side.BUY if long_ else Side.SELL
        stop, target = self._bracket(side, r.close, r.atr)
        conf = 0.5 + 0.1 * (r.adx >= 25) + 0.1 * (r.volume > r.vol_ma)
        return Setup(side, r.close, stop, target, clip01(conf), (
            (f"EMA50 sloping {'up' if long_ else 'down'}, pullback to session VWAP "
            f"{r.vwap:,.1f} held"), f"close {'above' if long_ else 'below'} VWAP and EMA20"),
            {"adx": r.adx, "atr_pct": r.atr_pct})


REGISTRY: dict[str, type[Strategy]] = {
    s.name: s for s in (EmaTrend, DonchianBreakout, RsiPullback, VwapIntraday)}
