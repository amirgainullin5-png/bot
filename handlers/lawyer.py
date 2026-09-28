import asyncio
import logging
import random
from datetime import datetime, timedelta

from vkbottle.bot import Message
from google import genai
from google.genai import types

from config import GEMINI_API_KEY, LAWS_FILE, MODEL_FALLBACKS, FAST_MODELS
from services.gemini_client import (
    generate_content_resilient, get_client, current_key_id,
    acquire_nn_slot, release_nn_slot,
)

logger = logging.getLogger("lawyer")

# laws.txt загружается ОТДЕЛЬНО для каждого API-ключа (File API не шарится между ключами)
_laws_by_key: dict[str, tuple] = {}  # key_id -> (file_ref, uploaded_at)

SYSTEM_INSTRUCTION_STANDARD = """Ты — главный юридический эксперт и аналитик нормативно-правовой базы ролевого проекта.
Твоя задача — давать исчерпывающие, юридически точные, окончательные разборы любых спорных ситуаций.

ПРИНЦИПЫ:
1. Используй ИСКЛЮЧИТЕЛЬНО акты из файла laws.txt.
2. Каждое утверждение подкрепляй точной ссылкой на статью.
3. Не задавай уточняющих вопросов.
4. При скриншотах: распознай текст, но в ответ включай ТОЛЬКО факты, нужные для юридической оценки вопроса пользователя.
5. Иерархия: Конституция > ФЗ > Постановления > Уставы.

ФИЛЬТР ФАКТОВ (обязательно):
- НЕ перечисляй сообщения рации, общий чат, объявления розыска, склад/инвентарь, полигон, новости ТРК и прочий HUD-шум, если это не относится к сути вопроса.
- НЕ копируй длинный «распознанный текст» целиком.
- В п.1 — 3–8 коротких предложений: кто, где, что произошло, что видно на фото по теме вопроса (позиция ТС, знаки, разметка, действия сторон).
- Дата/место — только если важны для оценки.

ВАЖНО: Никогда не используй символы ** и *.

СТРУКТУРА ОТВЕТА:
📋 1. Суть ситуации и факты: (кратко, только релевантное)
⚖️ 2. Нормативно-правовая база:
🔍 3. Анализ правомерности и нарушений:
👨‍⚖️ 4. Вердикт и рекомендации:
"""

SYSTEM_INSTRUCTION_FAST = """Ты — быстрый юридический помощник ролевого проекта.
Режим: FAST.

Правила:
- Отвечай максимально кратко.
- Используй ТОЛЬКО laws.txt.
- Никогда не используй символы ** и *.
- На скриншотах игнорируй рацию, общий чат, HUD, склад, розыск и т.п., если это не по теме вопроса.
- Не пересказывай весь распознанный текст — только 1–3 факта по существу.

Формат:
⚡ Статья: (номер + название)
📜 Цитата: (1-3 предложения)
✅ Что делать:
1. ...
2. ...
"""


async def upload_laws_for_current_key():
    """Загрузить laws.txt в File API текущего ключа."""
    if not LAWS_FILE.exists():
        raise FileNotFoundError("laws.txt не найден")
    kid = current_key_id()
    client = get_client()
    logger.info("Загружаю laws.txt для ключа %s...", kid)
    file = await asyncio.to_thread(
        client.files.upload,
        file=str(LAWS_FILE),
        config={"display_name": "Project Laws"},
    )
    while getattr(file.state, "name", None) == "PROCESSING":
        await asyncio.sleep(2)
        file = await asyncio.to_thread(client.files.get, name=file.name)
    if getattr(file.state, "name", None) == "FAILED":
        raise RuntimeError("Ошибка загрузки laws.txt")
    _laws_by_key[kid] = (file, datetime.now())
    logger.info("Laws загружены: %s (key=%s)", file.name, kid)
    return file


async def ensure_laws():
    """Файл laws для ТЕКУЩЕГО API-ключа (после ротации — своя копия)."""
    kid = current_key_id()
    entry = _laws_by_key.get(kid)
    if entry is None:
        return await upload_laws_for_current_key()
    file_ref, uploaded_at = entry
    if datetime.now() - uploaded_at > timedelta(hours=47):
        return await upload_laws_for_current_key()
    return file_ref


def invalidate_laws_for_current_key():
    kid = current_key_id()
    _laws_by_key.pop(kid, None)


async def generate_with_retry(parts, system_instruction: str, mode: str = "standard", max_retries=3) -> str:
    """При 403 на File API — перезаливаем laws под текущий ключ и повторяем."""
    last_err = None
    for round_i in range(2):
        try:
            return await generate_content_resilient(
                parts,
                system_instruction,
                mode=mode,
                max_output_tokens=8192 if mode == "standard" else 2048,
                temperature=0.05 if mode == "standard" else 0.1,
                max_retries=max_retries,
            )
        except Exception as e:
            last_err = e
            err = str(e)
            if "PERMISSION_DENIED" in err or "not have permission to access the File" in err:
                logger.warning("403 File API — перезаливка laws и повтор")
                invalidate_laws_for_current_key()
                laws_ref = await ensure_laws()
                # заменить первый part (laws) на новый
                if parts and hasattr(parts[0], "file_data") or True:
                    parts = list(parts)
                    parts[0] = types.Part.from_uri(
                        file_uri=laws_ref.uri, mime_type="text/plain"
                    )
                continue
            raise
    raise last_err or Exception("Сервис временно недоступен")


