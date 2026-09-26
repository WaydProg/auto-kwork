from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from offer import DURATION_OPTIONS


def project_kb(project_id: int, url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text='🔗 Открыть на Kwork', url=url)
    kb.button(text='💬 Откликнуться (вручную)', callback_data=f'respond_{project_id}')
    kb.button(text='✨ Сгенерировать автоотклик', callback_data=f'ai_respond_{project_id}')
    kb.button(text='🗑 Удалить/Игнорировать', callback_data=f'ignore_{project_id}')
    kb.adjust(1)
    return kb.as_markup()


def ai_preview_kb(project_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text='✅ Подтвердить и отправить', callback_data=f'confirm_ai_{project_id}')
    kb.button(text='💰 Цена', callback_data=f'price_ai_{project_id}')
    kb.button(text='⏱ Срок', callback_data=f'days_ai_{project_id}')
    kb.button(text='📝 Название', callback_data=f'title_ai_{project_id}')
    kb.button(text='✏️ Изменить отклик', callback_data=f'edit_ai_{project_id}')
    kb.button(text='🔄 Перегенерировать', callback_data=f'regenerate_ai_{project_id}')
    kb.button(text='❌ Отмена', callback_data=f'cancel_ai_{project_id}')
    kb.adjust(1, 3, 2, 1)
    return kb.as_markup()


def days_kb(project_id: int, current: int) -> InlineKeyboardMarkup:
    """Варианты срока — те же, что в форме Kwork."""
    kb = InlineKeyboardBuilder()
    for days, label in DURATION_OPTIONS:
        kb.button(text=f'• {label} •' if days == current else label, callback_data=f'setdays_ai_{project_id}_{days}')
    kb.button(text='← Назад', callback_data=f'back_ai_{project_id}')
    kb.adjust(4, 4, 4, 1)
    return kb.as_markup()


def title_kb(project_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text='↩️ Как в проекте', callback_data=f'resettitle_ai_{project_id}')
    kb.button(text='← Назад', callback_data=f'back_ai_{project_id}')
    kb.adjust(2)
    return kb.as_markup()


def price_kb(project_id: int, suggestions: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for price in suggestions:
        kb.button(text=f'{price:,} ₽'.replace(',', ' '), callback_data=f'setprice_ai_{project_id}_{price}')
    kb.button(text='← Назад', callback_data=f'back_ai_{project_id}')
    kb.adjust(min(len(suggestions), 3) or 1, 1)
    return kb.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text='✖️ Отмена', callback_data='cancel_respond')
    return kb.as_markup()
