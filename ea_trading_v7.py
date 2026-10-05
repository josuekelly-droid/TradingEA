from __future__ import annotations

import csv
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import MetaTrader5 as mt5


# ============================================================
# GLOBALS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config_v7.json"

BUY = 1
SELL = -1
NEUTRAL = 0


# ============================================================
# UTILS
# ============================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_timestamp() -> int:
    return int(now_utc().timestamp())


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        value = float(value)
        if not math.isfinite(value):
            return default
        return value
    except Exception:
        return default


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def round_price(price: float, digits: int) -> float:
    return round(float(price), int(digits))


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def normalize_volume(
    volume: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
) -> float:
    if volume <= 0:
        return 0.0

    if volume < volume_min:
        return 0.0

    volume = min(volume, volume_max)
    volume = floor_to_step(volume, volume_step)

    if volume < volume_min:
        return 0.0

    decimals = 2
    if volume_step < 0.01:
        decimals = 4
    elif volume_step < 0.1:
        decimals = 3

    return round(volume, decimals)


def direction_name(direction: int) -> str:
    if direction == BUY:
        return "BUY"
    if direction == SELL:
        return "SELL"
    return "NEUTRAL"


def safe_json_load(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default

        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def safe_json_save(path: Path, data: Any) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        temp_path = path.with_suffix(path.suffix + ".tmp")

        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        temp_path.replace(path)
        return True

    except Exception:
        return False


def parse_hhmm(value: str) -> Tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def time_in_window(current_minutes: int, start: str, end: str) -> bool:
    sh, sm = parse_hhmm(start)
    eh, em = parse_hhmm(end)

    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes < end_minutes

    return current_minutes >= start_minutes or current_minutes < end_minutes


# ============================================================
# CONFIG
# ============================================================

class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data = safe_json_load(path, None)

        if not isinstance(self.data, dict):
            raise RuntimeError(f"Configuration invalide: {path}")

        self.validate()

    def validate(self) -> None:
        required_sections = [
            "ea",
            "mt5",
            "symbols",
            "timeframes",
            "market_regime",
            "indicators",
            "signal_engine",
            "risk_management",
            "stop_loss",
            "take_profit",
            "position_management",
            "sessions",
            "news_filter",
            "execution",
            "filters",
            "logging",
            "state",
            "telegram",
        ]

        missing = [
            section
            for section in required_sections
            if section not in self.data
        ]

        if missing:
            raise RuntimeError(
                f"Sections manquantes dans config_v7.json: {missing}"
            )

    def get(self, *keys, default=None):
        value = self.data

        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            return default


# ============================================================
# LOGGING
# ============================================================

class AppLogger:
    def __init__(self, config: Config):
        self.config = config

        log_cfg = config.get("logging", default={})
        enabled = bool(log_cfg.get("enabled", True))

        log_level_name = config.get(
            "ea",
            "log_level",
            default="INFO",
        )

        level = getattr(
            logging,
            str(log_level_name).upper(),
            logging.INFO,
        )

        self.logger = logging.getLogger("BTC_XAU_V7")
        self.logger.setLevel(level)

        if self.logger.handlers:
            return

        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        self.logger.addHandler(console)

        if enabled:
            error_file = BASE_DIR / log_cfg.get(
                "error_log",
                "logs/errors_v7.log",
            )

            error_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            file_handler = logging.FileHandler(
                error_file,
                encoding="utf-8",
            )

            file_handler.setLevel(logging.ERROR)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

    def info(self, message: str):
        self.logger.info(message)

    def warning(self, message: str):
        self.logger.warning(message)

    def error(self, message: str):
        self.logger.error(message)

    def exception(self, message: str):
        self.logger.exception(message)


# ============================================================
# CSV LOGGER
# ============================================================

class CSVLogger:
    def __init__(self, config: Config, logger: AppLogger):
        self.config = config
        self.logger = logger

        log_cfg = config.get("logging", default={})

        self.trade_file = BASE_DIR / log_cfg.get(
            "trade_log",
            "logs/trades_v7.csv",
        )

        self.signal_file = BASE_DIR / log_cfg.get(
            "signal_log",
            "logs/signals_v7.csv",
        )

        self.trade_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    def _append(
        self,
        path: Path,
        row: Dict[str, Any],
    ):
        try:
            path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            exists = path.exists()

            with path.open(
                "a",
                newline="",
                encoding="utf-8",
            ) as f:

                writer = csv.DictWriter(
                    f,
                    fieldnames=list(row.keys()),
                )

                if not exists:
                    writer.writeheader()

                writer.writerow(row)

        except Exception as exc:
            self.logger.error(
                f"CSV logging error {path}: {exc}"
            )

    def signal(self, data: Dict[str, Any]):
        self._append(
            self.signal_file,
            data,
        )

    def trade(self, data: Dict[str, Any]):
        self._append(
            self.trade_file,
            data,
        )


# ============================================================
# TELEGRAM
# ============================================================

class TelegramNotifier:
    def __init__(
        self,
        config: Config,
        logger: AppLogger,
    ):
        self.config = config
        self.logger = logger

        cfg = config.get("telegram", default={})

        self.enabled = bool(cfg.get("enabled", False))
        self.bot_token = str(
            cfg.get("bot_token", "")
        )
        self.chat_id = str(
            cfg.get("chat_id", "")
        )

        self.notify_entries = bool(
            cfg.get("notify_entries", True)
        )

        self.notify_exits = bool(
            cfg.get("notify_exits", True)
        )

        self.notify_errors = bool(
            cfg.get("notify_errors", True)
        )

        self.notify_daily_summary = bool(
            cfg.get("notify_daily_summary", True)
        )

    def send(
        self,
        message: str,
        category: str = "general",
    ) -> bool:

        if not self.enabled:
            return False

        if not self.bot_token or not self.chat_id:
            return False

        if category == "entry" and not self.notify_entries:
            return False

        if category == "exit" and not self.notify_exits:
            return False

        if category == "error" and not self.notify_errors:
            return False

        if category == "daily" and not self.notify_daily_summary:
            return False

        url = (
            f"https://api.telegram.org/bot"
            f"{self.bot_token}/sendMessage"
        )

        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=10,
            )

            if response.status_code != 200:
                self.logger.warning(
                    f"Telegram error: "
                    f"{response.status_code} "
                    f"{response.text[:300]}"
                )
                return False

            return True

        except Exception as exc:
            self.logger.warning(
                f"Telegram exception: {exc}"
            )
            return False


# ============================================================
# MT5 CONNECTION
# ============================================================

class MT5Connection:
    def __init__(
        self,
        config: Config,
        logger: AppLogger,
    ):
        self.config = config
        self.logger = logger

    def connect(self) -> bool:
        cfg = self.config.get("mt5", default={})

        login = int(cfg.get("login", 0))
        password = str(cfg.get("password", ""))
        server = str(cfg.get("server", ""))

        path = str(cfg.get("path", "") or "")
        timeout = int(
            cfg.get("timeout", 60000)
        )

        try:
            if path:
                initialized = mt5.initialize(
                    path=path,
                    login=login,
                    password=password,
                    server=server,
                    timeout=timeout,
                )
            else:
                initialized = mt5.initialize(
                    login=login,
                    password=password,
                    server=server,
                    timeout=timeout,
                )

            if not initialized:
                self.logger.error(
                    f"MT5 initialize failed: {mt5.last_error()}"
                )
                return False

            account = mt5.account_info()

            if account is None:
                self.logger.error(
                    f"MT5 account_info failed: {mt5.last_error()}"
                )
                return False

            self.logger.info(
                f"MT5 connecté | "
                f"login={account.login} | "
                f"server={account.server} | "
                f"balance={account.balance:.2f} | "
                f"equity={account.equity:.2f}"
            )

            return True

        except Exception as exc:
            self.logger.exception(
                f"Erreur connexion MT5: {exc}"
            )
            return False

    def shutdown(self):
        try:
            mt5.shutdown()
        except Exception:
            pass

    def ensure_connection(self) -> bool:
        terminal = mt5.terminal_info()

        if terminal is None:
            self.logger.warning(
                "Connexion MT5 perdue. Reconnexion..."
            )

            mt5.shutdown()
            time.sleep(1)

            return self.connect()

        return True


# ============================================================
# MARKET DATA
# ============================================================

class MarketData:
    TF_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }

    def __init__(
        self,
        logger: AppLogger,
    ):
        self.logger = logger

    def fetch(
        self,
        symbol: str,
        timeframe_name: str,
        bars: int = 300,
    ) -> Optional[pd.DataFrame]:

        timeframe = self.TF_MAP.get(timeframe_name)

        if timeframe is None:
            self.logger.error(
                f"Timeframe inconnu: {timeframe_name}"
            )
            return None

        try:
            rates = mt5.copy_rates_from_pos(
                symbol,
                timeframe,
                0,
                bars + 3,
            )

            if rates is None or len(rates) < 50:
                self.logger.warning(
                    f"Données insuffisantes "
                    f"{symbol} {timeframe_name}"
                )
                return None

            df = pd.DataFrame(rates)

            df["time"] = pd.to_datetime(
                df["time"],
                unit="s",
                utc=True,
            )

            # La dernière bougie est potentiellement en formation.
            df = df.iloc[:-1].copy()

            df.reset_index(drop=True, inplace=True)

            return df

        except Exception as exc:
            self.logger.error(
                f"Erreur data {symbol} "
                f"{timeframe_name}: {exc}"
            )
            return None


# ============================================================
# INDICATORS
# ============================================================

