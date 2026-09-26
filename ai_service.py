"""Генерация откликов на проекты Kwork через OpenRouter API."""
import asyncio
import logging
import re

import httpx

import config

logger = logging.getLogger(__name__)

API_URL = 'https://openrouter.ai/api/v1/chat/completions'
MAX_DESCRIPTION_CHARS = 4000
RETRY_STATUSES = {429, 500, 502, 503, 504}

SYSTEM_PROMPT = '''Ты — опытный фрилансер на бирже Kwork. Ты пишешь отклики на проекты заказчиков.

Правила отклика:
- Пиши на русском языке, вежливо и по делу, без канцелярита и шаблонных фраз вроде «Я внимательно ознакомился с вашим проектом».
- Объём: 400–900 символов, 2–4 коротких абзаца.
- Начни с «Здравствуйте!».
- Покажи, что понял задачу: упомяни 1–2 конкретные детали из описания проекта.
- Коротко опиши, как будешь выполнять работу или какой релевантный опыт есть.
- Задай один уточняющий вопрос по задаче.
- Не выдумывай факты: названия компаний, ссылки на портфолио, отзывы, точные цифры опыта.
- Не указывай цену и сроки: их передают отдельно.
- Без Markdown, списков, эмодзи и заполнителей вроде [Имя].
- В ответе верни только текст отклика, без пояснений.'''

USER_PROMPT = '''Проект на Kwork.

Заголовок: {title}
Бюджет: {price}

Описание:
{description}
{profile}
Напиши отклик на этот проект.'''


class AIServiceError(Exception):
    """Ошибка с понятным пользователю текстом."""


def _clean(text: str) -> str:
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.S | re.I)  # рассуждения reasoning-моделей
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # остатки Markdown
    text = text.strip().strip('"«»').strip()
    return re.sub(r'\n{3,}', '\n\n', text)


class AIService:
    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.OPENROUTER_TIMEOUT, connect=10),
            headers={
                'Authorization': f'Bearer {config.OPENROUTER_API_KEY}',
                # Необязательные заголовки для статистики OpenRouter
                'HTTP-Referer': 'https://github.com/auto-kwork',
                'X-Title': 'auto-kwork bot',
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _build_messages(project: dict) -> list[dict]:
        description = project.get('description') or ''
        if len(description) > MAX_DESCRIPTION_CHARS:
            description = description[:MAX_DESCRIPTION_CHARS] + '…'
        profile = (f'\nО фрилансере (используй только эти факты об опыте):\n{config.FREELANCER_PROFILE}\n'
                   if config.FREELANCER_PROFILE else '')
        return [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': USER_PROMPT.format(
                title=project['title'], price=project.get('price') or 'не указан',
                description=description or '(описание отсутствует)', profile=profile,
            )},
        ]

    async def generate_response(self, project: dict, *, regenerate: bool = False) -> str:
        if not config.OPENROUTER_API_KEY:
            raise AIServiceError('Не задан OPENROUTER_API_KEY в .env')

        payload = {
            # Основная модель и запасные: OpenRouter сам переключится, если модель недоступна
            'models': [config.OPENROUTER_MODEL, *config.OPENROUTER_FALLBACK_MODELS][:3],
            'messages': self._build_messages(project),
            'temperature': 0.95 if regenerate else 0.7,
            'max_tokens': 800,
        }

        last_error = 'неизвестная ошибка'
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2 * attempt)
            try:
                resp = await self._client.post(API_URL, json=payload)
            except httpx.TimeoutException:
                last_error = 'OpenRouter не ответил вовремя'
                continue
            except httpx.HTTPError as e:
                last_error = f'сетевая ошибка: {e}'
                continue

            if resp.status_code in RETRY_STATUSES:
                last_error = ('бесплатная модель перегружена или исчерпан лимит запросов'
                              if resp.status_code == 429 else f'OpenRouter вернул HTTP {resp.status_code}')
                logger.warning('OpenRouter %s: %s', resp.status_code, resp.text[:300])
                continue
            if resp.status_code == 401:
                raise AIServiceError('Неверный OPENROUTER_API_KEY')
            if resp.status_code == 402:
                raise AIServiceError('На аккаунте OpenRouter недостаточно кредитов для этой модели')
            if resp.status_code >= 400:
                raise AIServiceError(f'OpenRouter вернул HTTP {resp.status_code}: {resp.text[:200]}')

            data = resp.json()
            if 'error' in data:  # ошибка провайдера может прийти и с HTTP 200
                last_error = str(data['error'].get('message', data['error']))[:200]
                logger.warning('OpenRouter error: %s', data['error'])
                continue
            try:
                text = _clean(data['choices'][0]['message']['content'] or '')
            except (KeyError, IndexError, TypeError):
                last_error = 'неожиданный формат ответа'
                continue
            if len(text) < 50:
                last_error = 'модель вернула пустой или слишком короткий ответ'
                continue
            logger.info('Отклик сгенерирован моделью %s', data.get('model'))
            return text

        raise AIServiceError(f'Не удалось сгенерировать отклик: {last_error}. Попробуйте ещё раз позже.')
