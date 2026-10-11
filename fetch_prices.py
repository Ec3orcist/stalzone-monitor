import os
import json
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# --- Настройки ---
REGION = os.environ.get("REGION", "EU")
LANG = "ru"
ICON_REALM = "ru"
TOKEN_URL = "https://exbo.net/oauth/token"
DATA_FILE = "prices.json"
MAX_HISTORY = 500
MAX_ITEMS = 500
WORKERS = 8
MAX_LOTS_PER_ITEM = 20

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

# Глобально — множество существующих иконок (заполняется в main)
EXISTING_ICONS = None


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


def load_existing_icons():
    """Собирает список всех .png иконок, которые есть в локально склонированном репо."""
    icons_root = Path("itemdb") / ICON_REALM / "icons"
    if not icons_root.exists():
        print(f"⚠️ Папка {icons_root} не найдена — проверка иконок отключена.")
        return None

    icons = set()
    for png in icons_root.rglob("*.png"):
        rel = png.relative_to(icons_root).as_posix()
        icons.add(rel)

    print(f"🖼️ Найдено иконок в базе: {len(icons)}")
    return icons


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
    """Возвращает URL иконки ТОЛЬКО если файл реально существует в базе."""
    if not category:
        return ""
    if EXISTING_ICONS is None:
        return ""

    variants = [category, category.split("/")[0]]
    for cat in variants:
        rel = f"{cat}/{item_id}.png"
        if rel in EXISTING_ICONS:
            return (
                f"https://github.com/EXBO-Studio/stalzone-database/raw/main/"
                f"{ICON_REALM}/icons/{rel}"
            )
    return ""


def discover_tradable_items():
    items = []
    if ITEM_DB_DIR is None:
        print("❌ Папка с базой предметов не найдена.")
        return items

    print(f"📁 Используем базу: {ITEM_DB_DIR}")
    all_files = list(ITEM_DB_DIR.rglob("*.json"))
    print(f"Всего файлов в базе: {len(all_files)}")

    skipped = 0
    with_icon = 0
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
        icon = build_icon_url(item_id, category)
        if icon:
            with_icon += 1

        # Верхнеуровневая категория для группировки
        top_category = category.split("/")[0] if category else "other"

        items.append({
            "id": item_id,
            "name": get_localized_name(content),
            "icon": icon,
            "category": top_category
        })

    print(f"Отфильтровано персональных: {skipped}")
    print(f"Торгуемых предметов: {len(items)} (с иконкой: {with_icon})")
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
            raw_lots = data["lots"]
        elif isinstance(data, list):
            raw_lots = data
        else:
            raw_lots = []

        processed = []
        for lot in raw_lots:
            buyout = lot.get("buyoutPrice") or 0
            amount = lot.get("amount") or 0
            if buyout <= 0 or amount <= 0:
                continue
            processed.append({
                "price_per_unit": round(buyout / amount),
                "buyout": buyout,
                "amount": amount,
                "endTime": lot.get("endTime", ""),
            })

        processed.sort(key=lambda l: l["price_per_unit"])
        return item_id, processed
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

    # Сохраняем метаданные для агрегированных записей
    last_name = ""
    last_icon = ""
    last_category = "other"
    for r in records:
        if r.get("name"):
            last_name = r["name"]
        if r.get("icon"):
            last_icon = r["icon"]
        if r.get("category"):
            last_category = r["category"]

    buckets = {}
    for ts, r in old:
        key = ts.strftime("%Y-%m-%dT%H")
        if key not in buckets:
            buckets[key] = {"min_price": r["price"]}
        else:
            buckets[key]["min_price"] = min(buckets[key]["min_price"], r["price"])

    aggregated = [
        {
            "timestamp": k + ":00:00+00:00",
            "price": v["min_price"],
            "name": last_name,
            "icon": last_icon,
            "category": last_category,
            "aggregated": True
        }
        for k, v in sorted(buckets.items())
    ]

    result = aggregated + fresh
    return result[-720:] if len(result) > 720 else result


def main():
    global EXISTING_ICONS

    print(f"Запуск сбора данных (регион: {REGION}, язык: {LANG})...")

    # 1. Загружаем список существующих иконок (локально, без HTTP)
    EXISTING_ICONS = load_existing_icons()

    # 2. Токен
    token = get_access_token()
    print("✅ Токен получен.")

    # 3. Предметы
    all_items = discover_tradable_items()
    if not all_items:
        print("❌ Не найдено торгуемых предметов.")
        return

    items_to_process = all_items[:MAX_ITEMS]
    print(f"Обрабатываем предметов: {len(items_to_process)}")

    meta_map = {
        item["id"]: {
            "name": item["name"],
            "icon": item["icon"],
            "category": item.get("category", "other")
        }
        for item in items_to_process
    }

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
                best = lots[0]
                total_amount = sum(l["amount"] for l in lots)
                top_lots = lots[:MAX_LOTS_PER_ITEM]

                meta = meta_map.get(item_id, {})
                history.setdefault(item_id, []).append({
                    "timestamp": now,
                    "price": best["price_per_unit"],
                    "best_lot_amount": best["amount"],
                    "best_lot_buyout": best["buyout"],
                    "total_amount": total_amount,
                    "lots_count": len(lots),
                    "lots": top_lots,
                    "name": meta.get("name", item_id),
                    "icon": meta.get("icon", ""),
                    "category": meta.get("category", "other"),
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
