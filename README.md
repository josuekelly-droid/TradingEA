# Expert Advisor Pro V6.0 - Institutional Grade

Automated trading system for **BTC/USD** and **Gold/USD** on MetaTrader 5 platform.

## Overview

This Expert Advisor implements a multi-timeframe trend-following strategy using professional technical indicators (SuperTrend, Ichimoku, RSI, MACD, Bollinger Bands) combined with session analysis, spread control, and economic calendar filtering.

## Key Features

### Trading Logic
- **Multi-Timeframe Analysis**: H1 and H4 timeframes
- **Anti-Hedging Protection**: Symbol lock prevents conflicting H1/H4 positions
- **Bar Close Execution**: Trades are triggered only on new candle formation
- **Weighted Scoring System**: SuperTrend (2pts), Ichimoku (3pts), RSI (1pt), MACD (1.5pts), Session (2pts)
- **3 Take-Profit Levels**: Configurable ATR-based multipliers (1.5x, 3.0x, 5.0x)
- **Lot Distribution**: 40% / 30% / 30% across TP levels

### Risk Management
- **Position Sizing**: 1% risk per trade based on account balance
- **Daily Loss Limit**: 2% maximum daily loss (hard stop)
- **Daily Trade Limit**: Maximum 3 trades per day
- **Spread Control**: Configurable max spread per symbol (BTC: 50.0, XAU: 5.0)
- **ATR-Based Stop Loss**: Dynamic SL at 2.0x ATR

### Position Management
- **Dynamic Trailing Stop**: Activates at 1.5x ATR profit, trails at 1.0x ATR distance
- **Break-Even Protection**: Activates at 1.0x ATR profit, sets SL to entry + 0.2x ATR
- **Magic Number Isolation**: All positions tagged with unique Magic Number (123456)
- **Startup Reconciliation**: Re-syncs all positions on EA restart

### Session & News Filters
- **Session Trading**: US and London sessions enabled, Asian disabled
- **High Confidence Required**: 70%+ confidence outside US session
- **Economic Calendar**: FED/NFP high-impact news filtering via MQL5 script
- **Volatility Filter**: Minimum ATR threshold (50% of 20-period average)

### Monitoring & Alerts
- **Telegram Notifications**: Real-time alerts with full trade details
- **Daily Report**: Automatic performance summary at 23:00
- **Detailed Logging**: File and console output with timestamps
- **Connection Monitoring**: Auto-reconnect on MT5 connection loss

## Requirements

- Python 3.10+
- MetaTrader 5 platform
- MetaTrader5 Python package
- Required Python packages: `numpy`, `pandas`, `requests`, `metatrader5`

## Installation

