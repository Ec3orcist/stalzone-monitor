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
SALES_PAGE_SIZE = 200
RECENT_SALE_DAYS = 30
LIQUID_SOLD14 = 20
PROBE_STALE_DAYS = 7
BACKFILL_PAGES = 4
MAX_HISTORY = 168
MAX_ITEMS = 3000
GRADUAL_PRICE_SLOTS = 48
GRADUAL_WARM_SLOTS = 8
GRADUAL_SEEK_SLOTS = 16
SALES_REQUEST_BUDGET = 48
LOT_PAGE = 200
MAX_LOT_PAGES = 2
LIQUID_SALES_CAP = 12
RARE_SALES_CAP = 4
CLASSIFY_CAP = 16
BACKFILL_REQUEST_CAP = 12
WORKERS = 4
MAX_LOTS_PER_ITEM = 10
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


def empty_scan_state():
    return {
        "hot": set(),
        "warm": set(),
        "probed": {},
        "deep": set(),
        "cursor": 0,
        "quiet_cursor": 0,
        "warm_cursor": 0,
        "probe_cursor": 0,
        "sales_cursor": 0,
        "rare_cursor": 0,
    }


def load_scan_state():
    raw = load_json(ACTIVE_ITEMS_FILE, None)
    state = empty_scan_state()
    if isinstance(raw, list):
        state["hot"] = set(raw)
        return state
    if not isinstance(raw, dict):
        return state
    state["hot"] = set(raw.get("items") or [])
    state["warm"] = set(raw.get("warm") or [])
    probed = raw.get("probed") or {}
    if isinstance(probed, dict):
        state["probed"] = {str(item_id): str(day) for item_id, day in probed.items()}
    state["deep"] = set(raw.get("deep") or [])
    for key in ("cursor", "quiet_cursor", "warm_cursor", "probe_cursor", "sales_cursor", "rare_cursor"):
        state[key] = int(raw.get(key) or 0)
    if "warm" not in raw:
        state["cursor"] = 0
    return state


def save_scan_state(state):
    save_json(ACTIVE_ITEMS_FILE, {
        "items": sorted(state["hot"]),
        "warm": sorted(state["warm"]),
        "probed": state["probed"],
        "deep": sorted(state["deep"]),
        "cursor": state["cursor"],
        "quiet_cursor": state["quiet_cursor"],
        "warm_cursor": state["warm_cursor"],
        "probe_cursor": state["probe_cursor"],
        "sales_cursor": state["sales_cursor"],
        "rare_cursor": state["rare_cursor"],
    }, compact=False)


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


def day_cutoff(days):
    return (utc_now() - timedelta(days=days)).date().isoformat()


def sale_bounds(rows):
    days = []
    sold14 = 0
    recent_from = day_cutoff(14)
    if not isinstance(rows, list):
        return "", "", 0
    for row in rows:
        if not isinstance(row, dict) or not row.get("t"):
            continue
        day = str(row["t"])[:10]
        days.append(day)
        if day >= recent_from:
            sold14 += int(row.get("a") or 0)
    if not days:
        return "", "", 0
    return min(days), max(days), sold14


def is_liquid(sales, item_id):
    rows = sales.get(item_id)
    if not isinstance(rows, list):
        return True
    _, _, sold14 = sale_bounds(rows)
    return sold14 >= LIQUID_SOLD14


def sold_this_month(sales, item_id):
    rows = sales.get(item_id)
    if not isinstance(rows, list):
        return False
    _, newest, _ = sale_bounds(rows)
    return bool(newest) and newest >= day_cutoff(RECENT_SALE_DAYS)


def rotate_ids(ids, cursor, slots):
    ordered = sorted(ids)
    if not ordered or slots <= 0:
        return [], cursor if ordered else 0
    start = cursor % len(ordered)
    window = (ordered[start:] + ordered[:start])[:slots]
    return window, (start + len(window)) % len(ordered)


def price_age(history, item_id):
    records = history.get(item_id) or []
    seen = _parse_ts(records[-1]) if records else None
    return seen or datetime.min.replace(tzinfo=timezone.utc)


