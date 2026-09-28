"""
handlers/complaint.py — жалоба (кратко) / иск (развёрнуто)
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
from services.gemini_client import generate_content_resilient, acquire_nn_slot, release_nn_slot
from handlers.lawyer import ensure_laws

logger = logging.getLogger("complaint")
user_states: dict[int, dict] = {}


def cancel_kb():
    kb = Keyboard(one_time=True)
    kb.add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def type_kb():
    kb = Keyboard(one_time=True)
    kb.add(Text("📝 Жалоба"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("📜 Иск"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def result_kb_lawsuit():
    """После иска: контраргументы / речь / готово."""
    kb = Keyboard(one_time=True)
    kb.add(Text("🎤 Речь"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("🛡️ Контраргументы"), color=KeyboardButtonColor.SECONDARY)
    kb.row()
    kb.add(Text("✅ Готово"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def result_kb_complaint():
    """После жалобы — только готово."""
    kb = Keyboard(one_time=True)
    kb.add(Text("✅ Готово"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def main_kb():
    kb = Keyboard(one_time=False)
    kb.add(Text("📋 Составление отчета"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("⚖️ Жалоба / иск"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("⚙️ Режим"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("👤 Профиль"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def clear_state(uid: int):
    user_states.pop(uid, None)


SYSTEM_COMPLAINT = """Ты — юрист ролевого проекта.
Задача: подготовить ТОЛЬКО описательную часть КРАТКОЙ жалобы по материалам заявителя.

Строгие ограничения по объёму ответа:
- НЕ пиши шапку (суд, адрес, истец, ответчик, реквизиты сторон).
- НЕ пиши заголовок вида «ЖАЛОБА» / «АДМИНИСТРАТИВНОЕ ИСКОВОЕ ЗАЯВЛЕНИЕ».
- НЕ пиши перечень доказательств, приложений, дату и подпись.
- НЕ пиши блок «ПРОШУ» с нумерованными требованиями к суду (это заполняется по форме).
- Только связный текст описания ситуации, квалификации нарушений и краткой сути претензии.

Правила:
1. Только нормы из laws.txt.
2. Каждое нарушение — со ссылкой на статью.
3. Без уточняющих вопросов. При нехватке данных — короткий блок «Допущения» в начале.
4. Официальный стиль, без ** и *.
5. Текст КОМПАКТНЫЙ, по делу.

Структура ответа (только эти блоки):
1. ОПИСАНИЕ СИТУАЦИИ (кратко, 2–6 предложений)
2. КВАЛИФИКАЦИЯ НАРУШЕНИЙ (пункты: действие — норма — почему)
3. СУТЬ ПРЕТЕНЗИИ (1–3 предложения: что оспаривается и почему это незаконно)
"""

SYSTEM_LAWSUIT = """Ты — юрист ролевого проекта.
Задача: подготовить ТОЛЬКО описательную (мотивировочную) часть РАЗВЁРНУТОГО административного иска.

Строгие ограничения по объёму ответа:
- НЕ пиши шапку (наименование суда, адрес, ФИО/данные истца и ответчиков, телефоны, e-mail).
- НЕ пиши заголовок «АДМИНИСТРАТИВНОЕ ИСКОВОЕ ЗАЯВЛЕНИЕ» и подобное.
- НЕ пиши раздел «ТРЕБОВАНИЯ» / «ПРОШУ СУД» с нумерованными пунктами.
- НЕ пиши перечень доказательств, приложений, дату подачи и подпись.
- Эти части пользователь заполнит сам по форме. Твоя задача — только текст описания и правовой анализ.

Правила:
1. Только нормы из laws.txt.
2. Каждое утверждение — со ссылкой на статью.
3. Без уточняющих вопросов. При нехватке данных — блок «Допущения» в начале.
4. Официальный стиль, без ** и *.
5. Текст подробный, структурированный, но без шаблонной «обвязки» заявления.

Структура ответа (только эти блоки):
1. ОПИСАНИЕ СИТУАЦИИ
2. КВАЛИФИКАЦИЯ НАРУШЕНИЙ (по пунктам с нормами)
3. ПРАВОВОЙ ВЫВОД (кратко: почему оспариваемый акт/действия незаконны; без нумерованного «ПРОШУ СУД»)
"""

SYSTEM_SPEECH = """Ты — юрист ролевого проекта.
По уже готовому тексту составь краткую устную речь (1–2 минуты).
Только norms из laws.txt. Без ** и *.
Структура: обращение → факты → нарушения → просьба.
"""

SYSTEM_COUNTER = """Ты — юрист ролевого проекта.
Смоделируй возможные контраргументы ответчика и опровержения.
В начале: «Внимание: информация носит вероятностный характер…»
Только laws.txt. Без ** и *.
Формат:
1) Контраргумент
   Опровержение: ...
