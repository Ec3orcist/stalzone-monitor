import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REGION = os.environ.get("REGION", "EU")
LANG = "ru"
ICON_REALM = "ru"

TOKEN_URL = "https://exbo.net/oauth/token"
API_BASE = "https://eapi.stalcraft.net"
DATA_FILE = Path("prices.json")
RECIPES_FILE = Path("recipes.json")
ACTIVE_ITEMS_FILE = Path("active_items.json")
SALES_FILE = Path("sales_history.json")
CRAFTS_FILE = Path("crafts.json")

SALES_REFRESH_HOURS = 6
SALES_MAX_DAYS = 60
SALES_MAX_PAGES = 8
SALES_PAGE_SIZE = 200
MAX_HISTORY = 168
MAX_ITEMS = 3000
RESCAN_BATCH = 80
WORKERS = 4
MAX_LOTS_PER_ITEM = 10
MIN_REQUEST_INTERVAL = 1.05
REQUEST_TIMEOUT = 15
RETRIES = 3

PERSONAL_STATES = frozenset({"PERSONAL_ON_USE", "PERSONAL"})


def utc_now():
    return datetime.now(timezone.utc)


def load_json(path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data, compact=True):
    kwargs = {"ensure_ascii": False}
    kwargs.update({"separators": (",", ":")} if compact else {"indent": 2})
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, **kwargs)


def parse_item_state(content):
    status = content.get("status", {})
    return status.get("state", "") if isinstance(status, dict) else str(status)


def localized_name(content):
    name = content.get("name", {})
    if isinstance(name, dict):
        lines = name.get("lines", {})
        return lines.get(LANG) or lines.get("en") or lines.get("ru") or ""
    return str(name) if name else ""


class RateLimiter:
    def __init__(self, interval):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


class StalcraftApi:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        self.limiter = RateLimiter(MIN_REQUEST_INTERVAL)

    def get(self, url):
        last_error = None
        for attempt in range(RETRIES):
            self.limiter.wait()
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 2 + attempt))
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return None
            try:
                return resp.json()
            except ValueError:
                return None
        if last_error:
            print(f"  сеть: {last_error}")
        return None


def get_access_token(client_id, client_secret):
    if not client_id or not client_secret:
        raise SystemExit("Нет CLIENT_ID / CLIENT_SECRET")
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "",
        },
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise SystemExit("OAuth не вернул access_token")
    return token


def find_item_db_dir():
    base = Path("itemdb")
    if not base.exists():
        return None
    for candidate in (LANG, REGION.lower(), "en", "ru"):
        path = base / candidate / "items"
        if path.exists():
            return path
    for directory in base.iterdir():
        items = directory / "items"
        if directory.is_dir() and items.exists():
            return items
    return None


def load_existing_icons():
    icons_root = Path("itemdb") / ICON_REALM / "icons"
    if not icons_root.exists():
        return frozenset()
    return frozenset(
        png.relative_to(icons_root).as_posix()
        for png in icons_root.rglob("*.png")
    )


def icon_url(item_id, category, existing_icons):
    if not category or not existing_icons:
        return ""
    for cat in (category, category.split("/")[0]):
        rel = f"{cat}/{item_id}.png"
        if rel in existing_icons:
            return (
                "https://github.com/EXBO-Studio/stalzone-database/raw/main/"
                f"{ICON_REALM}/icons/{rel}"
            )
    return ""


def scan_item_database(item_db_dir, existing_icons):
    tradable = []
    meta = {}
    skipped = 0
    files = list(item_db_dir.rglob("*.json"))
    print(f"📁 База: {item_db_dir} ({len(files)} файлов)")

    for path in files:
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        item_id = path.stem
        category = content.get("category", "") or ""
        top_category = category.split("/")[0] if category else "other"
        info = {
            "id": item_id,
            "name": localized_name(content),
            "icon": icon_url(item_id, category, existing_icons),
            "category": top_category,
        }
        meta[item_id] = info
        if parse_item_state(content) in PERSONAL_STATES:
            skipped += 1
            continue
        tradable.append(info)

    print(f"📚 Мета: {len(meta)}, торгуемых: {len(tradable)}, персональных: {skipped}")
    return tradable, meta