def pick_price_items(tradable, history, _sales, hot_ids, warm_ids, cursors, craft_ids):
    by_id = {item["id"]: item for item in tradable}
    craft_items = [by_id[item_id] for item_id in sorted(craft_ids) if item_id in by_id]
    craft_set = {item["id"] for item in craft_items}
    listed = []
    for item_id in hot_ids:
        if item_id in craft_set:
            continue
        item = by_id.get(item_id)
        if item:
            listed.append(item)
    listed.sort(key=lambda item: price_age(history, item["id"]))
    warm_pool = [item_id for item_id in warm_ids if item_id in by_id and item_id not in craft_set]
    warm_take, warm_cursor = rotate_ids(warm_pool, cursors["warm_cursor"], GRADUAL_WARM_SLOTS)
    listed_slots = max(0, GRADUAL_PRICE_SLOTS - GRADUAL_WARM_SLOTS - GRADUAL_SEEK_SLOTS)
    listed_take = listed[:listed_slots]
    skip = craft_set | set(hot_ids) | set(warm_ids) | {item["id"] for item in listed_take} | set(warm_take)
    ordered = sorted(tradable, key=lambda item: (discovery_rank(item, craft_set), item["id"]))
    window = []
    next_cursor = cursors["cursor"]
    if ordered and GRADUAL_SEEK_SLOTS > 0:
        start = cursors["cursor"] % len(ordered)
        rotated = ordered[start:] + ordered[:start]
        seen = 0
        for item in rotated:
            seen += 1
            if item["id"] in skip:
                continue
            window.append(item)
            if len(window) >= GRADUAL_SEEK_SLOTS:
                break
        next_cursor = (start + seen) % len(ordered)
    items = craft_items + listed_take + [by_id[item_id] for item_id in warm_take] + window
    print(
        f"⚡ Крафт каждый проход: {len(craft_items)}, остальные лоты: {len(listed_take)} из {len(listed)}, "
        f"без лота за месяц: {len(warm_take)} из {len(warm_pool)}, поиск: {len(window)}"
    )
    return items, next_cursor, cursors["quiet_cursor"], warm_cursor


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


def lot_numbers(lot):
    try:
        buyout = int(lot.get("buyoutPrice") or 0)
    except (TypeError, ValueError):
        return 0, 0
    raw_amount = lot.get("amount")
    if raw_amount is None:
        amount = 1
    else:
        try:
            amount = int(raw_amount)
        except (TypeError, ValueError):
            return 0, 0
    return buyout, amount


def fetch_lots(api, item_id):
    collected = []
    total = 0
    for page in range(MAX_LOT_PAGES):
        offset = page * LOT_PAGE
        data = api.get(
            f"{API_BASE}/{REGION}/auction/{item_id}/lots"
            f"?limit={LOT_PAGE}&offset={offset}&additional=true&sort=buyout_price&order=asc"
        )
        if not isinstance(data, dict) or not isinstance(data.get("lots"), list):
            if collected:
                break
            return item_id, None
        raw_lots = data["lots"]
        if page == 0:
            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
        collected.extend(raw_lots)
        if not raw_lots or len(raw_lots) < LOT_PAGE or (total and len(collected) >= total):
            break
    processed = []
    for lot in collected:
        buyout, amount = lot_numbers(lot)
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


def fetch_sales(api, item_id, max_pages):
    base = f"{API_BASE}/{REGION}/auction/{item_id}/history"
    collected = []
    total_available = None
    used = 0
    exhausted = False
    for page in range(max_pages):
        offset = page * SALES_PAGE_SIZE
        used += 1
        data = api.get(f"{base}?limit={SALES_PAGE_SIZE}&offset={offset}")
        if not isinstance(data, dict):
            if collected:
                break
            return item_id, None, [], used, False
        prices = data.get("prices") or []
        if total_available is None:
            total_available = data.get("total", 0)
        if not prices:
            exhausted = True
            break
        collected.extend(prices)
        if total_available and len(collected) >= total_available:
            exhausted = True
            break
        if len(prices) < SALES_PAGE_SIZE:
            exhausted = True
            break
    return item_id, parse_sales(collected), clock_from_entries(collected), used, exhausted


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
    meta_map = {item["id"]: item for item in items}
    found = set()
    failed = set()
    fetched = run_pool(
        [item["id"] for item in items],
        lambda item_id: fetch_lots(api, item_id),
        "цены",
    )
    now = utc_now().isoformat()
    for item_id, lots in fetched:
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


