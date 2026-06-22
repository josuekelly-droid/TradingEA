"""
Expert Advisor Professionnel - BTC/USD & Gold/USD
Auteur: Trading System Pro
Version: 4.1 - Pro (Full Config Driven)
- Tous les paramètres sont lus depuis config.json
- Intégration du calendrier économique MT5 (Filtre FED/NFP)
- Gestion du risque basée sur l'ATR (Trailing & Break-Even)
- Exécution à la clôture de la bougie (New Bar Detection)
- Correction des modes de remplissage MT5 (FOK/IOC)
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import requests
from typing import Dict, List, Tuple, Optional
import MetaTrader5 as mt5
from dataclasses import dataclass
import logging
import json
import time
import os
import traceback

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ea_trading.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class TradingConfig:
    """Configuration de trading dynamique"""
    symbol: str
    timeframe: str
    lot_size: float
    max_spread: float
    risk_percent: float
    magic_number: int
    
@dataclass
class TradeSetup:
    """Structure d'un setup de trade"""
    symbol: str
    direction: str
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    lot_sizes: List[float]
    confidence: float
    timestamp: datetime

class MT5EconomicCalendar:
    """Gestionnaire du calendrier économique natif MT5 (FED, NFP, etc.)"""
    
    def __init__(self, minutes_before: int = 30, minutes_after: int = 30):
        self.minutes_before = minutes_before
        self.minutes_after = minutes_after
        self.enabled = True
        
    def is_blackout_period(self, symbol: str) -> bool:
        """Vérifie si on est dans une période d'interdiction de trading (News à fort impact)"""
        if not self.enabled:
            return False
            
        try:
            now = datetime.now()
            start = now - timedelta(hours=2)
            end = now + timedelta(hours=2)
            
            events = mt5.calendar_events(start, end)
            if events is None:
                return False
                
            for event in events:
                if event.importance == 3:  # 3 = High Impact
                    event_time = event.time
                    time_diff = (now - event_time).total_seconds() / 60
                    
                    if -self.minutes_before <= time_diff < 0:
                        logger.warning(f"[NEWS BLACKOUT] Event {event.name} imminent. Trading suspendu.")
                        return True
                    elif 0 <= time_diff < self.minutes_after:
                        logger.warning(f"[NEWS BLACKOUT] Event {event.name} en cours. Trading suspendu.")
                        return True
        except Exception as e:
            logger.error(f"Erreur calendrier économique: {e}")
            
        return False