class Indicators:

    @staticmethod
    def true_range(df: pd.DataFrame) -> pd.Series:
        prev_close = df["close"].shift(1)

        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - prev_close).abs()
        tr3 = (df["low"] - prev_close).abs()

        return pd.concat(
            [tr1, tr2, tr3],
            axis=1,
        ).max(axis=1)

    @staticmethod
    def atr(
        df: pd.DataFrame,
        period: int = 14,
    ) -> pd.Series:

        tr = Indicators.true_range(df)

        # Wilder/RMA
        return tr.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

    @staticmethod
    def ema(
        series: pd.Series,
        period: int,
    ) -> pd.Series:

        return series.ewm(
            span=period,
            adjust=False,
            min_periods=period,
        ).mean()

    @staticmethod
    def rsi(
        series: pd.Series,
        period: int = 14,
    ) -> pd.Series:

        delta = series.diff()

        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

        avg_loss = loss.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

        rs = avg_gain / avg_loss.replace(
            0,
            np.nan,
        )

        rsi = 100 - (
            100 / (1 + rs)
        )

        return rsi.fillna(50)

    @staticmethod
    def macd(
        series: pd.Series,
        fast: int,
        slow: int,
        signal: int,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:

        ema_fast = Indicators.ema(
            series,
            fast,
        )

        ema_slow = Indicators.ema(
            series,
            slow,
        )

        line = ema_fast - ema_slow

        signal_line = Indicators.ema(
            line,
            signal,
        )

        histogram = line - signal_line

        return (
            line,
            signal_line,
            histogram,
        )

    @staticmethod
    def bollinger(
        series: pd.Series,
        period: int,
        std_mult: float,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:

        middle = series.rolling(
            period
        ).mean()

        std = series.rolling(
            period
        ).std()

        upper = middle + (
            std * std_mult
        )

        lower = middle - (
            std * std_mult
        )

        return middle, upper, lower

    @staticmethod
    def adx(
        df: pd.DataFrame,
        period: int = 14,
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:

        high = df["high"]
        low = df["low"]

        up_move = high.diff()
        down_move = -low.diff()

        plus_dm = pd.Series(
            np.where(
                (up_move > down_move)
                & (up_move > 0),
                up_move,
                0,
            ),
            index=df.index,
        )

        minus_dm = pd.Series(
            np.where(
                (down_move > up_move)
                & (down_move > 0),
                down_move,
                0,
            ),
            index=df.index,
        )

        tr = Indicators.true_range(df)

        atr = tr.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

        plus_dm_smoothed = plus_dm.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

        minus_dm_smoothed = minus_dm.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

        plus_di = (
            100
            * plus_dm_smoothed
            / atr.replace(0, np.nan)
        )

        minus_di = (
            100
            * minus_dm_smoothed
            / atr.replace(0, np.nan)
        )

        denominator = (
            plus_di + minus_di
        ).replace(0, np.nan)

        dx = (
            100
            * (plus_di - minus_di).abs()
            / denominator
        )

        adx = dx.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period,
        ).mean()

        return (
            adx.fillna(0),
            plus_di.fillna(0),
            minus_di.fillna(0),
        )

    @staticmethod
    def supertrend(
        df: pd.DataFrame,
        period: int,
        multiplier: float,
    ) -> Tuple[pd.Series, pd.Series]:

        atr = Indicators.atr(
            df,
            period,
        )

        hl2 = (
            df["high"] + df["low"]
        ) / 2

        upper = hl2 + multiplier * atr
        lower = hl2 - multiplier * atr

        final_upper = upper.copy()
        final_lower = lower.copy()

        trend = pd.Series(
            1,
            index=df.index,
            dtype=int,
        )

        for i in range(1, len(df)):
            prev_close = df["close"].iloc[i - 1]

            if (
                upper.iloc[i] < final_upper.iloc[i - 1]
                or prev_close > final_upper.iloc[i - 1]
            ):
                final_upper.iloc[i] = upper.iloc[i]
            else:
                final_upper.iloc[i] = final_upper.iloc[i - 1]

            if (
                lower.iloc[i] > final_lower.iloc[i - 1]
                or prev_close < final_lower.iloc[i - 1]
            ):
                final_lower.iloc[i] = lower.iloc[i]
            else:
                final_lower.iloc[i] = final_lower.iloc[i - 1]

            prev_trend = trend.iloc[i - 1]

            if prev_trend == -1:
                if df["close"].iloc[i] > final_upper.iloc[i]:
                    trend.iloc[i] = 1
                else:
                    trend.iloc[i] = -1
            else:
                if df["close"].iloc[i] < final_lower.iloc[i]:
                    trend.iloc[i] = -1
                else:
                    trend.iloc[i] = 1

        return trend, atr

    @staticmethod
    def ichimoku(
        df: pd.DataFrame,
        tenkan: int,
        kijun: int,
        senkou_b: int,
    ) -> Dict[str, pd.Series]:

        high = df["high"]
        low = df["low"]

        conversion = (
            high.rolling(tenkan).max()
            + low.rolling(tenkan).min()
        ) / 2

        base = (
            high.rolling(kijun).max()
            + low.rolling(kijun).min()
        ) / 2

        span_a = (
            (conversion + base) / 2
        ).shift(kijun)

        span_b = (
            (
                high.rolling(senkou_b).max()
                + low.rolling(senkou_b).min()
            ) / 2
        ).shift(kijun)

        return {
            "tenkan": conversion,
            "kijun": base,
            "span_a": span_a,
            "span_b": span_b,
        }


# ============================================================
# MARKET REGIME
# ============================================================

@dataclass
class Regime:
    name: str
    direction: int
    adx: float
    atr: float
    atr_percentile: float
    ema_fast: float
    ema_slow: float
    price: float


class RegimeEngine:
    def __init__(
        self,
        config: Config,
        logger: AppLogger,
    ):
        self.config = config
        self.logger = logger

    def analyze(
        self,
        df: pd.DataFrame,
    ) -> Optional[Regime]:

        cfg = self.config.get(
            "market_regime",
            default={},
        )

        if df is None or len(df) < 100:
            return None

        adx_period = int(
            cfg.get("adx_period", 14)
        )

        atr_period = int(
            cfg.get("atr_period", 14)
        )

        percentile_period = int(
            cfg.get(
                "atr_percentile_period",
                100,
            )
        )

        ema_fast_period = int(
            cfg.get("ema_fast", 20)
        )

        ema_slow_period = int(
            cfg.get("ema_slow", 50)
        )

        trend_ema_period = int(
            self.config.get(
                "indicators",
                "ema",
                "trend",
                default=200,
            )
        )

        adx, plus_di, minus_di = Indicators.adx(
            df,
            adx_period,
        )

        atr = Indicators.atr(
            df,
            atr_period,
        )

        ema_fast = Indicators.ema(
            df["close"],
            ema_fast_period,
        )

        ema_slow = Indicators.ema(
            df["close"],
            ema_slow_period,
        )

        trend_ema = Indicators.ema(
            df["close"],
            trend_ema_period,
        )

        last_price = safe_float(
            df["close"].iloc[-1]
        )

        last_adx = safe_float(
            adx.iloc[-1]
        )

        last_atr = safe_float(
            atr.iloc[-1]
        )

        last_fast = safe_float(
            ema_fast.iloc[-1]
        )

        last_slow = safe_float(
            ema_slow.iloc[-1]
        )

        last_trend_ema = safe_float(
            trend_ema.iloc[-1]
        )

        atr_window = atr.dropna().tail(
            percentile_period
        )

        if len(atr_window) >= 10:
            atr_percentile = float(
                (
                    atr_window <= last_atr
                ).mean()
                * 100
            )
        else:
            atr_percentile = 50.0

        if (
            last_price > last_fast
            and last_fast > last_slow
            and last_price > last_trend_ema
        ):
            direction = BUY

        elif (
            last_price < last_fast
            and last_fast < last_slow
            and last_price < last_trend_ema
        ):
            direction = SELL

        elif (
            last_fast > last_slow
        ):
            direction = BUY

        elif (
            last_fast < last_slow
        ):
            direction = SELL

        else:
            direction = NEUTRAL

        adx_trend = float(
            cfg.get(
                "adx_trend_threshold",
                22.0,
            )
        )

        adx_strong = float(
            cfg.get(
                "adx_strong_threshold",
                30.0,
            )
        )

        low_vol = float(
            cfg.get(
                "low_volatility_percentile",
                20.0,
            )
        )

        high_vol = float(
            cfg.get(
                "high_volatility_percentile",
                80.0,
            )
        )

        if (
            atr_percentile <= low_vol
        ):
            regime_name = "LOW_VOLATILITY"

        elif (
            atr_percentile >= high_vol
            and last_adx >= adx_trend
        ):
            regime_name = "HIGH_VOLATILITY_TREND"

        elif (
            last_adx >= adx_strong
            and direction == BUY
        ):
            regime_name = "STRONG_TREND_UP"

        elif (
            last_adx >= adx_strong
            and direction == SELL
        ):
            regime_name = "STRONG_TREND_DOWN"

        elif (
            last_adx >= adx_trend
            and direction == BUY
        ):
            regime_name = "TREND_UP"

        elif (
            last_adx >= adx_trend
            and direction == SELL
        ):
            regime_name = "TREND_DOWN"

        else:
            regime_name = "RANGE"

        return Regime(
            name=regime_name,
            direction=direction,
            adx=last_adx,
            atr=last_atr,
            atr_percentile=atr_percentile,
            ema_fast=last_fast,
            ema_slow=last_slow,
            price=last_price,
        )


# ============================================================
# SESSION ENGINE
# ============================================================

class SessionEngine:
    def __init__(
        self,
        config: Config,
    ):
        self.config = config

    def current_session(
        self,
        dt: Optional[datetime] = None,
    ) -> Tuple[str, bool]:

        if dt is None:
            dt = now_utc()

        # Configuration actuellement définie en UTC.
        dt_utc = dt.astimezone(timezone.utc)

        minutes = (
            dt_utc.hour * 60
            + dt_utc.minute
        )

        sessions = self.config.get(
            "sessions",
            default={},
        )

        priority = [
            "overlap",
            "london",
            "new_york",
            "asian",
        ]

        for name in priority:
            cfg = sessions.get(name, {})

            if not cfg.get("enabled", False):
                continue

            if time_in_window(
                minutes,
                cfg.get("start", "00:00"),
                cfg.get("end", "00:00"),
            ):
                return name.upper(), True

        outside = sessions.get(
            "outside_sessions",
            {},
        )

        return (
            "OUTSIDE",
            bool(
                outside.get(
                    "allow_trading",
                    False,
                )
            ),
        )


# ============================================================
# NEWS FILTER
# ============================================================

class NewsFilter:
    def __init__(
        self,
        config: Config,
        logger: AppLogger,
    ):
        self.config = config
        self.logger = logger

        cfg = config.get(
            "news_filter",
            default={},
        )

        self.enabled = bool(
            cfg.get("enabled", False)
        )

        self.lock_file_name = str(
            cfg.get(
                "lock_file",
                "news_lock.json",
            )
        )

        self.minutes_after = int(
            cfg.get(
                "minutes_after",
                30,
            )
        )

        self.post_news_cooldown = int(
            cfg.get(
                "post_news_cooldown_minutes",
                120,
            )
        )

        self.last_blackout_end = None

    def candidate_paths(self) -> List[Path]:
        paths = [
            BASE_DIR / self.lock_file_name,
            BASE_DIR / "logs" / self.lock_file_name,
        ]

        terminal = mt5.terminal_info()

        if terminal is not None:
            terminal_path = Path(
                terminal.data_path
            )

            paths.append(
                terminal_path
                / "MQL5"
                / "Files"
                / self.lock_file_name
            )

        return paths

    def _read_lock(self) -> Optional[Dict[str, Any]]:
        for path in self.candidate_paths():
            if not path.exists():
                continue

            data = safe_json_load(
                path,
                None,
            )

            if isinstance(data, dict):
                return data

        return None

    def is_blocked(
        self,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, str]:

        if not self.enabled:
            return False, ""

        if now is None:
            now = now_utc()

        lock = self._read_lock()

        if not lock:
            if (
                self.last_blackout_end
                and now < self.last_blackout_end
            ):
                return (
                    True,
                    "POST_NEWS_COOLDOWN",
                )

            return False, ""

        blackout = bool(
            lock.get("blackout", False)
        )

        if not blackout:
            return False, ""

        end_value = lock.get(
            "blackout_end"
        )

        explicit_end = None

        if end_value:
            try:
                if isinstance(
                    end_value,
                    (int, float),
                ):
                    explicit_end = datetime.fromtimestamp(
                        float(end_value),
                        tz=timezone.utc,
                    )
                else:
                    parsed = pd.to_datetime(
                        end_value,
                        utc=True,
                    )

                    explicit_end = parsed.to_pydatetime()

            except Exception:
                explicit_end = None

        calculated_end = (
            now
            + timedelta(
                minutes=self.minutes_after
            )
        )

        blackout_end = max(
            explicit_end or calculated_end,
            calculated_end,
        )

        self.last_blackout_end = (
            blackout_end
            + timedelta(
                minutes=self.post_news_cooldown
            )
        )

        if now < self.last_blackout_end:
            return (
                True,
                "NEWS_LOCK",
            )

        return False, ""


# ============================================================
# SIGNAL ENGINE
# ============================================================

@dataclass
class SignalResult:
    direction: int
    h4_direction: int
    h1_direction: int
    m15_direction: int

    h1_score: float
    h1_max_score: float

    m15_score: float
    m15_max_score: float

    regime: str
    adx: float
    atr: float
    atr_percentile: float

    rsi: float
    macd_histogram: float

    supertrend_direction: int
    ichimoku_direction: int

    quality: float
    reason: str


class SignalEngine:
    """
    V7 directional signal engine.

    IMPORTANT DESIGN RULE:
    - signal_score is the configured directional score on the 0..maximum_score scale.
    - quality is a separate setup-quality metric in the 0..1 range.
    - quality is NEVER converted into signal_score.

    With the supplied config, the maximum directional score is exactly 10.0:
        H4 alignment   3.0
        H1 trend       2.0
        Supertrend     1.5
        Ichimoku       1.0
        RSI             0.5
        MACD            0.5
        M15 confirm    1.5
        -------------------
        TOTAL          10.0
    """

    def __init__(
        self,
        config: Config,
        logger: AppLogger,
    ):
        self.config = config
        self.logger = logger

    def _weights(self) -> Dict[str, float]:
        weights = self.config.get(
            "signal_engine",
            "weights",
            default={},
        )

        return {
            "h4_trend": float(weights.get("h4_trend", 3.0)),
            "h1_trend": float(weights.get("h1_trend", 2.0)),
            "m15_confirmation": float(weights.get("m15_confirmation", 1.5)),
            "supertrend": float(weights.get("supertrend", 1.5)),
            "ichimoku": float(weights.get("ichimoku", 1.0)),
            "rsi": float(weights.get("rsi", 0.5)),
            "macd": float(weights.get("macd", 0.5)),
        }

    def _supertrend_direction(
        self,
        df: pd.DataFrame,
    ) -> int:
        cfg = self.config.get(
            "indicators",
            "supertrend",
            default={},
        )

        trend, _ = Indicators.supertrend(
            df,
            int(cfg.get("period", 10)),
            float(cfg.get("multiplier", 3.0)),
        )

        if trend.empty:
            return NEUTRAL

        value = safe_float(trend.iloc[-1], np.nan)

        if not math.isfinite(value) or value == 0:
            return NEUTRAL

        return BUY if value > 0 else SELL

    def _ichimoku_direction(
        self,
        df: pd.DataFrame,
    ) -> int:
        cfg = self.config.get(
            "indicators",
            "ichimoku",
            default={},
        )

        ichi = Indicators.ichimoku(
            df,
            int(cfg.get("tenkan", 9)),
            int(cfg.get("kijun", 26)),
            int(cfg.get("senkou_b", 52)),
        )

        close = safe_float(df["close"].iloc[-1], np.nan)
        tenkan = safe_float(ichi["tenkan"].iloc[-1], np.nan)
        kijun = safe_float(ichi["kijun"].iloc[-1], np.nan)
        span_a = safe_float(ichi["span_a"].iloc[-1], np.nan)
        span_b = safe_float(ichi["span_b"].iloc[-1], np.nan)

        if any(
            not math.isfinite(x)
            for x in [close, tenkan, kijun, span_a, span_b]
        ):
            return NEUTRAL

        cloud_top = max(span_a, span_b)
        cloud_bottom = min(span_a, span_b)

        if close > cloud_top and tenkan > kijun:
            return BUY

        if close < cloud_bottom and tenkan < kijun:
            return SELL

        return NEUTRAL

    def _ema_direction(
        self,
        df: pd.DataFrame,
    ) -> int:
        cfg = self.config.get(
            "indicators",
            "ema",
            default={},
        )

        fast = Indicators.ema(
            df["close"],
            int(cfg.get("fast", 20)),
        )
        slow = Indicators.ema(
            df["close"],
            int(cfg.get("slow", 50)),
        )
        trend = Indicators.ema(
            df["close"],
            int(cfg.get("trend", 200)),
        )

        close = safe_float(df["close"].iloc[-1], np.nan)
        f = safe_float(fast.iloc[-1], np.nan)
        s = safe_float(slow.iloc[-1], np.nan)
        t = safe_float(trend.iloc[-1], np.nan)

        if any(not math.isfinite(x) for x in [close, f, s, t]):
            return NEUTRAL

        if close > f and f > s and close > t:
            return BUY

        if close < f and f < s and close < t:
            return SELL

        return NEUTRAL

    def h4_direction(
        self,
        df: pd.DataFrame,
    ) -> int:
        """H4 structural direction: majority vote of ST / Ichimoku / EMA."""
        votes = [
            self._supertrend_direction(df),
            self._ichimoku_direction(df),
            self._ema_direction(df),
        ]

        buy_votes = votes.count(BUY)
        sell_votes = votes.count(SELL)

        if buy_votes >= 2:
            return BUY

        if sell_votes >= 2:
            return SELL

        return NEUTRAL

    def h1_analysis(
        self,
        df: pd.DataFrame,
    ) -> Tuple[int, float, float, Dict[str, Any]]:
        """
        Calculate the H1 directional components only.

        H1 max = h1_trend + supertrend + ichimoku + rsi + macd.
        The H4 and M15 weights are deliberately NOT included here.
        """
        weights = self._weights()
        ind_cfg = self.config.get("indicators", default={})

        score_buy = 0.0
        score_sell = 0.0

        st_dir = self._supertrend_direction(df)
        ichi_dir = self._ichimoku_direction(df)
        ema_dir = self._ema_direction(df)

        rsi_cfg = ind_cfg.get("rsi", {})
        rsi = safe_float(
            Indicators.rsi(
                df["close"],
                int(rsi_cfg.get("period", 14)),
            ).iloc[-1],
            np.nan,
        )

        macd_cfg = ind_cfg.get("macd", {})
        _, _, macd_hist = Indicators.macd(
            df["close"],
            int(macd_cfg.get("fast", 12)),
            int(macd_cfg.get("slow", 26)),
            int(macd_cfg.get("signal", 9)),
        )
        macd_value = safe_float(macd_hist.iloc[-1], np.nan)

        if ema_dir == BUY:
            score_buy += weights["h1_trend"]
        elif ema_dir == SELL:
            score_sell += weights["h1_trend"]

        if st_dir == BUY:
            score_buy += weights["supertrend"]
        elif st_dir == SELL:
            score_sell += weights["supertrend"]

        if ichi_dir == BUY:
            score_buy += weights["ichimoku"]
        elif ichi_dir == SELL:
            score_sell += weights["ichimoku"]

        buy_min = float(rsi_cfg.get("buy_min", 52.0))
        buy_max = float(rsi_cfg.get("buy_max", 72.0))
        sell_min = float(rsi_cfg.get("sell_min", 28.0))
        sell_max = float(rsi_cfg.get("sell_max", 48.0))

        if math.isfinite(rsi):
            if buy_min <= rsi <= buy_max:
                score_buy += weights["rsi"]
            if sell_min <= rsi <= sell_max:
                score_sell += weights["rsi"]

        if math.isfinite(macd_value):
            if macd_value > 0:
                score_buy += weights["macd"]
            elif macd_value < 0:
                score_sell += weights["macd"]

        maximum = (
            weights["h1_trend"]
            + weights["supertrend"]
            + weights["ichimoku"]
            + weights["rsi"]
            + weights["macd"]
        )

        if score_buy > score_sell:
            direction = BUY
            score = score_buy
        elif score_sell > score_buy:
            direction = SELL
            score = score_sell
        else:
            direction = NEUTRAL
            score = 0.0

        details = {
            "ema_direction": ema_dir,
            "supertrend_direction": st_dir,
            "ichimoku_direction": ichi_dir,
            "rsi": rsi,
            "macd_histogram": macd_value,
        }

        return direction, score, maximum, details

    def m15_confirmation(
        self,
        df: pd.DataFrame,
        desired_direction: int,
    ) -> Tuple[int, float, float, Dict[str, Any]]:
        """
        M15 is a confirmation block with ONE configured weight.

        The individual M15 indicators are used to determine confirmation,
        but the final contribution to the global score is exactly
        signal_engine.weights.m15_confirmation.
        """
        if desired_direction == NEUTRAL:
            return NEUTRAL, 0.0, 1.0, {
                "supertrend_direction": NEUTRAL,
                "ichimoku_direction": NEUTRAL,
                "ema_direction": NEUTRAL,
                "votes": 0,
            }

        st_dir = self._supertrend_direction(df)
        ichi_dir = self._ichimoku_direction(df)
        ema_dir = self._ema_direction(df)

        votes = sum(
            d == desired_direction
            for d in [st_dir, ichi_dir, ema_dir]
        )

        # 2/3 or 3/3 confirms the desired direction.
        direction = desired_direction if votes >= 2 else NEUTRAL

        return (
            direction,
            float(votes),
            3.0,
            {
                "supertrend_direction": st_dir,
                "ichimoku_direction": ichi_dir,
                "ema_direction": ema_dir,
                "votes": votes,
            },
        )

    def calculate_quality(
        self,
        regime: Regime,
        h1_score: float,
        h1_max: float,
        m15_score: float,
        m15_max: float,
    ) -> float:
        """Independent setup quality metric. Never used as the signal score."""
        if h1_max <= 0:
            return 0.0

        h1_strength = clamp(h1_score / h1_max, 0.0, 1.0)

        m15_strength = (
            clamp(m15_score / m15_max, 0.0, 1.0)
            if m15_max > 0
            else 0.0
        )

        adx_strength = clamp(regime.adx / 35.0, 0.0, 1.0)

        volatility_quality = 1.0
        if regime.atr_percentile < 20:
            volatility_quality = 0.4
        elif regime.atr_percentile > 90:
            volatility_quality = 0.6

        quality = (
            h1_strength * 0.45
            + m15_strength * 0.25
            + adx_strength * 0.20
            + volatility_quality * 0.10
        )

        return clamp(quality, 0.0, 1.0)

    def _global_score(
        self,
        candidate: int,
        h4_dir: int,
        h1_details: Dict[str, Any],
        m15_dir: int,
    ) -> Tuple[float, Dict[str, float]]:
        """Build the actual configured directional score on the 0..10 scale."""
        weights = self._weights()

        components = {
            "h4_trend": weights["h4_trend"] if h4_dir == candidate else 0.0,
            "h1_trend": weights["h1_trend"] if h1_details["ema_direction"] == candidate else 0.0,
            "supertrend": weights["supertrend"] if h1_details["supertrend_direction"] == candidate else 0.0,
            "ichimoku": weights["ichimoku"] if h1_details["ichimoku_direction"] == candidate else 0.0,
            "rsi": 0.0,
            "macd": 0.0,
            "m15_confirmation": weights["m15_confirmation"] if m15_dir == candidate else 0.0,
        }

        rsi = safe_float(h1_details.get("rsi"), np.nan)
        rsi_cfg = self.config.get(
            "indicators",
            "rsi",
            default={},
        )

        if math.isfinite(rsi):
            if candidate == BUY:
                if float(rsi_cfg.get("buy_min", 52.0)) <= rsi <= float(rsi_cfg.get("buy_max", 72.0)):
                    components["rsi"] = weights["rsi"]
            elif candidate == SELL:
                if float(rsi_cfg.get("sell_min", 28.0)) <= rsi <= float(rsi_cfg.get("sell_max", 48.0)):
                    components["rsi"] = weights["rsi"]

        macd_value = safe_float(h1_details.get("macd_histogram"), np.nan)
        if math.isfinite(macd_value):
            if candidate == BUY and macd_value > 0:
                components["macd"] = weights["macd"]
            elif candidate == SELL and macd_value < 0:
                components["macd"] = weights["macd"]

        return round(sum(components.values()), 4), components

    def _log_score(
        self,
        symbol: Optional[str],
        candidate: int,
        h4_dir: int,
        h1_dir: int,
        m15_dir: int,
        score: float,
        maximum: float,
        components: Dict[str, float],
        quality: float,
        regime: Regime,
        decision: int,
    ) -> None:
        label = symbol or "MARKET"
        self.logger.info(
            f"{label} | candidate={direction_name(candidate)} | "
            f"H4={direction_name(h4_dir)} | "
            f"H1={direction_name(h1_dir)} | "
            f"M15={direction_name(m15_dir)} | "
            f"SCORE={score:.2f}/{maximum:.2f} | "
            f"QUALITY={quality:.2f} | "
            f"REGIME={regime.name} | "
            f"ADX={regime.adx:.2f} | "
            f"ATR%={regime.atr_percentile:.1f} | "
            f"DECISION={direction_name(decision)}"
        )

        self.logger.info(
            f"{label} SCORE COMPONENTS | "
            f"H4={components['h4_trend']:.2f} | "
            f"H1={components['h1_trend']:.2f} | "
            f"ST={components['supertrend']:.2f} | "
            f"ICHI={components['ichimoku']:.2f} | "
            f"RSI={components['rsi']:.2f} | "
            f"MACD={components['macd']:.2f} | "
            f"M15={components['m15_confirmation']:.2f} | "
            f"TOTAL={score:.2f}/{maximum:.2f}"
        )

    def build_signal(
        self,
        h4: pd.DataFrame,
        h1: pd.DataFrame,
        m15: pd.DataFrame,
        symbol: Optional[str] = None,
    ) -> Optional[SignalResult]:
        regime_engine = RegimeEngine(self.config, self.logger)
        regime = regime_engine.analyze(h4)

        if regime is None:
            return None

        h4_dir = self.h4_direction(h4)

        (
            h1_dir,
            h1_score,
            h1_max,
            h1_details,
        ) = self.h1_analysis(h1)

        candidate = h1_dir

        (
            m15_dir,
            m15_confirmation_score,
            m15_confirmation_max,
            m15_details,
        ) = self.m15_confirmation(m15, candidate)

        signal_cfg = self.config.get("signal_engine", default={})
        weights = self._weights()

        configured_maximum = float(
            signal_cfg.get("maximum_score", 10.0)
        )

        weight_sum = sum(weights.values())

        if abs(weight_sum - configured_maximum) > 1e-9:
            self.logger.warning(
                f"Signal weights total {weight_sum:.4f} != "
                f"configured maximum_score {configured_maximum:.4f}. "
                f"The configured weights remain authoritative."
            )

        maximum_score = weight_sum

        # If H1 has no direction, there is no valid candidate.
        if candidate == NEUTRAL:
            score = 0.0
            components = {
                "h4_trend": 0.0,
                "h1_trend": 0.0,
                "supertrend": 0.0,
                "ichimoku": 0.0,
                "rsi": 0.0,
                "macd": 0.0,
                "m15_confirmation": 0.0,
            }
        else:
            score, components = self._global_score(
                candidate,
                h4_dir,
                h1_details,
                m15_dir,
            )

        quality = self.calculate_quality(
            regime,
            h1_score,
            h1_max,
            m15_confirmation_score,
            m15_confirmation_max,
        )

        # --------------------------------------------------------
        # Hard alignment requirements from config.
        # --------------------------------------------------------
        final_direction = candidate

        require_h4 = bool(signal_cfg.get("require_h4_alignment", True))
        require_h1 = bool(signal_cfg.get("require_h1_alignment", True))
        require_m15 = bool(signal_cfg.get("require_m15_confirmation", True))

        if final_direction == NEUTRAL:
            final_direction = NEUTRAL

        if require_h4 and (final_direction == NEUTRAL or h4_dir != final_direction):
            final_direction = NEUTRAL

        if require_h1 and (final_direction == NEUTRAL or h1_dir != final_direction):
            final_direction = NEUTRAL

        if require_m15 and (final_direction == NEUTRAL or m15_dir != final_direction):
            final_direction = NEUTRAL

        # --------------------------------------------------------
        # Market filters.
        # --------------------------------------------------------
        filters = self.config.get("filters", default={})

        min_adx = float(filters.get("min_adx", 18.0))
        if regime.adx < min_adx:
            final_direction = NEUTRAL

        if filters.get("avoid_overextended_rsi", True):
            rsi = safe_float(h1_details.get("rsi"), np.nan)
            extreme_buy = float(filters.get("rsi_extreme_buy", 75.0))
            extreme_sell = float(filters.get("rsi_extreme_sell", 25.0))

            if final_direction == BUY and math.isfinite(rsi) and rsi >= extreme_buy:
                final_direction = NEUTRAL

            if final_direction == SELL and math.isfinite(rsi) and rsi <= extreme_sell:
                final_direction = NEUTRAL

        market_cfg = self.config.get("market_regime", default={})
        low_percentile = float(market_cfg.get("low_volatility_percentile", 20.0))
        high_percentile = float(market_cfg.get("high_volatility_percentile", 80.0))

        if filters.get("avoid_low_volatility", True):
            if regime.atr_percentile <= low_percentile:
                final_direction = NEUTRAL

        # Config calls this high_volatility_percentile=80, but the existing
        # V7 policy explicitly avoided only extreme volatility at >=98.
        # Preserve that policy; 80 is not automatically a trade blocker.
        if filters.get("avoid_extreme_volatility", True):
            if regime.atr_percentile >= 98.0:
                final_direction = NEUTRAL

        minimum_score = float(signal_cfg.get("minimum_score", 7.0))

        # CRITICAL FIX:
        # The real configured score is tested here. QUALITY IS NOT THE SCORE.
        if score < minimum_score:
            final_direction = NEUTRAL

        reasons = []

        if h4_dir == final_direction and final_direction != NEUTRAL:
            reasons.append("H4_ALIGN")
        if h1_dir == final_direction and final_direction != NEUTRAL:
            reasons.append("H1_ALIGN")
        if m15_dir == final_direction and final_direction != NEUTRAL:
            reasons.append("M15_CONFIRM")
        if regime.adx >= min_adx:
            reasons.append("ADX_OK")
        if low_percentile < regime.atr_percentile < 98.0:
            reasons.append("VOL_OK")
        if score >= minimum_score:
            reasons.append("SCORE_OK")

        reason = "|".join(reasons) if final_direction != NEUTRAL else "NO_VALID_SIGNAL"

        self._log_score(
            symbol=symbol,
            candidate=candidate,
            h4_dir=h4_dir,
            h1_dir=h1_dir,
            m15_dir=m15_dir,
            score=score,
            maximum=maximum_score,
            components=components,
            quality=quality,
            regime=regime,
            decision=final_direction,
        )

        return SignalResult(
            direction=final_direction,
            h4_direction=h4_dir,
            h1_direction=h1_dir,
            m15_direction=m15_dir,
            h1_score=h1_score,
            h1_max_score=h1_max,
            m15_score=m15_confirmation_score,
            m15_max_score=m15_confirmation_max,
            regime=regime.name,
            adx=regime.adx,
            atr=regime.atr,
            atr_percentile=regime.atr_percentile,
            rsi=h1_details["rsi"],
            macd_histogram=h1_details["macd_histogram"],
            supertrend_direction=h1_details["supertrend_direction"],
            ichimoku_direction=h1_details["ichimoku_direction"],
            quality=quality,
            reason=reason,
        )


# ============================================================
# RISK ENGINE
# ============================================================

class RiskEngine:
    def __init__(
        self,
        config: Config,
        logger: AppLogger,
    ):
        self.config = config
        self.logger = logger

    def account(self):
        return mt5.account_info()

    def balance(self) -> float:
        account = self.account()

        if account is None:
            return 0.0

        return safe_float(
            account.balance
        )

    def equity(self) -> float:
        account = self.account()

        if account is None:
            return 0.0

        return safe_float(
            account.equity
        )

    def today_start(self) -> datetime:
        now = now_utc()

        return now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    def get_deals_today(self):
        start = self.today_start()
        end = now_utc()

        try:
            return mt5.history_deals_get(
                start,
                end,
            )
        except Exception:
            return None

    def realized_pnl_today(self) -> float:
        deals = self.get_deals_today()

        if deals is None:
            return 0.0

        total = 0.0

        magic = int(
            self.config.get(
                "ea",
                "magic_number",
                default=0,
            )
        )

        for deal in deals:

            if int(
                getattr(
                    deal,
                    "magic",
                    0,
                )
            ) != magic:
                continue

            total += safe_float(
                getattr(
                    deal,
                    "profit",
                    0,
                )
            )

            total += safe_float(
                getattr(
                    deal,
                    "swap",
                    0,
                )
            )

            total += safe_float(
                getattr(
                    deal,
                    "commission",
                    0,
                )
            )

            total += safe_float(
                getattr(
                    deal,
                    "fee",
                    0,
                )
            )

        return total

    def daily_loss_percent(self) -> float:
        balance = self.balance()

        if balance <= 0:
            return 0.0

        pnl = self.realized_pnl_today()

        if pnl >= 0:
            return 0.0

        return abs(pnl) / balance * 100.0

    def positions(
        self,
        symbol: Optional[str] = None,
    ) -> List[Any]:

        magic = int(
            self.config.get(
                "ea",
                "magic_number",
                default=0,
            )
        )

        try:
            if symbol:
                positions = mt5.positions_get(
                    symbol=symbol
                )
            else:
                positions = mt5.positions_get()

        except Exception:
            return []

        if not positions:
            return []

        return [
            p
            for p in positions
            if int(
                getattr(
                    p,
                    "magic",
                    0,
                )
            ) == magic
        ]

    def open_positions_count(self) -> int:
        return len(
            self.positions()
        )

    def symbol_positions_count(
        self,
        symbol: str,
    ) -> int:

        return len(
            self.positions(symbol)
        )

    def open_position_risk_money(
        self,
        position: Any,
    ) -> float:

        if not position.sl:
            return 0.0

        symbol = position.symbol

        info = mt5.symbol_info(symbol)

        if info is None:
            return 0.0

        volume = safe_float(
            position.volume
        )

        open_price = safe_float(
            position.price_open
        )

        stop_price = safe_float(
            position.sl
        )

        if volume <= 0:
            return 0.0

        if position.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
        else:
            order_type = mt5.ORDER_TYPE_BUY

        try:
            result = mt5.order_calc_profit(
                order_type,
                symbol,
                volume,
                open_price,
                stop_price,
            )

            if result is not None:
                return abs(
                    safe_float(result)
                )

        except Exception:
            pass

        tick_size = safe_float(
            info.trade_tick_size
        )

        tick_value = safe_float(
            info.trade_tick_value
        )

        if tick_size <= 0:
            return 0.0

        distance = abs(
            open_price - stop_price
        )

        return (
            distance
            / tick_size
            * tick_value
            * volume
        )

    def open_risk_money(self) -> float:
        total = 0.0

        for position in self.positions():
            total += self.open_position_risk_money(
                position
            )

        return total

    def open_risk_percent(self) -> float:
        equity = self.equity()

        if equity <= 0:
            return 0.0

        return (
            self.open_risk_money()
            / equity
            * 100.0
        )

    def _position_closed_results(
        self,
    ) -> List[Tuple[datetime, int, float]]:

        deals = self.get_deals_today()

        if not deals:
            return []

        magic = int(
            self.config.get(
                "ea",
                "magic_number",
                default=0,
            )
        )

        grouped: Dict[
            int,
            Dict[str, Any]
        ] = {}

        for deal in deals:

            if int(
                getattr(
                    deal,
                    "magic",
                    0,
                )
            ) != magic:
                continue

            entry = getattr(
                deal,
                "entry",
                None,
            )

            if entry not in (
                mt5.DEAL_ENTRY_OUT,
                mt5.DEAL_ENTRY_OUT_BY,
                mt5.DEAL_ENTRY_INOUT,
            ):
                continue

            position_id = int(
                getattr(
                    deal,
                    "position_id",
                    0,
                )
            )

            if position_id <= 0:
                continue

            pnl = (
                safe_float(
                    getattr(
                        deal,
                        "profit",
                        0,
                    )
                )
                + safe_float(
                    getattr(
                        deal,
                        "swap",
                        0,
                    )
                )
                + safe_float(
                    getattr(
                        deal,
                        "commission",
                        0,
                    )
                )
                + safe_float(
                    getattr(
                        deal,
                        "fee",
                        0,
                    )
                )
            )

            deal_time = getattr(
                deal,
                "time",
                0,
            )

            grouped.setdefault(
                position_id,
                {
                    "pnl": 0.0,
                    "last_time": deal_time,
                },
            )

            grouped[position_id]["pnl"] += pnl

            grouped[position_id]["last_time"] = max(
                grouped[position_id]["last_time"],
                deal_time,
            )

        results = []

        for position_id, item in grouped.items():

            dt = datetime.fromtimestamp(
                int(item["last_time"]),
                tz=timezone.utc,
            )

            results.append(
                (
                    dt,
                    position_id,
                    safe_float(
                        item["pnl"]
                    ),
                )
            )

        results.sort(
            key=lambda x: x[0],
            reverse=True,
        )

        return results

    def trades_today(self) -> int:
        return len(
            self._position_closed_results()
        ) + len(
            self._open_position_ids_today()
        )

    def _open_position_ids_today(self) -> set:
        start = self.today_start()

        result = set()

        for p in self.positions():
            # MT5 position time is epoch.
            position_time = getattr(
                p,
                "time",
                0,
            )

            if position_time:
                dt = datetime.fromtimestamp(
                    int(position_time),
                    tz=timezone.utc,
                )

                if dt >= start:
                    result.add(
                        int(p.ticket)
                    )

        return result

    def consecutive_losses(self) -> int:
        results = self._position_closed_results()

        count = 0

        for _, _, pnl in results:

            if pnl < 0:
                count += 1

            else:
                break

        return count

    def last_trade_time(
        self,
    ) -> Optional[datetime]:

        results = self._position_closed_results()

        if results:
            return results[0][0]

        latest = None

        for position in self.positions():

            t = getattr(
                position,
                "time",
                0,
            )

            if not t:
                continue

            dt = datetime.fromtimestamp(
                int(t),
                tz=timezone.utc,
            )

            if latest is None or dt > latest:
                latest = dt

        return latest

    def last_loss_time(
        self,
    ) -> Optional[datetime]:

        results = self._position_closed_results()

        for dt, _, pnl in results:
            if pnl < 0:
                return dt

        return None

    def can_trade(
        self,
        symbol: str,
    ) -> Tuple[bool, str]:

        cfg = self.config.get(
            "risk_management",
            default={},
        )

        max_daily_loss = float(
            cfg.get(
                "max_daily_loss_percent",
                2.0,
            )
        )

        if (
            self.daily_loss_percent()
            >= max_daily_loss
        ):
            return (
                False,
                "MAX_DAILY_LOSS",
            )

        max_positions = int(
            cfg.get(
                "max_positions",
                3,
            )
        )

        if (
            self.open_positions_count()
            >= max_positions
        ):
            return (
                False,
                "MAX_POSITIONS",
            )

        max_symbol_positions = int(
            cfg.get(
                "max_positions_per_symbol",
                1,
            )
        )

        if (
            self.symbol_positions_count(symbol)
            >= max_symbol_positions
        ):
            return (
                False,
                "MAX_SYMBOL_POSITIONS",
            )

        max_trades_day = int(
            cfg.get(
                "max_trades_per_day",
                3,
            )
        )

        if (
            self.trades_today()
            >= max_trades_day
        ):
            return (
                False,
                "MAX_TRADES_DAY",
            )

        max_losses = int(
            cfg.get(
                "max_consecutive_losses",
                2,
            )
        )

        if (
            self.consecutive_losses()
            >= max_losses
        ):
            return (
                False,
                "MAX_CONSECUTIVE_LOSSES",
            )

        last_loss = self.last_loss_time()

        if last_loss:

            cooldown = int(
                cfg.get(
                    "cooldown_after_loss_minutes",
                    120,
                )
            )

            if (
                now_utc()
                < last_loss
                + timedelta(
                    minutes=cooldown
                )
            ):
                return (
                    False,
                    "LOSS_COOLDOWN",
                )

        last_trade = self.last_trade_time()

        if last_trade:

            cooldown = int(
                cfg.get(
                    "cooldown_after_trade_minutes",
                    60,
                )
            )

            if (
                now_utc()
                < last_trade
                + timedelta(
                    minutes=cooldown
                )
            ):
                return (
                    False,
                    "TRADE_COOLDOWN",
                )

        return True, "OK"

    def calculate_volume(
        self,
        symbol: str,
        direction: int,
        entry: float,
        stop: float,
    ) -> float:

        cfg = self.config.get(
            "risk_management",
            default={},
        )

        risk_percent = float(
            cfg.get(
                "risk_percent",
                1.0,
            )
        )

        equity = self.equity()

        if equity <= 0:
            return 0.0

        risk_money = (
            equity
            * risk_percent
            / 100.0
        )

        info = mt5.symbol_info(symbol)

        if info is None:
            return 0.0

        if direction == BUY:
            order_type = mt5.ORDER_TYPE_BUY
        else:
            order_type = mt5.ORDER_TYPE_SELL

        try:
            loss_one_lot = mt5.order_calc_profit(
                order_type,
                symbol,
                1.0,
                entry,
                stop,
            )

            if loss_one_lot is not None:
                loss_one_lot = abs(
                    safe_float(loss_one_lot)
                )

                if loss_one_lot > 0:
                    raw_volume = (
                        risk_money
                        / loss_one_lot
                    )

                    return normalize_volume(
                        raw_volume,
                        safe_float(
                            info.volume_min
                        ),
                        safe_float(
                            info.volume_max
                        ),
                        safe_float(
                            info.volume_step
                        ),
                    )

        except Exception:
            pass

        tick_size = safe_float(
            info.trade_tick_size
        )

        tick_value = safe_float(
            info.trade_tick_value
        )

        if (
            tick_size <= 0
            or tick_value <= 0
        ):
            return 0.0

        stop_distance = abs(
            entry - stop
        )

        loss_one_lot = (
            stop_distance
            / tick_size
            * tick_value
        )

        if loss_one_lot <= 0:
            return 0.0

        raw_volume = (
            risk_money
            / loss_one_lot
        )

        return normalize_volume(
            raw_volume,
            safe_float(
                info.volume_min
            ),
            safe_float(
                info.volume_max
            ),
            safe_float(
                info.volume_step
            ),
        )


# ============================================================
# STOP / TP ENGINE
# ============================================================

class StopEngine:
    def __init__(
        self,
        config: Config,
        logger: AppLogger,
    ):
        self.config = config
        self.logger = logger

    def calculate(
        self,
        symbol: str,
        direction: int,
        entry: float,
        h1: pd.DataFrame,
    ) -> Optional[Tuple[float, float, float, float]]:

        cfg = self.config.get(
            "stop_loss",
            default={},
        )

        atr_period = int(
            cfg.get(
                "atr_period",
                14,
            )
        )

        atr_multiplier = float(
            cfg.get(
                "atr_multiplier",
                1.5,
            )
        )

        use_structure = bool(
            cfg.get(
                "use_swing_structure",
                True,
            )
        )

        swing_lookback = int(
            cfg.get(
                "swing_lookback",
                10,
            )
        )

        buffer_atr = float(
            cfg.get(
                "structure_buffer_atr",
                0.20,
            )
        )

        maximum_sl_atr = float(
            cfg.get(
                "maximum_sl_atr",
                3.0,
            )
        )

        atr_series = Indicators.atr(
            h1,
            atr_period,
        )

        atr = safe_float(
            atr_series.iloc[-1]
        )

        if atr <= 0:
            return None

        atr_distance = (
            atr
            * atr_multiplier
        )

        if direction == BUY:

            atr_sl = (
                entry
                - atr_distance
            )

            if use_structure:
                swing_low = safe_float(
                    h1["low"]
                    .tail(swing_lookback)
                    .min()
                )

                structure_sl = (
                    swing_low
                    - atr
                    * buffer_atr
                )

                sl = min(
                    atr_sl,
                    structure_sl,
                )
            else:
                sl = atr_sl

            maximum_distance = (
                atr
                * maximum_sl_atr
            )

            sl = max(
                sl,
                entry - maximum_distance,
            )

            risk_distance = (
                entry - sl
            )

        else:

            atr_sl = (
                entry
                + atr_distance
            )

            if use_structure:
                swing_high = safe_float(
                    h1["high"]
                    .tail(swing_lookback)
                    .max()
                )

                structure_sl = (
                    swing_high
                    + atr
                    * buffer_atr
                )

                sl = max(
                    atr_sl,
                    structure_sl,
                )
            else:
                sl = atr_sl

            maximum_distance = (
                atr
                * maximum_sl_atr
            )

            sl = min(
                sl,
                entry + maximum_distance,
            )

            risk_distance = (
                sl - entry
            )

        if risk_distance <= 0:
            return None

        tp_cfg = self.config.get(
            "take_profit",
            default={},
        )

        tp1_r = float(
            tp_cfg.get(
                "tp1_r",
                1.0,
            )
        )

        tp2_r = float(
            tp_cfg.get(
                "tp2_r",
                2.0,
            )
        )

        tp3_r = float(
            tp_cfg.get(
                "tp3_r",
                3.0,
            )
        )

        if direction == BUY:

            tp1 = entry + risk_distance * tp1_r
            tp2 = entry + risk_distance * tp2_r
            tp3 = entry + risk_distance * tp3_r

        else:

            tp1 = entry - risk_distance * tp1_r
            tp2 = entry - risk_distance * tp2_r
            tp3 = entry - risk_distance * tp3_r

        minimum_rr = float(
            cfg.get(
                "minimum_rr",
                1.0,
            )
        )

        if tp3_r < minimum_rr:
            return None

        return (
            sl,
            tp1,
            tp2,
            tp3,
        )


# ============================================================
# BROKER EXECUTION
# ============================================================

class ExecutionEngine:
    def __init__(
        self,
        config: Config,
        logger: AppLogger,
        telegram: TelegramNotifier,
        csv_logger: CSVLogger,
    ):
        self.config = config
        self.logger = logger
        self.telegram = telegram
        self.csv = csv_logger

    def get_tick(
        self,
        symbol: str,
    ):
        return mt5.symbol_info_tick(
            symbol
        )

    def validate_symbol(
        self,
        symbol: str,
    ) -> bool:

        if not mt5.symbol_select(
            symbol,
            True,
        ):
            self.logger.error(
                f"Impossible de sélectionner {symbol}"
            )
            return False

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            self.logger.error(
                f"Symbol info absent: {symbol}"
            )
            return False

        return True

    def current_spread(
        self,
        symbol: str,
    ) -> float:

        tick = self.get_tick(
            symbol
        )

        if tick is None:
            return float("inf")

        return abs(
            safe_float(tick.ask)
            - safe_float(tick.bid)
        )

    def spread_allowed(
        self,
        symbol: str,
    ) -> Tuple[bool, float]:

        filters = self.config.get(
            "filters",
            default={},
        )

        if not filters.get(
            "max_spread_enabled",
            True,
        ):
            return True, 0.0

        symbol_cfg = self.config.get(
            "symbols",
            symbol,
            default={},
        )

        max_spread = float(
            symbol_cfg.get(
                "max_spread_price",
                float("inf"),
            )
        )

        spread = self.current_spread(
            symbol
        )

        return (
            spread <= max_spread,
            spread,
        )

    def validate_stops(
        self,
        symbol: str,
        direction: int,
        entry: float,
        sl: float,
        tp: float,
    ) -> Tuple[bool, str]:

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return False, "NO_SYMBOL_INFO"

        point = safe_float(
            info.point
        )

        if point <= 0:
            return False, "NO_POINT"

        stops_level = int(
            getattr(
                info,
                "trade_stops_level",
                0,
            )
        )

        freeze_level = int(
            getattr(
                info,
                "trade_freeze_level",
                0,
            )
        )

        minimum_distance = (
            max(
                stops_level,
                freeze_level,
            )
            * point
        )

        if direction == BUY:

            if sl >= entry:
                return False, "BUY_SL_INVALID"

            if tp <= entry:
                return False, "BUY_TP_INVALID"

            if (
                entry - sl
                < minimum_distance
            ):
                return (
                    False,
                    "BUY_SL_TOO_CLOSE",
                )

            if (
                tp - entry
                < minimum_distance
            ):
                return (
                    False,
                    "BUY_TP_TOO_CLOSE",
                )

        else:

            if sl <= entry:
                return False, "SELL_SL_INVALID"

            if tp >= entry:
                return False, "SELL_TP_INVALID"

            if (
                sl - entry
                < minimum_distance
            ):
                return (
                    False,
                    "SELL_SL_TOO_CLOSE",
                )

            if (
                entry - tp
                < minimum_distance
            ):
                return (
                    False,
                    "SELL_TP_TOO_CLOSE",
                )

        return True, "OK"

    def select_filling_mode(
        self,
        symbol: str,
    ) -> int:

        cfg = self.config.get(
            "execution",
            default={},
        )

        requested = str(
            cfg.get(
                "fill_policy",
                "AUTO",
            )
        ).upper()

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return mt5.ORDER_FILLING_FOK

        supported = int(
            getattr(
                info,
                "filling_mode",
                0,
            )
        )

        if requested == "FOK":
            return mt5.ORDER_FILLING_FOK

        if requested == "IOC":
            return mt5.ORDER_FILLING_IOC

        # AUTO
        if supported & mt5.SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK

        if supported & mt5.SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC

        return mt5.ORDER_FILLING_RETURN

    def normalize_price(
        self,
        symbol: str,
        price: float,
    ) -> float:

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return price

        return round_price(
            price,
            int(info.digits),
        )

    def validate_volume(
        self,
        symbol: str,
        volume: float,
    ) -> float:

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return 0.0

        return normalize_volume(
            volume,
            safe_float(
                info.volume_min
            ),
            safe_float(
                info.volume_max
            ),
            safe_float(
                info.volume_step
            ),
        )

    def send_entry(
        self,
        symbol: str,
        direction: int,
        volume: float,
        sl: float,
        tp3: float,
        signal: SignalResult,
    ) -> Optional[Any]:

        if not self.validate_symbol(
            symbol
        ):
            return None

        tick = self.get_tick(
            symbol
        )

        if tick is None:
            self.logger.error(
                f"Tick indisponible {symbol}"
            )
            return None

        if direction == BUY:
            order_type = mt5.ORDER_TYPE_BUY
            entry = safe_float(
                tick.ask
            )
        else:
            order_type = mt5.ORDER_TYPE_SELL
            entry = safe_float(
                tick.bid
            )

        volume = self.validate_volume(
            symbol,
            volume,
        )

        if volume <= 0:
            self.logger.warning(
                f"{symbol}: volume invalide"
            )
            return None

        sl = self.normalize_price(
            symbol,
            sl,
        )

        tp3 = self.normalize_price(
            symbol,
            tp3,
        )

        entry = self.normalize_price(
            symbol,
            entry,
        )

        valid, reason = self.validate_stops(
            symbol,
            direction,
            entry,
            sl,
            tp3,
        )

        if not valid:
            self.logger.warning(
                f"{symbol}: stops invalides "
                f"{reason}"
            )
            return None

        cfg = self.config.get(
            "execution",
            default={},
        )

        deviation = int(
            cfg.get(
                "deviation_points",
                50,
            )
        )

        filling = self.select_filling_mode(
            symbol
        )

        magic = int(
            self.config.get(
                "ea",
                "magic_number",
                default=123456,
            )
        )

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": entry,
            "sl": sl,
            "tp": tp3,
            "deviation": deviation,
            "magic": magic,
            "comment": (
                "V7|"
                f"{direction_name(direction)}"
            ),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        retry_count = int(
            cfg.get(
                "retry_count",
                3,
            )
        )

        retry_delay = float(
            cfg.get(
                "retry_delay_seconds",
                2,
            )
        )

        for attempt in range(
            retry_count + 1
        ):

            try:
                if cfg.get(
                    "validate_volume",
                    True,
                ):
                    request["volume"] = (
                        self.validate_volume(
                            symbol,
                            request["volume"],
                        )
                    )

                check = mt5.order_check(
                    request
                )

                if check is None:
                    self.logger.warning(
                        f"{symbol}: "
                        f"order_check=None | "
                        f"{mt5.last_error()}"
                    )

                else:
                    if getattr(
                        check,
                        "retcode",
                        0,
                    ) not in (
                        0,
                        mt5.TRADE_RETCODE_DONE,
                    ):
                        self.logger.warning(
                            f"{symbol}: "
                            f"order_check retcode="
                            f"{check.retcode} "
                            f"{getattr(check, 'comment', '')}"
                        )

                result = mt5.order_send(
                    request
                )

                if result is None:
                    self.logger.warning(
                        f"{symbol}: order_send=None "
                        f"{mt5.last_error()}"
                    )

                elif result.retcode in (
                    mt5.TRADE_RETCODE_DONE,
                    mt5.TRADE_RETCODE_PLACED,
                    mt5.TRADE_RETCODE_DONE_PARTIAL,
                ):
                    self.logger.info(
                        f"ENTRY {symbol} "
                        f"{direction_name(direction)} "
                        f"volume={volume} "
                        f"price={entry} "
                        f"SL={sl} "
                        f"TP3={tp3} "
                        f"ticket={result.order}"
                    )

                    self.csv.trade({
                        "timestamp": now_utc().isoformat(),
                        "action": "ENTRY",
                        "symbol": symbol,
                        "direction": direction_name(
                            direction
                        ),
                        "order_ticket": getattr(
                            result,
                            "order",
                            0,
                        ),
                        "deal_ticket": getattr(
                            result,
                            "deal",
                            0,
                        ),
                        "volume": volume,
                        "entry": entry,
                        "sl": sl,
                        "tp1": "",
                        "tp2": "",
                        "tp3": tp3,
                        "regime": signal.regime,
                        "quality": signal.quality,
                        "adx": signal.adx,
                        "atr": signal.atr,
                        "atr_percentile": (
                            signal.atr_percentile
                        ),
                        "reason": signal.reason,
                    })

                    self.telegram.send(
                        (
                            f"<b>V7 ENTRY</b>\n"
                            f"{symbol} "
                            f"{direction_name(direction)}\n"
                            f"Volume: {volume}\n"
                            f"Entry: {entry}\n"
                            f"SL: {sl}\n"
                            f"TP3: {tp3}\n"
                            f"Regime: {signal.regime}\n"
                            f"Quality: "
                            f"{signal.quality:.2f}\n"
                            f"ADX: {signal.adx:.1f}"
                        ),
                        category="entry",
                    )

                    return result

                self.logger.warning(
                    f"{symbol}: "
                    f"order_send retcode="
                    f"{getattr(result, 'retcode', None)} "
                    f"{getattr(result, 'comment', '')}"
                )

            except Exception as exc:
                self.logger.exception(
                    f"Execution error "
                    f"{symbol}: {exc}"
                )

            if attempt < retry_count:
                time.sleep(
                    retry_delay
                )

        return None

    def modify_position(
        self,
        position: Any,
        new_sl: float,
        new_tp: Optional[float] = None,
    ) -> bool:

        symbol = position.symbol

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return False

        digits = int(
            info.digits
        )

        new_sl = round_price(
            new_sl,
            digits,
        )

        if new_tp is None:
            new_tp = safe_float(
                position.tp
            )
        else:
            new_tp = round_price(
                new_tp,
                digits,
            )

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": position.ticket,
            "sl": new_sl,
            "tp": new_tp,
            "magic": int(
                self.config.get(
                    "ea",
                    "magic_number",
                    default=123456,
                )
            ),
        }

        try:
            result = mt5.order_send(
                request
            )

            if result is None:
                return False

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                return True

            self.logger.warning(
                f"SL modification failed "
                f"{symbol} "
                f"{position.ticket}: "
                f"{result.retcode} "
                f"{result.comment}"
            )

        except Exception as exc:
            self.logger.exception(
                f"Modify position error: {exc}"
            )

        return False

    def close_position(
        self,
        position: Any,
        volume: Optional[float] = None,
    ) -> bool:

        symbol = position.symbol

        tick = mt5.symbol_info_tick(
            symbol
        )

        if tick is None:
            return False

        if position.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return False

        if volume is None:
            volume = safe_float(
                position.volume
            )

        volume = self.validate_volume(
            symbol,
            volume,
        )

        if volume <= 0:
            return False

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "position": position.ticket,
            "price": self.normalize_price(
                symbol,
                price,
            ),
            "deviation": int(
                self.config.get(
                    "execution",
                    "deviation_points",
                    default=50,
                )
            ),
            "magic": int(
                self.config.get(
                    "ea",
                    "magic_number",
                    default=123456,
                )
            ),
            "comment": "V7|CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.select_filling_mode(
                symbol
            ),
        }

        try:
            result = mt5.order_send(
                request
            )

            if result is None:
                return False

            if result.retcode in (
                mt5.TRADE_RETCODE_DONE,
                mt5.TRADE_RETCODE_DONE_PARTIAL,
            ):

                self.logger.info(
                    f"CLOSE {symbol} "
                    f"ticket={position.ticket} "
                    f"volume={volume}"
                )

                self.csv.trade({
                    "timestamp": now_utc().isoformat(),
                    "action": "CLOSE",
                    "symbol": symbol,
                    "direction": (
                        "BUY"
                        if position.type
                        == mt5.POSITION_TYPE_BUY
                        else "SELL"
                    ),
                    "order_ticket": getattr(
                        result,
                        "order",
                        0,
                    ),
                    "deal_ticket": getattr(
                        result,
                        "deal",
                        0,
                    ),
                    "volume": volume,
                    "entry": position.price_open,
                    "sl": position.sl,
                    "tp1": "",
                    "tp2": "",
                    "tp3": position.tp,
                    "regime": "",
                    "quality": "",
                    "adx": "",
                    "atr": "",
                    "atr_percentile": "",
                    "reason": "POSITION_CLOSE",
                })

                self.telegram.send(
                    (
                        f"<b>V7 EXIT</b>\n"
                        f"{symbol}\n"
                        f"Ticket: {position.ticket}\n"
                        f"Volume: {volume}"
                    ),
                    category="exit",
                )

                return True

        except Exception as exc:
            self.logger.exception(
                f"Close position error: {exc}"
            )

        return False


