import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# В обновлениях: VK_TOKEN_TEST приоритетнее; на проде оставьте только VK_TOKEN
VK_TOKEN = os.getenv("VK_TOKEN_TEST") or os.getenv("VK_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Несколько ключей через запятую — ротация при 429/503
_raw_keys = os.getenv("GEMINI_API_KEYS", "") or ""
GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
if GEMINI_API_KEY and GEMINI_API_KEY not in GEMINI_API_KEYS:
    GEMINI_API_KEYS.insert(0, GEMINI_API_KEY)
if not GEMINI_API_KEYS and GEMINI_API_KEY:
    GEMINI_API_KEYS = [GEMINI_API_KEY]

ADMIN_IDS = set(map(int, os.getenv("ADMIN_IDS", "572460798").split(",")))

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

WHITELIST_FILE = DATA_DIR / "whitelist.json"
USER_MODES_FILE = DATA_DIR / "user_modes.json"
LAWS_FILE = DATA_DIR / "laws.txt"
RANKS_FILE = DATA_DIR / "ranks.json"
ACCESS_STATE_FILE = DATA_DIR / "access_state.json"
CONSENTS_FILE = DATA_DIR / "consents.json"

MODEL_FALLBACKS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]

# Fast: старт с lite, далее любые flash из общего пула
FAST_MODELS = [
    # приоритет: менее перегруженные flash, lite — в хвосте
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
]

DEFAULT_LIMIT_STANDARD = 5
DEFAULT_LIMIT_FAST = 10

REPORT_COOLDOWN_SEC = 15
SUPPORT_REPORT_CD_SEC = 300
PREMIUM_ADD_CD_SEC = 3600

MAX_USER_TEXT_LEN = 8000
MAX_ATTACHMENTS = 10

# Очередь нейросети: одновременно обрабатывается не больше N запросов
NN_MAX_CONCURRENT = 2

# Лимит фото для юр. разбора: N штук за окно
PHOTO_LIMIT_COUNT = 3
PHOTO_LIMIT_WINDOW_SEC = 15 * 60