class NewsAnalyzer:
    """Analyseur de nouvelles economiques avec NewsAPI.org - Pour le sentiment"""
    
    def __init__(self, config: Dict = None):
        if isinstance(config, str):
            self.api_key = config
            self.enabled = True if config and config != "YOUR_API_KEY" else False
        elif isinstance(config, dict):
            self.api_key = config.get('api_key', '')
            self.enabled = config.get('enabled', False)
        else:
            self.api_key = ''
            self.enabled = False
        
        self.keywords = {
            'BTCUSD': ['bitcoin', 'BTC', 'cryptocurrency', 'crypto', 'blockchain', 'digital currency', 'Bitcoin ETF', 'SEC crypto'],
            'XAUUSD': ['gold', 'XAUUSD', 'precious metals', 'gold price', 'Federal Reserve', 'interest rates', 'inflation', 'central bank gold']
        }
        self.cache = {}
        self.cache_duration = 1800
        self.daily_requests = 0
        self.max_daily_requests = 90
        self.last_reset = datetime.now().date()
        self.last_api_call = {}
        self.min_interval = 900
        
    def fetch_news(self, symbol: str) -> pd.DataFrame:
        if datetime.now().date() > self.last_reset:
            self.daily_requests = 0
            self.last_reset = datetime.now().date()
            self.last_api_call = {}
        
        cache_key = f"{symbol}_{datetime.now().strftime('%Y%m%d_%H')}"
        if cache_key in self.cache:
            cache_time = self.cache[cache_key]['timestamp']
            if (datetime.now() - cache_time).seconds < self.cache_duration:
                return self.cache[cache_key]['data']
        
        if not self.enabled or not self.api_key:
            return pd.DataFrame()
            
        try:
            keywords = self.keywords.get(symbol, ['forex', 'trading'])
            query = ' OR '.join(keywords[:3])
            url = "https://newsapi.org/v2/everything"
            params = {
                'q': query, 'apiKey': self.api_key, 'language': 'en',
                'sortBy': 'publishedAt', 'pageSize': 20,
                'from': (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d')
            }
            
            self.daily_requests += 1
            self.last_api_call[symbol] = datetime.now()
            response = requests.get(url, params=params)
            
            if response.status_code == 200:
                news_data = response.json()
                if news_data['status'] == 'ok' and news_data['totalResults'] > 0:
                    articles = news_data['articles']
                    df = pd.DataFrame(articles)
                    df['publishedAt'] = pd.to_datetime(df['publishedAt'])
                    df['symbol'] = symbol
                    df['source_score'] = df['source'].apply(
                        lambda x: self._get_source_score(x['name']) if isinstance(x, dict) else 0
                    )
                    self.cache[cache_key] = {'timestamp': datetime.now(), 'data': df}
                    return df
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"[ERREUR] Recuperation news: {e}")
            return pd.DataFrame()

    def _get_source_score(self, source_name: str) -> float:
        high_quality = ['reuters', 'bloomberg', 'financial times', 'cnbc', 'wall street journal', 'marketwatch']
        medium_quality = ['coindesk', 'cointelegraph', 'fxstreet', 'dailyfx', 'forexlive', 'kitco']
        source_lower = source_name.lower()
        if any(hq in source_lower for hq in high_quality): return 2.0
        elif any(mq in source_lower for mq in medium_quality): return 1.5
        return 1.0

    def analyze_news_impact(self, symbol: str) -> Dict:
        if not self.enabled:
            return {'impact_score': 0, 'bias': 'neutral', 'high_impact_events': 0, 'total_articles': 0}
        
        news_df = self.fetch_news(symbol)
        if news_df.empty:
            return {'impact_score': 0, 'bias': 'neutral', 'high_impact_events': 0, 'total_articles': 0}
        
        sentiment_score = 0
        high_impact_count = 0
        
        for _, article in news_df.iterrows():
            title = str(article.get('title', '')).lower()
            description = str(article.get('description', '')).lower()
            content = title + ' ' + description
            
            positive_words = ['bullish', 'surge', 'rally', 'breakthrough', 'gain', 'rise', 'positive', 'growth', 'boost', 'strong', 'adoption', 'etf approved']
            negative_words = ['bearish', 'crash', 'plunge', 'decline', 'fall', 'drop', 'negative', 'loss', 'weak', 'ban', 'regulation', 'crackdown', 'hack']
            
            article_score = (sum(1 for word in positive_words if word in content) - sum(1 for word in negative_words if word in content)) * article.get('source_score', 1)
            sentiment_score += article_score
            if article.get('source_score', 0) >= 2.0: high_impact_count += 1
        
        max_score = max(len(news_df) * 5, 1)
        normalized_score = sentiment_score / max_score
        bias = 'bullish' if normalized_score > 0.1 else 'bearish' if normalized_score < -0.1 else 'neutral'
        
        return {'impact_score': abs(normalized_score), 'bias': bias, 'high_impact_events': high_impact_count, 'total_articles': len(news_df)}

class ProfessionalIndicators:
    """Indicateurs techniques professionnels"""
    
    @staticmethod
    def calculate_supertrend(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
        atr = ProfessionalIndicators.calculate_atr(high, low, close, period)
        hl2 = (high + low) / 2
        upper_band = hl2 + (multiplier * atr)
        lower_band = hl2 - (multiplier * atr)
        
        supertrend = pd.Series(index=close.index, dtype=float)
        direction = pd.Series(index=close.index, dtype=int)
        
        for i in range(1, len(close)):
            if close.iloc[i] > upper_band.iloc[i-1]: direction.iloc[i] = 1
            elif close.iloc[i] < lower_band.iloc[i-1]: direction.iloc[i] = -1
            else:
                direction.iloc[i] = direction.iloc[i-1]
                if direction.iloc[i] == 1 and lower_band.iloc[i] < lower_band.iloc[i-1]: lower_band.iloc[i] = lower_band.iloc[i-1]
                if direction.iloc[i] == -1 and upper_band.iloc[i] > upper_band.iloc[i-1]: upper_band.iloc[i] = upper_band.iloc[i-1]
            supertrend.iloc[i] = lower_band.iloc[i] if direction.iloc[i] == 1 else upper_band.iloc[i]
        
        return pd.DataFrame({'supertrend': supertrend, 'direction': direction, 'upper_band': upper_band, 'lower_band': lower_band})
    
    @staticmethod
    def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def calculate_ichimoku(high: pd.Series, low: pd.Series, close: pd.Series) -> Dict:
        period9_high = high.rolling(window=9).max()
        period9_low = low.rolling(window=9).min()
        tenkan_sen = (period9_high + period9_low) / 2
        
        period26_high = high.rolling(window=26).max()
        period26_low = low.rolling(window=26).min()
        kijun_sen = (period26_high + period26_low) / 2
        
        senkou_span_a = ((tenkan_sen + kijun_sen) / 2).shift(26)
        period52_high = high.rolling(window=52).max()
        period52_low = low.rolling(window=52).min()
        senkou_span_b = ((period52_high + period52_low) / 2).shift(26)
        chikou_span = close.shift(-26)
        
        return {'tenkan_sen': tenkan_sen, 'kijun_sen': kijun_sen, 'senkou_span_a': senkou_span_a, 'senkou_span_b': senkou_span_b, 'chikou_span': chikou_span}
    
    @staticmethod
    def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))
    
    @staticmethod
    def calculate_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict:
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        return {'macd': macd_line, 'signal': signal_line, 'histogram': macd_line - signal_line}
    
    @staticmethod
    def calculate_bollinger_bands(close: pd.Series, period: int = 20, std_dev: float = 2.0) -> Dict:
        sma = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        upper_band = sma + (std * std_dev)
        lower_band = sma - (std * std_dev)
        return {'middle': sma, 'upper': upper_band, 'lower': lower_band, 'width': (upper_band - lower_band) / sma * 100}

