import asyncio
import logging
import html
import re
import uuid
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    PreCheckoutQuery, LabeledPrice, BotCommand
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
    waiting_for_task_input = State()

class AdminState(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_add_req_id = State()
    waiting_for_add_req_amount = State()
    waiting_for_new_limit = State()
    waiting_for_pro_id = State()

# --- УСТАНОВКА МЕНЮ КОМАНД ---
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🚀 Главное меню"),
        BotCommand(command="profile", description="👤 Мой профиль"),
        BotCommand(command="premium", description="💎 Купить PRO"),
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
def get_main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать работу", callback_data="menu_categories")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"),
         InlineKeyboardButton(text="💎 PRO Доступ", callback_data="menu_premium")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="menu_help")]
    ])

def get_back_to_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main")]
    ])

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
    builder.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main")])
    return InlineKeyboardMarkup(inline_keyboard=builder)

def get_tasks_keyboard(category_id: str):
    builder = []
    tasks = config.CATEGORIES[category_id]["tasks"]
    for task_id, task_data in tasks.items():
        builder.append([InlineKeyboardButton(text=task_data["btn"], callback_data=f"select_{category_id}_{task_id}")])
    builder.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="menu_categories")])
    return InlineKeyboardMarkup(inline_keyboard=builder)

def get_cancel_input_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="menu_categories")]
    ])

def get_post_generation_keyboard(cat_id: str, task_id: str, is_premium: bool):
    buttons = [
        [InlineKeyboardButton(text="🔁 Повторить с другим текстом", callback_data=f"select_{cat_id}_{task_id}")],
        [InlineKeyboardButton(text="🔙 В меню инструментов", callback_data="menu_categories")]
    ]
    if not is_premium:
        buttons.insert(1, [InlineKeyboardButton(text="💎 Купить PRO безлимит", callback_data="menu_premium")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_paywall_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐️ Оплатить Stars ({config.SUBSCRIPTION_PRICE_STARS} ⭐️)", callback_data="buy_stars")],
        [InlineKeyboardButton(text=f"💳 Оплатить Картой/СБП ({config.SUBSCRIPTION_PRICE_RUB} ₽)", callback_data="buy_rub")],
        [InlineKeyboardButton(text="🪙 Крипта / Прямой перевод", callback_data="buy_crypto")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu_main")]
    ])

# --- КЛАВИАТУРЫ АДМИНА ---
def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🎁 Выдать запросы", callback_data="admin_add_req"),
         InlineKeyboardButton(text="👑 Выдать PRO", callback_data="admin_give_pro")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки бота", callback_data="admin_settings")]
    ])

def get_admin_settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Изменить лимит беспл. запросов", callback_data="admin_edit_limit")],
        [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel")]
    ])

def get_admin_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_cancel")]
    ])

async def check_user(user_id: int):
    database.create_user_if_not_exists(user_id)
    return database.get_user(user_id)

# --- ГЛАВНОЕ МЕНЮ И НАВИГАЦИЯ ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await check_user(message.from_user.id)
    await state.clear()
    await message.answer(config.TEXT_START, reply_markup=get_main_menu_keyboard())

