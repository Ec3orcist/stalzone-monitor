import os
import json
import requests
from datetime import datetime, timezone, timedelta

REGION = os.environ.get("REGION", "RU")
TOKEN_URL = "https://eapi.stalcraft.net/token"
API_URL = f"https://eapi.stalcraft.net/{REGION}/auction/lots"
DATA_FILE = "prices.json"

# Храним детально последние 24 часа, старые — по одной точке в час
DETAILED_WINDOW_HOURS = 24
MAX_TOTAL_POINTS = 720   # ~30 дней при часовой агрегации

def get_access_token():
    client_id = os.environ.get("CLIENT_ID")
    client_secret = os.environ.get("CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("CLIENT_ID и CLIENT_SECRET должны быть заданы.")
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }
    resp = requests.post(TOKEN_URL, data=payload, timeout=10)
    resp.raise_for_status()
    return resp.json().get("access_token")

def fetch_lots(token):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    resp = requests.get(API_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    elif isinstance(data, dict) and "lots" in data:
        return data["lots"]
    return []

def load_history():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_history(history):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))

def aggregate_history(records):
    """
    Оставляем детально последние DETAILED_WINDOW_HOURS часов.
    Всё, что старше — агрегируем по часам (берём минимальную цену за час).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=DETAILED_WINDOW_HOURS)

    fresh = []
    old = []
    for r in records:
        try:
            ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            fresh.append(r)
        else:
            old.append((ts, r))

    # Агрегация старых записей по часам
    buckets = {}
    for ts, r in old:
        key = ts.strftime("%Y-%m-%dT%H")
        price = r["price"]
        if key not in buckets:
            buckets[key] = {"prices": [price], "min_price": price}
        else:
            buckets[key]["prices"].append(price)
            buckets[key]["min_price"] = min(buckets[key]["min_price"], price)

    aggregated = []
    for key in sorted(buckets.keys()):
        aggregated.append({
            "timestamp": key + ":00:00+00:00",
            "price": buckets[key]["min_price"],
            "aggregated": True
        })

    result = aggregated + fresh
    # Жёсткий лимит по количеству точек
    if len(result) > MAX_TOTAL_POINTS:
        result = result[-MAX_TOTAL_POINTS:]
    return result

def main():
    print("Запуск сбора данных...")
    token = get_access_token()
    lots = fetch_lots(token)
    print(f"Получено лотов: {len(lots)}")
    if not lots:
        return

    history = load_history()
    now = datetime.now(timezone.utc).isoformat()

    item_prices = {}
    for lot in lots:
        item_id = lot.get("itemId")
        price = lot.get("price")
        if item_id is not None and price is not None:
            item_prices.setdefault(item_id, []).append(price)

    for item_id, prices in item_prices.items():
        if not prices:
            continue
        record = {
            "timestamp": now,
            "price": min(prices),
            "all_prices": prices
        }
        history.setdefault(item_id, []).append(record)
        history[item_id] = aggregate_history(history[item_id])

    save_history(history)
    print(f"Обновлена история для {len(item_prices)} предметов.")

if __name__ == "__main__":
    main()