class SessionAnalyzer:
    """Analyseur de sessions de marche"""
    def __init__(self):
        self.sessions = {'asian': {'start': 0, 'end': 9, 'name': 'Asian'}, 'london': {'start': 8, 'end': 17, 'name': 'London'}, 'us': {'start': 13, 'end': 22, 'name': 'US'}, 'overlap_london_us': {'start': 13, 'end': 17, 'name': 'London-US Overlap'}}
        self.session_characteristics = {
            'BTCUSD': {'us': {'volatility': 'high', 'volume': 'high', 'weight': 1.5}, 'london': {'volatility': 'medium', 'volume': 'medium', 'weight': 1.0}, 'asian': {'volatility': 'low', 'volume': 'low', 'weight': 0.7}},
            'XAUUSD': {'us': {'volatility': 'high', 'volume': 'high', 'weight': 1.4}, 'london': {'volatility': 'high', 'volume': 'high', 'weight': 1.3}, 'asian': {'volatility': 'low', 'volume': 'low', 'weight': 0.5}}
        }
    def get_current_session(self, timestamp: datetime) -> Dict:
        hour = timestamp.hour
        for session_key, session_data in self.sessions.items():
            if session_data['start'] <= hour < session_data['end']:
                return {'session': session_key, 'name': session_data['name'], 'is_active': True}
        return {'session': None, 'name': 'No Session', 'is_active': False}
    def get_session_weight(self, symbol: str, session: str) -> float:
        return self.session_characteristics.get(symbol, {}).get(session, {}).get('weight', 1.0)

class RiskManager:
    """Gestionnaire de risque professionnel - Piloté par config.json"""
    def __init__(self, account_balance: float, risk_config: Dict, tp_sl_config: Dict):
        self.account_balance = account_balance
        self.risk_percent = risk_config.get('risk_percent', 1.0)
        self.max_daily_loss = risk_config.get('max_daily_loss_percent', 2.0)
        self.max_positions = risk_config.get('max_positions', 3)
        self.tp_sl_config = tp_sl_config
        
    def calculate_position_size(self, entry_price: float, stop_loss: float, symbol: str) -> float:
        risk_amount = self.account_balance * (self.risk_percent / 100)
        stop_distance = abs(entry_price - stop_loss)
        pip_value = self._get_pip_value(symbol)
        lot_size = risk_amount / (stop_distance * pip_value)
        max_lot = self._get_max_lot_size(symbol)
        return min(lot_size, max_lot)
    
    def calculate_tp_levels(self, entry_price: float, direction: str, atr_value: float) -> List[float]:
        tp1_mult = self.tp_sl_config.get('tp1_atr_multiplier', 1.5)
        tp2_mult = self.tp_sl_config.get('tp2_atr_multiplier', 3.0)
        tp3_mult = self.tp_sl_config.get('tp3_atr_multiplier', 5.0)
        
        if direction == 'BUY':
            return [entry_price + (atr_value * tp1_mult), entry_price + (atr_value * tp2_mult), entry_price + (atr_value * tp3_mult)]
        else:
            return [entry_price - (atr_value * tp1_mult), entry_price - (atr_value * tp2_mult), entry_price - (atr_value * tp3_mult)]
    
    def calculate_stop_loss(self, entry_price: float, direction: str, atr_value: float, support_resistance: Dict) -> float:
        sl_mult = self.tp_sl_config.get('sl_atr_multiplier', 2.0)
        atr_sl = atr_value * sl_mult
        if direction == 'BUY':
            sr_sl = support_resistance.get('support', entry_price - atr_sl)
            return min(entry_price - atr_sl, sr_sl)
        else:
            sr_sl = support_resistance.get('resistance', entry_price + atr_sl)
            return max(entry_price + atr_sl, sr_sl)
    
    def _get_pip_value(self, symbol: str) -> float:
        return {'BTCUSD': 1.0, 'XAUUSD': 10.0}.get(symbol, 1.0)
    
    def _get_max_lot_size(self, symbol: str) -> float:
        return {'BTCUSD': 1.0, 'XAUUSD': 10.0}.get(symbol, 1.0)

