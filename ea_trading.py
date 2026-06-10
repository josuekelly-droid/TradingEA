"""
Expert Advisor Professionnel - BTC/USD & Gold/USD
Auteur: Trading System Pro
Version: 3.0
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
    direction: str  # 'BUY' ou 'SELL'
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    lot_sizes: List[float]  # [lot_tp1, lot_tp2, lot_tp3]
    confidence: float
    timestamp: datetime

class NewsAnalyzer:
    """Analyseur de nouvelles économiques avec NewsAPI.org"""
    
    def __init__(self, config: Dict = None):
        # Gérer l'ancien format (string) et le nouveau format (dict)
        if isinstance(config, str):
            # Compatibilité avec l'ancien code
            self.api_key = config
            self.enabled = True if config and config != "YOUR_API_KEY" else False
        elif isinstance(config, dict):
            self.api_key = config.get('api_key', '')
            self.enabled = config.get('enabled', False)
        else:
            self.api_key = ''
            self.enabled = False
        
        # Mots-clés par symbole pour filtrer les news pertinentes
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
        
        # Cache pour éviter trop de requêtes API
        self.cache = {}
        self.cache_duration = 300  # 5 minutes
        self.daily_requests = 0
        self.max_daily_requests = 80  # Marge de sécurité (limite 100/jour)
        self.last_reset = datetime.now().date()
        
        logger.info(f"NewsAnalyzer initialise - API: {'Activee' if self.enabled else 'Desactivee'}")
    
    def fetch_economic_calendar(self) -> pd.DataFrame:
        """Récupère le calendrier économique (garde la compatibilité)"""
        return pd.DataFrame()
    
    def fetch_news(self, symbol: str) -> pd.DataFrame:
        """Récupère les news via NewsAPI.org"""
        
        # Réinitialiser le compteur chaque jour
        if datetime.now().date() > self.last_reset:
            self.daily_requests = 0
            self.last_reset = datetime.now().date()
        
        # Vérifier le cache
        cache_key = f"{symbol}_{datetime.now().strftime('%Y%m%d_%H')}"
        if cache_key in self.cache:
            cache_time = self.cache[cache_key]['timestamp']
            if (datetime.now() - cache_time).seconds < self.cache_duration:
                logger.info(f"[CACHE] Utilisation du cache pour {symbol}")
                return self.cache[cache_key]['data']
        
        # Si API désactivée ou limite atteinte, retourner données vides
        if not self.enabled or not self.api_key:
            return pd.DataFrame()
        
        if self.daily_requests >= self.max_daily_requests:
            logger.warning("[LIMIT] Limite quotidienne de requetes NewsAPI atteinte")
            return pd.DataFrame()
        
        try:
            # Construire la requête pour NewsAPI
            keywords = self.keywords.get(symbol, ['forex', 'trading'])
            query = ' OR '.join(keywords[:3])  # Limiter à 3 mots-clés
            
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
            response = requests.get(url, params=params)
            
            if response.status_code == 200:
                news_data = response.json()
                
                if news_data['status'] == 'ok' and news_data['totalResults'] > 0:
                    articles = news_data['articles']
                    df = pd.DataFrame(articles)
                    df['publishedAt'] = pd.to_datetime(df['publishedAt'])
                    df['symbol'] = symbol
                    
                    # Calculer un score d'impact basé sur la source
                    df['source_score'] = df['source'].apply(
                        lambda x: self._get_source_score(x['name']) if isinstance(x, dict) else 0
                    )
                    
                    # Mettre en cache
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
        """Attribue un score de fiabilité aux sources"""
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
        """Analyse l'impact des news sur un symbole"""
        
        # Si l'API est désactivée, retourner neutre
        if not self.enabled:
            return {
                'impact_score': 0,
                'bias': 'neutral',
                'high_impact_events': 0,
                'total_articles': 0
            }
        
        # Récupérer les news
        news_df = self.fetch_news(symbol)
        
        if news_df.empty:
            return {
                'impact_score': 0,
                'bias': 'neutral',
                'high_impact_events': 0,
                'total_articles': 0
            }
        
        # Analyse de sentiment simplifiée
        sentiment_score = 0
        high_impact_count = 0
        
        for _, article in news_df.iterrows():
            # Analyser le titre et la description
            title = str(article.get('title', '')).lower()
            description = str(article.get('description', '')).lower()
            content = title + ' ' + description
            
            # Mots positifs/négatifs pour le trading
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
            
            # Compter les occurrences
            positive_count = sum(1 for word in positive_words if word in content)
            negative_count = sum(1 for word in negative_words if word in content)
            
            # Score par article
            article_score = (positive_count - negative_count) * article.get('source_score', 1)
            sentiment_score += article_score
            
            # Détecter les news à fort impact
            if article.get('source_score', 0) >= 2.0:
                high_impact_count += 1
        
        # Normaliser le score
        max_score = max(len(news_df) * 5, 1)  # Éviter division par zéro
        normalized_score = sentiment_score / max_score
        
        # Déterminer le biais
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
        """Calcule l'indicateur SuperTrend"""
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
        """Calcule l'ATR (Average True Range)"""
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()
        
        return atr
    
    @staticmethod
    def calculate_ichimoku(high: pd.Series, low: pd.Series, close: pd.Series) -> Dict:
        """Calcule l'indicateur Ichimoku Kinko Hyo"""
        # Tenkan-sen (Conversion Line)
        period9_high = high.rolling(window=9).max()
        period9_low = low.rolling(window=9).min()
        tenkan_sen = (period9_high + period9_low) / 2
        
        # Kijun-sen (Base Line)
        period26_high = high.rolling(window=26).max()
        period26_low = low.rolling(window=26).min()
        kijun_sen = (period26_high + period26_low) / 2
        
        # Senkou Span A (Leading Span A)
        senkou_span_a = ((tenkan_sen + kijun_sen) / 2).shift(26)
        
        # Senkou Span B (Leading Span B)
        period52_high = high.rolling(window=52).max()
        period52_low = low.rolling(window=52).min()
        senkou_span_b = ((period52_high + period52_low) / 2).shift(26)
        
        # Chikou Span (Lagging Span)
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
        """Calcule le RSI"""
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    @staticmethod
    def calculate_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict:
        """Calcule le MACD"""
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
        """Calcule les Bandes de Bollinger"""
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
    
    @staticmethod
    def calculate_market_profile(high: pd.Series, low: pd.Series, close: pd.Series, 
                                volume: pd.Series) -> Dict:
        """Calcule le Market Profile pour identifier les zones de valeur"""
        price_range = pd.concat([high, low, close])
        price_bins = pd.cut(price_range, bins=50)
        
        volume_profile = volume.groupby(price_bins).sum()
        poc_price = volume_profile.idxmax()  # Point of Control
        
        total_volume = volume_profile.sum()
        value_area_volume = total_volume * 0.70
        cumulative_volume = volume_profile.sort_index().cumsum()
        
        value_area = cumulative_volume[cumulative_volume <= value_area_volume]
        vah = value_area.index.max()  # Value Area High
        val = value_area.index.min()  # Value Area Low
        
        return {
            'poc': poc_price,
            'vah': vah,
            'val': val,
            'value_area_volume': value_area_volume
        }

