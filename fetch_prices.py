import os
import json
import requests
from datetime import datetime, timezone, timedelta

# --- Настройки ---
REGION = os.environ.get("REGION", "RU")
TOKEN_URL = "https://exbo.net/oauth/token"
DATA_FILE = "prices.json"
ITEMS_DB_URL = "https://api.github.com/repos/EXBO-Studio/stalzone-database/contents/ru/items"
MAX_HISTORY = 500

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

def get_access_token():
    """Получает токен доступа."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError("CLIENT_ID и CLIENT_SECRET должны быть заданы.")
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": ""
    }
    resp = requests.post(TOKEN_URL, data=payload, timeout=10)
    resp.raise_for_status()
    return resp.json().get("access_token")

def get_item_ids():
    """Получает список ID всех предметов из официальной базы данных."""
    print("Загрузка списка предметов из официальной базы данных...")
    try:
        resp = requests.get(ITEMS_DB_URL, timeout=15)
        resp.raise_for_status()
        # API GitHub возвращает список файлов и папок в директории
        # Нам нужны только имена папок (категории предметов)
        categories = [item["name"] for item in resp.json() if item["type"] == "dir"]
        print(f"Найдено категорий: {len(categories)}")
        
        item_ids = []
        for category in categories:
            cat_url = f"{ITEMS_DB_URL}/{category}"
            cat_resp = requests.get(cat_url, timeout=10)
            if cat_resp.status_code == 200:
                # Внутри категории могут быть подкатегории или сразу файлы .json
                for item in cat_resp.json():
                    if item["type"] == "dir":
                        # Если подкатегория, заходим внутрь
                        subcat_url = f"{cat_url}/{item['name']}"
                        subcat_resp = requests.get(subcat_url, timeout=10)
                        if subcat_resp.status_code == 200:
                            for subitem in subcat_resp.json():
                                if subitem["name"].endswith(".json"):
                                    # Имя файла = ID предмета
                                    item_ids.append(subitem["name"].replace(".json", ""))
                    elif item["name"].endswith(".json"):
                        item_ids.append(item["name"].replace(".json", ""))
        print(f"Всего найдено предметов: {len(item_ids)}")
        return item_ids
    except Exception as e:
        print(f"Ошибка при загрузке списка предметов: {e}")
        return []

def fetch_lots_for_item(token, item_id):
    """Запрашивает лоты для конкретного предмета."""
    url = f"https://eapi.stalcraft.net/{REGION}/auction/{item_id}/lots?limit=200"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            # API возвращает объект с ключом 'lots' или список
            if isinstance(data, dict) and "lots" in data:
                return data["lots"]
            elif isinstance(data, list):
                return data
        elif resp.status_code == 404:
            # Предмет не найден на аукционе - это нормально, пропускаем
            pass
        else:
            print(f"  Ошибка для {item_id}: {resp.status_code}")
    except Exception as e:
        print(f"  Исключение для {item_id}: {e}")
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
    """Агрегирует старые записи по часам."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
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
    if len(result) > 720:
        result = result[-720:]
    return result

def main():
    print("Запуск сбора данных...")
    token = get_access_token()
    print("Токен получен.")
    
    item_ids = get_item_ids()
    if not item_ids:
        print("Не удалось получить список предметов. Выход.")
        return
    
    history = load_history()
    now = datetime.now(timezone.utc).isoformat()
    updated_count = 0
    
    # Ограничиваем количество предметов для теста (можно убрать позже)
    # item_ids = item_ids[:50]  # Раскомментируйте для быстрого теста
    
    for i, item_id in enumerate(item_ids):
        if i % 50 == 0:
            print(f"Обработано {i}/{len(item_ids)} предметов...")
        
        lots = fetch_lots_for_item(token, item_id)
        if not lots:
            continue
        
        prices = [lot.get("price") for lot in lots if lot.get("price")]
        if not prices:
            continue
        
        record = {
            "timestamp": now,
            "price": min(prices),
            "all_prices": prices
        }
        history.setdefault(item_id, []).append(record)
        history[item_id] = aggregate_history(history[item_id])
        updated_count += 1
    
    save_history(history)
    print(f"Обновлена история для {updated_count} предметов.")

if __name__ == "__main__":
    main()
