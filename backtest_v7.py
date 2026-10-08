from __future__ import annotations

import argparse
import importlib.util
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5
import numpy as np
import pandas as pd


BUY = 1
SELL = -1
NEUTRAL = 0


@dataclass
class Position:
    symbol: str
    direction: int
    entry_time: pd.Timestamp
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    initial_risk: float
    volume: float
    initial_volume: float
    tp1_done: bool = False
    tp2_done: bool = False
    breakeven_done: bool = False
    trailing_done: bool = False
    realized: float = 0.0
    close_time: Optional[pd.Timestamp] = None
    close_price: Optional[float] = None
    exit_reason: str = ""


class Backtester:
    def __init__(self, ea_path: Path, config_path: Path, start: pd.Timestamp, end: pd.Timestamp, initial_balance: float):
        self.ea_path = ea_path
        self.config_path = config_path
        self.start = start
        self.end = end
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.equity_peak = initial_balance
        self.max_drawdown_money = 0.0
        self.positions: dict[str, Position] = {}
        self.trades = []
        self.signals = []
        self.equity_curve = []
        self.daily_pnl = {}
        self.last_trade_time = {}
        self.consecutive_losses = 0
        self._load_ea()

    def _load_ea(self):
        spec = importlib.util.spec_from_file_location("ea_v7_source", self.ea_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Impossible de charger {self.ea_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.mod = mod
        self.config = mod.Config(self.config_path)
        # Logger V7: les calculs du SignalEngine restent exactement ceux de l'EA.
        self.logger = mod.AppLogger(self.config)
        self.signal_engine = mod.SignalEngine(self.config, self.logger)
        self.regime_engine = mod.RegimeEngine(self.config, self.logger)
        self.stop_engine = mod.StopEngine(self.config, self.logger)

    def connect(self):
        mt5cfg = self.config.get("mt5", default={})
        path = mt5cfg.get("path", "") or None
        kwargs = {}
        if mt5cfg.get("login"):
            kwargs["login"] = int(mt5cfg["login"])
        if mt5cfg.get("password"):
            kwargs["password"] = str(mt5cfg["password"])
        if mt5cfg.get("server"):
            kwargs["server"] = str(mt5cfg["server"])
        if not mt5.initialize(path, **kwargs):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    def rates(self, symbol: str, timeframe, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        arr = mt5.copy_rates_range(symbol, timeframe, start.to_pydatetime(), end.to_pydatetime())
        if arr is None or len(arr) == 0:
            raise RuntimeError(f"Aucune donnée MT5 pour {symbol}: {mt5.last_error()}")
        df = pd.DataFrame(arr)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df.sort_values("time").drop_duplicates("time").reset_index(drop=True)

    def prepare_symbol(self, symbol: str):
        # Warm-up suffisamment large pour EMA200, Ichimoku, ATR percentile, etc.
        warm = self.start - pd.Timedelta(days=120)
        end = self.end + pd.Timedelta(days=2)
        return {
            "M1": self.rates(symbol, mt5.TIMEFRAME_M1, warm, end),
            "M15": self.rates(symbol, mt5.TIMEFRAME_M15, warm, end),
            "H1": self.rates(symbol, mt5.TIMEFRAME_H1, warm, end),
            "H4": self.rates(symbol, mt5.TIMEFRAME_H4, warm, end),
        }

    @staticmethod
    def closed_before(df: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
        return df[df["time"] < t]

    @staticmethod
    def price_for_entry(row) -> float:
        return float(row["open"])

    def volume_for_risk(self, symbol: str, entry: float, sl: float) -> float:
        rm = self.config.get("risk_management", default={})
        risk_pct = float(rm.get("risk_percent", 1.0))
        risk_money = self.balance * risk_pct / 100.0
        info = mt5.symbol_info(symbol)
        if info is None:
            return 0.0
        tick_size = float(getattr(info, "trade_tick_size", 0) or 0)
        tick_value = float(getattr(info, "trade_tick_value", 0) or 0)
        if tick_size <= 0 or tick_value <= 0:
            return 0.0
        loss_one_lot = abs(entry - sl) / tick_size * tick_value
        if loss_one_lot <= 0:
            return 0.0
        raw = risk_money / loss_one_lot
        vmin = float(getattr(info, "volume_min", 0) or 0)
        vmax = float(getattr(info, "volume_max", 0) or raw)
        step = float(getattr(info, "volume_step", 0) or 0)
        if step <= 0:
            return 0.0
        raw = min(raw, vmax)
        raw = math.floor(raw / step) * step
        if raw < vmin:
            return 0.0
        decimals = 4 if step < 0.01 else (3 if step < 0.1 else 2)
        return round(raw, decimals)

    def pnl(self, symbol: str, direction: int, volume: float, entry: float, exit_price: float) -> float:
        info = mt5.symbol_info(symbol)
        if info is None:
            return 0.0
        tick_size = float(getattr(info, "trade_tick_size", 0) or 0)
        tick_value = float(getattr(info, "trade_tick_value", 0) or 0)
        if tick_size <= 0 or tick_value <= 0:
            return 0.0
        points = (exit_price - entry) if direction == BUY else (entry - exit_price)
        return points / tick_size * tick_value * volume

    def record_equity(self, t: pd.Timestamp):
        floating = 0.0
        for p in self.positions.values():
            floating += self.pnl(p.symbol, p.direction, p.volume, p.entry, p.entry)
        equity = self.balance + floating
        self.equity_peak = max(self.equity_peak, equity)
        dd = self.equity_peak - equity
        self.max_drawdown_money = max(self.max_drawdown_money, dd)
        self.equity_curve.append({"time": t, "balance": self.balance, "equity": equity, "drawdown": dd})

    def daily_loss_blocked(self, t: pd.Timestamp) -> bool:
        max_loss = float(self.config.get("risk_management", "max_daily_loss_percent", default=2.0))
        pnl = self.daily_pnl.get(t.date(), 0.0)
        return pnl < 0 and abs(pnl) / max(self.initial_balance, 1e-9) * 100 >= max_loss

    def trade_allowed(self, symbol: str, t: pd.Timestamp) -> bool:
        rm = self.config.get("risk_management", default={})
        if len(self.positions) >= int(rm.get("max_positions", 3)):
            return False
        if symbol in self.positions:
            return False
        if self.daily_loss_blocked(t):
            return False
        last = self.last_trade_time.get(symbol)
        if last is not None:
            cooldown = int(rm.get("cooldown_after_trade_minutes", 60))
            if t < last + pd.Timedelta(minutes=cooldown):
                return False
        return True

    def process_position_bar(self, p: Position, row, t: pd.Timestamp):
        high, low = float(row.high), float(row.low)
        direction = p.direction

        # Conservative intrabar assumption: if SL and TP are both touched, SL wins.
        if direction == BUY and low <= p.sl:
            self.close_position(p, p.sl, t, "SL")
            return
        if direction == SELL and high >= p.sl:
            self.close_position(p, p.sl, t, "SL")
            return

        # Partial TP1 / TP2 / final TP3.
        tp_cfg = self.config.get("take_profit", default={})
        c1 = float(tp_cfg.get("tp1_close_percent", 40.0)) / 100.0
        c2 = float(tp_cfg.get("tp2_close_percent", 30.0)) / 100.0

        if not p.tp1_done and ((direction == BUY and high >= p.tp1) or (direction == SELL and low <= p.tp1)):
            close_vol = p.initial_volume * c1
            close_vol = min(close_vol, p.volume)
            if close_vol > 0:
                self.partial_close(p, close_vol, p.tp1, t, "TP1")
            p.tp1_done = True

            be = self.config.get("position_management", "breakeven", default={})
            if bool(be.get("enabled", True)) and float(be.get("activation_r", 1.0)) <= 1.0:
                off = float(be.get("offset_r", 0.10)) * p.initial_risk
                p.sl = p.entry + off if direction == BUY else p.entry - off
                p.breakeven_done = True

        if p.volume > 0 and not p.tp2_done and ((direction == BUY and high >= p.tp2) or (direction == SELL and low <= p.tp2)):
            close_vol = p.initial_volume * c2
            close_vol = min(close_vol, p.volume)
            if close_vol > 0:
                self.partial_close(p, close_vol, p.tp2, t, "TP2")
            p.tp2_done = True

        if p.volume > 0 and ((direction == BUY and high >= p.tp3) or (direction == SELL and low <= p.tp3)):
            self.close_position(p, p.tp3, t, "TP3")
            return

        # BE after 1R.
        be = self.config.get("position_management", "breakeven", default={})
        if bool(be.get("enabled", True)) and not p.breakeven_done:
            activation = float(be.get("activation_r", 1.0))
            reached = (high - p.entry) / p.initial_risk >= activation if direction == BUY else (p.entry - low) / p.initial_risk >= activation
            if reached:
                off = float(be.get("offset_r", 0.10)) * p.initial_risk
                p.sl = p.entry + off if direction == BUY else p.entry - off
                p.breakeven_done = True

        # ATR trailing: approximated using H1 ATR supplied separately by caller.

    def partial_close(self, p: Position, volume: float, price: float, t: pd.Timestamp, reason: str):
        profit = self.pnl(p.symbol, p.direction, volume, p.entry, price)
        p.realized += profit
        p.volume -= volume
        self.balance += profit
        self.daily_pnl[t.date()] = self.daily_pnl.get(t.date(), 0.0) + profit
        if p.volume <= 1e-12:
            p.volume = 0.0

    def close_position(self, p: Position, price: float, t: pd.Timestamp, reason: str):
        if p.volume > 0:
            profit = self.pnl(p.symbol, p.direction, p.volume, p.entry, price)
            p.realized += profit
            self.balance += profit
            self.daily_pnl[t.date()] = self.daily_pnl.get(t.date(), 0.0) + profit
        p.close_time = t
        p.close_price = price
        p.exit_reason = reason
        self.trades.append({
            "symbol": p.symbol,
            "direction": "BUY" if p.direction == BUY else "SELL",
            "entry_time": p.entry_time,
            "entry": p.entry,
            "sl_initial": p.entry - p.initial_risk if p.direction == BUY else p.entry + p.initial_risk,
            "tp1": p.tp1,
            "tp2": p.tp2,
            "tp3": p.tp3,
            "exit_time": t,
            "exit": price,
            "reason": reason,
            "volume_initial": p.initial_volume,
            "pnl": p.realized,
            "R": p.realized / max(self.pnl(p.symbol, p.direction, p.initial_volume, p.entry, p.entry + (p.initial_risk if p.direction == BUY else -p.initial_risk)), 1e-9),
        })
        if p.realized < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        self.last_trade_time[p.symbol] = t
        self.positions.pop(p.symbol, None)

    def run_symbol(self, symbol: str, data: dict[str, pd.DataFrame]):
        h1 = data["H1"]
        h4 = data["H4"]
        m15 = data["M15"]
        m1 = data["M1"]

        signal_times = h1[(h1.time >= self.start) & (h1.time < self.end)]["time"].tolist()
        for t in signal_times:
            t = pd.Timestamp(t)
            current_h1 = h1[h1.time == t]
            if current_h1.empty:
                continue

            # The EA analyses closed candles only. At the opening of H1 bar t,
            # the signal uses all bars strictly before t.
            h1_closed = self.closed_before(h1, t).tail(5000).copy()
            h4_closed = self.closed_before(h4, t).tail(1500).copy()
            m15_closed = self.closed_before(m15, t).tail(5000).copy()
            if len(h1_closed) < 250 or len(h4_closed) < 120 or len(m15_closed) < 150:
                continue

            try:
                signal = self.signal_engine.build_signal(h4_closed, h1_closed, m15_closed, symbol=symbol)
            except Exception as exc:
                self.signals.append({"symbol": symbol, "time": t, "error": str(exc)})
                continue
            if signal is None:
                continue

            self.signals.append({
                "symbol": symbol,
                "time": t,
                "direction": {BUY: "BUY", SELL: "SELL", NEUTRAL: "NEUTRAL"}.get(signal.direction, "NEUTRAL"),
                "h4": signal.h4_direction,
                "h1": signal.h1_direction,
                "m15": signal.m15_direction,
                "score": getattr(signal, "score", np.nan),
                "quality": signal.quality,
                "regime": signal.regime,
                "adx": signal.adx,
                "atr_percentile": signal.atr_percentile,
                "reason": signal.reason,
            })

            if signal.direction == NEUTRAL or not self.trade_allowed(symbol, t):
                continue

            entry_row = current_h1.iloc[0]
            entry = self.price_for_entry(entry_row)
            stops = self.stop_engine.calculate(symbol, signal.direction, entry, h1_closed)
            if stops is None:
                continue
            sl, tp1, tp2, tp3 = stops
            volume = self.volume_for_risk(symbol, entry, sl)
            if volume <= 0:
                continue

            p = Position(symbol, signal.direction, t, entry, sl, tp1, tp2, tp3, abs(entry-sl), volume, volume)
            self.positions[symbol] = p

            # Replay M1 bars from entry until the position closes or end is reached.
            future_m1 = m1[(m1.time >= t) & (m1.time < self.end)]
            for _, row in future_m1.iterrows():
                if symbol not in self.positions:
                    break
                self.process_position_bar(p, row, pd.Timestamp(row.time))
                self.record_equity(pd.Timestamp(row.time))

                # Keep one position per symbol, exactly like the V7 configuration.
                if symbol not in self.positions:
                    break

    def summary(self):
        tr = pd.DataFrame(self.trades)
        if tr.empty:
            return {"trades": 0, "final_balance": self.balance, "net_pnl": self.balance-self.initial_balance}
        wins = tr[tr.pnl > 0]
        losses = tr[tr.pnl < 0]
        gross_profit = float(wins.pnl.sum())
        gross_loss = float(abs(losses.pnl.sum()))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        return {
            "initial_balance": self.initial_balance,
            "final_balance": self.balance,
            "net_pnl": self.balance - self.initial_balance,
            "return_pct": (self.balance/self.initial_balance-1)*100,
            "trades": len(tr),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": len(wins)/len(tr)*100,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": pf,
            "max_drawdown_money": self.max_drawdown_money,
            "max_drawdown_pct": self.max_drawdown_money/max(self.initial_balance,1e-9)*100,
            "average_pnl": float(tr.pnl.mean()),
            "average_R": float(tr.R.mean()),
            "best_R": float(tr.R.max()),
            "worst_R": float(tr.R.min()),
        }

    def run(self, symbols):
        self.connect()
        try:
            for symbol in symbols:
                print(f"\n=== {symbol} ===")
                data = self.prepare_symbol(symbol)
                self.run_symbol(symbol, data)
        finally:
            mt5.shutdown()

        out = Path.cwd()
        pd.DataFrame(self.trades).to_csv(out / "backtest_v7_trades.csv", index=False)
        pd.DataFrame(self.signals).to_csv(out / "backtest_v7_signals.csv", index=False)
        pd.DataFrame(self.equity_curve).to_csv(out / "backtest_v7_equity.csv", index=False)
        summary = self.summary()
        (out / "backtest_v7_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

        print("\n" + "="*64)
        print("V7 BACKTEST — SUMMARY")
        print("="*64)
        for k,v in summary.items():
            if isinstance(v,float): print(f"{k:24s}: {v:.4f}")
            else: print(f"{k:24s}: {v}")
        print("\nFichiers créés dans:", out)


def parse_dt(s: str) -> pd.Timestamp:
    x = pd.Timestamp(s)
    if x.tzinfo is None:
        x = x.tz_localize("UTC")
    return x.tz_convert("UTC")


def main():
    ap = argparse.ArgumentParser(description="Backtest V7 basé directement sur ea_trading_v7.py")
    ap.add_argument("--ea", default="ea_trading_v7.py")
    ap.add_argument("--config", default="config_v7.json")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--balance", type=float, default=10000.0)
    ap.add_argument("--symbols", nargs="+", default=["BTCUSD", "XAUUSD"])
    args = ap.parse_args()

    bt = Backtester(Path(args.ea).resolve(), Path(args.config).resolve(), parse_dt(args.start), parse_dt(args.end), args.balance)
    bt.run(args.symbols)


if __name__ == "__main__":
    main()