def pick_items(tradable, active_ids):
    if not active_ids:
        chosen = tradable[:MAX_ITEMS]
        print(f"🐌 Первый запуск: {len(chosen)}")
        return chosen

    known = {item["id"] for item in tradable}
    live_active = active_ids & known
    by_id = {item["id"]: item for item in tradable}
    primary = [by_id[item_id] for item_id in live_active]
    unknown = [item for item in tradable if item["id"] not in live_active][:RESCAN_BATCH]
    print(f"⚡ Кэш активных: {len(primary)} + рескан {len(unknown)}")
    return primary + unknown


def fetch_lots(api, item_id):
    data = api.get(f"{API_BASE}/{REGION}/auction/{item_id}/lots?limit=200")
    if not data:
        return item_id, []
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
    processed.sort(key=lambda lot: lot["price_per_unit"])
    return item_id, processed


def parse_sales(raw_entries):
    days = {}
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        stamp = entry.get("time") or entry.get("timestamp") or ""
        price = entry.get("price") or entry.get("buyoutPrice")
        amount = entry.get("amount") or 1
        if not stamp or not price:
            continue
        try:
            day = stamp[:10]
            price = int(price)
            amount = int(amount)
        except (TypeError, ValueError):
            continue
        bucket = days.setdefault(day, {"prices": [], "total_amount": 0, "total_sum": 0})
        bucket["prices"].append(price)
        bucket["total_amount"] += amount
        bucket["total_sum"] += price * amount

    result = [
        {
            "t": f"{day}T00:00:00Z",
            "a": bucket["total_amount"],
            "min": min(bucket["prices"]),
            "max": max(bucket["prices"]),
            "avg": round(bucket["total_sum"] / bucket["total_amount"]) if bucket["total_amount"] else 0,
        }
        for day, bucket in sorted(days.items())
    ]
    return result[-SALES_MAX_DAYS:]


def fetch_sales(api, item_id):
    base = f"{API_BASE}/{REGION}/auction/{item_id}/history"
    collected = []
    total_available = None
    for page in range(SALES_MAX_PAGES):
        offset = page * SALES_PAGE_SIZE
        data = api.get(f"{base}?limit={SALES_PAGE_SIZE}&offset={offset}")
        if not isinstance(data, dict):
            break
        prices = data.get("prices") or []
        if total_available is None:
            total_available = data.get("total", 0)
        if not prices:
            break
        collected.extend(prices)
        if total_available and len(collected) >= total_available:
            break
        if len(prices) < SALES_PAGE_SIZE:
            break
    return item_id, parse_sales(collected)


def sales_needs_refresh(sales):
    ts_str = sales.get("_updated_at", "")
    if not ts_str:
        return True
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (utc_now() - ts).total_seconds() >= SALES_REFRESH_HOURS * 3600


def aggregate_history(records):
    now = utc_now()
    cutoff = now - timedelta(hours=24)
    fresh, old = [], []
    last_name = last_icon = ""
    last_category = "other"
    for record in records:
        if record.get("name"):
            last_name = record["name"]
        if record.get("icon"):
            last_icon = record["icon"]
        if record.get("category"):
            last_category = record["category"]
        try:
            ts = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if ts >= cutoff:
            fresh.append(record)
        else:
            old.append((ts, record))

    buckets = {}
    for ts, record in old:
        key = ts.strftime("%Y-%m-%dT%H")
        prev = buckets.get(key)
        if prev is None or record["price"] < prev["price"]:
            buckets[key] = record

    aggregated = [
        {
            "timestamp": f"{key}:00:00+00:00",
            "price": record["price"],
            "name": last_name,
            "icon": last_icon,
            "category": last_category,
            "aggregated": True,
        }
        for key, record in sorted(buckets.items())
    ]
    result = aggregated + fresh
    return result[-MAX_HISTORY:]