class SessionAnalyzer:
    """Analyseur de sessions de marché"""
    
    def __init__(self):
        # Sessions en UTC
        self.sessions = {
            'asian': {'start': 0, 'end': 9, 'name': 'Asian'},
            'london': {'start': 8, 'end': 17, 'name': 'London'},
            'us': {'start': 13, 'end': 22, 'name': 'US'},
            'overlap_london_us': {'start': 13, 'end': 17, 'name': 'London-US Overlap'}
        }
        
        # Caractéristiques des sessions pour BTC et Gold
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
        """Détermine la session actuelle"""
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
        """Retourne le poids de la session pour un symbole donné"""
        if symbol in self.session_characteristics:
            if session in self.session_characteristics[symbol]:
                return self.session_characteristics[symbol][session]['weight']
        return 1.0
    
    def is_high_impact_session(self, symbol: str, current_session: str) -> bool:
        """Vérifie si la session actuelle est à fort impact"""
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
        self.max_daily_loss = 0.02  # 2% perte quotidienne max
        self.max_positions = 3  # Maximum de positions simultanées
        
    def calculate_position_size(self, entry_price: float, stop_loss: float, 
                               symbol: str) -> float:
        """Calcule la taille de position basée sur le risque"""
        risk_amount = self.account_balance * (self.risk_percent / 100)
        stop_distance = abs(entry_price - stop_loss)
        
        # Ajustement pour le type de symbole
        pip_value = self._get_pip_value(symbol)
        lot_size = risk_amount / (stop_distance * pip_value)
        
        # Limiter la taille de la position
        max_lot = self._get_max_lot_size(symbol)
        return min(lot_size, max_lot)
    
    def calculate_tp_levels(self, entry_price: float, direction: str, 
                           atr_value: float) -> List[float]:
        """Calcule les 3 niveaux de Take Profit"""
        if direction == 'BUY':
            tp1 = entry_price + (atr_value * 1.5)  # TP1: 1.5x ATR
            tp2 = entry_price + (atr_value * 3.0)  # TP2: 3x ATR
            tp3 = entry_price + (atr_value * 5.0)  # TP3: 5x ATR
        else:
            tp1 = entry_price - (atr_value * 1.5)
            tp2 = entry_price - (atr_value * 3.0)
            tp3 = entry_price - (atr_value * 5.0)
        
        return [tp1, tp2, tp3]
    
    def calculate_stop_loss(self, entry_price: float, direction: str, 
                           atr_value: float, support_resistance: Dict) -> float:
        """Calcule le Stop Loss intelligent"""
        # SL basé sur l'ATR
        atr_sl = atr_value * 2.0
        
        # SL basé sur les niveaux de support/résistance
        if direction == 'BUY':
            sr_sl = support_resistance.get('support', entry_price - atr_sl)
            dynamic_sl = min(entry_price - atr_sl, sr_sl)
        else:
            sr_sl = support_resistance.get('resistance', entry_price + atr_sl)
            dynamic_sl = max(entry_price + atr_sl, sr_sl)
        
        return dynamic_sl
    
    def _get_pip_value(self, symbol: str) -> float:
        """Retourne la valeur du pip pour un symbole"""
        pip_values = {
            'BTCUSD': 1.0,
            'XAUUSD': 10.0
        }
        return pip_values.get(symbol, 1.0)
    
    def _get_max_lot_size(self, symbol: str) -> float:
        """Retourne la taille de lot maximale"""
        max_lots = {
            'BTCUSD': 1.0,
            'XAUUSD': 10.0
        }
        return max_lots.get(symbol, 1.0)

