import os
import json
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REGION = os.environ.get("REGION", "EU")
LANG = "ru"
ICON_REALM = "ru"
TOKEN_URL = "https://exbo.net/oauth/token"
DATA_FILE = "prices.json"
RECIPES_FILE = "recipes.json"
ACTIVE_ITEMS_FILE = "active_items.json"
HISTORY_FILE = "sales_history.json"
HISTORY_MAX_AGE_HOURS = 6
MAX_HISTORY = 168
MAX_ITEMS = 3000
WORKERS = 10
MAX_LOTS_PER_ITEM = 10

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
EXISTING_ICONS = None
SESSION = None


def get_session(token):
    global SESSION
    if SESSION is None:
        SESSION = requests.Session()
        SESSION.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        })
    return SESSION


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
    icons_root = Path("itemdb") / ICON_REALM / "icons"
    if not icons_root.exists():
        return None
    icons = set()
    for png in icons_root.rglob("*.png"):
        icons.add(png.relative_to(icons_root).as_posix())
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
    if not category or EXISTING_ICONS is None:
        return ""
    for cat in [category, category.split("/")[0]]:
        rel = f"{cat}/{item_id}.png"
        if rel in EXISTING_ICONS:
            return (f"https://github.com/EXBO-Studio/stalzone-database/raw/main/"
                    f"{ICON_REALM}/icons/{rel}")
    return ""


def build_all_items_meta():
    meta = {}
    if ITEM_DB_DIR is None:
        return meta
    for dirpath, _, filenames in os.walk(ITEM_DB_DIR):
        for fname in filenames:
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = json.load(f)
            except Exception:
                continue
            item_id = fname[:-5]
            category = content.get("category", "")
            meta[item_id] = {
                "name": get_localized_name(content),
                "icon": build_icon_url(item_id, category),
                "category": category.split("/")[0] if category else "other"
            }
    print(f"📚 Метаданные обо всех предметах: {len(meta)}")
    return meta


def discover_tradable_items():
    items = []
    if ITEM_DB_DIR is None:
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
        items.append({
            "id": item_id,
            "name": get_localized_name(content),
            "icon": icon,
            "category": category.split("/")[0] if category else "other"
        })
    print(f"Отфильтровано персональных: {skipped}")
    print(f"Торгуемых предметов: {len(items)} (с иконкой: {with_icon})")
    return items


def fetch_lots_for_item(token, item_id):
    session = get_session(token)
    url = f"https://eapi.stalcraft.net/{REGION}/auction/{item_id}/lots?limit=200"
    try:
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            return item_id, []
        data = r.json()
        raw_lots = data.get("lots", []) if isinstance(data, dict) else data
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


def fetch_history_for_item(token, item_id):
    """Запрашивает историю продаж предмета за последние ~30 дней."""
    session = get_session(token)
    url = f"https://eapi.stalcraft.net/{REGION}/auction/{item_id}/history"
    try:
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            return item_id, []
        data = r.json()
        # API может возвращать list или dict
        if isinstance(data, list):
            raw = data
        elif isinstance(data, dict):
            raw = data.get("history") or data.get("prices") or data.get("sales") or []
        else:
            raw = []

        processed = []
        for entry in raw:
            time_str = entry.get("time") or entry.get("timestamp") or ""
            prices = entry.get("prices") or []
            amount = entry.get("amount")
            if amount is None:
                amount = len(prices) if prices else 1
            if not prices:
                # Попробуем альтернативные поля
                single_price = entry.get("price") or entry.get("buyoutPrice")
                if single_price:
                    prices = [single_price]
            if not prices:
                continue
            processed.append({
                "time": time_str,
                "amount": int(amount),
                "min_price": min(prices),
                "max_price": max(prices),
                "avg_price": round(sum(prices) / len(prices)),
            })
        processed.sort(key=lambda x: x["time"])
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


def load_sales_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_sales_history(data):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def sales_history_is_fresh():
    """Проверяет, обновлялась ли история меньше N часов назад."""
    if not os.path.exists(HISTORY_FILE):
        return False
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        updated_at = data.get("_updated_at")
        if not updated_at:
            return False
        ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - ts
        return age.total_seconds() < HISTORY_MAX_AGE_HOURS * 3600
    except Exception:
        return False


