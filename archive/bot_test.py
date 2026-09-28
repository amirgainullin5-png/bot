"""
bot.py — юрист Amazing Online
Версия с защитой от злоупотреблений + режимы standard / fast
"""

import os
import json
import asyncio
import logging
import random
import time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

from dotenv import load_dotenv
from vkbottle.bot import Bot, Message
from vkbottle import Keyboard, KeyboardButtonColor, Text
from google import genai
from google.genai import types

load_dotenv()

# ========== НАСТРОЙКИ ==========
VK_TOKEN = os.getenv("VK_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_IDS = set(map(int, os.getenv("ADMIN_IDS", "572460798").split(",")))

WHITELIST_FILE = Path("../whitelist.json")
USER_MODES_FILE = Path("../user_modes.json")
LAWS_FILE = Path("laws.txt")

# Лимиты защиты
MAX_TEXT_LENGTH = 3500          # символов
MAX_PHOTOS = 3                  # фото за раз
MAX_PHOTO_SIZE_MB = 8           # МБ
RATE_LIMIT_SECONDS = 12         # минимум секунд между запросами одного пользователя
RATE_LIMIT_BURST = 3            # сколько можно подряд, потом жёсткий лимит

MODEL_FALLBACKS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]

SYSTEM_INSTRUCTION_STANDARD = """Ты — главный юридический эксперт, судья и аналитик нормативно-правовой базы RP-проекта Amazing Online.
Твоя задача — давать исчерпывающие, юридически точные, окончательные разборы любых спорных ситуаций, жалоб, действий игроков и сотрудников госслужб.

ПРИНЦИПЫ:
1. Используй ИСКЛЮЧИТЕЛЬНО акты из файла laws.txt. Запрещено ссылаться на реальное законодательство РФ, если статья не прописана в laws.txt.
2. Каждое утверждение подкрепляй точной ссылкой на статью/пункт.
3. Не задавай уточняющих вопросов. Делай разумные допущения и указывай их.
4. При скриншотах сначала распознай весь текст.
5. Иерархия: Конституция > ФЗ > Постановления > Уставы.

СТРУКТУРА ОТВЕТА (строго):
📋 **1. Суть ситуации и факты:**
⚖️ **2. Нормативно-правовая база:**
🔍 **3. Анализ правомерности и нарушений:**
👨‍⚖️ **4. Вердикт и рекомендации:**
"""

