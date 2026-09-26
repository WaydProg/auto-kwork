from aiogram.fsm.state import State, StatesGroup


class RespondState(StatesGroup):
    waiting_text = State()