# ============================================================
# STATE
# ============================================================

@dataclass
class PositionState:
    ticket: int
    symbol: str
    direction: int

    entry: float
    initial_sl: float
    initial_risk: float

    tp1: float
    tp2: float
    tp3: float

    initial_volume: float

    tp1_hit: bool = False
    tp2_hit: bool = False

    created_at: str = ""


class StateManager:
    def __init__(
        self,
        config: Config,
        logger: AppLogger,
    ):
        self.config = config
        self.logger = logger

        self.enabled = bool(
            config.get(
                "state",
                "enabled",
                default=True,
            )
        )

        self.path = BASE_DIR / str(
            config.get(
                "state",
                "file",
                default="trade_state_v7.json",
            )
        )

        self.data = {
            "positions": {},
            "meta": {},
        }

        self.load()

    def load(self):
        if not self.enabled:
            return

        loaded = safe_json_load(
            self.path,
            None,
        )

        if isinstance(
            loaded,
            dict,
        ):
            self.data = loaded

            self.data.setdefault(
                "positions",
                {},
            )

            self.data.setdefault(
                "meta",
                {},
            )

    def save(self):
        if not self.enabled:
            return

        safe_json_save(
            self.path,
            self.data,
        )

    def add(
        self,
        state: PositionState,
    ):
        self.data["positions"][
            str(state.ticket)
        ] = asdict(state)

        self.save()

    def remove(
        self,
        ticket: int,
    ):
        self.data["positions"].pop(
            str(ticket),
            None,
        )

        self.save()

    def get(
        self,
        ticket: int,
    ) -> Optional[PositionState]:

        raw = self.data["positions"].get(
            str(ticket)
        )

        if not raw:
            return None

        try:
            return PositionState(
                **raw
            )
        except Exception:
            return None

    def all_states(
        self,
    ) -> List[PositionState]:

        result = []

        for raw in self.data[
            "positions"
        ].values():

            try:
                result.append(
                    PositionState(**raw)
                )
            except Exception:
                continue

        return result

    def set_meta(
        self,
        key: str,
        value: Any,
    ):
        self.data["meta"][key] = value
        self.save()

    def get_meta(
        self,
        key: str,
        default=None,
    ):
        return self.data[
            "meta"
        ].get(
            key,
            default,
        )

    def reconcile(
        self,
        positions: List[Any],
        config: Config,
    ):

        live_tickets = {
            str(p.ticket)
            for p in positions
        }

        stored_tickets = list(
            self.data[
                "positions"
            ].keys()
        )

        changed = False

        for ticket in stored_tickets:
            if ticket not in live_tickets:
                self.data[
                    "positions"
                ].pop(
                    ticket,
                    None,
                )
                changed = True

        # Reconstruction minimale si une position
        # existe mais n'a pas d'état.
        for position in positions:

            if str(position.ticket) in (
                self.data["positions"]
            ):
                continue

            reconstructed = self.reconstruct(
                position,
                config,
            )

            if reconstructed:
                self.data[
                    "positions"
                ][str(position.ticket)] = asdict(
                    reconstructed
                )
                changed = True

        if changed:
            self.save()

    def reconstruct(
        self,
        position: Any,
        config: Config,
    ) -> Optional[PositionState]:

        entry = safe_float(
            position.price_open
        )

        sl = safe_float(
            position.sl
        )

        if entry <= 0 or sl <= 0:
            return None

        direction = (
            BUY
            if position.type
            == mt5.POSITION_TYPE_BUY
            else SELL
        )

        initial_risk = abs(
            entry - sl
        )

        if initial_risk <= 0:
            return None

        tp_cfg = config.get(
            "take_profit",
            default={},
        )

        tp1_r = float(
            tp_cfg.get(
                "tp1_r",
                1,
            )
        )

        tp2_r = float(
            tp_cfg.get(
                "tp2_r",
                2,
            )
        )

        tp3_r = float(
            tp_cfg.get(
                "tp3_r",
                3,
            )
        )

        if direction == BUY:

            tp1 = entry + initial_risk * tp1_r
            tp2 = entry + initial_risk * tp2_r
            tp3 = entry + initial_risk * tp3_r

        else:

            tp1 = entry - initial_risk * tp1_r
            tp2 = entry - initial_risk * tp2_r
            tp3 = entry - initial_risk * tp3_r

        return PositionState(
            ticket=int(position.ticket),
            symbol=str(position.symbol),
            direction=direction,
            entry=entry,
            initial_sl=sl,
            initial_risk=initial_risk,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            initial_volume=safe_float(
                position.volume
            ),
            created_at=now_utc().isoformat(),
        )


