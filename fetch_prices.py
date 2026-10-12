import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REGION = os.environ.get("REGION", "EU")
LANG = "ru"
ICON_REALM = "ru"
ICON_CDN = "https://raw.githubusercontent.com/EXBO-Studio/stalzone-database/main"

TOKEN_URL = "https://exbo.net/oauth/token"
API_BASE = "https://eapi.stalcraft.net"
DATA_FILE = Path("prices.json")
RECIPES_FILE = Path("recipes.json")
ACTIVE_ITEMS_FILE = Path("active_items.json")
SALES_FILE = Path("sales_history.json")
CLOCK_FILE = Path("sale_clock.json")
CRAFTS_FILE = Path("crafts.json")
ITEMS_FILE = Path("items.json")

SALES_MAX_DAYS = 60
SALES_MAX_PAGES = 8
SALES_PAGE_SIZE = 200
MAX_HISTORY = 168
MAX_ITEMS = 3000
PRICE_BUDGET = 460
DISCOVERY_SLOTS = 140
SALES_SLICE_ITEMS = 16
SALES_SLICE_PAGES = 2
WORKERS = 4
MAX_LOTS_PER_ITEM = 10
ALWAYS_PRICE_CATS = {"weapon", "armor", "armour", "weapon_modules"}
RANK_CODES = {
    "RANK_NEWBIE": "newbie",
    "RANK_STALKER": "stalker",
    "RANK_VETERAN": "veteran",
    "RANK_MASTER": "master",
    "RANK_LEGEND": "legend",
}
MIN_REQUEST_INTERVAL = 1.05
REQUEST_TIMEOUT = 15
RETRIES = 3

MSK = timezone(timedelta(hours=3))
CLOCK_SLOTS = 8

UNTRADABLE_STATES = frozenset({
    "PERSONAL",
    "PERSONAL_ON_GET",
    "PERSONAL_DROP_ON_GET",
})


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
    if not category:
        return ""
    candidates = (category, category.split("/")[0])
    for cat in candidates:
        rel = f"{cat}/{item_id}.png"
        if not existing_icons or rel in existing_icons:
            return f"{ICON_CDN}/{ICON_REALM}/icons/{rel}"
    return f"{ICON_CDN}/{ICON_REALM}/icons/{category}/{item_id}.png"


def normalize_icon(icon):
    if not icon:
        return ""
    path = icon.replace("\\", "/").replace(".webp", ".png")
    if path.startswith(ICON_CDN + "/"):
        return path
    if "stalzone-database/" in path:
        path = path.split("stalzone-database/", 1)[1]
        for prefix in ("raw/main/", "blob/main/", "main/"):
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
    elif path.startswith("http"):
        return path
    else:
        path = path.lstrip("/")
        if not path.startswith("ru/"):
            path = f"ru/{path}"
    return f"{ICON_CDN}/{path}"


def scan_item_database(item_db_dir, existing_icons):
    tradable = []
    meta = {}
    skipped = 0
    files = [path for path in item_db_dir.rglob("*.json") if "_variants" not in path.parts]
    print(f"📁 База: {item_db_dir} ({len(files)} предметов)")

    for path in files:
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        item_id = content.get("id") or path.stem
        if not item_id:
            continue
        category = content.get("category") or ""
        if not category:
            category = path.relative_to(item_db_dir).parts[0]
        top_category = category.split("/")[0] or "other"
        info = {
            "id": item_id,
            "name": localized_name(content),
            "icon": icon_url(item_id, category, existing_icons),
            "category": top_category,
            "rank": RANK_CODES.get(content.get("color") or "", ""),
        }
        meta[item_id] = info
        if parse_item_state(content) in UNTRADABLE_STATES:
            skipped += 1
            continue
        tradable.append(info)

    print(f"📚 Мета: {len(meta)}, торгуемых: {len(tradable)}, неторгуемых: {skipped}")
    return tradable, meta


