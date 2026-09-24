import json
from pathlib import Path
from vkbottle import Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Message
from datetime import datetime
from urllib.parse import quote, urlparse

from config import BASE_DIR

RANKS_FILE = BASE_DIR / "data" / "ranks.json"

def load_ranks() -> dict:
    if RANKS_FILE.exists():
        with open(RANKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

RANKS = load_ranks()  # загружаем при старте

user_states = {}

def is_valid_url(text: str) -> bool:
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return False
    try:
        result = urlparse(text)
        return bool(result.scheme and result.netloc)
    except:
        return False

def cancel_kb():
    kb = Keyboard(one_time=True)
    kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def clear_state(uid: int):
    user_states.pop(uid, None)

# ================== ГЕНЕРАЦИЯ BB-КОДА ==================
def generate_bb_code(data: dict) -> str:
    faction = data["faction"]
    department = data["department"]
    rank_key = data["rank_key"]

    rank_info = RANKS[faction]["departments"][department][rank_key]
    fio = data.get("fio", "(ФИО)")
    zvan = rank_info["from"]

    # Родительный падеж звания
    zvan_genitive_map = {
        "Младший сержант полиции": "Младшего сержанта полиции",
        "Сержант полиции": "Сержанта полиции",
        "Старшина полиции": "Старшины полиции",
        "Прапорщик полиции": "Прапорщика полиции",
        "Младший лейтенант полиции": "Младшего лейтенанта полиции",
        "Лейтенант полиции": "Лейтенанта полиции",
        "Капитан полиции": "Капитана полиции",
    }
    zvan_genitive = zvan_genitive_map.get(zvan, zvan)

    # Простое склонение ФИО
    parts = fio.split()
    if len(parts) >= 3:
        surname, name, patronymic = parts[0], parts[1], parts[2]
        name_gen = name[:-1] + "я" if name.endswith(("й", "ь")) else (name[:-1] + "ы" if name.endswith("а") else name + "а")
        patronymic_gen = patronymic + "а" if patronymic.endswith("ич") else (patronymic[:-1] + "ы" if patronymic.endswith("на") else patronymic + "а")
        fio_genitive = f"{surname} {name_gen} {patronymic_gen}"
    else:
        fio_genitive = fio

    date_from = data.get("date_from", "____")
    date_to = data.get("date_to", "____")
    signature = data.get("signature", "____")
    report_date = data.get("report_date", datetime.now().strftime("%d.%m.%Y"))

    def make_redirect_link(url: str, text: str) -> str:
        encoded = quote(url, safe='')
        redirect = f"https://forum.amazing-online.com/custom_redirect.php?target={encoded}&userid=196785"
        return f"[URL='{redirect}']{text}[/URL]"

    # Должность можно сделать динамической потом
    position = f"инспектор {department} {faction}"

    work_lines = []
    for i, crit in enumerate(rank_info["criteria"], 1):
        link = data.get(f"work_{i}", "#")
        work_lines.append(f"3.{i}. {crit}: {make_redirect_link(link, 'доказательства')}")

    work_block = "\n".join(work_lines)

    bb = f"""[RIGHT][SIZE=15px][FONT=Times New Roman]Начальнику УГИБДД по Нижегородской области
генерал-лейтенанту полиции[/FONT][/SIZE]
[FONT=Times New Roman]Грации Х.А.[/FONT]
[SIZE=15px][FONT=Times New Roman]от {zvan_genitive} {fio_genitive}[/FONT][/SIZE][/RIGHT]

[CENTER][SIZE=15px][FONT=Times New Roman][B]Р А П О Р Т[/B][/FONT][/SIZE][/CENTER]

[SIZE=15px][FONT=Times New Roman]Я, {position}, {zvan} {fio}, прошу рассмотреть рапорт о проделанной мной работе за период с {date_from} по {date_to}. К рапорту прилагаю ксерокопии паспорта и трудовой книги, а так же фиксации проделанной работы.

Приложения:
1. Ксерокопия паспортных данных: {make_redirect_link(data.get('passport', '#'), 'ксерокопия')}
2. Ксерокопия трудовой книги: {make_redirect_link(data.get('trudovaya', '#'), 'ксерокопия')}
3. Фиксации проделанной работы (по пунктам):
{work_block}[/FONT][/SIZE]

[RIGHT][SIZE=15px][FONT=Times New Roman]Дата: {report_date}
Подпись: [B][I]{signature}[/I][/B][/FONT][/SIZE][/RIGHT]
[FONT=Times New Roman][non_personal][/non_personal][/FONT]"""
    return bb

# ================== ОБРАБОТЧИК ==================
async def handle_report(message: Message, has_access_func) -> bool:
    uid = message.from_id
    text = (message.text or "").strip()

    if not has_access_func(uid):
        return False

    if text == "Отмена":
        if uid in user_states:
            clear_state(uid)
            await message.answer("Действие отменено.")
            return True
        return False

    state = user_states.get(uid)

    # Старт
    if text in ("Составление отчета", "/report", "/отчет"):
        if not RANKS:
            await message.answer("Критерии ещё не загружены. Сначала обнови ranks.json")
            return True

        user_states[uid] = {"step": "faction"}
        kb = Keyboard(one_time=True)
        for faction in RANKS.keys():
            kb.add(Text(faction), color=KeyboardButtonColor.PRIMARY)
            kb.row()
        kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
        await message.answer("Выберите фракцию:", keyboard=kb.get_json())
        return True

    if not state:
        return False

    step = state.get("step")

    # 1. Выбор фракции
    if step == "faction":
        if text not in RANKS:
            await message.answer("Выберите фракцию из списка")
            return True
        state["faction"] = text
        state["step"] = "department"

        departments = list(RANKS[text]["departments"].keys())
        kb = Keyboard(one_time=True)
        for dep in departments:
            kb.add(Text(dep), color=KeyboardButtonColor.PRIMARY)
            kb.row()
        kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
        await message.answer("Выберите отдел:", keyboard=kb.get_json())
        return True

    # 2. Выбор отдела
    if step == "department":
        faction = state["faction"]
        if text not in RANKS[faction]["departments"]:
            await message.answer("Выберите отдел из списка")
            return True
        state["department"] = text
        state["step"] = "rank"

        ranks = RANKS[faction]["departments"][text]
        kb = Keyboard(one_time=True)
        for key, info in ranks.items():
            kb.add(Text(f"[{key}] {info['from']}"), color=KeyboardButtonColor.SECONDARY)
            kb.row()
        kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
        await message.answer("Выберите текущее звание:", keyboard=kb.get_json())
        return True

    # 3. Выбор звания
    if step == "rank":
        faction = state["faction"]
        department = state["department"]
        chosen = None
        for key in RANKS[faction]["departments"][department]:
            if text.startswith(f"[{key}]"):
                chosen = key
                break
        if not chosen:
            await message.answer("Выберите звание из списка")
            return True

        state["rank_key"] = chosen
        state["step"] = "fio"
        await message.answer("Введите ФИО полностью:", keyboard=cancel_kb())
        return True

    # Дальше поток тот же (fio → dates → passport → trudovaya → work → signature → review)
    # ... (оставляем как было)

    return False