# ============================================================
# POSITION MANAGER
# ============================================================

class PositionManager:
    def __init__(
        self,
        config: Config,
        logger: AppLogger,
        state: StateManager,
        execution: ExecutionEngine,
        market_data: MarketData,
    ):
        self.config = config
        self.logger = logger
        self.state = state
        self.execution = execution
        self.market_data = market_data

    def current_price(
        self,
        position: Any,
    ) -> Optional[float]:

        tick = mt5.symbol_info_tick(
            position.symbol
        )

        if tick is None:
            return None

        if position.type == mt5.POSITION_TYPE_BUY:
            return safe_float(
                tick.bid
            )

        return safe_float(
            tick.ask
        )

    def r_multiple(
        self,
        position: Any,
        state: PositionState,
    ) -> float:

        price = self.current_price(
            position
        )

        if price is None:
            return 0.0

        if state.initial_risk <= 0:
            return 0.0

        if state.direction == BUY:
            profit_distance = (
                price - state.entry
            )
        else:
            profit_distance = (
                state.entry - price
            )

        return (
            profit_distance
            / state.initial_risk
        )

    def partial_volume(
        self,
        symbol: str,
        current_volume: float,
        initial_volume: float,
        percent: float,
    ) -> float:

        if percent <= 0:
            return 0.0

        desired = (
            initial_volume
            * percent
            / 100.0
        )

        desired = min(
            desired,
            current_volume,
        )

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return 0.0

        min_volume = safe_float(
            info.volume_min
        )

        step = safe_float(
            info.volume_step
        )

        remaining = (
            current_volume
            - desired
        )

        desired = floor_to_step(
            desired,
            step,
        )

        # Si le reliquat devient inférieur
        # au minimum broker, on ferme la partie
        # nécessaire pour laisser 0.
        if (
            remaining > 0
            and remaining < min_volume
        ):
            desired = current_volume

        if desired < min_volume:
            return 0.0

        return normalize_volume(
            desired,
            min_volume,
            safe_float(
                info.volume_max
            ),
            step,
        )

    def manage_tp(
        self,
        position: Any,
        state: PositionState,
        r: float,
    ):

        tp_cfg = self.config.get(
            "take_profit",
            default={},
        )

        if not state.tp1_hit:

            tp1_r = float(
                tp_cfg.get(
                    "tp1_r",
                    1,
                )
            )

            if r >= tp1_r:

                percent = float(
                    tp_cfg.get(
                        "tp1_close_percent",
                        40,
                    )
                )

                volume = self.partial_volume(
                    position.symbol,
                    safe_float(
                        position.volume
                    ),
                    state.initial_volume,
                    percent,
                )

                if volume > 0:

                    if self.execution.close_position(
                        position,
                        volume,
                    ):
                        state.tp1_hit = True
                        self.state.add(state)

                        self.logger.info(
                            f"{position.symbol}: "
                            f"TP1 atteint | "
                            f"R={r:.2f}"
                        )

                        return

        if not state.tp2_hit:

            tp2_r = float(
                tp_cfg.get(
                    "tp2_r",
                    2,
                )
            )

            if r >= tp2_r:

                percent = float(
                    tp_cfg.get(
                        "tp2_close_percent",
                        30,
                    )
                )

                volume = self.partial_volume(
                    position.symbol,
                    safe_float(
                        position.volume
                    ),
                    state.initial_volume,
                    percent,
                )

                if volume > 0:

                    if self.execution.close_position(
                        position,
                        volume,
                    ):
                        state.tp2_hit = True
                        self.state.add(state)

                        self.logger.info(
                            f"{position.symbol}: "
                            f"TP2 atteint | "
                            f"R={r:.2f}"
                        )

    def manage_breakeven(
        self,
        position: Any,
        state: PositionState,
        r: float,
    ):

        cfg = self.config.get(
            "position_management",
            "breakeven",
            default={},
        )

        if not cfg.get(
            "enabled",
            True,
        ):
            return

        activation_r = float(
            cfg.get(
                "activation_r",
                1.0,
            )
        )

        offset_r = float(
            cfg.get(
                "offset_r",
                0.10,
            )
        )

        if r < activation_r:
            return

        if state.direction == BUY:

            new_sl = (
                state.entry
                + state.initial_risk
                * offset_r
            )

            if (
                position.sl <= 0
                or new_sl > position.sl
            ):
                if self.execution.modify_position(
                    position,
                    new_sl,
                    position.tp,
                ):
                    self.logger.info(
                        f"{position.symbol}: "
                        f"BUY BE/LOCK "
                        f"SL={new_sl}"
                    )

        else:

            new_sl = (
                state.entry
                - state.initial_risk
                * offset_r
            )

            if (
                position.sl <= 0
                or new_sl < position.sl
            ):
                if self.execution.modify_position(
                    position,
                    new_sl,
                    position.tp,
                ):
                    self.logger.info(
                        f"{position.symbol}: "
                        f"SELL BE/LOCK "
                        f"SL={new_sl}"
                    )

    def manage_trailing(
        self,
        position: Any,
        state: PositionState,
        r: float,
    ):

        cfg = self.config.get(
            "position_management",
            "trailing",
            default={},
        )

        if not cfg.get(
            "enabled",
            True,
        ):
            return

        activation_r = float(
            cfg.get(
                "activation_r",
                2.0,
            )
        )

        if r < activation_r:
            return

        atr_period = int(
            cfg.get(
                "atr_period",
                14,
            )
        )

        atr_multiplier = float(
            cfg.get(
                "atr_multiplier",
                1.0,
            )
        )

        tf_name = self.config.get(
            "timeframes",
            "atr",
            default="H1",
        )

        df = self.market_data.fetch(
            position.symbol,
            tf_name,
            150,
        )

        if df is None:
            return

        atr = Indicators.atr(
            df,
            atr_period,
        )

        current_atr = safe_float(
            atr.iloc[-1]
        )

        if current_atr <= 0:
            return

        price = self.current_price(
            position
        )

        if price is None:
            return

        stop_cfg = self.config.get(
            "stop_loss",
            default={},
        )

        swing_lookback = int(
            stop_cfg.get(
                "swing_lookback",
                10,
            )
        )

        buffer_atr = float(
            stop_cfg.get(
                "structure_buffer_atr",
                0.20,
            )
        )

        if state.direction == BUY:

            atr_stop = (
                price
                - current_atr
                * atr_multiplier
            )

            swing_low = safe_float(
                df["low"]
                .tail(swing_lookback)
                .min()
            )

            structure_stop = (
                swing_low
                - current_atr
                * buffer_atr
            )

            candidate = max(
                atr_stop,
                structure_stop,
            )

            # Le trailing ne doit jamais
            # repasser sous le BE/SL existant.
            if (
                position.sl <= 0
                or candidate > position.sl
            ):

                info = mt5.symbol_info(
                    position.symbol
                )

                if info:
                    min_distance = (
                        max(
                            int(
                                getattr(
                                    info,
                                    "trade_stops_level",
                                    0,
                                )
                            ),
                            int(
                                getattr(
                                    info,
                                    "trade_freeze_level",
                                    0,
                                )
                            ),
                        )
                        * safe_float(
                            info.point
                        )
                    )

                    candidate = min(
                        candidate,
                        price - min_distance,
                    )

                if (
                    candidate > state.entry
                    and (
                        position.sl <= 0
                        or candidate > position.sl
                    )
                ):
                    self.execution.modify_position(
                        position,
                        candidate,
                        position.tp,
                    )

        else:

            atr_stop = (
                price
                + current_atr
                * atr_multiplier
            )

            swing_high = safe_float(
                df["high"]
                .tail(swing_lookback)
                .max()
            )

            structure_stop = (
                swing_high
                + current_atr
                * buffer_atr
            )

            candidate = min(
                atr_stop,
                structure_stop,
            )

            if (
                position.sl <= 0
                or candidate < position.sl
            ):

                info = mt5.symbol_info(
                    position.symbol
                )

                if info:
                    min_distance = (
                        max(
                            int(
                                getattr(
                                    info,
                                    "trade_stops_level",
                                    0,
                                )
                            ),
                            int(
                                getattr(
                                    info,
                                    "trade_freeze_level",
                                    0,
                                )
                            ),
                        )
                        * safe_float(
                            info.point
                        )
                    )

                    candidate = max(
                        candidate,
                        price + min_distance,
                    )

                if (
                    candidate < state.entry
                    and (
                        position.sl <= 0
                        or candidate < position.sl
                    )
                ):
                    self.execution.modify_position(
                        position,
                        candidate,
                        position.tp,
                    )

    def manage_position(
        self,
        position: Any,
    ):

        state = self.state.get(
            int(position.ticket)
        )

        if state is None:

            state = self.state.reconstruct(
                position,
                self.config,
            )

            if state:
                self.state.add(
                    state
                )

        if state is None:
            return

        r = self.r_multiple(
            position,
            state,
        )

        self.manage_tp(
            position,
            state,
            r,
        )

        # On relit la position car TP partiel
        # peut avoir changé son volume.
        refreshed = mt5.positions_get(
            ticket=position.ticket
        )

        if not refreshed:
            self.state.remove(
                int(position.ticket)
            )
            return

        position = refreshed[0]

        self.manage_breakeven(
            position,
            state,
            r,
        )

        self.manage_trailing(
            position,
            state,
            r,
        )

    def manage_all(self):
        positions = RiskEngine(
            self.config,
            self.logger,
        ).positions()

        self.state.reconcile(
            positions,
            self.config,
        )

        for position in positions:

            try:
                self.manage_position(
                    position
                )
            except Exception as exc:
                self.logger.exception(
                    f"Position manager "
                    f"error {position.symbol}: "
                    f"{exc}"
                )


