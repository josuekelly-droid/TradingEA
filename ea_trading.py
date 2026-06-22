"""
Expert Advisor Professionnel - BTC/USD & Gold/USD
Auteur: Trading System Pro
Version: 6.0 - Institutional Grade
- Verrouillage par symbole (Anti-Hedging H1/H4)
- Trailing & Break-Even dynamiques (config.json)
- Contrôle du spread et Limite de trades journaliers
- Rapport quotidien automatique (23h00)
- Réconciliation des positions au démarrage
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

# Constante globale pour le Magic Number
MAGIC_NUMBER = 123456

@dataclass
class TradeSetup:
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
    def __init__(self, minutes_before: int = 30, minutes_after: int = 30):
        self.minutes_before = minutes_before
        self.minutes_after = minutes_after
        self.enabled = True
        
    def is_blackout_period(self, symbol: str) -> bool:
        if not self.enabled: return False
        try:
            now = datetime.now()
            events = mt5.calendar_events(now - timedelta(hours=2), now + timedelta(hours=2))
            if events is None: return False
            for event in events:
                if event.importance == 3:
                    time_diff = (now - event.time).total_seconds() / 60
                    if -self.minutes_before <= time_diff < 0: return True
                    elif 0 <= time_diff < self.minutes_after: return True
        except Exception as e:
            logger.error(f"Erreur calendrier économique: {e}")
        return False

class NewsAnalyzer:
    def __init__(self, config: Dict = None):
        self.enabled = config.get('enabled', False) if isinstance(config, dict) else False
        
        # Alerte explicite au démarrage
        if not self.enabled:
            logger.warning("="*60)
            logger.warning("[ATTENTION] L'ANALYSE DES NEWS EST DÉSACTIVÉE !")
            logger.warning("L'EA ne prendra pas en compte le sentiment du marché.")
            logger.warning("Le filtre du calendrier MT5 (FED/NFP) reste actif.")
            logger.warning("="*60)
            
    def analyze_news_impact(self, symbol: str) -> Dict:
        return {'impact_score': 0, 'bias': 'neutral'}

class ProfessionalIndicators:
    @staticmethod
    def calculate_supertrend(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
        atr = ProfessionalIndicators.calculate_atr(high, low, close, period)
        hl2 = (high + low) / 2
        upper_band = (hl2 + (multiplier * atr)).copy()
        lower_band = (hl2 - (multiplier * atr)).copy()
        supertrend = pd.Series(index=close.index, dtype=float).copy()
        direction = pd.Series(index=close.index, dtype=int).copy()
        
        for i in range(1, len(close)):
            if close.iloc[i] > upper_band.iloc[i-1]: direction.iloc[i] = 1
            elif close.iloc[i] < lower_band.iloc[i-1]: direction.iloc[i] = -1
            else:
                direction.iloc[i] = direction.iloc[i-1]
                if direction.iloc[i] == 1 and lower_band.iloc[i] < lower_band.iloc[i-1]: lower_band.iloc[i] = lower_band.iloc[i-1]
                if direction.iloc[i] == -1 and upper_band.iloc[i] > upper_band.iloc[i-1]: upper_band.iloc[i] = upper_band.iloc[i-1]
            supertrend.iloc[i] = lower_band.iloc[i] if direction.iloc[i] == 1 else upper_band.iloc[i]
        return pd.DataFrame({'supertrend': supertrend, 'direction': direction})
    
    @staticmethod
    def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def calculate_ichimoku(high: pd.Series, low: pd.Series, close: pd.Series) -> Dict:
        t9 = (high.rolling(9).max() + low.rolling(9).min()) / 2
        k26 = (high.rolling(26).max() + low.rolling(26).min()) / 2
        ssa = ((t9 + k26) / 2).shift(26)
        ssb = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
        return {'tenkan_sen': t9, 'kijun_sen': k26, 'senkou_span_a': ssa, 'senkou_span_b': ssb}
    
    @staticmethod
    def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        return 100 - (100 / (1 + gain / loss))
    
    @staticmethod
    def calculate_macd(close: pd.Series, fast: int, slow: int, signal: int) -> Dict:
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        return {'macd': macd_line, 'signal': signal_line, 'histogram': macd_line - signal_line}
    
    @staticmethod
    def calculate_bollinger_bands(close: pd.Series, period: int, std_dev: float) -> Dict:
        sma = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        return {'middle': sma, 'upper': sma + (std * std_dev), 'lower': sma - (std * std_dev)}

class SessionAnalyzer:
    def __init__(self):
        self.sessions = {'asian': (0, 9), 'london': (8, 17), 'us': (13, 22), 'overlap_london_us': (13, 17)}
        self.weights = {'BTCUSD': {'us': 1.5, 'london': 1.0, 'asian': 0.7, 'overlap_london_us': 2.0},
                        'XAUUSD': {'us': 1.4, 'london': 1.3, 'asian': 0.5, 'overlap_london_us': 1.8}}
    def get_current_session(self, timestamp: datetime) -> str:
        h = timestamp.hour
        for s, (start, end) in self.sessions.items():
            if start <= h < end: return s
        return None
    def get_session_weight(self, symbol: str, session: str) -> float:
        return self.weights.get(symbol, {}).get(session, 1.0)

class RiskManager:
    def __init__(self, account_balance: float, risk_config: Dict, tp_sl_config: Dict):
        self.account_balance = account_balance
        self.risk_percent = risk_config.get('risk_percent', 1.0)
        self.max_daily_loss = risk_config.get('max_daily_loss_percent', 2.0)
        self.max_trades_per_day = risk_config.get('max_trades_per_day', 3)
        self.tp_sl_config = tp_sl_config
        
    def get_daily_pnl(self) -> float:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(today_start, datetime.now())
        if deals:
            return sum(d.profit for d in deals if d.entry == mt5.DEAL_ENTRY_OUT and d.magic == MAGIC_NUMBER)
        return 0.0
        
    def is_daily_loss_limit_reached(self) -> bool:
        pnl = self.get_daily_pnl()
        if pnl <= -abs(self.account_balance * (self.max_daily_loss / 100)):
            logger.warning(f"[RISQUE MAX] Perte quotidienne limite atteinte! PnL: {pnl}$")
            return True
        return False
        
    def get_trades_today(self) -> int:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(today_start, datetime.now())
        if deals:
            return len([d for d in deals if d.entry == mt5.DEAL_ENTRY_IN and d.magic == MAGIC_NUMBER])
        return 0
        
    def is_max_trades_reached(self) -> bool:
        if self.get_trades_today() >= self.max_trades_per_day:
            logger.warning(f"[SURTRADING] Limite de trades quotidiens atteinte ({self.max_trades_per_day})")
            return True
        return False

    def calculate_position_size(self, entry_price: float, stop_loss: float, symbol: str) -> float:
        risk_amount = self.account_balance * (self.risk_percent / 100)
        stop_distance = abs(entry_price - stop_loss)
        pip_value = 1.0 if 'BTC' in symbol else 10.0
        lot_size = risk_amount / (stop_distance * pip_value)
        max_lot = 1.0 if 'BTC' in symbol else 10.0
        return min(lot_size, max_lot)
    
    def calculate_tp_levels(self, entry_price: float, direction: str, atr_value: float) -> List[float]:
        m = [self.tp_sl_config.get(f'tp{i}_atr_multiplier', i*1.5) for i in [1, 2, 3]]
        if direction == 'BUY': return [entry_price + atr_value * mult for mult in m]
        else: return [entry_price - atr_value * mult for mult in m]
    
    def calculate_stop_loss(self, entry_price: float, direction: str, atr_value: float) -> float:
        sl_mult = self.tp_sl_config.get('sl_atr_multiplier', 2.0)
        return entry_price - (atr_value * sl_mult) if direction == 'BUY' else entry_price + (atr_value * sl_mult)

class TradingEngine:
    def __init__(self, config: Dict):
        self.config = config
        self.test_mode = config.get('test_mode', False)
        self.indicators = ProfessionalIndicators()
        self.news_analyzer = NewsAnalyzer(config.get('news_api', {}))
        self.mt5_calendar = MT5EconomicCalendar()
        self.session_analyzer = SessionAnalyzer()
        self.risk_manager = None
        self.mt5_credentials = config.get('mt5_credentials', {})
        self._initialize_mt5(self.mt5_credentials)
        
    def _initialize_mt5(self, credentials: Dict) -> bool:
        if not credentials: return False
        if not mt5.initialize(): return False
        if not mt5.login(credentials['login'], password=credentials['password'], server=credentials['server']):
            logger.error(f"Echec auth MT5: {mt5.last_error()}"); return False
        logger.info("Connexion MT5 reussie."); return True
        
    def check_connection(self) -> bool:
        if mt5.terminal_info() is None or mt5.account_info() is None:
            logger.error("Connexion MT5 perdue. Reconnexion..."); mt5.shutdown(); time.sleep(5)
            return self._initialize_mt5(self.mt5_credentials)
        return True
    
    def fetch_market_data(self, symbol: str, timeframe: str, num_bars: int = 500) -> pd.DataFrame:
        tf_map = {'H1': mt5.TIMEFRAME_H1, 'H4': mt5.TIMEFRAME_H4}
        rates = mt5.copy_rates_from_pos(symbol, tf_map.get(timeframe, mt5.TIMEFRAME_H1), 0, num_bars)
        if rates is None: return pd.DataFrame()
        df = pd.DataFrame(rates); df['time'] = pd.to_datetime(df['time'], unit='s'); df.set_index('time', inplace=True)
        return df
    
    def analyze_market_conditions(self, symbol: str, data: pd.DataFrame) -> Dict:
        ind_cfg = self.config.get('indicators', {})
        st = self.indicators.calculate_supertrend(data['high'], data['low'], data['close'], ind_cfg.get('supertrend_period', 10), ind_cfg.get('supertrend_multiplier', 3.0))
        ich = self.indicators.calculate_ichimoku(data['high'], data['low'], data['close'])
        rsi = self.indicators.calculate_rsi(data['close'], ind_cfg.get('rsi_period', 14))
        macd = self.indicators.calculate_macd(data['close'], ind_cfg.get('macd_fast', 12), ind_cfg.get('macd_slow', 26), ind_cfg.get('macd_signal', 9))
        bb = self.indicators.calculate_bollinger_bands(data['close'], ind_cfg.get('bollinger_period', 20), ind_cfg.get('bollinger_std', 2.0))
        atr = self.indicators.calculate_atr(data['high'], data['low'], data['close'])
        
        score, signals = 0, []
        if st['direction'].iloc[-1] == 1: score += 2; signals.append("ST Haussier")
        else: score -= 2; signals.append("ST Baissier")
        
        if ich['tenkan_sen'].iloc[-1] > ich['kijun_sen'].iloc[-1] and data['close'].iloc[-1] > ich['senkou_span_a'].iloc[-1]: score += 3; signals.append("Ich Haussier")
        elif ich['tenkan_sen'].iloc[-1] < ich['kijun_sen'].iloc[-1] and data['close'].iloc[-1] < ich['senkou_span_b'].iloc[-1]: score -= 3; signals.append("Ich Baissier")
        
        if rsi.iloc[-1] > 50: score += 1
        elif rsi.iloc[-1] < 50: score -= 1
        
        if macd['histogram'].iloc[-1] > 0: score += 1.5
        else: score -= 1.5
        
        session = self.session_analyzer.get_current_session(datetime.now())
        score += 2 * self.session_analyzer.get_session_weight(symbol, session)
        
        max_score = 10.5
        norm_score = max(-max_score, min(max_score, score))
        return {
            'atr': atr.iloc[-1], 'atr_series': atr, 'session': session, 'current_price': data['close'].iloc[-1],
            'trade_score': {
                'direction': 'BUY' if norm_score > 2 else 'SELL' if norm_score < -2 else 'NEUTRAL',
                'confidence': abs(norm_score) / max_score, 'signals': signals
            }
        }
    
    def generate_trade_setup(self, symbol: str, analysis: Dict) -> Optional[TradeSetup]:
        ts = analysis['trade_score']
        if ts['confidence'] < 0.60 or ts['direction'] == 'NEUTRAL': return None
        
        entry = analysis['current_price']; atr = analysis['atr']
        sl = self.risk_manager.calculate_stop_loss(entry, ts['direction'], atr)
        tps = self.risk_manager.calculate_tp_levels(entry, ts['direction'], atr)
        total_lot = self.risk_manager.calculate_position_size(entry, sl, symbol)
        lots = [total_lot * d for d in self.risk_manager.tp_sl_config.get('lot_distribution', [0.4, 0.3, 0.3])]
        
        return TradeSetup(symbol, ts['direction'], entry, sl, tps[0], tps[1], tps[2], lots, ts['confidence'], datetime.now())
    
    def _pre_trade_checks(self, setup: TradeSetup) -> bool:
        tick = mt5.symbol_info_tick(setup.symbol)
        if tick is None: return False
        
        # 1. Vérification stricte du Spread
        spread = tick.ask - tick.bid
        max_spread = self.config.get('risk_management', {}).get('max_spread', {}).get(setup.symbol, 10.0)
        if spread > max_spread:
            logger.warning(f"Spread trop élevé pour {setup.symbol}: {spread} > {max_spread}")
            return False
            
        # 2. Vérification des trades journaliers
        if self.risk_manager.is_max_trades_reached(): return False
        
        return True
    
    def execute_trade(self, setup: TradeSetup, timeframe: str) -> bool:
        if not self._pre_trade_checks(setup): return False
        
        if self.test_mode:
            logger.info(f"[TEST MODE] Trade simulé: {setup.symbol} {setup.direction} Prix: {setup.entry_price}")
            return True
            
        info = mt5.symbol_info(setup.symbol)
        filling = mt5.ORDER_FILLING_FOK if (info.filling_mode & mt5.SYMBOL_FILLING_FOK) else mt5.ORDER_FILLING_IOC
        
        for i, (tp, lot) in enumerate(zip([setup.tp1_price, setup.tp2_price, setup.tp3_price], setup.lot_sizes)):
            order_type = mt5.ORDER_TYPE_BUY if setup.direction == 'BUY' else mt5.ORDER_TYPE_SELL
            price = info.ask if setup.direction == 'BUY' else info.bid
            vol = max(info.volume_min, round(float(lot) / info.volume_step) * info.volume_step)
            
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": setup.symbol, "volume": round(vol, 2),
                   "type": order_type, "price": round(price, info.digits), "sl": round(float(setup.sl_price), info.digits),
                   "tp": round(float(tp), info.digits), "deviation": 30, "magic": MAGIC_NUMBER, 
                   "comment": f"EA_Pro_{timeframe}_TP{i+1}", "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling}
            
            res = mt5.order_send(req)
            if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(f"Erreur exécution TP{i+1}: {mt5.last_error()}"); return False
            logger.info(f"[OK] Trade TP{i+1} exécuté: Ticket {res.order}")
        return True
    
    def manage_open_positions(self):
        positions = mt5.positions_get(magic=MAGIC_NUMBER)
        if positions is None: return
        
        trail_cfg = self.config.get('trailing_settings', {})
        trail_act = trail_cfg.get('trailing_activation_atr', 1.5)
        trail_dist = trail_cfg.get('trailing_distance_atr', 1.0)
        be_act = trail_cfg.get('breakeven_activation_atr', 1.0)
        be_off = trail_cfg.get('breakeven_offset_atr', 0.2)
        
        for pos in positions:
            data = self.fetch_market_data(pos.symbol, 'H1', 20)
            if data.empty: continue
            atr = ProfessionalIndicators.calculate_atr(data['high'], data['low'], data['close']).iloc[-1]
            
            tick = mt5.symbol_info_tick(pos.symbol)
            curr_price = tick.bid if pos.type == 0 else tick.ask
            profit = (curr_price - pos.price_open) if pos.type == 0 else (pos.price_open - curr_price)
                
            # Trailing Stop Dynamique
            if profit >= atr * trail_act:
                if pos.type == 0: # BUY
                    new_sl = curr_price - (atr * trail_dist)
                    if new_sl > pos.sl: self._modify_position(pos.ticket, new_sl, pos.tp)
                else: # SELL
                    new_sl = curr_price + (atr * trail_dist)
                    if pos.sl == 0 or new_sl < pos.sl: self._modify_position(pos.ticket, new_sl, pos.tp)
            
            # Break-Even Dynamique
            if profit >= atr * be_act:
                if pos.type == 0: new_sl = pos.price_open + (atr * be_off)
                else: new_sl = pos.price_open - (atr * be_off)
                
                if (pos.type == 0 and (pos.sl == 0 or new_sl > pos.sl)) or \
                   (pos.type == 1 and (pos.sl == 0 or new_sl < pos.sl)):
                    self._modify_position(pos.ticket, new_sl, pos.tp)

    def _modify_position(self, ticket: int, sl: float, tp: float) -> bool:
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": sl, "tp": tp}
        res = mt5.order_send(req)
        return res is not None and res.retcode == mt5.TRADE_RETCODE_DONE

class ExpertAdvisor:
    def __init__(self, config: Dict):
        self.config = config
        self.symbols = config['symbols']
        self.timeframes = config['timeframes']
        self.trading_engine = TradingEngine(config)
        self.is_running = False
        self.last_report_date = None
        
    def start(self):
        logger.info("Démarrage EA (Mode Institutionnel V6)...")
        self.is_running = True
        acc = mt5.account_info()
        if acc is None: return
        
        self.trading_engine.risk_manager = RiskManager(acc.balance, self.config.get('risk_management', {}), self.config.get('tp_sl_settings', {}))
        
        # 7. Réconciliation au démarrage (Force la mise à jour de toutes les positions)
        logger.info("Réconciliation des positions ouvertes au démarrage...")
        self.trading_engine.manage_open_positions()
        
        last_bar_time = {}
        last_conn_check = time.time()
        
        while self.is_running:
            try:
                # 6. Rapport Quotidien à 23h00
                self._generate_daily_report()
                
                # Vérification connexion toutes les 5 min
                if time.time() - last_conn_check > 300:
                    if not self.trading_engine.check_connection():
                        time.sleep(60); continue
                    last_conn_check = time.time()
                
                # Gestion des positions à chaque boucle
                self.trading_engine.manage_open_positions()
                
                # Analyse (Uniquement à la nouvelle bougie)
                for symbol in self.symbols:
                    for tf in self.timeframes:
                        tf_map = {'H1': mt5.TIMEFRAME_H1, 'H4': mt5.TIMEFRAME_H4}
                        rates = mt5.copy_rates_from_pos(symbol, tf_map.get(tf, mt5.TIMEFRAME_H1), 0, 2)
                        if rates is None or len(rates) < 2: continue
                        
                        key = f"{symbol}_{tf}"
                        if last_bar_time.get(key) != rates[-1]['time']:
                            last_bar_time[key] = rates[-1]['time']
                            self.run_iteration_for_symbol(symbol, tf)
                            
                time.sleep(10)
            except KeyboardInterrupt:
                self.stop()
            except Exception as e:
                logger.error(f"Erreur boucle principale: {e}"); time.sleep(60)

    def run_iteration_for_symbol(self, symbol: str, timeframe: str):
        if self.trading_engine.risk_manager.is_daily_loss_limit_reached(): return
        
        # 1. Verrouillage par Symbole (Anti-Hedging H1/H4)
        positions = mt5.positions_get(symbol=symbol, magic=MAGIC_NUMBER)
        if positions and len(positions) > 0:
            return # On a déjà un setup actif sur ce symbole, on bloque
            
        data = self.trading_engine.fetch_market_data(symbol, timeframe, 500)
        if data.empty: return
        
        analysis = self.trading_engine.analyze_market_conditions(symbol, data)
        setup = self.trading_engine.generate_trade_setup(symbol, analysis)
        
        if setup and self.additional_filters(setup, analysis):
            self.trading_engine.execute_trade(setup, timeframe)
    
    def additional_filters(self, setup: TradeSetup, analysis: Dict) -> bool:
        th = self.config.get('trading_hours', {})
        sess = analysis['session']; conf = analysis['trade_score']['confidence']
        
        allowed = []
        if th.get('trade_us_session', True): allowed.extend(['us', 'overlap_london_us'])
        if th.get('trade_london_session', True): allowed.append('london')
        if th.get('trade_asian_session', False): allowed.append('asian')
            
        if sess not in allowed: return False
        if th.get('us_session_only_high_confidence', True) and sess not in ['us', 'overlap_london_us'] and conf < 0.70: return False
        if analysis['atr'] < analysis['atr_series'].rolling(20).mean().iloc[-1] * 0.5: return False
        if self.trading_engine.mt5_calendar.is_blackout_period(setup.symbol): return False
        return True

    def _generate_daily_report(self):
        now = datetime.now()
        if now.hour == 23 and (self.last_report_date is None or self.last_report_date < now.date()):
            pnl = self.trading_engine.risk_manager.get_daily_pnl()
            trades = self.trading_engine.risk_manager.get_trades_today()
            logger.info("="*50)
            logger.info("[RAPPORT QUOTIDIEN DE FIN DE JOURNÉE]")
            logger.info(f"  Date: {now.date()}")
            logger.info(f"  Trades exécutés aujourd'hui: {trades}")
            logger.info(f"  PnL Réalisé: {pnl:.2f}$")
            logger.info(f"  Limite de perte: {self.trading_engine.risk_manager.max_daily_loss}%")
            logger.info("="*50)
            self.last_report_date = now.date()

    def stop(self):
        self.is_running = False
        mt5.shutdown()
        logger.info("EA arrêté.")

if __name__ == "__main__":
    config_path = "config.json"
    if not os.path.exists(config_path):
        logger.error(f"Fichier {config_path} non trouvé !"); exit(1)
        
    with open(config_path, 'r') as f:
        config = json.load(f)
        
    ea = ExpertAdvisor(config)
    try: ea.start()
    except Exception as e: logger.error(f"Erreur fatale: {e}")
    finally: ea.stop()