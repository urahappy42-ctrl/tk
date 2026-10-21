import os
import random
import json
import logging
import asyncio
import aiohttp
from threading import Event
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackContext,
    ConversationHandler,
)
from telegram.error import TimedOut, NetworkError, BadRequest, RetryAfter

# ─── Логирование ─────────────────────────────────────────────────────────────

class CustomFormatter(logging.Formatter):
    def format(self, record):
        return f"⛧{record.getMessage()}⛧"

handler = logging.StreamHandler()
handler.setFormatter(CustomFormatter())

logging.root.setLevel(logging.CRITICAL + 1)
logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)
logger.addHandler(handler)
logger.propagate = False

for noisy in ("httpx", "httpcore", "telegram", "apscheduler", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.CRITICAL + 1)
    logging.getLogger(noisy).propagate = False

LOGIN_FILE = "login.json"

def load_login():
    if os.path.exists(LOGIN_FILE):
        try:
            with open(LOGIN_FILE, 'r') as f:
                data = json.load(f)
            if data.get('bot_token') and data.get('allowed_user_id'):
                return data
        except Exception:
            pass
    return None

def save_login(data):
    try:
        with open(LOGIN_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⛧ошибка сохранения логина: {e}⛧")

def interactive_login():
    print("⛧первый запуск — нужна настройка⛧")
    token = input("токен бота (BotFather): ").strip()
    while True:
        try:
            owner_id = int(input("твой telegram id (кого бот будет слушаться): ").strip())
            break
        except ValueError:
            print("⛧id должен быть числом, попробуй снова⛧")
    data = {"bot_token": token, "allowed_user_id": owner_id}
    save_login(data)
    return data

_login = load_login() or interactive_login()
BOT_TOKEN = _login['bot_token']
ALLOWED_USER_ID = _login['allowed_user_id']

TEMPLATES_DIR = "templates"
ACTIVE_CHATS_FILE = "active_chats.json"

# Состояния
(WAITING_CHAT_ID, WAITING_INTERVAL, SELECTING_CHAT_TO_STOP,
 WAITING_HEADER, SELECTING_TEMPLATE, UPLOADING_TEMPLATE,
 SELECTING_CHAT_TO_EDIT, SELECTING_NEW_TEMPLATE_FOR_CHAT,
 UPLOADING_MEDIA, SELECTING_TEMPLATE_TO_DELETE,
 SELECTING_CHAT_FOR_HEADER, WAITING_NEW_HEADER,
 WAITING_MEDIA_URL,
 SELECTING_CHAT_FOR_MEDIA, WAITING_NEW_MEDIA_URL,
 UPLOADING_RESTORE_FILE) = range(16)

# Глобальные переменные
current_jobs = {}
stop_events = {}
chat_headers = {}
chat_templates = {}
chat_media = {}
send_counts = {}

# Типы медиа
VIDEO_EXTENSIONS = ('.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.mpeg', '.mpg', '.3gp')
PHOTO_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')

# ─── Шаблоны ─────────────────────────────────────────────────────────────────

def ensure_templates_dir():
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    os.makedirs("uploaded_media", exist_ok=True)

def get_all_templates():
    ensure_templates_dir()
    files = [f for f in os.listdir(TEMPLATES_DIR) if f.endswith('.txt')]
    return sorted(set(files))

def load_template_by_name(name: str):
    path = os.path.join(TEMPLATES_DIR, name)
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
        return []
    except Exception as e:
        logger.error(f"ошибка загрузки шаблона {name}: {e}")
        return []

# ─── Активные чаты ────────────────────────────────────────────────────────────

def load_data():
    try:
        if os.path.exists(ACTIVE_CHATS_FILE):
            with open(ACTIVE_CHATS_FILE, 'r') as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"ошибка загрузки данных: {e}")
        return {}

def save_data(data):
    try:
        with open(ACTIVE_CHATS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"ошибка сохранения данных: {e}")

# ─── Определение типа медиа ──────────────────────────────────────────────────

def get_media_type(url: str) -> str:
    if not url:
        return 'unknown'
    url_lower = url.lower().split('?')[0]
    for ext in VIDEO_EXTENSIONS:
        if url_lower.endswith(ext):
            return 'video'
    for ext in PHOTO_EXTENSIONS:
        if url_lower.endswith(ext):
            return 'photo'
    return 'unknown'

# ─── Отправка с retry ────────────────────────────────────────────────────────