@router.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text(config.TEXT_START, reply_markup=get_main_menu_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.message(Command("help"))
@router.callback_query(F.data == "menu_help")
async def show_help(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(config.TEXT_HELP, reply_markup=get_back_to_main_keyboard())
        except TelegramBadRequest:
            pass
        await event.answer()
    else:
        await event.answer(config.TEXT_HELP, reply_markup=get_back_to_main_keyboard())

@router.message(Command("profile"))
@router.callback_query(F.data == "menu_profile")
async def show_profile(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = event.from_user.id
    user = await check_user(user_id)
    status_text = "💎 <b>PRO (Безлимит)</b>" if user['is_premium'] else "🆓 <b>Бесплатный тариф</b>"
    
    text = (
        f"👤 <b>Ваш профиль:</b>\n\n"
        f"Тариф: {status_text}\n"
        f"Сгенерировано ответов: <b>{user['total_requests']}</b>\n"
        f"Ваш ID: <code>{user_id}</code>\n"
    )
    
    kb = get_back_to_main_keyboard()
    if not user['is_premium']:
        text += f"Осталось бесплатных попыток: <b>{user['free_requests']}</b>\n\n"
        text += "<i>PRO-тариф дает безлимитный доступ ко всем инструментам продаж и контента навсегда.</i>"
        kb = get_paywall_keyboard()
        
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)

# --- ЛОГИКА: ВЫБОР ИНСТРУМЕНТА -> ВВОД ТЕКСТА ---
@router.callback_query(F.data == "menu_categories")
async def show_categories(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("🧰 <b>Выберите категорию инструментов:</b>", reply_markup=get_categories_keyboard())
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
            f"📂 Раздел: <b>{cat_info['name']}</b>\nВыберите инструмент 👇", 
            reply_markup=get_tasks_keyboard(cat_id)
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("select_"))
async def ask_for_text(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    cat_id = parts[1]
    task_id = parts[2]
    
    task_info = config.CATEGORIES.get(cat_id, {}).get("tasks", {}).get(task_id)
    if not task_info:
        await callback.answer("Ошибка инструмента", show_alert=True)
        return

    user = await check_user(callback.from_user.id)
    if not user['is_premium'] and user['free_requests'] <= 0:
        await callback.message.edit_text(config.TEXT_PAYWALL, reply_markup=get_paywall_keyboard())
        await callback.answer()
        return

    await state.set_state(AppState.waiting_for_task_input)
    await state.update_data(cat_id=cat_id, task_id=task_id)
    
    try:
        await callback.message.edit_text(
            f"Вы выбрали: <b>{task_info['btn']}</b>\n\n"
            f"✍️ <b>Отправьте мне текст, идею или набросок для этой задачи:</b>",
            reply_markup=get_cancel_input_keyboard()
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.message(AppState.waiting_for_task_input, F.text)
async def process_task_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.startswith('/'):
        return
        
    if len(text) < config.MIN_TEXT_LENGTH:
        await message.answer("⚠️ Текст слишком короткий. Напишите подробнее.", reply_markup=get_cancel_input_keyboard())
        return
    if len(text) > config.MAX_TEXT_LENGTH:
        await message.answer(f"⚠️ Текст слишком длинный (максимум {config.MAX_TEXT_LENGTH} символов).", reply_markup=get_cancel_input_keyboard())
        return

    data = await state.get_data()
    cat_id = data.get("cat_id")
    task_id = data.get("task_id")
    
    task_info = config.CATEGORIES.get(cat_id, {}).get("tasks", {}).get(task_id)
    if not task_info:
        await state.clear()
        await message.answer("Произошла ошибка. Начните заново.", reply_markup=get_main_menu_keyboard())
        return

    user_id = message.from_user.id
    user = await check_user(user_id)
    
    if not user['is_premium'] and user['free_requests'] <= 0:
        await state.clear()
        await message.answer(config.TEXT_PAYWALL, reply_markup=get_paywall_keyboard())
        return

    processing_msg = await message.answer(f"⏳ Генерирую результат...")
    await state.clear()

    try:
        prompt = f"{task_info['prompt']}\n\nВводные данные от пользователя:\n{text}"
        response = await gemini_client.aio.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        result_text = response.text.strip()
        if not result_text:
            raise ValueError("Empty response from Gemini")
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        await processing_msg.edit_text("❌ Ошибка при обращении к нейросети. Попробуйте позже.", reply_markup=get_back_to_main_keyboard())
        return

    database.save_usage(user_id, f"{cat_id}_{task_id}", text, result_text)
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

    await processing_msg.edit_text(final_msg, reply_markup=get_post_generation_keyboard(cat_id, task_id, user['is_premium']))

# --- ПЛАТЕЖИ (МУЛЬТИ-ОПЛАТА) ---
@router.message(Command("premium"))
@router.callback_query(F.data == "menu_premium")
async def show_premium_info(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = event.from_user.id
    user = await check_user(user_id)
    
    if user['is_premium']:
        msg = "У вас уже активирован PRO! 💎 Вы можете пользоваться ботом без ограничений."
        kb = get_back_to_main_keyboard()
    else:
        msg = (
            "💎 <b>PRO Доступ навсегда</b>\n\n"
            "Снимите все лимиты. Делайте вирусные Reels, закрывайте сделки и пишите посты в 1 клик.\n\n"
            "Выберите удобный способ оплаты ниже 👇"
        )
        kb = get_paywall_keyboard()

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(msg, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await event.answer()
    else:
        await event.answer(msg, reply_markup=kb)

@router.callback_query(F.data == "buy_stars")
async def process_buy_stars(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await check_user(user_id)
    if user['is_premium']:
        await callback.answer("У вас уже есть PRO!", show_alert=True)
        return

    payload = f"premium_stars_{user_id}_{uuid.uuid4().hex[:8]}"
    prices = [LabeledPrice(label="PRO Доступ", amount=config.SUBSCRIPTION_PRICE_STARS)]
    
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="PRO Доступ навсегда 💎",
        description="Оплата через Telegram Stars.",
        payload=payload,
        provider_token="", # ДЛЯ STARS ТОКЕН ДОЛЖЕН БЫТЬ ПУСТЫМ!
        currency="XTR",
        prices=prices
    )
    await callback.answer()

@router.callback_query(F.data == "buy_rub")
async def process_buy_rub(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await check_user(user_id)
    if user['is_premium']:
        await callback.answer("У вас уже есть PRO!", show_alert=True)
        return

    if not config.PROVIDER_TOKEN:
        await callback.answer("Оплата картой временно недоступна. Выберите Stars или Прямой перевод.", show_alert=True)
        return

    payload = f"premium_rub_{user_id}_{uuid.uuid4().hex[:8]}"
    prices = [LabeledPrice(label="PRO Доступ", amount=config.SUBSCRIPTION_PRICE_RUB * 100)] # В копейках
    
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="PRO Доступ навсегда 💎",
        description="Оплата банковской картой или СБП.",
        payload=payload,
        provider_token=config.PROVIDER_TOKEN,
        currency="RUB",
        prices=prices
    )
    await callback.answer()

@router.callback_query(F.data == "buy_crypto")
async def process_buy_crypto(callback: CallbackQuery):
    text = (
        "🪙 <b>Оплата Криптовалютой или прямым переводом (СБП)</b>\n\n"
        f"Переведите <b>{config.SUBSCRIPTION_PRICE_RUB}₽</b> (или эквивалент) по реквизитам:\n\n"
        f"<code>{config.MANUAL_PAYMENT_DETAILS}</code>\n\n"
        f"После оплаты отправьте скриншот чека администратору: {config.ADMIN_USERNAME}\n"
        "Администратор выдаст вам PRO-доступ вручную."
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu_premium")]]))
    await callback.answer()

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
        await message.answer(config.TEXT_PREMIUM_SUCCESS, reply_markup=get_main_menu_keyboard())
    else:
        logger.warning(f"Duplicate payment payload received: {payment_info.invoice_payload}")

# --- АДМИН ПАНЕЛЬ ---
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
        f"💰 Доход: {stats['total_revenue_stars']} Stars/RUB"
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

# --- ВЫДАТЬ PRO (АДМИН) ---
@router.callback_query(F.data == "admin_give_pro")
async def admin_give_pro_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_pro_id)
    await callback.message.edit_text("Введите <b>ID пользователя</b>, которому нужно выдать PRO-доступ навсегда:", reply_markup=get_admin_cancel_keyboard())
    await callback.answer()

@router.message(AdminState.waiting_for_pro_id)
async def admin_give_pro_id(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        user = database.get_user(target_id)
        if not user:
            await message.answer("❌ Пользователь не найден. Проверьте ID и попробуйте снова.", reply_markup=get_admin_cancel_keyboard())
            return
        
        database.set_premium(target_id, True)
        await message.answer(f"✅ Успешно! Пользователю <code>{target_id}</code> выдан PRO-доступ навсегда.")
        
        try:
            await bot.send_message(target_id, "🎉 <b>Поздравляем!</b>\nАдминистратор выдал вам <b>PRO-доступ</b> навсегда. Наслаждайтесь безлимитом!")
        except Exception:
            pass
            
        await state.clear()
    except ValueError:
        await message.answer("❌ ID должен быть числом. Попробуйте снова.", reply_markup=get_admin_cancel_keyboard())

# --- ДИНАМИЧЕСКИЕ НАСТРОЙКИ АДМИНА ---
@router.callback_query(F.data == "admin_settings")
async def admin_settings_menu(callback: CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID:
        return
    current_limit = database.get_setting("free_requests_default", str(config.FREE_REQUESTS_FALLBACK))
    text = (
        "⚙️ <b>Настройки бота</b>\n\n"
        f"🎁 Лимит бесплатных запросов для новых пользователей: <b>{current_limit}</b>\n"
    )
    await callback.message.edit_text(text, reply_markup=get_admin_settings_keyboard())
    await callback.answer()

@router.callback_query(F.data == "admin_edit_limit")
async def admin_edit_limit_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_new_limit)
    await callback.message.edit_text("Введите <b>новое число</b> бесплатных запросов, которое будут получать новые пользователи при регистрации:", reply_markup=get_admin_cancel_keyboard())
    await callback.answer()

@router.message(AdminState.waiting_for_new_limit)
async def admin_edit_limit_save(message: Message, state: FSMContext):
    try:
        new_limit = int(message.text.strip())
        if new_limit < 0:
            raise ValueError
        database.set_setting("free_requests_default", str(new_limit))
        await message.answer(f"✅ Настройки обновлены! Теперь новые пользователи будут получать <b>{new_limit}</b> бесплатных запросов.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В админку", callback_data="admin_cancel")]]))
        await state.clear()
    except ValueError:
        await message.answer("❌ Пожалуйста, введите положительное целое число.", reply_markup=get_admin_cancel_keyboard())

# --- ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ---
@dp.errors()
async def global_error_handler(event):
    logger.error(f"Update caused error: {event.exception}")
    return True

# --- ЖИЗНЕННЫЙ ЦИКЛ БОТА ---
async def on_startup(bot: Bot):
    database.init_db()
    await set_bot_commands(bot)
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
