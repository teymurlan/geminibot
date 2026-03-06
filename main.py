import asyncio
import logging
import html
import re
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)

# --- FSM СОСТОЯНИЯ ---
class DiplomatState(StatesGroup):
    text_saved = State()

class AdminState(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_add_req_id = State()
    waiting_for_add_req_amount = State()

# --- УТИЛИТЫ ФОРМАТИРОВАНИЯ ---
def format_gemini_response(text: str) -> str:
    """Безопасное преобразование Markdown от Gemini в HTML для Telegram."""
    text = html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'^###\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^#\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^(\s*)\* ', r'\1• ', text, flags=re.MULTILINE)
    text = re.sub(r'^(\s*)- ', r'\1• ', text, flags=re.MULTILINE)
    return text

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✍️ Отправить текст")],
            [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="💎 Купить безлимит")],
            [KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )

def get_action_keyboard():
    """Клавиатура выбора действия после отправки текста"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Переписать текст (Стили)", callback_data="action_rewrite")],
        [InlineKeyboardButton(text="💼 Бизнес-задачи (КП, Резюме...)", callback_data="action_business")]
    ])

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
    builder.append([InlineKeyboardButton(text="🔙 Назад", callback_data="action_back")])
    return InlineKeyboardMarkup(inline_keyboard=builder)

def get_tasks_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📑 Коммерческое предложение", callback_data="task_cp")],
        [InlineKeyboardButton(text="📄 Резюме", callback_data="task_resume"),
         InlineKeyboardButton(text="📊 Презентация", callback_data="task_presentation")],
        [InlineKeyboardButton(text="📝 Выжимка", callback_data="task_summary"),
         InlineKeyboardButton(text="💡 Идеи", callback_data="task_brainstorm")],
        [InlineKeyboardButton(text="✉️ Холодное письмо", callback_data="task_cold_email"),
         InlineKeyboardButton(text="📱 Пост", callback_data="task_post")],
        [InlineKeyboardButton(text="🗣 Интервью", callback_data="task_interview")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="action_back")]
    ])

def get_post_generation_keyboard(is_premium: bool):
    buttons = [[InlineKeyboardButton(text="🔁 Другая задача с этим же текстом", callback_data="action_back")]]
    if not is_premium:
        buttons.append([InlineKeyboardButton(text="💎 Купить безлимит", callback_data="buy_premium")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_paywall_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Купить безлимит (50 Stars)", callback_data="buy_premium")]
    ])

def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🎁 Выдать запросы", callback_data="admin_add_req")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")]
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

@router.message(Command("status"))
@router.message(F.text == "👤 Мой профиль")
async def cmd_status(message: Message, state: FSMContext):
    await state.clear()
    user = await check_user(message.from_user.id)
    status_text = "💎 <b>Premium (Безлимит)</b>" if user['is_premium'] else "🆓 <b>Бесплатный тариф</b>"
    
    text = (
        f"👤 <b>Ваш профиль:</b>\n\n"
        f"Тариф: {status_text}\n"
        f"Использовано запросов: <b>{user['total_requests']}</b>\n"
        f"Ваш ID: <code>{message.from_user.id}</code>\n"
    )
    
    if not user['is_premium']:
        text += f"Осталось бесплатных попыток: <b>{user['free_requests']}</b>\n\n"
        text += "<i>Premium даёт безлимитный доступ навсегда. Вы забудете о лимитах и сможете решать любые рабочие задачи.</i>"
        await message.answer(text, reply_markup=get_paywall_keyboard())
    else:
        await message.answer(text)

@router.message(F.text == "✍️ Отправить текст")
async def btn_send_text(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Просто отправьте мне любой текст или данные в чат 👇")

# --- ИНТЕРАКТИВНАЯ АДМИН-ПАНЕЛЬ ---
@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != config.ADMIN_ID:
        return
    await message.answer("🔧 <b>Панель управления</b>\nВыберите действие:", reply_markup=get_admin_keyboard())

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != config.ADMIN_ID:
        return
    stats = database.get_stats()
    text = (
        "📊 <b>Статистика проекта:</b>\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"💎 Premium пользователей: {stats['premium_users']}\n"
        f"📝 Всего генераций: {stats['total_generations']}\n"
        f"💳 Успешных оплат: {stats['total_payments']}\n"
        f"💰 Доход: {stats['total_revenue_stars']} Stars"
    )
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())
    await callback.answer()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.ADMIN_ID:
        return
    await state.set_state(AdminState.waiting_for_broadcast)
    await callback.message.edit_text("📢 Отправьте сообщение (текст, фото, видео), которое нужно разослать всем пользователям бота.\n\n<i>Для отмены отправьте /start</i>")
    await callback.answer()

@router.message(AdminState.waiting_for_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.text == '/start':
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=get_main_keyboard())
        return

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
    await callback.message.edit_text("Введите <b>ID пользователя</b>, которому нужно начислить запросы:")
    await callback.answer()

@router.message(AdminState.waiting_for_add_req_id)
async def admin_add_req_id(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        user = database.get_user(target_id)
        if not user:
            await message.answer("❌ Пользователь не найден. Проверьте ID и попробуйте снова.")
            return
        await state.update_data(target_id=target_id)
        await state.set_state(AdminState.waiting_for_add_req_amount)
        await message.answer("Отлично. Теперь введите <b>количество запросов</b> для начисления:")
    except ValueError:
        await message.answer("❌ ID должен быть числом. Попробуйте снова.")

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
        await message.answer("❌ Количество должно быть числом. Попробуйте снова.")

# --- ЕДИНАЯ ТОЧКА ВХОДА ДЛЯ ТЕКСТА ---
@router.message(F.text)
async def process_any_text(message: Message, state: FSMContext):
    # Игнорируем команды
    if message.text.startswith('/'):
        return

    # Игнорируем кнопки Reply-клавиатуры, если они случайно попали сюда
    if message.text in ["✍️ Отправить текст", "👤 Мой профиль", "💎 Купить безлимит", "❓ Помощь"]:
        return

    text = message.text.strip()
    if len(text) < config.MIN_TEXT_LENGTH:
        await message.answer("⚠️ Текст слишком короткий. Пожалуйста, отправьте более содержательное сообщение или данные.")
        return
    if len(text) > config.MAX_TEXT_LENGTH:
        await message.answer(f"⚠️ Текст слишком длинный (максимум {config.MAX_TEXT_LENGTH} символов). Сократите его.")
        return

    # Сохраняем текст и спрашиваем, что с ним делать
    await state.update_data(source_text=text)
    await state.set_state(DiplomatState.text_saved)
    
    await message.answer(
        "✅ <b>Текст принят!</b>\nЧто вы хотите с ним сделать?",
        reply_markup=get_action_keyboard()
    )

# --- НАВИГАЦИЯ ПО INLINE МЕНЮ ---
@router.callback_query(F.data == "action_back")
async def action_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("source_text"):
        await callback.message.answer("Текст потерялся. Пожалуйста, отправьте его заново.")
        await callback.answer()
        return
    try:
        await callback.message.edit_text("✅ <b>Текст принят!</b>\nЧто вы хотите с ним сделать?", reply_markup=get_action_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data == "action_rewrite")
async def action_rewrite(callback: CallbackQuery):
    try:
        await callback.message.edit_text("Выберите, в каком стиле переписать текст 👇", reply_markup=get_styles_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data == "action_business")
async def action_business(callback: CallbackQuery):
    try:
        await callback.message.edit_text("Выберите бизнес-задачу 👇", reply_markup=get_tasks_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer()

# --- ВЫПОЛНЕНИЕ ЗАДАЧ (GEMINI) ---
@router.callback_query(F.data.startswith("style_") | F.data.startswith("task_"))
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
        await callback.message.answer("Текст потерялся 😔 Пожалуйста, отправьте его заново.")
        await callback.answer()
        return

    # Определяем, что выбрал пользователь (стиль или бизнес-задачу)
    action_type, action_key = callback.data.split("_", 1)
    
    if action_type == "style":
        info = config.AI_MODES.get(action_key)
        name = info['btn'] if info else "Переписывание"
    else:
        info = config.ASSISTANT_TASKS.get(action_key)
        name = info['name'] if info else "Бизнес-задача"
    
    if not info:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    try:
        await callback.message.edit_text(f"⏳ Обрабатываю (Режим: {name})...")
    except TelegramBadRequest:
        pass

    try:
        prompt = f"{info['prompt']}\n\nДанные от пользователя:\n{source_text}"
        response = await gemini_client.aio.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        result_text = response.text.strip()
        if not result_text:
            raise ValueError("Empty response from Gemini")
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при обращении к нейросети. Пожалуйста, попробуйте чуть позже.")
        await callback.answer()
        return

    database.save_usage(user_id, action_key, source_text, result_text)
    database.increment_total_requests(user_id)
    
    if not user['is_premium']:
        database.decrement_request(user_id)
        user['free_requests'] -= 1

    safe_result = format_gemini_response(result_text)
    final_msg = f"✨ <b>Результат ({name}):</b>\n\n{safe_result}"
    
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

    payload = f"premium_{user_id}_{uuid.uuid4().hex[:8]}"
    prices = [LabeledPrice(label="Безлимитный доступ", amount=config.SUBSCRIPTION_PRICE_STARS)]
    
    invoice_kwargs = {
        "title": "Безлимит в «Дипломате» 💎",
        "description": "Навсегда снимите ограничения. Решайте любые бизнес-задачи без лимитов.",
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