# ============================================================
# TRADING ENGINE
# ============================================================

class TradingEngine:
    def __init__(
        self,
        config: Config,
        logger: AppLogger,
        market_data: MarketData,
        telegram: TelegramNotifier,
        csv_logger: CSVLogger,
        state: StateManager,
    ):

        self.config = config
        self.logger = logger
        self.market_data = market_data
        self.telegram = telegram
        self.csv = csv_logger
        self.state = state

        self.regime_engine = RegimeEngine(
            config,
            logger,
        )

        self.signal_engine = SignalEngine(
            config,
            logger,
        )

        self.risk_engine = RiskEngine(
            config,
            logger,
        )

        self.stop_engine = StopEngine(
            config,
            logger,
        )

        self.execution = ExecutionEngine(
            config,
            logger,
            telegram,
            csv_logger,
        )

    def enabled_symbols(
        self,
    ) -> List[str]:

        symbols = self.config.get(
            "symbols",
            default={},
        )

        result = []

        for symbol, cfg in symbols.items():

            if isinstance(
                cfg,
                dict,
            ) and cfg.get(
                "enabled",
                False,
            ):
                result.append(
                    symbol
                )

        return result

    def atr_allowed(
        self,
        symbol: str,
        atr: float,
    ) -> bool:

        cfg = self.config.get(
            "symbols",
            symbol,
            default={},
        )

        min_atr = float(
            cfg.get(
                "min_atr",
                0.0,
            )
        )

        max_atr = float(
            cfg.get(
                "max_atr",
                float("inf"),
            )
        )

        return (
            min_atr
            <= atr
            <= max_atr
        )

    def open_risk_after_trade(
        self,
        symbol: str,
        direction: int,
        entry: float,
        stop: float,
        volume: float,
    ) -> float:

        current = self.risk_engine.open_risk_money()

        if direction == BUY:
            order_type = mt5.ORDER_TYPE_BUY
        else:
            order_type = mt5.ORDER_TYPE_SELL

        try:
            new_risk = mt5.order_calc_profit(
                order_type,
                symbol,
                volume,
                entry,
                stop,
            )

            if new_risk is not None:
                current += abs(
                    safe_float(new_risk)
                )

        except Exception:
            pass

        equity = self.risk_engine.equity()

        if equity <= 0:
            return float("inf")

        return (
            current
            / equity
            * 100.0
        )

    def signal_key(
        self,
        symbol: str,
        direction: int,
        candle_time: Any,
    ) -> str:

        return (
            f"{symbol}|"
            f"{direction_name(direction)}|"
            f"{candle_time}"
        )

    def already_traded_signal(
        self,
        key: str,
    ) -> bool:

        if not self.config.get(
            "risk_management",
            "one_trade_per_signal",
            default=True,
        ):
            return False

        previous = self.state.get_meta(
            "last_signal_key",
            "",
        )

        return previous == key

    def process_symbol(
        self,
        symbol: str,
        h1_bar_timestamp: Optional[int] = None,
    ) -> Optional[SignalResult]:

        self.logger.info(
            f"Analyse {symbol}..."
        )

        session_engine = SessionEngine(
            self.config
        )

        session_name, session_allowed = (
            session_engine.current_session()
        )

        if not session_allowed:
            self.logger.info(
                f"{symbol}: hors session "
                f"({session_name})"
            )
            return None

        news_filter = NewsFilter(
            self.config,
            self.logger,
        )

        blocked, news_reason = (
            news_filter.is_blocked()
        )

        if blocked:
            self.logger.info(
                f"{symbol}: news block "
                f"{news_reason}"
            )
            return None

        can_trade, risk_reason = (
            self.risk_engine.can_trade(
                symbol
            )
        )

        if not can_trade:
            self.logger.info(
                f"{symbol}: risk block "
                f"{risk_reason}"
            )
            return None

        spread_ok, spread = (
            self.execution.spread_allowed(
                symbol
            )
        )

        if not spread_ok:
            self.logger.info(
                f"{symbol}: spread trop élevé "
                f"{spread}"
            )
            return None

        trend_tf = self.config.get(
            "timeframes",
            "trend",
            default="H4",
        )

        signal_tf = self.config.get(
            "timeframes",
            "signal",
            default="H1",
        )

        confirmation_tf = self.config.get(
            "timeframes",
            "confirmation",
            default="M15",
        )

        h4 = self.market_data.fetch(
            symbol,
            trend_tf,
            350,
        )

        h1 = self.market_data.fetch(
            symbol,
            signal_tf,
            350,
        )

        m15 = self.market_data.fetch(
            symbol,
            confirmation_tf,
            350,
        )

        if (
            h4 is None
            or h1 is None
            or m15 is None
        ):
            return None

        signal = self.signal_engine.build_signal(
            h4,
            h1,
            m15,
            symbol=symbol,
        )

        if signal is None:
            return None

        candle_time = (
            h1.iloc[-1]["time"]
        )

        key = self.signal_key(
            symbol,
            signal.direction,
            candle_time,
        )

        self.csv.signal({
            "timestamp": now_utc().isoformat(),
            "symbol": symbol,
            "signal_candle": str(
                candle_time
            ),
            "direction": direction_name(
                signal.direction
            ),
            "h4_direction": direction_name(
                signal.h4_direction
            ),
            "h1_direction": direction_name(
                signal.h1_direction
            ),
            "m15_direction": direction_name(
                signal.m15_direction
            ),
            "h1_score": signal.h1_score,
            "h1_max_score": signal.h1_max_score,
            "m15_score": signal.m15_score,
            "m15_max_score": signal.m15_max_score,
            "regime": signal.regime,
            "adx": signal.adx,
            "atr": signal.atr,
            "atr_percentile": signal.atr_percentile,
            "rsi": signal.rsi,
            "macd_histogram": signal.macd_histogram,
            "quality": signal.quality,
            "spread": spread,
            "session": session_name,
            "reason": signal.reason,
        })

        self.logger.info(
            f"{symbol} | "
            f"Signal={direction_name(signal.direction)} | "
            f"H4={direction_name(signal.h4_direction)} | "
            f"H1={direction_name(signal.h1_direction)} | "
            f"M15={direction_name(signal.m15_direction)} | "
            f"Regime={signal.regime} | "
            f"ADX={signal.adx:.2f} | "
            f"ATR%={signal.atr_percentile:.1f} | "
            f"Quality={signal.quality:.2f}"
        )

        if signal.direction == NEUTRAL:
            return signal

        if self.already_traded_signal(
            key
        ):
            self.logger.info(
                f"{symbol}: signal déjà traité"
            )
            return signal

        if not self.atr_allowed(
            symbol,
            signal.atr,
        ):
            self.logger.info(
                f"{symbol}: ATR hors plage"
            )
            return signal

        tick = mt5.symbol_info_tick(
            symbol
        )

        if tick is None:
            return signal

        if signal.direction == BUY:
            entry = safe_float(
                tick.ask
            )
        else:
            entry = safe_float(
                tick.bid
            )

        stop_data = self.stop_engine.calculate(
            symbol,
            signal.direction,
            entry,
            h1,
        )

        if stop_data is None:
            self.logger.warning(
                f"{symbol}: "
                f"SL/TP calculation impossible"
            )
            return signal

        sl, tp1, tp2, tp3 = stop_data

        volume = (
            self.risk_engine.calculate_volume(
                symbol,
                signal.direction,
                entry,
                sl,
            )
        )

        if volume <= 0:
            self.logger.warning(
                f"{symbol}: volume calculé = 0"
            )
            return signal

        max_open_risk = float(
            self.config.get(
                "risk_management",
                "max_open_risk_percent",
                default=2.0,
            )
        )

        risk_after = (
            self.open_risk_after_trade(
                symbol,
                signal.direction,
                entry,
                sl,
                volume,
            )
        )

        if risk_after > max_open_risk:
            self.logger.warning(
                f"{symbol}: "
                f"open risk {risk_after:.2f}% "
                f"> {max_open_risk:.2f}%"
            )
            return signal

        self.logger.info(
            f"{symbol} SETUP | "
            f"{direction_name(signal.direction)} | "
            f"Entry={entry} | "
            f"SL={sl} | "
            f"TP1={tp1} | "
            f"TP2={tp2} | "
            f"TP3={tp3} | "
            f"Volume={volume} | "
            f"Risque={self.config.get('risk_management', 'risk_percent', default=1.0)}%"
        )

        self.state.set_meta(
            "last_signal_key",
            key,
        )

        test_mode = bool(
            self.config.get(
                "ea",
                "test_mode",
                default=True,
            )
        )

        if test_mode:

            self.logger.info(
                f"[TEST MODE] "
                f"{symbol}: "
                f"aucun ordre envoyé."
            )

            return signal

        result = self.execution.send_entry(
            symbol,
            signal.direction,
            volume,
            sl,
            tp3,
            signal,
        )

        if result is None:
            return signal

        # On récupère la position créée.
        time.sleep(0.5)

        positions = (
            self.risk_engine.positions(
                symbol
            )
        )

        matching = None

        for position in positions:

            if position.type == (
                mt5.POSITION_TYPE_BUY
                if signal.direction == BUY
                else mt5.POSITION_TYPE_SELL
            ):
                matching = position
                break

        if matching:

            state = PositionState(
                ticket=int(
                    matching.ticket
                ),
                symbol=symbol,
                direction=signal.direction,
                entry=safe_float(
                    matching.price_open
                ),
                initial_sl=sl,
                initial_risk=abs(
                    safe_float(
                        matching.price_open
                    )
                    - sl
                ),
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                initial_volume=safe_float(
                    matching.volume
                ),
                created_at=now_utc().isoformat(),
            )

            self.state.add(
                state
            )

            self.state.set_meta(
                f"last_trade_{symbol}",
                now_utc().isoformat(),
            )

        return signal


