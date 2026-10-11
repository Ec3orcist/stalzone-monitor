import os
import json
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# --- Настройки ---
REGION = os.environ.get("REGION", "RU")
TOKEN_URL = "https://exbo.net/oauth/token"
DATA_FILE = "prices.json"
ITEM_DB_DIR = "itemdb/ru/items"
MAX_HISTORY = 500
MAX_ITEMS = 300
WORKERS = 15

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

def get_access_token():
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": ""
    }
    resp = requests.post(TOKEN_URL, data=payload, timeout=10)
    resp.raise_for_status()
    return resp.json().get("access_token")

def discover_tradable_item_ids():
    """
    Читает базу предметов и возвращает только те ID,
    которые НЕ являются персональными (можно продать на аукционе).
    """
    ids = []
    root = Path(ITEM_DB_DIR)
    if not root.exists():
        print(f"❌ Папка {ITEM_DB_DIR} не найдена.")
        return ids

    all_files = list(root.rglob("*.json"))
    print(f"Всего файлов в базе: {len(all_files)}")

    skipped_personal = 0
    for path in all_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = json.load(f)
        except Exception:
            continue

        # Проверяем статус: если предмет персональный — пропускаем
        status = content.get("status", {})
        if isinstance(status, dict):
            state = status.get("state", "")
        else:
            state = str(status) if status else ""

        # PERSONAL_ON_USE и PERSONAL — не торгуются
        if state in ("PERSONAL_ON_USE", "PERSONAL"):
            skipped_personal += 1
            continue

        ids.append(path.stem)

    print(f"Отфильтровано персональных: {skipped_personal}")
    print(f"Торгуемых предметов: {len(ids)}")
    return ids

def fetch_lots_for_item(token, item_id):
    url = f"https://eapi.stalcraft.net/{REGION}/auction/{item_id}/lots?limit=200"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "lots" in data:
                return item_id, data["lots"]
            if isinstance(data, list):
                return item_id, data
        return item_id, []
    except Exception:
        return item_id, []

def load_history():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_history(history):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))

def aggregate_history(records):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    fresh, old = [], []
    for r in records:
        try:
            ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            fresh.append(r)
        else:
            old.append((ts, r))
    buckets = {}
    for ts, r in old:
        key = ts.strftime("%Y-%m-%dT%H")
        if key not in buckets:
            buckets[key] = {"min_price": r["price"]}
        else:
            buckets[key]["min_price"] = min(buckets[key]["min_price"], r["price"])
    aggregated = [
        {"timestamp": k + ":00:00+00:00", "price": v["min_price"], "aggregated": True}
        for k, v in sorted(buckets.items())
    ]
    result = aggregated + fresh
    return result[-720:] if len(result) > 720 else result

def main():
    print("Запуск сбора данных...")
    token = get_access_token()
    print("✅ Токен получен.")

    tradable_ids = discover_tradable_item_ids()
    if not tradable_ids:
        print("❌ Не найдено торгуемых предметов.")
        return

    # Берём только первые MAX_ITEMS торгуемых
    item_ids = tradable_ids[:MAX_ITEMS]
    print(f"Обрабатываем предметов: {len(item_ids)}")

    history = load_history()
    now = datetime.now(timezone.utc).isoformat()
    updated = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(fetch_lots_for_item, token, iid): iid for iid in item_ids}
        for n, fut in enumerate(as_completed(futures), 1):
            item_id, lots = fut.result()
            if lots:
                prices = [l.get("price") for l in lots if l.get("price")]
                if prices:
                    history.setdefault(item_id, []).append({
                        "timestamp": now,
                        "price": min(prices),
                        "all_prices": prices,
                    })
                    history[item_id] = aggregate_history(history[item_id])
                    updated += 1
            if n % 50 == 0:
                print(f"  обработано {n}/{len(item_ids)}")

    save_history(history)
    print(f"✅ Готово. Обновлено предметов: {updated}")

if __name__ == "__main__":
    main()
