import asyncio
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path

from vkbottle.bot import Message
from google import genai
from google.genai import types

from config import GEMINI_API_KEY, LAWS_FILE, MODEL_FALLBACKS

logger = logging.getLogger("lawyer")

client = genai.Client(api_key=GEMINI_API_KEY)

laws_file_ref = None
laws_uploaded_at = None

SYSTEM_INSTRUCTION_STANDARD = """Ты — главный юридический эксперт, судья и аналитик нормативно-правовой базы RP-проекта Amazing Online.
Твоя задача — давать исчерпывающие, юридически точные, окончательные разборы любых спорных ситуаций.

ПРИНЦИПЫ:
1. Используй ИСКЛЮЧИТЕЛЬНО акты из файла laws.txt.
2. Каждое утверждение подкрепляй точной ссылкой на статью.
3. Не задавай уточняющих вопросов.
4. При скриншотах сначала распознай весь текст.
5. Иерархия: Конституция > ФЗ > Постановления > Уставы.

ВАЖНО: Никогда не используй символы ** и *.

СТРУКТУРА ОТВЕТА:
📋 1. Суть ситуации и факты:
⚖️ 2. Нормативно-правовая база:
🔍 3. Анализ правомерности и нарушений:
👨‍⚖️ 4. Вердикт и рекомендации:
"""

SYSTEM_INSTRUCTION_FAST = """Ты — быстрый юридический помощник RP-проекта Amazing Online.
Режим: FAST.

Правила:
- Отвечай максимально кратко.
- Используй ТОЛЬКО laws.txt.
- Никогда не используй символы ** и *.

Формат:
⚡ Статья: (номер + название)
📜 Цитата: (1-3 предложения)
✅ Что делать:
1. ...
2. ...
"""

async def upload_laws():
    global laws_file_ref, laws_uploaded_at
    if not LAWS_FILE.exists():
        raise FileNotFoundError("laws.txt не найден")
    logger.info("Загружаю laws.txt...")
    file = client.files.upload(file=str(LAWS_FILE), config={"display_name": "Amazing Online Laws"})
    while getattr(file.state, "name", None) == "PROCESSING":
        await asyncio.sleep(2)
        file = client.files.get(name=file.name)
    if getattr(file.state, "name", None) == "FAILED":
        raise RuntimeError("Ошибка загрузки laws.txt")
    laws_file_ref = file
    laws_uploaded_at = datetime.now()
    logger.info(f"Laws загружены: {file.name}")

async def ensure_laws():
    global laws_file_ref, laws_uploaded_at
    if laws_file_ref is None or laws_uploaded_at is None:
        await upload_laws()
    elif datetime.now() - laws_uploaded_at > timedelta(hours=47):
        await upload_laws()
    return laws_file_ref

async def generate_with_retry(parts, system_instruction: str, mode: str = "standard", max_retries=4) -> str:
    if mode == "fast":
        models = [
            "gemini-3.5-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
            "gemini-3.8-flash",
        ]
    else:
        models = MODEL_FALLBACKS

    last_error = None
    for model in models:
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.05 if mode == "standard" else 0.1,
                        max_output_tokens=8192 if mode == "standard" else 2048,
                    )
                )
                if response.text:
                    return response.text
                raise ValueError("Пустой ответ")
            except Exception as e:
                last_error = e
                err = str(e)
                logger.warning(f"{model}: {err[:150]}")
                if any(x in err for x in ["503", "UNAVAILABLE", "429", "high demand"]) and attempt < max_retries - 1:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.3, 1.0))
                    continue
                break
    raise last_error or Exception("Все модели недоступны")

async def handle_lawyer(message: Message, mode: str = "standard") -> bool:
    """Обрабатывает юридический запрос. Возвращает True если обработал."""
    text = (message.text or "").strip()
    if not text and not message.attachments:
        return False

    # Убираем префиксы режима
    clean_text = text
    lower = text.lower()
    if lower.startswith(("фаст ", "fast ")):
        mode = "fast"
        clean_text = text.split(" ", 1)[1] if " " in text else ""
    elif lower.startswith(("стандарт ", "standard ")):
        mode = "standard"
        clean_text = text.split(" ", 1)[1] if " " in text else ""

    status = await message.answer(f"⏳ Анализирую ({mode})...")

    try:
        await ensure_laws()

        parts = [types.Part.from_uri(file_uri=laws_file_ref.uri, mime_type="text/plain")]
        if clean_text:
            parts.append(types.Part.from_text(text=clean_text))

        if message.attachments:
            import aiohttp
            for att in message.attachments:
                if att.photo:
                    photo = max(att.photo.sizes, key=lambda s: s.width * s.height)
                    async with aiohttp.ClientSession() as session:
                        async with session.get(photo.url) as resp:
                            img_bytes = await resp.read()
                    parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))

        if len(parts) == 1:
            await message.answer("Пришли текст или скриншот")
            return True

        system_prompt = SYSTEM_INSTRUCTION_FAST if mode == "fast" else SYSTEM_INSTRUCTION_STANDARD
        answer = await generate_with_retry(parts, system_prompt, mode=mode)

        if len(answer) > 3900:
            for i in range(0, len(answer), 3900):
                await message.answer(answer[i:i+3900])
                await asyncio.sleep(0.3)
        else:
            await message.answer(answer)

    except Exception as e:
        logger.exception("Ошибка юриста")
        err = str(e)
        if any(x in err for x in ["503", "UNAVAILABLE", "high demand"]):
            await message.answer("❌ Модели перегружены. Попробуй позже.")
        else:
            await message.answer(f"❌ {err[:350]}")

    return True