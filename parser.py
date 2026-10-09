"""Selenium-парсер Kwork и фоновый мониторинг.

Все операции с браузером выполняются в одном выделенном потоке (ThreadPoolExecutor
с одним воркером): Selenium не потокобезопасен, а так парсинг и отправка откликов
автоматически выстраиваются в очередь и не мешают друг другу.
"""
import asyncio
import html
import http.client
import json
import logging
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
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
from offer import PAYMENT_FULL, PAYMENT_STAGES, Offer

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


# Редактор trumbowyg (описание, название заказа, задачи): Vue читает содержимое видимого
# contenteditable-блока, поэтому пишем и в него, и в скрытую textarea.
_SET_TRUMBOWYG_JS = '''
const ta = arguments[0], v = arguments[1];
const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const value = v.split('\\n').map(l => '<div>' + (esc(l) || '<br>') + '</div>').join('');
const ed = ta.closest('.trumbowyg-box')?.querySelector('.trumbowyg-editor');
if (ed) {
    ed.innerHTML = value;
    ed.dispatchEvent(new Event('input', {bubbles: true}));
    ed.dispatchEvent(new Event('keyup', {bubbles: true}));
}
ta.value = value;
ta.dispatchEvent(new Event('input', {bubbles: true}));
ta.dispatchEvent(new Event('change', {bubbles: true}));
'''

# В свёрнутом окне обычный клик до страницы не доходит — повторяем его событиями мыши.
_CLICK_JS = '''
for (const type of ['mousedown', 'mouseup', 'click']) {
    arguments[0].dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
}
'''


def _duration_days(label: str) -> int | None:
    """«3 дня» → 3, «2 недели» → 14, «1 месяц» → 30."""
    m = re.match(r'\s*(\d+)\s*(\w+)', label or '')
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith('нед'):
        return n * 7
    if unit.startswith('мес'):
        return n * 30
    return n


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
        # С этой цены отклика Kwork предлагает выбрать порядок оплаты (целиком / частями)
        'price_threshold': int(want.get('getPriceThreshold') or 0) or None,
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


def _ascii_prefs(path: Path) -> None:
    """Переписывает Preferences профиля в чистый ASCII (\\uXXXX).

    uc при каждом старте читает этот файл как latin1 и сохраняет обратно, поэтому кириллица
    (например, имя профиля «Ваш Chrome») с каждым запуском удваивается. Файл дорастает до
    гигабайтов, и Chrome падает на старте: `session not created: cannot connect to chrome`.
    """
    try:
        raw = path.read_bytes()
        if raw.isascii():
            return
        path.write_text(json.dumps(json.loads(raw)), encoding='ascii')
    except (OSError, ValueError) as e:
        logger.warning('Не удалось подготовить Preferences профиля Chrome: %s', e)


def _user_id(driver) -> int:
    try:
        return int(driver.execute_script('return window.USER_ID || 0') or 0)
    except (WebDriverException, TypeError, ValueError):
        return 0


