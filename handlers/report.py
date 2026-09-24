"""
handlers/report.py — модуль составления рапортов
Ссылки без вложенного SIZE/FONT внутри [URL]
"""

import json
import logging
import re
from datetime import datetime
from urllib.parse import quote, urlparse

from vkbottle.bot import Message
from vkbottle import Keyboard, KeyboardButtonColor, Text

from config import RANKS_FILE, BASE_DIR

logger = logging.getLogger("report")

FORMS_FILE = BASE_DIR / "data" / "forms.json"


def load_ranks() -> dict:
    if RANKS_FILE.exists():
        with open(RANKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_forms() -> dict:
    if FORMS_FILE.exists():
        with open(FORMS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


RANKS = load_ranks()
FORMS = load_forms()
user_states: dict[int, dict] = {}


def reload_ranks():
    global RANKS
    RANKS = load_ranks()


def reload_forms():
    global FORMS
    FORMS = load_forms()


def is_valid_url(text: str) -> bool:
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return False
    try:
        r = urlparse(text)
        return bool(r.scheme and r.netloc)
    except Exception:
        return False


MAX_LABEL_LEN = 40


def safe_label(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_LABEL_LEN:
        return text
    return text[:MAX_LABEL_LEN - 1].rstrip() + "…"


def cancel_kb():
    kb = Keyboard(one_time=True)
    kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def clear_state(uid: int):
    user_states.pop(uid, None)


def make_redirect_link(url: str, text: str) -> str:
    """
    BB-ссылка через редирект форума.
    Без SIZE/FONT внутри URL — меньше шансов, что редактор сломает тег.
    """
    encoded = quote(url, safe="")
    redirect = (
        "https://forum.amazing-online.com/custom_redirect.php"
        f"?target={encoded}&userid=196785"
    )
    return f"[URL='{redirect}']{text}[/URL]"


def is_leadership_position(position: str) -> bool:
    pos = position.lower()
    keywords = [
        "начальник", "заместитель", "зам.", "зам ", "командир",
        "руководитель", "руковод", "лидер", "шеф", "глава",
    ]
    return any(k in pos for k in keywords)


def get_template(faction: str, position: str) -> str | None:
    faction_forms = FORMS.get(faction, {})
    if is_leadership_position(position):
        form = faction_forms.get("leadership") or faction_forms.get("default")
    else:
        form = faction_forms.get("default") or faction_forms.get("leadership")
    if form and isinstance(form, dict):
        return form.get("template")
    return None


# ========== СКЛОНЕНИЕ ==========
ZVAN_GENITIVE = {
    "Рядовой полиции": "Рядового полиции",
    "Младший сержант полиции": "Младшего сержанта полиции",
    "Сержант полиции": "Сержанта полиции",
    "Старший сержант полиции": "Старшего сержанта полиции",
    "Старшина полиции": "Старшины полиции",
    "Прапорщик полиции": "Прапорщика полиции",
    "Старший прапорщик полиции": "Старшего прапорщика полиции",
    "Младший лейтенант полиции": "Младшего лейтенанта полиции",
    "Лейтенант полиции": "Лейтенанта полиции",
    "Старший лейтенант полиции": "Старшего лейтенанта полиции",
    "Капитан полиции": "Капитана полиции",
    "Майор полиции": "Майора полиции",
    "Подполковник полиции": "Подполковника полиции",
    "Полковник полиции": "Полковника полиции",
}


def decline_fio(fio: str) -> str:
    """
    Родительный падеж ФИО.
    Басаев Али Ахматович → Басаева Али Ахматовича
    """
    parts = fio.strip().split()
    if len(parts) < 3:
        return fio

    surname, name, patronymic = parts[0], parts[1], parts[2]

    # Фамилия
    low_s = surname.lower()
    if low_s.endswith(("ов", "ев", "ёв", "ин", "ын")):
        surname_gen = surname + "а"
    elif low_s.endswith(("ова", "ева", "ина", "ына")):
        surname_gen = surname[:-1] + "ой"
    elif low_s.endswith("ский") or low_s.endswith("цкий"):
        surname_gen = surname[:-2] + "ого"
    elif low_s.endswith(("ой", "ый")):
        surname_gen = surname[:-2] + "ого"
    elif low_s.endswith("ая"):
        surname_gen = surname[:-2] + "ой"
    elif surname[-1].lower() in "бвгджзклмнпрстфхцчшщ":
        surname_gen = surname + "а"
    else:
        surname_gen = surname

    # Имя
    name_keep = {
        "али", "омар", "мурат", "руслан", "тимур", "амир",
        "илья", "никита", "данила", "кузьма", "фома", "лука",
        "саша", "паша", "коля", "ваня", "дима", "женя", "вася", "петя",
    }
    low_n = name.lower()
    if low_n in name_keep:
        name_gen = name
    elif name.endswith(("й", "ь")):
        name_gen = name[:-1] + "я"
    elif name.endswith(("а", "я")):
        name_gen = name[:-1] + "и"
    else:
        name_gen = name + "а"

    # Отчество
    if patronymic.endswith(("ович", "евич", "ич")):
        patronymic_gen = patronymic + "а"
    elif patronymic.endswith(("овна", "евна", "ична", "инична")):
        patronymic_gen = patronymic[:-1] + "ы"
    else:
        patronymic_gen = patronymic + "а"

    return f"{surname_gen} {name_gen} {patronymic_gen}"


# ========== ГЕНЕРАЦИЯ BB-КОДА ==========
def generate_bb_code(data: dict) -> str:
    faction = data["faction"]
    department = data["department"]
    rank_key = data["rank_key"]
    rank_info = RANKS[faction]["departments"][department][rank_key]

    fio = data.get("fio", "(ФИО)")
    zvan = rank_info["from"]
    position = data.get("position", "")
    date_from = data.get("date_from", "____")
    date_to = data.get("date_to", "____")
    signature = data.get("signature", "____")
    report_date = data.get("report_date", datetime.now().strftime("%d.%m.%Y"))

    zvan_genitive = ZVAN_GENITIVE.get(zvan, zvan)
    fio_genitive = decline_fio(fio)

    passport_link = make_redirect_link(data.get("passport", "#"), "ксерокопия")
    trudovaya_link = make_redirect_link(data.get("trudovaya", "#"), "ксерокопия")

    work_lines = []
    for i, crit in enumerate(rank_info.get("criteria", []), 1):
        crit_clean = crit.rstrip(" ;.")
        link = data.get(f"work_{i}", "#")
        work_lines.append(
            f"3.{i}. {crit_clean}: {make_redirect_link(link, 'доказательства')}"
        )
    work_block = "\n".join(work_lines)

    # Надёжный шаблон без вложенного FONT внутри URL
    bb = (
        "[RIGHT][SIZE=15px][FONT=Times New Roman]Начальнику УГИБДД по Нижегородской области\n"
        "генерал-лейтенанту полиции[/FONT][/SIZE]\n"
        "[FONT=Times New Roman]Грации Х.А.[/FONT]\n"
        f"[SIZE=15px][FONT=Times New Roman]от {zvan_genitive} {fio_genitive}[/FONT][/SIZE][/RIGHT]\n"
        "\n"
        "[CENTER][SIZE=15px][FONT=Times New Roman][B]Р А П О Р Т[/B][/FONT][/SIZE][/CENTER]\n"
        "\n"
        f"[SIZE=15px][FONT=Times New Roman]Я, {position}, {zvan} {fio}, "
        f"прошу рассмотреть рапорт о проделанной мной работе за период с {date_from} по {date_to}. "
        "К рапорту прилагаю ксерокопии паспорта и трудовой книги, а так же фиксации проделанной работы.\n"
        "\n"
        "Приложения:\n"
        f"1. Ксерокопия паспортных данных: [/FONT][/SIZE]{passport_link}\n"
        f"[SIZE=15px][FONT=Times New Roman]2. Ксерокопия трудовой книги: [/FONT][/SIZE]{trudovaya_link}\n"
        "[SIZE=15px][FONT=Times New Roman]3. Фиксации проделанной работы (по пунктам):\n"
        f"{work_block}[/FONT][/SIZE]\n"
        "\n"
        f"[RIGHT][SIZE=15px][FONT=Times New Roman]Дата: {report_date}\n"
        f"Подпись: [B][I]{signature}[/I][/B][/FONT][/SIZE][/RIGHT]\n"
        "[FONT=Times New Roman][non_personal][/non_personal][/FONT]"
    )
    return bb


# ========== ЭКРАН ПРОВЕРКИ ==========
async def show_review(message: Message, state: dict):
    faction = state["faction"]
    department = state["department"]
    rank_key = state["rank_key"]
    rank_info = RANKS[faction]["departments"][department][rank_key]

    lines = [
        "📋 Проверьте данные:\n",
        f"Фракция: {faction}",
        f"Отдел: {department}",
        f"Звание: {rank_info['from']}",
        f"Должность: {state.get('position')}",
        f"ФИО: {state.get('fio')}",
        f"Период: {state.get('date_from')} — {state.get('date_to')}",
        f"Паспорт: {state.get('passport')}",
        f"Трудовая: {state.get('trudovaya')}",
        "\nПункты:",
    ]
    for i, crit in enumerate(rank_info.get("criteria", []), 1):
        lines.append(f"{i}. {crit}\n   → {state.get(f'work_{i}', '—')}")
    lines.append(f"\nПодпись: {state.get('signature')}")

    kb = Keyboard(one_time=True)
    kb.add(Text("✏️ Должность"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("✏️ ФИО"), color=KeyboardButtonColor.SECONDARY)
    kb.row()
    kb.add(Text("✏️ Даты"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("✏️ Паспорт"), color=KeyboardButtonColor.SECONDARY)
    kb.row()
    kb.add(Text("✏️ Трудовая"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("✏️ Подпись"), color=KeyboardButtonColor.SECONDARY)
    kb.row()
    for i in range(1, len(rank_info.get("criteria", [])) + 1):
        kb.add(Text(f"✏️ Пункт {i}"), color=KeyboardButtonColor.SECONDARY)
        if i % 2 == 0:
            kb.row()
    kb.row()
    kb.add(Text("✅ Готово"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)

    await message.answer("\n".join(lines), keyboard=kb.get_json())


# ========== ГЛАВНЫЙ ОБРАБОТЧИК ==========
async def handle_report(message: Message, has_access_func) -> bool:
    uid = message.from_id
    text = (message.text or "").strip()

    logger.info(f"[report] uid={uid} text='{text[:80]}' state={user_states.get(uid)}")

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
        reload_ranks()
        reload_forms()
        if not RANKS:
            await message.answer(
                "❌ Критерии не загружены.\n"
                "Сначала: python -m parser.forum_parser --ranks"
            )
            return True

        faction_labels = {}
        kb = Keyboard(one_time=True)
        for faction in RANKS.keys():
            label = safe_label(faction)
            faction_labels[label] = faction
            kb.add(Text(label), color=KeyboardButtonColor.PRIMARY)
            kb.row()
        kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)

        user_states[uid] = {"step": "faction", "faction_labels": faction_labels}
        await message.answer("Выберите фракцию:", keyboard=kb.get_json())
        return True

    if not state:
        return False

    step = state.get("step")

    # Фракция
    if step == "faction":
        faction = state.get("faction_labels", {}).get(text) or (text if text in RANKS else None)
        if not faction:
            await message.answer("Выберите фракцию кнопкой")
            return True
        state["faction"] = faction
        state["step"] = "department"

        deps = list(RANKS[faction].get("departments", {}).keys())
        if not deps:
            await message.answer("❌ У фракции нет отделов")
            clear_state(uid)
            return True

        department_labels = {}
        kb = Keyboard(one_time=True)
        for d in deps:
            label = safe_label(d)
            department_labels[label] = d
            kb.add(Text(label), color=KeyboardButtonColor.PRIMARY)
            kb.row()
        kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
        state["department_labels"] = department_labels
        await message.answer("Выберите отдел:", keyboard=kb.get_json())
        return True

    # Отдел
    if step == "department":
        faction = state["faction"]
        department = state.get("department_labels", {}).get(text) or (
            text if text in RANKS[faction]["departments"] else None
        )
        if not department:
            await message.answer("Выберите отдел кнопкой")
            return True
        state["department"] = department
        state["step"] = "rank"

        ranks = RANKS[faction]["departments"][department]
        kb = Keyboard(one_time=True)
        for key, info in ranks.items():
            kb.add(Text(safe_label(f"[{key}] {info['from']}")), color=KeyboardButtonColor.SECONDARY)
            kb.row()
        kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
        await message.answer("Выберите текущее звание:", keyboard=kb.get_json())
        return True

    # Звание
    if step == "rank":
        faction = state["faction"]
        department = state["department"]
        chosen = None
        for key in RANKS[faction]["departments"][department]:
            if text.startswith(f"[{key}]") or f"[{key}]" in text:
                chosen = key
                break
        if not chosen:
            await message.answer("Выберите звание кнопкой")
            return True
        state["rank_key"] = chosen
        state["step"] = "position"
        await message.answer(
            "Укажите вашу должность.\n"
            "Например: инспектор Отдельного батальона УГИБДД по Нижегородской области",
            keyboard=cancel_kb(),
        )
        return True

    # Должность
    if step == "position":
        state["position"] = text
        state["step"] = "fio"
        await message.answer("Введите ФИО полностью (Фамилия Имя Отчество):", keyboard=cancel_kb())
        return True

    # ФИО
    if step == "fio":
        state["fio"] = text
        state["step"] = "date_from"
        await message.answer("Дата начала периода (ДД.ММ.ГГГГ):", keyboard=cancel_kb())
        return True

    if step == "date_from":
        state["date_from"] = text
        state["step"] = "date_to"
        await message.answer("Дата окончания периода:", keyboard=cancel_kb())
        return True

    if step == "date_to":
        state["date_to"] = text
        state["step"] = "passport"
        await message.answer("Ссылка на паспорт:", keyboard=cancel_kb())
        return True

    if step == "passport":
        if not is_valid_url(text):
            await message.answer("❌ Нужна корректная ссылка (http/https)", keyboard=cancel_kb())
            return True
        state["passport"] = text
        state["step"] = "trudovaya"
        await message.answer("Ссылка на трудовую книгу:", keyboard=cancel_kb())
        return True

    if step == "trudovaya":
        if not is_valid_url(text):
            await message.answer("❌ Нужна корректная ссылка", keyboard=cancel_kb())
            return True
        state["trudovaya"] = text
        state["work_index"] = 0
        state["step"] = "work"

        rank_info = RANKS[state["faction"]]["departments"][state["department"]][state["rank_key"]]
        criteria = rank_info.get("criteria", [])
        if not criteria:
            await message.answer("❌ Нет критериев у этого звания")
            clear_state(uid)
            return True

        await message.answer(
            f"Пункт 1/{len(criteria)}:\n{criteria[0]}\n\nСсылка на доказательства:",
            keyboard=cancel_kb(),
        )
        return True

    if step == "work":
        if not is_valid_url(text):
            await message.answer("❌ Нужна корректная ссылка", keyboard=cancel_kb())
            return True

        idx = state["work_index"]
        state[f"work_{idx + 1}"] = text

        rank_info = RANKS[state["faction"]]["departments"][state["department"]][state["rank_key"]]
        criteria = rank_info.get("criteria", [])
        total = len(criteria)

        if idx + 1 >= total:
            state["step"] = "signature"
            await message.answer("Введите подпись:", keyboard=cancel_kb())
            return True

        state["work_index"] = idx + 1
        await message.answer(
            f"Пункт {idx + 2}/{total}:\n{criteria[idx + 1]}\n\nСсылка на доказательства:",
            keyboard=cancel_kb(),
        )
        return True

    if step == "signature":
        state["signature"] = text
        state["step"] = "review"
        await show_review(message, state)
        return True

    # Review
    if step == "review":
        if text == "✅ Готово":
            state["report_date"] = datetime.now().strftime("%d.%m.%Y")
            bb = generate_bb_code(state)
            clear_state(uid)
            await message.answer(
                "✅ Рапорт готов! Скопируйте BB-код.\n"
                "На форуме вставляйте в режиме [] (BB-код), не в визуальном редакторе."
            )
            if len(bb) > 3900:
                for i in range(0, len(bb), 3900):
                    await message.answer(bb[i:i + 3900])
            else:
                await message.answer(bb)
            return True

        if text == "✏️ Должность":
            state["step"] = "edit_position"
            await message.answer("Новая должность:", keyboard=cancel_kb())
            return True
        if text == "✏️ ФИО":
            state["step"] = "edit_fio"
            await message.answer("Новое ФИО:", keyboard=cancel_kb())
            return True
        if text == "✏️ Даты":
            state["step"] = "edit_date_from"
            await message.answer("Новая дата начала:", keyboard=cancel_kb())
            return True
        if text == "✏️ Паспорт":
            state["step"] = "edit_passport"
            await message.answer("Новая ссылка на паспорт:", keyboard=cancel_kb())
            return True
        if text == "✏️ Трудовая":
            state["step"] = "edit_trudovaya"
            await message.answer("Новая ссылка на трудовую:", keyboard=cancel_kb())
            return True
        if text == "✏️ Подпись":
            state["step"] = "edit_signature"
            await message.answer("Новая подпись:", keyboard=cancel_kb())
            return True
        if text.startswith("✏️ Пункт "):
            try:
                num = int(text.replace("✏️ Пункт ", "").strip())
                state["step"] = f"edit_work_{num}"
                await message.answer(f"Новая ссылка для пункта {num}:", keyboard=cancel_kb())
            except ValueError:
                pass
            return True
        return True

    # Редактирование
    if step == "edit_position":
        state["position"] = text
        state["step"] = "review"
        await show_review(message, state)
        return True
    if step == "edit_fio":
        state["fio"] = text
        state["step"] = "review"
        await show_review(message, state)
        return True
    if step == "edit_date_from":
        state["date_from"] = text
        state["step"] = "edit_date_to"
        await message.answer("Новая дата окончания:", keyboard=cancel_kb())
        return True
    if step == "edit_date_to":
        state["date_to"] = text
        state["step"] = "review"
        await show_review(message, state)
        return True
    if step == "edit_passport":
        if not is_valid_url(text):
            await message.answer("❌ Нужна ссылка", keyboard=cancel_kb())
            return True
        state["passport"] = text
        state["step"] = "review"
        await show_review(message, state)
        return True
    if step == "edit_trudovaya":
        if not is_valid_url(text):
            await message.answer("❌ Нужна ссылка", keyboard=cancel_kb())
            return True
        state["trudovaya"] = text
        state["step"] = "review"
        await show_review(message, state)
        return True
    if step == "edit_signature":
        state["signature"] = text
        state["step"] = "review"
        await show_review(message, state)
        return True
    if step and step.startswith("edit_work_"):
        num = int(step.split("_")[-1])
        if not is_valid_url(text):
            await message.answer("❌ Нужна ссылка", keyboard=cancel_kb())
            return True
        state[f"work_{num}"] = text
        state["step"] = "review"
        await show_review(message, state)
        return True

    return False