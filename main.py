import asyncio
import logging
import html
import uuid
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, PreCheckoutQuery, LabeledPrice
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from google import genai

import config
import database

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера (новый синтаксис aiogram 3.7.0+)
bot = Bot(
    token=config.BOT_TOKEN, 
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
router = Router()

# Настройка нового Gemini API клиента
gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)

# FSM Состояния
class DiplomatState(StatesGroup):
    waiting_for_text = State()
    text_saved = State()

# --- КЛАВИАТУРЫ ---

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✍️ Новый текст"), KeyboardButton(text="💎 Купить безлимит")],
            [KeyboardButton(text="📊 Мой статус"), KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )

def get_styles_keyboard():
    builder = []
    row = []
    for key, data in config.AI_MODES.items():
        row.append(InlineKeyboardButton(text=data["btn"], callback_data=f"style_{key}"))
        if len(row) == 2:
            builder.append(row)
            row = []
    if row:
        builder.append(row)
    return InlineKeyboardMarkup(inline_keyboard=builder)

def get_post_generation_keyboard(is_premium: bool):
    buttons = [[InlineKeyboardButton(text="🔁 Выбрать другой стиль", callback_data="reselect_style")]]
    if not is_premium:
        buttons.append([InlineKeyboardButton(text="💎 Купить безлимит", callback_data="buy_premium")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_paywall_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Купить безлимит (50 Stars)", callback_data="buy_premium")]
    ])

# --- MIDDLEWARE / УТИЛИТЫ ---

async def check_user(user_id: int):
    database.create_user_if_not_exists(user_id)
    return database.get_user(user_id)

# --- ОБРАБОТЧИКИ КОМАНД ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await check_user(message.from_user.id)
    await state.clear()
    await message.answer(config.TEXT_START, reply_markup=get_main_keyboard())

@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def cmd_help(message: Message):
    await message.answer(config.TEXT_HELP, reply_markup=get_main_keyboard())

@router.message(Command("status"))
@router.message(F.text == "📊 Мой статус")
async def cmd_status(message: Message):
    user = await check_user(message.from_user.id)
    status_text = "💎 <b>Premium (Безлимит)</b>" if user['is_premium'] else "🆓 <b>Бесплатный тариф</b>"
    
    text = (
        f"📊 <b>Ваш статус:</b>\n\n"
        f"Тариф: {status_text}\n"
        f"Использовано запросов: <b>{user['total_requests']}</b>\n"
    )
    
    if not user['is_premium']:
        text += f"Осталось бесплатных попыток: <b>{user['free_requests']}</b>\n\n"
        text += "<i>Premium даёт безлимитный доступ навсегда. Вы забудете о лимитах и сможете переписывать любые рабочие сообщения.</i>"
        await message.answer(text, reply_markup=get_paywall_keyboard())
    else:
        await message.answer(text)

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != config.ADMIN_ID:
        return
    stats = database.get_stats()
    text = (
        "🔧 <b>Админ-панель</b>\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"💎 Premium пользователей: {stats['premium_users']}\n"
        f"📝 Всего генераций: {stats['total_generations']}\n"
        f"💳 Успешных оплат: {stats['total_payments']}\n"
        f"💰 Доход: {stats['total_revenue_stars']} Stars"
    )
    await message.answer(text)

# --- ОБРАБОТКА ТЕКСТА ---