# ============================================================
# EXPERT ADVISOR
# ============================================================

class ExpertAdvisor:
    def __init__(
        self,
        config: Config,
    ):
        self.config = config

        self.logger = AppLogger(
            config
        )

        self.csv = CSVLogger(
            config,
            self.logger,
        )

        self.telegram = TelegramNotifier(
            config,
            self.logger,
        )

        self.connection = MT5Connection(
            config,
            self.logger,
        )

        self.market_data = MarketData(
            self.logger
        )

        self.state = StateManager(
            config,
            self.logger,
        )

        self.execution = ExecutionEngine(
            config,
            self.logger,
            self.telegram,
            self.csv,
        )

        self.trading_engine = TradingEngine(
            config,
            self.logger,
            self.market_data,
            self.telegram,
            self.csv,
            self.state,
        )

        self.position_manager = PositionManager(
            config,
            self.logger,
            self.state,
            self.execution,
            self.market_data,
        )

        self.last_h1_bars: Dict[
            str,
            int,
        ] = {}

        self.last_daily_summary_date = None

    def symbols(self) -> List[str]:
        return (
            self.trading_engine.enabled_symbols()
        )

    def initialize_symbols(self) -> bool:

        valid = True

        for symbol in self.symbols():

            if not self.execution.validate_symbol(
                symbol
            ):
                valid = False
                continue

            info = mt5.symbol_info(
                symbol
            )

            self.logger.info(
                f"{symbol} | "
                f"digits={info.digits} | "
                f"point={info.point} | "
                f"tick_size={info.trade_tick_size} | "
                f"tick_value={info.trade_tick_value} | "
                f"volume_min={info.volume_min} | "
                f"volume_step={info.volume_step}"
            )

        return valid

    def maybe_daily_summary(self):

        today = now_utc().date()

        if (
            self.last_daily_summary_date
            == today
        ):
            return

        # Envoie uniquement après minuit
        # si le programme tourne encore.
        if now_utc().hour != 0:
            return

        self.last_daily_summary_date = today

        risk = self.trading_engine.risk_engine

        message = (
            f"<b>V7 DAILY</b>\n"
            f"Balance: {risk.balance():.2f}\n"
            f"Equity: {risk.equity():.2f}\n"
            f"PnL jour: "
            f"{risk.realized_pnl_today():.2f}\n"
            f"Risque ouvert: "
            f"{risk.open_risk_percent():.2f}%"
        )

        self.telegram.send(
            message,
            category="daily",
        )

    def run(self):

        self.logger.info(
            "=================================================="
        )

        self.logger.info(
            "BTC_XAU_PROFESSIONAL_V7 START"
        )

        self.logger.info(
            f"Version: "
            f"{self.config.get('ea', 'version', default='7.0.0')}"
        )

        self.logger.info(
            f"Test mode: "
            f"{self.config.get('ea', 'test_mode', default=True)}"
        )

        self.logger.info(
            f"Symbols: {self.symbols()}"
        )

        self.logger.info(
            "=================================================="
        )

        if not self.connection.connect():
            raise RuntimeError(
                "Connexion MT5 impossible."
            )

        if not self.initialize_symbols():
            self.logger.warning(
                "Au moins un symbole est invalide."
            )

        self.state.reconcile(
            self.trading_engine.risk_engine.positions(),
            self.config,
        )

        try:

            while True:

                if not self.connection.ensure_connection():
                    time.sleep(5)
                    continue

                # Gestion active des positions
                self.position_manager.manage_all()

                self.maybe_daily_summary()

                signal_tf = self.config.get(
                    "timeframes",
                    "signal",
                    default="H1",
                )

                timeframe = (
                    MarketData.TF_MAP[
                        signal_tf
                    ]
                )

                for symbol in self.symbols():

                    try:

                        rates = (
                            mt5.copy_rates_from_pos(
                                symbol,
                                timeframe,
                                0,
                                3,
                            )
                        )

                        if (
                            rates is None
                            or len(rates) < 2
                        ):
                            continue

                        # Bougie fermée.
                        closed_bar = rates[-2]

                        timestamp = int(
                            closed_bar["time"]
                        )

                        previous = (
                            self.last_h1_bars.get(
                                symbol
                            )
                        )

                        if previous == timestamp:
                            continue

                        self.last_h1_bars[
                            symbol
                        ] = timestamp

                        self.logger.info(
                            f"NEW {signal_tf} BAR | "
                            f"{symbol} | "
                            f"{datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()}"
                        )

                        self.trading_engine.process_symbol(
                            symbol,
                            timestamp,
                        )

                    except Exception as exc:
                        self.logger.exception(
                            f"Processing error "
                            f"{symbol}: {exc}"
                        )

                        self.telegram.send(
                            (
                                f"<b>V7 ERROR</b>\n"
                                f"{symbol}\n"
                                f"{str(exc)[:500]}"
                            ),
                            category="error",
                        )

                time.sleep(5)

        except KeyboardInterrupt:

            self.logger.info(
                "Arrêt demandé."
            )

        except Exception as exc:

            self.logger.exception(
                f"Fatal EA error: {exc}"
            )

            self.telegram.send(
                (
                    f"<b>V7 FATAL ERROR</b>\n"
                    f"{str(exc)[:700]}"
                ),
                category="error",
            )

        finally:

            self.connection.shutdown()

            self.logger.info(
                "V7 arrêté."
            )


# ============================================================
# MAIN
# ============================================================

def main():

    if not CONFIG_FILE.exists():
        print(
            f"ERREUR: config introuvable: "
            f"{CONFIG_FILE}"
        )
        sys.exit(1)

    try:

        config = Config(
            CONFIG_FILE
        )

        advisor = ExpertAdvisor(
            config
        )

        advisor.run()

    except Exception as exc:

        print(
            f"FATAL: {exc}"
        )

        raise


if __name__ == "__main__":
    main()