class KworkBrowser:
    """Синхронная обёртка над Chrome. Методы вызывать только из потока executor'а."""

    def __init__(self, headless: bool = config.HEADLESS, background: bool = config.BROWSER_BACKGROUND):
        self.headless = headless
        self.background = background and not headless
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
        _ascii_prefs(Path(config.CHROME_PROFILE_DIR) / 'Default' / 'Preferences')
        for attempt in range(3):
            options = uc.ChromeOptions()  # uc не разрешает переиспользовать объект опций
            options.add_argument('--lang=ru-RU')
            options.add_argument('--window-size=1400,1000')
            options.add_argument('--no-first-run')
            options.add_argument('--no-default-browser-check')
            # Свёрнутое окно Chrome иначе «усыпляет»: таймеры и отрисовка замедляются
            options.add_argument('--disable-background-timer-throttling')
            options.add_argument('--disable-backgrounding-occluded-windows')
            options.add_argument('--disable-renderer-backgrounding')
            try:
                self.driver = uc.Chrome(
                    options=options,
                    user_data_dir=config.CHROME_PROFILE_DIR,
                    headless=self.headless,
                    version_main=_detect_chrome_major(),
                )
                break
            except (OSError, http.client.HTTPException, urllib.error.URLError) as e:
                # uc при старте скачивает chromedriver — сетевые сбои тут бывают разовыми
                if attempt == 2:
                    raise
                logger.warning('Не удалось запустить chromedriver (%s), повтор...', e)
                time.sleep(3)
        self.driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
        if self.background:
            try:
                self.driver.minimize_window()
            except WebDriverException:
                logger.warning('Не удалось свернуть окно браузера')
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

    def _wait_visible(self, selector: str, timeout: float = 10, root: WebElement | None = None) -> WebElement:
        def find(_):
            elements = (root or self.driver).find_elements(By.CSS_SELECTOR, selector)
            return next((e for e in elements if e.is_displayed()), None)
        try:
            return WebDriverWait(self.driver, timeout).until(find)
        except TimeoutException:
            raise ParserError(f'На форме отклика не найден элемент {selector}')

    def _pause(self) -> None:
        time.sleep(random.uniform(0.4, 1.0))

    def _click(self, element: WebElement, done, timeout: float = 5) -> None:
        """Кликает и ждёт done(driver). Если обычный клик не сработал (свёрнутое окно), кликает через JS."""
        try:
            element.click()
            WebDriverWait(self.driver, 1.5).until(done)
            return
        except WebDriverException:  # в т.ч. TimeoutException
            pass
        self.driver.execute_script(_CLICK_JS, element)
        WebDriverWait(self.driver, timeout).until(done)

    def _type_number(self, inp: WebElement, value: int) -> None:
        """Цены вводятся посимвольно: от этого поля зависят блоки, которые форма показывает дальше.

        send_keys в свёрнутом окне (BROWSER_BACKGROUND) до поля не доходит, поэтому символы
        вставляются через CDP в сфокусированное поле, а результат сверяется со значением поля.
        """
        def typed() -> str:
            return re.sub(r'\D', '', inp.get_attribute('value') or '')

        self.driver.execute_script('arguments[0].scrollIntoView({block: "center"})', inp)
        if inp.get_attribute('value'):
            self.driver.execute_script(_SET_VALUE_JS, inp, '')
        self.driver.execute_script('arguments[0].focus()', inp)
        if self.driver.execute_script('return document.activeElement === arguments[0]', inp):
            for char in str(value):
                self.driver.execute_cdp_cmd('Input.insertText', {'text': char})
                time.sleep(random.uniform(0.05, 0.15))
        if typed() != str(value):
            self.driver.execute_script(_SET_VALUE_JS, inp, str(value))
        if typed() != str(value):
            raise ParserError(f'Не удалось ввести цену {value} ₽: в поле осталось «{inp.get_attribute("value")}»')
        self._pause()

    def _set_editor(self, textarea: WebElement, value: str) -> None:
        self.driver.execute_script('arguments[0].scrollIntoView({block: "center"})', textarea.find_element(
            By.XPATH, './ancestor::div[contains(@class, "trumbowyg-box")][1]'))
        self.driver.execute_script(_SET_TRUMBOWYG_JS, textarea, value)
        self._pause()

    def _select_duration(self, days: int) -> str:
        """Выбирает в списке срока ближайший вариант, не меньший days. Возвращает подпись варианта."""
        root = self._wait_visible('.v-select.duration-select')
        self.driver.execute_script('arguments[0].scrollIntoView({block: "center"})', root)
        self._click(root.find_element(By.CSS_SELECTOR, '.vs__dropdown-toggle'),
                    lambda d: d.find_elements(By.CSS_SELECTOR, '.vs__dropdown-option'))
        # .text пуст у пунктов вне видимой области меню, поэтому читаем textContent
        options = [(o, _duration_days(o.get_attribute('textContent')))
                   for o in self.driver.find_elements(By.CSS_SELECTOR, '.vs__dropdown-option')]
        options = [(o, n) for o, n in options if n]
        if not options:
            raise ParserError('Не удалось прочитать варианты срока выполнения')
        option, _ = min(((o, n) for o, n in options if n >= days), key=lambda x: x[1], default=max(options, key=lambda x: x[1]))
        label = option.get_attribute('textContent').strip()
        self.driver.execute_script('arguments[0].scrollIntoView({block: "nearest"})', option)
        self._click(option, lambda d: 'vs--open' not in (root.get_attribute('class') or ''))
        self._pause()
        return label

    def _fill_stages(self, stages: list[tuple[str, int]]) -> None:
        # Kwork показывает 3 пустые задачи; недостающие добавляем, лишние удаляем
        for i, (name, price) in enumerate(stages):
            rows = self.driver.find_elements(By.CSS_SELECTOR, '.stages__stage')
            if i >= len(rows):
                add = self._wait_visible('.stages__add-stage')
                if 'disabled' in (add.get_attribute('class') or ''):
                    raise ParserError(f'Kwork не даёт добавить задачу №{i + 1}: слишком много задач или не хватает суммы')
                self.driver.execute_script('arguments[0].click()', add)
                WebDriverWait(self.driver, 5).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, '.stages__stage')) > i)
                rows = self.driver.find_elements(By.CSS_SELECTOR, '.stages__stage')
            row = rows[i]
            self._set_editor(row.find_element(By.CSS_SELECTOR, 'textarea[name^="stageTitle"]'), name)
            self._type_number(row.find_element(By.CSS_SELECTOR, '.stages__stage-price-input'), price)
        for row in reversed(self.driver.find_elements(By.CSS_SELECTOR, '.stages__stage')[len(stages):]):
            delete = row.find_elements(By.CSS_SELECTOR, '.stages__stage-controls-delete')
            if delete:
                self.driver.execute_script('arguments[0].click()', delete[0])
                self._pause()

    def _title_field(self) -> WebElement | None:
        """Textarea «Название заказа», если её редактор сейчас виден (сама textarea всегда скрыта)."""
        for textarea in self.driver.find_elements(By.CSS_SELECTOR, 'textarea[placeholder="Введите название заказа"]'):
            boxes = textarea.find_elements(By.XPATH, './ancestor::div[contains(@class, "trumbowyg-box")][1]')
            if boxes and boxes[0].is_displayed():
                return textarea
        return None

    def _wait_payment_or_title(self) -> list[WebElement]:
        """После ввода цены ждёт блок порядка оплаты или сразу поле названия. [] — выбора оплаты нет."""
        def probe(d):
            items = [e for e in d.find_elements(By.CSS_SELECTOR, '.offer-payment-type__item') if e.is_displayed()]
            if items:
                return 'payment', items
            if self._title_field():
                return 'title', []
            return None
        try:
            WebDriverWait(self.driver, 10).until(probe)
        except TimeoutException:
            raise ParserError('После ввода цены не появился ни выбор оплаты, ни поле названия заказа')
        time.sleep(0.5)  # форма перерисовывается после ввода цены — перепроверяем уже устоявшееся состояние
        return (probe(self.driver) or ('title', []))[1]

    def _form_errors(self) -> list[str]:
        return [e.text.strip() for e in self._visible('.form-item__error, [class*="error-text"], .kw-error')
                if e.text.strip()]

    def send_offer(self, project_id: int, offer: Offer, dry_run: bool = False) -> str:
        """Заполняет и отправляет форму отклика. dry_run — заполнить, сделать скриншот и не отправлять."""
        if not self.login():
            raise ParserError('Нет авторизации на Kwork. Проверьте KWORK_EMAIL/KWORK_PASSWORD '
                              'или выполните `python parser.py --login`.')
        driver = self._ensure_driver()
        driver.get(config.OFFER_URL.format(project_id=project_id))

        try:
            # Сама textarea скрыта редактором trumbowyg, поэтому ждём присутствия, а не видимости
            field = WebDriverWait(driver, 20).until(lambda d: next(iter(
                d.find_elements(By.CSS_SELECTOR, 'textarea[name="description"]')), None))
        except TimeoutException:
            shot = self._screenshot(f'offer_{project_id}_no_form')
            raise ParserError(f'Форма отклика не найдена (url={driver.current_url}, скриншот: {shot}). '
                              'Возможно, отклик уже отправлен или закончились коннекты.')

        try:
            # 1. Описание
            self._set_editor(field, offer.text)

            # 2. Цена — после неё появляется выбор порядка оплаты
            price_input = self._wait_visible('#offer-custom-price')
            if m := re.fullmatch(r'\s*([\d\s]+)\s*-\s*([\d\s]+)\s*', price_input.get_attribute('placeholder') or ''):
                low, high = (int(re.sub(r'\s', '', g)) for g in m.groups())
                if not low <= offer.price <= high:
                    raise ParserError(f'Цена {offer.price} ₽ вне допустимого диапазона {low}–{high} ₽')
            self._type_number(price_input, offer.price)

            # 3. Порядок оплаты. Kwork показывает выбор только с определённой цены отклика
            #    (getPriceThreshold проекта, обычно 4000 ₽), иначе сразу поле названия
            payment, notes = offer.payment, []
            if items := self._wait_payment_or_title():
                item = items[1 if payment == PAYMENT_STAGES else 0]
                driver.execute_script('arguments[0].scrollIntoView({block: "center"})', item)
                self._click(item, lambda d: 'active' in (item.get_attribute('class') or ''))
                self._pause()
            else:
                if payment == PAYMENT_STAGES:
                    if offer.payment_explicit:
                        raise ParserError(f'Kwork не предлагает оплату частями при цене {offer.price} ₽ '
                                          '(выбор появляется только с более высокой цены)')
                    payment = PAYMENT_FULL
                notes.append('выбора оплаты не было')

            # 4. Название заказа (целиком) или задачи (по частям)
            if payment == PAYMENT_STAGES:
                self._wait_visible('.stages__stage')
                self._fill_stages(offer.stages)
            else:
                title = WebDriverWait(driver, 10).until(lambda d: self._title_field())
                self._set_editor(title, offer.title)

            # 5. Срок
            duration = self._select_duration(offer.days)
            notes.insert(0, f'срок: {duration}')
        except (TimeoutException, WebDriverException) as e:
            shot = self._screenshot(f'offer_{project_id}_fill_failed')
            raise ParserError(f'Не удалось заполнить форму: {str(e).splitlines()[0][:200]}. Скриншот: {shot}')
        except ParserError as e:
            shot = self._screenshot(f'offer_{project_id}_fill_failed')
            raise ParserError(f'{e}. Скриншот: {shot}')

        if errors := self._form_errors():
            shot = self._screenshot(f'offer_{project_id}_invalid')
            raise ParserError(f'Kwork показывает ошибки в форме: {"; ".join(errors[:3])}. Скриншот: {shot}')

        if dry_run:
            shot = self._screenshot(f'offer_{project_id}_dry_run')
            return f'Форма заполнена без отправки ({", ".join(notes)}). Скриншот: {shot}'

        buttons = [b for b in self._visible('button') if b.is_enabled() and b.text.strip() == 'Предложить']
        if not buttons:
            shot = self._screenshot(f'offer_{project_id}_no_button')
            raise ParserError(f'Кнопка «Предложить» не найдена или неактивна (скриншот: {shot})')
        start_url = driver.current_url
        driver.execute_script('arguments[0].scrollIntoView({block: "center"})', buttons[-1])
        driver.execute_script('arguments[0].click()', buttons[-1])

        try:
            WebDriverWait(driver, 20).until(
                lambda d: d.current_url != start_url
                or re.search(r'предложение (успешно )?отправлено|ваше предложение',
                             d.find_element(By.TAG_NAME, 'body').text, re.I)
            )
        except TimeoutException:
            errors = self._form_errors() or [e.text.strip() for e in self._visible('[class*="error"]') if e.text.strip()]
            shot = self._screenshot(f'offer_{project_id}_failed')
            raise ParserError('Kwork не подтвердил отправку. '
                              + (f'Ошибки формы: {"; ".join(errors[:3])}. ' if errors else '')
                              + f'Скриншот: {shot}')
        return ', '.join(notes)


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
    browser = KworkBrowser(headless=False, background=False)
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
    elif '--dry-run-offer' in sys.argv:
        # python parser.py --dry-run-offer <project_id> [файл с текстом отклика]
        # Заполняет форму отклика и сохраняет скриншот в logs/, НЕ нажимая «Предложить».
        from offer import parse_offer
        args = sys.argv[sys.argv.index('--dry-run-offer') + 1:]
        pid = int(args[0])
        raw = Path(args[1]).read_text(encoding='utf-8') if len(args) > 1 else (
            'Здравствуйте! Это тестовое заполнение формы отклика, отправка не выполняется. ' * 3)
        b = KworkBrowser()
        try:
            projects, _ = b.fetch_page(1)
            project = next((p for p in projects if p['id'] == pid), {'id': pid, 'title': 'Тестовый заказ', 'price': ''})
            print(b.send_offer(pid, parse_offer(raw, project), dry_run=True))
        finally:
            b.close()
    else:
        b = KworkBrowser()
        try:
            items, last = b.fetch_page(1)
            print(f'Страница 1 из {last}, проектов: {len(items)}')
            for it in items:
                print(it['id'], it['price'], it['title'], f'файлов: {len(it["files"])}')
        finally:
            b.close()
