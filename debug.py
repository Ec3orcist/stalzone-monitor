import os
import json
import requests
from pathlib import Path

REGION = os.environ.get("REGION", "RU")
TOKEN_URL = "https://exbo.net/oauth/token"
ITEM_DB_DIR = "itemdb/ru/items"

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

print("=" * 70)
print("ДИАГНОСТИКА API STALZONE")
print("=" * 70)

# --- Токен ---
payload = {
    "grant_type": "client_credentials",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "scope": ""
}
r = requests.post(TOKEN_URL, data=payload, timeout=10)
token = r.json().get("access_token")
print(f"\nТокен: {'✅ получен' if token else '❌ не получен'}")

# --- Берём 5 торгуемых предметов ---
root = Path(ITEM_DB_DIR)
tradable = []
for path in root.rglob("*.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = json.load(f)
        status = content.get("status", {})
        state = status.get("state", "") if isinstance(status, dict) else str(status)
        if state in ("PERSONAL_ON_USE", "PERSONAL"):
            continue
        tradable.append((path, content))
        if len(tradable) >= 5:
            break
    except Exception:
        continue

print(f"\nВзято торгуемых предметов для теста: {len(tradable)}")

# --- Для каждого пробуем несколько URL ---
print("\n" + "=" * 70)
print("ПРОВЕРКА URL")
print("=" * 70)

for path, content in tradable:
    stem = path.stem
    internal_id = content.get("id") or content.get("Id") or content.get("ID")
    name = content.get("name") or content.get("Name")

    print(f"\n📄 Файл: {path.relative_to(root)}")
    print(f"   stem (имя файла): {stem}")
    print(f"   внутренний id:    {internal_id}")
    print(f"   name:             {name}")
    print(f"   ключи в JSON:     {list(content.keys())[:15]}")

    # Пробуем разные варианты URL
    candidates = []
    if stem:
        candidates.append(("stem", stem))
    if internal_id and internal_id != stem:
        candidates.append(("internal_id", internal_id))

    for label, item_id in candidates:
        # Вариант 1: /auction/{id}/lots
        url1 = f"https://eapi.stalcraft.net/{REGION}/auction/{item_id}/lots?limit=3"
        try:
            resp = requests.get(url1, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            print(f"   ➡ [{label}={item_id}] /auction/{item_id}/lots → {resp.status_code}")
            if resp.status_code == 200:
                print(f"      ✅ {resp.text[:300]}")
        except Exception as e:
            print(f"   ➡ {url1} → ошибка: {e}")

        # Вариант 2: /auction/lots?item={id}
        url2 = f"https://eapi.stalcraft.net/{REGION}/auction/lots?item={item_id}&limit=3"
        try:
            resp = requests.get(url2, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            print(f"   ➡ [{label}={item_id}] /auction/lots?item=... → {resp.status_code}")
            if resp.status_code == 200:
                print(f"      ✅ {resp.text[:300]}")
        except Exception as e:
            print(f"   ➡ {url2} → ошибка: {e}")

    print("-" * 70)

print("\nДИАГНОСТИКА ЗАВЕРШЕНА")
