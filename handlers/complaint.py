"""
handlers/complaint.py — написание жалобы / административного иска
Только текст + фото (без видео)
"""

import asyncio
import logging
import random

import aiohttp
from vkbottle.bot import Message
from vkbottle import Keyboard, KeyboardButtonColor, Text
from google import genai
from google.genai import types

from config import GEMINI_API_KEY, MODEL_FALLBACKS
from handlers.lawyer import ensure_laws

logger = logging.getLogger("complaint")

client = genai.Client(api_key=GEMINI_API_KEY)
user_states: dict[int, dict] = {}


# ========== КЛАВИАТУРЫ ==========
def cancel_kb():
    kb = Keyboard(one_time=True)
    kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def result_kb():
    kb = Keyboard(one_time=True)
    kb.add(Text("Готово"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("Потенциальные контраргументы"), color=KeyboardButtonColor.SECONDARY)
    kb.row()
    kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def clear_state(uid: int):
    user_states.pop(uid, None)


# ========== ПРОМПТЫ ==========
SYSTEM_COMPLAINT = """Ты — юрист RP-проекта Amazing Online.
Твоя задача: на основе скриншотов и описания ситуации подготовить материалы для жалобы или административного искового заявления.

Правила:
1. Используй ИСКЛЮЧИТЕЛЬНО нормы из laws.txt. Не ссылайся на реальное законодательство РФ, если его нет в базе.
2. Каждое утверждение о нарушении подкрепляй точной ссылкой на акт/статью из laws.txt.
3. Не задавай уточняющих вопросов. Если данных мало — укажи допущения отдельным блоком.
4. Стиль: официальный, грамотный, без разговорных оборотов.
5. Никогда не используй символы ** и *.

Структура ответа (строго):

1. ОПИСАНИЕ СИТУАЦИИ
(связный официальный текст: что произошло, кто участники, какие действия зафиксированы на материалах)

2. КВАЛИФИКАЦИЯ НАРУШЕНИЙ
(по пунктам: какое действие / какая норма / почему нарушено)

3. ТРЕБОВАНИЯ
(что просим: признать незаконным, привлечь к ответственности, восстановить права и т.д.)

4. ПЕРЕЧЕНЬ ДОКАЗАТЕЛЬСТВ
(кратко: скриншоты, что на них видно; если материалов нет — указать, что анализ проведён по тексту заявителя)
"""

SYSTEM_SPEECH = """Ты — юрист RP-проекта Amazing Online.
На основе уже подготовленного описания ситуации и квалификации нарушений составь краткую устную речь (1–2 минуты) для выступления при подаче жалобы/в суде.

Правила:
- Только нормы из laws.txt.
- Официальный тон, без ** и *.
- Структура: обращение → факты → нарушения со ссылками → просьба.
"""

SYSTEM_COUNTER = """Ты — юрист RP-проекта Amazing Online.
Задача: смоделировать возможные контраргументы стороны ответчика и дать на них опровержения.

ВАЖНО в начале ответа обязательно напиши:
«Внимание: информация носит вероятностный характер и может не соответствовать реальной позиции ответчика.»

Правила:
- Только нормы из laws.txt.
- Без ** и *.
- Формат:
1) Возможный контраргумент
   Опровержение: ...
2) ...
"""


# ========== МЕДИА (только фото) ==========
async def download_attachment(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} при скачивании вложения")
            return await resp.read()


async def collect_media_parts(message: Message) -> list:
    """Только фото. Видео и документы игнорируются."""
    parts = []
    if not message.attachments:
        return parts

    for att in message.attachments:
        if not att.photo:
            continue
        try:
            photo = max(att.photo.sizes, key=lambda s: s.width * s.height)
            data = await download_attachment(photo.url)
            parts.append(types.Part.from_bytes(data=data, mime_type="image/jpeg"))
            logger.info(f"Фото добавлено: {len(data)} bytes")
        except Exception as e:
            logger.warning(f"Фото не скачалось: {e}")

    return parts


# ========== GEMINI ==========
async def generate_with_retry(parts, system_instruction: str, max_tokens: int = 8192) -> str:
    last_error = None
    for model in MODEL_FALLBACKS:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.1,
                        max_output_tokens=max_tokens,
                    ),
                )
                if response.text:
                    return response.text
                raise ValueError("Пустой ответ модели")
            except Exception as e:
                last_error = e
                err = str(e)
                logger.warning(f"{model} attempt={attempt + 1}: {err[:180]}")
                retryable = any(
                    x in err
                    for x in (
                        "503", "UNAVAILABLE", "429", "high demand",
                        "SSL", "EOF", "10054", "ConnectError", "timeout", "Timeout",
                    )
                )
                if retryable and attempt < 2:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.5, 1.5))
                    continue
                break
    raise last_error or Exception("Все модели недоступны")


async def send_long(message: Message, text: str):
    if len(text) <= 3900:
        await message.answer(text)
        return
    for i in range(0, len(text), 3900):
        await message.answer(text[i:i + 3900])
        await asyncio.sleep(0.35)