def trim_sales(sales):
    cutoff = day_cutoff(SALES_MAX_DAYS)
    trimmed = {}
    for item_id, rows in sales.items():
        if str(item_id).startswith("_") or not isinstance(rows, list):
            continue
        kept = [
            row for row in rows
            if isinstance(row, dict) and str(row.get("t") or "")[:10] >= cutoff
        ]
        if kept:
            trimmed[item_id] = kept
    return trimmed


def probe_due(item_id, probed):
    stamp = probed.get(item_id)
    if not stamp:
        return True
    return stamp < day_cutoff(PROBE_STALE_DAYS)


def needs_probe(item_id, sales, hot_ids, probed):
    if item_id in hot_ids:
        return False
    if sold_this_month(sales, item_id):
        return False
    return probe_due(item_id, probed)


def remember_sales(item_id, fresh, sales):
    if not fresh:
        return False
    _, newest, _ = sale_bounds(fresh)
    if not newest or newest < day_cutoff(RECENT_SALE_DAYS):
        return False
    sales[item_id] = merge_sales(sales.get(item_id), fresh)
    return True


def note_depth(item_id, fresh, exhausted, pages_requested, sales, deep):
    if fresh is None:
        return
    oldest, newest, _ = sale_bounds(sales.get(item_id) or fresh or [])
    if not newest:
        deep.discard(item_id)
        return
    if oldest <= day_cutoff(RECENT_SALE_DAYS) or exhausted:
        deep.discard(item_id)
        return
    if pages_requested >= BACKFILL_PAGES:
        deep.add(item_id)


def store_clock(clock, item_id, slots):
    fresh_amount = sum(int(slot.get("a") or 0) for slot in slots if isinstance(slot, dict))
    stored = clock.get(item_id) if isinstance(clock.get(item_id), list) else []
    stored_amount = sum(int(slot.get("a") or 0) for slot in stored if isinstance(slot, dict))
    if fresh_amount and fresh_amount >= stored_amount:
        clock[item_id] = slots


def pull_history(api, item_id, pages, sales, clock, deep):
    _item_id, fresh, slots, used, exhausted = fetch_sales(api, item_id, pages)
    if fresh is not None:
        remember_sales(item_id, fresh, sales)
        store_clock(clock, item_id, slots)
        note_depth(item_id, fresh, exhausted, pages, sales, deep)
    return fresh, used


def walk_ids(ordered, cursor, limit, accept):
    if not ordered or limit <= 0:
        return [], cursor
    start = cursor % len(ordered)
    rotated = ordered[start:] + ordered[:start]
    chosen = []
    seen = 0
    for item_id in rotated:
        seen += 1
        if accept(item_id):
            chosen.append(item_id)
            if len(chosen) >= limit:
                break
    return chosen, (start + seen) % len(ordered)


