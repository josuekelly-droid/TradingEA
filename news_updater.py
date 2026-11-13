# news_updater.py
import requests
import json
import datetime
from pathlib import Path

# ==========================================
# CONFIGURATION
# ==========================================
NEWS_FILE = Path(__file__).resolve().parent / "news_lock.json"
MINUTES_BEFORE = 30
MINUTES_AFTER = 30
IMPACT_LEVEL = "High"

# On surveille les devises majeures. 
# USD est le plus critique pour XAUUSD et BTCUSD.
CURRENCIES = ["USD", "EUR", "GBP", "JPY"]

def fetch_news():
    """Récupère le calendrier économique de la semaine (ForexFactory)"""
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[ERROR] Impossible de récupérer les news: {e}")
        return []

def update_lock_file():
    news_data = fetch_news()
    now = datetime.datetime.now(datetime.timezone.utc)
    
    blackout_active = False
    blackout_end = None
    active_event = None

    for event in news_data:
        # Filtrer par impact (High = Rouge) et par devise
        if event.get("impact") != IMPACT_LEVEL:
            continue
        if event.get("country") not in CURRENCIES:
            continue

        # Convertir l'heure de la news en UTC (le flux ForexFactory donne l'heure EST/EDT)
        try:
            event_time = datetime.datetime.fromisoformat(event["date"])
            event_time_utc = event_time.astimezone(datetime.timezone.utc)
        except Exception:
            continue

        # Calculer la fenêtre de blocage
        start_window = event_time_utc - datetime.timedelta(minutes=MINUTES_BEFORE)
        end_window = event_time_utc + datetime.timedelta(minutes=MINUTES_AFTER)

        # Si l'heure actuelle est DANS la fenêtre
        if start_window <= now <= end_window:
            blackout_active = True
            blackout_end = end_window
            active_event = event
            break # On s'arrête à la première news active

    # Préparer le fichier JSON pour l'EA
    if blackout_active:
        lock_data = {
            "blackout": True,
            "blackout_end": blackout_end.isoformat(),
            "event_title": active_event.get("title"),
            "event_time": event_time_utc.isoformat(),
            "country": active_event.get("country")
        }
        print(f"[{now.strftime('%H:%M:%S')}] 🚫 BLACKOUT ACTIF : {lock_data['country']} {lock_data['event_title']} (jusqu'à {lock_data['blackout_end']})")
    else:
        lock_data = {
            "blackout": False,
            "blackout_end": now.isoformat()
        }
        print(f"[{now.strftime('%H:%M:%S')}] ✅ Aucune news bloquante. Le trading est autorisé.")

    # Écriture du fichier
    with open(NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(lock_data, f, indent=4)

if __name__ == "__main__":
    update_lock_file()