class TradingEngine:
    """Moteur de trading principal"""
    
    def __init__(self, config: Dict):
        self.indicators = ProfessionalIndicators()
        # ✅ MODIFIÉ : Passage de la config complète au lieu d'une string
        self.news_analyzer = NewsAnalyzer(config.get('news_api', {}))
        self.session_analyzer = SessionAnalyzer()
        self.risk_manager = None
        
        # ✅ CORRIGÉ : Bon ordre des paramètres
        self._initialize_mt5(config.get('mt5_credentials', {}))
        
        # Cache des données
        self.data_cache = {}
        
    def _initialize_mt5(self, credentials: Dict) -> bool:
        """Initialise la connexion MT5"""
        if not mt5.initialize():
            logger.error("Échec d'initialisation de MT5")
            return False
            
        authorized = mt5.login(
            credentials['login'],
            password=credentials['password'],
            server=credentials['server']
        )
        
        if not authorized:
            logger.error(f"Échec d'authentification MT5: {mt5.last_error()}")
            return False
            
        logger.info("Connexion MT5 réussie")
        return True
    
    def fetch_market_data(self, symbol: str, timeframe: str, 
                         num_bars: int = 500) -> pd.DataFrame:
        """Récupère les données de marché"""
        try:
            rates = mt5.copy_rates_from_pos(symbol, self._get_timeframe(timeframe), 
                                           0, num_bars)
            
            if rates is None:
                logger.error(f"Erreur récupération données pour {symbol}")
                return pd.DataFrame()
            
            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df.set_index('time', inplace=True)
            
            return df
        except Exception as e:
            logger.error(f"Erreur: {e}")
            return pd.DataFrame()
    
    def _get_timeframe(self, timeframe: str):
        """Convertit le timeframe en constante MT5"""
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
        """Analyse complète des conditions de marché"""
        
        # Calcul des indicateurs techniques
        supertrend = self.indicators.calculate_supertrend(
            data['high'], data['low'], data['close'])
        
        ichimoku = self.indicators.calculate_ichimoku(
            data['high'], data['low'], data['close'])
        
        rsi = self.indicators.calculate_rsi(data['close'])
        macd = self.indicators.calculate_macd(data['close'])
        bb = self.indicators.calculate_bollinger_bands(data['close'])
        atr = self.indicators.calculate_atr(data['high'], data['low'], data['close'])
        
        # Analyse des sessions
        current_time = datetime.now()
        session_info = self.session_analyzer.get_current_session(current_time)
        
        # Analyse des news
        news_analysis = self.news_analyzer.analyze_news_impact(symbol)
        
        # Calcul du score de trading
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
        """Calcule le score de trading basé sur tous les facteurs"""
        
        score = 0
        signals = []
        max_score = 10
        
        # 1. Tendance SuperTrend (2 points)
        if supertrend['direction'].iloc[-1] == 1:
            score += 1
            signals.append("SuperTrend haussier")
        else:
            score -= 1
            signals.append("SuperTrend baissier")
        
        # 2. Ichimoku Cloud (2 points)
        if (ichimoku['tenkan_sen'].iloc[-1] > ichimoku['kijun_sen'].iloc[-1] and
            data['close'].iloc[-1] > ichimoku['senkou_span_a'].iloc[-1]):
            score += 2
            signals.append("Ichimoku haussier")
        elif (ichimoku['tenkan_sen'].iloc[-1] < ichimoku['kijun_sen'].iloc[-1] and
              data['close'].iloc[-1] < ichimoku['senkou_span_b'].iloc[-1]):
            score -= 2
            signals.append("Ichimoku baissier")
        
        # 3. RSI (1 point)
        if 30 < rsi.iloc[-1] < 70:
            if rsi.iloc[-1] > 50:
                score += 0.5
                signals.append("RSI momentum positif")
            else:
                score -= 0.5
                signals.append("RSI momentum négatif")
        
        # 4. MACD (1 point)
        if macd['histogram'].iloc[-1] > 0 and macd['histogram'].iloc[-1] > macd['histogram'].iloc[-2]:
            score += 1
            signals.append("MACD momentum haussier")
        elif macd['histogram'].iloc[-1] < 0 and macd['histogram'].iloc[-1] < macd['histogram'].iloc[-2]:
            score -= 1
            signals.append("MACD momentum baissier")
        
        # 5. Bollinger Bands (1 point)
        bb_position = (data['close'].iloc[-1] - bb['lower'].iloc[-1]) / (bb['upper'].iloc[-1] - bb['lower'].iloc[-1])
        if bb_position < 0.2:  # Proche de la bande inférieure
            score += 1
            signals.append("Prix proche bande inférieure BB")
        elif bb_position > 0.8:  # Proche de la bande supérieure
            score -= 1
            signals.append("Prix proche bande supérieure BB")
        
        # 6. Session de trading (2 points)
        session_weight = self.session_analyzer.get_session_weight(
            symbol, session['session'])
        if session['session'] == 'us':
            score += 2 * session_weight
            signals.append("Session US active")
        elif session['session'] == 'overlap_london_us':
            score += 2.5 * session_weight
            signals.append("Overlap London-US")
        
        # 7. Impact des news (1 point)
        if news['impact_score'] > 0:
            if news['bias'] == 'bullish':
                score += 1
                signals.append("News haussières")
            elif news['bias'] == 'bearish':
                score -= 1
                signals.append("News baissières")
        
        # Normaliser le score
        normalized_score = max(-max_score, min(max_score, score))
        
        return {
            'total_score': normalized_score,
            'normalized_score': (normalized_score + max_score) / (2 * max_score),  # 0-1
            'direction': 'BUY' if normalized_score > 2 else 'SELL' if normalized_score < -2 else 'NEUTRAL',
            'signals': signals,
            'confidence': abs(normalized_score) / max_score
        }
    
    def generate_trade_setup(self, symbol: str, analysis: Dict) -> Optional[TradeSetup]:
        """Génère un setup de trade complet"""
        
        trade_score = analysis['trade_score']
        
        # Vérifier si les conditions sont réunies pour un trade
        if trade_score['confidence'] < 0.60:
            logger.info(f"Score de confiance insuffisant pour {symbol}: {trade_score['confidence']:.2f}")
            return None
        
        direction = trade_score['direction']
        if direction == 'NEUTRAL':
            return None
        
        entry_price = analysis['current_price']
        atr_value = analysis['atr']
        
        # Calculer SL et TPs
        support_resistance = self._find_support_resistance(symbol)
        sl_price = self.risk_manager.calculate_stop_loss(
            entry_price, direction, atr_value, support_resistance)
        
        tp_levels = self.risk_manager.calculate_tp_levels(
            entry_price, direction, atr_value)
        
        # Calculer la taille des positions
        total_lot = self.risk_manager.calculate_position_size(
            entry_price, sl_price, symbol)
        
        # Répartition des lots: 40% TP1, 30% TP2, 30% TP3
        lot_sizes = [
            total_lot * 0.4,  # TP1
            total_lot * 0.3,  # TP2
            total_lot * 0.3   # TP3
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
        
        logger.info(f"Trade setup généré pour {symbol}:")
        logger.info(f"Direction: {direction}")
        logger.info(f"Entry: {entry_price}")
        logger.info(f"SL: {sl_price}")
        logger.info(f"TPs: {tp_levels}")
        logger.info(f"Confiance: {trade_score['confidence']:.2%}")
        
        return setup
    
    def _find_support_resistance(self, symbol: str) -> Dict:
        """Trouve les niveaux de support et résistance"""
        data = self.fetch_market_data(symbol, 'H1', 200)
        
        # Utiliser les plus hauts/bas récents comme S/R
        recent_high = data['high'].rolling(20).max().iloc[-1]
        recent_low = data['low'].rolling(20).min().iloc[-1]
        
        return {
            'resistance': recent_high,
            'support': recent_low
        }
    
    def execute_trade(self, setup: TradeSetup) -> bool:
        """Exécute le trade sur MT5"""
        try:
            # Vérifier les conditions avant l'exécution
            if not self._pre_trade_checks(setup):
                return False
            
            # Préparer les ordres
            for i, (tp_price, lot) in enumerate(zip(
                [setup.tp1_price, setup.tp2_price, setup.tp3_price],
                setup.lot_sizes)):
                
                order_type = mt5.ORDER_TYPE_BUY if setup.direction == 'BUY' else mt5.ORDER_TYPE_SELL
                
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": setup.symbol,
                    "volume": lot,
                    "type": order_type,
                    "price": setup.entry_price,
                    "sl": setup.sl_price,
                    "tp": tp_price,
                    "deviation": 20,
                    "magic": 123456,
                    "comment": f"EA_TP{i+1}",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                
                result = mt5.order_send(request)
                
                if result.retcode != mt5.TRADE_RETCODE_DONE:
                    logger.error(f"Erreur exécution trade TP{i+1}: {result.comment}")
                    return False
                
                logger.info(f"Trade TP{i+1} exécuté: Ticket {result.order}")
            
            return True
            
        except Exception as e:
            logger.error(f"Erreur exécution trade: {e}")
            return False
    
    def _pre_trade_checks(self, setup: TradeSetup) -> bool:
        """Vérifications pré-trade"""
        
        # 1. Vérifier le spread
        tick = mt5.symbol_info_tick(setup.symbol)
        if tick is None:
            return False
        
        spread = tick.ask - tick.bid
        max_spread = self._get_max_spread(setup.symbol)
        
        if spread > max_spread:
            logger.warning(f"Spread trop élevé: {spread}")
            return False
        
        # 2. Vérifier les positions ouvertes
        positions = mt5.positions_get(symbol=setup.symbol)
        if len(positions) >= self.risk_manager.max_positions:
            logger.warning("Nombre maximum de positions atteint")
            return False
        
        # 3. Vérifier l'exposition au risque
        if not self._check_risk_exposure(setup):
            return False
        
        return True
    
    def _get_max_spread(self, symbol: str) -> float:
        """Retourne le spread maximum acceptable"""
        spreads = {
            'BTCUSD': 50.0,
            'XAUUSD': 3.0
        }
        return spreads.get(symbol, 10.0)
    
    def _check_risk_exposure(self, setup: TradeSetup) -> bool:
        """Vérifie l'exposition au risque"""
        account_info = mt5.account_info()
        if account_info is None:
            return False
        
        # Vérifier la perte quotidienne
        # Implémenter la logique de vérification de perte quotidienne
        return True
    
    def manage_open_positions(self):
        """Gère les positions ouvertes (trailing stop, break-even, etc.)"""
        positions = mt5.positions_get()
        
        if positions is None:
            return
        
        for position in positions:
            self._manage_single_position(position)
    
    def _manage_single_position(self, position):
        """Gère une position individuelle"""
        symbol = position.symbol
        tick = mt5.symbol_info_tick(symbol)
        
        if tick is None:
            return
        
        current_price = tick.bid if position.type == 0 else tick.ask
        entry_price = position.price_open
        
        # Calcul du profit en pips
        if position.type == 0:  # BUY
            profit_pips = (current_price - entry_price) / mt5.symbol_info(symbol).point
        else:  # SELL
            profit_pips = (entry_price - current_price) / mt5.symbol_info(symbol).point
        
        # Trailing Stop
        self._apply_trailing_stop(position, current_price, profit_pips)
        
        # Break-even
        self._apply_break_even(position, current_price, profit_pips)
    
    def _apply_trailing_stop(self, position, current_price: float, profit_pips: float):
        """Applique un trailing stop"""
        symbol = position.symbol
        activation_pips = 50  # Activer après 50 pips de profit
        trailing_distance = 30  # Distance du trailing stop
        
        if profit_pips >= activation_pips:
            if position.type == 0:  # BUY
                new_sl = current_price - (trailing_distance * mt5.symbol_info(symbol).point)
                if new_sl > position.sl:
                    self._modify_position(position.ticket, new_sl, position.tp)
            else:  # SELL
                new_sl = current_price + (trailing_distance * mt5.symbol_info(symbol).point)
                if new_sl < position.sl or position.sl == 0:
                    self._modify_position(position.ticket, new_sl, position.tp)
    
    def _apply_break_even(self, position, current_price: float, profit_pips: float):
        """Applique le break-even"""
        be_activation = 30  # Activer après 30 pips de profit
        
        if profit_pips >= be_activation and position.sl != position.price_open:
            self._modify_position(position.ticket, position.price_open, position.tp)
            logger.info(f"Break-even appliqué sur position {position.ticket}")
    
    def _modify_position(self, ticket: int, sl: float, tp: float) -> bool:
        """Modifie une position existante"""
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": sl,
            "tp": tp,
        }
        
        result = mt5.order_send(request)
        return result.retcode == mt5.TRADE_RETCODE_DONE