def tradable_from_catalog():
    catalog = load_json(ITEMS_FILE, {})
    tradable = []
    meta = {}
    if not isinstance(catalog, dict):
        return tradable, meta
    for item_id, row in catalog.items():
        if not isinstance(row, dict) or not row.get("name"):
            continue
        info = {
            "id": item_id,
            "name": row.get("name") or item_id,
            "icon": row.get("icon") or "",
            "category": row.get("category") or "other",
            "rank": row.get("rank") or "",
        }
        meta[item_id] = info
        if row.get("tradable"):
            tradable.append(info)
    print(f"📚 Каталог с диска: {len(tradable)} торгуемых, без клона базы")
    return tradable, meta


def recipe_item_ids(recipes):
    ids = set(recipes)
    for recipe in recipes.values():
        ids.update(ing["id"] for ing in recipe.get("ingredients", []))
    return ids


def load_scan_state():
    raw = load_json(ACTIVE_ITEMS_FILE, None)
    if isinstance(raw, list):
        return set(raw), 0, 0
    if isinstance(raw, dict):
        items = set(raw.get("items") or [])
        if "sales_cursor" not in raw:
            return items, 0, 0
        return items, int(raw.get("cursor") or 0), int(raw.get("sales_cursor") or 0)
    return set(), 0, 0


def discovery_rank(item, priority_ids):
    category = item.get("category")
    if category == "weapon":
        return 0
    if category in {"armor", "armour"}:
        return 1
    if category == "weapon_modules":
        return 2
    if item["id"] in priority_ids:
        return 3
    if category == "attachment":
        return 4
    return 5


def pick_price_items(tradable, history, hot_ids, cursor, priority_ids):
    by_id = {item["id"]: item for item in tradable}
    hot = []
    for item_id in hot_ids:
        item = by_id.get(item_id)
        if not item:
            continue
        records = history.get(item_id) or []
        seen = _parse_ts(records[-1]) if records else None
        hot.append((seen or datetime.min.replace(tzinfo=timezone.utc), item))
    hot.sort(key=lambda row: (0 if row[1].get("category") in ALWAYS_PRICE_CATS else 1, row[0]))
    discover_slots = min(DISCOVERY_SLOTS, PRICE_BUDGET)
    refresh_slots = PRICE_BUDGET - discover_slots
    if len(hot) < refresh_slots:
        discover_slots += refresh_slots - len(hot)
        refresh_slots = len(hot)
    chosen = [item for _, item in hot[:refresh_slots]]
    ordered = sorted(tradable, key=lambda item: (discovery_rank(item, priority_ids), item["id"]))
    if not ordered:
        window = []
        next_cursor = 0
    else:
        start = cursor % len(ordered)
        rotated = ordered[start:] + ordered[:start]
        window = []
        seen = 0
        for item in rotated:
            seen += 1
            if item["id"] in hot_ids:
                continue
            window.append(item)
            if len(window) >= discover_slots:
                break
        next_cursor = (start + seen) % len(ordered)
    weapons = sum(1 for item in window if item.get("category") == "weapon")
    print(
        f"⚡ Живые лоты: {len(chosen)} из {len(hot)}, поиск новых: {len(window)} "
        f"(пушек в поиске {weapons})"
    )
    return chosen + window, next_cursor


def bounded_int(extra, keys, low, high):
    if not isinstance(extra, dict):
        return None
    for key in keys:
        if key not in extra:
            continue
        try:
            value = int(extra[key])
        except (TypeError, ValueError):
            continue
        if low <= value <= high:
            return value
    return None


def lot_traits(raw):
    extra = raw.get("additional") if isinstance(raw, dict) else None
    traits = {}
    quality = bounded_int(extra, ("qlt", "quality"), 0, 5)
    upgrade = bounded_int(extra, ("ptn", "upgrade", "level"), 0, 15)
    if quality is not None:
        traits["q"] = quality
    if upgrade is not None:
        traits["u"] = upgrade
    return traits


def keep_lots(lots):
    grouped = {}
    plain = []
    for lot in lots:
        key = (lot.get("q"), lot.get("u"))
        if key == (None, None):
            if len(plain) < MAX_LOTS_PER_ITEM:
                plain.append(lot)
            continue
        grouped.setdefault(key, lot)
    if not grouped:
        return plain
    chosen = sorted(grouped.values(), key=lambda lot: lot["price_per_unit"])
    return chosen[:24]


