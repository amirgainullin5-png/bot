import re
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

def clean_text(text: str) -> str:
    text = re.sub(r"[\n\r\t\xa0]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def parse_links(file_path: Path) -> list[str]:
    if not file_path.exists():
        print(f"Файл {file_path} не найден")
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip().startswith("https://")]

def parse_ranks_from_text(raw_text: str) -> dict:
    """
    Разбирает сырой текст темы критериев в структуру:
    {
      "ОБ ДПС": {
        "2": {"from": "...", "to": "...", "criteria": [...]},
        ...
      },
      "ЦППС": {...},
      ...
    }
    """
    departments = {}

    # Ищем блоки отделов
    # Пример: "Критерии для присвоения очередного специального звания инспекторам ОБ ДПС:"
    dept_pattern = re.compile(
        r"Критерии для присвоения очередного специального звания\s+(.+?):",
        re.IGNORECASE
    )

    # Разбиваем текст на секции по отделам
    parts = dept_pattern.split(raw_text)
    # parts[0] — преамбула, parts[1] — название отдела, parts[2] — текст отдела, parts[3] — следующий отдел и т.д.

    for i in range(1, len(parts), 2):
        dept_name_raw = parts[i].strip()
        dept_text = parts[i + 1] if i + 1 < len(parts) else ""

        # Нормализуем название отдела
        dept_name = dept_name_raw
        dept_name = re.sub(r"инспекторам\s+", "", dept_name, flags=re.IGNORECASE)
        dept_name = re.sub(r"инструкторам\s+", "", dept_name, flags=re.IGNORECASE)
        dept_name = re.sub(r"Курсантам", "Курсанты", dept_name, flags=re.IGNORECASE)
        dept_name = dept_name.strip(" :")

        # Ищем блоки званий внутри отдела
        # [2] Младший сержант полиции » Сержант полиции [3]
        rank_pattern = re.compile(
            r"\[(\d+)\]\s*(.+?)\s*»\s*(.+?)\s*\[(\d+)\]",
            re.IGNORECASE
        )

        ranks = {}
        rank_matches = list(rank_pattern.finditer(dept_text))

        for idx, match in enumerate(rank_matches):
            from_key = match.group(1)
            from_rank = match.group(2).strip()
            to_rank = match.group(3).strip()
            to_key = match.group(4)

            # Текст критериев — от конца текущего матча до начала следующего
            start = match.end()
            end = rank_matches[idx + 1].start() if idx + 1 < len(rank_matches) else len(dept_text)
            criteria_block = dept_text[start:end]

            # Вытаскиваем пункты 1. 2. 3. ...
            criteria = []
            for line in re.split(r"\n|\r", criteria_block):
                line = line.strip()
                m = re.match(r"^\d+\.\s*(.+)", line)
                if m:
                    criteria.append(m.group(1).strip())

            if criteria:
                ranks[from_key] = {
                    "from": from_rank,
                    "to": to_rank,
                    "criteria": criteria
                }

        if ranks:
            departments[dept_name] = ranks

    return departments


def update_laws():
    links = parse_links(DATA_DIR / "законы.txt")
    if not links:
        print("Нет ссылок в законы.txt")
        return

    print("=== Обновление законов ===")
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for idx, url in enumerate(links, 1):
            try:
                print(f"[{idx}/{len(links)}] {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)

                title_elem = page.query_selector("h1.p-title-value")
                title = clean_text(title_elem.inner_text()) if title_elem else f"Документ_{idx}"

                posts = page.query_selector_all(".bbWrapper")
                text = "\n\n".join(p.inner_text().strip() for p in posts)

                results.append(f"НАЗВАНИЕ: {title}\nИСТОЧНИК: {url}\n{'='*70}\n\n{text}")
                print(f"  → {title}")
            except Exception as e:
                print(f"  → Ошибка: {e}")
        browser.close()

    with open(DATA_DIR / "laws.txt", "w", encoding="utf-8") as f:
        f.write("\n\n".join(results))
    print(f"\nГотово! Законов: {len(results)}")


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
def update_forms():
    """
    Парсит темы с образцами рапортов из data/форма.txt
    Сохраняет в data/forms.json
    """
    links = parse_links(DATA_DIR / "форма.txt")
    if not links:
        print("Нет ссылок в форма.txt")
        return

    print("=== Обновление форм рапортов ===")
    forms = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for idx, url in enumerate(links, 1):
            try:
                print(f"[{idx}/{len(links)}] {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                # Название темы
                title_elem = page.query_selector("h1.p-title-value")
                title = clean_text(title_elem.inner_text()) if title_elem else f"Форма_{idx}"

                # Хлебные крошки для определения фракции
                breadcrumbs = page.query_selector_all(".p-breadcrumbs a, .breadcrumb a")
                crumb_texts = [clean_text(b.inner_text()) for b in breadcrumbs if b.inner_text().strip()]

                faction = "ДПС"  # по умолчанию
                for crumb in reversed(crumb_texts):
                    lower = crumb.lower()
                    if "дпс" in lower or "дорожно-патрульная" in lower:
                        faction = "ДПС"
                        break
                    if "гибдд" in lower or "угибдд" in lower:
                        faction = "УГИБДД"
                        break

                # Ищем BB-код в спойлерах / code-блоках
                bb_code = None

                # Вариант 1: блок <pre> или .bbCodeBlock
                code_blocks = page.query_selector_all("pre, .bbCodeBlock-content, .bbCodeSpoiler-content pre")
                for block in code_blocks:
                    text = block.inner_text().strip()
                    if "[RIGHT]" in text or "[CENTER]" in text or "РАПОРТ" in text.upper():
                        bb_code = text
                        break

                # Вариант 2: весь текст поста, если есть характерные теги
                if not bb_code:
                    posts = page.query_selector_all(".bbWrapper")
                    full = "\n".join(p.inner_text() for p in posts)
                    # Ищем кусок от [RIGHT] до [non_personal]
                    m = re.search(r"(\[RIGHT\].*?\[/?non_personal\].*)", full, re.DOTALL | re.IGNORECASE)
                    if m:
                        bb_code = m.group(1).strip()

                if not bb_code:
                    print("  → BB-код не найден")
                    continue

                # Определяем тип формы
                is_leadership = any(x in title.lower() for x in [
                    "руководящего", "руководства", "начальств", "командир"
                ])

                if faction not in forms:
                    forms[faction] = {}

                key = "leadership" if is_leadership else "default"
                forms[faction][key] = {
                    "source": url,
                    "title": title,
                    "template": bb_code
                }
                print(f"  → {faction} / {key}")

            except Exception as e:
                print(f"  → Ошибка: {e}")

        browser.close()

    forms_path = DATA_DIR / "forms.json"
    with open(forms_path, "w", encoding="utf-8") as f:
        json.dump(forms, f, ensure_ascii=False, indent=2)

    print(f"\nГотово! Формы сохранены в {forms_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--laws", action="store_true")
    parser.add_argument("--ranks", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--forms", action="store_true", help="Обновить forms.json")

    args = parser.parse_args()

    if args.all or args.laws:
        update_laws()
    if args.all or args.ranks:
        update_ranks()
    if not any([args.laws, args.ranks, args.all]):
        print("Используй: --laws / --ranks / --all --forms")
    if args.all or args.forms:
        update_forms()