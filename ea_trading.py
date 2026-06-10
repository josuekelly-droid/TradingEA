"""
Expert Advisor Professionnel - BTC/USD & Gold/USD
Auteur: Trading System Pro
Version: 3.4 - Stable avec corrections d'execution
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import requests
from typing import Dict, List, Tuple, Optional
import MetaTrader5 as mt5
from dataclasses import dataclass
import logging
from concurrent.futures import ThreadPoolExecutor
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
    """Configuration de trading"""
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

class NewsAnalyzer:
    """Analyseur de nouvelles economiques avec NewsAPI.org - Optimise"""
    
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
            'BTCUSD': [
                'bitcoin', 'BTC', 'cryptocurrency', 'crypto', 'blockchain',
                'digital currency', 'Bitcoin ETF', 'Bitcoin halving',
                'SEC crypto', 'crypto regulation', 'BTC price'
            ],
            'XAUUSD': [
                'gold', 'XAUUSD', 'precious metals', 'gold price',
                'Federal Reserve', 'interest rates', 'inflation',
                'geopolitical', 'central bank gold', 'gold reserves'
            ]
        }
        
        self.cache = {}
        self.cache_duration = 1800
        self.daily_requests = 0
        self.max_daily_requests = 90
        self.last_reset = datetime.now().date()
        self.last_api_call = {}
        self.min_interval = 900
        
        logger.info(f"NewsAnalyzer initialise - API: {'Activee' if self.enabled else 'Desactivee'} | Cache: {self.cache_duration//60}min | Interval: {self.min_interval//60}min")
    
    def fetch_economic_calendar(self) -> pd.DataFrame:
        return pd.DataFrame()
    
    def fetch_news(self, symbol: str) -> pd.DataFrame:
        if datetime.now().date() > self.last_reset:
            self.daily_requests = 0
            self.last_reset = datetime.now().date()
            self.last_api_call = {}
            logger.info("[NEWS] Compteur quotidien reinitialise")
        
        cache_key = f"{symbol}_{datetime.now().strftime('%Y%m%d_%H')}"
        if cache_key in self.cache:
            cache_time = self.cache[cache_key]['timestamp']
            if (datetime.now() - cache_time).seconds < self.cache_duration:
                logger.info(f"[CACHE] Utilisation du cache pour {symbol} (valide {self.cache_duration//60}min)")
                return self.cache[cache_key]['data']
        
        if not self.enabled or not self.api_key:
            return pd.DataFrame()
        
        if symbol in self.last_api_call:
            elapsed = (datetime.now() - self.last_api_call[symbol]).seconds
            if elapsed < self.min_interval:
                logger.info(f"[NEWS] Attente {self.min_interval - elapsed}s avant prochaine requete pour {symbol}")
                if cache_key in self.cache:
                    return self.cache[cache_key]['data']
                return pd.DataFrame()
        
        if self.daily_requests >= self.max_daily_requests:
            logger.warning(f"[LIMIT] Limite quotidienne atteinte ({self.daily_requests}/{self.max_daily_requests})")
            return pd.DataFrame()
        
        try:
            keywords = self.keywords.get(symbol, ['forex', 'trading'])
            query = ' OR '.join(keywords[:3])
            
            url = "https://newsapi.org/v2/everything"
            params = {
                'q': query,
                'apiKey': self.api_key,
                'language': 'en',
                'sortBy': 'publishedAt',
                'pageSize': 20,
                'from': (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d')
            }
            
            self.daily_requests += 1
            self.last_api_call[symbol] = datetime.now()
            
            logger.info(f"[NEWS] Requete API pour {symbol} ({self.daily_requests}/{self.max_daily_requests} aujourd'hui)")
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
                    
                    self.cache[cache_key] = {
                        'timestamp': datetime.now(),
                        'data': df
                    }
                    
                    logger.info(f"[OK] {len(df)} news recuperees pour {symbol}")
                    return df
                else:
                    logger.info(f"[INFO] Aucune news trouvee pour {symbol}")
                    return pd.DataFrame()
            else:
                logger.error(f"[ERREUR] API NewsAPI: {response.status_code}")
                return pd.DataFrame()
                
        except Exception as e:
            logger.error(f"[ERREUR] Recuperation news: {e}")
            return pd.DataFrame()
    
    def _get_source_score(self, source_name: str) -> float:
        high_quality = ['reuters', 'bloomberg', 'financial times', 'cnbc', 
                       'wall street journal', 'marketwatch', 'investing.com']
        medium_quality = ['coindesk', 'cointelegraph', 'fxstreet', 'dailyfx',
                         'forexlive', 'kitco', 'seeking alpha']
        
        source_lower = source_name.lower()
        
        if any(hq in source_lower for hq in high_quality):
            return 2.0
        elif any(mq in source_lower for mq in medium_quality):
            return 1.5
        else:
            return 1.0
    
    def analyze_news_impact(self, symbol: str) -> Dict:
        if not self.enabled:
            return {
                'impact_score': 0,
                'bias': 'neutral',
                'high_impact_events': 0,
                'total_articles': 0
            }
        
        news_df = self.fetch_news(symbol)
        
        if news_df.empty:
            return {
                'impact_score': 0,
                'bias': 'neutral',
                'high_impact_events': 0,
                'total_articles': 0
            }
        
        sentiment_score = 0
        high_impact_count = 0
        
        for _, article in news_df.iterrows():
            title = str(article.get('title', '')).lower()
            description = str(article.get('description', '')).lower()
            content = title + ' ' + description
            
            positive_words = [
                'bullish', 'surge', 'rally', 'breakthrough', 'gain', 'rise',
                'positive', 'growth', 'boost', 'outperform', 'strong',
                'adoption', 'institutional', 'etf approved', 'halving'
            ]
            
            negative_words = [
                'bearish', 'crash', 'plunge', 'decline', 'fall', 'drop',
                'negative', 'loss', 'weak', 'underperform', 'ban',
                'regulation', 'crackdown', 'hack', 'scam'
            ]
            
            positive_count = sum(1 for word in positive_words if word in content)
            negative_count = sum(1 for word in negative_words if word in content)
            
            article_score = (positive_count - negative_count) * article.get('source_score', 1)
            sentiment_score += article_score
            
            if article.get('source_score', 0) >= 2.0:
                high_impact_count += 1
        
        max_score = max(len(news_df) * 5, 1)
        normalized_score = sentiment_score / max_score
        
        if normalized_score > 0.1:
            bias = 'bullish'
        elif normalized_score < -0.1:
            bias = 'bearish'
        else:
            bias = 'neutral'
        
        impact_analysis = {
            'impact_score': abs(normalized_score),
            'bias': bias,
            'high_impact_events': high_impact_count,
            'total_articles': len(news_df)
        }
        
        logger.info(f"[NEWS] Analyse {symbol}: impact={impact_analysis['impact_score']:.2f}, bias={bias}")
        
        return impact_analysis

class ProfessionalIndicators:
    """Indicateurs techniques professionnels"""
    
    @staticmethod
    def calculate_supertrend(high: pd.Series, low: pd.Series, close: pd.Series, 
                           period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
        atr = ProfessionalIndicators.calculate_atr(high, low, close, period)
        
        hl2 = (high + low) / 2
        upper_band = hl2 + (multiplier * atr)
        lower_band = hl2 - (multiplier * atr)
        
        supertrend = pd.Series(index=close.index, dtype=float)
        direction = pd.Series(index=close.index, dtype=int)
        
        for i in range(1, len(close)):
            if close.iloc[i] > upper_band.iloc[i-1]:
                direction.iloc[i] = 1
            elif close.iloc[i] < lower_band.iloc[i-1]:
                direction.iloc[i] = -1
            else:
                direction.iloc[i] = direction.iloc[i-1]
                
                if direction.iloc[i] == 1 and lower_band.iloc[i] < lower_band.iloc[i-1]:
                    lower_band.iloc[i] = lower_band.iloc[i-1]
                if direction.iloc[i] == -1 and upper_band.iloc[i] > upper_band.iloc[i-1]:
                    upper_band.iloc[i] = upper_band.iloc[i-1]
            
            supertrend.iloc[i] = lower_band.iloc[i] if direction.iloc[i] == 1 else upper_band.iloc[i]
        
        return pd.DataFrame({
            'supertrend': supertrend,
            'direction': direction,
            'upper_band': upper_band,
            'lower_band': lower_band
        })
    
    @staticmethod
    def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        
        return atr
    
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
        
        return {
            'tenkan_sen': tenkan_sen,
            'kijun_sen': kijun_sen,
            'senkou_span_a': senkou_span_a,
            'senkou_span_b': senkou_span_b,
            'chikou_span': chikou_span
        }
    
    @staticmethod
    def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def calculate_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict:
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        
        return {
            'macd': macd_line,
            'signal': signal_line,
            'histogram': histogram
        }
    
    @staticmethod
    def calculate_bollinger_bands(close: pd.Series, period: int = 20, std_dev: float = 2.0) -> Dict:
        sma = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        
        upper_band = sma + (std * std_dev)
        lower_band = sma - (std * std_dev)
        
        return {
            'middle': sma,
            'upper': upper_band,
            'lower': lower_band,
            'width': (upper_band - lower_band) / sma * 100
        }

class SessionAnalyzer:
    """Analyseur de sessions de marche"""
    
    def __init__(self):
        self.sessions = {
            'asian': {'start': 0, 'end': 9, 'name': 'Asian'},
            'london': {'start': 8, 'end': 17, 'name': 'London'},
            'us': {'start': 13, 'end': 22, 'name': 'US'},
            'overlap_london_us': {'start': 13, 'end': 17, 'name': 'London-US Overlap'}
        }
        
        self.session_characteristics = {
            'BTCUSD': {
                'us': {'volatility': 'high', 'volume': 'high', 'weight': 1.5},
                'london': {'volatility': 'medium', 'volume': 'medium', 'weight': 1.0},
                'asian': {'volatility': 'low', 'volume': 'low', 'weight': 0.7}
            },
            'XAUUSD': {
                'us': {'volatility': 'high', 'volume': 'high', 'weight': 1.4},
                'london': {'volatility': 'high', 'volume': 'high', 'weight': 1.3},
                'asian': {'volatility': 'low', 'volume': 'low', 'weight': 0.5}
            }
        }
    
    def get_current_session(self, timestamp: datetime) -> Dict:
        hour = timestamp.hour
        
        for session_key, session_data in self.sessions.items():
            if session_data['start'] <= hour < session_data['end']:
                return {
                    'session': session_key,
                    'name': session_data['name'],
                    'is_active': True
                }
        
        return {'session': None, 'name': 'No Session', 'is_active': False}
    
    def get_session_weight(self, symbol: str, session: str) -> float:
        if symbol in self.session_characteristics:
            if session in self.session_characteristics[symbol]:
                return self.session_characteristics[symbol][session]['weight']
        return 1.0
    
    def is_high_impact_session(self, symbol: str, current_session: str) -> bool:
        if symbol in self.session_characteristics:
            if current_session in self.session_characteristics[symbol]:
                characteristics = self.session_characteristics[symbol][current_session]
                return (characteristics['volatility'] == 'high' and 
                       characteristics['volume'] == 'high')
        return False

class RiskManager:
    """Gestionnaire de risque professionnel"""
    
    def __init__(self, account_balance: float, risk_percent: float = 1.0):
        self.account_balance = account_balance
        self.risk_percent = risk_percent
        self.max_daily_loss = 0.02
        self.max_positions = 3
        
    def calculate_position_size(self, entry_price: float, stop_loss: float, 
                               symbol: str) -> float:
        risk_amount = self.account_balance * (self.risk_percent / 100)
        stop_distance = abs(entry_price - stop_loss)
        
        pip_value = self._get_pip_value(symbol)
        lot_size = risk_amount / (stop_distance * pip_value)
        
        max_lot = self._get_max_lot_size(symbol)
        return min(lot_size, max_lot)
    
    def calculate_tp_levels(self, entry_price: float, direction: str, 
                           atr_value: float) -> List[float]:
        if direction == 'BUY':
            tp1 = entry_price + (atr_value * 1.5)
            tp2 = entry_price + (atr_value * 3.0)
            tp3 = entry_price + (atr_value * 5.0)
        else:
            tp1 = entry_price - (atr_value * 1.5)
            tp2 = entry_price - (atr_value * 3.0)
            tp3 = entry_price - (atr_value * 5.0)
        
        return [tp1, tp2, tp3]
    
    def calculate_stop_loss(self, entry_price: float, direction: str, 
                           atr_value: float, support_resistance: Dict) -> float:
        atr_sl = atr_value * 2.0
        
        if direction == 'BUY':
            sr_sl = support_resistance.get('support', entry_price - atr_sl)
            dynamic_sl = min(entry_price - atr_sl, sr_sl)
        else:
            sr_sl = support_resistance.get('resistance', entry_price + atr_sl)
            dynamic_sl = max(entry_price + atr_sl, sr_sl)
        
        return dynamic_sl
    
    def _get_pip_value(self, symbol: str) -> float:
        pip_values = {
            'BTCUSD': 1.0,
            'XAUUSD': 10.0
        }
        return pip_values.get(symbol, 1.0)
    
    def _get_max_lot_size(self, symbol: str) -> float:
        max_lots = {
            'BTCUSD': 1.0,
            'XAUUSD': 10.0
        }
        return max_lots.get(symbol, 1.0)

class TradingEngine:
    """Moteur de trading principal - Version stable"""
    
    def __init__(self, config: Dict):
        self.indicators = ProfessionalIndicators()
        self.news_analyzer = NewsAnalyzer(config.get('news_api', {}))
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
            
        authorized = mt5.login(
            credentials['login'],
            password=credentials['password'],
            server=credentials['server']
        )
        
        if not authorized:
            logger.error(f"Echec d'authentification MT5: {mt5.last_error()}")
            return False
            
        logger.info("Connexion MT5 reussie")
        return True
    
    def fetch_market_data(self, symbol: str, timeframe: str, 
                         num_bars: int = 500) -> pd.DataFrame:
        try:
            rates = mt5.copy_rates_from_pos(symbol, self._get_timeframe(timeframe), 
                                           0, num_bars)
            
            if rates is None:
                logger.error(f"Erreur recuperation donnees pour {symbol}")
                return pd.DataFrame()
            
            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
            
            return df
        except Exception as e:
            logger.error(f"Erreur: {e}")
            return pd.DataFrame()
    
    def _get_timeframe(self, timeframe: str):
        timeframes = {
            'M1': mt5.TIMEFRAME_M1,
            'M5': mt5.TIMEFRAME_M5,
            'M15': mt5.TIMEFRAME_M15,
            'M30': mt5.TIMEFRAME_M30,
            'H1': mt5.TIMEFRAME_H1,
            'H4': mt5.TIMEFRAME_H4,
            'D1': mt5.TIMEFRAME_D1,
        }
        return timeframes.get(timeframe, mt5.TIMEFRAME_H1)
    
    def analyze_market_conditions(self, symbol: str, data: pd.DataFrame) -> Dict:
        supertrend = self.indicators.calculate_supertrend(
            data['high'], data['low'], data['close'])
        
        ichimoku = self.indicators.calculate_ichimoku(
            data['high'], data['low'], data['close'])
        
        rsi = self.indicators.calculate_rsi(data['close'])
        macd = self.indicators.calculate_macd(data['close'])
        bb = self.indicators.calculate_bollinger_bands(data['close'])
        atr = self.indicators.calculate_atr(data['high'], data['low'], data['close'])
        
        current_time = datetime.now()
        session_info = self.session_analyzer.get_current_session(current_time)
        
        news_analysis = self.news_analyzer.analyze_news_impact(symbol)
        
        trade_score = self._calculate_trade_score(
            data, supertrend, ichimoku, rsi, macd, bb, 
            session_info, news_analysis, symbol)
        
        return {
            'supertrend': supertrend,
            'ichimoku': ichimoku,
            'rsi': rsi.iloc[-1],
            'macd': macd,
            'bollinger_bands': bb,
            'atr': atr.iloc[-1],
            'session': session_info,
            'news': news_analysis,
            'trade_score': trade_score,
            'current_price': data['close'].iloc[-1]
        }
    
    def _calculate_trade_score(self, data: pd.DataFrame, supertrend: pd.DataFrame,
                              ichimoku: Dict, rsi: pd.Series, macd: Dict,
                              bb: Dict, session: Dict, news: Dict,
                              symbol: str) -> Dict:
        
        score = 0
        signals = []
        max_score = 10
        
        if supertrend['direction'].iloc[-1] == 1:
            score += 1
            signals.append("SuperTrend haussier")
        else:
            score -= 1
            signals.append("SuperTrend baissier")
        
        if (ichimoku['tenkan_sen'].iloc[-1] > ichimoku['kijun_sen'].iloc[-1] and
            data['close'].iloc[-1] > ichimoku['senkou_span_a'].iloc[-1]):
            score += 2
            signals.append("Ichimoku haussier")
        elif (ichimoku['tenkan_sen'].iloc[-1] < ichimoku['kijun_sen'].iloc[-1] and
              data['close'].iloc[-1] < ichimoku['senkou_span_b'].iloc[-1]):
            score -= 2
            signals.append("Ichimoku baissier")
        
        if 30 < rsi.iloc[-1] < 70:
            if rsi.iloc[-1] > 50:
                score += 0.5
                signals.append("RSI momentum positif")
            else:
                score -= 0.5
                signals.append("RSI momentum negatif")
        
        if macd['histogram'].iloc[-1] > 0 and macd['histogram'].iloc[-1] > macd['histogram'].iloc[-2]:
            score += 1
            signals.append("MACD momentum haussier")
        elif macd['histogram'].iloc[-1] < 0 and macd['histogram'].iloc[-1] < macd['histogram'].iloc[-2]:
            score -= 1
            signals.append("MACD momentum baissier")
        
        bb_position = (data['close'].iloc[-1] - bb['lower'].iloc[-1]) / (bb['upper'].iloc[-1] - bb['lower'].iloc[-1])
        if bb_position < 0.2:
            score += 1
            signals.append("Prix proche bande inferieure BB")
        elif bb_position > 0.8:
            score -= 1
            signals.append("Prix proche bande superieure BB")
        
        session_weight = self.session_analyzer.get_session_weight(
            symbol, session['session'])
        if session['session'] == 'us':
            score += 2 * session_weight
            signals.append("Session US active")
        elif session['session'] == 'overlap_london_us':
            score += 2.5 * session_weight
            signals.append("Overlap London-US")
        
        if news['impact_score'] > 0:
            if news['bias'] == 'bullish':
                score += 1
                signals.append("News haussieres")
            elif news['bias'] == 'bearish':
                score -= 1
                signals.append("News baissieres")
        
        normalized_score = max(-max_score, min(max_score, score))
        
        return {
            'total_score': normalized_score,
            'normalized_score': (normalized_score + max_score) / (2 * max_score),
            'direction': 'BUY' if normalized_score > 1 else 'SELL' if normalized_score < -1 else 'NEUTRAL',
            'signals': signals,
            'confidence': abs(normalized_score) / max_score
        }
    
    def generate_trade_setup(self, symbol: str, analysis: Dict) -> Optional[TradeSetup]:
        trade_score = analysis['trade_score']
        
        if trade_score['confidence'] < 0.25:
            logger.info(f"Score de confiance insuffisant pour {symbol}: {trade_score['confidence']:.2f}")
            return None
        
        direction = trade_score['direction']
        if direction == 'NEUTRAL':
            return None
        
        entry_price = analysis['current_price']
        atr_value = analysis['atr']
        
        support_resistance = self._find_support_resistance(symbol)
        sl_price = self.risk_manager.calculate_stop_loss(
            entry_price, direction, atr_value, support_resistance)
        
        tp_levels = self.risk_manager.calculate_tp_levels(
            entry_price, direction, atr_value)
        
        total_lot = self.risk_manager.calculate_position_size(
            entry_price, sl_price, symbol)
        
        lot_sizes = [
            total_lot * 0.4,
            total_lot * 0.3,
            total_lot * 0.3
        ]
        
        setup = TradeSetup(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp_levels[0],
            tp2_price=tp_levels[1],
            tp3_price=tp_levels[2],
            lot_sizes=lot_sizes,
            confidence=trade_score['confidence'],
            timestamp=datetime.now()
        )
        
        logger.info(f"Trade setup genere pour {symbol}:")
        logger.info(f"Direction: {direction}")
        logger.info(f"Entry: {entry_price}")
        logger.info(f"SL: {sl_price}")
        logger.info(f"TPs: {tp_levels}")
        logger.info(f"Confiance: {trade_score['confidence']:.2%}")
        
        return setup
    
    def _find_support_resistance(self, symbol: str) -> Dict:
        data = self.fetch_market_data(symbol, 'H1', 200)
        
        if data.empty:
            return {'resistance': 0, 'support': 0}
        
        recent_high = data['high'].rolling(20).max().iloc[-1]
        recent_low = data['low'].rolling(20).min().iloc[-1]
        
        return {
            'resistance': recent_high,
            'support': recent_low
        }
    
    def _send_mt5_notification(self, setup: TradeSetup, analysis: Dict, timeframe: str) -> str:
        """Envoie une notification detaillee dans les logs - Commentaire court pour MT5"""
        try:
            signals = analysis['trade_score']['signals']
            session = analysis['session']
            news = analysis['news']
            
            st_signal = [s for s in signals if 'SuperTrend' in s]
            st_direction = st_signal[0] if st_signal else "Neutre"
            
            rsi_value = analysis['rsi']
            if rsi_value > 70:
                rsi_state = "Surachat"
            elif rsi_value < 30:
                rsi_state = "Survente"
            elif rsi_value > 50:
                rsi_state = "Haussier"
            else:
                rsi_state = "Baissier"
            
            macd_hist = analysis['macd']['histogram'].iloc[-1]
            macd_state = "Haussier" if macd_hist > 0 else "Baissier"
            
            bb_lower = analysis['bollinger_bands']['lower'].iloc[-1]
            bb_upper = analysis['bollinger_bands']['upper'].iloc[-1]
            current_price = analysis['current_price']
            
            if current_price <= bb_lower * 1.05:
                bb_state = "Proche bande basse"
            elif current_price >= bb_upper * 0.95:
                bb_state = "Proche bande haute"
            else:
                bb_state = "Dans les bandes"
            
            # Commentaire COURT pour MT5 (limite 27 caracteres)
            comment = f"EA_{setup.direction}_{timeframe}"
            
            # Logs detailles (pas de limite)
            logger.info("=" * 60)
            logger.info(f"[TRADE OUVERT] {setup.symbol} - {setup.direction}")
            logger.info(f"  Timeframe: {timeframe}")
            logger.info(f"  Session: {session['name']}")
            logger.info(f"  Prix: {setup.entry_price:.2f}")
            logger.info(f"  SL: {setup.sl_price:.2f}")
            logger.info(f"  TP1: {setup.tp1_price:.2f} | TP2: {setup.tp2_price:.2f} | TP3: {setup.tp3_price:.2f}")
            logger.info(f"  Lots: {[f'{l:.3f}' for l in setup.lot_sizes]}")
            logger.info(f"  Confiance: {setup.confidence:.1%}")
            logger.info(f"  --- Indicateurs ---")
            logger.info(f"  SuperTrend: {st_direction}")
            logger.info(f"  RSI: {rsi_value:.1f} ({rsi_state})")
            logger.info(f"  MACD: {macd_state} (Histogram: {macd_hist:.1f})")
            logger.info(f"  Bollinger: {bb_state}")
            logger.info(f"  ATR: {analysis['atr']:.1f}")
            logger.info(f"  News: {news['bias']} (impact: {news['impact_score']:.2f})")
            logger.info(f"  --- Signaux ---")
            for signal in signals:
                logger.info(f"  - {signal}")
            logger.info("=" * 60)
            
            return comment
            
        except Exception as e:
            logger.error(f"Erreur creation notification MT5: {e}")
            return f"EA_Pro"
    
    def _send_telegram_alert(self, setup: TradeSetup, analysis: Dict, timeframe: str, telegram_config: Dict):
        """Envoie une alerte Telegram detaillee"""
        try:
            if not telegram_config.get('enabled', False):
                return
            
            bot_token = telegram_config.get('bot_token', '')
            chat_id = telegram_config.get('chat_id', '')
            
            if not bot_token or not chat_id:
                return
            
            signals = analysis['trade_score']['signals']
            session = analysis['session']
            news = analysis['news']
            rsi_value = analysis['rsi']
            macd_hist = analysis['macd']['histogram'].iloc[-1]
            
            if rsi_value > 70:
                rsi_state = "Surachat"
            elif rsi_value < 30:
                rsi_state = "Survente"
            elif rsi_value > 50:
                rsi_state = "Haussier"
            else:
                rsi_state = "Baissier"
            
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
            params = {
                'chat_id': chat_id,
                'text': message.strip()
            }
            
            response = requests.post(url, params=params, timeout=10)
            
            if response.status_code == 200:
                logger.info("[TELEGRAM] Alerte envoyee avec succes")
            else:
                logger.warning(f"[TELEGRAM] Erreur envoi: {response.status_code}")
                
        except Exception as e:
            logger.error(f"[TELEGRAM] Erreur: {e}")
    
    def execute_trade(self, setup: TradeSetup, analysis: Dict = None, timeframe: str = "H1", telegram_config: Dict = None) -> bool:
        """Execute le trade sur MT5 avec notifications - Version corrigee"""
        try:
            if not self._pre_trade_checks(setup):
                return False
            
            # Generer le commentaire MT5 (court)
            if analysis:
                mt5_comment = self._send_mt5_notification(setup, analysis, timeframe)
            else:
                mt5_comment = "EA_Pro"
            
            # Envoyer l'alerte Telegram
            if analysis and telegram_config:
                self._send_telegram_alert(setup, analysis, timeframe, telegram_config)
            
            # Infos du symbole
            symbol_info = mt5.symbol_info(setup.symbol)
            if symbol_info is None:
                logger.error(f"Impossible d'obtenir les infos pour {setup.symbol}")
                return False
            digits = symbol_info.digits
            vol_min = symbol_info.volume_min
            vol_step = symbol_info.volume_step
            
            # Tick pour le prix reel
            tick = mt5.symbol_info_tick(setup.symbol)
            if tick is None:
                logger.error(f"Impossible d'obtenir le tick pour {setup.symbol}")
                return False
            
            for i, (tp_price, lot) in enumerate(zip(
                [setup.tp1_price, setup.tp2_price, setup.tp3_price],
                setup.lot_sizes)):
                
                order_type = mt5.ORDER_TYPE_BUY if setup.direction == 'BUY' else mt5.ORDER_TYPE_SELL
                price = tick.ask if setup.direction == 'BUY' else tick.bid
                
                # Calcul du volume correct
                volume = max(vol_min, round(float(lot) / vol_step) * vol_step)
                volume = round(volume, 2)
                
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": setup.symbol,
                    "volume": volume,
                    "type": order_type,
                    "price": round(price, digits),
                    "sl": round(float(setup.sl_price), digits),
                    "tp": round(float(tp_price), digits),
                    "deviation": 20,
                    "magic": 123456,
                    "comment": f"{mt5_comment}_TP{i+1}",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_FOK,
                }
                
                logger.info(f"Envoi ordre {i+1}: {setup.symbol} {setup.direction} Vol:{volume} Prix:{round(price, digits)}")
                
                result = mt5.order_send(request)
                
                if result is None:
                    logger.error(f"Erreur execution trade TP{i+1}: resultat None")
                    logger.error(f"MT5 Last Error: {mt5.last_error()}")
                    return False
                
                if result.retcode != mt5.TRADE_RETCODE_DONE:
                    logger.error(f"Erreur execution trade TP{i+1}: {result.comment} (code: {result.retcode})")
                    return False
                
                logger.info(f"[OK] Trade TP{i+1} execute: Ticket {result.order}")
            
            return True
            
        except Exception as e:
            logger.error(f"Erreur execution trade: {e}")
            traceback.print_exc()
            return False
    
    def _pre_trade_checks(self, setup: TradeSetup) -> bool:
        tick = mt5.symbol_info_tick(setup.symbol)
        if tick is None:
            return False
        
        spread = tick.ask - tick.bid
        max_spread = self._get_max_spread(setup.symbol)
        
        if spread > max_spread:
            logger.warning(f"Spread trop eleve: {spread} (max: {max_spread})")
            return False
        
        positions = mt5.positions_get(symbol=setup.symbol)
        if positions is None:
            positions = []
        if len(positions) >= self.risk_manager.max_positions:
            logger.warning("Nombre maximum de positions atteint")
            return False
        
        if not self._check_risk_exposure(setup):
            return False
        
        return True
    
    def _get_max_spread(self, symbol: str) -> float:
        spreads = {
            'BTCUSD': 50.0,
            'XAUUSD': 5.0
        }
        return spreads.get(symbol, 10.0)
    
    def _check_risk_exposure(self, setup: TradeSetup) -> bool:
        account_info = mt5.account_info()
        if account_info is None:
            return False
        return True
    
    def manage_open_positions(self):
        positions = mt5.positions_get()
        
        if positions is None:
            return
        
        for position in positions:
            self._manage_single_position(position)
    
    def _manage_single_position(self, position):
        symbol = position.symbol
        tick = mt5.symbol_info_tick(symbol)
        
        if tick is None:
            return
        
        current_price = tick.bid if position.type == 0 else tick.ask
        entry_price = position.price_open
        
        if position.type == 0:
            profit_pips = (current_price - entry_price) / mt5.symbol_info(symbol).point
        else:
            profit_pips = (entry_price - current_price) / mt5.symbol_info(symbol).point
        
        self._apply_trailing_stop(position, current_price, profit_pips)
        self._apply_break_even(position, current_price, profit_pips)
    
        def _apply_trailing_stop(self, position, current_price: float, profit_pips: float):
        """Applique un trailing stop - Active a 150 pips"""
        symbol = position.symbol
        activation_pips = 150
        trailing_distance = 50
        
        if profit_pips >= activation_pips:
            point = mt5.symbol_info(symbol).point
            if position.type == 0:  # BUY
                new_sl = current_price - (trailing_distance * point)
                if new_sl > position.sl:
                    self._modify_position(position.ticket, new_sl, position.tp)
                    logger.info(f"Trailing stop applique sur {position.ticket} a {new_sl}")
            else:  # SELL
                new_sl = current_price + (trailing_distance * point)
                if new_sl < position.sl or position.sl == 0:
                    self._modify_position(position.ticket, new_sl, position.tp)
                    logger.info(f"Trailing stop applique sur {position.ticket} a {new_sl}")
    
    def _apply_break_even(self, position, current_price: float, profit_pips: float):
        """Applique le break-even en positif (+10 pips) a 150 pips de profit"""
        symbol = position.symbol
        be_activation = 150
        be_positive_offset = 10
        
        if profit_pips >= be_activation:
            point = mt5.symbol_info(symbol).point
            if position.type == 0:  # BUY
                new_sl = position.price_open + (be_positive_offset * point)
            else:  # SELL
                new_sl = position.price_open - (be_positive_offset * point)
            
            if new_sl != position.sl:
                self._modify_position(position.ticket, new_sl, position.tp)
                logger.info(f"Break-even +{be_positive_offset} pips applique sur {position.ticket}")
    
    def _modify_position(self, ticket: int, sl: float, tp: float) -> bool:
        """Modifie une position existante"""
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": sl,
            "tp": tp,
        }
        
        result = mt5.order_send(request)
        if result is None:
            return False
        return result.retcode == mt5.TRADE_RETCODE_DONE

class ExpertAdvisor:
    """Classe principale de l'Expert Advisor"""
    
    def __init__(self, config: Dict):
        self.symbols = config['symbols']
        self.timeframes = config['timeframes']
        self.mt5_credentials = config['mt5_credentials']
        self.config = config
        
        self.trading_engine = TradingEngine(config)
        self.is_running = False
        
    def start(self):
        logger.info("Demarrage de l'Expert Advisor...")
        self.is_running = True
        
        account_info = mt5.account_info()
        self.trading_engine.risk_manager = RiskManager(
            account_info.balance,
            risk_percent=1.0
        )
        
        while self.is_running:
            try:
                self.run_iteration()
                time.sleep(60)
                
            except KeyboardInterrupt:
                logger.info("Arret demande par l'utilisateur")
                self.stop()
            except Exception as e:
                logger.error(f"Erreur dans la boucle principale: {e}")
                time.sleep(300)
    
    def run_iteration(self):
        for symbol in self.symbols:
            for timeframe in self.timeframes:
                data = self.trading_engine.fetch_market_data(
                    symbol, timeframe, 500)
                
                if data.empty:
                    continue
                
                analysis = self.trading_engine.analyze_market_conditions(
                    symbol, data)
                
                setup = self.trading_engine.generate_trade_setup(
                    symbol, analysis)
                
                if setup:
                    if self.additional_filters(setup, analysis):
                        telegram_config = self.config.get('telegram', {})
                        self.trading_engine.execute_trade(setup, analysis, timeframe, telegram_config)
        
        self.trading_engine.manage_open_positions()
    
    def additional_filters(self, setup: TradeSetup, analysis: Dict) -> bool:
        # Filtre session desactive pour test
        # if analysis['session']['session'] not in ['us', 'overlap_london_us']:
        #     if analysis['trade_score']['confidence'] < 0.50:
        #         logger.info("Trade hors session US avec confiance insuffisante")
        #         return False
        
        if analysis['atr'] < analysis['atr'] * 0.5:
            logger.info("Volatilite insuffisante")
            return False
        
        if not self.confirm_multi_timeframe(setup.symbol, setup.direction):
            logger.info("Tendance non confirmee sur timeframe superieur")
            return False
        
        return True
    
    def confirm_multi_timeframe(self, symbol: str, direction: str) -> bool:
        higher_tf_data = self.trading_engine.fetch_market_data(
            symbol, 'H4', 100)
        
        if higher_tf_data.empty:
            return True
        
        ichimoku = ProfessionalIndicators.calculate_ichimoku(
            higher_tf_data['high'],
            higher_tf_data['low'],
            higher_tf_data['close']
        )
        
        close = higher_tf_data['close'].iloc[-1]
        cloud_top = max(ichimoku['senkou_span_a'].iloc[-1], 
                       ichimoku['senkou_span_b'].iloc[-1])
        cloud_bottom = min(ichimoku['senkou_span_a'].iloc[-1],
                          ichimoku['senkou_span_b'].iloc[-1])
        
        if direction == 'BUY':
            return close > cloud_top
        else:
            return close < cloud_bottom
    
    def stop(self):
        self.is_running = False
        mt5.shutdown()
        logger.info("EA arrete")