def _main_kb():
    from vkbottle import Keyboard, KeyboardButtonColor, Text
    kb = Keyboard(one_time=False)
    kb.add(Text("📋 Составление отчета"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("⚖️ Жалоба / иск"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("⚙️ Режим"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("👤 Профиль"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


async def handle_lawyer(message: Message, mode: str = "standard") -> bool:
    from config import MAX_USER_TEXT_LEN, MAX_ATTACHMENTS

    text = (message.text or "").strip()
    if len(text) > MAX_USER_TEXT_LEN:
        text = text[:MAX_USER_TEXT_LEN]
    if not text and not message.attachments:
        return False

    if message.attachments and len(message.attachments) > MAX_ATTACHMENTS:
        await message.answer(
            f"❌ Слишком много вложений (макс. {MAX_ATTACHMENTS}).",
            keyboard=_main_kb(),
        )
        return True

    clean_text = text
    lower = text.lower()
    if lower.startswith(("фаст ", "fast ")):
        mode = "fast"
        clean_text = text.split(" ", 1)[1] if " " in text else ""
    elif lower.startswith(("стандарт ", "standard ")):
        mode = "standard"
        clean_text = text.split(" ", 1)[1] if " " in text else ""

    mode_label = "фаст" if mode == "fast" else "стандарт"

    # Подсчёт фото во вложении
    photo_atts = []
    if message.attachments:
        for att in message.attachments:
            if getattr(att, "photo", None):
                photo_atts.append(att)

    # Фаст — фото игнорируются
    if mode == "fast" and photo_atts:
        await message.answer(
            "ℹ️ Режим «фаст» не анализирует фотографии — только текст.\n"
            "Опишите ситуацию текстом или переключитесь на «стандарт».",
            keyboard=_main_kb(),
        )
        if not clean_text.strip():
            return True
        # есть текст — продолжаем без фото
        photo_atts = []

    # Лимит фото (стандарт)
    if photo_atts:
        from services.whitelist import check_photo_limit, consume_photo_slots, is_admin, is_premium
        n = min(len(photo_atts), MAX_ATTACHMENTS)
        ok, reason, _ = check_photo_limit(message.from_id, n)
        if not ok:
            await message.answer(reason, keyboard=_main_kb())
            return True

    await message.answer(f"⏳ Анализирую ({mode_label})...")

    async def _queue_notify(pos: int):
        await message.answer(
            f"📋 Запрос в очереди: №{pos}. Дождитесь освобождения слота."
        )

    got_slot = False
    try:
        await acquire_nn_slot(_queue_notify)
        got_slot = True

        laws_ref = await ensure_laws()
        parts = [types.Part.from_uri(file_uri=laws_ref.uri, mime_type="text/plain")]
        if clean_text:
            parts.append(types.Part.from_text(text=clean_text))

        if photo_atts and mode != "fast":
            import aiohttp
            from services.whitelist import consume_photo_slots
            photo_count = 0
            for att in photo_atts:
                if photo_count >= MAX_ATTACHMENTS:
                    break
                photo = max(att.photo.sizes, key=lambda s: s.width * s.height)
                async with aiohttp.ClientSession() as session:
                    async with session.get(photo.url) as resp:
                        if resp.status != 200:
                            continue
                        img_bytes = await resp.read()
                        if len(img_bytes) > 8 * 1024 * 1024:
                            continue
                parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
                photo_count += 1
            if photo_count:
                consume_photo_slots(message.from_id, photo_count)

        if len(parts) == 1:
            await message.answer("Пришлите текст или скриншот.", keyboard=_main_kb())
            return True

        system_prompt = SYSTEM_INSTRUCTION_FAST if mode == "fast" else SYSTEM_INSTRUCTION_STANDARD
        answer = await generate_with_retry(parts, system_prompt, mode=mode)

        if len(answer) > 3900:
            for i in range(0, len(answer), 3900):
                await message.answer(answer[i:i + 3900])
                await asyncio.sleep(0.3)
            await message.answer("—", keyboard=_main_kb())
        else:
            await message.answer(answer, keyboard=_main_kb())

    except Exception as e:
        logger.exception("Ошибка юриста")
        err = str(e)
        await message.answer(
            "⏳ Сервис временно недоступен. Попробуйте через несколько минут.",
            keyboard=_main_kb(),
        )
        try:
            from config import ADMIN_IDS
            detail = (
                f"⚠️ Ошибка нейросети (юрист)\n"
                f"user_id={message.from_id}\n"
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
    finally:
        if got_slot:
            await release_nn_slot()

    return True
