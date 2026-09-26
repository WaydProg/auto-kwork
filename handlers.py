import html
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Message

import config
import database as db
from ai_service import AIService, AIServiceError
from keyboards import ai_preview_kb, cancel_kb
from offer import OfferError, parse_offer
from parser import Monitor, ParserError
from states import RespondState

logger = logging.getLogger(__name__)

router = Router()
_submitting: set[int] = set()  # проекты, отклик на которые отправляется прямо сейчас
# Ботом могут пользоваться только админы из config.LIST_ADMINS_ID
router.message.filter(F.from_user.id.in_(config.LIST_ADMINS_ID))
router.callback_query.filter(F.from_user.id.in_(config.LIST_ADMINS_ID))

PARAMS_HELP = (
    'Параметры отклика задаются отдельными строками в тексте (все необязательны):\n'
    '<code>Цена: 5000</code> — по умолчанию бюджет проекта\n'
    f'<code>Срок: 3</code> — по умолчанию {config.OFFER_DEFAULT_DAYS} дн. '
    '(выбирается ближайший вариант Kwork)\n'
    '<code>Название: Доработка бота</code> — по умолчанию заголовок проекта, до 70 символов\n'
    '<code>Оплата: целиком</code> или <code>Оплата: частями</code>\n'
    '<code>Задача: Разработка — 3000</code> — для оплаты частями, минимум 2 задачи; '
    'цена = сумма задач'
)

HELP = (
    '🤖 <b>Мониторинг проектов Kwork</b>\n\n'
    '/start_work — запустить мониторинг\n'
    '/stop_work — остановить мониторинг\n'
    '/status — состояние\n'
    '/cancel — отменить ввод или правку отклика\n\n'
    '✨ <b>Автоотклик</b> генерирует текст через ИИ: его можно отредактировать, '
    'перегенерировать или сразу отправить.\n\n'
    + PARAMS_HELP
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
async def cmd_cancel(message: Message, state: FSMContext, bot: Bot):
    if await state.get_state() == RespondState.editing_ai_draft:
        # Правка отменена — возвращаем кнопки прежнему предпросмотру
        data = await state.get_data()
        try:
            await bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=data['preview_message_id'],
                                                reply_markup=ai_preview_kb(data['project_id']))
        except TelegramBadRequest:
            pass
    await state.clear()
    await message.answer('Отменено.')


async def _submit_offer(project: dict, raw_text: str, status_msg: Message, monitor: Monitor, bot: Bot,
                        source_message_id: int | None) -> bool:
    """Отправляет отклик через Selenium и показывает результат в status_msg."""
    project_id = project['id']
    if project_id in _submitting:
        await status_msg.edit_text('⏳ Отклик на этот проект уже отправляется, подождите.')
        return False
    try:
        offer = parse_offer(raw_text, project)
    except OfferError as e:
        await status_msg.edit_text(f'⚠️ {html.escape(str(e))}')
        return False

    _submitting.add(project_id)
    try:
        result = await monitor.run_in_browser(monitor.browser.send_offer, project_id, offer)
    except ParserError as e:
        await status_msg.edit_text(f'❌ Не удалось отправить отклик: {html.escape(str(e))}')
        return False
    except Exception as e:
        logger.exception('Ошибка отправки отклика на %s', project_id)
        await status_msg.edit_text(f'❌ Ошибка браузера: {html.escape(str(e))[:500]}')
        return False
    finally:
        _submitting.discard(project_id)
        if not monitor.running:  # браузер открывался только ради отклика
            await monitor.run_in_browser(monitor.browser.close)

    await db.set_status(project_id, db.STATUS_RESPONDED)
    await db.set_draft(project_id, None)
    await status_msg.edit_text(
        f'✅ Отклик на «{html.escape(project["title"])}» отправлен ({html.escape(result)})\n\n'
        + offer.summary_html()
    )
    if source_message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=status_msg.chat.id,
                message_id=source_message_id,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text='✅ Отклик отправлен · открыть', url=project['url']),
                ]]),
            )
        except TelegramBadRequest:
            pass
    return True


def _notification_id(project: dict) -> int | None:
    """Сообщение с карточкой проекта — последнее из отправленных при уведомлении."""
    return project['message_ids'][-1] if project.get('message_ids') else None


@router.message(RespondState.waiting_text, F.text)
async def respond_text(message: Message, state: FSMContext, monitor: Monitor, bot: Bot):
    data = await state.get_data()
    project = await db.get_project(data['project_id'])
    if project is None:
        await state.clear()
        await message.answer('Проект не найден в базе.')
        return
    try:
        parse_offer(message.text, project)
    except OfferError as e:  # остаёмся в состоянии ввода, чтобы можно было прислать исправленный текст
        await message.answer(f'⚠️ {html.escape(str(e))}\n\nОтправьте текст ещё раз или /cancel.')
        return

    await state.clear()
    wait = await message.answer('⏳ Отправляю отклик через браузер...')
    await _submit_offer(project, message.text, wait, monitor, bot, data['source_message_id'])


# ---------- AI-отклик ----------

def _preview_text(project: dict, draft: str, note: str = '') -> str:
    header = (f'📌 <b>Проект:</b> <a href="{html.escape(project["url"])}">{html.escape(project["title"])}</a>\n'
              f'Бюджет: {html.escape(project["price"] or "")}\n\n'
              f'✨ <b>{note or "Сгенерированный отклик"}:</b>\n')
    try:
        params = '<b>Будет отправлено:</b>\n' + parse_offer(draft, project).summary_html()
    except OfferError as e:
        params = f'⚠️ <b>Отправить пока нельзя:</b> {html.escape(str(e))}'
    footer = ('\n\n<i>Нажмите на текст, чтобы скопировать. Параметры меняются строками в тексте: '
              '«Цена: 5000», «Срок: 3», «Название: …», «Оплата: частями», «Задача: Название — 3000». '
              'Подробнее — /start</i>')
    body = html.escape(draft[:2500])
    return f'{header}<code>{body}</code>\n\n{params}{footer}'