# Configuration et execution
if __name__ == "__main__":
    config_path = "config.json"
    
    if not os.path.exists(config_path):
        logger.error(f"Fichier {config_path} non trouve !")
        logger.error("Creez un fichier config.json avec vos identifiants MT5 et API News.")
        exit(1)
    
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        logger.info("[OK] Configuration chargee depuis config.json")
    except Exception as e:
        logger.error(f"Erreur lecture config.json: {e}")
        exit(1)
    
    required_fields = ['symbols', 'timeframes', 'mt5_credentials']
    for field in required_fields:
        if field not in config:
            logger.error(f"Champ '{field}' manquant dans config.json")
            exit(1)
    
    safe_config = config.copy()
    if 'mt5_credentials' in safe_config:
        safe_config['mt5_credentials'] = {
            'login': safe_config['mt5_credentials'].get('login', 'N/A'),
            'password': '***MASQUE***',
            'server': safe_config['mt5_credentials'].get('server', 'N/A')
        }
    if 'news_api' in safe_config:
        safe_config['news_api'] = {
            'api_key': '***MASQUE***' if safe_config['news_api'].get('api_key') else 'N/A',
            'enabled': safe_config['news_api'].get('enabled', False)
        }
    if 'telegram' in safe_config:
        safe_config['telegram'] = {
            'enabled': safe_config['telegram'].get('enabled', False),
            'bot_token': '***MASQUE***' if safe_config['telegram'].get('bot_token') else 'N/A',
            'chat_id': '***MASQUE***' if safe_config['telegram'].get('chat_id') else 'N/A'
        }
    
    logger.info("=" * 50)
    logger.info("[CONFIG] Configuration chargee :")
    logger.info(f"  Symboles: {config.get('symbols', [])}")
    logger.info(f"  Timeframes: {config.get('timeframes', [])}")
    logger.info(f"  MT5 Login: {safe_config['mt5_credentials']['login']}")
    logger.info(f"  MT5 Server: {safe_config['mt5_credentials']['server']}")
    logger.info(f"  News API: {'Activee' if config.get('news_api', {}).get('enabled') else 'Desactivee'}")
    logger.info(f"  Telegram: {'Active' if config.get('telegram', {}).get('enabled') else 'Desactive'}")
    logger.info("=" * 50)
    
    ea = ExpertAdvisor(config)
    
    try:
        ea.start()
    except KeyboardInterrupt:
        logger.info("Arret demande par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
    finally:
        ea.stop()