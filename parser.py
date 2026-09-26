"""Selenium-парсер Kwork и фоновый мониторинг.

Все операции с браузером выполняются в одном выделенном потоке (ThreadPoolExecutor
с одним воркером): Selenium не потокобезопасен, а так парсинг и отправка откликов
автоматически выстраиваются в очередь и не мешают друг другу.
"""
import asyncio
import html
import json
import logging
import random
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiohttp
import undetected_chromedriver as uc
from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaDocument
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

import config
import database as db
from keyboards import project_kb

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = config.BASE_DIR / 'logs'

# Извлекает список проектов из JSON, который Kwork встраивает в страницу (window.stateData).
# Это надёжнее, чем разбирать вёрстку карточек.
_EXTRACT_JS = '''
const s = window.stateData || {};
const l = s.wantsListData || s;
const p = l.pagination || s.pagination || {};
return JSON.stringify({
    wants: l.wants || s.wants || [],
    lastPage: p.last_page || 1,
    userId: window.USER_ID || 0,
});
'''

# Установка значения поля так, чтобы Vue/React увидели изменение.
_SET_VALUE_JS = '''
const el = arguments[0], v = arguments[1];
if (el.isContentEditable) {
    el.focus();
    el.innerText = v;
} else {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
}
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
el.dispatchEvent(new Event('blur', {bubbles: true}));
'''


class ParserError(Exception):
    pass


def _fmt_money(value) -> str:
    return f'{int(float(value)):,}'.replace(',', ' ') + ' ₽'


def _normalize(want: dict) -> dict:
    project_id = int(want['id'])
    price = _fmt_money(want.get('priceLimit') or 0)
    possible = want.get('possiblePriceLimit')
    if possible and float(possible) > float(want.get('priceLimit') or 0):
        price += f' (допустимо до {_fmt_money(possible)})'
    return {
        'id': project_id,
        'title': html.unescape(want.get('name') or '').strip(),
        'description': html.unescape(want.get('description') or '').replace('\r\n', '\n').strip(),
        'price': price,
        'url': config.PROJECT_URL.format(project_id=project_id),
        'files': [
            {'name': html.unescape(f.get('fname') or 'file'), 'url': f['url'], 'size': int(f.get('size') or 0)}
            for f in want.get('files') or []
            if f.get('url')
        ],
    }