async def _get_active_project(callback: CallbackQuery, prefix: str) -> dict | None:
    project = await db.get_project(int(callback.data.removeprefix(prefix)))
    if project is None:
        await callback.answer('Проект не найден в базе', show_alert=True)
        return None
    if project['status'] == db.STATUS_RESPONDED:
        await callback.answer('Отклик на этот проект уже отправлен', show_alert=True)
        return None
    return project


async def _generate_into(msg: Message, project: dict, ai: AIService, bot: Bot, regenerate: bool) -> None:
    """Генерирует отклик и показывает предпросмотр в сообщении msg."""
    await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    try:
        draft = await ai.generate_response(project, regenerate=regenerate)
    except AIServiceError as e:
        await msg.edit_text(
            f'❌ {html.escape(str(e))}',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text='🔄 Попробовать снова', callback_data=f'regenerate_ai_{project["id"]}')]]),
        )
        return
    await db.set_draft(project['id'], draft)
    await msg.edit_text(_preview_text(project, draft), reply_markup=ai_preview_kb(project['id']),
                        disable_web_page_preview=True)


@router.callback_query(F.data.startswith('ai_respond_'))
async def cb_ai_respond(callback: CallbackQuery, ai: AIService, bot: Bot):
    project = await _get_active_project(callback, 'ai_respond_')
    if project is None:
        return
    await callback.answer('Генерирую отклик…')
    wait = await callback.message.answer(
        f'⏳ Генерирую отклик на «{html.escape(project["title"])}»…'
    )
    await _generate_into(wait, project, ai, bot, regenerate=False)


@router.callback_query(F.data.startswith('regenerate_ai_'))
async def cb_ai_regenerate(callback: CallbackQuery, ai: AIService, bot: Bot):
    project = await _get_active_project(callback, 'regenerate_ai_')
    if project is None:
        return
    await callback.answer('Генерирую новый вариант…')
    await callback.message.edit_text(f'⏳ Генерирую новый вариант отклика на «{html.escape(project["title"])}»…')
    await _generate_into(callback.message, project, ai, bot, regenerate=True)


@router.callback_query(F.data.startswith('confirm_ai_'))
async def cb_ai_confirm(callback: CallbackQuery, state: FSMContext, monitor: Monitor, bot: Bot):
    project = await _get_active_project(callback, 'confirm_ai_')
    if project is None:
        return
    if not project.get('draft'):
        await callback.answer('Черновик не найден — сгенерируйте отклик заново', show_alert=True)
        return
    await state.clear()
    await callback.answer('Отправляю…')
    # Предпросмотр остаётся с текстом, но без кнопок — чтобы не нажать дважды
    await callback.message.edit_reply_markup(reply_markup=None)
    wait = await callback.message.answer('⏳ Отправляю отклик через браузер...')
    ok = await _submit_offer(project, project['draft'], wait, monitor, bot, _notification_id(project))
    if not ok:  # вернуть кнопки, чтобы можно было повторить или поправить текст
        try:
            await callback.message.edit_reply_markup(reply_markup=ai_preview_kb(project['id']))
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith('edit_ai_'))
async def cb_ai_edit(callback: CallbackQuery, state: FSMContext):
    project = await _get_active_project(callback, 'edit_ai_')
    if project is None:
        return
    await state.set_state(RespondState.editing_ai_draft)
    await state.update_data(project_id=project['id'], preview_message_id=callback.message.message_id)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        '✏️ Отправьте исправленный текст отклика одним сообщением '
        '(текущий вариант можно скопировать нажатием выше). /cancel — отменить правку.'
    )
    await callback.answer()


@router.message(RespondState.editing_ai_draft, F.text)
async def ai_edit_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    project = await db.get_project(data['project_id'])
    await state.clear()
    if project is None:
        await message.answer('Проект не найден в базе.')
        return
    text = message.text.strip()
    try:
        parse_offer(text, project)
    except OfferError as e:  # остаёмся в режиме правки
        await state.set_state(RespondState.editing_ai_draft)
        await state.update_data(**data)
        await message.answer(f'⚠️ {html.escape(str(e))}\n\nОтправьте исправленный текст ещё раз или /cancel.')
        return

    await db.set_draft(project['id'], text)
    try:  # старый предпросмотр больше не актуален
        await bot.edit_message_text(
            chat_id=message.chat.id, message_id=data['preview_message_id'],
            text='✏️ Отклик изменён — актуальная версия ниже.',
        )
    except TelegramBadRequest:
        pass
    await message.answer(_preview_text(project, text, note='Изменённый отклик'),
                         reply_markup=ai_preview_kb(project['id']), disable_web_page_preview=True)


@router.callback_query(F.data.startswith('cancel_ai_'))
async def cb_ai_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await db.set_draft(int(callback.data.removeprefix('cancel_ai_')), None)
    await callback.message.edit_text('❌ Автоотклик отменён. Карточка проекта осталась выше.')
    await callback.answer()


@router.message(StateFilter(RespondState.waiting_text, RespondState.editing_ai_draft))
async def respond_not_text(message: Message):
    await message.answer('Нужен текст отклика. Или /cancel.')
