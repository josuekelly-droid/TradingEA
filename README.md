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