"""Общий клиент Gemini с ротацией ключей, моделей и очередью запросов."""
from __future__ import annotations

import asyncio
import logging
import random
import threading
from typing import Any, Awaitable, Callable, Optional

from google import genai
from google.genai import types

from config import GEMINI_API_KEYS, MODEL_FALLBACKS, FAST_MODELS

logger = logging.getLogger("gemini")

try:
    from config import NN_MAX_CONCURRENT
except Exception:
    NN_MAX_CONCURRENT = 2

# --- очередь: не больше NN_MAX_CONCURRENT одновременных запросов к NN ---
_nn_active = 0
_nn_waiting = 0
_nn_cond: Optional[asyncio.Condition] = None


def _get_cond() -> asyncio.Condition:
    global _nn_cond
    if _nn_cond is None:
        _nn_cond = asyncio.Condition()
    return _nn_cond


async def acquire_nn_slot(
    notify_cb: Optional[Callable[[int], Awaitable[None]]] = None,
) -> None:
    """
    Занять слот обработки. Если все NN_MAX_CONCURRENT заняты — ждать в очереди.
    notify_cb(position) — номер в очереди ожидания (1 = первый после текущих).
    """
    global _nn_active, _nn_waiting
    cond = _get_cond()

    async with cond:
        must_wait = _nn_active >= NN_MAX_CONCURRENT
        if must_wait:
            _nn_waiting += 1
            position = _nn_waiting
        else:
            position = 0

    if must_wait and notify_cb is not None:
        try:
            await notify_cb(position)
        except Exception as e:
            logger.warning("queue notify: %s", e)

    async with cond:
        while _nn_active >= NN_MAX_CONCURRENT:
            await cond.wait()
        if must_wait:
            _nn_waiting = max(0, _nn_waiting - 1)
        _nn_active += 1


async def release_nn_slot() -> None:
    global _nn_active
    cond = _get_cond()
    async with cond:
        _nn_active = max(0, _nn_active - 1)
        cond.notify(1)


# --- ключи ---
_lock = threading.Lock()
_key_index = 0
_clients: dict[str, genai.Client] = {}


def _all_keys() -> list[str]:
    return list(GEMINI_API_KEYS) if GEMINI_API_KEYS else []


def current_key_id() -> str:
    keys = _all_keys()
    if not keys:
        return ""
    with _lock:
        return keys[_key_index % len(keys)][:12]  # short id for logs/cache


def get_client(force_next: bool = False) -> genai.Client:
    global _key_index
    keys = _all_keys()
    if not keys:
        raise RuntimeError("GEMINI_API_KEY / GEMINI_API_KEYS не заданы")
    with _lock:
        if force_next and len(keys) > 1:
            _key_index = (_key_index + 1) % len(keys)
            logger.warning("Ротация API-ключа → index=%s", _key_index)
        key = keys[_key_index % len(keys)]
        if key not in _clients:
            _clients[key] = genai.Client(api_key=key)
        return _clients[key]


def _afc_disabled_config(**kwargs) -> types.GenerateContentConfig:
    base = dict(kwargs)
    try:
        base["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
            disable=True
        )
    except Exception:
        pass
    return types.GenerateContentConfig(**base)


def _is_overload(err: str) -> bool:
    e = err.lower()
    return any(
        x in e
        for x in (
            "503",
            "unavailable",
            "high demand",
            "429",
            "resource_exhausted",
            "quota",
            "rate limit",
            "exceeded",
        )
    )


async def generate_content_resilient(
    parts: Any,
    system_instruction: str,
    *,
    mode: str = "standard",
    max_output_tokens: int = 8192,
    temperature: float = 0.1,
    max_retries: int = 2,
) -> str:
    models = list(FAST_MODELS) if mode == "fast" else list(MODEL_FALLBACKS)
    keys = _all_keys()
    n_keys = max(1, len(keys))
    last_error: Exception | None = None

    # Сначала все модели на текущем ключе, потом ротация ключа
    for key_try in range(n_keys):
        if key_try > 0:
            get_client(force_next=True)
        client = get_client()

        for model in models:
            for attempt in range(max_retries):
                try:
                    config = _afc_disabled_config(
                        system_instruction=system_instruction,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                    )
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model,
                        contents=parts,
                        config=config,
                    )
                    if response and response.text:
                        return response.text
                    raise ValueError("Пустой ответ модели")
                except Exception as e:
                    last_error = e
                    err = str(e)
                    logger.warning(
                        "[%s] key_try=%s attempt=%s: %s",
                        model,
                        key_try,
                        attempt,
                        err[:180],
                    )
                    # 403 на чужой File — пробрасываем наверх (lawyer перезальёт laws)
                    if "PERMISSION_DENIED" in err or "access the File" in err:
                        raise
                    if _is_overload(err):
                        if attempt < max_retries - 1:
                            await asyncio.sleep(
                                (1.2 ** attempt) + random.uniform(0.15, 0.5)
                            )
                            continue
                        # следующая модель на том же ключе
                        break
                    # иная ошибка — следующая модель
                    break

    raise last_error or Exception("Сервис временно недоступен")