async def safe_send(context, chat_id, text, media_url=None, retries=3):
    for attempt in range(retries):
        try:
            if media_url:
                media_type = get_media_type(media_url)
                try:
                    if media_type == 'video':
                        await context.bot.send_video(chat_id=chat_id, video=media_url, caption=text)
                    elif media_type == 'photo':
                        await context.bot.send_photo(chat_id=chat_id, photo=media_url, caption=text)
                    else:
                        # неизвестный тип — пробуем фото → видео → документ
                        try:
                            await context.bot.send_photo(chat_id=chat_id, photo=media_url, caption=text)
                        except Exception:
                            try:
                                await context.bot.send_video(chat_id=chat_id, video=media_url, caption=text)
                            except Exception:
                                await context.bot.send_document(chat_id=chat_id, document=media_url, caption=text)
                    return True
                except Exception:
                    await context.bot.send_message(chat_id=chat_id, text=text)
                    return True
            else:
                await context.bot.send_message(chat_id=chat_id, text=text)
                return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError):
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"ошибка отправки (попытка {attempt+1}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2)
    return False

async def send_random_message(context: CallbackContext):
    try:
        job_data = context.job.data
        chat_id = job_data['chat_id']
        header = chat_headers.get(chat_id, "")
        template_name = chat_templates.get(chat_id)
        media_url = chat_media.get(chat_id)

        if stop_events.get(chat_id, Event()).is_set():
            return

        messages = load_template_by_name(template_name) if template_name else []
        if not messages:
            return

        message = random.choice(messages)
        full_message = f"{header} {message}" if header else message

        # typing action
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(min(len(full_message) * 0.03, 5))
        except Exception:
            pass

        success = await safe_send(context, chat_id, full_message, media_url)
        if success:
            send_counts[chat_id] = send_counts.get(chat_id, 0) + 1
    except Exception as e:
        logger.error(f"ошибка отправки сообщения: {e}")

# ─── Клавиатуры ───────────────────────────────────────────────────────────────

main_keyboard = ReplyKeyboardMarkup(
    [["⛧ загрузить шаблоны ⛧", "⛧ все шаблоны ⛧"],
     ["⛧ начать утилизирование ⛧", "⛧ остановить рассылку ⛧"],
     ["⛧ активные чаты ⛧", "⛧ статистика ⛧"],
     ["⛧ загрузить медиа ⛧", "⛧ восстановить ⛧"],
     ["⛧ удалить шаблон ⛧", "⛧ информация ⛧"]],
    resize_keyboard=True
)
cancel_keyboard = ReplyKeyboardMarkup([["⛧ отмена ⛧"]], resize_keyboard=True)
header_keyboard = ReplyKeyboardMarkup([["⛧ без шапки ⛧"], ["⛧ отмена ⛧"]], resize_keyboard=True)
media_keyboard = ReplyKeyboardMarkup([["⛧ без медиа ⛧"], ["⛧ отмена ⛧"]], resize_keyboard=True)