SYSTEM_INSTRUCTION_FAST = """Ты — быстрый юридический помощник RP-проекта Amazing Online.
Режим: FAST (экстренный).

Правила:
- Отвечай максимально кратко и по делу.
- Используй ТОЛЬКО laws.txt.
- Формат ответа строго такой:

⚡ **Статья:** (номер + название)
📜 **Цитата:** (самая важная часть статьи, 1-3 предложения)
✅ **Что делать:** (2-4 коротких пункта действий)

Никаких длинных анализов, никаких вступлений. Только суть для ситуации «здесь и сейчас».
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("lawyer_bot")

bot = Bot(token=VK_TOKEN)
client = genai.Client(api_key=GEMINI_API_KEY)

laws_file_ref = None
laws_uploaded_at = None

# Rate-limit: user_id → список timestamp последних запросов
user_last_requests: dict[int, list[float]] = defaultdict(list)

# ========== ФАЙЛЫ ==========
def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(path: Path, data):
    # Атомарная запись
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

WHITELIST: set[int] = set(load_json(WHITELIST_FILE, [572460798]))
USER_MODES: dict[str, str] = load_json(USER_MODES_FILE, {})  # str(uid) → "standard" | "fast"

def save_whitelist():
    save_json(WHITELIST_FILE, list(WHITELIST))

def save_user_modes():
    save_json(USER_MODES_FILE, USER_MODES)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def has_access(uid: int) -> bool:
    return uid in WHITELIST or uid in ADMIN_IDS

def get_user_mode(uid: int) -> str:
    return USER_MODES.get(str(uid), "standard")

def set_user_mode(uid: int, mode: str):
    USER_MODES[str(uid)] = mode
    save_user_modes()

# ========== ЗАЩИТА ==========
def check_rate_limit(uid: int) -> tuple[bool, str]:
    now = time.time()
    times = user_last_requests[uid]
    # Чистим старые
    times[:] = [t for t in times if now - t < 60]

    if len(times) >= RATE_LIMIT_BURST:
        oldest = min(times)
        wait = RATE_LIMIT_SECONDS - (now - oldest)
        if wait > 0:
            return False, f"⏳ Слишком часто. Подожди {wait:.0f} сек."

    times.append(now)
    return True, ""

# ========== LAWS ==========
async def upload_laws():
    global laws_file_ref, laws_uploaded_at
    if not LAWS_FILE.exists():
        raise FileNotFoundError("laws.txt не найден!")
    logger.info("Загружаю laws.txt...")
    file = client.files.upload(file=str(LAWS_FILE), config={"display_name": "Amazing Online Laws"})
    while getattr(file.state, "name", None) == "PROCESSING":
        await asyncio.sleep(2)
        file = client.files.get(name=file.name)
    if getattr(file.state, "name", None) == "FAILED":
        raise RuntimeError(f"Ошибка файла: {file.state}")
    laws_file_ref = file
    laws_uploaded_at = datetime.now()
    logger.info(f"Laws загружены: {file.name}")
    return file

async def ensure_laws_file():
    global laws_file_ref, laws_uploaded_at
    if laws_file_ref is None or laws_uploaded_at is None:
        await upload_laws()
        return
    if datetime.now() - laws_uploaded_at > timedelta(hours=47):
        await upload_laws()

# ========== ГЕНЕРАЦИЯ ==========
async def generate_with_retry(parts, system_instruction: str, status_message=None, max_retries=4) -> str:
    last_error = None
    for model in MODEL_FALLBACKS:
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.05 if "FAST" not in system_instruction else 0.1,
                        max_output_tokens=8192 if "FAST" not in system_instruction else 2048,
                    )
                )
                if response.text:
                    return response.text
                raise ValueError("Пустой ответ")
            except Exception as e:
                last_error = e
                err = str(e)
                logger.warning(f"{model}: {err[:160]}")
                if any(x in err for x in ["503", "UNAVAILABLE", "429", "high demand"]) and attempt < max_retries - 1:
                    wait = (2 ** attempt) + random.uniform(0.3, 1.0)
                    if status_message:
                        try:
                            await status_message.answer(f"⏳ Перегружено, жду {wait:.0f} сек...")
                        except Exception:
                            pass
                    await asyncio.sleep(wait)
                    continue
                break
    raise last_error or Exception("Все модели недоступны")

# ========== КЛАВИАТУРЫ ==========
def mode_keyboard():
    kb = Keyboard(one_time=True, inline=False)
    kb.add(Text("📘 Стандартный режим"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("⚡ Быстрый режим"), color=KeyboardButtonColor.POSITIVE)
    return kb.get_json()

def main_keyboard():
    kb = Keyboard(one_time=False, inline=False)
    kb.add(Text("Установить режим"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()

# ========== ОБРАБОТЧИК ==========
@bot.on.message()
async def universal_handler(message: Message):
    uid = message.from_id
    text = (message.text or "").strip()

    # ---------- КОМАНДЫ ----------
    if text.startswith(("/", "!")):
        parts = text[1:].split()
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("start", "help", "помощь", "начать"):
            if not has_access(uid):
                return
            mode = get_user_mode(uid)
            await message.answer(
                f"⚖️ **Юрист Amazing Online**\n\n"
                f"Текущий режим: **{mode}**\n\n"
                f"Пришли ситуацию текстом или скриншотом.\n"
                f"Можно начать сообщение словами «фаст» или «стандарт» — режим применится только к этому запросу.\n\n"
                f"Команды:\n"
                f"• /mode standard | fast\n"
                f"• /add_id <id>  (админ)\n"
                f"• /del_id <id>  (админ)\n"
                f"• /list  (админ)\n"
                f"• /reload_laws  (админ)",
                keyboard=main_keyboard()
            )
            return

        if cmd == "mode":
            if not has_access(uid):
                return
            if not args or args[0].lower() not in ("standard", "fast", "стандарт", "быстрый"):
                await message.answer("Использование: /mode standard   или   /mode fast")
                return
            new_mode = "fast" if args[0].lower() in ("fast", "быстрый") else "standard"
            set_user_mode(uid, new_mode)
            await message.answer(f"✅ Режим установлен: **{new_mode}**", keyboard=main_keyboard())
            return

        # Админские команды
        if not is_admin(uid):
            return

        if cmd == "list":
            ids = "\n".join(map(str, sorted(WHITELIST))) or "пусто"
            await message.answer(f"Белый список ({len(WHITELIST)}):\n{ids}")
            return

        if cmd in ("add_id", "add"):
            if not args:
                await message.answer("Использование: /add_id 123456789")
                return
            try:
                new_id = int(args[0])
                if new_id in WHITELIST:
                    await message.answer(f"ℹ️ {new_id} уже в списке")
                else:
                    WHITELIST.add(new_id)
                    save_whitelist()
                    await message.answer(f"✅ {new_id} добавлен\nВсего: {len(WHITELIST)}")
            except ValueError:
                await message.answer("❌ Нужно число")
            return

        if cmd in ("del_id", "remove_id", "delete_id", "del", "remove"):
            if not args:
                await message.answer("Использование: /del_id 123456789")
                return
            try:
                del_id = int(args[0])
                if del_id not in WHITELIST:
                    await message.answer(f"ℹ️ {del_id} нет в списке")
                else:
                    WHITELIST.discard(del_id)
                    save_whitelist()
                    await message.answer(f"✅ {del_id} удалён\nОсталось: {len(WHITELIST)}")
            except ValueError:
                await message.answer("❌ Нужно число")
            return

        if cmd == "reload_laws":
            await message.answer("⏳ Перезагружаю laws.txt...")
            try:
                await upload_laws()
                await message.answer("✅ Готово")
            except Exception as e:
                await message.answer(f"❌ {e}")
            return

        return  # неизвестная команда

    # ---------- КНОПКИ РЕЖИМА ----------
    if text == "Установить режим":
        if not has_access(uid):
            return
        await message.answer("Выбери режим:", keyboard=mode_keyboard())
        return

    if text == "📘 Стандартный режим":
        if not has_access(uid):
            return
        set_user_mode(uid, "standard")
        await message.answer("✅ Установлен **стандартный** режим (полный разбор)", keyboard=main_keyboard())
        return

    if text == "⚡ Быстрый режим":
        if not has_access(uid):
            return
        set_user_mode(uid, "fast")
        await message.answer("✅ Установлен **быстрый** режим (краткий ответ)", keyboard=main_keyboard())
        return

    # ---------- ОБЫЧНЫЕ СООБЩЕНИЯ ----------
    if not has_access(uid):
        return

    # Rate-limit
    ok, msg = check_rate_limit(uid)
    if not ok:
        await message.answer(msg)
        return

    # Определяем режим для этого запроса
    mode = get_user_mode(uid)
    clean_text = text

    lower = text.lower()
    if lower.startswith("фаст ") or lower.startswith("fast "):
        mode = "fast"
        clean_text = text.split(" ", 1)[1] if " " in text else ""
    elif lower.startswith("стандарт ") or lower.startswith("standard "):
        mode = "standard"
        clean_text = text.split(" ", 1)[1] if " " in text else ""

    # Защита длины
    if len(clean_text) > MAX_TEXT_LENGTH:
        await message.answer(f"❌ Слишком длинное сообщение (макс. {MAX_TEXT_LENGTH} символов)")
        return

    status = await message.answer(f"⏳ Анализирую ({mode})...")

    try:
        await ensure_laws_file()

        parts = [
            types.Part.from_uri(file_uri=laws_file_ref.uri, mime_type="text/plain")
        ]

        if clean_text:
            parts.append(types.Part.from_text(text=clean_text))

        # Фото
        photo_count = 0
        if message.attachments:
            import aiohttp
            for att in message.attachments:
                if att.photo and photo_count < MAX_PHOTOS:
                    photo = max(att.photo.sizes, key=lambda s: s.width * s.height)
                    async with aiohttp.ClientSession() as session:
                        async with session.get(photo.url) as resp:
                            if int(resp.headers.get("Content-Length", 0)) > MAX_PHOTO_SIZE_MB * 1024 * 1024:
                                continue
                            img_bytes = await resp.read()
                    parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
                    photo_count += 1

        if len(parts) == 1 and not clean_text:
            await message.answer("Пришли текст или скриншот")
            return

        system_prompt = SYSTEM_INSTRUCTION_FAST if mode == "fast" else SYSTEM_INSTRUCTION_STANDARD
        answer = await generate_with_retry(parts, system_prompt, status_message=message)

        # Разбивка
        if len(answer) > 3900:
            for i in range(0, len(answer), 3900):
                await message.answer(answer[i:i+3900])
                await asyncio.sleep(0.3)
        else:
            await message.answer(answer)

    except Exception as e:
        logger.exception("Ошибка")
        err = str(e)
        if any(x in err for x in ["503", "UNAVAILABLE", "high demand"]):
            await message.answer("❌ Модели перегружены. Попробуй позже.")
        else:
            await message.answer(f"❌ {err[:350]}")

# ========== ЗАПУСК ==========
async def main():
    logger.info("Бот запущен (защищённая версия + режимы)")
    logger.info(f"Админы: {ADMIN_IDS}")
    try:
        await ensure_laws_file()
        logger.info("laws.txt готов")
    except Exception as e:
        logger.error(f"laws.txt: {e}")

    await bot.run_polling()

if __name__ == "__main__":
    asyncio.run(main())