class ExpertAdvisor:
    """Classe principale de l'Expert Advisor"""
    
    def __init__(self, config: Dict):
        self.symbols = config['symbols']  # ['BTCUSD', 'XAUUSD']
        self.timeframes = config['timeframes']  # ['H1', 'H4']
        self.mt5_credentials = config['mt5_credentials']
        
        self.trading_engine = TradingEngine(config)
        self.is_running = False
        
    def start(self):
        """Démarre l'EA"""
        logger.info("Démarrage de l'Expert Advisor...")
        self.is_running = True
        
        # Initialiser le gestionnaire de risque
        account_info = mt5.account_info()
        self.trading_engine.risk_manager = RiskManager(
            account_info.balance,
            risk_percent=1.0
        )
        
        # Boucle principale
        while self.is_running:
            try:
                self.run_iteration()
                time.sleep(60)  # Attendre 1 minute entre les itérations
                
            except KeyboardInterrupt:
                logger.info("Arrêt demandé par l'utilisateur")
                self.stop()
            except Exception as e:
                logger.error(f"Erreur dans la boucle principale: {e}")
                time.sleep(300)  # Attendre 5 minutes en cas d'erreur
    
    def run_iteration(self):
        """Exécute une itération de l'EA"""
        for symbol in self.symbols:
            for timeframe in self.timeframes:
                # 1. Récupérer les données
                data = self.trading_engine.fetch_market_data(
                    symbol, timeframe, 500)
                
                if data.empty:
                    continue
                
                # 2. Analyser le marché
                analysis = self.trading_engine.analyze_market_conditions(
                    symbol, data)
                
                # 3. Générer un setup de trade
                setup = self.trading_engine.generate_trade_setup(
                    symbol, analysis)
                
                if setup:
                    # 4. Vérifier les conditions de filtrage additionnelles
                    if self.additional_filters(setup, analysis):
                        # 5. Exécuter le trade
                        self.trading_engine.execute_trade(setup)
        
        # 6. Gérer les positions ouvertes
        self.trading_engine.manage_open_positions()
    
    def additional_filters(self, setup: TradeSetup, analysis: Dict) -> bool:
        """Filtres additionnels spécifiques à l'EA"""
        
        # Filtre de session US
        if analysis['session']['session'] not in ['us', 'overlap_london_us']:
            # Permettre les trades hors session US uniquement si score très élevé
            if analysis['trade_score']['confidence'] < 0.85:
                logger.info("Trade hors session US avec confiance insuffisante")
                return False
        
        # Filtre de volatilité
        if analysis['atr'] < analysis['atr'] * 0.5:  # Volatilité trop basse
            logger.info("Volatilité insuffisante")
            return False
        
        # Filtre de tendance multiple timeframe
        if not self.confirm_multi_timeframe(setup.symbol, setup.direction):
            logger.info("Tendance non confirmée sur timeframe supérieur")
            return False
        
        return True
    
    def confirm_multi_timeframe(self, symbol: str, direction: str) -> bool:
        """Confirme la tendance sur un timeframe supérieur"""
        higher_tf_data = self.trading_engine.fetch_market_data(
            symbol, 'H4', 100)
        
        if higher_tf_data.empty:
            return True
        
        # Vérifier la tendance H4 avec Ichimoku
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
        """Arrête l'EA"""
        self.is_running = False
        mt5.shutdown()
        logger.info("EA arrêté")

