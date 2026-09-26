import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message

import config
import database as db
from keyboards import cancel_kb
from parser import Monitor, ParserError
from states import RespondState

logger = logging.getLogger(__name__)

router = Router()
# Ботом могут пользоваться только админы из config.LIST_ADMINS_ID
router.message.filter(F.from_user.id.in_(config.LIST_ADMINS_ID))
router.callback_query.filter(F.from_user.id.in_(config.LIST_ADMINS_ID))

HELP = (
    '🤖 <b>Мониторинг проектов Kwork</b>\n\n'
    '/start_work — запустить мониторинг\n'
    '/stop_work — остановить мониторинг\n'
    '/status — состояние\n'
    '/cancel — отменить ввод отклика\n\n'
    'В тексте отклика можно указать отдельными строками <code>Цена: 5000</code> '
    'и <code>Срок: 3</code> — иначе цена берётся из проекта, срок — '
    f'{config.OFFER_DEFAULT_DAYS} дн.'
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(HELP)


# Telegram не подсвечивает команды с дефисом, но текст "/start-work" всё равно придёт — поддерживаем оба варианта
@router.message(Command('start_work', 'start-work'))
async def cmd_start_work(message: Message, monitor: Monitor):
    if await monitor.start(message.chat.id):
        await message.answer(
            '▶️ Мониторинг запущен. Проверка каждые '
            f'{config.PARSE_INTERVAL_MIN:.0f}–{config.PARSE_INTERVAL_MAX:.0f} сек.'
        )
    else:
        await message.answer('Мониторинг уже работает.')


@router.message(Command('stop_work', 'stop-work', 'stop_kwork', 'stop-kwork'))
async def cmd_stop_work(message: Message, monitor: Monitor):
    wait = await message.answer('⏳ Останавливаю мониторинг...')
    if await monitor.stop():
        await wait.edit_text('⏹ Мониторинг остановлен, браузер закрыт.')
    else:
        await wait.edit_text('Мониторинг не был запущен.')


@router.message(Command('status'))
async def cmd_status(message: Message, monitor: Monitor):
    await message.answer('🟢 Мониторинг работает' if monitor.running else '🔴 Мониторинг остановлен')


# ---------- игнорирование ----------

@router.callback_query(F.data.startswith('ignore_'))
async def cb_ignore(callback: CallbackQuery, bot: Bot):
    project_id = int(callback.data.removeprefix('ignore_'))
    project = await db.get_project(project_id)
    await db.set_status(project_id, db.STATUS_IGNORED)

    message_ids = set(project['message_ids'] if project else [])
    message_ids.add(callback.message.message_id)
    try:
        await bot.delete_messages(callback.message.chat.id, sorted(message_ids))
    except TelegramBadRequest:
        # Сообщения старше 48 часов удалить нельзя — просто помечаем
        await callback.message.edit_text(
            f'🗑 <s>{html.escape(project["title"] if project else str(project_id))}</s> — проигнорировано'
        )
    await callback.answer('Проект проигнорирован')


# ---------- отклик ----------

@router.callback_query(F.data.startswith('respond_'))
async def cb_respond(callback: CallbackQuery, state: FSMContext):
    project_id = int(callback.data.removeprefix('respond_'))
    project = await db.get_project(project_id)
    if project is None:
        await callback.answer('Проект не найден в базе', show_alert=True)
        return
    if project['status'] == db.STATUS_RESPONDED:
        await callback.answer('Отклик на этот проект уже отправлен', show_alert=True)
        return

    await state.set_state(RespondState.waiting_text)
    await state.update_data(project_id=project_id, source_message_id=callback.message.message_id)
    await callback.message.answer(
        f'💬 Введите текст отклика на проект «{html.escape(project["title"])}»:',
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == 'cancel_respond')
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text('Отклик отменён.')
    await callback.answer()


@router.message(Command('cancel'), StateFilter('*'))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Отменено.')


def _extract_overrides(text: str, default_price: int | None) -> tuple[str, int | None, int]:
    """Вырезает из текста строки «Цена: N» и «Срок: N»."""
    price, days = default_price, config.OFFER_DEFAULT_DAYS
    lines = []
    for line in text.splitlines():
        if m := re.fullmatch(r'\s*цена\s*:\s*([\d\s]+)\s*(₽|руб\.?)?\s*', line, re.I):
            price = int(re.sub(r'\s', '', m.group(1)))
        elif m := re.fullmatch(r'\s*срок\s*:\s*(\d+)\s*(дн\.?|дней|дня|день)?\s*', line, re.I):
            days = int(m.group(1))
        else:
            lines.append(line)
    return '\n'.join(lines).strip(), price, days


@router.message(RespondState.waiting_text, F.text)
async def respond_text(message: Message, state: FSMContext, monitor: Monitor, bot: Bot):
    data = await state.get_data()
    project_id = data['project_id']
    project = await db.get_project(project_id)

    m = re.match(r'[\d\s]+', project['price'] if project else '')
    default_price = int(re.sub(r'\s', '', m.group())) if m and m.group().strip() else None
    text, price, days = _extract_overrides(message.text, default_price)
    if not text:
        await message.answer('Текст отклика пустой, введите ещё раз или /cancel.')
        return

    await state.clear()
    wait = await message.answer('⏳ Отправляю отклик через браузер...')
    try:
        await monitor.run_in_browser(monitor.browser.send_offer, project_id, text, price, days)
    except ParserError as e:
        await wait.edit_text(f'❌ Не удалось отправить отклик: {html.escape(str(e))}')
        return
    except Exception as e:
        logger.exception('Ошибка отправки отклика на %s', project_id)
        await wait.edit_text(f'❌ Ошибка браузера: {html.escape(str(e))[:500]}')
        return
    finally:
        if not monitor.running:  # браузер открывался только ради отклика
            await monitor.run_in_browser(monitor.browser.close)

    await db.set_status(project_id, db.STATUS_RESPONDED)
    await wait.edit_text(f'✅ Отклик отправлен (цена: {price} ₽, срок: {days} дн.)')
    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=data['source_message_id'],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='✅ Отклик отправлен · открыть', url=project['url']),
            ]]),
        )
    except TelegramBadRequest:
        pass


@router.message(RespondState.waiting_text)
async def respond_not_text(message: Message):
    await message.answer('Нужен текст отклика. Или /cancel.')