def load_recipes():
    data = load_json(RECIPES_FILE, {})
    if isinstance(data, dict) and "recipes" in data:
        source = data["recipes"]
    elif isinstance(data, list):
        source = data
    elif isinstance(data, dict):
        source = [
            {"itemId": item_id, **recipe} if isinstance(recipe, dict) else {}
            for item_id, recipe in data.items()
            if item_id not in {"success", "meta", "totalRecipes", "filteredCount"}
        ]
    else:
        source = []

    recipes = {}
    for recipe in source:
        if not isinstance(recipe, dict):
            continue
        item_id = recipe.get("itemId") or recipe.get("id")
        if not item_id:
            continue
        resources = recipe.get("resources") or recipe.get("ingredients") or []
        recipes[item_id] = {
            "name": recipe.get("name", item_id),
            "energy": recipe.get("energy", 0),
            "craftAmount": recipe.get("craftAmount", 1) or 1,
            "station": recipe.get("station", ""),
            "bench": recipe.get("bench", ""),
            "skill": recipe.get("skill", ""),
            "skillId": recipe.get("skillId", ""),
            "skillLevel": recipe.get("skillLevel", 0),
            "experience": recipe.get("experience", 0),
            "experienceSkill": recipe.get("experienceSkill", ""),
            "category": recipe.get("category", ""),
            "subcategory": recipe.get("subcategory", ""),
            "icon": recipe.get("icon", ""),
            "requiredStations": recipe.get("requiredStations", []),
            "stations": recipe.get("stations", []),
            "ingredients": [
                {"id": res.get("itemId") or res.get("id"), "amount": res["amount"]}
                for res in resources
                if isinstance(res, dict) and (res.get("itemId") or res.get("id")) and res.get("amount")
            ],
        }
    print(f"✅ Рецептов: {len(recipes)}")
    return recipes


def build_craft_tree(recipes, prices, item_id, visited=None):
    visited = set() if visited is None else visited
    if item_id in visited:
        return None
    visited = visited | {item_id}

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
        "cost": 0,
        "profit": 0,
        "roi": 0,
    }
    if not recipe:
        node["cost"] = sell_price
        return node

    node.update({
        "energy": recipe.get("energy", 0),
        "craftAmount": recipe.get("craftAmount", 1),
        "station": recipe.get("station", ""),
        "skill": recipe.get("skill", ""),
        "skillLevel": recipe.get("skillLevel", 0),
        "experience": recipe.get("experience", 0),
        "experienceSkill": recipe.get("experienceSkill", ""),
        "stations": recipe.get("stations", []),
        "category": recipe.get("category", ""),
        "subcategory": recipe.get("subcategory", ""),
    })
    if not node["icon"]:
        node["icon"] = recipe.get("icon", "")

    total_cost = 0
    direct_cost = 0
    for ing in recipe.get("ingredients", []):
        sub = build_craft_tree(recipes, prices, ing["id"], visited)
        if sub is None:
            continue
        sub["amount"] = ing["amount"]
        sub["total_cost"] = sub["cost"] * ing["amount"]
        node["ingredients"].append(sub)
        total_cost += sub["total_cost"]
        direct_cost += prices.get(ing["id"], {}).get("price", 0) * ing["amount"]

    craft_amount = recipe.get("craftAmount", 1) or 1
    node["total_cost_batch"] = total_cost
    node["cost"] = total_cost / craft_amount
    node["profit"] = sell_price - node["cost"]
    node["roi"] = (node["profit"] / node["cost"] * 100) if node["cost"] > 0 else 0
    node["total_direct_cost_batch"] = direct_cost
    node["cost_direct"] = direct_cost / craft_amount
    node["profit_direct"] = sell_price - node["cost_direct"]
    node["roi_direct"] = (node["profit_direct"] / node["cost_direct"] * 100) if node["cost_direct"] > 0 else 0
    return node


def run_pool(ids, worker, label):
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(worker, item_id): item_id for item_id in ids}
        for n, fut in enumerate(as_completed(futures), 1):
            results.append(fut.result())
            if n % 100 == 0:
                print(f"  {label}: {n}/{len(ids)}")
    return results


