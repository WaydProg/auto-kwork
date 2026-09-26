from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


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
    kb.button(text='✏️ Изменить отклик', callback_data=f'edit_ai_{project_id}')
    kb.button(text='🔄 Перегенерировать', callback_data=f'regenerate_ai_{project_id}')
    kb.button(text='❌ Отмена', callback_data=f'cancel_ai_{project_id}')
    kb.adjust(1, 2, 1)
    return kb.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text='✖️ Отмена', callback_data='cancel_respond')
    return kb.as_markup()