# Configuration et exécution
if __name__ == "__main__":
    import os
    
    config_path = "config.json"
    
    # Vérifier si le fichier de configuration existe
    if not os.path.exists(config_path):
        logger.error(f"Fichier {config_path} non trouve !")
        logger.error("Creez un fichier config.json avec vos identifiants MT5 et API News.")
        exit(1)
    
    # Charger la configuration depuis le fichier JSON
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        logger.info("[OK] Configuration chargee depuis config.json")
    except Exception as e:
        logger.error(f"Erreur lecture config.json: {e}")
        exit(1)
    
    # Vérifier les champs obligatoires
    required_fields = ['symbols', 'timeframes', 'mt5_credentials']
    for field in required_fields:
        if field not in config:
            logger.error(f"Champ '{field}' manquant dans config.json")
            exit(1)
    
    # Sécuriser l'affichage : masquer les données sensibles
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
    
    logger.info("=" * 50)
    logger.info("[CONFIG] Configuration chargee :")
    logger.info(f"  Symboles: {config.get('symbols', [])}")
    logger.info(f"  Timeframes: {config.get('timeframes', [])}")
    logger.info(f"  MT5 Login: {safe_config['mt5_credentials']['login']}")
    logger.info(f"  MT5 Server: {safe_config['mt5_credentials']['server']}")
    logger.info(f"  News API: {'Activee' if config.get('news_api', {}).get('enabled') else 'Desactivee'}")
    logger.info("=" * 50)
    
    # Créer et démarrer l'EA
    ea = ExpertAdvisor(config)
    
    try:
        ea.start()
    except KeyboardInterrupt:
        logger.info("Arret demande par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
    finally:
        ea.stop()