class TradingEngine:
    """Moteur de trading principal - Version Pro"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.indicators = ProfessionalIndicators()
        self.news_analyzer = NewsAnalyzer(config.get('news_api', {}))
        self.mt5_calendar = MT5EconomicCalendar(minutes_before=30, minutes_after=30)
        self.session_analyzer = SessionAnalyzer()
        self.risk_manager = None
        self._initialize_mt5(config.get('mt5_credentials', {}))
        self.data_cache = {}
        
    def _initialize_mt5(self, credentials: Dict) -> bool:
        if not credentials:
            logger.error("Credentials MT5 non fournis")
            return False
        if not mt5.initialize():
            logger.error("Echec d'initialisation de MT5")
            return False
        authorized = mt5.login(credentials['login'], password=credentials['password'], server=credentials['server'])
        if not authorized:
            logger.error(f"Echec d'authentification MT5: {mt5.last_error()}")
            return False
        logger.info("Connexion MT5 reussie")
        return True
    
    def fetch_market_data(self, symbol: str, timeframe: str, num_bars: int = 500) -> pd.DataFrame:
        try:
            rates = mt5.copy_rates_from_pos(symbol, self._get_timeframe(timeframe), 0, num_bars)
            if rates is None: return pd.DataFrame()
            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
            return df
        except Exception as e:
            logger.error(f"Erreur: {e}")
            return pd.DataFrame()
    
    def _get_timeframe(self, timeframe: str):
        timeframes = {'M1': mt5.TIMEFRAME_M1, 'M5': mt5.TIMEFRAME_M5, 'M15': mt5.TIMEFRAME_M15, 'M30': mt5.TIMEFRAME_M30, 'H1': mt5.TIMEFRAME_H1, 'H4': mt5.TIMEFRAME_H4, 'D1': mt5.TIMEFRAME_D1}
        return timeframes.get(timeframe, mt5.TIMEFRAME_H1)
    
    def analyze_market_conditions(self, symbol: str, data: pd.DataFrame) -> Dict:
        # Récupération des paramètres des indicateurs depuis config.json
        ind_cfg = self.config.get('indicators', {})
        
        supertrend = self.indicators.calculate_supertrend(
            data['high'], data['low'], data['close'], 
            period=ind_cfg.get('supertrend_period', 10), 
            multiplier=ind_cfg.get('supertrend_multiplier', 3.0)
        )
        ichimoku = self.indicators.calculate_ichimoku(data['high'], data['low'], data['close'])
        rsi = self.indicators.calculate_rsi(data['close'], period=ind_cfg.get('rsi_period', 14))
        macd = self.indicators.calculate_macd(
            data['close'], 
            fast=ind_cfg.get('macd_fast', 12), 
            slow=ind_cfg.get('macd_slow', 26), 
            signal=ind_cfg.get('macd_signal', 9)
        )
        bb = self.indicators.calculate_bollinger_bands(
            data['close'], 
            period=ind_cfg.get('bollinger_period', 20), 
            std_dev=ind_cfg.get('bollinger_std', 2.0)
        )
        atr_series = self.indicators.calculate_atr(data['high'], data['low'], data['close'])
        
        current_time = datetime.now()
        session_info = self.session_analyzer.get_current_session(current_time)
        news_analysis = self.news_analyzer.analyze_news_impact(symbol)
        trade_score = self._calculate_trade_score(data, supertrend, ichimoku, rsi, macd, bb, session_info, news_analysis, symbol)
        
        return {
            'supertrend': supertrend, 'ichimoku': ichimoku, 'rsi': rsi.iloc[-1], 'macd': macd,
            'bollinger_bands': bb, 'atr': atr_series.iloc[-1], 'atr_series': atr_series, 
            'session': session_info, 'news': news_analysis, 'trade_score': trade_score, 'current_price': data['close'].iloc[-1]
        }
    
    def _calculate_trade_score(self, data: pd.DataFrame, supertrend: pd.DataFrame, ichimoku: Dict, rsi: pd.Series, macd: Dict, bb: Dict, session: Dict, news: Dict, symbol: str) -> Dict:
        score = 0
        signals = []
        max_score = 10
        
        if supertrend['direction'].iloc[-1] == 1: score += 1; signals.append("SuperTrend haussier")
        else: score -= 1; signals.append("SuperTrend baissier")
        
        if (ichimoku['tenkan_sen'].iloc[-1] > ichimoku['kijun_sen'].iloc[-1] and data['close'].iloc[-1] > ichimoku['senkou_span_a'].iloc[-1]): score += 2; signals.append("Ichimoku haussier")
        elif (ichimoku['tenkan_sen'].iloc[-1] < ichimoku['kijun_sen'].iloc[-1] and data['close'].iloc[-1] < ichimoku['senkou_span_b'].iloc[-1]): score -= 2; signals.append("Ichimoku baissier")
        
        if 30 < rsi.iloc[-1] < 70:
            if rsi.iloc[-1] > 50: score += 0.5; signals.append("RSI momentum positif")
            else: score -= 0.5; signals.append("RSI momentum negatif")
        
        if macd['histogram'].iloc[-1] > 0 and macd['histogram'].iloc[-1] > macd['histogram'].iloc[-2]: score += 1; signals.append("MACD momentum haussier")
        elif macd['histogram'].iloc[-1] < 0 and macd['histogram'].iloc[-1] < macd['histogram'].iloc[-2]: score -= 1; signals.append("MACD momentum baissier")
        
        bb_position = (data['close'].iloc[-1] - bb['lower'].iloc[-1]) / (bb['upper'].iloc[-1] - bb['lower'].iloc[-1])
        if bb_position < 0.2: score += 1; signals.append("Prix proche bande inferieure BB")
        elif bb_position > 0.8: score -= 1; signals.append("Prix proche bande superieure BB")
        
        session_weight = self.session_analyzer.get_session_weight(symbol, session['session'])
        if session['session'] == 'us': score += 2 * session_weight; signals.append("Session US active")
        elif session['session'] == 'overlap_london_us': score += 2.5 * session_weight; signals.append("Overlap London-US")
        
        if news['impact_score'] > 0:
            if news['bias'] == 'bullish': score += 1; signals.append("News haussieres")
            elif news['bias'] == 'bearish': score -= 1; signals.append("News baissieres")
        
        normalized_score = max(-max_score, min(max_score, score))
        return {
            'total_score': normalized_score, 'normalized_score': (normalized_score + max_score) / (2 * max_score),
            'direction': 'BUY' if normalized_score > 1 else 'SELL' if normalized_score < -1 else 'NEUTRAL',
            'signals': signals, 'confidence': abs(normalized_score) / max_score
        }
    
    def generate_trade_setup(self, symbol: str, analysis: Dict) -> Optional[TradeSetup]:
        trade_score = analysis['trade_score']
        if trade_score['confidence'] < 0.60: return None
        if trade_score['direction'] == 'NEUTRAL': return None
        
        entry_price = analysis['current_price']
        atr_value = analysis['atr']
        support_resistance = self._find_support_resistance(symbol)
        sl_price = self.risk_manager.calculate_stop_loss(entry_price, trade_score['direction'], atr_value, support_resistance)
        tp_levels = self.risk_manager.calculate_tp_levels(entry_price, trade_score['direction'], atr_value)
        total_lot = self.risk_manager.calculate_position_size(entry_price, sl_price, symbol)
        
        # Récupération de la distribution des lots depuis config.json
        lot_dist = self.risk_manager.tp_sl_config.get('lot_distribution', [0.4, 0.3, 0.3])
        lot_sizes = [total_lot * lot_dist[0], total_lot * lot_dist[1], total_lot * lot_dist[2]]
        
        setup = TradeSetup(symbol=symbol, direction=trade_score['direction'], entry_price=entry_price, sl_price=sl_price, tp1_price=tp_levels[0], tp2_price=tp_levels[1], tp3_price=tp_levels[2], lot_sizes=lot_sizes, confidence=trade_score['confidence'], timestamp=datetime.now())
        logger.info(f"Trade setup genere pour {symbol}: {trade_score['direction']} | Confiance: {trade_score['confidence']:.2%}")
        return setup
    
    def _find_support_resistance(self, symbol: str) -> Dict:
        data = self.fetch_market_data(symbol, 'H1', 200)
        if data.empty: return {'resistance': 0, 'support': 0}
        return {'resistance': data['high'].rolling(20).max().iloc[-1], 'support': data['low'].rolling(20).min().iloc[-1]}
    
    def _send_telegram_alert(self, setup: TradeSetup, analysis: Dict, timeframe: str, telegram_config: Dict):
        """Envoie une alerte Telegram détaillée"""
        try:
            if not telegram_config.get('enabled', False): return
            bot_token = telegram_config.get('bot_token', '')
            chat_id = telegram_config.get('chat_id', '')
            if not bot_token or not chat_id: return
            
            signals = analysis['trade_score']['signals']
            session = analysis['session']
            news = analysis['news']
            rsi_value = analysis['rsi']
            macd_hist = analysis['macd']['histogram'].iloc[-1]
            
            rsi_state = "Surachat" if rsi_value > 70 else "Survente" if rsi_value < 30 else "Haussier" if rsi_value > 50 else "Baissier"
            macd_state = "Haussier" if macd_hist > 0 else "Baissier"
            risk_amount = self.risk_manager.account_balance * (self.risk_manager.risk_percent / 100) if self.risk_manager else 0
            
            message = f"""