@router.message(F.text == "✍️ Новый текст")
async def btn_new_text(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отправьте мне текст, который нужно переписать ✍️")

@router.message(F.text)
async def process_user_text(message: Message, state: FSMContext):
    # Игнорируем команды и кнопки
    if message.text.startswith('/') or message.text in ["✍️ Новый текст", "💎 Купить безлимит", "📊 Мой статус", "❓ Помощь"]:
        return

    text = message.text.strip()
    
    if len(text) < config.MIN_TEXT_LENGTH:
        await message.answer("⚠️ Текст слишком короткий. Пожалуйста, отправьте более содержательное сообщение.")
        return
    if len(text) > config.MAX_TEXT_LENGTH:
        await message.answer(f"⚠️ Текст слишком длинный (максимум {config.MAX_TEXT_LENGTH} символов). Сократите его.")
        return

    # Сохраняем текст в состояние
    await state.update_data(source_text=text)
    await state.set_state(DiplomatState.text_saved)
    
    await message.answer(
        "Текст принят! Выберите, в каком стиле его переписать 👇",
        reply_markup=get_styles_keyboard()
    )

@router.message()
async def process_non_text(message: Message):
    await message.answer("⚠️ Пожалуйста, отправьте текстовое сообщение. Я работаю только с текстом.")

# --- ОБРАБОТКА ВЫБОРА СТИЛЯ (GEMINI) ---

@router.callback_query(F.data == "reselect_style")
async def reselect_style(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("source_text"):
        await callback.message.answer("Текст не найден. Пожалуйста, отправьте сообщение заново.")
        await callback.answer()
        return
    
    try:
        await callback.message.edit_text(
            "Выберите другой стиль для этого текста 👇",
            reply_markup=get_styles_keyboard()
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("style_"))
async def process_style_selection(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user = await check_user(user_id)
    
    # Проверка лимитов
    if not user['is_premium'] and user['free_requests'] <= 0:
        await callback.message.answer(config.TEXT_PAYWALL, reply_markup=get_paywall_keyboard())
        await callback.answer()
        return

    data = await state.get_data()
    source_text = data.get("source_text")
    
    if not source_text:
        await callback.message.answer("Текст потерялся 😔 Пожалуйста, отправьте его заново.")
        await callback.answer()
        return

    style_key = callback.data.split("_")[1]
    style_info = config.AI_MODES.get(style_key)
    
    if not style_info:
        await callback.answer("Неизвестный стиль.", show_alert=True)
        return

    # Уведомляем пользователя о начале работы
    try:
        await callback.message.edit_text(f"⏳ Переписываю в стиле «{style_info['btn']}»...")
    except TelegramBadRequest:
        pass

    # Обращение к Gemini (новый синтаксис)
    try:
        prompt = f"{style_info['prompt']}\n\nТекст для обработки:\n{source_text}"
        
        response = await gemini_client.aio.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        result_text = response.text.strip()
        
        if not result_text:
            raise ValueError("Empty response from Gemini")
            
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        await callback.message.edit_text(
            "❌ Произошла ошибка при обращении к нейросети. Пожалуйста, попробуйте чуть позже."
        )
        await callback.answer()
        return

    # Обновление БД
    database.save_usage(user_id, style_key, source_text, result_text)
    database.increment_total_requests(user_id)
    
    if not user['is_premium']:
        database.decrement_request(user_id)
        user['free_requests'] -= 1

    # Формирование ответа
    # Экранируем HTML, чтобы ответ Gemini не сломал разметку Telegram
    safe_result = html.escape(result_text)
    
    final_msg = f"✨ <b>Результат ({style_info['btn']}):</b>\n\n<code>{safe_result}</code>"
    
    # Маркетинговая вставка
    if not user['is_premium']:
        if user['free_requests'] == 1:
            final_msg += config.TEXT_LAST_ATTEMPT
        elif user['free_requests'] > 1:
            final_msg += f"\n\n💡 <i>Осталось бесплатных попыток: {user['free_requests']}</i>"

    try:
        await callback.message.edit_text(
            final_msg,
            reply_markup=get_post_generation_keyboard(user['is_premium'])
        )
    except TelegramBadRequest:
        await callback.message.answer(
            final_msg,
            reply_markup=get_post_generation_keyboard(user['is_premium'])
        )

    await callback.answer()

# --- ПЛАТЕЖИ (TELEGRAM STARS) ---

@router.message(Command("buy"))
@router.message(F.text == "💎 Купить безлимит")
@router.callback_query(F.data == "buy_premium")
async def process_buy(event: Message | CallbackQuery):
    user_id = event.from_user.id
    user = await check_user(user_id)
    
    if user['is_premium']:
        msg = "У вас уже активирован Premium! 💎 Вы можете пользоваться ботом без ограничений."
        if isinstance(event, CallbackQuery):
            await event.message.answer(msg)
            await event.answer()
        else:
            await event.answer(msg)
        return

    # Уникальный payload для транзакции
    payload = f"premium_{user_id}_{uuid.uuid4().hex[:8]}"
    
    prices = [LabeledPrice(label="Безлимитный доступ", amount=config.SUBSCRIPTION_PRICE_STARS)]
    
    # Отправка инвойса
    # Для Telegram Stars provider_token ДОЛЖЕН быть пустым, currency ДОЛЖНА быть "XTR"
    invoice_kwargs = {
        "title": "Безлимит в «Дипломате» 💎",
        "description": "Навсегда снимите ограничения. Переписывайте любые сообщения в профессиональный деловой стиль.",
        "payload": payload,
        "provider_token": config.PROVIDER_TOKEN,
        "currency": "XTR",
        "prices": prices
    }

    if isinstance(event, CallbackQuery):
        await bot.send_invoice(chat_id=event.message.chat.id, **invoice_kwargs)
        await event.answer()
    else:
        await bot.send_invoice(chat_id=event.chat.id, **invoice_kwargs)

@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    # Подтверждаем готовность принять платеж
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment_info = message.successful_payment
    user_id = message.from_user.id
    
    # Сохраняем платеж. Если payload уже есть, save_payment вернет False
    is_new = database.save_payment(
        user_id=user_id,
        amount=payment_info.total_amount,
        currency=payment_info.currency,
        payload=payment_info.invoice_payload,
        status="success"
    )
    
    if is_new:
        database.set_premium(user_id, True)
        await message.answer(config.TEXT_PREMIUM_SUCCESS, reply_markup=get_main_keyboard())
    else:
        logger.warning(f"Duplicate payment payload received: {payment_info.invoice_payload}")

# --- ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ---

@dp.errors()
async def global_error_handler(event):
    logger.error(f"Update caused error: {event.exception}")
    # В продакшене мы логируем ошибку, но не показываем traceback пользователю
    return True

# --- ЗАПУСК ---

async def main():
    database.init_db()
    dp.include_router(router)
    
    # Удаляем вебхуки и запускаем polling
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