def fetch_lots(api, item_id):
    data = api.get(f"{API_BASE}/{REGION}/auction/{item_id}/lots?limit=200&additional=true")
    if not isinstance(data, dict) or not isinstance(data.get("lots"), list):
        return item_id, None
    raw_lots = data["lots"]
    processed = []
    for lot in raw_lots:
        buyout = lot.get("buyoutPrice") or 0
        amount = lot.get("amount") or 0
        if buyout <= 0 or amount <= 0:
            continue
        row = {
            "price_per_unit": round(buyout / amount),
            "buyout": buyout,
            "amount": amount,
            "endTime": lot.get("endTime", ""),
        }
        row.update(lot_traits(lot))
        processed.append(row)
    processed.sort(key=lambda lot: lot["price_per_unit"])
    return item_id, processed


def clock_from_entries(raw_entries):
    slots = [{"a": 0, "sum": 0} for _ in range(CLOCK_SLOTS)]
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        stamp = entry.get("time") or entry.get("timestamp") or ""
        price = entry.get("price") or entry.get("buyoutPrice")
        amount = entry.get("amount") or 1
        if not stamp or not price:
            continue
        try:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            price = int(price)
            amount = int(amount)
        except (TypeError, ValueError):
            continue
        if amount <= 0 or price <= 0:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        slot = dt.astimezone(MSK).hour // 3
        slots[slot]["a"] += amount
        slots[slot]["sum"] += price
    return [
        {"a": slot["a"], "p": round(slot["sum"] / slot["a"]) if slot["a"] else 0}
        for slot in slots
    ]


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
        if amount <= 0 or price <= 0:
            continue
        unit = round(price / amount)
        bucket = days.setdefault(day, {"prices": [], "total_amount": 0, "total_sum": 0})
        bucket["prices"].append(unit)
        bucket["total_amount"] += amount
        bucket["total_sum"] += price

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


def fetch_sales(api, item_id, max_pages=SALES_MAX_PAGES):
    base = f"{API_BASE}/{REGION}/auction/{item_id}/history"
    collected = []
    total_available = None
    used = 0
    for page in range(max_pages):
        offset = page * SALES_PAGE_SIZE
        used += 1
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
    return item_id, parse_sales(collected), clock_from_entries(collected), used


def merge_sales(previous_rows, fresh_rows):
    by_day = {}
    for row in previous_rows or []:
        if isinstance(row, dict) and row.get("t"):
            by_day[str(row["t"])[:10]] = row
    for row in fresh_rows or []:
        if isinstance(row, dict) and row.get("t"):
            by_day[str(row["t"])[:10]] = row
    merged = [by_day[day] for day in sorted(by_day)]
    return merged[-SALES_MAX_DAYS:]


def _parse_ts(record):
    try:
        return datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None


def slim_point(record, keep_meta=False, keep_lots=False):
    point = {"timestamp": record.get("timestamp", ""), "price": record.get("price", 0)}
    if keep_meta:
        point.update({
            "name": record.get("name", ""),
            "icon": record.get("icon", ""),
            "category": record.get("category", "other"),
            "total_amount": record.get("total_amount", 0),
            "lots_count": record.get("lots_count", 0),
            "best_lot_amount": record.get("best_lot_amount"),
            "best_lot_buyout": record.get("best_lot_buyout"),
        })
    if keep_lots:
        point["lots"] = record.get("lots") or []
    return point


def compact_history(records):
    if not records:
        return []
    now = utc_now()
    cutoff = now - timedelta(hours=48)
    last_meta = {}
    last_full = records[-1]
    hourly = {}
    fresh = []
    for record in records:
        ts = _parse_ts(record)
        if ts is None:
            continue
        for key in ("name", "icon", "category"):
            if record.get(key):
                last_meta[key] = record[key]
        if ts >= cutoff:
            fresh.append(slim_point(record))
            continue
        key = ts.strftime("%Y-%m-%dT%H")
        prev = hourly.get(key)
        if prev is None or record.get("price", 0) < prev["price"]:
            hourly[key] = {"timestamp": f"{key}:00:00+00:00", "price": record.get("price", 0), "aggregated": True}

    result = [hourly[k] for k in sorted(hourly)] + fresh
    if not result:
        result = [last_full]
    result[-1] = slim_point(
        {**last_meta, **last_full},
        keep_meta=True,
        keep_lots=True,
    )
    return result[-MAX_HISTORY:]


