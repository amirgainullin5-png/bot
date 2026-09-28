import os
import re
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

def clean_filename(title: str) -> str:
    title = re.sub(r"[\n\r\t\xa0]", " ", title)
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    title = re.sub(r"(ЗАКРЕПЛЕНО|ИНФОРМАЦИЯ|ВАЖНО|ОТКРЫТО|ЗАКРЫТО)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title)
    return title.strip()


def parse_links(file_path: Path) -> list[str]:
    if not file_path.exists():
        print(f"Файл {file_path} не найден")
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip().startswith("https://")]


def parse_forum_content(urls: list[str], mode: str = "laws"):
    """
    mode = "laws"     → собирает в один laws.txt
    mode = "ranks"    → собирает структурированные критерии в ranks.json
    """
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        for idx, url in enumerate(urls, 1):
            try:
                print(f"[{idx}/{len(urls)}] Обрабатываю: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                title_elem = page.query_selector("h1.p-title-value")
                raw_title = title_elem.inner_text() if title_elem else f"Документ_{idx}"
                safe_title = clean_filename(raw_title) or f"Документ_{idx}"

                posts = page.query_selector_all(".bbWrapper")
                if not posts:
                    print(f"  → Текст не найден")
                    continue

                full_text = "\n\n".join(post.inner_text().strip() for post in posts)

                results.append({
                    "title": safe_title,
                    "url": url,
                    "text": full_text
                })
                print(f"  → Успешно: {safe_title}")

            except Exception as e:
                print(f"  → Ошибка: {e}")

        browser.close()

    return results


def update_laws():
    """Парсит законы.txt → data/laws.txt"""
    links = parse_links(DATA_DIR / "законы.txt")
    if not links:
        return

    print("\n=== Обновление законов ===")
    data = parse_forum_content(links, mode="laws")

    output = []
    for item in data:
        output.append(
            f"НАЗВАНИЕ: {item['title']}\n"
            f"ИСТОЧНИК: {item['url']}\n"
            f"{'='*70}\n\n"
            f"{item['text']}\n"
        )

    laws_path = DATA_DIR / "laws.txt"
    with open(laws_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(output))

    print(f"\nГотово! Законы сохранены в {laws_path}")
    print(f"Всего документов: {len(data)}")


def update_ranks():
    links = parse_links(DATA_DIR / "критерии.txt")
    if not links:
        print("Нет ссылок в критерии.txt")
        return

    print("=== Обновление критериев ===")
    ranks = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for idx, url in enumerate(links, 1):
            try:
                print(f"[{idx}/{len(links)}] {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                # --- Пытаемся взять название из хлебных крошек ---
                faction_name = None
                breadcrumbs = page.query_selector_all(".p-breadcrumbs a, .breadcrumb a, nav a")
                crumb_texts = [clean_text(b.inner_text()) for b in breadcrumbs if b.inner_text().strip()]

                # Ищем ключевые слова в крошках
                for crumb in reversed(crumb_texts):
                    lower = crumb.lower()
                    if "дпс" in lower or "дорожно-патрульная" in lower:
                        faction_name = "ДПС"
                        break
                    if "гибдд" in lower or "угибдд" in lower:
                        faction_name = "УГИБДД"
                        break
                    if "полиц" in lower:
                        faction_name = "Полиция"
                        break

                # Если не нашли — берём из заголовка
                if not faction_name:
                    title_elem = page.query_selector("h1.p-title-value")
                    raw_title = clean_text(title_elem.inner_text()) if title_elem else f"Фракция_{idx}"
                    if "дпс" in raw_title.lower():
                        faction_name = "ДПС"
                    elif "угибдд" in raw_title.lower() or "гибдд" in raw_title.lower():
                        faction_name = "УГИБДД"
                    else:
                        faction_name = "УГИБДД"  # по умолчанию для этой темы

                posts = page.query_selector_all(".bbWrapper")
                raw_text = "\n\n".join(p.inner_text().strip() for p in posts)

                departments = parse_ranks_from_text(raw_text)

                ranks[faction_name] = {
                    "source": url,
                    "departments": departments or {},
                    "raw_text": raw_text[:500] + "..."  # для отладки
                }
                print(f"  → Фракция: {faction_name}")
                print(f"  → Отделы: {list(departments.keys()) if departments else 'не найдены'}")

            except Exception as e:
                print(f"  → Ошибка: {e}")

        browser.close()

    with open(DATA_DIR / "ranks.json", "w", encoding="utf-8") as f:
        json.dump(ranks, f, ensure_ascii=False, indent=2)

    print(f"\nГотово! Фракций: {len(ranks)}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Парсер форума Amazing Online")
    parser.add_argument("--laws", action="store_true", help="Обновить laws.txt")
    parser.add_argument("--ranks", action="store_true", help="Обновить ranks.json")
    parser.add_argument("--all", action="store_true", help="Обновить всё")

    args = parser.parse_args()

    if args.all or args.laws:
        update_laws()
    if args.all or args.ranks:
        update_ranks()

    if not any([args.laws, args.ranks, args.all]):
        print("Укажи что обновлять: --laws / --ranks / --all")