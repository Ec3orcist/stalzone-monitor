import os
import re
import json
import time
import random
from pathlib import Path
from datetime import datetime

try:
    import cloudscraper
except ImportError:
    print("❌ Установите cloudscraper: pip install cloudscraper")
    exit(1)

# --- Настройки ---
ITEM_DB_DIR = "itemdb/ru/items"
OUTPUT_FILE = "recipes.json"
REGION = "EU"
LANG = "ru"

# Задержка между запросами (сек), чтобы не забанили
MIN_DELAY = 0.5
MAX_DELAY = 1.5

# Сколько предметов обрабатывать за один запуск (для отладки)
MAX_ITEMS = 3000

# Пропускать предметы, для которых уже есть рецепт
SKIP_EXISTING = True


def load_existing_recipes():
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_recipes(recipes):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(recipes, f, ensure_ascii=False, indent=2)
    print(f"💾 Сохранено рецептов: {len(recipes)}")


def get_tradable_items():
    """Возвращает список ID торгуемых предметов из локальной базы."""
    root = Path(ITEM_DB_DIR)
    if not root.exists():
        print(f"❌ Папка {ITEM_DB_DIR} не найдена. Убедитесь, что шаг 'Clone item database' выполнен.")
        return []

    items = []
    for path in root.rglob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = json.load(f)
        except Exception:
            continue

        status = content.get("status", {})
        state = status.get("state", "") if isinstance(status, dict) else str(status)
        if state in ("PERSONAL_ON_USE", "PERSONAL"):
            continue

        items.append(path.stem)

    print(f"📦 Найдено торгуемых предметов: {len(items)}")
    return items


def parse_ingredients(html_text):
    """
    Извлекает ингредиенты из HTML-страницы рецепта.
    Возвращает список словарей: [{"id": "...", "amount": N}, ...]
    """
    ingredients = []

    # Ищем блок с ингредиентами. На StalzoneHQ они обычно в тексте после "Ingredients:"
    # Пример: "Ingredients: 10 x Boards 2000 Energy"
    # Или: "5x Plastic Bottle 5x Fried Boar Meat ..."

    # Убираем HTML-теги для упрощения
    text = re.sub(r'<[^>]+>', ' ', html_text)
    text = re.sub(r'\s+', ' ', text)

    # Ищем паттерн "Ingredients:" и берём текст до "Crafted at" или конца
    m = re.search(r'Ingredients?:\s*(.*?)(?:Crafted at|Requires|The recipe will craft|$)', text, re.IGNORECASE)
    if not m:
        return []

    ingredients_text = m.group(1)

    # Паттерн для "N x ItemName" или "Nx ItemName"
    pattern = r'(\d+)\s*x\s*([A-ZА-Я][A-Za-zА-Яа-я0-9\s\-\.\'«»"]+)'
    matches = re.findall(pattern, ingredients_text)

    for amount_str, name in matches:
        name = name.strip()
        if not name or len(name) < 2:
            continue
        # Пропускаем "Energy", "Crafted", и т.п.
        if name.lower() in ('energy', 'crafted', 'requires', 'the recipe'):
            continue
        ingredients.append({
            "name": name,
            "amount": int(amount_str)
        })

    return ingredients


def parse_recipe_page(item_id, scraper):
    """
    Загружает страницу рецепта и возвращает словарь с рецептом или None.
    """
    url = f"https://stalzonehq.com/hideout/recipes/{item_id}"

    try:
        resp = scraper.get(url, timeout=20)

        if resp.status_code == 404:
            return None

        if resp.status_code != 200:
            print(f"  ⚠️ {item_id}: HTTP {resp.status_code}")
            return None

        html = resp.text

        # Проверяем, что страница содержит рецепт
        if "Hideout Recipes" not in html and "hideout" not in html.lower():
            return None

        # Проверяем, есть ли вообще ингредиенты
        if "Ingredients" not in html and "ingredients" not in html.lower():
            # Возможно, это страница предмета, который не крафтится
            return None

        ingredients = parse_ingredients(html)

        if not ingredients:
            return None

        # Пытаемся вытащить энергию
        energy_match = re.search(r'(\d+)\s*Energy', html, re.IGNORECASE)
        energy = int(energy_match.group(1)) if energy_match else 0

        # Ищем название предмета (первый заголовок ##)
        title_match = re.search(r'##\s*([^\n#]+)', html)
        name = title_match.group(1).strip() if title_match else item_id

        return {
            "name": name,
            "energy": energy,
            "ingredients": ingredients
        }

    except Exception as e:
        print(f"  ❌ {item_id}: {e}")
        return None


def main():
    print("=" * 60)
    print("ПАРСЕР РЕЦЕПТОВ STALZONEHQ")
    print("=" * 60)

    # Загружаем существующие рецепты
    recipes = load_existing_recipes()
    print(f"📂 Уже есть рецептов: {len(recipes)}")

    # Получаем список предметов
    items = get_tradable_items()
    if not items:
        return

    # Ограничиваем
    items = items[:MAX_ITEMS]
    print(f"🔍 Проверяем предметов: {len(items)}")

    # Создаём scraper для обхода Cloudflare
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )

    added = 0
    checked = 0

    for i, item_id in enumerate(items):
        if SKIP_EXISTING and item_id in recipes:
            continue

        checked += 1
        if checked % 50 == 0:
            print(f"  проверено {checked}/{len(items)}...")

        recipe = parse_recipe_page(item_id, scraper)

        if recipe:
            recipes[item_id] = recipe
            added += 1
            print(f"  ✅ +{item_id}: {recipe['name']} ({len(recipe['ingredients'])} ингредиентов)")
            # Сохраняем после каждого успеха, чтобы не потерять при сбое
            save_recipes(recipes)

        # Задержка
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    print(f"\n✅ Готово!")
    print(f"   Добавлено новых рецептов: {added}")
    print(f"   Всего рецептов в файле: {len(recipes)}")

    save_recipes(recipes)


if __name__ == "__main__":
    main()