[EA PRO] TRADE OUVERT - {setup.timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC

Symbol: {setup.symbol} | Direction: {setup.direction}
Timeframe: {timeframe} | Session: {session['name']}
Prix d'entree: {setup.entry_price:.2f}

Stop Loss: {setup.sl_price:.2f}
Take Profit 1: {setup.tp1_price:.2f}
Take Profit 2: {setup.tp2_price:.2f}
Take Profit 3: {setup.tp3_price:.2f}

Lots: {setup.lot_sizes[0]:.3f} / {setup.lot_sizes[1]:.3f} / {setup.lot_sizes[2]:.3f}
Confiance: {setup.confidence:.1%}

Indicateurs en temps reel:
- SuperTrend: {signals[0] if signals else 'N/A'}
- RSI: {rsi_value:.1f} ({rsi_state})
- MACD: {macd_state} (Histogram: {macd_hist:.1f})
- ATR: {analysis['atr']:.1f}
- News Impact: {news['impact_score']:.2f} ({news['bias']})

Risque: {risk_amount:.2f}$ ({self.risk_manager.risk_percent if self.risk_manager else 1.0}% du capital)
            """
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            params = {'chat_id': chat_id, 'text': message.strip()}
            response = requests.post(url, params=params, timeout=10)
            if response.status_code == 200: logger.info("[TELEGRAM] Alerte envoyee avec succes")
            else: logger.warning(f"[TELEGRAM] Erreur envoi: {response.status_code}")
        except Exception as e:
            logger.error(f"[TELEGRAM] Erreur: {e}")
    
    def execute_trade(self, setup: TradeSetup, analysis: Dict = None, timeframe: str = "H1", telegram_config: Dict = None) -> bool:
        try:
            if not self._pre_trade_checks(setup): return False
            mt5_comment = f"EA_{setup.direction}_{timeframe}"[:31]
            
            symbol_info = mt5.symbol_info(setup.symbol)
            if symbol_info is None: return False
            digits = symbol_info.digits
            vol_min = symbol_info.volume_min
            vol_step = symbol_info.volume_step
            tick = mt5.symbol_info_tick(setup.symbol)
            if tick is None: return False
            
            filling_type = mt5.ORDER_FILLING_FOK
            if (symbol_info.filling_mode & mt5.SYMBOL_FILLING_FOK) == 0:
                filling_type = mt5.ORDER_FILLING_IOC
            
            for i, (tp_price, lot) in enumerate(zip([setup.tp1_price, setup.tp2_price, setup.tp3_price], setup.lot_sizes)):
                order_type = mt5.ORDER_TYPE_BUY if setup.direction == 'BUY' else mt5.ORDER_TYPE_SELL
                price = tick.ask if setup.direction == 'BUY' else tick.bid
                volume = max(vol_min, round(float(lot) / vol_step) * vol_step)
                volume = round(volume, 2)
                
                request = {
                    "action": mt5.TRADE_ACTION_DEAL, "symbol": setup.symbol, "volume": volume,
                    "type": order_type, "price": round(price, digits),
                    "sl": round(float(setup.sl_price), digits), "tp": round(float(tp_price), digits),
                    "deviation": 30, "magic": 123456, "comment": f"{mt5_comment}_TP{i+1}",
                    "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling_type,
                }
                
                result = mt5.order_send(request)
                if result is None:
                    logger.error(f"Erreur execution trade TP{i+1}: {mt5.last_error()}")
                    return False
                if result.retcode != mt5.TRADE_RETCODE_DONE:
                    logger.error(f"Erreur execution TP{i+1}: {result.comment} (code: {result.retcode})")
                    return False
                logger.info(f"[OK] Trade TP{i+1} execute: Ticket {result.order}")
            
            # Envoi de l'alerte Telegram APRÈS exécution réussie
            if analysis and telegram_config:
                self._send_telegram_alert(setup, analysis, timeframe, telegram_config)
                
            return True
        except Exception as e:
            logger.error(f"Erreur execution trade: {e}")
            return False
    
    def _pre_trade_checks(self, setup: TradeSetup) -> bool:
        tick = mt5.symbol_info_tick(setup.symbol)
        if tick is None: return False
        spread = tick.ask - tick.bid
        
        # Récupération du spread max depuis config.json
        max_spread_cfg = self.config.get('risk_management', {}).get('max_spread', {})
        max_spread = max_spread_cfg.get(setup.symbol, 10.0)
        
        if spread > max_spread:
            logger.warning(f"Spread trop eleve: {spread} (max: {max_spread})")
            return False
        
        positions = mt5.positions_get(symbol=setup.symbol)
        if positions is None: positions = []
        if len(positions) >= self.risk_manager.max_positions: 
            logger.warning(f"Limite de positions atteinte pour {setup.symbol}")
            return False
        return True
    
    def manage_open_positions(self):
        positions = mt5.positions_get()
        if positions is None: return
        for position in positions:
            self._manage_single_position(position)

    def _manage_single_position(self, position):
        symbol = position.symbol
        tick = mt5.symbol_info_tick(symbol)
        if tick is None: return
        
        data = self.fetch_market_data(symbol, 'H1', 20)
        if data.empty: return
        current_atr = ProfessionalIndicators.calculate_atr(data['high'], data['low'], data['close']).iloc[-1]
        
        current_price = tick.bid if position.type == 0 else tick.ask
        entry_price = position.price_open
        
        if position.type == 0: profit_value = current_price - entry_price
        else: profit_value = entry_price - current_price
            
        self._apply_trailing_stop(position, current_price, profit_value, current_atr)
        self._apply_break_even(position, current_price, profit_value, current_atr)

    def _apply_trailing_stop(self, position, current_price: float, profit_value: float, atr: float):
        activation_threshold = atr * 1.5
        trailing_distance = atr * 1.0
        
        if profit_value >= activation_threshold:
            if position.type == 0:
                new_sl = current_price - trailing_distance
                if new_sl > position.sl:
                    self._modify_position(position.ticket, new_sl, position.tp)
                    logger.info(f"Trailing Stop appliqué (BUY) sur {position.ticket} à {new_sl}")
            else:
                new_sl = current_price + trailing_distance
                if position.sl == 0 or new_sl < position.sl:
                    self._modify_position(position.ticket, new_sl, position.tp)
                    logger.info(f"Trailing Stop appliqué (SELL) sur {position.ticket} à {new_sl}")

    def _apply_break_even(self, position, current_price: float, profit_value: float, atr: float):
        activation_threshold = atr * 1.0
        be_offset = atr * 0.2
        
        if profit_value >= activation_threshold:
            if position.type == 0: new_sl = position.price_open + be_offset
            else: new_sl = position.price_open - be_offset
            
            if position.type == 0 and (position.sl == 0 or new_sl > position.sl):
                self._modify_position(position.ticket, new_sl, position.tp)
                logger.info(f"Break-Even appliqué (BUY) sur {position.ticket}")
            elif position.type == 1 and (position.sl == 0 or new_sl < position.sl):
                self._modify_position(position.ticket, new_sl, position.tp)
                logger.info(f"Break-Even appliqué (SELL) sur {position.ticket}")

    def _modify_position(self, ticket: int, sl: float, tp: float) -> bool:
        request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": sl, "tp": tp}
        result = mt5.order_send(request)
        if result is None: return False
        return result.retcode == mt5.TRADE_RETCODE_DONE

class ExpertAdvisor:
    """Classe principale de l'Expert Advisor"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.symbols = config['symbols']
        self.timeframes = config['timeframes']
        self.trading_engine = TradingEngine(config)
        self.is_running = False
        
    def start(self):
        logger.info("Demarrage de l'Expert Advisor (Mode Pro)...")
        self.is_running = True
        
        account_info = mt5.account_info()
        if account_info is None:
            logger.error("Impossible de récupérer les infos du compte. Arrêt.")
            return
            
        # Initialisation du RiskManager avec la configuration JSON
        self.trading_engine.risk_manager = RiskManager(
            account_info.balance,
            risk_config=self.config.get('risk_management', {}),
            tp_sl_config=self.config.get('tp_sl_settings', {})
        )
        
        last_bar_time = {}
        
        while self.is_running:
            try:
                for symbol in self.symbols:
                    for tf in self.timeframes:
                        rates = mt5.copy_rates_from_pos(symbol, self.trading_engine._get_timeframe(tf), 0, 2)
                        if rates is None or len(rates) < 2: continue
                        
                        current_bar_time = rates[-1]['time']
                        key = f"{symbol}_{tf}"
                        
                        if last_bar_time.get(key) != current_bar_time:
                            last_bar_time[key] = current_bar_time
                            logger.info(f"Nouvelle bougie {tf} détectée pour {symbol}. Analyse en cours...")
                            self.run_iteration_for_symbol(symbol, tf)
                            
                self.trading_engine.manage_open_positions()
                time.sleep(10)
                
            except KeyboardInterrupt:
                logger.info("Arret demande par l'utilisateur")
                self.stop()
            except Exception as e:
                logger.error(f"Erreur dans la boucle principale: {e}")
                time.sleep(60)
    
    def run_iteration_for_symbol(self, symbol: str, timeframe: str):
        data = self.trading_engine.fetch_market_data(symbol, timeframe, 500)
        if data.empty: return
        
        analysis = self.trading_engine.analyze_market_conditions(symbol, data)
        setup = self.trading_engine.generate_trade_setup(symbol, analysis)
        
        if setup:
            if self.additional_filters(setup, analysis):
                telegram_config = self.config.get('telegram', {})
                self.trading_engine.execute_trade(setup, analysis, timeframe, telegram_config)
    
    def additional_filters(self, setup: TradeSetup, analysis: Dict) -> bool:
        # 1. Filtre de sessions basé sur config.json
        trading_hours = self.config.get('trading_hours', {})
        current_session = analysis['session']['session']
        confidence = analysis['trade_score']['confidence']
        
        allowed_sessions = []
        if trading_hours.get('trade_us_session', True):
            allowed_sessions.extend(['us', 'overlap_london_us'])
        if trading_hours.get('trade_london_session', True):
            allowed_sessions.append('london')
        if trading_hours.get('trade_asian_session', False):
            allowed_sessions.append('asian')
            
        if current_session not in allowed_sessions:
            logger.info(f"Trade bloqué: Session {current_session} non autorisée dans la config.")
            return False
        
        if trading_hours.get('us_session_only_high_confidence', True):
            if current_session not in ['us', 'overlap_london_us'] and confidence < 0.70:
                logger.info("Trade hors session US avec confiance insuffisante (< 0.70)")
                return False
        
        # 2. Filtre de volatilité ATR 
        current_atr = analysis['atr']
        avg_atr = analysis['atr_series'].rolling(20).mean().iloc[-1]
        if current_atr < avg_atr * 0.5: 
            logger.info("Volatilite insuffisante (ATR bas)")
            return False
        
        # 3. Filtre Multi-Timeframe
        if not self.confirm_multi_timeframe(setup.symbol, setup.direction):
            logger.info("Tendance non confirmee sur timeframe superieur")
            return False
            
        # 4. Filtre News FED / NFP 
        if self.trading_engine.mt5_calendar.is_blackout_period(setup.symbol):
            logger.info(f"Trade bloque: News économique à fort impact en cours/à venir.")
            return False
        
        return True
    
    def confirm_multi_timeframe(self, symbol: str, direction: str) -> bool:
        higher_tf_data = self.trading_engine.fetch_market_data(symbol, 'H4', 100)
        if higher_tf_data.empty: return True
        
        ichimoku = ProfessionalIndicators.calculate_ichimoku(
            higher_tf_data['high'], higher_tf_data['low'], higher_tf_data['close'])
        
        close = higher_tf_data['close'].iloc[-1]
        cloud_top = max(ichimoku['senkou_span_a'].iloc[-1], ichimoku['senkou_span_b'].iloc[-1])
        cloud_bottom = min(ichimoku['senkou_span_a'].iloc[-1], ichimoku['senkou_span_b'].iloc[-1])
        
        if direction == 'BUY': return close > cloud_top
        else: return close < cloud_bottom
    
    def stop(self):
        self.is_running = False
        mt5.shutdown()
        logger.info("EA arrete")

if __name__ == "__main__":
    config_path = "config.json"
    
    if not os.path.exists(config_path):
        logger.error(f"Fichier {config_path} non trouve !")
        exit(1)
    
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        logger.info("[OK] Configuration chargee depuis config.json")
    except Exception as e:
        logger.error(f"Erreur lecture config.json: {e}")
        exit(1)
    
    ea = ExpertAdvisor(config)
    
    try:
        ea.start()
    except KeyboardInterrupt:
        logger.info("Arret demande par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
    finally:
        ea.stop()