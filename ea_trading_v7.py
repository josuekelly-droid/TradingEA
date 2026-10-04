"""
====================================================================
EA TRADING V7.0 - PROFESSIONAL
BTCUSD / XAUUSD

Architecture:

H4
 └── Market Regime + Direction
          ↓
H1
 └── Setup + Structure + Momentum
          ↓
M15
 └── Entry Confirmation
          ↓
Quality Filter
          ↓
Risk Engine
          ↓
ONE POSITION
          ↓
TP1 → partial close
TP2 → partial close
TP3 → final exit
          ↓
Break-even
          ↓
ATR + Structure Trailing

IMPORTANT:
- Toutes les configurations sont dans config_v7.json
- Pas de secrets dans les variables d'environnement
- Bougies clôturées uniquement pour les signaux
- Position sizing basé sur MT5 order_calc_profit()
- Respect des volume_min / max / step
- Respect des stops_level / freeze_level
- Protection perte journalière
- Protection pertes consécutives
- Protection exposition
- Cooldowns
- Signal fingerprint
- State persistence
- News lock MT5
- Telegram
- CSV logs
- Test mode
====================================================================
"""

import os
import sys
import csv
import json
import time
import math
import logging
import traceback

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import MetaTrader5 as mt5
import requests


# =================================================================
# PATHS / CONFIG
# =================================================================

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config_v7.json"