def _detect_chrome_major() -> int | None:
    """Мажорная версия Chrome: без неё uc может скачать несовместимый chromedriver."""
    if config.CHROME_VERSION_MAIN:
        return config.CHROME_VERSION_MAIN
    candidates = [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        *filter(None, map(shutil.which, ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'))),
    ]
    for path in candidates:
        try:
            out = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if m := re.search(r'(\d+)\.\d+\.\d+', out):
            return int(m.group(1))
    return None


def _user_id(driver) -> int:
    try:
        return int(driver.execute_script('return window.USER_ID || 0') or 0)
    except (WebDriverException, TypeError, ValueError):
        return 0


class KworkBrowser:
    """Синхронная обёртка над Chrome. Методы вызывать только из потока executor'а."""

    def __init__(self, headless: bool = config.HEADLESS):
        self.headless = headless
        self.driver: uc.Chrome | None = None

    # ---------- жизненный цикл ----------

    def _ensure_driver(self) -> uc.Chrome:
        if self.driver is not None:
            try:
                _ = self.driver.current_url  # проверка, что браузер жив
                return self.driver
            except WebDriverException:
                logger.warning('Браузер не отвечает, перезапускаю')
                self.close()

        Path(config.CHROME_PROFILE_DIR).mkdir(parents=True, exist_ok=True)
        options = uc.ChromeOptions()
        options.add_argument('--lang=ru-RU')
        options.add_argument('--window-size=1400,1000')
        options.add_argument('--no-first-run')
        options.add_argument('--no-default-browser-check')
        self.driver = uc.Chrome(
            options=options,
            user_data_dir=config.CHROME_PROFILE_DIR,
            headless=self.headless,
            version_main=_detect_chrome_major(),
        )
        self.driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
        return self.driver

    def close(self) -> None:
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except Exception:
            logger.exception('Ошибка при закрытии браузера')
        finally:
            self.driver = None

    def _screenshot(self, name: str) -> Path | None:
        if self.driver is None:
            return None
        try:
            SCREENSHOT_DIR.mkdir(exist_ok=True)
            path = SCREENSHOT_DIR / f'{name}_{int(time.time())}.png'
            self.driver.save_screenshot(str(path))
            return path
        except WebDriverException:
            return None

    # ---------- авторизация ----------

    def is_logged_in(self) -> bool:
        driver = self._ensure_driver()
        if 'kwork.ru' not in driver.current_url:
            driver.get(config.PROJECTS_URL.format(page=1))
        return _user_id(driver) > 0

    def login(self) -> bool:
        """Вход по логину/паролю из .env. Сессия сохраняется в профиле Chrome."""
        if self.is_logged_in():
            return True
        if not (config.KWORK_EMAIL and config.KWORK_PASSWORD):
            logger.warning('KWORK_EMAIL/KWORK_PASSWORD не заданы — работаю без авторизации')
            return False

        driver = self._ensure_driver()
        driver.get(config.PROJECTS_URL.format(page=1))
        try:
            WebDriverWait(driver, 10).until(lambda d: d.find_elements(By.CLASS_NAME, 'login-js'))
            driver.execute_script('arguments[0].click()', driver.find_elements(By.CLASS_NAME, 'login-js')[0])
            password = WebDriverWait(driver, 10).until(
                lambda d: next((e for e in d.find_elements(By.CSS_SELECTOR, 'input[type="password"]')
                                if e.is_displayed()), None)
            )
            # Поле логина — ближайший видимый текстовый input перед полем пароля в той же форме
            form = password.find_element(By.XPATH, './ancestor::form[1]')
            login_input = next(
                e for e in form.find_elements(By.CSS_SELECTOR, 'input[type="text"], input[type="email"], input:not([type])')
                if e.is_displayed()
            )
            login_input.clear()
            login_input.send_keys(config.KWORK_EMAIL)
            time.sleep(random.uniform(0.3, 0.8))
            password.clear()
            password.send_keys(config.KWORK_PASSWORD)
            time.sleep(random.uniform(0.3, 0.8))
            submit = form.find_elements(By.CSS_SELECTOR, 'button[type="submit"], .auth-form__button')
            if submit:
                driver.execute_script('arguments[0].click()', submit[0])
            else:
                password.submit()
            WebDriverWait(driver, 20).until(lambda d: _user_id(d) > 0)
        except (TimeoutException, StopIteration, WebDriverException) as e:
            shot = self._screenshot('login_failed')
            logger.warning('Не удалось войти автоматически (%s). Скриншот: %s. '
                           'Возможно, нужна капча — выполните `python parser.py --login`.', e, shot)
            return False

        driver.get(config.PROJECTS_URL.format(page=1))
        return self.is_logged_in()

    # ---------- парсинг ----------

    def fetch_page(self, page: int) -> tuple[list[dict], int]:
        """Возвращает (проекты страницы, номер последней страницы)."""
        driver = self._ensure_driver()
        try:
            driver.get(config.PROJECTS_URL.format(page=page))
        except TimeoutException:
            logger.warning('Страница %s грузится слишком долго, пробую прочитать то, что есть', page)
        try:
            WebDriverWait(driver, config.PAGE_LOAD_TIMEOUT).until(
                lambda d: d.execute_script('return !!(window.stateData && '
                                           '(window.stateData.wantsListData || window.stateData.wants))')
            )
        except TimeoutException:
            shot = self._screenshot(f'page_{page}_failed')
            raise ParserError(f'На странице {page} не найден список проектов '
                              f'(title="{driver.title}", скриншот: {shot}). Возможна блокировка/капча.')

        data = json.loads(driver.execute_script(_EXTRACT_JS))
        projects = []
        for want in data['wants']:
            try:
                projects.append(_normalize(want))
            except (KeyError, TypeError, ValueError):
                logger.exception('Не удалось разобрать проект: %r', want.get('id'))
        return projects, int(data['lastPage'])

    def get_cookies(self) -> dict[str, str]:
        driver = self._ensure_driver()
        return {c['name']: c['value'] for c in driver.get_cookies()}

    def user_agent(self) -> str:
        return self._ensure_driver().execute_script('return navigator.userAgent')

    # ---------- отклик ----------

    def _visible(self, selector: str) -> list[WebElement]:
        return [e for e in self.driver.find_elements(By.CSS_SELECTOR, selector) if e.is_displayed()]

    def send_offer(self, project_id: int, text: str, price: int | None, days: int | None) -> str:
        """Отправляет отклик. Возвращает описание результата или бросает ParserError."""
        if not self.login():
            raise ParserError('Нет авторизации на Kwork. Проверьте KWORK_EMAIL/KWORK_PASSWORD '
                              'или выполните `python parser.py --login`.')
        driver = self._ensure_driver()
        driver.get(config.OFFER_URL.format(project_id=project_id))

        try:
            field = WebDriverWait(driver, 20).until(
                lambda d: next(iter(
                    self._visible('textarea[name="description"]')
                    or self._visible('textarea')
                    or self._visible('[contenteditable="true"]')
                ), None)
            )
        except TimeoutException:
            shot = self._screenshot(f'offer_{project_id}_no_form')
            raise ParserError(f'Форма отклика не найдена (url={driver.current_url}, скриншот: {shot}). '
                              'Возможно, отклик уже отправлен или закончились коннекты.')

        driver.execute_script('arguments[0].scrollIntoView({block: "center"})', field)
        driver.execute_script(_SET_VALUE_JS, field, text)
        time.sleep(random.uniform(0.5, 1.2))

        # Цена и срок: заполняем только если поля есть и пустые
        if price:
            for inp in self._visible('input[name*="price"], input[name*="cost"]'):
                if not inp.get_attribute('value'):
                    driver.execute_script(_SET_VALUE_JS, inp, str(price))
        if days:
            for inp in self._visible('input[name*="duration"], input[name*="days"]'):
                if not inp.get_attribute('value'):
                    driver.execute_script(_SET_VALUE_JS, inp, str(days))
            for sel in driver.find_elements(By.CSS_SELECTOR, 'select[name*="duration"], select[name*="days"]'):
                driver.execute_script(
                    'const s = arguments[0], d = String(arguments[1]);'
                    'const o = [...s.options].find(o => o.value === d) || [...s.options].find(o => o.value);'
                    'if (o) { s.value = o.value; s.dispatchEvent(new Event("change", {bubbles: true})); }',
                    sel, days,
                )
        time.sleep(random.uniform(0.5, 1.2))

        buttons = [
            b for b in self._visible('button, input[type="submit"]')
            if b.is_enabled() and re.search(r'предложить|отправить|откликнуться',
                                            (b.text or b.get_attribute('value') or ''), re.I)
        ]
        if not buttons:
            shot = self._screenshot(f'offer_{project_id}_no_button')
            raise ParserError(f'Кнопка отправки не найдена (скриншот: {shot})')
        start_url = driver.current_url
        driver.execute_script('arguments[0].click()', buttons[-1])

        try:
            WebDriverWait(driver, 20).until(
                lambda d: d.current_url != start_url
                or re.search(r'предложение (успешно )?отправлено|ваше предложение',
                             d.find_element(By.TAG_NAME, 'body').text, re.I)
            )
        except TimeoutException:
            errors = [e.text.strip() for e in self._visible('[class*="error"]') if e.text.strip()]
            shot = self._screenshot(f'offer_{project_id}_failed')
            raise ParserError('Kwork не подтвердил отправку. '
                              + (f'Ошибки формы: {"; ".join(errors[:3])}. ' if errors else '')
                              + f'Скриншот: {shot}')
        return driver.current_url


class Monitor:
    """Фоновый мониторинг новых проектов с безопасным стартом/остановкой."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.browser = KworkBrowser()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='selenium')
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()  # защищает start/stop от гонок
        self.chat_id: int | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_in_browser(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, func, *args)

    async def start(self, chat_id: int) -> bool:
        async with self._lock:
            if self.running:
                return False
            self.chat_id = chat_id
            self._task = asyncio.create_task(self._loop(), name='kwork-monitor')
            return True

    async def stop(self) -> bool:
        async with self._lock:
            if not self.running:
                return False
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            # Попадёт в очередь executor'а после текущей Selenium-операции
            await self.run_in_browser(self.browser.close)
            return True

    async def shutdown(self) -> None:
        await self.stop()
        await self.run_in_browser(self.browser.close)
        self._executor.shutdown(wait=True)

    async def _loop(self) -> None:
        logger.info('Мониторинг запущен')
        try:
            logged_in = await self.run_in_browser(self.browser.login)
            if not logged_in:
                await self.bot.send_message(
                    self.chat_id,
                    '⚠️ Не удалось авторизоваться на Kwork. Мониторинг работает, '
                    'но отклики отправить не получится.',
                )
            while True:
                try:
                    count = await self.run_cycle()
                    logger.info('Цикл завершён, новых проектов: %s', count)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception('Ошибка цикла парсинга')
                    await self._safe_send(f'⚠️ Ошибка парсинга: {html.escape(str(e))[:500]}')
                    await self.run_in_browser(self.browser.close)  # следующий цикл начнёт с чистого браузера
                await asyncio.sleep(random.uniform(config.PARSE_INTERVAL_MIN, config.PARSE_INTERVAL_MAX))
        finally:
            logger.info('Мониторинг остановлен')

    async def run_cycle(self) -> int:
        sent = 0
        page, last_page = 1, 1
        while page <= min(config.MAX_PAGES, last_page):
            projects, last_page = await self.run_in_browser(self.browser.fetch_page, page)
            statuses = await db.get_statuses(p['id'] for p in projects)
            # 'new' — сохранён, но отправка в Telegram не удалась: пробуем ещё раз
            fresh = [p for p in projects if statuses.get(p['id'], db.STATUS_NEW) == db.STATUS_NEW]
            for project in fresh:
                await db.add_project(project)
                if await self.notify(project):
                    sent += 1
                await asyncio.sleep(config.SEND_DELAY)
            if not fresh:
                break  # дальше идут уже виденные проекты
            page += 1
            await asyncio.sleep(random.uniform(config.PAGE_DELAY_MIN, config.PAGE_DELAY_MAX))
        return sent

    async def _safe_send(self, text: str) -> None:
        try:
            await self.bot.send_message(self.chat_id, text)
        except Exception:
            logger.exception('Не удалось отправить сообщение в Telegram')

    # ---------- уведомления ----------

    async def _download_files(self, files: list[dict]) -> tuple[list[BufferedInputFile], list[dict]]:
        """Скачивает файлы проекта. Возвращает (скачанные, не удалось/слишком большие)."""
        if not files:
            return [], []
        cookies = await self.run_in_browser(self.browser.get_cookies)
        user_agent = await self.run_in_browser(self.browser.user_agent)
        downloaded, failed = [], []
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(cookies=cookies, headers={'User-Agent': user_agent},
                                         timeout=timeout) as session:
            for f in files:
                if f['size'] > config.MAX_FILE_SIZE:
                    failed.append(f)
                    continue
                try:
                    # Без авторизации Kwork редиректит на страницу проекта (HTML) вместо файла
                    async with session.get(f['url'], allow_redirects=False) as resp:
                        if resp.status != 200 or resp.content_type == 'text/html':
                            logger.warning('Файл %s недоступен (HTTP %s, %s) — нужна авторизация?',
                                           f['url'], resp.status, resp.content_type)
                            failed.append(f)
                            continue
                        data = await resp.read()
                    if len(data) > config.MAX_FILE_SIZE:
                        failed.append(f)
                        continue
                    downloaded.append(BufferedInputFile(data, filename=f['name']))
                except Exception:
                    logger.exception('Не удалось скачать файл %s', f['url'])
                    failed.append(f)
        return downloaded, failed

    @staticmethod
    def _format(project: dict, failed_files: list[dict]) -> str:
        head = (f'🆕 <b>{html.escape(project["title"])}</b>\n\n'
                f'💰 <b>Цена:</b> {html.escape(project["price"])}\n')
        tail = f'\n🔗 {project["url"]}'
        if failed_files:
            tail += '\n\n📎 Файлы (не удалось прикрепить):\n' + '\n'.join(
                f'<a href="{html.escape(f["url"])}">{html.escape(f["name"])}</a>' for f in failed_files
            )
        limit = 4000 - len(head) - len(tail)
        desc = html.escape(project['description'])
        if len(desc) > limit:
            desc = desc[:limit - 1].rsplit('&', 1)[0] if '&' in desc[limit - 10:limit] else desc[:limit - 1]
            desc += '…'
        return f'{head}\n{desc}\n{tail}'

    async def notify(self, project: dict) -> bool:
        try:
            files, failed = await self._download_files(project['files'])
            message_ids = []
            for i in range(0, len(files), 10):
                chunk = files[i:i + 10]
                if len(chunk) == 1:
                    msg = await self.bot.send_document(self.chat_id, chunk[0])
                    message_ids.append(msg.message_id)
                else:
                    msgs = await self.bot.send_media_group(
                        self.chat_id, [InputMediaDocument(media=f) for f in chunk]
                    )
                    message_ids.extend(m.message_id for m in msgs)
            msg = await self.bot.send_message(
                self.chat_id,
                self._format(project, failed),
                reply_markup=project_kb(project['id'], project['url']),
                disable_web_page_preview=True,
            )
            message_ids.append(msg.message_id)
            await db.mark_sent(project['id'], message_ids)
            return True
        except Exception:
            logger.exception('Не удалось отправить проект %s', project['id'])
            return False


def _manual_login() -> None:
    """Открывает Chrome с профилем бота, чтобы войти в Kwork вручную (например, при капче)."""
    browser = KworkBrowser(headless=False)
    try:
        browser._ensure_driver().get('https://kwork.ru/login')
        input('Войдите в аккаунт Kwork в открывшемся окне и нажмите Enter здесь...')
        print('Авторизация сохранена.' if browser.is_logged_in() else 'Авторизация не обнаружена.')
    finally:
        browser.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    if '--login' in sys.argv:
        _manual_login()
    else:
        b = KworkBrowser()
        try:
            items, last = b.fetch_page(1)
            print(f'Страница 1 из {last}, проектов: {len(items)}')
            for it in items:
                print(it['id'], it['price'], it['title'], f'файлов: {len(it["files"])}')
        finally:
            b.close()
