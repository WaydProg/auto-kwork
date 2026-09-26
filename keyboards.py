from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def project_kb(project_id: int, url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text='🔗 Открыть на Kwork', url=url)
    kb.button(text='💬 Откликнуться', callback_data=f'respond_{project_id}')
    kb.button(text='🗑 Удалить/Игнорировать', callback_data=f'ignore_{project_id}')
    kb.adjust(1, 2)
    return kb.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text='✖️ Отмена', callback_data='cancel_respond')
    return kb.as_markup()