def update_prices(api, items, history):
    now = utc_now().isoformat()
    meta_map = {item["id"]: item for item in items}
    updated = 0
    for item_id, lots in run_pool(
        [item["id"] for item in items],
        lambda item_id: fetch_lots(api, item_id),
        "цены",
    ):
        if not lots:
            continue
        best = lots[0]
        meta = meta_map.get(item_id, {})
        history.setdefault(item_id, []).append({
            "timestamp": now,
            "price": best["price_per_unit"],
            "best_lot_amount": best["amount"],
            "best_lot_buyout": best["buyout"],
            "total_amount": sum(lot["amount"] for lot in lots),
            "lots_count": len(lots),
            "lots": lots[:MAX_LOTS_PER_ITEM],
            "name": meta.get("name", item_id),
            "icon": meta.get("icon", ""),
            "category": meta.get("category", "other"),
        })
        history[item_id] = aggregate_history(history[item_id])
        updated += 1
    save_json(DATA_FILE, history)
    print(f"✅ Цены: {updated} предметов")
    return history


def update_sales(api, item_ids, previous):
    if not sales_needs_refresh(previous):
        print(f"💤 История продаж свежая (< {SALES_REFRESH_HOURS} ч)")
        return previous

    print(f"📊 История продаж: {len(item_ids)} предметов, до {SALES_MAX_PAGES} стр.")
    sales = {
        key: value
        for key, value in previous.items()
        if key != "_updated_at"
    }
    got = empty = 0
    for item_id, entries in run_pool(
        item_ids,
        lambda item_id: fetch_sales(api, item_id),
        "продажи",
    ):
        if entries:
            sales[item_id] = entries
            got += 1
        else:
            empty += 1
    sales["_updated_at"] = utc_now().isoformat()
    save_json(SALES_FILE, sales)
    size_kb = SALES_FILE.stat().st_size / 1024
    print(f"✅ Продажи: {got} с данными, {empty} пусто ({size_kb:.0f} КБ)")
    return sales


def update_crafts(recipes, history, all_meta):
    if not recipes:
        return
    current_prices = {}
    for item_id in set(history) | set(all_meta):
        records = history.get(item_id, [])
        last = records[-1] if records else {}
        meta = all_meta.get(item_id, {})
        current_prices[item_id] = {
            "name": last.get("name") or meta.get("name") or item_id,
            "icon": last.get("icon") or meta.get("icon") or "",
            "price": last.get("price", 0),
        }
    trees = {
        item_id: tree
        for item_id in recipes
        if (tree := build_craft_tree(recipes, current_prices, item_id))
    }
    save_json(CRAFTS_FILE, trees)
    print(f"✅ Крафты: {len(trees)}")


def main():
    print(f"Запуск (регион: {REGION})")
    item_db_dir = find_item_db_dir()
    if item_db_dir is None:
        raise SystemExit("Нет itemdb — сначала клонируй EXBO-Studio/stalzone-database")

    existing_icons = load_existing_icons()
    print(f"🖼️ Иконок: {len(existing_icons)}")
    tradable, all_meta = scan_item_database(item_db_dir, existing_icons)
    if not tradable:
        raise SystemExit("В базе нет торгуемых предметов")

    token = get_access_token(os.environ.get("CLIENT_ID"), os.environ.get("CLIENT_SECRET"))
    print("✅ Токен получен")
    api = StalcraftApi(token)

    cached_active = load_json(ACTIVE_ITEMS_FILE, None)
    active_ids = set(cached_active) if isinstance(cached_active, list) else None
    items = pick_items(tradable, active_ids)

    history = update_prices(api, items, load_json(DATA_FILE, {}))
    active_now = {item_id for item_id, records in history.items() if records}
    save_json(ACTIVE_ITEMS_FILE, sorted(active_now), compact=False)
    print(f"💾 Активных: {len(active_now)}")

    update_sales(api, [item["id"] for item in items], load_json(SALES_FILE, {}))
    update_crafts(load_recipes(), history, all_meta)


if __name__ == "__main__":
    main()
