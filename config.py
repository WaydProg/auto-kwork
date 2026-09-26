import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


# --- Telegram ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
# Кто может управлять ботом. Через запятую в .env: ADMIN_IDS=123,456
LIST_ADMINS_ID = [int(x) for x in os.getenv('ADMIN_IDS', '1473795067').split(',') if x.strip()]

# --- Kwork ---
KWORK_EMAIL = os.getenv('KWORK_EMAIL') or os.getenv('email')
KWORK_PASSWORD = os.getenv('KWORK_PASSWORD') or os.getenv('password')
PROJECTS_URL = 'https://kwork.ru/projects?a=1&page={page}'
OFFER_URL = 'https://kwork.ru/new_offer?project={project_id}'
PROJECT_URL = 'https://kwork.ru/projects/{project_id}/view'

# --- Selenium ---
# Постоянный профиль Chrome: в нём хранятся cookies авторизованной сессии.
CHROME_PROFILE_DIR = os.getenv('CHROME_PROFILE_DIR', str(BASE_DIR / 'chrome_profile'))
HEADLESS = _bool('HEADLESS', False)
# Мажорная версия установленного Chrome (если uc скачивает несовместимый chromedriver)
CHROME_VERSION_MAIN = _int('CHROME_VERSION_MAIN', 0) or None

# --- Задержки (секунды) ---
PARSE_INTERVAL_MIN = _float('PARSE_INTERVAL_MIN', 60)    # пауза между циклами парсинга
PARSE_INTERVAL_MAX = _float('PARSE_INTERVAL_MAX', 180)
PAGE_DELAY_MIN = _float('PAGE_DELAY_MIN', 2)             # пауза между страницами пагинации
PAGE_DELAY_MAX = _float('PAGE_DELAY_MAX', 5)
PAGE_LOAD_TIMEOUT = _float('PAGE_LOAD_TIMEOUT', 30)
MAX_PAGES = _int('MAX_PAGES', 3)                         # сколько страниц листать за цикл
SEND_DELAY = _float('SEND_DELAY', 1.0)                   # пауза между сообщениями в Telegram
OFFER_DEFAULT_DAYS = _int('OFFER_DEFAULT_DAYS', 3)            # срок в отклике по умолчанию

# --- Файлы ---
MAX_FILE_SIZE = 50 * 1024 * 1024  # лимит Bot API на отправку файла

DB_PATH = os.getenv('DB_PATH', str(BASE_DIR / 'db.sqlite3'))
