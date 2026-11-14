# MetaTrader 5 Algorithmic Trading Bot (v7)

Bot de trading algorithmique automatisé pour **MetaTrader 5**, développé en Python. Il intègre la gestion dynamique des régimes de marché, des filtres de sessions, une gestion des risques et des notifications en temps réel via Telegram.

---

## Structure & Composants

Le script est architecturé de manière modulaire :

* **Configuration & Utilitaires** : Chargement sécurisé de `config_v7.json`, calculs mathématiques et normalisation des volumes.
* **Logging** : Double journalisation (logs système dans la console/fichiers d'erreurs, et traçage des signaux/trades au format CSV).
* **TelegramNotifier** : Envoi de notifications asynchrones (entrées, sorties, erreurs, résumés quotidiens).
* **MT5Connection** : Gestion de la connexion, surveillance et reconnexion automatique au terminal MetaTrader 5.
* **MarketData** : Récupération et nettoyage des données historiques par unité de temps.
* **Indicators** : Bibliothèque d'indicateurs techniques (ATR, EMA, RSI, MACD, Bollinger, ADX, Supertrend, Ichimoku).
* **RegimeEngine** : Analyse des régimes de marché (tendances, volatilité, range).
* **SessionEngine** : Gestion des horaires de trading par session boursière (Londres, New York, Asie, Overlap).
* **NewsFilter** : Filtre d'actualités économiques basé sur des fichiers de verrouillage (`news_lock.json`).

---

## Prérequis

1. **Python 3.8+**
2. **MetaTrader 5** installé et ouvert localement avec un compte configuré.
3. Bibliothèques Python requises :
   ```bash
   pip install numpy pandas requests MetaTrader5


## Configuration
Le bot nécessite un fichier de configuration nommé config_v7.json placé à la racine du projet, contenant les sections indispensables suivantes : 
```bash
ea, mt5, symbols, timeframes, market_regime , indicators, signal_engine, risk_management, stop_loss, take_profit , position_management, sessions, news_filter, execution, filters, logging, state, telegram


## Utilisation
Lancez simplement le script principal : 
```bash
python nom_du_script.py