def update_sales_budget(api, tradable, hot_ids, warm_ids, previous, state, priority_ids):
    sales = trim_sales(previous if isinstance(previous, dict) else {})
    clock = load_json(CLOCK_FILE, {})
    if not isinstance(clock, dict):
        clock = {}
    budget = SALES_REQUEST_BUDGET
    stats = {"backfill": 0, "classify": 0, "probe": 0, "liquid": 0, "rare": 0, "requests": 0}
    known = {item["id"] for item in tradable}

    def spend(item_ids, pages, bucket, request_cap):
        nonlocal budget
        spent = 0
        for item_id in item_ids:
            if budget <= 0 or spent >= request_cap:
                break
            fresh, used = pull_history(
                api, item_id, min(pages, budget, request_cap - spent), sales, clock, state["deep"]
            )
            budget -= used
            spent += used
            stats["requests"] += used
            stats[bucket] += 1
            if bucket == "probe" and fresh is not None:
                if sold_this_month(sales, item_id):
                    state["warm"].add(item_id)
                    state["probed"].pop(item_id, None)
                else:
                    state["probed"][item_id] = utc_now().date().isoformat()

    unsettled = [
        item_id for item_id in sorted((hot_ids | warm_ids) & known)
        if item_id not in state["deep"] and isinstance(sales.get(item_id), list)
        and (sale_bounds(sales[item_id])[0] or "9999") > day_cutoff(RECENT_SALE_DAYS)
    ]
    liquid_ids = [
        item_id for item_id in (hot_ids | warm_ids) & known
        if isinstance(sales.get(item_id), list) and is_liquid(sales, item_id)
    ]
    craft_liquid = [item_id for item_id in liquid_ids if item_id in priority_ids]
    liquid_take, state["sales_cursor"] = rotate_ids(
        craft_liquid or liquid_ids, state["sales_cursor"], min(LIQUID_SALES_CAP, budget)
    )
    spend(liquid_take, 1, "liquid", budget)
    rare_ids = [
        item_id for item_id in (hot_ids | warm_ids) & known
        if isinstance(sales.get(item_id), list) and not is_liquid(sales, item_id)
    ]
    rare_take, state["rare_cursor"] = rotate_ids(rare_ids, state["rare_cursor"], min(RARE_SALES_CAP, budget))
    spend(rare_take, 1, "rare", budget)
    spend(unsettled, BACKFILL_PAGES, "backfill", min(BACKFILL_REQUEST_CAP, budget))

    missing = [item_id for item_id in sorted(hot_ids & known) if not isinstance(sales.get(item_id), list)]
    spend(missing, 1, "classify", min(CLASSIFY_CAP, budget))

    probe_order = [
        item["id"] for item in sorted(tradable, key=lambda item: (item["id"] not in priority_ids, item["id"]))
    ]
    probes, state["probe_cursor"] = walk_ids(
        probe_order,
        state["probe_cursor"],
        budget,
        lambda item_id: needs_probe(item_id, sales, hot_ids, state["probed"]),
    )
    spend(probes, 1, "probe", budget)

    state["warm"] = {
        item_id for item_id in (state["warm"] | set(sales)) & known
        if item_id not in hot_ids and sold_this_month(sales, item_id)
    }
    state["probed"] = {
        item_id: day
        for item_id, day in state["probed"].items()
        if item_id in known and item_id not in hot_ids and item_id not in state["warm"]
    }
    sales["_updated_at"] = utc_now().isoformat()
    save_json(SALES_FILE, sales)
    save_clock(clock)
    print(
        f"📊 История: добор {stats['backfill']}, новые лоты {stats['classify']}, "
        f"проба каталога {stats['probe']}, ходовые {stats['liquid']}, тихие {stats['rare']}, "
        f"запросов {stats['requests']}"
    )
    return sales


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
    state = load_scan_state()
    known = {item["id"] for item in tradable}
    sales_raw = load_json(SALES_FILE, {})
    state["hot"] &= known
    state["warm"] = {
        item_id for item_id in (state["warm"] | set(sales_raw))
        if item_id in known and item_id not in state["hot"] and sold_this_month(sales_raw, item_id)
    }
    history = {item_id: compact_history(records) for item_id, records in load_json(DATA_FILE, {}).items()}
    craft_ids = recipe_item_ids(recipes)
    price_items, state["cursor"], state["quiet_cursor"], state["warm_cursor"] = pick_price_items(
        tradable, history, sales_raw, state["hot"], state["warm"], state, craft_ids
    )

    history, found, failed = update_prices(api, price_items, history)
    scanned = {item["id"] for item in price_items}
    previous_hot = set(state["hot"])
    state["hot"] = ((previous_hot - scanned) | found | (failed & previous_hot)) & known
    dropped = (previous_hot & scanned) - found - failed
    state["warm"] = ((state["warm"] - found) | {
        item_id for item_id in dropped if sold_this_month(sales_raw, item_id)
    }) - state["hot"]
    state["warm"] &= known
    save_scan_state(state)
    print(f"💾 Живых лотов: {len(state['hot'])}, за месяц без лота: {len(state['warm'])}")

    sales = update_sales_budget(
        api, tradable, state["hot"], state["warm"], sales_raw, state, craft_ids
    )
    save_scan_state(state)
    print(f"💾 После истории без лота: {len(state['warm'])}")
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
