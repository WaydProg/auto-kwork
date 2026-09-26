from aiogram.fsm.state import State, StatesGroup


class RespondState(StatesGroup):
    waiting_text = State()     # ручной отклик
    editing_ai_draft = State()  # правка сгенерированного отклика
    editing_price = State()     # ввод цены для черновика
    editing_title = State()     # ввод названия заказа для черновика
