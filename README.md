# Expert Advisor Pro - BTC/USD & Gold/USD

Expert Advisor professionnel pour le trading automatique de BTC/USD et Gold/USD sur MetaTrader 5.

## 🚀 Fonctionnalités

- 📊 Analyse multi-indicateurs (SuperTrend, Ichimoku, RSI, MACD, Bollinger Bands)
- 📰 Analyse des news économiques via NewsAPI.org
- 🕐 Détection des sessions de marché (Asian, London, US)
- 🎯 3 Take Profits avec gestion dynamique
- 🛡️ Risk management professionnel (1% risque par trade)
- 📈 Trailing stop automatique
- 🔒 Break-even automatique
- 💾 Cache des news pour économiser les requêtes API

## 📦 Installation

```bash
# Cloner le repo
git clone https://github.com/josuekelly-droid/TradingEA.git
cd TradingEA

# Créer l'environnement virtuel
python -m venv venv

# Activer l'environnement
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# Installer les dépendances
pip install -r requirements.txt