def templates_keyboard():
    templates = get_all_templates()
    rows = [[f"⛧ {t} ⛧"] for t in templates]
    rows.append(["⛧ отмена ⛧"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            await update.message.reply_text("⛧ доступ запрещен ⛧")
            return
        await update.message.reply_text(
            f"⛧ бот активен ⛧\n"
            f"⛧ шаблонов загружено: {len(get_all_templates())} ⛧\n"
            f"⛧ активных чатов: {len(load_data())} ⛧",
            reply_markup=main_keyboard
        )
    except Exception as e:
        logger.error(f"ошибка в start: {e}")

async def show_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⛧ bot created by omyt ⛧")

async def new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        for member in update.message.new_chat_members:
            if member.id == context.bot.id:
                chat = update.effective_chat
                await context.bot.send_message(
                    ALLOWED_USER_ID,
                    f"⛧ бота добавили в новый чат ⛧\n"
                    f"⛧ название чата: {chat.title} ⛧\n"
                    f"⛧ айди чата: {chat.id} ⛧"
                )
                break
    except Exception as e:
        logger.error(f"ошибка в new_chat_members: {e}")

# ─── ЗАГРУЗИТЬ МЕДИА (x0.at) ─────────────────────────────────────────────────

async def request_upload_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "⛧ отправьте медиа файл (фото/видео/документ) ⛧\n"
        "⛧ будет загружен на x0.at и вы получите ссылку ⛧",
        reply_markup=cancel_keyboard
    )
    return UPLOADING_MEDIA

async def handle_upload_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message.text and update.message.text.strip() == "⛧ отмена ⛧":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        file_obj = None
        file_name = "media"

        if update.message.photo:
            file_obj = await context.bot.get_file(update.message.photo[-1].file_id)
            file_name = f"photo_{update.message.photo[-1].file_id}.jpg"
        elif update.message.video:
            file_obj = await context.bot.get_file(update.message.video.file_id)
            file_name = update.message.video.file_name or f"video_{update.message.video.file_id}.mp4"
        elif update.message.document:
            file_obj = await context.bot.get_file(update.message.document.file_id)
            file_name = update.message.document.file_name or f"doc_{update.message.document.file_id}"
        else:
            await update.message.reply_text("⛧ нужно медиа (фото/видео/файл) ⛧")
            return UPLOADING_MEDIA

        await update.message.reply_text("⛧ скачиваю файл... ⛧")
        local_path = os.path.join("uploaded_media", file_name)
        await file_obj.download_to_drive(local_path)

        await update.message.reply_text("⛧ загружаю на x0.at... ⛧")

        try:
            async with aiohttp.ClientSession() as session:
                with open(local_path, "rb") as f:
                    form = aiohttp.FormData()
                    form.add_field("file", f, filename=file_name)
                    async with session.post(
                        "https://x0.at/",
                        data=form,
                        timeout=aiohttp.ClientTimeout(total=120)
                    ) as resp:
                        text = (await resp.text()).strip()
                        if resp.status == 200 and text.startswith("https://"):
                            await update.message.reply_text(
                                f"⛧ медиа загружено ⛧\n"
                                f"⛧ ссылка: {text} ⛧\n\n"
                                f"⛧ можно использовать при настройке рассылки ⛧",
                                reply_markup=main_keyboard
                            )
                        else:
                            await update.message.reply_text(
                                f"⛧ ошибка загрузки x0.at: {text[:200]} ⛧",
                                reply_markup=main_keyboard
                            )
        except Exception as e:
            await update.message.reply_text(f"⛧ ошибка: {str(e)} ⛧", reply_markup=main_keyboard)

        return ConversationHandler.END
    except Exception as e:
        logger.error(f"ошибка в handle_upload_media: {e}")
        await update.message.reply_text(f"⛧ ошибка: {str(e)} ⛧", reply_markup=main_keyboard)
        return ConversationHandler.END

# ─── ЗАГРУЗИТЬ ШАБЛОНЫ ────────────────────────────────────────────────────────

async def request_upload_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        await update.message.reply_text(
            "⛧ отправьте .txt файл ⛧\n"
            "⛧ имя файла станет именем шаблона ⛧",
            reply_markup=cancel_keyboard
        )
        return UPLOADING_TEMPLATE
    except Exception as e:
        logger.error(f"ошибка в request_upload_template: {e}")
        return ConversationHandler.END

async def handle_upload_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message.text and update.message.text.strip().lower() == "⛧ отмена ⛧":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        document = update.message.document
        if not document:
            await update.message.reply_text("⛧ нужен .txt файл ⛧")
            return UPLOADING_TEMPLATE

        if not (document.mime_type == 'text/plain' or document.file_name.lower().endswith('.txt')):
            await update.message.reply_text("⛧ нужен текстовый файл (.txt) ⛧")
            return UPLOADING_TEMPLATE

        file_name = document.file_name
        if not file_name.lower().endswith('.txt'):
            file_name += '.txt'

        ensure_templates_dir()
        dest = os.path.join(TEMPLATES_DIR, file_name)
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(dest)

        lines = load_template_by_name(file_name)
        await update.message.reply_text(
            f"⛧ шаблон '{file_name}' сохранён ⛧\n"
            f"⛧ строк: {len(lines)} ⛧\n"
            f"⛧ всего шаблонов: {len(get_all_templates())} ⛧",
            reply_markup=main_keyboard
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"ошибка в handle_upload_template: {e}")
        await update.message.reply_text(f"⛧ ошибка: {str(e)} ⛧")
        return ConversationHandler.END

# ─── УДАЛИТЬ ШАБЛОН ───────────────────────────────────────────────────────────

async def start_delete_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        templates = get_all_templates()
        if not templates:
            await update.message.reply_text("⛧ шаблонов нет ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END
        await update.message.reply_text(
            "⛧ выберите шаблон для удаления ⛧",
            reply_markup=templates_keyboard()
        )
        return SELECTING_TEMPLATE_TO_DELETE
    except Exception as e:
        logger.error(f"ошибка в start_delete_template: {e}")
        return ConversationHandler.END

async def handle_delete_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip('⛧').strip()
        if text.lower() == "отмена":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        templates = get_all_templates()
        if text not in templates:
            await update.message.reply_text("⛧ шаблон не найден, выберите из списка ⛧")
            return SELECTING_TEMPLATE_TO_DELETE

        path = os.path.join(TEMPLATES_DIR, text)
        os.remove(path)

        active_chats = load_data()
        for chat_id, info in active_chats.items():
            if info.get('template') == text:
                active_chats[chat_id]['template'] = ''
                if chat_id in chat_templates:
                    chat_templates[chat_id] = ''
        save_data(active_chats)

        await update.message.reply_text(
            f"⛧ шаблон '{text}' удалён ⛧\n"
            f"⛧ осталось шаблонов: {len(get_all_templates())} ⛧",
            reply_markup=main_keyboard
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"ошибка в handle_delete_template: {e}")
        return ConversationHandler.END

# ─── ВСЕ ШАБЛОНЫ ─────────────────────────────────────────────────────────────

async def show_all_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        templates = get_all_templates()
        if not templates:
            await update.message.reply_text("⛧ шаблонов нет ⛧", reply_markup=main_keyboard)
            return
        msg = "⛧ все шаблоны ⛧\n\n"
        for i, t in enumerate(templates, 1):
            lines = load_template_by_name(t)
            msg += f"⛧ {i}. {t} — {len(lines)} строк ⛧\n"
        await update.message.reply_text(msg, reply_markup=main_keyboard)
    except Exception as e:
        logger.error(f"ошибка в show_all_templates: {e}")

# ─── АКТИВНЫЕ ЧАТЫ ───────────────────────────────────────────────────────────

async def show_active_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        active_chats = load_data()
        if not active_chats:
            await update.message.reply_text("⛧ нет активных рассылок ⛧", reply_markup=main_keyboard)
            return

        msg = "⛧ активные рассылки ⛧\n\n"
        for chat_id, info in active_chats.items():
            media_url = chat_media.get(chat_id) or info.get('media_url', '')
            msg += (
                f"⛧ чат: {chat_id} ⛧\n"
                f"⛧ шаблон: {info.get('template', '???')} ⛧\n"
                f"⛧ интервал: {info['interval']} сек ⛧\n"
                f"⛧ шапка: {info.get('header', '') or 'отсутствует'} ⛧\n"
                f"⛧ медиа: {media_url if media_url else 'нет'} ⛧\n\n"
            )

        keyboard = ReplyKeyboardMarkup(
            [["⛧ изменить шаблон в чате ⛧"],
             ["⛧ изменить шапку в чате ⛧"],
             ["⛧ изменить медиа в чате ⛧"],
             ["⛧ назад ⛧"]],
            resize_keyboard=True
        )
        await update.message.reply_text(msg, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"ошибка в show_active_chats: {e}")

# ─── ИЗМЕНИТЬ МЕДИА В ЧАТЕ ────────────────────────────────────────────────────

async def start_change_chat_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        active_chats = load_data()
        if not active_chats:
            await update.message.reply_text("⛧ нет активных рассылок ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        keyboard = [[f"⛧ {chat_id} ⛧"] for chat_id in active_chats]
        keyboard.append(["⛧ отмена ⛧"])
        await update.message.reply_text(
            "⛧ выберите чат для изменения медиа ⛧",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return SELECTING_CHAT_FOR_MEDIA
    except Exception as e:
        logger.error(f"ошибка в start_change_chat_media: {e}")
        return ConversationHandler.END

async def handle_select_chat_for_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.message.text.strip('⛧').strip()
        if chat_id.lower() == "отмена":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        active_chats = load_data()
        if chat_id not in active_chats:
            await update.message.reply_text("⛧ чат не найден ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        context.user_data['editing_media_chat_id'] = chat_id
        current_media = chat_media.get(chat_id) or active_chats[chat_id].get('media_url', '') or 'отсутствует'

        await update.message.reply_text(
            f"⛧ чат: {chat_id} ⛧\n"
            f"⛧ текущее медиа: {current_media} ⛧\n\n"
            f"⛧ вставьте новую ссылку на медиа ⛧\n"
            f"⛧ или нажмите 'без медиа' чтобы убрать ⛧",
            reply_markup=media_keyboard
        )
        return WAITING_NEW_MEDIA_URL
    except Exception as e:
        logger.error(f"ошибка в handle_select_chat_for_media: {e}")
        return ConversationHandler.END

async def handle_new_media_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip()
        if text == "⛧ отмена ⛧":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        chat_id = context.user_data['editing_media_chat_id']
        new_media = "" if text == "⛧ без медиа ⛧" else text

        chat_media[chat_id] = new_media

        active_chats = load_data()
        if chat_id in active_chats:
            active_chats[chat_id]['media_url'] = new_media
            save_data(active_chats)

        display = new_media if new_media else 'удалено'
        await update.message.reply_text(
            f"⛧ медиа в чате {chat_id} обновлено ⛧\n"
            f"⛧ новое медиа: {display} ⛧",
            reply_markup=main_keyboard
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"ошибка в handle_new_media_url: {e}")
        return ConversationHandler.END

# ─── ИЗМЕНИТЬ ШАПКУ В ЧАТЕ ───────────────────────────────────────────────────

async def start_change_chat_header(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        active_chats = load_data()
        if not active_chats:
            await update.message.reply_text("⛧ нет активных рассылок ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        keyboard = [[f"⛧ {chat_id} ⛧"] for chat_id in active_chats]
        keyboard.append(["⛧ отмена ⛧"])
        await update.message.reply_text(
            "⛧ выберите чат для изменения шапки ⛧",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return SELECTING_CHAT_FOR_HEADER
    except Exception as e:
        logger.error(f"ошибка в start_change_chat_header: {e}")
        return ConversationHandler.END

async def handle_select_chat_for_header(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.message.text.strip('⛧').strip()
        if chat_id.lower() == "отмена":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        active_chats = load_data()
        if chat_id not in active_chats:
            await update.message.reply_text("⛧ чат не найден ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        context.user_data['editing_header_chat_id'] = chat_id
        current_header = active_chats[chat_id].get('header', '') or 'отсутствует'

        await update.message.reply_text(
            f"⛧ чат: {chat_id} ⛧\n"
            f"⛧ текущая шапка: {current_header} ⛧\n\n"
            f"⛧ введите новую шапку или нажмите 'без шапки' ⛧",
            reply_markup=header_keyboard
        )
        return WAITING_NEW_HEADER
    except Exception as e:
        logger.error(f"ошибка в handle_select_chat_for_header: {e}")
        return ConversationHandler.END

async def handle_new_header(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip()
        if text == "⛧ отмена ⛧":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        chat_id = context.user_data['editing_header_chat_id']
        new_header = "" if text == "⛧ без шапки ⛧" else text

        chat_headers[chat_id] = new_header

        active_chats = load_data()
        if chat_id in active_chats:
            active_chats[chat_id]['header'] = new_header
            save_data(active_chats)

        display = new_header if new_header else 'удалена'
        await update.message.reply_text(
            f"⛧ шапка в чате {chat_id} обновлена ⛧\n"
            f"⛧ новая шапка: {display} ⛧",
            reply_markup=main_keyboard
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"ошибка в handle_new_header: {e}")
        return ConversationHandler.END

# ─── ИЗМЕНИТЬ ШАБЛОН В ЧАТЕ ───────────────────────────────────────────────────

async def start_change_chat_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        active_chats = load_data()
        if not active_chats:
            await update.message.reply_text("⛧ нет активных рассылок ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        keyboard = [[f"⛧ {chat_id} ⛧"] for chat_id in active_chats]
        keyboard.append(["⛧ отмена ⛧"])
        await update.message.reply_text(
            "⛧ выберите чат в котором хотите сменить шаблон ⛧",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return SELECTING_CHAT_TO_EDIT
    except Exception as e:
        logger.error(f"ошибка в start_change_chat_template: {e}")
        return ConversationHandler.END

async def handle_select_chat_to_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.message.text.strip('⛧').strip()
        if chat_id.lower() == "отмена":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        active_chats = load_data()
        if chat_id not in active_chats:
            await update.message.reply_text("⛧ чат не найден ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        context.user_data['editing_chat_id'] = chat_id
        current_tmpl = active_chats[chat_id].get('template', '???')

        await update.message.reply_text(
            f"⛧ чат: {chat_id} ⛧\n"
            f"⛧ текущий шаблон: {current_tmpl} ⛧\n\n"
            f"⛧ выберите новый шаблон ⛧",
            reply_markup=templates_keyboard()
        )
        return SELECTING_NEW_TEMPLATE_FOR_CHAT
    except Exception as e:
        logger.error(f"ошибка в handle_select_chat_to_edit: {e}")
        return ConversationHandler.END

async def handle_new_template_for_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip('⛧').strip()
        if text.lower() == "отмена":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        templates = get_all_templates()
        if text not in templates:
            await update.message.reply_text("⛧ шаблон не найден, выберите из списка ⛧")
            return SELECTING_NEW_TEMPLATE_FOR_CHAT

        chat_id = context.user_data['editing_chat_id']
        chat_templates[chat_id] = text

        active_chats = load_data()
        if chat_id in active_chats:
            active_chats[chat_id]['template'] = text
            save_data(active_chats)

        lines = load_template_by_name(text)
        await update.message.reply_text(
            f"⛧ шаблон в чате {chat_id} изменён ⛧\n"
            f"⛧ новый шаблон: {text} ({len(lines)} строк) ⛧\n"
            f"⛧ следующие сообщения уже из нового шаблона ⛧",
            reply_markup=main_keyboard
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"ошибка в handle_new_template_for_chat: {e}")
        return ConversationHandler.END

# ─── НАЗАД ────────────────────────────────────────────────────────────────────

async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        await update.message.reply_text("⛧ главное меню ⛧", reply_markup=main_keyboard)
    except Exception as e:
        logger.error(f"ошибка в go_back: {e}")

# ─── НАЧАТЬ УТИЛИЗИРОВАНИЕ ────────────────────────────────────────────────────

async def start_utilization(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        templates = get_all_templates()
        if not templates:
            await update.message.reply_text(
                "⛧ нет загруженных шаблонов ⛧\n"
                "⛧ сначала загрузите шаблон ⛧",
                reply_markup=main_keyboard
            )
            return ConversationHandler.END
        await update.message.reply_text(
            "⛧ выберите шаблон для рассылки ⛧",
            reply_markup=templates_keyboard()
        )
        return SELECTING_TEMPLATE
    except Exception as e:
        logger.error(f"ошибка в start_utilization: {e}")
        return ConversationHandler.END

async def handle_select_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip('⛧').strip()
        if text.lower() == "отмена":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        templates = get_all_templates()
        if text not in templates:
            await update.message.reply_text("⛧ шаблон не найден, выберите из списка ⛧")
            return SELECTING_TEMPLATE

        context.user_data['selected_template'] = text
        await update.message.reply_text(
            f"⛧ выбран шаблон: {text} ⛧\n⛧ введите id чата ⛧",
            reply_markup=cancel_keyboard
        )
        return WAITING_CHAT_ID
    except Exception as e:
        logger.error(f"ошибка в handle_select_template: {e}")
        return ConversationHandler.END

async def handle_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.message.text
        if chat_id.lower() == "⛧ отмена ⛧":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END
        try:
            context.user_data['target_chat_id'] = int(chat_id)
            await update.message.reply_text("⛧ введите интервал (в секундах) ⛧", reply_markup=cancel_keyboard)
            return WAITING_INTERVAL
        except ValueError:
            await update.message.reply_text("⛧ неверный формат ID! должно быть число ⛧")
            return WAITING_CHAT_ID
    except Exception as e:
        logger.error(f"ошибка в handle_chat_id: {e}")
        return ConversationHandler.END

async def handle_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        interval = update.message.text
        if interval.lower() == "⛧ отмена ⛧":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END
        try:
            interval = int(interval)
            if interval <= 0:
                raise ValueError
            context.user_data['interval'] = interval
            await update.message.reply_text(
                "⛧ шапка (по желанию) ⛧\n"
                "⛧ введите текст перед каждым сообщением ⛧\n"
                "⛧ или нажмите 'без шапки' ⛧",
                reply_markup=header_keyboard
            )
            return WAITING_HEADER
        except ValueError:
            await update.message.reply_text("⛧ интервал должен быть числом больше 0! ⛧")
            return WAITING_INTERVAL
    except Exception as e:
        logger.error(f"ошибка в handle_interval: {e}")
        return ConversationHandler.END

async def handle_header(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        header = update.message.text
        if header.lower() == "⛧ отмена ⛧":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        header = "" if header.lower() == "⛧ без шапки ⛧" else header.strip()
        context.user_data['header'] = header

        await update.message.reply_text(
            "⛧ медиа (по желанию) ⛧\n"
            "⛧ вставьте ссылку на фото/медиа которое бот будет отправлять с сообщением ⛧\n"
            "⛧ или нажмите 'без медиа' ⛧",
            reply_markup=media_keyboard
        )
        return WAITING_MEDIA_URL
    except Exception as e:
        logger.error(f"ошибка в handle_header: {e}")
        return ConversationHandler.END

async def handle_media_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip()
        if text.lower() == "⛧ отмена ⛧":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        media_url = "" if text.lower() == "⛧ без медиа ⛧" else text

        chat_id = str(context.user_data['target_chat_id'])
        interval = context.user_data['interval']
        header = context.user_data['header']
        template_name = context.user_data['selected_template']

        chat_headers[chat_id] = header
        chat_templates[chat_id] = template_name
        chat_media[chat_id] = media_url
        stop_events[chat_id] = Event()

        active_chats = load_data()
        active_chats[chat_id] = {
            "interval": interval,
            "header": header,
            "template": template_name,
            "media_url": media_url
        }
        save_data(active_chats)

        if context.job_queue is None:
            await update.message.reply_text(
                "⛧ ошибка: job-queue не установлен ⛧\n"
                "⛧ выполните: pip install 'python-telegram-bot[job-queue]' ⛧",
                reply_markup=main_keyboard
            )
            return ConversationHandler.END

        current_jobs[chat_id] = context.job_queue.run_repeating(
            send_random_message,
            interval=interval,
            first=0,
            data={"chat_id": chat_id},
            name=str(chat_id)
        )

        lines = load_template_by_name(template_name)
        await update.message.reply_text(
            f"⛧ утилизирование запущено ⛧\n"
            f"⛧ чат: {chat_id} ⛧\n"
            f"⛧ шаблон: {template_name} ({len(lines)} строк) ⛧\n"
            f"⛧ интервал: {interval} сек ⛧\n"
            f"⛧ шапка: {header if header else 'отсутствует'} ⛧\n"
            f"⛧ медиа: {media_url if media_url else 'отсутствует'} ⛧",
            reply_markup=main_keyboard
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"ошибка в handle_media_url: {e}")
        await update.message.reply_text(f"⛧ ошибка: {str(e)} ⛧")
        return ConversationHandler.END

# ─── ОСТАНОВИТЬ РАССЫЛКУ ──────────────────────────────────────────────────────

async def stop_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        active_chats = load_data()
        if not active_chats:
            await update.message.reply_text("⛧ нет активных рассылок ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END
        keyboard = [[f"⛧ {chat_id} ⛧"] for chat_id in active_chats]
        keyboard.append(["⛧ отмена ⛧"])
        await update.message.reply_text(
            "⛧ выберите чат для остановки ⛧",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return SELECTING_CHAT_TO_STOP
    except Exception as e:
        logger.error(f"ошибка в stop_distribution: {e}")
        return ConversationHandler.END

async def handle_chat_to_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.message.text.strip('⛧').strip()
        if chat_id.lower() == "отмена":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        active_chats = load_data()
        if chat_id not in active_chats:
            await update.message.reply_text("⛧ чат не найден ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        if chat_id in current_jobs:
            current_jobs[chat_id].schedule_removal()
            del current_jobs[chat_id]
        if chat_id in stop_events:
            stop_events[chat_id].set()
            del stop_events[chat_id]
        if chat_id in chat_headers:
            del chat_headers[chat_id]
        if chat_id in chat_templates:
            del chat_templates[chat_id]
        if chat_id in chat_media:
            del chat_media[chat_id]

        active_chats.pop(chat_id)
        save_data(active_chats)

        await update.message.reply_text(f"⛧ рассылка остановлена в чате {chat_id} ⛧", reply_markup=main_keyboard)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"ошибка в handle_chat_to_stop: {e}")
        return ConversationHandler.END

# ─── СТАТИСТИКА ───────────────────────────────────────────────────────────────

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        active_chats = load_data()
        if not active_chats and not send_counts:
            await update.message.reply_text("⛧ статистика пуста ⛧", reply_markup=main_keyboard)
            return
        msg = "⛧ статистика отправок ⛧\n\n"
        for chat_id in active_chats:
            count = send_counts.get(chat_id, 0)
            tmpl = active_chats[chat_id].get('template', '???')
            msg += f"⛧ чат {chat_id} [{tmpl}]: {count} сообщений ⛧\n"
        await update.message.reply_text(msg, reply_markup=main_keyboard)
    except Exception as e:
        logger.error(f"ошибка в show_stats: {e}")

# ─── ВОССТАНОВИТЬ ЗАДАЧИ ──────────────────────────────────────────────────────

async def request_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        await update.message.reply_text(
            "⛧ отправьте файл active_chats.json ⛧\n"
            "⛧ все задачи будут восстановлены и запущены ⛧",
            reply_markup=cancel_keyboard
        )
        return UPLOADING_RESTORE_FILE
    except Exception as e:
        logger.error(f"ошибка в request_restore: {e}")
        return ConversationHandler.END

async def handle_restore_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message.text and update.message.text.strip() == "⛧ отмена ⛧":
            await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        document = update.message.document
        if not document:
            await update.message.reply_text("⛧ нужен json файл ⛧")
            return UPLOADING_RESTORE_FILE

        file = await context.bot.get_file(document.file_id)
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

        try:
            with open(tmp_path, 'r') as f:
                restored = json.load(f)
        except Exception:
            await update.message.reply_text("⛧ ошибка: неверный json файл ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END
        finally:
            os.unlink(tmp_path)

        if not isinstance(restored, dict):
            await update.message.reply_text("⛧ ошибка: неверный формат данных ⛧", reply_markup=main_keyboard)
            return ConversationHandler.END

        # остановить все текущие задачи
        for chat_id in list(current_jobs.keys()):
            try:
                current_jobs[chat_id].schedule_removal()
            except Exception:
                pass
        current_jobs.clear()
        stop_events.clear()
        chat_headers.clear()
        chat_templates.clear()
        chat_media.clear()

        # сохранить и запустить восстановленные задачи
        save_data(restored)
        started = 0
        for chat_id, info in restored.items():
            stop_events[chat_id] = Event()
            chat_headers[chat_id] = info.get('header', '')
            chat_templates[chat_id] = info.get('template', '')
            chat_media[chat_id] = info.get('media_url', '')
            interval = info.get('interval', 60)
            if context.job_queue:
                current_jobs[chat_id] = context.job_queue.run_repeating(
                    send_random_message,
                    interval=interval,
                    first=5,
                    data={"chat_id": chat_id},
                    name=str(chat_id)
                )
                started += 1

        await update.message.reply_text(
            f"⛧ восстановление завершено ⛧\n"
            f"⛧ чатов загружено: {len(restored)} ⛧\n"
            f"⛧ задач запущено: {started} ⛧",
            reply_markup=main_keyboard
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"ошибка в handle_restore_file: {e}")
        await update.message.reply_text(f"⛧ ошибка: {str(e)} ⛧", reply_markup=main_keyboard)
        return ConversationHandler.END

# ─── ОТМЕНА ───────────────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("⛧ действие отменено ⛧", reply_markup=main_keyboard)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"ошибка в cancel: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if isinstance(error, (TimedOut, NetworkError)):
        return
    if isinstance(error, RetryAfter):
        return
    logger.error(f"ошибка в обработчике: {error}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    try:
        ensure_templates_dir()
        active_chats = load_data()
        for chat_id, info in active_chats.items():
            stop_events[chat_id] = Event()
            chat_headers[chat_id] = info.get('header', '')
            chat_templates[chat_id] = info.get('template', '')
            chat_media[chat_id] = info.get('media_url', '')

        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .connect_timeout(30)
            .read_timeout(30)
            .write_timeout(30)
            .pool_timeout(30)
            .build()
        )

        # авто-запуск активных задач после старта бота
        async def auto_start_jobs(app):
            saved = load_data()
            for chat_id, info in saved.items():
                interval = info.get('interval', 60)
                if app.job_queue:
                    current_jobs[chat_id] = app.job_queue.run_repeating(
                        send_random_message,
                        interval=interval,
                        first=10,
                        data={"chat_id": chat_id},
                        name=str(chat_id)
                    )
            count = len(saved)
            if count:
                try:
                    await app.bot.send_message(
                        ALLOWED_USER_ID,
                        f"⛧ бот перезапущен ⛧\n⛧ авто-восстановлено задач: {count} ⛧"
                    )
                except Exception:
                    pass

        app.post_init = auto_start_jobs

        upload_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⛧ загрузить шаблоны ⛧$"), request_upload_template)],
            states={
                UPLOADING_TEMPLATE: [
                    MessageHandler(filters.Document.ALL, handle_upload_template),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_upload_template),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        upload_media_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⛧ загрузить медиа ⛧$"), request_upload_media)],
            states={
                UPLOADING_MEDIA: [
                    MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, handle_upload_media),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_upload_media),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        delete_template_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⛧ удалить шаблон ⛧$"), start_delete_template)],
            states={
                SELECTING_TEMPLATE_TO_DELETE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delete_template),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        start_utilization_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⛧ начать утилизирование ⛧$"), start_utilization)],
            states={
                SELECTING_TEMPLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_select_template)],
                WAITING_CHAT_ID:    [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_id)],
                WAITING_INTERVAL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interval)],
                WAITING_HEADER:     [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_header)],
                WAITING_MEDIA_URL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_media_url)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        stop_distribution_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⛧ остановить рассылку ⛧$"), stop_distribution)],
            states={
                SELECTING_CHAT_TO_STOP: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat_to_stop)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        change_template_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⛧ изменить шаблон в чате ⛧$"), start_change_chat_template)],
            states={
                SELECTING_CHAT_TO_EDIT:          [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_select_chat_to_edit)],
                SELECTING_NEW_TEMPLATE_FOR_CHAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_template_for_chat)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        change_header_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⛧ изменить шапку в чате ⛧$"), start_change_chat_header)],
            states={
                SELECTING_CHAT_FOR_HEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_select_chat_for_header)],
                WAITING_NEW_HEADER:        [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_header)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        change_media_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⛧ изменить медиа в чате ⛧$"), start_change_chat_media)],
            states={
                SELECTING_CHAT_FOR_MEDIA: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_select_chat_for_media)],
                WAITING_NEW_MEDIA_URL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_media_url)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        restore_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⛧ восстановить ⛧$"), request_restore)],
            states={
                UPLOADING_RESTORE_FILE: [
                    MessageHandler(filters.Document.ALL, handle_restore_file),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_restore_file),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )

        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_chat_members))
        app.add_handler(upload_handler)
        app.add_handler(upload_media_handler)
        app.add_handler(delete_template_handler)
        app.add_handler(start_utilization_handler)
        app.add_handler(stop_distribution_handler)
        app.add_handler(change_template_handler)
        app.add_handler(change_header_handler)
        app.add_handler(change_media_handler)
        app.add_handler(restore_handler)
        app.add_handler(MessageHandler(filters.Regex("^⛧ все шаблоны ⛧$"), show_all_templates))
        app.add_handler(MessageHandler(filters.Regex("^⛧ активные чаты ⛧$"), show_active_chats))
        app.add_handler(MessageHandler(filters.Regex("^⛧ назад ⛧$"), go_back))
        app.add_handler(MessageHandler(filters.Regex("^⛧ статистика ⛧$"), show_stats))
        app.add_handler(MessageHandler(filters.Regex("^⛧ информация ⛧$"), show_info))
        app.add_error_handler(error_handler)

        print("⛧omyt ebashit ot tokena⛧")
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
    except Exception as e:
        print(f"⛧{e}⛧")

if __name__ == "__main__":
    main()
