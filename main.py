import asyncio
import logging
import html
import re
import uuid
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, PreCheckoutQuery, LabeledPrice,
    BotCommand
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)

# --- FSM СОСТОЯНИЯ ---
class AppState(StatesGroup):
    text_saved = State()

class AdminState(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_add_req_id = State()
    waiting_for_add_req_amount = State()

# --- УСТАНОВКА МЕНЮ КОМАНД В TELEGRAM ---
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Главное меню"),
        BotCommand(command="profile", description="👤 Мой профиль и лимиты"),
        BotCommand(command="premium", description="💎 Купить PRO безлимит"),
        BotCommand(command="help", description="❓ Инструкция"),
    ]
    await bot.set_my_commands(commands)

# --- УТИЛИТЫ ФОРМАТИРОВАНИЯ ---
def format_gemini_response(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'^###\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^#\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^(\s*)\* ', r'\1• ', text, flags=re.MULTILINE)
    text = re.sub(r'^(\s*)- ', r'\1• ', text, flags=re.MULTILINE)
    return text

# --- КЛАВИАТУРЫ ПОЛЬЗОВАТЕЛЯ ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="💎 Купить PRO")],
            [KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )

def get_categories_keyboard():
    builder = []
    row = []
    for cat_id, cat_data in config.CATEGORIES.items():
        row.append(InlineKeyboardButton(text=cat_data["name"], callback_data=f"cat_{cat_id}"))
        if len(row) == 2:
            builder.append(row)
            row = []
    if row:
        builder.append(row)
    return InlineKeyboardMarkup(inline_keyboard=builder)

def get_tasks_keyboard(category_id: str):
    builder = []
    tasks = config.CATEGORIES[category_id]["tasks"]
    for task_id, task_data in tasks.items():
        builder.append([InlineKeyboardButton(text=task_data["btn"], callback_data=f"task_{category_id}_{task_id}")])
    builder.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="back_to_cats")])
    return InlineKeyboardMarkup(inline_keyboard=builder)

def get_post_generation_keyboard(is_premium: bool):
    buttons = [[InlineKeyboardButton(text="🔁 Сделать что-то еще с этим текстом", callback_data="back_to_cats")]]
    if not is_premium:
        buttons.append([InlineKeyboardButton(text="💎 Купить PRO безлимит", callback_data="buy_premium")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_paywall_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💎 Купить PRO ({config.SUBSCRIPTION_PRICE_STARS} Stars)", callback_data="buy_premium")]
    ])

# --- КЛАВИАТУРЫ АДМИНА ---
def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🎁 Выдать запросы", callback_data="admin_add_req")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")]
    ])

def get_admin_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_cancel")]
    ])

async def check_user(user_id: int):
    database.create_user_if_not_exists(user_id)
    return database.get_user(user_id)

# --- ОБРАБОТЧИКИ БАЗОВЫХ КОМАНД ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await check_user(message.from_user.id)
    await state.clear()
    await message.answer(config.TEXT_START, reply_markup=get_main_keyboard())

@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(config.TEXT_HELP, reply_markup=get_main_keyboard())

@router.message(Command("profile"))
@router.message(Command("status"))
@router.message(F.text == "👤 Мой профиль")
async def cmd_status(message: Message, state: FSMContext):
    await state.clear()
    user = await check_user(message.from_user.id)
    status_text = "💎 <b>PRO (Безлимит)</b>" if user['is_premium'] else "🆓 <b>Бесплатный тариф</b>"
    
    text = (
        f"👤 <b>Ваш профиль:</b>\n\n"
        f"Тариф: {status_text}\n"
        f"Сгенерировано ответов: <b>{user['total_requests']}</b>\n"
        f"Ваш ID: <code>{message.from_user.id}</code>\n"
    )
    
    if not user['is_premium']:
        text += f"Осталось бесплатных попыток: <b>{user['free_requests']}</b>\n\n"
        text += "<i>PRO-тариф дает безлимитный доступ ко всем инструментам продаж и контента навсегда.</i>"
        await message.answer(text, reply_markup=get_paywall_keyboard())
    else:
        await message.answer(text)

# --- ИНТЕРАКТИВНАЯ АДМИН-ПАНЕЛЬ С КНОПКАМИ НАЗАД ---
@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != config.ADMIN_ID:
        return
    await message.answer("🔧 <b>Панель управления</b>\nВыберите действие:", reply_markup=get_admin_keyboard())