# ========== ОБРАБОТКА ==========
async def _process_complaint(reply_to: Message, media_msg: Message, description: str, state: dict):
    uid = reply_to.from_id
    await reply_to.answer("⏳ Анализирую материалы и готовлю текст жалобы...")

    try:
        laws_ref = await ensure_laws()
        if laws_ref is None:
            await reply_to.answer("❌ Не удалось загрузить laws.txt")
            clear_state(uid)
            return

        parts = [types.Part.from_uri(file_uri=laws_ref.uri, mime_type="text/plain")]
        parts.append(types.Part.from_text(text=f"Описание ситуации от заявителя:\n{description}"))

        media_parts = await collect_media_parts(media_msg)
        if media_parts:
            logger.info(f"Фото: {len(media_parts)}")
        else:
            logger.info("Медиа нет — анализ только по тексту")
        parts.extend(media_parts)

        complaint_text = await generate_with_retry(parts, SYSTEM_COMPLAINT)
        state["complaint_text"] = complaint_text
        await send_long(reply_to, "📋 Текст для жалобы / иска:\n\n" + complaint_text)

        await reply_to.answer("⏳ Готовлю возможную речь...")
        speech_parts = [
            types.Part.from_uri(file_uri=laws_ref.uri, mime_type="text/plain"),
            types.Part.from_text(text=complaint_text),
        ]
        speech_text = await generate_with_retry(speech_parts, SYSTEM_SPEECH, max_tokens=4096)
        state["speech_text"] = speech_text
        await send_long(reply_to, "🎤 Возможная речь с аргументами:\n\n" + speech_text)

        state["step"] = "result"
        await reply_to.answer("Выберите действие:", keyboard=result_kb())

    except Exception as e:
        logger.exception("Ошибка обработки жалобы")
        err = str(e)
        clear_state(uid)
        if any(x in err for x in ("503", "UNAVAILABLE", "high demand")):
            await reply_to.answer("❌ Модели перегружены. Попробуйте через 1–2 минуты.")
        elif any(x in err for x in ("SSL", "EOF", "10054", "ConnectError")):
            await reply_to.answer("❌ Сбой сети при обращении к Gemini. Попробуйте ещё раз.")
        else:
            await reply_to.answer(f"❌ Ошибка: {err[:350]}")


# ========== ГЛАВНЫЙ ОБРАБОТЧИК ==========
async def handle_complaint(message: Message, has_access_func) -> bool:
    uid = message.from_id
    text = (message.text or "").strip()

    if not has_access_func(uid):
        return False

    if text == "Отмена":
        if uid in user_states:
            clear_state(uid)
            await message.answer("Составление жалобы отменено.")
            return True
        return False

    state = user_states.get(uid)

    if text in ("Написание жалобы/иска", "/complaint", "/жалоба", "/иск"):
        user_states[uid] = {"step": "waiting_materials"}
        await message.answer(
            "Опишите ситуацию и основные претензии.\n\n"
            "Желательно приложить скриншоты — так разбор будет точнее.\n"
            "Можно прислать только текст, без вложений.\n\n"
            "Отмена — сбросить:",
            keyboard=cancel_kb(),
        )
        return True

    if not state:
        return False

    step = state.get("step")

    if step == "waiting_materials":
        has_media = bool(message.attachments)
        has_text = bool(text) and text not in ("Отмена",)

        if not has_media and not has_text:
            await message.answer(
                "Пришлите описание ситуации и/или скриншоты.",
                keyboard=cancel_kb(),
            )
            return True

        if has_media and not has_text:
            state["pending_media_msg"] = message
            state["step"] = "waiting_description"
            await message.answer(
                "Скриншоты получены. Теперь напишите краткое описание ситуации и претензии:",
                keyboard=cancel_kb(),
            )
            return True

        await _process_complaint(message, message, text, state)
        return True

    if step == "waiting_description":
        if not text:
            await message.answer("Напишите описание ситуации:", keyboard=cancel_kb())
            return True
        media_msg = state.pop("pending_media_msg", message)
        await _process_complaint(message, media_msg, text, state)
        return True

    if step == "result":
        if text == "Готово":
            clear_state(uid)
            await message.answer("Готово. Можете начать новую жалобу или задать вопрос юристу.")
            return True

        if text == "Потенциальные контраргументы":
            await message.answer("⏳ Разбираю возможные контраргументы...")
            try:
                laws_ref = await ensure_laws()
                if laws_ref is None:
                    await message.answer("❌ Не удалось загрузить laws.txt", keyboard=result_kb())
                    return True

                parts = [types.Part.from_uri(file_uri=laws_ref.uri, mime_type="text/plain")]
                context = (
                    state.get("complaint_text", "")
                    + "\n\n"
                    + state.get("speech_text", "")
                )
                parts.append(types.Part.from_text(text=context))
                answer = await generate_with_retry(parts, SYSTEM_COUNTER, max_tokens=4096)
                await send_long(message, answer)
                await message.answer(
                    "Можете нажать «Готово» или запросить контраргументы ещё раз.",
                    keyboard=result_kb(),
                )
            except Exception as e:
                logger.exception("Ошибка контраргументов")
                await message.answer(f"❌ {str(e)[:300]}", keyboard=result_kb())
            return True

        return True

    return False