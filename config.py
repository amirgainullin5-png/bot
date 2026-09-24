import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

VK_TOKEN = os.getenv("VK_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_IDS = set(map(int, os.getenv("ADMIN_IDS", "572460798").split(",")))

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

WHITELIST_FILE = DATA_DIR / "whitelist.json"
USER_MODES_FILE = DATA_DIR / "user_modes.json"
LAWS_FILE = DATA_DIR / "laws.txt"
RANKS_FILE = DATA_DIR / "ranks.json"

MODEL_FALLBACKS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]