1. Clone or download the repository
2. Install dependencies:
   ```bash
   pip install numpy pandas requests MetaTrader5


3. Configure config.json with your MT5 credentials
4. Launch the EA:
python ea_trading.py

Configuration
Edit config.json to customize all parameters:

{
  "test_mode": false,
  "symbols": ["BTCUSD", "XAUUSD"],
  "timeframes": ["H1", "H4"],
  "mt5_credentials": {
    "login": 12345678,
    "password": "your_password",
    "server": "YourBroker-Demo"
  },
  "risk_management": {
    "risk_percent": 1.0,
    "max_daily_loss_percent": 2.0,
    "max_positions": 3,
    "max_trades_per_day": 3,
    "max_spread": {
      "BTCUSD": 50.0,
      "XAUUSD": 5.0
    }
  },
  "trailing_settings": {
    "trailing_activation_atr": 1.5,
    "trailing_distance_atr": 1.0,
    "breakeven_activation_atr": 1.0,
    "breakeven_offset_atr": 0.2
  },
  "trading_hours": {
    "trade_us_session": true,
    "trade_london_session": true,
    "trade_asian_session": false,
    "us_session_only_high_confidence": true
  },
  "indicators": {
    "supertrend_period": 10,
    "supertrend_multiplier": 3.0,
    "rsi_period": 14,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "bollinger_period": 20,
    "bollinger_std": 2.0
  },
  "tp_sl_settings": {
    "tp1_atr_multiplier": 1.5,
    "tp2_atr_multiplier": 3.0,
    "tp3_atr_multiplier": 5.0,
    "sl_atr_multiplier": 2.0,
    "lot_distribution": [0.4, 0.3, 0.3]
  },
  "news_api": {
    "api_key": "",
    "enabled": false,
    "provider": "newsapi"
  },
  "telegram": {
    "enabled": true,
    "bot_token": "YOUR_BOT_TOKEN",
    "chat_id": "YOUR_CHAT_ID"
  }
}


Economic Calendar Setup
The EA uses a native MT5 economic calendar via an external MQL5 script:

Compile CalendarFilter.mq5 in MetaEditor

Launch the script in MT5 (any chart)

The script writes news_lock.json to MT5's MQL5/Files/ folder

The Python EA reads this file to detect high-impact news events

Trading is blocked 30 minutes before and after FED/NFP events

Architecture //

ea_trading.py          # Main EA (Python)
├── MT5EconomicCalendar  # News filter via MQL5 bridge
├── NewsAnalyzer         # News sentiment (disabled by default)
├── ProfessionalIndicators # SuperTrend, Ichimoku, RSI, MACD, Bollinger
├── SessionAnalyzer      # Market session detection
├── RiskManager          # Position sizing, TP/SL, daily limits
├── TradingEngine        # Trade execution and position management
└── ExpertAdvisor        # Main loop and orchestration

CalendarFilter.mq5     # MQL5 script for economic calendar
config.json            # All configuration parameters
ea_trading.log         # Execution logs


Scoring System
Indicator	Weight	Signal
SuperTrend	±2.0	Trend direction
Ichimoku	±3.0	Cloud position + TK cross
RSI	±1.0	Above/below 50
MACD	±1.5	Histogram direction
Session	+2.0×	Session weight (US: 1.5, London: 1.0)
Direction threshold: Score > +2 → BUY, Score < -2 → SELL
Confidence threshold: 60% minimum to generate trade

Position Management Flow
Entry: 3 separate orders with individual TP levels

Trailing Activation: When profit exceeds 1.5× ATR

Break-Even Activation: When profit exceeds 1.0× ATR

Trailing Distance: SL follows price at 1.0× ATR distance

Safety Features
Test mode available (simulated trades, no real orders)

Maximum 3 concurrent positions per symbol

Spread protection prevents trading during high volatility

Connection health check every 5 minutes with auto-reconnect

Graceful shutdown on keyboard interrupt

Logs
Logs are written to both console and ea_trading.log file with the following format:

2026-06-22 19:31:30,485 - INFO - [OK] Trade TP1 executed: Ticket 733209906

Telegram Notification Example:

📊 [EA PRO] TRADE OUVERT - 2026-06-22 19:42:47

XAUUSD | BUY | Timeframe: H1
Session: us
Prix d'entrée: 4180.71

🛑 Stop Loss: 4142.46
🎯 TP1: 4209.40
🎯 TP2: 4238.08
🎯 TP3: 4276.33

📦 Lots: 4.000 / 3.000 / 3.000
✅ Confiance: 69.5%
💰 Risque: 40126.24€ (1.0%)

📈 Signaux: ST Haussier, Ich Haussier


Files Structure //

C:\TradingEA\
├── ea_trading.py          # Main EA script
├── config.json            # Configuration file
├── CalendarFilter.mq5     # MQL5 calendar script
├── CalendarFilter.ex5     # Compiled MQL5 script
├── news_lock.json         # Bridge file (auto-generated)
├── ea_trading.log         # Execution log
└── README.md              # This file


Disclaimer
This software is for educational purposes. Trading involves substantial risk of loss. Past performance does not guarantee future results. Test thoroughly on demo accounts before live deployment.

Version History
V6.0 - Institutional Grade

Anti-hedging H1/H4 symbol lock

Dynamic trailing & break-even from config.json

Spread control & daily trade limit

Daily report at 23:00

Startup position reconciliation

Telegram notifications

MT5 native economic calendar via MQL5 bridge

Connection auto-reconnect

Test mode support