def load_config() -> Dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Configuration introuvable: {CONFIG_FILE}"
        )

    with open(CONFIG_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


CONFIG = load_config()


# =================================================================
# DIRECTORIES
# =================================================================

LOG_CONFIG = CONFIG.get("logging", {})

for key in [
    "trade_log",
    "signal_log",
    "error_log"
]:
    value = LOG_CONFIG.get(key)

    if value:
        path = BASE_DIR / value
        path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

STATE_CONFIG = CONFIG.get("state", {})

STATE_FILE = (
    BASE_DIR /
    STATE_CONFIG.get(
        "file",
        "trade_state_v7.json"
    )
)


# =================================================================
# LOGGING
# =================================================================

ERROR_LOG = (
    BASE_DIR /
    LOG_CONFIG.get(
        "error_log",
        "logs/errors_v7.log"
    )
)

ERROR_LOG.parent.mkdir(
    parents=True,
    exist_ok=True
)

logging.basicConfig(
    level=getattr(
        logging,
        CONFIG.get(
            "ea",
            {}
        ).get(
            "log_level",
            "INFO"
        ).upper(),
        logging.INFO
    ),
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(
            ERROR_LOG,
            encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("EA_V7")


# =================================================================
# CONSTANTS
# =================================================================

BUY = "BUY"
SELL = "SELL"
NEUTRAL = "NEUTRAL"


EA_CONFIG = CONFIG["ea"]

MAGIC_NUMBER = int(
    EA_CONFIG["magic_number"]
)

TEST_MODE = bool(
    EA_CONFIG.get(
        "test_mode",
        True
    )
)


# =================================================================
# DATA CLASSES
# =================================================================

@dataclass
class TradeSetup:

    symbol: str
    direction: str

    entry_price: float

    sl_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float

    total_volume: float

    risk_percent: float

    score: float
    quality: float

    regime: str

    h4_direction: str
    h1_direction: str
    m15_direction: str

    atr: float
    adx: float
    atr_percentile: float

    session: str

    signal_time: str


@dataclass
class PositionState:

    ticket: int

    symbol: str

    direction: str

    entry_price: float

    initial_sl: float

    initial_risk_distance: float

    tp1_price: float
    tp2_price: float
    tp3_price: float

    initial_volume: float

    tp1_hit: bool = False
    tp2_hit: bool = False

    partial_management: bool = True

    created_at: str = ""


# =================================================================
# STATE STORE
# =================================================================

class StateStore:

    def __init__(
        self,
        path: Path
    ):

        self.path = path

        self.data = {
            "positions": {},
            "last_signals": {},
            "risk": {}
        }

        self.load()

    # -------------------------------------------------------------

    def load(self):

        if not self.path.exists():
            return

        try:

            with open(
                self.path,
                "r",
                encoding="utf-8"
            ) as file:

                loaded = json.load(file)

            if isinstance(loaded, dict):
                self.data.update(loaded)

        except Exception as error:

            logger.warning(
                f"[STATE] Impossible de charger "
                f"{self.path}: {error}"
            )

    # -------------------------------------------------------------

    def save(self):

        try:

            self.path.parent.mkdir(
                parents=True,
                exist_ok=True
            )

            temporary = self.path.with_suffix(
                ".tmp"
            )

            with open(
                temporary,
                "w",
                encoding="utf-8"
            ) as file:

                json.dump(
                    self.data,
                    file,
                    indent=2,
                    ensure_ascii=False
                )

            os.replace(
                temporary,
                self.path
            )

        except Exception as error:

            logger.error(
                f"[STATE] Sauvegarde impossible: "
                f"{error}"
            )

    # -------------------------------------------------------------

    def get_position(
        self,
        ticket: int
    ) -> Optional[PositionState]:

        raw = self.data.get(
            "positions",
            {}
        ).get(
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

    # -------------------------------------------------------------

    def set_position(
        self,
        state: PositionState
    ):

        self.data.setdefault(
            "positions",
            {}
        )[str(state.ticket)] = asdict(
            state
        )

        self.save()

    # -------------------------------------------------------------

    def remove_position(
        self,
        ticket: int
    ):

        self.data.setdefault(
            "positions",
            {}
        ).pop(
            str(ticket),
            None
        )

        self.save()

    # -------------------------------------------------------------

    def get_last_signal(
        self,
        symbol: str
    ) -> Optional[str]:

        return self.data.get(
            "last_signals",
            {}
        ).get(symbol)

    # -------------------------------------------------------------

    def set_last_signal(
        self,
        symbol: str,
        fingerprint: str
    ):

        self.data.setdefault(
            "last_signals",
            {}
        )[symbol] = fingerprint

        self.save()

    # -------------------------------------------------------------

    def get_risk_state(self) -> Dict:

        return self.data.setdefault(
            "risk",
            {}
        )

    # -------------------------------------------------------------

    def set_risk_state(
        self,
        data: Dict
    ):

        self.data["risk"] = data
        self.save()


# =================================================================
# MT5 CONNECTION
# =================================================================

class MT5Connection:

    def __init__(
        self,
        config: Dict
    ):

        self.config = config["mt5"]

    # -------------------------------------------------------------

    def initialize(self) -> bool:

        login = self.config.get(
            "login"
        )

        password = self.config.get(
            "password"
        )

        server = self.config.get(
            "server"
        )

        path = self.config.get(
            "path",
            ""
        )

        timeout = int(
            self.config.get(
                "timeout",
                60000
            )
        )

        if not login:
            logger.error(
                "MT5 login absent."
            )
            return False

        if not password:
            logger.error(
                "MT5 password absente."
            )
            return False

        if not server:
            logger.error(
                "MT5 server absent."
            )
            return False

        try:

            if path:

                initialized = mt5.initialize(
                    path=path,
                    timeout=timeout
                )

            else:

                initialized = mt5.initialize(
                    timeout=timeout
                )

            if not initialized:

                logger.error(
                    f"mt5.initialize() échoué: "
                    f"{mt5.last_error()}"
                )

                return False

            connected = mt5.login(
                int(login),
                password=str(password),
                server=str(server)
            )

            if not connected:

                logger.error(
                    f"mt5.login() échoué: "
                    f"{mt5.last_error()}"
                )

                mt5.shutdown()

                return False

            account = mt5.account_info()

            if account is None:

                logger.error(
                    "account_info() indisponible."
                )

                mt5.shutdown()

                return False

            logger.info(
                "=================================================="
            )

            logger.info(
                "MT5 CONNECTÉ"
            )

            logger.info(
                f"Login   : {account.login}"
            )

            logger.info(
                f"Server  : {account.server}"
            )

            logger.info(
                f"Balance : {account.balance:.2f}"
            )

            logger.info(
                f"Equity  : {account.equity:.2f}"
            )

            logger.info(
                f"Leverage: 1:{account.leverage}"
            )

            logger.info(
                "=================================================="
            )

            return True

        except Exception as error:

            logger.error(
                f"Erreur connexion MT5: {error}"
            )

            return False

    # -------------------------------------------------------------

    def check(self) -> bool:

        terminal = mt5.terminal_info()
        account = mt5.account_info()

        if terminal is None or account is None:

            logger.warning(
                "[MT5] Connexion perdue. "
                "Tentative de reconnexion..."
            )

            mt5.shutdown()

            time.sleep(3)

            return self.initialize()

        if hasattr(
            terminal,
            "trade_allowed"
        ):

            if not terminal.trade_allowed:

                logger.warning(
                    "[MT5] Trading terminal désactivé."
                )

        return True


# =================================================================
# MARKET DATA
# =================================================================

class MarketData:

    TF_MAP = {
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4
    }

    def __init__(self):

        self.cache = {}

    # -------------------------------------------------------------

    def ensure_symbol(
        self,
        symbol: str
    ) -> bool:

        info = mt5.symbol_info(
            symbol
        )

        if info is None:

            logger.error(
                f"[SYMBOL] {symbol} "
                "introuvable."
            )

            return False

        if not info.visible:

            if not mt5.symbol_select(
                symbol,
                True
            ):

                logger.error(
                    f"[SYMBOL] Impossible "
                    f"d'activer {symbol}."
                )

                return False

        return True

    # -------------------------------------------------------------

    def fetch(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 500,
        cache_seconds: int = 10
    ) -> pd.DataFrame:

        if timeframe not in self.TF_MAP:

            raise ValueError(
                f"Timeframe non supporté: "
                f"{timeframe}"
            )

        if not self.ensure_symbol(symbol):

            return pd.DataFrame()

        cache_key = (
            symbol,
            timeframe
        )

        now = time.time()

        cached = self.cache.get(
            cache_key
        )

        if cached:

            cached_time, cached_df = cached

            if (
                now - cached_time
                <= cache_seconds
            ):

                return cached_df.copy()

        rates = mt5.copy_rates_from_pos(
            symbol,
            self.TF_MAP[timeframe],
            0,
            bars + 2
        )

        if rates is None:

            logger.warning(
                f"[DATA] {symbol} {timeframe}: "
                f"{mt5.last_error()}"
            )

            return pd.DataFrame()

        if len(rates) < 100:

            logger.warning(
                f"[DATA] {symbol} {timeframe}: "
                f"données insuffisantes "
                f"({len(rates)})."
            )

            return pd.DataFrame()

        df = pd.DataFrame(
            rates
        )

        df["time"] = pd.to_datetime(
            df["time"],
            unit="s",
            utc=True
        )

        df.set_index(
            "time",
            inplace=True
        )

        # ---------------------------------------------------------
        # IMPORTANT
        #
        # La dernière ligne est la bougie en formation.
        # Elle est exclue de toutes les décisions.
        # ---------------------------------------------------------

        df = df.iloc[:-1].copy()

        self.cache[cache_key] = (
            now,
            df.copy()
        )

        return df

    # -------------------------------------------------------------

    @staticmethod
    def latest_closed_timestamp(
        symbol: str,
        timeframe
    ) -> Optional[int]:

        rates = mt5.copy_rates_from_pos(
            symbol,
            timeframe,
            0,
            3
        )

        if rates is None or len(rates) < 2:
            return None

        return int(
            rates[-2]["time"]
        )


# =================================================================
# INDICATORS
# =================================================================

class Indicators:

    @staticmethod
    def atr(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> pd.Series:

        previous_close = close.shift(1)

        tr = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs()
            ],
            axis=1
        ).max(axis=1)

        # Wilder-style smoothing
        return tr.ewm(
            alpha=1 / period,
            adjust=False
        ).mean()

    # -------------------------------------------------------------

    @staticmethod
    def ema(
        close: pd.Series,
        period: int
    ) -> pd.Series:

        return close.ewm(
            span=period,
            adjust=False
        ).mean()

    # -------------------------------------------------------------

    @staticmethod
    def rsi(
        close: pd.Series,
        period: int = 14
    ) -> pd.Series:

        delta = close.diff()

        gain = delta.clip(
            lower=0
        )

        loss = -delta.clip(
            upper=0
        )

        avg_gain = gain.ewm(
            alpha=1 / period,
            adjust=False
        ).mean()

        avg_loss = loss.ewm(
            alpha=1 / period,
            adjust=False
        ).mean()

        rs = (
            avg_gain /
            avg_loss.replace(
                0,
                np.nan
            )
        )

        rsi = 100 - (
            100 /
            (1 + rs)
        )

        rsi = rsi.fillna(
            100
        )

        return rsi

    # -------------------------------------------------------------

    @staticmethod
    def macd(
        close: pd.Series,
        fast: int,
        slow: int,
        signal: int
    ) -> Dict:

        fast_ema = Indicators.ema(
            close,
            fast
        )

        slow_ema = Indicators.ema(
            close,
            slow
        )

        line = fast_ema - slow_ema

        signal_line = Indicators.ema(
            line,
            signal
        )

        histogram = (
            line -
            signal_line
        )

        return {
            "line": line,
            "signal": signal_line,
            "histogram": histogram
        }

    # -------------------------------------------------------------

    @staticmethod
    def bollinger(
        close: pd.Series,
        period: int,
        std_dev: float
    ) -> Dict:

        middle = close.rolling(
            period
        ).mean()

        std = close.rolling(
            period
        ).std()

        return {
            "middle": middle,
            "upper":
                middle + std * std_dev,
            "lower":
                middle - std * std_dev
        }

    # -------------------------------------------------------------

    @staticmethod
    def adx(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> pd.Series:

        up_move = high.diff()

        down_move = -low.diff()

        plus_dm = pd.Series(
            np.where(
                (
                    up_move > down_move
                ) &
                (
                    up_move > 0
                ),
                up_move,
                0.0
            ),
            index=high.index
        )

        minus_dm = pd.Series(
            np.where(
                (
                    down_move > up_move
                ) &
                (
                    down_move > 0
                ),
                down_move,
                0.0
            ),
            index=high.index
        )

        previous_close = close.shift(1)

        tr = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs()
            ],
            axis=1
        ).max(axis=1)

        atr = tr.ewm(
            alpha=1 / period,
            adjust=False
        ).mean()

        plus_smoothed = plus_dm.ewm(
            alpha=1 / period,
            adjust=False
        ).mean()

        minus_smoothed = minus_dm.ewm(
            alpha=1 / period,
            adjust=False
        ).mean()

        plus_di = (
            100 *
            plus_smoothed /
            atr.replace(
                0,
                np.nan
            )
        )

        minus_di = (
            100 *
            minus_smoothed /
            atr.replace(
                0,
                np.nan
            )
        )

        denominator = (
            plus_di +
            minus_di
        ).replace(
            0,
            np.nan
        )

        dx = (
            100 *
            (
                plus_di -
                minus_di
            ).abs() /
            denominator
        )

        return dx.ewm(
            alpha=1 / period,
            adjust=False
        ).mean()

    # -------------------------------------------------------------

    @staticmethod
    def supertrend(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int,
        multiplier: float
    ) -> pd.DataFrame:

        atr = Indicators.atr(
            high,
            low,
            close,
            period
        )

        hl2 = (
            high + low
        ) / 2

        basic_upper = (
            hl2 +
            multiplier * atr
        )

        basic_lower = (
            hl2 -
            multiplier * atr
        )

        final_upper = basic_upper.copy()
        final_lower = basic_lower.copy()

        direction = pd.Series(
            1,
            index=close.index,
            dtype=int
        )

        for i in range(
            1,
            len(close)
        ):

            if (
                basic_upper.iloc[i]
                < final_upper.iloc[i - 1]
                or
                close.iloc[i - 1]
                > final_upper.iloc[i - 1]
            ):

                final_upper.iloc[i] = (
                    basic_upper.iloc[i]
                )

            else:

                final_upper.iloc[i] = (
                    final_upper.iloc[i - 1]
                )

            if (
                basic_lower.iloc[i]
                > final_lower.iloc[i - 1]
                or
                close.iloc[i - 1]
                < final_lower.iloc[i - 1]
            ):

                final_lower.iloc[i] = (
                    basic_lower.iloc[i]
                )

            else:

                final_lower.iloc[i] = (
                    final_lower.iloc[i - 1]
                )

            previous_direction = (
                direction.iloc[i - 1]
            )

            if previous_direction == 1:

                if (
                    close.iloc[i]
                    < final_lower.iloc[i]
                ):

                    direction.iloc[i] = -1

                else:

                    direction.iloc[i] = 1

            else:

                if (
                    close.iloc[i]
                    > final_upper.iloc[i]
                ):

                    direction.iloc[i] = 1

                else:

                    direction.iloc[i] = -1

        supertrend = pd.Series(
            np.where(
                direction == 1,
                final_lower,
                final_upper
            ),
            index=close.index
        )

        return pd.DataFrame(
            {
                "supertrend":
                    supertrend,
                "direction":
                    direction
            },
            index=close.index
        )

    # -------------------------------------------------------------

    @staticmethod
    def ichimoku(
        high: pd.Series,
        low: pd.Series,
        tenkan_period: int = 9,
        kijun_period: int = 26,
        senkou_b_period: int = 52
    ) -> Dict:

        tenkan = (
            high.rolling(
                tenkan_period
            ).max()
            +
            low.rolling(
                tenkan_period
            ).min()
        ) / 2

        kijun = (
            high.rolling(
                kijun_period
            ).max()
            +
            low.rolling(
                kijun_period
            ).min()
        ) / 2

        span_a = (
            (
                tenkan +
                kijun
            ) / 2
        ).shift(
            kijun_period
        )

        span_b = (
            (
                high.rolling(
                    senkou_b_period
                ).max()
                +
                low.rolling(
                    senkou_b_period
                ).min()
            ) / 2
        ).shift(
            kijun_period
        )

        return {
            "tenkan": tenkan,
            "kijun": kijun,
            "span_a": span_a,
            "span_b": span_b
        }


# =================================================================
# REGIME ENGINE
# =================================================================

class RegimeEngine:

    def __init__(
        self,
        config: Dict
    ):

        self.config = config[
            "market_regime"
        ]

    # -------------------------------------------------------------

    def analyze(
        self,
        df: pd.DataFrame
    ) -> Dict:

        cfg = self.config

        close = df["close"]

        ema_fast = Indicators.ema(
            close,
            cfg["ema_fast"]
        )

        ema_slow = Indicators.ema(
            close,
            cfg["ema_slow"]
        )

        ema_trend = Indicators.ema(
            close,
            200
        )

        atr = Indicators.atr(
            df["high"],
            df["low"],
            close,
            cfg["atr_period"]
        )

        adx = Indicators.adx(
            df["high"],
            df["low"],
            close,
            cfg["adx_period"]
        )

        bb = Indicators.bollinger(
            close,
            cfg["bb_period"],
            cfg["bb_std"]
        )

        price = float(
            close.iloc[-1]
        )

        ef = float(
            ema_fast.iloc[-1]
        )

        es = float(
            ema_slow.iloc[-1]
        )

        et = float(
            ema_trend.iloc[-1]
        )

        atr_value = float(
            atr.iloc[-1]
        )

        adx_value = float(
            adx.iloc[-1]
        )

        upper = float(
            bb["upper"].iloc[-1]
        )

        lower = float(
            bb["lower"].iloc[-1]
        )

        bb_width = 0.0

        if price > 0:

            bb_width = (
                upper - lower
            ) / price

        lookback = int(
            cfg[
                "atr_percentile_period"
            ]
        )

        atr_history = (
            atr.dropna()
            .tail(lookback)
        )

        if len(atr_history) >= 20:

            rank = (
                atr_history.rank(
                    pct=True
                ).iloc[-1]
            )

            atr_percentile = float(
                rank
            )

        else:

            atr_percentile = 0.50

        bullish = (
            price > et
            and ef > es
            and es > et
        )

        bearish = (
            price < et
            and ef < es
            and es < et
        )

        trend = (
            adx_value >=
            cfg[
                "adx_trend_threshold"
            ]
        )

        strong_trend = (
            adx_value >=
            cfg[
                "adx_strong_threshold"
            ]
        )

        low_threshold = (
            cfg[
                "low_volatility_percentile"
            ] / 100
        )

        high_threshold = (
            cfg[
                "high_volatility_percentile"
            ] / 100
        )

        if bullish and trend:

            regime = (
                "STRONG_TREND_UP"
                if strong_trend
                else "TREND_UP"
            )

        elif bearish and trend:

            regime = (
                "STRONG_TREND_DOWN"
                if strong_trend
                else "TREND_DOWN"
            )

        elif (
            atr_percentile
            <= low_threshold
        ):

            regime = "LOW_VOLATILITY"

        elif (
            atr_percentile
            >= high_threshold
        ):

            regime = "HIGH_VOLATILITY"

        else:

            regime = "RANGE"

        # Breakout context
        if (
            regime == "RANGE"
            and atr_percentile
            >= high_threshold
        ):

            regime = "BREAKOUT"

        direction = NEUTRAL

        if bullish:
            direction = BUY

        elif bearish:
            direction = SELL

        return {
            "regime": regime,
            "direction": direction,
            "adx": adx_value,
            "atr": atr_value,
            "atr_percentile": atr_percentile,
            "bb_width": float(bb_width),
            "ema_fast": ef,
            "ema_slow": es,
            "ema_trend": et
        }


# =================================================================
# SESSION ENGINE
# =================================================================

class SessionEngine:

    def __init__(
        self,
        config: Dict
    ):

        self.config = config[
            "sessions"
        ]

    # -------------------------------------------------------------

    @staticmethod
    def _time_to_minutes(
        value: str
    ) -> int:

        hour, minute = map(
            int,
            value.split(":")
        )

        return (
            hour * 60 +
            minute
        )

    # -------------------------------------------------------------

    def current_session(
        self
    ) -> str:

        timezone_name = self.config.get(
            "timezone",
            "UTC"
        )

        now = datetime.now(
            timezone.utc
        )

        # Configuration actuelle = UTC.
        # On conserve cette implémentation
        # volontairement déterministe.
        if timezone_name != "UTC":

            try:

                from zoneinfo import ZoneInfo

                now = datetime.now(
                    ZoneInfo(
                        timezone_name
                    )
                )

            except Exception:

                logger.warning(
                    f"[SESSION] Timezone "
                    f"{timezone_name} invalide. "
                    "UTC utilisé."
                )

        minutes = (
            now.hour * 60 +
            now.minute
        )

        overlap = self.config[
            "overlap"
        ]

        london = self.config[
            "london"
        ]

        new_york = self.config[
            "new_york"
        ]

        asian = self.config[
            "asian"
        ]

        if (
            self._time_to_minutes(
                overlap["start"]
            )
            <= minutes
            <
            self._time_to_minutes(
                overlap["end"]
            )
        ):

            return "overlap"

        if (
            self._time_to_minutes(
                london["start"]
            )
            <= minutes
            <
            self._time_to_minutes(
                london["end"]
            )
        ):

            return "london"

        if (
            self._time_to_minutes(
                new_york["start"]
            )
            <= minutes
            <
            self._time_to_minutes(
                new_york["end"]
            )
        ):

            return "new_york"

        if (
            self._time_to_minutes(
                asian["start"]
            )
            <= minutes
            <
            self._time_to_minutes(
                asian["end"]
            )
        ):

            return "asian"

        return "closed"

    # -------------------------------------------------------------

    def allowed(self) -> bool:

        session = self.current_session()

        if session == "asian":

            return bool(
                self.config[
                    "asian"
                ]["enabled"]
            )

        if session == "london":

            return bool(
                self.config[
                    "london"
                ]["enabled"]
            )

        if session == "overlap":

            return bool(
                self.config[
                    "overlap"
                ]["enabled"]
            )

        if session == "new_york":

            return bool(
                self.config[
                    "new_york"
                ]["enabled"]
            )

        return bool(
            self.config[
                "outside_sessions"
            ]["allow_trading"]
        )


# =================================================================
# NEWS FILTER
# =================================================================

class NewsFilter:

    def __init__(
        self,
        config: Dict
    ):

        self.config = config[
            "news_filter"
        ]

        self.enabled = bool(
            self.config.get(
                "enabled",
                False
            )
        )

        self.lock_file = self.config.get(
            "lock_file",
            "news_lock.json"
        )

        self.blackout_active = False

        self.cooldown_until = None

    # -------------------------------------------------------------

    def _find_lock_file(
        self
    ) -> Optional[Path]:

        candidates = [
            BASE_DIR /
            self.lock_file
        ]

        terminal = mt5.terminal_info()

        if terminal:

            candidates.append(
                Path(
                    terminal.data_path
                )
                /
                "MQL5"
                /
                "Files"
                /
                self.lock_file
            )

        for candidate in candidates:

            if candidate.exists():
                return candidate

        return None

    # -------------------------------------------------------------

    def _parse_until(
        self,
        value
    ) -> Optional[datetime]:

        if value is None:
            return None

        try:

            if isinstance(
                value,
                (int, float)
            ):

                return datetime.fromtimestamp(
                    float(value),
                    tz=timezone.utc
                )

            if isinstance(
                value,
                str
            ):

                parsed = datetime.fromisoformat(
                    value.replace(
                        "Z",
                        "+00:00"
                    )
                )

                if parsed.tzinfo is None:

                    parsed = parsed.replace(
                        tzinfo=timezone.utc
                    )

                return parsed.astimezone(
                    timezone.utc
                )

        except Exception:
            pass

        return None

    # -------------------------------------------------------------

    def blocked(self) -> bool:

        if not self.enabled:
            return False

        now = datetime.now(
            timezone.utc
        )

        path = self._find_lock_file()

        active = False

        explicit_until = None

        if path:

            try:

                # Si le fichier est très ancien,
                # on considère le lock inactif.
                age = (
                    time.time()
                    -
                    path.stat().st_mtime
                )

                if age <= 120:

                    with open(
                        path,
                        "r",
                        encoding="utf-8"
                    ) as file:

                        data = json.load(
                            file
                        )

                    active = bool(
                        data.get(
                            "blackout",
                            False
                        )
                    )

                    explicit_until = (
                        self._parse_until(
                            data.get(
                                "blackout_until"
                            )
                        )
                        or
                        self._parse_until(
                            data.get(
                                "blackout_end"
                            )
                        )
                    )

            except Exception as error:

                logger.warning(
                    f"[NEWS] Erreur lecture "
                    f"lock file: {error}"
                )

        if explicit_until:

            if now < explicit_until:

                self.blackout_active = True

                return True

        if active:

            self.blackout_active = True

            return True

        # Transition blackout -> normal
        if self.blackout_active:

            cooldown_minutes = int(
                self.config.get(
                    "post_news_cooldown_minutes",
                    120
                )
            )

            self.cooldown_until = (
                now +
                timedelta(
                    minutes=cooldown_minutes
                )
            )

            self.blackout_active = False

            logger.info(
                "[NEWS] Fin blackout. "
                f"Cooldown {cooldown_minutes} min."
            )

        if (
            self.cooldown_until
            and now <
            self.cooldown_until
        ):

            return True

        self.cooldown_until = None

        return False


# =================================================================
# SIGNAL ENGINE
# =================================================================

class SignalEngine:

    def __init__(
        self,
        config: Dict
    ):

        self.config = config

        self.indicators = config[
            "indicators"
        ]

        self.weights = config[
            "signal_engine"
        ]["weights"]

    # -------------------------------------------------------------

    def h4_direction(
        self,
        df: pd.DataFrame
    ) -> str:

        st_cfg = self.indicators[
            "supertrend"
        ]

        st = Indicators.supertrend(
            df["high"],
            df["low"],
            df["close"],
            st_cfg["period"],
            st_cfg["multiplier"]
        )

        ich_cfg = self.indicators[
            "ichimoku"
        ]

        ich = Indicators.ichimoku(
            df["high"],
            df["low"],
            ich_cfg["tenkan"],
            ich_cfg["kijun"],
            ich_cfg["senkou_b"]
        )

        ema_cfg = self.indicators[
            "ema"
        ]

        ema_fast = Indicators.ema(
            df["close"],
            ema_cfg["fast"]
        )

        ema_slow = Indicators.ema(
            df["close"],
            ema_cfg["slow"]
        )

        ema_trend = Indicators.ema(
            df["close"],
            ema_cfg["trend"]
        )

        close = float(
            df["close"].iloc[-1]
        )

        span_a = ich[
            "span_a"
        ].iloc[-1]

        span_b = ich[
            "span_b"
        ].iloc[-1]

        if (
            pd.isna(span_a)
            or pd.isna(span_b)
        ):

            return NEUTRAL

        cloud_top = max(
            float(span_a),
            float(span_b)
        )

        cloud_bottom = min(
            float(span_a),
            float(span_b)
        )

        bullish_points = 0
        bearish_points = 0

        if (
            st["direction"].iloc[-1]
            == 1
        ):

            bullish_points += 1

        else:

            bearish_points += 1

        if close > cloud_top:

            bullish_points += 1

        elif close < cloud_bottom:

            bearish_points += 1

        if (
            ema_fast.iloc[-1]
            >
            ema_slow.iloc[-1]
            >
            ema_trend.iloc[-1]
        ):

            bullish_points += 1

        elif (
            ema_fast.iloc[-1]
            <
            ema_slow.iloc[-1]
            <
            ema_trend.iloc[-1]
        ):

            bearish_points += 1

        if bullish_points >= 3:

            return BUY

        if bearish_points >= 3:

            return SELL

        return NEUTRAL

    # -------------------------------------------------------------

    def analyze_h1(
        self,
        df: pd.DataFrame
    ) -> Dict:

        st_cfg = self.indicators[
            "supertrend"
        ]

        st = Indicators.supertrend(
            df["high"],
            df["low"],
            df["close"],
            st_cfg["period"],
            st_cfg["multiplier"]
        )

        rsi_cfg = self.indicators[
            "rsi"
        ]

        rsi = Indicators.rsi(
            df["close"],
            rsi_cfg["period"]
        )

        macd_cfg = self.indicators[
            "macd"
        ]

        macd = Indicators.macd(
            df["close"],
            macd_cfg["fast"],
            macd_cfg["slow"],
            macd_cfg["signal"]
        )

        ich_cfg = self.indicators[
            "ichimoku"
        ]

        ich = Indicators.ichimoku(
            df["high"],
            df["low"],
            ich_cfg["tenkan"],
            ich_cfg["kijun"],
            ich_cfg["senkou_b"]
        )

        ema_cfg = self.indicators[
            "ema"
        ]

        ema_fast = Indicators.ema(
            df["close"],
            ema_cfg["fast"]
        )

        ema_slow = Indicators.ema(
            df["close"],
            ema_cfg["slow"]
        )

        close = float(
            df["close"].iloc[-1]
        )

        score = 0.0

        reasons = []

        # ---------------------------------------------------------
        # H1 TREND
        # ---------------------------------------------------------

        if (
            ema_fast.iloc[-1]
            >
            ema_slow.iloc[-1]
        ):

            score += self.weights[
                "h1_trend"
            ]

            reasons.append(
                "H1_TREND_UP"
            )

        elif (
            ema_fast.iloc[-1]
            <
            ema_slow.iloc[-1]
        ):

            score -= self.weights[
                "h1_trend"
            ]

            reasons.append(
                "H1_TREND_DOWN"
            )

        # ---------------------------------------------------------
        # SUPERTREND
        # ---------------------------------------------------------

        if (
            st["direction"].iloc[-1]
            == 1
        ):

            score += self.weights[
                "supertrend"
            ]

            reasons.append(
                "SUPERTREND_UP"
            )

        else:

            score -= self.weights[
                "supertrend"
            ]

            reasons.append(
                "SUPERTREND_DOWN"
            )

        # ---------------------------------------------------------
        # ICHIMOKU
        # ---------------------------------------------------------

        tenkan = ich[
            "tenkan"
        ].iloc[-1]

        kijun = ich[
            "kijun"
        ].iloc[-1]

        span_a = ich[
            "span_a"
        ].iloc[-1]

        span_b = ich[
            "span_b"
        ].iloc[-1]

        ich_direction = NEUTRAL

        if not any(
            pd.isna(value)
            for value in [
                tenkan,
                kijun,
                span_a,
                span_b
            ]
        ):

            cloud_top = max(
                span_a,
                span_b
            )

            cloud_bottom = min(
                span_a,
                span_b
            )

            if (
                tenkan > kijun
                and close > cloud_top
            ):

                score += self.weights[
                    "ichimoku"
                ]

                ich_direction = BUY

                reasons.append(
                    "ICHIMOKU_UP"
                )

            elif (
                tenkan < kijun
                and close < cloud_bottom
            ):

                score -= self.weights[
                    "ichimoku"
                ]

                ich_direction = SELL

                reasons.append(
                    "ICHIMOKU_DOWN"
                )

        # ---------------------------------------------------------
        # RSI
        # ---------------------------------------------------------

        rsi_value = float(
            rsi.iloc[-1]
        )

        if rsi_value >= 50:

            score += self.weights[
                "rsi"
            ]

            reasons.append(
                "RSI_UP"
            )

        else:

            score -= self.weights[
                "rsi"
            ]

            reasons.append(
                "RSI_DOWN"
            )

        # ---------------------------------------------------------
        # MACD
        # ---------------------------------------------------------

        macd_hist = float(
            macd[
                "histogram"
            ].iloc[-1]
        )

        if macd_hist > 0:

            score += self.weights[
                "macd"
            ]

            reasons.append(
                "MACD_UP"
            )

        elif macd_hist < 0:

            score -= self.weights[
                "macd"
            ]

            reasons.append(
                "MACD_DOWN"
            )

        # ---------------------------------------------------------
        # DIRECTION
        # ---------------------------------------------------------

        direction = NEUTRAL

        if score > 0:

            direction = BUY

        elif score < 0:

            direction = SELL

        return {
            "direction": direction,
            "score": float(score),
            "rsi": rsi_value,
            "macd_hist": macd_hist,
            "ichimoku_direction":
                ich_direction,
            "reasons": reasons
        }

    # -------------------------------------------------------------

    def analyze_m15(
        self,
        df: pd.DataFrame,
        expected_direction: str
    ) -> Dict:

        ema_cfg = self.indicators[
            "ema"
        ]

        rsi_cfg = self.indicators[
            "rsi"
        ]

        macd_cfg = self.indicators[
            "macd"
        ]

        close = df["close"]

        ema_fast = Indicators.ema(
            close,
            ema_cfg["fast"]
        )

        ema_slow = Indicators.ema(
            close,
            ema_cfg["slow"]
        )

        rsi = Indicators.rsi(
            close,
            rsi_cfg["period"]
        )

        macd = Indicators.macd(
            close,
            macd_cfg["fast"],
            macd_cfg["slow"],
            macd_cfg["signal"]
        )

        last = -1
        previous = -2

        score = 0

        reasons = []

        if expected_direction == BUY:

            if (
                close.iloc[last]
                >
                ema_fast.iloc[last]
            ):

                score += 1
                reasons.append(
                    "PRICE_ABOVE_EMA"
                )

            if (
                ema_fast.iloc[last]
                >
                ema_slow.iloc[last]
            ):

                score += 1
                reasons.append(
                    "EMA_BULLISH"
                )

            if (
                rsi.iloc[last]
                > 50
            ):

                score += 1
                reasons.append(
                    "RSI_BULLISH"
                )

            if (
                macd["histogram"].iloc[last]
                > 0
            ):

                score += 1
                reasons.append(
                    "MACD_BULLISH"
                )

            if (
                close.iloc[last]
                >
                close.iloc[previous]
            ):

                score += 1
                reasons.append(
                    "MOMENTUM_BULLISH"
                )

            direction = (
                BUY
                if score >= 3
                else NEUTRAL
            )

        elif expected_direction == SELL:

            if (
                close.iloc[last]
                <
                ema_fast.iloc[last]
            ):

                score += 1
                reasons.append(
                    "PRICE_BELOW_EMA"
                )

            if (
                ema_fast.iloc[last]
                <
                ema_slow.iloc[last]
            ):

                score += 1
                reasons.append(
                    "EMA_BEARISH"
                )

            if (
                rsi.iloc[last]
                < 50
            ):

                score += 1
                reasons.append(
                    "RSI_BEARISH"
                )

            if (
                macd["histogram"].iloc[last]
                < 0
            ):

                score += 1
                reasons.append(
                    "MACD_BEARISH"
                )

            if (
                close.iloc[last]
                <
                close.iloc[previous]
            ):

                score += 1
                reasons.append(
                    "MOMENTUM_BEARISH"
                )

            direction = (
                SELL
                if score >= 3
                else NEUTRAL
            )

        else:

            direction = NEUTRAL

        return {
            "direction": direction,
            "score": score,
            "rsi": float(
                rsi.iloc[last]
            ),
            "reasons": reasons
        }


# =================================================================
# RISK ENGINE
# =================================================================

class RiskEngine:

    def __init__(
        self,
        config: Dict,
        state: StateStore
    ):

        self.config = config[
            "risk_management"
        ]

        self.state = state

    # -------------------------------------------------------------

    def account(
        self
    ):

        return mt5.account_info()

    # -------------------------------------------------------------

    def balance(self) -> float:

        account = self.account()

        if account is None:
            return 0.0

        return float(
            account.balance
        )

    # -------------------------------------------------------------

    def equity(self) -> float:

        account = self.account()

        if account is None:
            return 0.0

        return float(
            account.equity
        )

    # -------------------------------------------------------------

    def positions(
        self
    ) -> List:

        all_positions = mt5.positions_get()

        if not all_positions:
            return []

        return [
            position
            for position in all_positions
            if (
                int(
                    getattr(
                        position,
                        "magic",
                        0
                    )
                )
                == MAGIC_NUMBER
            )
        ]

    # -------------------------------------------------------------

    def floating_pnl(self) -> float:

        total = 0.0

        for position in self.positions():

            total += float(
                getattr(
                    position,
                    "profit",
                    0.0
                )
            )

            total += float(
                getattr(
                    position,
                    "swap",
                    0.0
                )
            )

        return total

    # -------------------------------------------------------------

    def _today_start(
        self
    ) -> datetime:

        return datetime.utcnow().replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

    # -------------------------------------------------------------

    def _history_deals(
        self,
        days: int = 60
    ):

        now = datetime.utcnow()

        start = (
            now -
            timedelta(
                days=days
            )
        )

        deals = mt5.history_deals_get(
            start,
            now
        )

        if not deals:
            return []

        return [
            deal
            for deal in deals
            if int(
                getattr(
                    deal,
                    "magic",
                    0
                )
            ) == MAGIC_NUMBER
        ]

    # -------------------------------------------------------------

    def daily_realized_pnl(
        self
    ) -> float:

        deals = mt5.history_deals_get(
            self._today_start(),
            datetime.utcnow()
        )

        if not deals:
            return 0.0

        pnl = 0.0

        for deal in deals:

            if int(
                getattr(
                    deal,
                    "magic",
                    0
                )
            ) != MAGIC_NUMBER:

                continue

            if deal.entry not in (
                mt5.DEAL_ENTRY_OUT,
                mt5.DEAL_ENTRY_OUT_BY
            ):

                continue

            pnl += float(
                deal.profit
            )

            pnl += float(
                deal.swap
            )

            pnl += float(
                deal.commission
            )

        return pnl

    # -------------------------------------------------------------

    def _ensure_risk_day(
        self
    ):

        today = (
            datetime.utcnow()
            .date()
            .isoformat()
        )

        risk_state = (
            self.state.get_risk_state()
        )

        if (
            risk_state.get(
                "date"
            )
            != today
        ):

            risk_state = {
                "date": today,
                "day_start_balance":
                    self.balance()
            }

            self.state.set_risk_state(
                risk_state
            )

        return risk_state

    # -------------------------------------------------------------

    def daily_drawdown(
        self
    ) -> float:

        risk_state = (
            self._ensure_risk_day()
        )

        start_balance = float(
            risk_state.get(
                "day_start_balance",
                self.balance()
            )
        )

        if start_balance <= 0:
            return 0.0

        realized = (
            self.daily_realized_pnl()
        )

        floating = (
            self.floating_pnl()
        )

        return (
            realized +
            floating
        )

    # -------------------------------------------------------------

    def daily_loss_reached(
        self
    ) -> bool:

        risk_state = (
            self._ensure_risk_day()
        )

        start_balance = float(
            risk_state.get(
                "day_start_balance",
                self.balance()
            )
        )

        if start_balance <= 0:
            return True

        max_loss = (
            start_balance *
            self.config[
                "max_daily_loss_percent"
            ] /
            100
        )

        drawdown = (
            self.daily_drawdown()
        )

        return drawdown <= -max_loss

    # -------------------------------------------------------------

    def trades_today(
        self
    ) -> int:

        deals = mt5.history_deals_get(
            self._today_start(),
            datetime.utcnow()
        )

        if not deals:
            return 0

        position_ids = set()

        for deal in deals:

            if int(
                getattr(
                    deal,
                    "magic",
                    0
                )
            ) != MAGIC_NUMBER:

                continue

            if deal.entry != mt5.DEAL_ENTRY_IN:
                continue

            position_id = int(
                getattr(
                    deal,
                    "position_id",
                    0
                )
            )

            if position_id:
                position_ids.add(
                    position_id
                )

        return len(
            position_ids
        )

    # -------------------------------------------------------------

    def closed_trade_summary(
        self,
        days: int = 60
    ) -> List[Dict]:

        deals = self._history_deals(
            days
        )

        grouped = {}

        for deal in deals:

            position_id = int(
                getattr(
                    deal,
                    "position_id",
                    0
                )
            )

            if not position_id:
                continue

            if deal.entry not in (
                mt5.DEAL_ENTRY_OUT,
                mt5.DEAL_ENTRY_OUT_BY
            ):

                continue

            if position_id not in grouped:

                grouped[position_id] = {
                    "position_id":
                        position_id,
                    "pnl": 0.0,
                    "last_time": 0
                }

            grouped[position_id]["pnl"] += (
                float(
                    deal.profit
                )
                +
                float(
                    deal.swap
                )
                +
                float(
                    deal.commission
                )
            )

            grouped[position_id]["last_time"] = max(
                grouped[position_id]["last_time"],
                int(deal.time)
            )

        results = list(
            grouped.values()
        )

        results.sort(
            key=lambda x:
                x["last_time"]
        )

        return results

    # -------------------------------------------------------------

    def consecutive_losses(
        self
    ) -> int:

        trades = (
            self.closed_trade_summary()
        )

        count = 0

        for trade in reversed(
            trades
        ):

            if trade["pnl"] < 0:

                count += 1

            else:

                break

        return count

    # -------------------------------------------------------------

    def last_closed_trade(
        self
    ) -> Optional[Dict]:

        trades = (
            self.closed_trade_summary()
        )

        if not trades:
            return None

        return trades[-1]

    # -------------------------------------------------------------

    def last_entry_timestamp(
        self
    ) -> Optional[int]:

        deals = self._history_deals()

        entries = [
            deal
            for deal in deals
            if deal.entry == mt5.DEAL_ENTRY_IN
        ]

        if not entries:
            return None

        return max(
            int(
                deal.time
            )
            for deal in entries
        )

    # -------------------------------------------------------------

    def open_risk_money(
        self
    ) -> float:

        total = 0.0

        for position in self.positions():

            if not position.sl:
                continue

            order_type = (
                mt5.ORDER_TYPE_BUY
                if position.type ==
                mt5.POSITION_TYPE_BUY
                else mt5.ORDER_TYPE_SELL
            )

            loss = mt5.order_calc_profit(
                order_type,
                position.symbol,
                float(position.volume),
                float(position.price_open),
                float(position.sl)
            )

            if loss is not None:

                total += abs(
                    float(loss)
                )

        return total

    # -------------------------------------------------------------

    def new_trade_risk_money(
        self,
        symbol: str,
        direction: str,
        volume: float,
        entry: float,
        sl: float
    ) -> float:

        order_type = (
            mt5.ORDER_TYPE_BUY
            if direction == BUY
            else mt5.ORDER_TYPE_SELL
        )

        loss = mt5.order_calc_profit(
            order_type,
            symbol,
            volume,
            entry,
            sl
        )

        if loss is None:
            return 0.0

        return abs(
            float(loss)
        )

    # -------------------------------------------------------------

    def calculate_volume(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop: float,
        risk_percent: float
    ) -> float:

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return 0.0

        balance = self.balance()

        if balance <= 0:
            return 0.0

        risk_money = (
            balance *
            risk_percent /
            100
        )

        order_type = (
            mt5.ORDER_TYPE_BUY
            if direction == BUY
            else mt5.ORDER_TYPE_SELL
        )

        # ---------------------------------------------------------
        # REAL BROKER CALCULATION
        # ---------------------------------------------------------

        loss_per_lot = mt5.order_calc_profit(
            order_type,
            symbol,
            1.0,
            entry,
            stop
        )

        if loss_per_lot is None:

            logger.error(
                f"[RISK] order_calc_profit "
                f"échoué pour {symbol}: "
                f"{mt5.last_error()}"
            )

            return 0.0

        loss_per_lot = abs(
            float(loss_per_lot)
        )

        if loss_per_lot <= 0:
            return 0.0

        raw_volume = (
            risk_money /
            loss_per_lot
        )

        volume = self.normalize_volume(
            symbol,
            raw_volume
        )

        if volume <= 0:

            logger.warning(
                f"[RISK] {symbol}: volume "
                f"insuffisant pour risquer "
                f"{risk_percent:.2f}%."
            )

            return 0.0

        actual_risk = (
            self.new_trade_risk_money(
                symbol,
                direction,
                volume,
                entry,
                stop
            )
        )

        # On ne force jamais le volume minimum
        # si celui-ci dépasse le risque autorisé.
        if actual_risk > risk_money * 1.01:

            logger.warning(
                f"[RISK] {symbol}: "
                f"volume minimum trop risqué. "
                f"Risque={actual_risk:.2f} "
                f"Max={risk_money:.2f}"
            )

            return 0.0

        return volume

    # -------------------------------------------------------------

    @staticmethod
    def volume_digits(
        step: float
    ) -> int:

        if step >= 1:
            return 0

        text = (
            f"{step:.10f}"
            .rstrip("0")
        )

        if "." not in text:
            return 0

        return len(
            text.split(".")[1]
        )

    # -------------------------------------------------------------

    def normalize_volume(
        self,
        symbol: str,
        volume: float
    ) -> float:

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return 0.0

        minimum = float(
            info.volume_min
        )

        maximum = float(
            info.volume_max
        )

        step = float(
            info.volume_step
        )

        if (
            volume <= 0
            or step <= 0
        ):

            return 0.0

        volume = min(
            volume,
            maximum
        )

        # Toujours arrondir vers le bas
        # pour ne pas dépasser le risque.
        volume = (
            math.floor(
                volume / step
            ) * step
        )

        if volume < minimum:
            return 0.0

        digits = self.volume_digits(
            step
        )

        return round(
            volume,
            digits
        )

    # -------------------------------------------------------------

    def can_trade(
        self
    ) -> bool:

        if self.daily_loss_reached():

            logger.warning(
                "[RISK] Limite perte journalière atteinte."
            )

            return False

        max_trades = int(
            self.config[
                "max_trades_per_day"
            ]
        )

        if (
            self.trades_today()
            >= max_trades
        ):

            logger.warning(
                "[RISK] Nombre maximal "
                "de trades/jour atteint."
            )

            return False

        losses = (
            self.consecutive_losses()
        )

        max_losses = int(
            self.config[
                "max_consecutive_losses"
            ]
        )

        if losses >= max_losses:

            logger.warning(
                f"[RISK] {losses} pertes "
                "consécutives."
            )

            return False

        # ---------------------------------------------------------
        # COOLDOWN AFTER LOSS
        # ---------------------------------------------------------

        last_closed = (
            self.last_closed_trade()
        )

        if last_closed:

            last_time = datetime.fromtimestamp(
                last_closed["last_time"],
                tz=timezone.utc
            )

            elapsed_minutes = (
                datetime.now(
                    timezone.utc
                )
                -
                last_time
            ).total_seconds() / 60

            if (
                last_closed["pnl"] < 0
                and
                elapsed_minutes
                <
                self.config[
                    "cooldown_after_loss_minutes"
                ]
            ):

                return False

            if (
                last_closed["pnl"] >= 0
                and
                elapsed_minutes
                <
                self.config[
                    "cooldown_after_trade_minutes"
                ]
            ):

                return False

        return True


# =================================================================
# TRADE LOGGER
# =================================================================

class TradeLogger:

    def __init__(
        self,
        config: Dict
    ):

        logging_cfg = config[
            "logging"
        ]

        self.trade_file = (
            BASE_DIR /
            logging_cfg.get(
                "trade_log",
                "logs/trades_v7.csv"
            )
        )

        self.signal_file = (
            BASE_DIR /
            logging_cfg.get(
                "signal_log",
                "logs/signals_v7.csv"
            )
        )

        self.trade_file.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self.signal_file.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self.trade_fields = [
            "timestamp",
            "symbol",
            "direction",
            "regime",
            "h4_direction",
            "h1_direction",
            "m15_direction",
            "score",
            "quality",
            "atr",
            "adx",
            "atr_percentile",
            "session",
            "entry",
            "sl",
            "tp1",
            "tp2",
            "tp3",
            "risk_percent",
            "volume",
            "reason"
        ]

        self.signal_fields = [
            "timestamp",
            "symbol",
            "accepted",
            "reason",
            "regime",
            "h4_direction",
            "h1_direction",
            "m15_direction",
            "score",
            "quality",
            "adx",
            "atr",
            "atr_percentile",
            "session"
        ]

        self._ensure_file(
            self.trade_file,
            self.trade_fields
        )

        self._ensure_file(
            self.signal_file,
            self.signal_fields
        )

    # -------------------------------------------------------------

    @staticmethod
    def _ensure_file(
        path: Path,
        fields: List[str]
    ):

        if path.exists():
            return

        with open(
            path,
            "w",
            newline="",
            encoding="utf-8"
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=fields
            )

            writer.writeheader()

    # -------------------------------------------------------------

    def log_trade(
        self,
        data: Dict
    ):

        row = {
            field:
                data.get(
                    field,
                    ""
                )
            for field in self.trade_fields
        }

        with open(
            self.trade_file,
            "a",
            newline="",
            encoding="utf-8"
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=self.trade_fields
            )

            writer.writerow(row)

    # -------------------------------------------------------------

    def log_signal(
        self,
        data: Dict
    ):

        row = {
            field:
                data.get(
                    field,
                    ""
                )
            for field in self.signal_fields
        }

        with open(
            self.signal_file,
            "a",
            newline="",
            encoding="utf-8"
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=self.signal_fields
            )

            writer.writerow(row)


# =================================================================
# ORDER ENGINE
# =================================================================

class OrderEngine:

    def __init__(
        self,
        config: Dict,
        risk: RiskEngine
    ):

        self.config = config

        self.execution = config[
            "execution"
        ]

        self.risk = risk

    # -------------------------------------------------------------

    @staticmethod
    def choose_filling(
        info
    ):

        configured = (
            str(
                CONFIG[
                    "execution"
                ].get(
                    "fill_policy",
                    "AUTO"
                )
            )
            .upper()
        )

        if configured == "FOK":

            return mt5.ORDER_FILLING_FOK

        if configured == "IOC":

            return mt5.ORDER_FILLING_IOC

        if configured == "RETURN":

            return mt5.ORDER_FILLING_RETURN

        filling_mode = int(
            getattr(
                info,
                "filling_mode",
                0
            )
        )

        if (
            filling_mode
            &
            mt5.SYMBOL_FILLING_FOK
        ):

            return mt5.ORDER_FILLING_FOK

        if (
            filling_mode
            &
            mt5.SYMBOL_FILLING_IOC
        ):

            return mt5.ORDER_FILLING_IOC

        return mt5.ORDER_FILLING_RETURN

    # -------------------------------------------------------------

    @staticmethod
    def minimum_stop_distance(
        info
    ) -> float:

        point = float(
            info.point
        )

        stops_level = float(
            getattr(
                info,
                "trade_stops_level",
                0
            )
        )

        freeze_level = float(
            getattr(
                info,
                "trade_freeze_level",
                0
            )
        )

        return (
            max(
                stops_level,
                freeze_level
            )
            *
            point
        )

    # -------------------------------------------------------------

    def validate_stops(
        self,
        symbol: str,
        direction: str,
        price: float,
        sl: float,
        tp: float
    ) -> bool:

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return False

        min_distance = (
            self.minimum_stop_distance(
                info
            )
        )

        if direction == BUY:

            if sl >= price:
                return False

            if tp <= price:
                return False

            if (
                min_distance > 0
                and
                price - sl
                <
                min_distance
            ):

                return False

            if (
                min_distance > 0
                and
                tp - price
                <
                min_distance
            ):

                return False

        else:

            if sl <= price:
                return False

            if tp >= price:
                return False

            if (
                min_distance > 0
                and
                sl - price
                <
                min_distance
            ):

                return False

            if (
                min_distance > 0
                and
                price - tp
                <
                min_distance
            ):

                return False

        return True

    # -------------------------------------------------------------

    def send_order(
        self,
        setup: TradeSetup
    ) -> Optional[object]:

        info = mt5.symbol_info(
            setup.symbol
        )

        tick = mt5.symbol_info_tick(
            setup.symbol
        )

        if info is None or tick is None:
            return None

        order_type = (
            mt5.ORDER_TYPE_BUY
            if setup.direction == BUY
            else mt5.ORDER_TYPE_SELL
        )

        price = (
            tick.ask
            if setup.direction == BUY
            else tick.bid
        )

        price = round(
            price,
            info.digits
        )

        sl = round(
            setup.sl_price,
            info.digits
        )

        tp = round(
            setup.tp3_price,
            info.digits
        )

        if not self.validate_stops(
            setup.symbol,
            setup.direction,
            price,
            sl,
            tp
        ):

            logger.error(
                f"[ORDER] Stops invalides "
                f"{setup.symbol}: "
                f"entry={price} "
                f"sl={sl} "
                f"tp={tp}"
            )

            return None

        filling = self.choose_filling(
            info
        )

        request = {
            "action":
                mt5.TRADE_ACTION_DEAL,

            "symbol":
                setup.symbol,

            "volume":
                setup.total_volume,

            "type":
                order_type,

            "price":
                price,

            "sl":
                sl,

            "tp":
                tp,

            "deviation":
                int(
                    self.execution[
                        "deviation_points"
                    ]
                ),

            "magic":
                MAGIC_NUMBER,

            "comment":
                "EA_V7",

            "type_time":
                mt5.ORDER_TIME_GTC,

            "type_filling":
                filling
        }

        retries = int(
            self.execution.get(
                "retry_count",
                3
            )
        )

        delay = float(
            self.execution.get(
                "retry_delay_seconds",
                2
            )
        )

        for attempt in range(
            retries
        ):

            try:

                check = mt5.order_check(
                    request
                )

                if check is None:

                    logger.error(
                        f"[ORDER CHECK] "
                        f"{mt5.last_error()}"
                    )

                    return None

                check_retcode = int(
                    getattr(
                        check,
                        "retcode",
                        0
                    )
                )

                if check_retcode not in (
                    0,
                    mt5.TRADE_RETCODE_DONE
                ):

                    logger.error(
                        f"[ORDER CHECK] "
                        f"refusé: {check}"
                    )

                    return None

                result = mt5.order_send(
                    request
                )

                if result is None:

                    logger.error(
                        f"[ORDER] None: "
                        f"{mt5.last_error()}"
                    )

                    time.sleep(
                        delay
                    )

                    continue

                accepted_codes = {
                    mt5.TRADE_RETCODE_DONE,
                    mt5.TRADE_RETCODE_PLACED,
                    mt5.TRADE_RETCODE_DONE_PARTIAL
                }

                if result.retcode in accepted_codes:

                    logger.info(
                        f"[ORDER] {setup.symbol} "
                        f"{setup.direction} "
                        f"volume={setup.total_volume} "
                        f"retcode={result.retcode}"
                    )

                    return result

                logger.error(
                    f"[ORDER] Refusé "
                    f"tentative={attempt + 1}/"
                    f"{retries}: "
                    f"{result}"
                )

            except Exception as error:

                logger.error(
                    f"[ORDER] Exception: "
                    f"{error}"
                )

            time.sleep(
                delay
            )

        return None


# =================================================================
# POSITION MANAGER
# =================================================================

class PositionManager:

    def __init__(
        self,
        config: Dict,
        market: MarketData,
        state: StateStore,
        risk: RiskEngine,
        orders: OrderEngine
    ):

        self.config = config

        self.market = market

        self.state = state

        self.risk = risk

        self.orders = orders

        self.tp_config = config[
            "take_profit"
        ]

        self.management = config[
            "position_management"
        ]

        self.stop_config = config[
            "stop_loss"
        ]

    # -------------------------------------------------------------

    def positions(self) -> List:

        return self.risk.positions()

    # -------------------------------------------------------------

    def _get_or_rebuild_state(
        self,
        position
    ) -> PositionState:

        existing = self.state.get_position(
            position.ticket
        )

        if existing:
            return existing

        info = mt5.symbol_info(
            position.symbol
        )

        if info is None:

            raise RuntimeError(
                f"Symbol info indisponible: "
                f"{position.symbol}"
            )

        direction = (
            BUY
            if position.type ==
            mt5.POSITION_TYPE_BUY
            else SELL
        )

        entry = float(
            position.price_open
        )

        tp3 = float(
            position.tp
        )

        if tp3 > 0:

            initial_risk = (
                abs(tp3 - entry)
                / 3.0
            )

        elif position.sl > 0:

            initial_risk = abs(
                entry -
                position.sl
            )

        else:

            initial_risk = 0.0

        if initial_risk <= 0:

            atr_df = self.market.fetch(
                position.symbol,
                CONFIG[
                    "timeframes"
                ]["atr"],
                100,
                5
            )

            if not atr_df.empty:

                atr = Indicators.atr(
                    atr_df["high"],
                    atr_df["low"],
                    atr_df["close"],
                    self.stop_config[
                        "atr_period"
                    ]
                ).iloc[-1]

                initial_risk = float(
                    atr *
                    self.stop_config[
                        "atr_multiplier"
                    ]
                )

        if initial_risk <= 0:

            initial_risk = (
                abs(
                    entry -
                    position.sl
                )
                if position.sl > 0
                else 0
            )

        if direction == BUY:

            tp1 = entry + initial_risk
            tp2 = entry + initial_risk * 2
            tp3_calc = entry + initial_risk * 3

        else:

            tp1 = entry - initial_risk
            tp2 = entry - initial_risk * 2
            tp3_calc = entry - initial_risk * 3

        if tp3 <= 0:
            tp3 = tp3_calc

        initial_volume = float(
            position.volume
        )

        partial_possible = (
            initial_volume
            >=
            float(info.volume_min) * 3
        )

        state = PositionState(
            ticket=int(
                position.ticket
            ),
            symbol=position.symbol,
            direction=direction,
            entry_price=entry,
            initial_sl=float(
                position.sl
                if position.sl > 0
                else (
                    entry - initial_risk
                    if direction == BUY
                    else entry + initial_risk
                )
            ),
            initial_risk_distance=
                initial_risk,
            tp1_price=tp1,
            tp2_price=tp2,
            tp3_price=tp3,
            initial_volume=
                initial_volume,
            tp1_hit=False,
            tp2_hit=False,
            partial_management=
                partial_possible,
            created_at=datetime.utcnow().isoformat()
        )

        self.state.set_position(
            state
        )

        return state

    # -------------------------------------------------------------

    def modify_position(
        self,
        position,
        new_sl: float,
        new_tp: Optional[float] = None
    ) -> bool:

        info = mt5.symbol_info(
            position.symbol
        )

        if info is None:
            return False

        tick = mt5.symbol_info_tick(
            position.symbol
        )

        if tick is None:
            return False

        is_buy = (
            position.type ==
            mt5.POSITION_TYPE_BUY
        )

        min_distance = (
            self.orders.minimum_stop_distance(
                info
            )
        )

        if is_buy:

            if (
                new_sl >= tick.bid
            ):
                return False

            if (
                min_distance > 0
                and
                tick.bid - new_sl
                <
                min_distance
            ):

                return False

        else:

            if (
                new_sl <= tick.ask
            ):
                return False

            if (
                min_distance > 0
                and
                new_sl - tick.ask
                <
                min_distance
            ):

                return False

        new_sl = round(
            new_sl,
            info.digits
        )

        tp = (
            position.tp
            if new_tp is None
            else new_tp
        )

        request = {
            "action":
                mt5.TRADE_ACTION_SLTP,

            "symbol":
                position.symbol,

            "position":
                position.ticket,

            "sl":
                new_sl,

            "tp":
                tp
        }

        result = mt5.order_send(
            request
        )

        if (
            result
            and
            result.retcode ==
            mt5.TRADE_RETCODE_DONE
        ):

            return True

        logger.warning(
            f"[SLTP] Modification échouée "
            f"{position.symbol} "
            f"#{position.ticket}: "
            f"{result}"
        )

        return False

    # -------------------------------------------------------------

    def close_partial(
        self,
        position,
        volume: float,
        label: str
    ) -> bool:

        info = mt5.symbol_info(
            position.symbol
        )

        tick = mt5.symbol_info_tick(
            position.symbol
        )

        if info is None or tick is None:
            return False

        volume = self.risk.normalize_volume(
            position.symbol,
            volume
        )

        if volume <= 0:
            return False

        # Ne jamais tenter de fermer plus
        # que le volume réellement disponible.
        volume = min(
            volume,
            float(position.volume)
        )

        volume = self.risk.normalize_volume(
            position.symbol,
            volume
        )

        if volume <= 0:
            return False

        if position.type == mt5.POSITION_TYPE_BUY:

            order_type = (
                mt5.ORDER_TYPE_SELL
            )

            price = tick.bid

        else:

            order_type = (
                mt5.ORDER_TYPE_BUY
            )

            price = tick.ask

        request = {
            "action":
                mt5.TRADE_ACTION_DEAL,

            "symbol":
                position.symbol,

            "volume":
                volume,

            "type":
                order_type,

            "position":
                position.ticket,

            "price":
                round(
                    price,
                    info.digits
                ),

            "deviation":
                int(
                    CONFIG[
                        "execution"
                    ]["deviation_points"]
                ),

            "magic":
                MAGIC_NUMBER,

            "comment":
                f"V7_{label}",

            "type_time":
                mt5.ORDER_TIME_GTC,

            "type_filling":
                self.orders.choose_filling(
                    info
                )
        }

        result = mt5.order_send(
            request
        )

        if result is None:

            logger.error(
                f"[PARTIAL] {label}: "
                f"{mt5.last_error()}"
            )

            return False

        accepted = {
            mt5.TRADE_RETCODE_DONE,
            mt5.TRADE_RETCODE_PLACED,
            mt5.TRADE_RETCODE_DONE_PARTIAL
        }

        if result.retcode not in accepted:

            logger.error(
                f"[PARTIAL] {label} refusé: "
                f"{result}"
            )

            return False

        logger.info(
            f"[PARTIAL] {position.symbol} "
            f"#{position.ticket} "
            f"{label} "
            f"volume={volume}"
        )

        return True

    # -------------------------------------------------------------

    def manage_position(
        self,
        position
    ):

        try:

            state = (
                self._get_or_rebuild_state(
                    position
                )
            )

        except Exception as error:

            logger.error(
                f"[MANAGER] State error "
                f"#{position.ticket}: "
                f"{error}"
            )

            return

        timeframe = CONFIG[
            "timeframes"
        ]["atr"]

        df = self.market.fetch(
            position.symbol,
            timeframe,
            200,
            10
        )

        if df.empty:
            return

        atr = Indicators.atr(
            df["high"],
            df["low"],
            df["close"],
            self.management[
                "trailing"
            ]["atr_period"]
        ).iloc[-1]

        if pd.isna(atr) or atr <= 0:
            return

        atr = float(
            atr
        )

        tick = mt5.symbol_info_tick(
            position.symbol
        )

        if tick is None:
            return

        is_buy = (
            position.type ==
            mt5.POSITION_TYPE_BUY
        )

        current = (
            tick.bid
            if is_buy
            else tick.ask
        )

        risk_distance = (
            state.initial_risk_distance
        )

        if risk_distance <= 0:
            return

        profit_distance = (
            current -
            state.entry_price
            if is_buy
            else
            state.entry_price -
            current
        )

        r_multiple = (
            profit_distance /
            risk_distance
        )

        # =========================================================
        # TP1
        # =========================================================

        tp1_condition = (
            current >= state.tp1_price
            if is_buy
            else
            current <= state.tp1_price
        )

        if (
            state.partial_management
            and
            not state.tp1_hit
            and
            tp1_condition
        ):

            ratio = (
                self.tp_config[
                    "tp1_close_percent"
                ] / 100
            )

            volume_to_close = (
                state.initial_volume *
                ratio
            )

            if self.close_partial(
                position,
                volume_to_close,
                "TP1"
            ):

                state.tp1_hit = True

                self.state.set_position(
                    state
                )

        # =========================================================
        # TP2
        # =========================================================

        tp2_condition = (
            current >= state.tp2_price
            if is_buy
            else
            current <= state.tp2_price
        )

        if (
            state.partial_management
            and
            state.tp1_hit
            and
            not state.tp2_hit
            and
            tp2_condition
        ):

            ratio = (
                self.tp_config[
                    "tp2_close_percent"
                ] / 100
            )

            volume_to_close = (
                state.initial_volume *
                ratio
            )

            # Récupération du dernier état de position
            current_positions = [
                p
                for p in self.positions()
                if p.ticket ==
                position.ticket
            ]

            if current_positions:

                current_position = (
                    current_positions[0]
                )

                volume_to_close = min(
                    volume_to_close,
                    float(
                        current_position.volume
                    )
                )

                if self.close_partial(
                    current_position,
                    volume_to_close,
                    "TP2"
                ):

                    state.tp2_hit = True

                    self.state.set_position(
                        state
                    )

        # =========================================================
        # BREAK EVEN
        # =========================================================

        be_cfg = self.management[
            "breakeven"
        ]

        if (
            be_cfg["enabled"]
            and
            r_multiple >=
            be_cfg["activation_r"]
        ):

            offset = (
                risk_distance *
                be_cfg["offset_r"]
            )

            if is_buy:

                new_sl = (
                    state.entry_price +
                    offset
                )

                should_modify = (
                    position.sl <= 0
                    or
                    new_sl > position.sl
                )

            else:

                new_sl = (
                    state.entry_price -
                    offset
                )

                should_modify = (
                    position.sl <= 0
                    or
                    new_sl < position.sl
                )

            if should_modify:

                if self.modify_position(
                    position,
                    new_sl
                ):

                    logger.info(
                        f"[BE] {position.symbol} "
                        f"#{position.ticket} "
                        f"SL={new_sl}"
                    )

        # =========================================================
        # TRAILING
        # =========================================================

        trailing_cfg = self.management[
            "trailing"
        ]

        if (
            trailing_cfg["enabled"]
            and
            r_multiple >=
            trailing_cfg["activation_r"]
        ):

            distance = (
                atr *
                trailing_cfg[
                    "atr_multiplier"
                ]
            )

            lookback = int(
                self.stop_config[
                    "swing_lookback"
                ]
            )

            recent = df.tail(
                lookback
            )

            buffer = (
                atr *
                self.stop_config[
                    "structure_buffer_atr"
                ]
            )

            if is_buy:

                atr_sl = (
                    current -
                    distance
                )

                structure_sl = (
                    float(
                        recent["low"].min()
                    )
                    -
                    buffer
                )

                # Le trailing doit rester
                # derrière la structure.
                new_sl = max(
                    atr_sl,
                    structure_sl
                )

                if (
                    position.sl <= 0
                    or
                    new_sl > position.sl
                ):

                    self.modify_position(
                        position,
                        new_sl
                    )

            else:

                atr_sl = (
                    current +
                    distance
                )

                structure_sl = (
                    float(
                        recent["high"].max()
                    )
                    +
                    buffer
                )

                new_sl = min(
                    atr_sl,
                    structure_sl
                )

                if (
                    position.sl <= 0
                    or
                    new_sl < position.sl
                ):

                    self.modify_position(
                        position,
                        new_sl
                    )

    # -------------------------------------------------------------

    def cleanup_states(self):

        positions = self.positions()

        active_tickets = {
            int(
                position.ticket
            )
            for position in positions
        }

        stored = list(
            self.state.data.get(
                "positions",
                {}
            ).keys()
        )

        for ticket_text in stored:

            try:

                ticket = int(
                    ticket_text
                )

            except ValueError:

                continue

            if ticket not in active_tickets:

                self.state.remove_position(
                    ticket
                )

    # -------------------------------------------------------------

    def manage(self):

        self.cleanup_states()

        positions = self.positions()

        if not positions:
            return

        for position in positions:

            self.manage_position(
                position
            )


# =================================================================
# TELEGRAM
# =================================================================

class Telegram:

    def __init__(
        self,
        config: Dict
    ):

        self.config = config[
            "telegram"
        ]

        self.enabled = bool(
            self.config.get(
                "enabled",
                False
            )
        )

        self.token = (
            self.config.get(
                "bot_token",
                ""
            )
        )

        self.chat_id = (
            self.config.get(
                "chat_id",
                ""
            )
        )

    # -------------------------------------------------------------

    def send(
        self,
        message: str
    ):

        if not self.enabled:
            return

        if (
            not self.token
            or
            not self.chat_id
        ):

            logger.warning(
                "[TELEGRAM] "
                "bot_token/chat_id manquant."
            )

            return

        try:

            url = (
                "https://api.telegram.org/"
                f"bot{self.token}/sendMessage"
            )

            response = requests.post(
                url,
                params={
                    "chat_id":
                        self.chat_id,

                    "text":
                        message
                },
                timeout=10
            )

            if response.status_code != 200:

                logger.warning(
                    f"[TELEGRAM] HTTP "
                    f"{response.status_code}: "
                    f"{response.text[:300]}"
                )

        except Exception as error:

            logger.warning(
                f"[TELEGRAM] {error}"
            )


# =================================================================
# TRADING ENGINE
# =================================================================

class TradingEngine:

    def __init__(
        self,
        config: Dict
    ):

        self.config = config

        self.market = MarketData()

        self.state = StateStore(
            STATE_FILE
        )

        self.regime = RegimeEngine(
            config
        )

        self.signal = SignalEngine(
            config
        )

        self.session = SessionEngine(
            config
        )

        self.news = NewsFilter(
            config
        )

        self.risk = RiskEngine(
            config,
            self.state
        )

        self.logger = TradeLogger(
            config
        )

        self.telegram = Telegram(
            config
        )

        self.orders = OrderEngine(
            config,
            self.risk
        )

        self.position_manager = (
            PositionManager(
                config,
                self.market,
                self.state,
                self.risk,
                self.orders
            )
        )

    # -------------------------------------------------------------

    def positions(self) -> List:

        return self.risk.positions()

    # -------------------------------------------------------------

    def total_positions(self) -> int:

        return len(
            self.positions()
        )

    # -------------------------------------------------------------

    def symbol_positions(
        self,
        symbol: str
    ) -> int:

        return sum(
            1
            for position in self.positions()
            if position.symbol == symbol
        )

    # -------------------------------------------------------------

    def spread_ok(
        self,
        symbol: str
    ) -> bool:

        tick = mt5.symbol_info_tick(
            symbol
        )

        info = mt5.symbol_info(
            symbol
        )

        if tick is None or info is None:
            return False

        spread_price = (
            float(tick.ask)
            -
            float(tick.bid)
        )

        symbol_config = (
            self.config[
                "symbols"
            ][symbol]
        )

        maximum = float(
            symbol_config[
                "max_spread_price"
            ]
        )

        if spread_price > maximum:

            logger.info(
                f"[SPREAD] {symbol}: "
                f"{spread_price:.8f} > "
                f"{maximum:.8f}"
            )

            return False

        spread_points = (
            spread_price /
            float(info.point)
        )

        logger.debug(
            f"[SPREAD] {symbol}: "
            f"{spread_price:.8f} "
            f"({spread_points:.1f} points)"
        )

        return True

    # -------------------------------------------------------------

    def quality(
        self,
        regime_data: Dict,
        h1: Dict,
        m15: Dict,
        direction: str
    ) -> float:

        score = 0.0

        # ADX quality
        adx = regime_data["adx"]

        trend_threshold = float(
            self.config[
                "market_regime"
            ]["adx_trend_threshold"]
        )

        strong_threshold = float(
            self.config[
                "market_regime"
            ]["adx_strong_threshold"]
        )

        if adx >= strong_threshold:

            score += 0.30

        elif adx >= trend_threshold:

            score += 0.20

        # Volatility quality
        percentile = (
            regime_data[
                "atr_percentile"
            ]
        )

        if 0.20 <= percentile <= 0.85:

            score += 0.20

        # H1 directional strength
        max_h1 = (
            self.signal.weights[
                "h1_trend"
            ]
            +
            self.signal.weights[
                "supertrend"
            ]
            +
            self.signal.weights[
                "ichimoku"
            ]
            +
            self.signal.weights[
                "rsi"
            ]
            +
            self.signal.weights[
                "macd"
            ]
        )

        if max_h1 > 0:

            h1_strength = (
                abs(
                    h1["score"]
                ) /
                max_h1
            )

            score += (
                min(
                    1.0,
                    h1_strength
                )
                * 0.25
            )

        # M15 confirmation
        m15_strength = (
            m15["score"] /
            5.0
        )

        score += (
            min(
                1.0,
                m15_strength
            )
            * 0.25
        )

        # Extreme RSI penalty
        rsi = h1["rsi"]

        if (
            direction == BUY
            and
            rsi >= self.config[
                "filters"
            ]["rsi_extreme_buy"]
        ):

            score -= 0.15

        if (
            direction == SELL
            and
            rsi <= self.config[
                "filters"
            ]["rsi_extreme_sell"]
        ):

            score -= 0.15

        return max(
            0.0,
            min(
                1.0,
                score
            )
        )

    # -------------------------------------------------------------

    def calculate_stop(
        self,
        symbol: str,
        direction: str,
        df: pd.DataFrame,
        atr: float
    ) -> float:

        info = mt5.symbol_info(
            symbol
        )

        if info is None:
            return 0.0

        cfg = self.config[
            "stop_loss"
        ]

        close = float(
            df["close"].iloc[-1]
        )

        atr_sl_distance = (
            atr *
            float(
                cfg[
                    "atr_multiplier"
                ]
            )
        )

        if cfg[
            "use_swing_structure"
        ]:

            lookback = int(
                cfg[
                    "swing_lookback"
                ]
            )

            recent = df.tail(
                lookback
            )

            buffer = (
                atr *
                float(
                    cfg[
                        "structure_buffer_atr"
                    ]
                )
            )

            if direction == BUY:

                structure_sl = (
                    float(
                        recent["low"].min()
                    )
                    -
                    buffer
                )

                atr_sl = (
                    close -
                    atr_sl_distance
                )

                sl = min(
                    structure_sl,
                    atr_sl
                )

                max_distance = (
                    atr *
                    float(
                        cfg[
                            "maximum_sl_atr"
                        ]
                    )
                )

                actual_distance = (
                    close - sl
                )

                # Si la structure exige un SL
                # beaucoup trop éloigné,
                # on refuse le trade.
                if actual_distance > max_distance:

                    logger.info(
                        f"[SL] {symbol}: "
                        "structure trop éloignée."
                    )

                    return 0.0

            else:

                structure_sl = (
                    float(
                        recent["high"].max()
                    )
                    +
                    buffer
                )

                atr_sl = (
                    close +
                    atr_sl_distance
                )

                sl = max(
                    structure_sl,
                    atr_sl
                )

                max_distance = (
                    atr *
                    float(
                        cfg[
                            "maximum_sl_atr"
                        ]
                    )
                )

                actual_distance = (
                    sl - close
                )

                if actual_distance > max_distance:

                    logger.info(
                        f"[SL] {symbol}: "
                        "structure trop éloignée."
                    )

                    return 0.0

        else:

            if direction == BUY:

                sl = (
                    close -
                    atr_sl_distance
                )

            else:

                sl = (
                    close +
                    atr_sl_distance
                )

        return round(
            sl,
            info.digits
        )

    # -------------------------------------------------------------

    def build_setup(
        self,
        symbol: str
    ) -> Optional[TradeSetup]:

        if not self.config[
            "symbols"
        ][symbol].get(
            "enabled",
            True
        ):

            return None

        # =========================================================
        # GLOBAL RISK
        # =========================================================

        if not self.risk.can_trade():
            return None

        # =========================================================
        # SESSION
        # =========================================================

        session_name = (
            self.session.current_session()
        )

        if not self.session.allowed():

            logger.info(
                f"[SESSION] {symbol}: "
                f"{session_name} interdite."
            )

            return None

        # =========================================================
        # NEWS
        # =========================================================

        if self.news.blocked():

            logger.info(
                f"[NEWS] {symbol}: "
                "trading bloqué."
            )

            return None

        # =========================================================
        # SPREAD
        # =========================================================

        if self.config[
            "filters"
        ]["max_spread_enabled"]:

            if not self.spread_ok(
                symbol
            ):

                return None

        # =========================================================
        # POSITIONS
        # =========================================================

        max_positions = int(
            self.config[
                "risk_management"
            ]["max_positions"]
        )

        max_symbol_positions = int(
            self.config[
                "risk_management"
            ]["max_positions_per_symbol"]
        )

        if (
            self.total_positions()
            >= max_positions
        ):

            logger.info(
                "[EXPOSURE] "
                "Nombre maximal de positions."
            )

            return None

        if (
            self.symbol_positions(symbol)
            >= max_symbol_positions
        ):

            return None

        # =========================================================
        # DATA
        # =========================================================

        bars_cfg = {
            "H4": 500,
            "H1": 500,
            "M15": 500
        }

        h4 = self.market.fetch(
            symbol,
            "H4",
            bars_cfg["H4"],
            5
        )

        h1 = self.market.fetch(
            symbol,
            "H1",
            bars_cfg["H1"],
            5
        )

        m15 = self.market.fetch(
            symbol,
            "M15",
            bars_cfg["M15"],
            5
        )

        if (
            h4.empty
            or
            h1.empty
            or
            m15.empty
        ):

            return None

        # =========================================================
        # REGIME
        # =========================================================

        regime = (
            self.regime.analyze(
                h4
            )
        )

        # =========================================================
        # LOW / EXTREME VOLATILITY
        # =========================================================

        if self.config[
            "filters"
        ]["avoid_low_volatility"]:

            low_percentile = (
                self.config[
                    "market_regime"
                ]["low_volatility_percentile"]
                / 100
            )

            if (
                regime[
                    "atr_percentile"
                ]
                <= low_percentile
            ):

                logger.info(
                    f"[VOL] {symbol}: "
                    "volatilité trop faible."
                )

                return None

        if self.config[
            "filters"
        ]["avoid_extreme_volatility"]:

            high_percentile = (
                self.config[
                    "market_regime"
                ]["high_volatility_percentile"]
                / 100
            )

            if (
                regime[
                    "atr_percentile"
                ]
                >= high_percentile
                and
                regime[
                    "regime"
                ]
                == "HIGH_VOLATILITY"
            ):

                logger.info(
                    f"[VOL] {symbol}: "
                    "volatilité extrême."
                )

                return None

        if (
            regime["adx"]
            <
            self.config[
                "filters"
            ]["min_adx"]
        ):

            logger.info(
                f"[ADX] {symbol}: "
                f"{regime['adx']:.2f} "
                "trop faible."
            )

            return None

        # =========================================================
        # H4
        # =========================================================

        h4_direction = (
            self.signal.h4_direction(
                h4
            )
        )

        if (
            self.config[
                "signal_engine"
            ]["require_h4_alignment"]
            and
            h4_direction == NEUTRAL
        ):

            logger.info(
                f"[H4] {symbol}: "
                "direction neutre."
            )

            return None

        # =========================================================
        # H1
        # =========================================================

        h1_analysis = (
            self.signal.analyze_h1(
                h1
            )
        )

        h1_direction = (
            h1_analysis[
                "direction"
            ]
        )

        if h1_direction == NEUTRAL:

            return None

        if (
            self.config[
                "signal_engine"
            ]["require_h1_alignment"]
            and
            h1_direction
            !=
            h4_direction
        ):

            logger.info(
                f"[ALIGNMENT] {symbol}: "
                f"H4={h4_direction} "
                f"H1={h1_direction}"
            )

            return None

        # =========================================================
        # M15
        # =========================================================

        m15_analysis = (
            self.signal.analyze_m15(
                m15,
                h1_direction
            )
        )

        m15_direction = (
            m15_analysis[
                "direction"
            ]
        )

        if (
            self.config[
                "signal_engine"
            ]["require_m15_confirmation"]
            and
            m15_direction
            !=
            h1_direction
        ):

            logger.info(
                f"[M15] {symbol}: "
                f"{m15_direction} "
                f"attendu={h1_direction}"
            )

            return None

        # =========================================================
        # COMBINED SCORE
        # =========================================================

        weights = (
            self.signal.weights
        )

        combined_score = 0.0

        if (
            h4_direction ==
            h1_direction
        ):

            combined_score += (
                weights[
                    "h4_trend"
                ]
            )

        h1_sign = (
            1
            if h1_analysis[
                "score"
            ] > 0
            else -1
        )

        # H1 components
        combined_score += (
            weights[
                "h1_trend"
            ]
            if h1_sign > 0
            else -weights[
                "h1_trend"
            ]
        )

        # Supertrend
        st_cfg = self.config[
            "indicators"
        ]["supertrend"]

        st = Indicators.supertrend(
            h1["high"],
            h1["low"],
            h1["close"],
            st_cfg["period"],
            st_cfg["multiplier"]
        )

        st_direction = (
            BUY
            if st[
                "direction"
            ].iloc[-1] == 1
            else SELL
        )

        combined_score += (
            weights[
                "supertrend"
            ]
            if st_direction ==
            h1_direction
            else 0.0
        )

        # Ichimoku
        if (
            h1_analysis[
                "ichimoku_direction"
            ]
            ==
            h1_direction
        ):

            combined_score += (
                weights[
                    "ichimoku"
                ]
            )

        # RSI
        if (
            h1_direction == BUY
            and
            h1_analysis["rsi"] >= 50
        ):

            combined_score += (
                weights["rsi"]
            )

        elif (
            h1_direction == SELL
            and
            h1_analysis["rsi"] < 50
        ):

            combined_score += (
                weights["rsi"]
            )

        # MACD
        if (
            h1_direction == BUY
            and
            h1_analysis[
                "macd_hist"
            ] > 0
        ):

            combined_score += (
                weights["macd"]
            )

        elif (
            h1_direction == SELL
            and
            h1_analysis[
                "macd_hist"
            ] < 0
        ):

            combined_score += (
                weights["macd"]
            )

        # M15 normalized score
        m15_component = (
            m15_analysis["score"]
            / 5.0
        )

        combined_score += (
            m15_component *
            weights["m15_confirmation"]
        )

        max_score = float(
            self.config[
                "signal_engine"
            ]["maximum_score"]
        )

        combined_score = min(
            max_score,
            max(
                0.0,
                combined_score
            )
        )

        minimum_score = float(
            self.config[
                "signal_engine"
            ]["minimum_score"]
        )

        if (
            combined_score
            <
            minimum_score
        ):

            logger.info(
                f"[SCORE] {symbol}: "
                f"{combined_score:.2f}/"
                f"{max_score:.2f} "
                f"< {minimum_score:.2f}"
            )

            self.logger.log_signal({
                "timestamp":
                    datetime.utcnow().isoformat(),
                "symbol":
                    symbol,
                "accepted":
                    False,
                "reason":
                    "score_below_minimum",
                "regime":
                    regime["regime"],
                "h4_direction":
                    h4_direction,
                "h1_direction":
                    h1_direction,
                "m15_direction":
                    m15_direction,
                "score":
                    combined_score,
                "quality":
                    0,
                "adx":
                    regime["adx"],
                "atr":
                    regime["atr"],
                "atr_percentile":
                    regime[
                        "atr_percentile"
                    ],
                "session":
                    session_name
            })

            return None

        # =========================================================
        # QUALITY
        # =========================================================

        quality = self.quality(
            regime,
            h1_analysis,
            m15_analysis,
            h1_direction
        )

        # La qualité minimale vient du config.
        minimum_quality = float(
            self.config[
                "signal_engine"
            ].get(
                "minimum_quality",
                0.70
            )
        )

        if quality < minimum_quality:

            logger.info(
                f"[QUALITY] {symbol}: "
                f"{quality:.2%} < "
                f"{minimum_quality:.2%}"
            )

            self.logger.log_signal({
                "timestamp":
                    datetime.utcnow().isoformat(),
                "symbol":
                    symbol,
                "accepted":
                    False,
                "reason":
                    "quality_below_minimum",
                "regime":
                    regime["regime"],
                "h4_direction":
                    h4_direction,
                "h1_direction":
                    h1_direction,
                "m15_direction":
                    m15_direction,
                "score":
                    combined_score,
                "quality":
                    quality,
                "adx":
                    regime["adx"],
                "atr":
                    regime["atr"],
                "atr_percentile":
                    regime[
                        "atr_percentile"
                    ],
                "session":
                    session_name
            })

            return None

        # =========================================================
        # RSI EXTREME
        # =========================================================

        if self.config[
            "filters"
        ]["avoid_overextended_rsi"]:

            rsi = (
                h1_analysis[
                    "rsi"
                ]
            )

            if (
                h1_direction == BUY
                and
                rsi >= self.config[
                    "filters"
                ]["rsi_extreme_buy"]
            ):

                logger.info(
                    f"[RSI] {symbol}: "
                    "BUY trop étendu."
                )

                return None

            if (
                h1_direction == SELL
                and
                rsi <= self.config[
                    "filters"
                ]["rsi_extreme_sell"]
            ):

                logger.info(
                    f"[RSI] {symbol}: "
                    "SELL trop étendu."
                )

                return None

        # =========================================================
        # ATR
        # =========================================================

        atr = float(
            regime["atr"]
        )

        if atr <= 0:
            return None

        # =========================================================
        # CURRENT PRICE
        # =========================================================

        tick = mt5.symbol_info_tick(
            symbol
        )

        info = mt5.symbol_info(
            symbol
        )

        if tick is None or info is None:
            return None

        entry = (
            float(tick.ask)
            if h1_direction == BUY
            else float(tick.bid)
        )

        # =========================================================
        # STOP
        # =========================================================

        sl = self.calculate_stop(
            symbol,
            h1_direction,
            h1,
            atr
        )

        if sl <= 0:
            return None

        risk_distance = abs(
            entry - sl
        )

        if risk_distance <= 0:
            return None

        # =========================================================
        # TP R-MULTIPLES
        # =========================================================

        tp_cfg = self.config[
            "take_profit"
        ]

        tp1_r = float(
            tp_cfg["tp1_r"]
        )

        tp2_r = float(
            tp_cfg["tp2_r"]
        )

        tp3_r = float(
            tp_cfg["tp3_r"]
        )

        if h1_direction == BUY:

            tp1 = (
                entry +
                risk_distance *
                tp1_r
            )

            tp2 = (
                entry +
                risk_distance *
                tp2_r
            )

            tp3 = (
                entry +
                risk_distance *
                tp3_r
            )

        else:

            tp1 = (
                entry -
                risk_distance *
                tp1_r
            )

            tp2 = (
                entry -
                risk_distance *
                tp2_r
            )

            tp3 = (
                entry -
                risk_distance *
                tp3_r
            )

        # =========================================================
        # BROKER STOP VALIDATION
        # =========================================================

        if not self.orders.validate_stops(
            symbol,
            h1_direction,
            entry,
            sl,
            tp3
        ):

            logger.info(
                f"[STOPS] {symbol}: "
                "niveaux incompatibles broker."
            )

            return None

        # =========================================================
        # RISK
        # =========================================================

        risk_percent = float(
            self.config[
                "risk_management"
            ]["risk_percent"]
        )

        volume = (
            self.risk.calculate_volume(
                symbol,
                h1_direction,
                entry,
                sl,
                risk_percent
            )
        )

        if volume <= 0:

            return None

        # =========================================================
        # OPEN RISK LIMIT
        # =========================================================

        new_risk_money = (
            self.risk.new_trade_risk_money(
                symbol,
                h1_direction,
                volume,
                entry,
                sl
            )
        )

        existing_risk = (
            self.risk.open_risk_money()
        )

        account_balance = (
            self.risk.balance()
        )

        maximum_open_risk = (
            account_balance *
            self.config[
                "risk_management"
            ]["max_open_risk_percent"]
            /
            100
        )

        if (
            existing_risk +
            new_risk_money
            >
            maximum_open_risk
        ):

            logger.info(
                f"[OPEN RISK] {symbol}: "
                f"existant={existing_risk:.2f} "
                f"nouveau={new_risk_money:.2f} "
                f"max={maximum_open_risk:.2f}"
            )

            return None

        # =========================================================
        # SIGNAL TIMESTAMP
        # =========================================================

        closed_h1_time = (
            h1.index[-1]
        )

        signal_timestamp = (
            closed_h1_time
            .isoformat()
        )

        fingerprint = (
            f"{symbol}|"
            f"{h1_direction}|"
            f"{signal_timestamp}|"
            f"{round(combined_score, 2)}"
        )

        previous_fingerprint = (
            self.state.get_last_signal(
                symbol
            )
        )

        if (
            self.config[
                "risk_management"
            ]["one_trade_per_signal"]
            and
            previous_fingerprint
            ==
            fingerprint
        ):

            logger.info(
                f"[SIGNAL] {symbol}: "
                "signal déjà traité."
            )

            return None

        # =========================================================
        # SETUP
        # =========================================================

        setup = TradeSetup(
            symbol=symbol,
            direction=h1_direction,
            entry_price=round(
                entry,
                info.digits
            ),
            sl_price=round(
                sl,
                info.digits
            ),
            tp1_price=round(
                tp1,
                info.digits
            ),
            tp2_price=round(
                tp2,
                info.digits
            ),
            tp3_price=round(
                tp3,
                info.digits
            ),
            total_volume=volume,
            risk_percent=risk_percent,
            score=combined_score,
            quality=quality,
            regime=regime["regime"],
            h4_direction=h4_direction,
            h1_direction=h1_direction,
            m15_direction=m15_direction,
            atr=atr,
            adx=regime["adx"],
            atr_percentile=
                regime[
                    "atr_percentile"
                ],
            session=session_name,
            signal_time=
                signal_timestamp
        )

        self.state.set_last_signal(
            symbol,
            fingerprint
        )

        self.logger.log_signal({
            "timestamp":
                signal_timestamp,
            "symbol":
                symbol,
            "accepted":
                True,
            "reason":
                "H4_H1_M15_ALIGNED",
            "regime":
                setup.regime,
            "h4_direction":
                setup.h4_direction,
            "h1_direction":
                setup.h1_direction,
            "m15_direction":
                setup.m15_direction,
            "score":
                setup.score,
            "quality":
                setup.quality,
            "adx":
                setup.adx,
            "atr":
                setup.atr,
            "atr_percentile":
                setup.atr_percentile,
            "session":
                setup.session
        })

        return setup

    # -------------------------------------------------------------

    def execute(
        self,
        setup: TradeSetup
    ) -> bool:

        # =========================================================
        # TEST MODE
        # =========================================================

        if TEST_MODE:

            logger.info(
                "=================================================="
            )

            logger.info(
                "[TEST MODE] SETUP VALIDÉ"
            )

            logger.info(
                f"Symbol  : {setup.symbol}"
            )

            logger.info(
                f"Direction: {setup.direction}"
            )

            logger.info(
                f"Entry   : {setup.entry_price}"
            )

            logger.info(
                f"SL      : {setup.sl_price}"
            )

            logger.info(
                f"TP1     : {setup.tp1_price}"
            )

            logger.info(
                f"TP2     : {setup.tp2_price}"
            )

            logger.info(
                f"TP3     : {setup.tp3_price}"
            )

            logger.info(
                f"Volume  : {setup.total_volume}"
            )

            logger.info(
                f"Risk    : {setup.risk_percent:.2f}%"
            )

            logger.info(
                f"Score   : {setup.score:.2f}"
            )

            logger.info(
                f"Quality : {setup.quality:.2%}"
            )

            logger.info(
                "=================================================="
            )

            self.log_trade(
                setup,
                "TEST_MODE"
            )

            self.send_telegram(
                setup,
                "TEST MODE - SETUP"
            )

            return True

        # =========================================================
        # LIVE ORDER
        # =========================================================

        result = self.orders.send_order(
            setup
        )

        if result is None:

            logger.error(
                f"[TRADE] "
                f"{setup.symbol}: "
                "ordre non exécuté."
            )

            return False

        # ---------------------------------------------------------
        # Récupérer la position
        # ---------------------------------------------------------

        position = None

        for _ in range(10):

            time.sleep(0.3)

            positions = (
                self.positions()
            )

            candidates = [
                p
                for p in positions
                if (
                    p.symbol ==
                    setup.symbol
                    and
                    (
                        (
                            setup.direction ==
                            BUY
                            and
                            p.type ==
                            mt5.POSITION_TYPE_BUY
                        )
                        or
                        (
                            setup.direction ==
                            SELL
                            and
                            p.type ==
                            mt5.POSITION_TYPE_SELL
                        )
                    )
                )
            ]

            if candidates:

                candidates.sort(
                    key=lambda p:
                        getattr(
                            p,
                            "time",
                            0
                        ),
                    reverse=True
                )

                position = (
                    candidates[0]
                )

                break

        if position is None:

            logger.error(
                f"[TRADE] Position "
                f"{setup.symbol} "
                "introuvable après exécution."
            )

            return False

        # ---------------------------------------------------------
        # State
        # ---------------------------------------------------------

        state = PositionState(
            ticket=int(
                position.ticket
            ),
            symbol=setup.symbol,
            direction=setup.direction,
            entry_price=float(
                position.price_open
            ),
            initial_sl=float(
                setup.sl_price
            ),
            initial_risk_distance=abs(
                setup.entry_price -
                setup.sl_price
            ),
            tp1_price=float(
                setup.tp1_price
            ),
            tp2_price=float(
                setup.tp2_price
            ),
            tp3_price=float(
                setup.tp3_price
            ),
            initial_volume=float(
                position.volume
            ),
            tp1_hit=False,
            tp2_hit=False,
            partial_management=(
                float(
                    position.volume
                )
                >=
                float(
                    mt5.symbol_info(
                        setup.symbol
                    ).volume_min
                ) * 3
            ),
            created_at=
                datetime.utcnow().isoformat()
        )

        self.state.set_position(
            state
        )

        self.log_trade(
            setup,
            "TRADE_OPENED"
        )

        self.send_telegram(
            setup,
            "TRADE OUVERT"
        )

        logger.info(
            f"[TRADE] {setup.symbol} "
            f"{setup.direction} "
            f"ticket={position.ticket}"
        )

        return True

    # -------------------------------------------------------------

    def log_trade(
        self,
        setup: TradeSetup,
        reason: str
    ):

        self.logger.log_trade({

            "timestamp":
                datetime.utcnow().isoformat(),

            "symbol":
                setup.symbol,

            "direction":
                setup.direction,

            "regime":
                setup.regime,

            "h4_direction":
                setup.h4_direction,

            "h1_direction":
                setup.h1_direction,

            "m15_direction":
                setup.m15_direction,

            "score":
                setup.score,

            "quality":
                setup.quality,

            "atr":
                setup.atr,

            "adx":
                setup.adx,

            "atr_percentile":
                setup.atr_percentile,

            "session":
                setup.session,

            "entry":
                setup.entry_price,

            "sl":
                setup.sl_price,

            "tp1":
                setup.tp1_price,

            "tp2":
                setup.tp2_price,

            "tp3":
                setup.tp3_price,

            "risk_percent":
                setup.risk_percent,

            "volume":
                setup.total_volume,

            "reason":
                reason
        })

    # -------------------------------------------------------------

    def send_telegram(
        self,
        setup: TradeSetup,
        status: str
    ):

        message = (
            f"[EA V7] {status}\n\n"
            f"{setup.symbol} "
            f"{setup.direction}\n"
            f"Regime: {setup.regime}\n"
            f"Session: {setup.session}\n\n"
            f"Entry: {setup.entry_price}\n"
            f"SL: {setup.sl_price}\n"
            f"TP1: {setup.tp1_price}\n"
            f"TP2: {setup.tp2_price}\n"
            f"TP3: {setup.tp3_price}\n\n"
            f"Score: {setup.score:.2f}/"
            f"{self.config['signal_engine']['maximum_score']:.2f}\n"
            f"Quality: {setup.quality:.1%}\n"
            f"Risk: {setup.risk_percent:.2f}%\n"
            f"Volume: {setup.total_volume}\n"
            f"ADX: {setup.adx:.2f}\n"
            f"ATR percentile: "
            f"{setup.atr_percentile:.1%}"
        )

        self.telegram.send(
            message
        )


# =================================================================
# EXPERT ADVISOR
# =================================================================

class ExpertAdvisor:

    def __init__(
        self,
        config: Dict
    ):

        self.config = config

        self.symbols = [
            symbol
            for symbol, settings
            in config[
                "symbols"
            ].items()
            if settings.get(
                "enabled",
                True
            )
        ]

        self.connection = (
            MT5Connection(
                config
            )
        )

        self.engine = (
            TradingEngine(
                config
            )
        )

        self.running = False

        self.last_h1_bar = {}

    # -------------------------------------------------------------

    def start(self):

        logger.info(
            "=========================================================="
        )

        logger.info(
            "EA TRADING V7.0 PROFESSIONAL"
        )

        logger.info(
            "BTCUSD / XAUUSD"
        )

        logger.info(
            "=========================================================="
        )

        logger.info(
            f"Magic Number : {MAGIC_NUMBER}"
        )

        logger.info(
            f"Symbols      : {self.symbols}"
        )

        logger.info(
            "Architecture : H4 -> H1 -> M15"
        )

        logger.info(
            f"TEST MODE    : {TEST_MODE}"
        )

        logger.info(
            "=========================================================="
        )

        if not self.connection.initialize():

            raise RuntimeError(
                "Connexion MT5 impossible."
            )

        self._check_account_permissions()

        self.running = True

        self.loop()

    # -------------------------------------------------------------

    def _check_account_permissions(
        self
    ):

        terminal = mt5.terminal_info()
        account = mt5.account_info()

        if terminal:

            logger.info(
                f"Terminal trade_allowed: "
                f"{getattr(terminal, 'trade_allowed', 'N/A')}"
            )

        if account:

            logger.info(
                f"Account trade_allowed: "
                f"{getattr(account, 'trade_allowed', 'N/A')}"
            )

            logger.info(
                f"Account margin_mode: "
                f"{getattr(account, 'margin_mode', 'N/A')}"
            )

    # -------------------------------------------------------------

    def loop(self):

        interval = 5

        while self.running:

            try:

                if not self.connection.check():

                    time.sleep(30)

                    continue

                # -------------------------------------------------
                # MANAGEMENT
                # -------------------------------------------------

                self.engine.position_manager.manage()

                # -------------------------------------------------
                # NEW SIGNALS
                # -------------------------------------------------

                for symbol in self.symbols:

                    self.process_symbol(
                        symbol
                    )

                time.sleep(
                    interval
                )

            except KeyboardInterrupt:

                self.stop()

            except Exception as error:

                logger.error(
                    f"[LOOP] Exception: "
                    f"{error}"
                )

                logger.error(
                    traceback.format_exc()
                )

                time.sleep(30)

    # -------------------------------------------------------------

    def process_symbol(
        self,
        symbol: str
    ):

        timestamp = (
            MarketData.latest_closed_timestamp(
                symbol,
                mt5.TIMEFRAME_H1
            )
        )

        if timestamp is None:
            return

        if (
            self.last_h1_bar.get(
                symbol
            )
            ==
            timestamp
        ):

            return

        self.last_h1_bar[
            symbol
        ] = timestamp

        readable_time = (
            datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc
            ).isoformat()
        )

        logger.info(
            f"[NEW H1] {symbol} "
            f"| closed={readable_time}"
        )

        setup = (
            self.engine.build_setup(
                symbol
            )
        )

        if setup is None:

            logger.info(
                f"[NO TRADE] {symbol}"
            )

            return

        logger.info(
            "----------------------------------------------------------"
        )

        logger.info(
            f"[SETUP] {symbol} "
            f"{setup.direction}"
        )

        logger.info(
            f"Regime={setup.regime}"
        )

        logger.info(
            f"H4={setup.h4_direction} "
            f"H1={setup.h1_direction} "
            f"M15={setup.m15_direction}"
        )

        logger.info(
            f"Score={setup.score:.2f} "
            f"Quality={setup.quality:.2%}"
        )

        logger.info(
            f"Entry={setup.entry_price} "
            f"SL={setup.sl_price}"
        )

        logger.info(
            f"TP1={setup.tp1_price} "
            f"TP2={setup.tp2_price} "
            f"TP3={setup.tp3_price}"
        )

        logger.info(
            f"Volume={setup.total_volume} "
            f"Risk={setup.risk_percent:.2f}%"
        )

        logger.info(
            "----------------------------------------------------------"
        )

        self.engine.execute(
            setup
        )

    # -------------------------------------------------------------

    def stop(self):

        if not self.running:
            return

        self.running = False

        try:

            mt5.shutdown()

        except Exception:
            pass

        logger.info(
            "EA V7 arrêté."
        )


# =================================================================
# MAIN
# =================================================================

def main():

    try:

        ea = ExpertAdvisor(
            CONFIG
        )

        ea.start()

    except KeyboardInterrupt:

        logger.info(
            "Arrêt demandé par utilisateur."
        )

    except Exception as error:

        logger.error(
            f"[FATAL] {error}"
        )

        logger.error(
            traceback.format_exc()
        )

    finally:

        try:

            mt5.shutdown()

        except Exception:
            pass


# =================================================================
# ENTRY POINT
# =================================================================

if __name__ == "__main__":

    main()