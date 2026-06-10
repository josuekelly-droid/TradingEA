# 🤖 Expert Advisor Pro - BTC/USD & Gold/USD

![Version](https://img.shields.io/badge/version-3.0-blue)
![Python](https://img.shields.io/badge/python-3.9+-green)
![MT5](https://img.shields.io/badge/MT5-Compatible-orange)
![Licence](https://img.shields.io/badge/licence-MIT-yellow)

Expert Advisor professionnel pour le trading automatique de **BTC/USD** et **Gold/USD (XAUUSD)** sur MetaTrader 5.

## ✨ Fonctionnalités

### 📊 Analyse Technique Multi-Indicateurs
- **SuperTrend** - Détection de tendance
- **Ichimoku Kinko Hyo** - Support/Résistance avancé
- **RSI** - Momentum
- **MACD** - Force de tendance
- **Bandes de Bollinger** - Volatilité

### 📰 Analyse Fondamentale
- Intégration **NewsAPI.org** pour les nouvelles économiques
- Analyse de sentiment automatique
- Pondération par source (Reuters, Bloomberg, etc.)
- Cache intelligent pour économiser les requêtes API

### 🕐 Sessions de Marché
- Session Asiatique
- Session Londres
- **Session US (prioritaire)**
- Overlap Londres-US

### 🎯 Gestion des Trades
- **3 Take Profits** échelonnés (40%/30%/30%)
- Stop Loss dynamique basé sur l'ATR
- Trailing Stop automatique
- Break-even automatique

### 🛡️ Risk Management
- Risque maximum : **1% par trade**
- Perte quotidienne max : **2%**
- Maximum **3 positions simultanées**
- Filtre de spread

## 🚀 Installation Rapide

```bash
# Cloner le repo
git clone https://github.com/josuekelly-droid/TradingEA.git
cd TradingEA

# Créer l'environnement virtuel
python -m venv venv

# Activer (Windows)
venv\Scripts\activate

# Installer les dépendances
pip install MetaTrader5 pandas numpy requests