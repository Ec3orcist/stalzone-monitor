import os
import json
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# --- Настройки ---
REGION = os.environ.get("REGION", "EU")
LANG = "en"           # "en" для EU, "ru" для RU
ICON_REALM = "ru"     # Иконки лежат в папке ru даже для EU-региона
TOKEN_URL = "https://exbo.net/oauth/token"
DATA_FILE = "prices.json"
MAX_HISTORY = 500
MAX_ITEMS = 500
WORKERS = 8

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

# Путь к базе предметов (ищем папку ru, т.к. ID одинаковые для всех регионов)
def find_item_db_dir():
    base = Path("itemdb")
    if not base.exists():
        return None
    for candidate in [LANG, REGION.lower(), "en", "ru"]:
        path = base / candidate / "items"
        if path.exists():
            return path
    for d in base.iterdir():
        if d.is_dir() and (d / "items").exists():
            return d / "items"
    return None

ITEM_DB_DIR = find_item_db_dir()

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

def get_localized_name(content):
    try:
        name = content.get("name", {})
        if isinstance(name, dict):
            lines = name.get("lines", {})
            return lines.get(LANG) or lines.get("en") or lines.get("ru") or ""
        return str(name)
    except Exception:
        return ""

def build_icon_url(item_id, category):
    """Строит URL иконки по официальной схеме."""
    if not category:
        return ""
    # category вида "weapon/pistol" или "misc"
    return f"https://github.com/EXBO-Studio/stalzone-database/raw/main/{ICON_REALM}/icons/{category}/{item_id}.png"

def discover_tradable_items():
    items = []
    if ITEM_DB_DIR is None:
        print("❌ Папка с базой предметов не найдена.")
        return items

    print(f"📁 Используем базу: {ITEM_DB_DIR}")
    all_files = list(ITEM_DB_DIR.rglob("*.json"))
    print(f"Всего файлов в базе: {len(all_files)}")

    skipped = 0
    for path in all_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = json.load(f)
        except Exception:
            continue

        status = content.get("status", {})
        state = status.get("state", "") if isinstance(status, dict) else str(status)
        if state in ("PERSONAL_ON_USE", "PERSONAL"):
            skipped += 1
            continue

        item_id = path.stem
        category = content.get("category", "")
        items.append({
            "id": item_id,
            "name": get_localized_name(content),
            "icon": build_icon_url(item_id, category)
        })

    print(f"Отфильтровано персональных: {skipped}")
    print(f"Торгуемых предметов: {len(items)}")
    return items

def fetch_lots_for_item(token, item_id):
    url = f"https://eapi.stalcraft.net/{REGION}/auction/{item_id}/lots?limit=200"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return item_id, []
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
    print(f"Запуск сбора данных (регион: {REGION}, язык: {LANG})...")
    token = get_access_token()
    print("✅ Токен получен.")

    all_items = discover_tradable_items()
    if not all_items:
        print("❌ Не найдено торгуемых предметов.")
        return

    items_to_process = all_items[:MAX_ITEMS]
    print(f"Обрабатываем предметов: {len(items_to_process)}")

    # Сохраняем мета-данные (имя, иконку) для каждого предмета
    meta_map = {item["id"]: {"name": item["name"], "icon": item["icon"]} for item in items_to_process}

    history = load_history()
    now = datetime.now(timezone.utc).isoformat()
    updated = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(fetch_lots_for_item, token, item["id"]): item["id"]
            for item in items_to_process
        }
        for n, fut in enumerate(as_completed(futures), 1):
            item_id, lots = fut.result()
            if lots:
                prices = []
                for lot in lots:
                    p = lot.get("buyoutPrice") or lot.get("startPrice") or 0
                    if p > 0:
                        prices.append(p)
                if prices:
                    meta = meta_map.get(item_id, {})
                    history.setdefault(item_id, []).append({
                        "timestamp": now,
                        "price": min(prices),
                        "name": meta.get("name", item_id),
                        "icon": meta.get("icon", ""),
                        "all_prices": prices,
                    })
                    history[item_id] = aggregate_history(history[item_id])
                    updated += 1
            if n % 50 == 0:
                print(f"  обработано {n}/{len(items_to_process)}")

    save_history(history)
    print(f"✅ Готово. Обновлено предметов: {updated}")
    print(f"   Файл {DATA_FILE}: {os.path.getsize(DATA_FILE)} байт")

if __name__ == "__main__":
    main()
