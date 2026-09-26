import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

import database as db
from config import BOT_TOKEN
from handlers import router
from parser import Monitor


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    monitor = Monitor(bot)
    dp['monitor'] = monitor  # попадает в хендлеры как аргумент `monitor`
    dp.include_router(router)

    await db.init_db()
    await bot.set_my_commands([
        BotCommand(command='start_work', description='Запустить мониторинг'),
        BotCommand(command='stop_work', description='Остановить мониторинг'),
        BotCommand(command='status', description='Состояние мониторинга'),
        BotCommand(command='cancel', description='Отменить ввод отклика'),
    ])
    try:
        await dp.start_polling(bot)
    finally:
        await monitor.shutdown()
        await bot.session.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info('exit')