def sales_typical(entries, days=14):
    if not entries:
        return 0
    cutoff = (utc_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [e for e in entries if isinstance(e, dict) and e.get("avg", 0) > 0 and e.get("t", "")[:10] >= cutoff]
    if not recent:
        recent = [e for e in entries if isinstance(e, dict) and e.get("avg", 0) > 0][-days:]
    if not recent:
        return 0
    avgs = sorted(e["avg"] for e in recent)
    median = avgs[len(avgs) // 2]
    clean = [e for e in recent if e["avg"] <= median * 8]
    if not clean:
        clean = recent
    total_qty = sum(e.get("a", 0) for e in clean)
    if total_qty <= 0:
        return median
    return round(sum(e["avg"] * e.get("a", 0) for e in clean) / total_qty)


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
            "icon": normalize_icon(recipe.get("icon", "")),
            "requiredStations": recipe.get("requiredStations", []),
            "stations": recipe.get("stations", []),
            "ingredients": [
                {
                    "id": res.get("itemId") or res.get("id"),
                    "amount": res["amount"],
                    "name": res.get("name") or "",
                    "icon": normalize_icon(res.get("icon", "")),
                }
                for res in resources
                if isinstance(res, dict) and (res.get("itemId") or res.get("id")) and res.get("amount")
            ],
        }
    print(f"✅ Рецептов: {len(recipes)}")
    return recipes


def market_price(info):
    return info.get("price") or info.get("sales_avg") or 0


def profit_fields(sell, cost):
    if sell <= 0 or cost <= 0:
        return {"profit": None, "roi": None}
    profit = sell - cost
    return {"profit": profit, "roi": profit / cost * 100}


def compute_recipe_costs(recipes, prices):
    computed = {}

    def deep(item_id, stack):
        cached = computed.get(item_id)
        if cached is not None:
            return cached["cost_craft"]
        info = prices.get(item_id, {})
        market = market_price(info)
        recipe = recipes.get(item_id)
        if not recipe or item_id in stack:
            computed[item_id] = {
                "cost_craft": market,
                "cost_direct": market,
                "cost": market,
                "sell_price": market,
            }
            return market

        stack = stack | {item_id}
        batch_craft = 0
        batch_direct = 0
        missing_direct = 0
        for ing in recipe["ingredients"]:
            child_id = ing["id"]
            child_craft = deep(child_id, stack)
            child_market = market_price(prices.get(child_id, {}))
            batch_craft += (child_craft or child_market) * ing["amount"]
            if child_market > 0:
                batch_direct += child_market * ing["amount"]
            elif child_craft > 0:
                batch_direct += child_craft * ing["amount"]
            else:
                missing_direct += 1

        amount = recipe.get("craftAmount", 1) or 1
        cost_craft = batch_craft / amount
        cost_direct = batch_direct / amount
        options = [n for n in (market, cost_craft) if n > 0]
        cost = min(options) if options else 0
        computed[item_id] = {
            "cost_craft": cost_craft,
            "cost_direct": cost_direct,
            "cost": cost,
            "sell_price": market,
            "missing_direct": missing_direct,
            "total_direct_cost_batch": batch_direct,
            "total_cost_batch": batch_craft,
        }
        return cost_craft

    for item_id in recipes:
        deep(item_id, frozenset())

    result = {}
    for item_id, recipe in recipes.items():
        info = prices.get(item_id, {})
        costs = computed.get(item_id, {})
        sell = costs.get("sell_price") or 0
        cost = costs.get("cost") or 0
        cost_direct = costs.get("cost_direct") or 0
        cost_craft = costs.get("cost_craft") or 0
        row = {
            "id": item_id,
            "name": info.get("name") or recipe.get("name", item_id),
            "icon": normalize_icon(info.get("icon") or recipe.get("icon", "")),
            "sell_price": sell,
            "craftable": True,
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
            "ingredients": recipe.get("ingredients", []),
            "cost": cost,
            "cost_craft": cost_craft,
            "cost_direct": cost_direct,
            "total_direct_cost_batch": costs.get("total_direct_cost_batch", 0),
            "total_cost_batch": costs.get("total_cost_batch", 0),
            "missing_direct": costs.get("missing_direct", 0),
        }
        row.update({f"{k}_direct": v for k, v in profit_fields(sell, cost_direct).items()})
        row.update({f"{k}_craft": v for k, v in profit_fields(sell, cost_craft).items()})
        row.update(profit_fields(sell, cost_direct if cost_direct > 0 else cost))
        result[item_id] = row
    return result


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
    found = set()
    failed = set()
    for item_id, lots in run_pool(
        [item["id"] for item in items],
        lambda item_id: fetch_lots(api, item_id),
        "цены",
    ):
        if lots is None:
            failed.add(item_id)
            continue
        if not lots:
            continue
        found.add(item_id)
        best = lots[0]
        meta = meta_map.get(item_id, {})
        history.setdefault(item_id, []).append({
            "timestamp": now,
            "price": best["price_per_unit"],
            "best_lot_amount": best["amount"],
            "best_lot_buyout": best["buyout"],
            "total_amount": sum(lot["amount"] for lot in lots),
            "lots_count": len(lots),
            "lots": keep_lots(lots),
            "name": meta.get("name", item_id),
            "icon": meta.get("icon", ""),
            "category": meta.get("category", "other"),
        })
        history[item_id] = compact_history(history[item_id])
    save_json(DATA_FILE, history)
    print(f"✅ Цены: {len(found)} с лотами, {len(failed)} сбоев, пустых {len(items) - len(found) - len(failed)}")
    return history, found, failed


def save_clock(clock):
    body = {key: value for key, value in clock.items() if not str(key).startswith("_")}
    save_json(CLOCK_FILE, {
        "_updated_at": utc_now().isoformat(),
        "_tz": "MSK",
        "_slot": 3,
        **body,
    })


def update_sales_slice(api, item_ids, previous, cursor):
    ordered = sorted({item_id for item_id in item_ids if item_id})
    sales = {
        key: value
        for key, value in previous.items()
        if key != "_updated_at"
    }
    if not ordered:
        return sales, cursor
    clock = load_json(CLOCK_FILE, {})
    if not isinstance(clock, dict):
        clock = {}
    start = cursor % len(ordered)
    rotated = ordered[start:] + ordered[:start]
    done = 0
    for item_id in rotated[:SALES_SLICE_ITEMS]:
        item_id, fresh, slots, _used = fetch_sales(api, item_id, SALES_SLICE_PAGES)
        if fresh:
            sales[item_id] = merge_sales(sales.get(item_id), fresh)
            fresh_amount = sum(int(slot.get("a") or 0) for slot in slots if isinstance(slot, dict))
            stored = clock.get(item_id) if isinstance(clock.get(item_id), list) else []
            stored_amount = sum(int(slot.get("a") or 0) for slot in stored if isinstance(slot, dict))
            if fresh_amount and fresh_amount >= stored_amount:
                clock[item_id] = slots
        done += 1
    sales["_updated_at"] = utc_now().isoformat()
    save_json(SALES_FILE, sales)
    save_clock(clock)
    print(f"📊 Продажи: {done} из {len(ordered)}, по {SALES_SLICE_PAGES} стр.")
    return sales, (start + done) % len(ordered)


def save_item_catalog(meta, tradable):
    tradable_ids = {item["id"] for item in tradable}
    catalog = {}
    for item_id, info in meta.items():
        if not info.get("name"):
            continue
        row = {
            "name": info.get("name") or item_id,
            "icon": info.get("icon") or "",
            "category": info.get("category") or "other",
            "tradable": item_id in tradable_ids,
        }
        if info.get("rank"):
            row["rank"] = info["rank"]
        catalog[item_id] = row
    save_json(ITEMS_FILE, catalog)
    tradable_count = sum(1 for row in catalog.values() if row["tradable"])
    print(f"✅ Каталог: {len(catalog)} предметов, торгуемых {tradable_count}")
    return catalog


def update_crafts(recipes, history, all_meta, sales):
    if not recipes:
        return
    current_prices = {}
    for item_id in set(history) | set(all_meta) | set(recipes):
        records = history.get(item_id, [])
        last = records[-1] if records else {}
        meta = all_meta.get(item_id, {})
        listing = last.get("price", 0) or 0
        typical = sales_typical(sales.get(item_id) or [])
        recipe = recipes.get(item_id, {})
        current_prices[item_id] = {
            "name": last.get("name") or meta.get("name") or recipe.get("name") or item_id,
            "icon": normalize_icon(last.get("icon") or meta.get("icon") or recipe.get("icon") or ""),
            "price": listing or typical,
            "sales_avg": typical,
        }
    trees = compute_recipe_costs(recipes, current_prices)
    catalog = {}
    for item_id, recipe in recipes.items():
        catalog[item_id] = {
            "name": trees.get(item_id, {}).get("name") or recipe.get("name") or item_id,
            "icon": trees.get(item_id, {}).get("icon") or recipe.get("icon") or "",
        }
        for ing in recipe.get("ingredients", []):
            if ing["id"] in catalog:
                continue
            catalog[ing["id"]] = {
                "name": ing.get("name") or ing["id"],
                "icon": ing.get("icon") or "",
            }
    trees["_items"] = catalog
    save_json(CRAFTS_FILE, trees)
    print(f"✅ Крафты: {len(trees) - 1}")


def main():
    print(f"Запуск (регион: {REGION})")
    item_db_dir = find_item_db_dir()
    if item_db_dir is None:
        tradable, all_meta = tradable_from_catalog()
    else:
        existing_icons = load_existing_icons()
        print(f"🖼️ Иконок: {len(existing_icons)}")
        tradable, all_meta = scan_item_database(item_db_dir, existing_icons)
        save_item_catalog(all_meta, tradable)
    if not tradable:
        raise SystemExit("В базе нет торгуемых предметов")

    token = get_access_token(os.environ.get("CLIENT_ID"), os.environ.get("CLIENT_SECRET"))
    print("✅ Токен получен")
    api = StalcraftApi(token)

    recipes = load_recipes()
    hot_ids, cursor, sales_cursor = load_scan_state()
    history = {item_id: compact_history(records) for item_id, records in load_json(DATA_FILE, {}).items()}
    priced = set(history)
    missing_recipe_ids = recipe_item_ids(recipes) - priced
    price_items, cursor = pick_price_items(tradable, history, hot_ids, cursor, missing_recipe_ids)

    history, found, failed = update_prices(api, price_items, history)
    scanned = {item["id"] for item in price_items}
    known = {item["id"] for item in tradable}
    hot_next = ((hot_ids - scanned) | found | failed) & known
    save_json(
        ACTIVE_ITEMS_FILE,
        {"items": sorted(hot_next), "cursor": cursor, "sales_cursor": sales_cursor},
        compact=False,
    )
    print(f"💾 Живых лотов: {len(hot_next)}")

    sales, sales_cursor = update_sales_slice(api, sorted(hot_next), load_json(SALES_FILE, {}), sales_cursor)
    save_json(
        ACTIVE_ITEMS_FILE,
        {"items": sorted(hot_next), "cursor": cursor, "sales_cursor": sales_cursor},
        compact=False,
    )
    update_crafts(recipes, history, all_meta, sales)


def rebuild_offline():
    history = {item_id: compact_history(records) for item_id, records in load_json(DATA_FILE, {}).items()}
    save_json(DATA_FILE, history)
    print(f"🗜️ История сжата: {DATA_FILE.stat().st_size / 1024:.0f} КБ")
    sales = load_json(SALES_FILE, {})
    update_crafts(load_recipes(), history, {}, sales)


def catalog_only():
    item_db_dir = find_item_db_dir()
    if item_db_dir is None:
        raise SystemExit("Нет itemdb — сначала клонируй EXBO-Studio/stalzone-database")
    tradable, all_meta = scan_item_database(item_db_dir, load_existing_icons())
    save_item_catalog(all_meta, tradable)


if __name__ == "__main__":
    if "--offline" in sys.argv:
        rebuild_offline()
    elif "--catalog" in sys.argv:
        catalog_only()
    else:
        main()