"""


async def download_attachment(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.read()


async def collect_media_parts(message: Message) -> list:
    parts = []
    if not message.attachments:
        return parts
    for att in message.attachments:
        if not att.photo:
            continue
        try:
            photo = max(att.photo.sizes, key=lambda s: s.width * s.height)
            data = await download_attachment(photo.url)
            if len(data) > 8 * 1024 * 1024:
                continue
            parts.append(types.Part.from_bytes(data=data, mime_type="image/jpeg"))
        except Exception as e:
            logger.warning(f"Фото: {e}")
    return parts


async def generate_with_retry(parts, system_instruction: str, max_tokens: int = 8192) -> str:
    await acquire_nn_slot()
    try:
        return await generate_content_resilient(
            parts,
            system_instruction,
            mode="standard",
            max_output_tokens=max_tokens,
            temperature=0.1,
            max_retries=3,
        )
    finally:
        await release_nn_slot()


async def send_long(message: Message, text: str):
    if len(text) <= 3900:
        await message.answer(text)
        return
    for i in range(0, len(text), 3900):
        await message.answer(text[i:i + 3900])
        await asyncio.sleep(0.35)


def _result_kb(state: dict):
    if state.get("doc_type") == "lawsuit":
        return result_kb_lawsuit()
    return result_kb_complaint()


async def _process_doc(reply_to: Message, media_msg: Message, description: str, state: dict):
    uid = reply_to.from_id
    doc_type = state.get("doc_type", "complaint")
    label = "иска" if doc_type == "lawsuit" else "жалобы"
    await reply_to.answer(f"⏳ Готовлю текст {label}...")

    try:
        laws_ref = await ensure_laws()
        if laws_ref is None:
            await reply_to.answer("❌ База норм недоступна.", keyboard=main_kb())
            clear_state(uid)
            return

        parts = [types.Part.from_uri(file_uri=laws_ref.uri, mime_type="text/plain")]
        parts.append(types.Part.from_text(text=f"Описание от заявителя:\n{description}"))
        parts.extend(await collect_media_parts(media_msg))

        system = SYSTEM_LAWSUIT if doc_type == "lawsuit" else SYSTEM_COMPLAINT
        text_out = await generate_with_retry(parts, system)
        state["complaint_text"] = text_out
        state["step"] = "result"

        title = "📜 Описание для иска:" if doc_type == "lawsuit" else "📝 Описание для жалобы:"
        await send_long(reply_to, f"{title}\n\n{text_out}")

        if doc_type == "lawsuit":
            await reply_to.answer(
                "Выберите действие:\n"
                "• 🎤 Речь — устное выступление\n"
                "• 🛡️ Контраргументы — возможные возражения\n"
                "• ✅ Готово — завершить",
                keyboard=result_kb_lawsuit(),
            )
        else:
            await reply_to.answer(
                "Жалоба готова. Нажмите «✅ Готово», когда закончите.",
                keyboard=result_kb_complaint(),
            )
    except Exception as e:
        logger.exception("process doc")
        err = str(e)
        clear_state(uid)
        await reply_to.answer(
            "⏳ Сервис временно недоступен. Попробуйте через несколько минут.",
            keyboard=main_kb(),
        )
        try:
            from config import ADMIN_IDS
            detail = (
                f"⚠️ Ошибка нейросети (жалоба/иск)\n"
                f"user_id={uid}\n"
                f"{err[:1500]}"
            )
            for aid in ADMIN_IDS:
                try:
                    await reply_to.ctx_api.messages.send(
                        user_id=aid, message=detail, random_id=0
                    )
                except Exception as ne:
                    logger.warning(f"notify admin {aid}: {ne}")
        except Exception as ne:
            logger.warning(f"admin notify failed: {ne}")


async def handle_complaint(message: Message, has_access_func) -> bool:
    uid = message.from_id
    text = (message.text or "").strip()

    if not has_access_func(uid):
        return False

    # Отмена
    if text in ("❌ Отмена", "Отмена"):
        if uid in user_states:
            clear_state(uid)
            await message.answer("Составление отменено.", keyboard=main_kb())
            return True
        return False

    state = user_states.get(uid)

    # Старт
    if text in (
        "⚖️ Жалоба / иск",
        "Написание жалобы/иска",
        "/complaint",
        "/жалоба",
        "/иск",
    ):
        user_states[uid] = {"step": "choose_type"}
        await message.answer(
            "Что составляем?\n\n"
            "📝 Жалоба — кратко, по делу\n"
            "📜 Иск — развёрнутое заявление",
            keyboard=type_kb(),
        )
        return True

    if not state:
        return False

    step = state.get("step")

    # Выбор типа
    if step == "choose_type":
        if text == "📝 Жалоба":
            state["doc_type"] = "complaint"
            state["step"] = "waiting_materials"
            await message.answer(
                "Опишите ситуацию и приложите скриншоты (по желанию).\n"
                "Можно только текст.",
                keyboard=cancel_kb(),
            )
            return True
        if text == "📜 Иск":
            state["doc_type"] = "lawsuit"
            state["step"] = "waiting_materials"
            await message.answer(
                "Опишите ситуацию подробно и приложите скриншоты (по желанию).\n"
                "Можно только текст.",
                keyboard=cancel_kb(),
            )
            return True
        # неверная кнопка — вернуть клавиатуру
        await message.answer(
            "Выберите тип документа кнопкой ниже.",
            keyboard=type_kb(),
        )
        return True

    if step == "waiting_materials":
        has_media = bool(message.attachments)
        has_text = bool(text) and text not in ("❌ Отмена", "Отмена")
        if not has_media and not has_text:
            await message.answer(
                "Пришлите описание и/или скриншоты.",
                keyboard=cancel_kb(),
            )
            return True
        if has_media and not has_text:
            state["pending_media_msg"] = message
            state["step"] = "waiting_description"
            await message.answer(
                "Скриншоты получены. Теперь краткое описание:",
                keyboard=cancel_kb(),
            )
            return True
        await _process_doc(message, message, text, state)
        return True

    if step == "waiting_description":
        if not text:
            await message.answer("Напишите описание:", keyboard=cancel_kb())
            return True
        media_msg = state.pop("pending_media_msg", message)
        await _process_doc(message, media_msg, text, state)
        return True

    if step == "result":
        if text == "✅ Готово":
            clear_state(uid)
            await message.answer(
                "Готово. Можете задать вопрос или начать заново.",
                keyboard=main_kb(),
            )
            return True

        if text == "🎤 Речь" and state.get("doc_type") == "lawsuit":
            # проверка лимита — при отказе остаёмся на этапе
            try:
                from services.whitelist import check_and_consume_limit, is_admin, is_premium
                if not (is_admin(uid) or is_premium(uid)):
                    ok, reason = check_and_consume_limit(uid, "standard")
                    if not ok:
                        await message.answer(reason, keyboard=result_kb_lawsuit())
                        return True
            except Exception:
                pass
            await message.answer("⏳ Готовлю речь...")
            try:
                laws_ref = await ensure_laws()
                parts = [
                    types.Part.from_uri(file_uri=laws_ref.uri, mime_type="text/plain"),
                    types.Part.from_text(text=state.get("complaint_text", "")),
                ]
                speech = await generate_with_retry(parts, SYSTEM_SPEECH, max_tokens=4096)
                state["speech_text"] = speech
                await send_long(message, "🎤 Возможная речь:\n\n" + speech)
                await message.answer("Выберите действие:", keyboard=result_kb_lawsuit())
            except Exception as e:
                logger.exception("speech")
                err = str(e)
                await message.answer(
                    "⏳ Сервис временно недоступен. Кнопки ниже по-прежнему активны.",
                    keyboard=result_kb_lawsuit(),
                )
                try:
                    from config import ADMIN_IDS
                    detail = (
                        f"⚠️ Ошибка нейросети (речь)\n"
                        f"user_id={uid}\n"
                        f"{err[:1500]}"
                    )
                    for aid in ADMIN_IDS:
                        try:
                            await message.ctx_api.messages.send(
                                user_id=aid, message=detail, random_id=0
                            )
                        except Exception as ne:
                            logger.warning(f"notify admin {aid}: {ne}")
                except Exception as ne:
                    logger.warning(f"admin notify failed: {ne}")
            return True

        if text == "🛡️ Контраргументы" and state.get("doc_type") == "lawsuit":
            try:
                from services.whitelist import check_and_consume_limit, is_admin, is_premium
                if not (is_admin(uid) or is_premium(uid)):
                    ok, reason = check_and_consume_limit(uid, "standard")
                    if not ok:
                        await message.answer(reason, keyboard=result_kb_lawsuit())
                        return True
            except Exception:
                pass
            await message.answer("⏳ Разбираю контраргументы...")
            try:
                laws_ref = await ensure_laws()
                ctx = state.get("complaint_text", "") + "\n\n" + state.get("speech_text", "")
                parts = [
                    types.Part.from_uri(file_uri=laws_ref.uri, mime_type="text/plain"),
                    types.Part.from_text(text=ctx),
                ]
                answer = await generate_with_retry(parts, SYSTEM_COUNTER, max_tokens=4096)
                await send_long(message, answer)
                await message.answer("Выберите действие:", keyboard=result_kb_lawsuit())
            except Exception as e:
                logger.exception("counter")
                err = str(e)
                await message.answer(
                    "⏳ Сервис временно недоступен. Кнопки ниже по-прежнему активны.",
                    keyboard=result_kb_lawsuit(),
                )
                try:
                    from config import ADMIN_IDS
                    detail = (
                        f"⚠️ Ошибка нейросети (контраргументы)\n"
                        f"user_id={uid}\n"
                        f"{err[:1500]}"
                    )
                    for aid in ADMIN_IDS:
                        try:
                            await message.ctx_api.messages.send(
                                user_id=aid, message=detail, random_id=0
                            )
                        except Exception as ne:
                            logger.warning(f"notify admin {aid}: {ne}")
                except Exception as ne:
                    logger.warning(f"admin notify failed: {ne}")
            return True

        # любое другое сообщение на этапе result — вернуть кнопки
        await message.answer(
            "Используйте кнопки ниже.",
            keyboard=_result_kb(state),
        )
        return True

    return False