def load_active_items():
    if os.path.exists(ACTIVE_ITEMS_FILE):
        try:
            with open(ACTIVE_ITEMS_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return None
    return None


def save_active_items(active_ids):
    with open(ACTIVE_ITEMS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(active_ids), f, ensure_ascii=False, indent=2)


def load_recipes():
    if not os.path.exists(RECIPES_FILE):
        return {}
    with open(RECIPES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    recipes = {}
    if isinstance(data, dict) and "recipes" in data:
        source_list = data["recipes"]
        print(f"📖 Найдено рецептов: {len(source_list)}")
    elif isinstance(data, list):
        source_list = data
    else:
        source_list = []
    for r in source_list:
        item_id = r.get("itemId")
        if not item_id:
            continue
        recipes[item_id] = {
            "name": r.get("name", item_id),
            "energy": r.get("energy", 0),
            "craftAmount": r.get("craftAmount", 1),
            "station": r.get("station", ""),
            "bench": r.get("bench", ""),
            "skill": r.get("skill", ""),
            "skillId": r.get("skillId", ""),
            "skillLevel": r.get("skillLevel", 0),
            "experience": r.get("experience", 0),
            "experienceSkill": r.get("experienceSkill", ""),
            "category": r.get("category", ""),
            "subcategory": r.get("subcategory", ""),
            "icon": r.get("icon", ""),
            "requiredStations": r.get("requiredStations", []),
            "stations": r.get("stations", []),
            "ingredients": [
                {"id": res["itemId"], "amount": res["amount"]}
                for res in r.get("resources", [])
                if res.get("itemId") and res.get("amount")
            ]
        }
    print(f"✅ Загружено рецептов: {len(recipes)}")
    return recipes


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
    last_name = last_icon = ""
    last_category = "other"
    for r in records:
        if r.get("name"): last_name = r["name"]
        if r.get("icon"): last_icon = r["icon"]
        if r.get("category"): last_category = r["category"]
    buckets = {}
    for ts, r in old:
        key = ts.strftime("%Y-%m-%dT%H")
        if key not in buckets:
            buckets[key] = {"min_price": r["price"]}
        else:
            buckets[key]["min_price"] = min(buckets[key]["min_price"], r["price"])
    aggregated = [
        {"timestamp": k + ":00:00+00:00", "price": v["min_price"],
         "name": last_name, "icon": last_icon, "category": last_category,
         "aggregated": True}
        for k, v in sorted(buckets.items())
    ]
    result = aggregated + fresh
    return result[-MAX_HISTORY:] if len(result) > MAX_HISTORY else result


def build_craft_tree(recipes, prices, item_id, visited=None):
    if visited is None:
        visited = set()
    if item_id in visited:
        return None
    visited.add(item_id)

    price_info = prices.get(item_id, {})
    sell_price = price_info.get("price", 0)
    recipe = recipes.get(item_id)

    node = {
        "id": item_id,
        "name": price_info.get("name") or (recipe or {}).get("name", item_id),
        "icon": price_info.get("icon") or "",
        "sell_price": sell_price,
        "craftable": bool(recipe and recipe.get("ingredients")),
        "ingredients": [],
        "cost": 0, "profit": 0, "roi": 0,
    }

    if not recipe:
        node["cost"] = sell_price
        return node

    node["energy"] = recipe.get("energy", 0)
    node["craftAmount"] = recipe.get("craftAmount", 1)
    node["station"] = recipe.get("station", "")
    node["skill"] = recipe.get("skill", "")
    node["skillLevel"] = recipe.get("skillLevel", 0)
    node["experience"] = recipe.get("experience", 0)
    node["experienceSkill"] = recipe.get("experienceSkill", "")
    node["stations"] = recipe.get("stations", [])
    node["category"] = recipe.get("category", "")
    node["subcategory"] = recipe.get("subcategory", "")

    total_cost = 0
    for ing in recipe.get("ingredients", []):
        sub = build_craft_tree(recipes, prices, ing["id"], visited.copy())
        if sub is None:
            continue
        sub["amount"] = ing["amount"]
        sub["total_cost"] = sub["cost"] * ing["amount"]
        node["ingredients"].append(sub)
        total_cost += sub["total_cost"]

    craft_amount = recipe.get("craftAmount", 1) or 1
    node["total_cost_batch"] = total_cost
    node["cost"] = total_cost / craft_amount
    node["profit"] = sell_price - node["cost"]
    node["roi"] = (node["profit"] / node["cost"] * 100) if node["cost"] > 0 else 0
    return node


def main():
    global EXISTING_ICONS
    print(f"Запуск сбора данных (регион: {REGION})...")
    EXISTING_ICONS = load_existing_icons()
    token = get_access_token()
    print("✅ Токен получен.")
    all_items = discover_tradable_items()
    if not all_items:
        return

    all_meta = build_all_items_meta()

    active_ids = load_active_items()
    if active_ids is not None:
        all_ids = {item["id"] for item in all_items}
        active_ids = active_ids & all_ids
        items_to_process = [item for item in all_items if item["id"] in active_ids]
        print(f"⚡ Кэш активных: {len(items_to_process)} (из {len(all_items)})")
    else:
        items_to_process = all_items[:MAX_ITEMS]
        print(f"🐌 Первый запуск: {len(items_to_process)}")

    meta_map = {item["id"]: item for item in items_to_process}

    history = load_history()
    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(fetch_lots_for_item, token, item["id"]): item["id"]
                   for item in items_to_process}
        for n, fut in enumerate(as_completed(futures), 1):
            item_id, lots = fut.result()
            if lots:
                best = lots[0]
                total_amount = sum(l["amount"] for l in lots)
                meta = meta_map.get(item_id, {})
                history.setdefault(item_id, []).append({
                    "timestamp": now,
                    "price": best["price_per_unit"],
                    "best_lot_amount": best["amount"],
                    "best_lot_buyout": best["buyout"],
                    "total_amount": total_amount,
                    "lots_count": len(lots),
                    "lots": lots[:MAX_LOTS_PER_ITEM],
                    "name": meta.get("name", item_id),
                    "icon": meta.get("icon", ""),
                    "category": meta.get("category", "other"),
                })
                history[item_id] = aggregate_history(history[item_id])
                updated += 1
            if n % 200 == 0:
                print(f"  обработано {n}/{len(items_to_process)}")
    save_history(history)
    print(f"✅ Цены обновлены. Предметов: {updated}")

    # --- ИСТОРИЯ ПРОДАЖ ---
    sales_history = load_sales_history()
    if sales_history_is_fresh():
        print(f"💤 История продаж свежая (обновлялась < {HISTORY_MAX_AGE_HOURS} ч назад), пропускаем.")
    else:
        print(f"📊 Загружаем историю продаж для {len(items_to_process)} предметов...")
        sales_updated = 0
        new_sales = {"_updated_at": datetime.now(timezone.utc).isoformat()}
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {executor.submit(fetch_history_for_item, token, item["id"]): item["id"]
                       for item in items_to_process}
            for n, fut in enumerate(as_completed(futures), 1):
                item_id, entries = fut.result()
                if entries:
                    new_sales[item_id] = entries
                    sales_updated += 1
                if n % 200 == 0:
                    print(f"  история: {n}/{len(items_to_process)}")
        save_sales_history(new_sales)
        print(f"✅ История продаж сохранена для {sales_updated} предметов")

    active_now = {iid for iid, rec in history.items() if rec}
    save_active_items(active_now)
    print(f"💾 Активных предметов: {len(active_now)}")

    recipes = load_recipes()
    if recipes:
        current_prices = {}
        all_ids_for_prices = set(history.keys()) | set(all_meta.keys())
        for iid in all_ids_for_prices:
            records = history.get(iid, [])
            last_rec = records[-1] if records else {}
            meta = all_meta.get(iid, {})
            current_prices[iid] = {
                "name": last_rec.get("name") or meta.get("name") or iid,
                "icon": last_rec.get("icon") or meta.get("icon") or "",
                "price": last_rec.get("price", 0),
            }
        craft_trees = {}
        for iid in recipes:
            tree = build_craft_tree(recipes, current_prices, iid)
            if tree:
                craft_trees[iid] = tree
        with open("crafts.json", "w", encoding="utf-8") as f:
            json.dump(craft_trees, f, ensure_ascii=False, separators=(",", ":"))
        print(f"✅ crafts.json сохранён ({len(craft_trees)} рецептов)")


if __name__ == "__main__":
    main()