@router.callback_query(F.data == "admin_cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("🔧 <b>Панель управления</b>\nВыберите действие:", reply_markup=get_admin_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer("Действие отменено")

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID:
        return
    stats = database.get_stats()
    text = (
        "📊 <b>Статистика проекта:</b>\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"💎 PRO пользователей: {stats['premium_users']}\n"
        f"📝 Всего генераций: {stats['total_generations']}\n"
        f"💳 Успешных оплат: {stats['total_payments']}\n"
        f"💰 Доход: {stats['total_revenue_stars']} Stars"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_cancel")]])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_broadcast)
    await callback.message.edit_text("📢 Отправьте сообщение (текст, фото, видео), которое нужно разослать всем пользователям бота.", reply_markup=get_admin_cancel_keyboard())
    await callback.answer()

@router.message(AdminState.waiting_for_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    users = database.get_all_users()
    sent_count = 0
    await message.answer(f"⏳ Начинаю рассылку для {len(users)} пользователей...")
    
    for u in users:
        try:
            await message.copy_to(u['user_id'])
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
            
    await message.answer(f"✅ Рассылка завершена!\nУспешно доставлено: <b>{sent_count}</b>.")
    await state.clear()

@router.callback_query(F.data == "admin_add_req")
async def admin_add_req_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_add_req_id)
    await callback.message.edit_text("Введите <b>ID пользователя</b>, которому нужно начислить запросы:", reply_markup=get_admin_cancel_keyboard())
    await callback.answer()

@router.message(AdminState.waiting_for_add_req_id)
async def admin_add_req_id(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        user = database.get_user(target_id)
        if not user:
            await message.answer("❌ Пользователь не найден. Проверьте ID и попробуйте снова.", reply_markup=get_admin_cancel_keyboard())
            return
        await state.update_data(target_id=target_id)
        await state.set_state(AdminState.waiting_for_add_req_amount)
        await message.answer("Отлично. Теперь введите <b>количество запросов</b> для начисления:", reply_markup=get_admin_cancel_keyboard())
    except ValueError:
        await message.answer("❌ ID должен быть числом. Попробуйте снова.", reply_markup=get_admin_cancel_keyboard())

@router.message(AdminState.waiting_for_add_req_amount)
async def admin_add_req_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
        data = await state.get_data()
        target_id = data['target_id']
        
        database.add_requests(target_id, amount)
        await message.answer(f"✅ Успешно! Пользователю <code>{target_id}</code> начислено <b>{amount}</b> запросов.")
        
        try:
            await bot.send_message(target_id, f"🎁 <b>Вам начислен бонус!</b>\nАдминистратор добавил вам <b>{amount}</b> бесплатных запросов.")
        except Exception:
            pass
            
        await state.clear()
    except ValueError:
        await message.answer("❌ Количество должно быть числом. Попробуйте снова.", reply_markup=get_admin_cancel_keyboard())

# --- ЕДИНАЯ ТОЧКА ВХОДА ДЛЯ ТЕКСТА ПОЛЬЗОВАТЕЛЯ ---
@router.message(F.text)
async def process_any_text(message: Message, state: FSMContext):
    if message.text.startswith('/'):
        return
    if message.text in ["👤 Мой профиль", "💎 Купить PRO", "❓ Помощь"]:
        return

    text = message.text.strip()
    if len(text) < config.MIN_TEXT_LENGTH:
        await message.answer("⚠️ Текст слишком короткий. Пожалуйста, отправьте более содержательное сообщение или идею.")
        return
    if len(text) > config.MAX_TEXT_LENGTH:
        await message.answer(f"⚠️ Текст слишком длинный (максимум {config.MAX_TEXT_LENGTH} символов). Сократите его.")
        return

    await state.update_data(source_text=text)
    
    await message.answer(
        f"✅ <b>Данные получены!</b>\nВыберите, что мы будем с этим делать:",
        reply_markup=get_categories_keyboard()
    )

# --- НАВИГАЦИЯ ПО INLINE МЕНЮ (НАЗАД И ВПЕРЕД) ---
@router.callback_query(F.data == "back_to_cats")
async def back_to_categories(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("source_text"):
        await callback.message.answer("Данные потерялись. Пожалуйста, отправьте текст заново.")
        await callback.answer()
        return
    try:
        await callback.message.edit_text("✅ <b>Данные получены!</b>\nВыберите, что мы будем с этим делать:", reply_markup=get_categories_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("cat_"))
async def show_category_tasks(callback: CallbackQuery, state: FSMContext):
    cat_id = callback.data.split("_")[1]
    cat_info = config.CATEGORIES.get(cat_id)
    
    if not cat_info:
        await callback.answer("Ошибка категории", show_alert=True)
        return
        
    try:
        await callback.message.edit_text(
            f"Раздел: <b>{cat_info['name']}</b>\nВыберите конкретную задачу 👇", 
            reply_markup=get_tasks_keyboard(cat_id)
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

# --- ВЫПОЛНЕНИЕ ЗАДАЧ (GEMINI) ---
@router.callback_query(F.data.startswith("task_"))
async def process_generation(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user = await check_user(user_id)
    
    if not user['is_premium'] and user['free_requests'] <= 0:
        await callback.message.answer(config.TEXT_PAYWALL, reply_markup=get_paywall_keyboard())
        await callback.answer()
        return

    data = await state.get_data()
    source_text = data.get("source_text")
    
    if not source_text:
        await callback.message.answer("Данные потерялись 😔 Пожалуйста, отправьте текст заново.")
        await callback.answer()
        return

    parts = callback.data.split("_")
    cat_id = parts[1]
    task_id = parts[2]
    
    task_info = config.CATEGORIES.get(cat_id, {}).get("tasks", {}).get(task_id)
    
    if not task_info:
        await callback.answer("Неизвестная задача.", show_alert=True)
        return

    try:
        await callback.message.edit_text(f"⏳ Генерирую (Задача: {task_info['btn']})...")
    except TelegramBadRequest:
        pass

    try:
        prompt = f"{task_info['prompt']}\n\nВводные данные от пользователя:\n{source_text}"
        response = await gemini_client.aio.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        result_text = response.text.strip()
        if not result_text:
            raise ValueError("Empty response from Gemini")
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при обращении к нейросети. Пожалуйста, попробуйте чуть позже.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_cats")]]))
        await callback.answer()
        return

    database.save_usage(user_id, f"{cat_id}_{task_id}", source_text, result_text)
    database.increment_total_requests(user_id)
    
    if not user['is_premium']:
        database.decrement_request(user_id)
        user['free_requests'] -= 1

    safe_result = format_gemini_response(result_text)
    final_msg = f"✨ <b>Результат ({task_info['btn']}):</b>\n\n{safe_result}"
    
    if not user['is_premium']:
        if user['free_requests'] == 1:
            final_msg += config.TEXT_LAST_ATTEMPT
        elif user['free_requests'] > 1:
            final_msg += f"\n\n💡 <i>Осталось бесплатных попыток: {user['free_requests']}</i>"

    try:
        await callback.message.edit_text(final_msg, reply_markup=get_post_generation_keyboard(user['is_premium']))
    except TelegramBadRequest:
        await callback.message.answer(final_msg, reply_markup=get_post_generation_keyboard(user['is_premium']))

    await callback.answer()

# --- ПЛАТЕЖИ (TELEGRAM STARS) ---
@router.message(Command("buy"))
@router.message(Command("premium"))
@router.message(F.text == "💎 Купить PRO")
@router.callback_query(F.data == "buy_premium")
async def process_buy(event: Message | CallbackQuery):
    user_id = event.from_user.id
    user = await check_user(user_id)
    
    if user['is_premium']:
        msg = "У вас уже активирован PRO! 💎 Вы можете пользоваться ботом без ограничений."
        if isinstance(event, CallbackQuery):
            await event.message.answer(msg)
            await event.answer()
        else:
            await event.answer(msg)
        return

    payload = f"premium_{user_id}_{uuid.uuid4().hex[:8]}"
    prices = [LabeledPrice(label="PRO Доступ", amount=config.SUBSCRIPTION_PRICE_STARS)]
    
    invoice_kwargs = {
        "title": "PRO Доступ навсегда 💎",
        "description": "Снимите все лимиты. Делайте вирусные Reels, закрывайте сделки и пишите посты в 1 клик.",
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
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment_info = message.successful_payment
    user_id = message.from_user.id
    
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
    return True

# --- ЖИЗНЕННЫЙ ЦИКЛ БОТА ---
async def on_startup(bot: Bot):
    database.init_db()
    await set_bot_commands(bot) # <-- Устанавливаем меню команд при запуске!
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started and webhook dropped")

async def on_shutdown(bot: Bot):
    logger.info("Bot shutting down. Closing session...")
    await bot.session.close()

async def main():
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
