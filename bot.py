from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import aiofiles
from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in .env")

if not OWNER_ID_RAW.isdigit():
    raise RuntimeError("OWNER_ID is missing or invalid in .env")

OWNER_ID = int(OWNER_ID_RAW)

DATA_FILE = Path("config.json")

# 30 minutes visible
VISIBLE_SECONDS = 30 * 60

# 10 minutes deleted
DELETED_SECONDS = 10 * 60

# How many retries for Telegram temporary errors
MAX_RETRIES = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("cycle-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# One scheduler per chat
scheduler_tasks: dict[int, asyncio.Task] = {}

# Per-chat locks
chat_locks: dict[int, asyncio.Lock] = {}

# Selected chat for owner
selected_chat: dict[int, int] = {}

# ============================================================
# DEFAULT SETTINGS
# ============================================================

DEFAULT_MESSAGE = (
    "<b>♛ IMPORTANT</b>\n\n"
    "Welcome to <b>{chat_title}</b>.\n\n"
    "Stay connected and check the buttons below."
)

DEFAULT_BUTTONS: list[list[dict[str, str]]] = [
    [
        {
            "text": "🔗 Open",
            "url": "https://t.me/"
        }
    ]
]

# ============================================================
# FILE STORAGE
# ============================================================

db_lock = asyncio.Lock()


async def ensure_database() -> None:
    if not DATA_FILE.exists():
        async with aiofiles.open(DATA_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps({"chats": {}}, indent=4))


async def load_database() -> dict[str, Any]:
    await ensure_database()

    async with aiofiles.open(DATA_FILE, "r", encoding="utf-8") as f:
        content = await f.read()

    if not content.strip():
        return {"chats": {}}

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.error("config.json is corrupted. Resetting database.")
        return {"chats": {}}

    if not isinstance(data, dict):
        return {"chats": {}}

    data.setdefault("chats", {})

    return data


async def save_database(data: dict[str, Any]) -> None:
    temp_file = DATA_FILE.with_suffix(".tmp")

    async with aiofiles.open(temp_file, "w", encoding="utf-8") as f:
        await f.write(
            json.dumps(
                data,
                indent=4,
                ensure_ascii=False,
            )
        )

    os.replace(temp_file, DATA_FILE)


async def get_chat_config(chat_id: int) -> dict[str, Any] | None:
    async with db_lock:
        data = await load_database()

        return data["chats"].get(str(chat_id))


async def create_chat_config(
    chat_id: int,
    chat_title: str,
    chat_type: str,
) -> dict[str, Any]:

    async with db_lock:
        data = await load_database()

        key = str(chat_id)

        if key not in data["chats"]:
            data["chats"][key] = {
                "chat_id": chat_id,
                "title": chat_title,
                "type": chat_type,
                "enabled": True,
                "message": DEFAULT_MESSAGE,
                "buttons": DEFAULT_BUTTONS,
                "last_message_id": None
            }

            await save_database(data)

        else:
            data["chats"][key]["title"] = chat_title
            data["chats"][key]["type"] = chat_type

            await save_database(data)

        return data["chats"][key]


async def update_chat_config(
    chat_id: int,
    **updates: Any,
) -> bool:

    async with db_lock:
        data = await load_database()

        key = str(chat_id)

        if key not in data["chats"]:
            return False

        data["chats"][key].update(updates)

        await save_database(data)

    return True


async def delete_chat_config(chat_id: int) -> None:
    async with db_lock:
        data = await load_database()

        data["chats"].pop(str(chat_id), None)

        await save_database(data)


# ============================================================
# HELPERS
# ============================================================

def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()

    return chat_locks[chat_id]


def escape_placeholder_title(text: str) -> str:
    # Telegram HTML-safe title
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_message_text(
    template: str,
    chat_title: str,
) -> str:

    return template.replace(
        "{chat_title}",
        escape_placeholder_title(chat_title),
    )


def build_keyboard(
    buttons: list[list[dict[str, str]]]
) -> InlineKeyboardMarkup | None:

    rows = []

    for row in buttons:
        keyboard_row = []

        for button in row:
            text = str(button.get("text", "")).strip()
            url = str(button.get("url", "")).strip()

            if not text or not url:
                continue

            keyboard_row.append(
                InlineKeyboardButton(
                    text=text,
                    url=url,
                )
            )

        if keyboard_row:
            rows.append(keyboard_row)

    if not rows:
        return None

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def telegram_call(func, *args, **kwargs):
    """
    Telegram API wrapper with retry/backoff.

    Handles:
    - RetryAfter
    - Network errors
    - Temporary Telegram errors
    """

    delay = 2

    for attempt in range(MAX_RETRIES):

        try:
            return await func(*args, **kwargs)

        except TelegramRetryAfter as e:
            wait_time = max(int(e.retry_after), 1)

            logger.warning(
                "Telegram rate limit. Sleeping %s seconds.",
                wait_time,
            )

            await asyncio.sleep(wait_time)

        except TelegramNetworkError:
            if attempt == MAX_RETRIES - 1:
                raise

            logger.warning(
                "Network error. Retry %s/%s.",
                attempt + 1,
                MAX_RETRIES,
            )

            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

        except TelegramBadRequest:
            raise

        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise

            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    return None


async def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


async def get_selected_chat(user_id: int) -> int | None:
    return selected_chat.get(user_id)


# ============================================================
# CHAT ADMIN CHECK
# ============================================================

async def bot_can_manage_chat(chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id,
            (await bot.get_me()).id,
        )

        return member.status == ChatMemberStatus.ADMINISTRATOR

    except Exception:
        return False


async def check_chat_permissions(chat_id: int) -> str:
    try:
        me = await bot.get_me()

        member = await bot.get_chat_member(
            chat_id,
            me.id,
        )

        if member.status not in {
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            return "❌ Bot is not an administrator."

        if member.status == ChatMemberStatus.CREATOR:
            return "OK"

        # aiogram ChatMemberAdministrator fields
        can_post = getattr(member, "can_post_messages", True)
        can_delete = getattr(member, "can_delete_messages", True)
        can_pin = getattr(member, "can_pin_messages", True)

        if not can_post:
            return "❌ Bot needs permission to post messages."

        if not can_delete:
            return "❌ Bot needs permission to delete messages."

        if not can_pin:
            return "❌ Bot needs permission to pin messages."

        return "OK"

    except TelegramForbiddenError:
        return "❌ Bot doesn't have access to this chat."

    except Exception as e:
        logger.exception("Permission check failed: %s", e)
        return "❌ Unable to check bot permissions."


# ============================================================
# MESSAGE OPERATIONS
# ============================================================

async def delete_old_bot_message(chat_id: int) -> None:

    config = await get_chat_config(chat_id)

    if not config:
        return

    old_message_id = config.get("last_message_id")

    if not old_message_id:
        return

    try:
        await telegram_call(
            bot.delete_message,
            chat_id,
            int(old_message_id),
        )

    except TelegramBadRequest as e:
        # Message already deleted / not found
        logger.debug(
            "Old message deletion skipped for %s: %s",
            chat_id,
            e,
        )

    except TelegramForbiddenError:
        logger.warning(
            "No permission to delete message in %s",
            chat_id,
        )

    except Exception:
        logger.exception(
            "Failed deleting old message in %s",
            chat_id,
        )

    await update_chat_config(
        chat_id,
        last_message_id=None,
    )


async def send_cycle_message(chat_id: int) -> int | None:

    config = await get_chat_config(chat_id)

    if not config or not config.get("enabled", True):
        return None

    title = config.get("title", "this chat")
    template = config.get("message", DEFAULT_MESSAGE)
    buttons = config.get("buttons", [])

    text = build_message_text(
        template,
        title,
    )

    keyboard = build_keyboard(buttons)

    # Make sure our previous message doesn't remain.
    await delete_old_bot_message(chat_id)

    try:
        sent = await telegram_call(
            bot.send_message,
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    except TelegramForbiddenError:
        logger.warning(
            "Bot lost access to chat %s",
            chat_id,
        )

        await stop_scheduler(chat_id)

        return None

    except TelegramBadRequest as e:
        logger.error(
            "Could not send message to %s: %s",
            chat_id,
            e,
        )

        return None

    # Save current message ID
    await update_chat_config(
        chat_id,
        last_message_id=sent.message_id,
    )

    # Pin OUR message only.
    try:
        await telegram_call(
            bot.pin_chat_message,
            chat_id=chat_id,
            message_id=sent.message_id,
            disable_notification=True,
        )

    except TelegramBadRequest as e:
        logger.warning(
            "Could not pin message in %s: %s",
            chat_id,
            e,
        )

    except TelegramForbiddenError:
        logger.warning(
            "No pin permission in %s",
            chat_id,
        )

    return sent.message_id


# ============================================================
# SCHEDULER
# ============================================================

async def scheduler_loop(chat_id: int) -> None:

    logger.info(
        "Scheduler started for %s",
        chat_id,
    )

    try:

        while True:

            config = await get_chat_config(chat_id)

            if not config:
                break

            if not config.get("enabled", True):
                await asyncio.sleep(5)
                continue

            lock = get_lock(chat_id)

            async with lock:

                message_id = await send_cycle_message(
                    chat_id
                )

            if message_id:

                # Message remains visible for 30 minutes.
                await asyncio.sleep(
                    VISIBLE_SECONDS
                )

                config = await get_chat_config(chat_id)

                if not config:
                    break

                # Delete only our own message.
                async with lock:
                    await delete_old_bot_message(
                        chat_id
                    )

                # Keep it deleted for 10 minutes.
                await asyncio.sleep(
                    DELETED_SECONDS
                )

            else:
                # Avoid rapid retry if Telegram temporarily
                # rejected the message.
                await asyncio.sleep(60)

    except asyncio.CancelledError:
        logger.info(
            "Scheduler stopped for %s",
            chat_id,
        )

        raise

    except Exception:
        logger.exception(
            "Scheduler crashed for %s",
            chat_id,
        )


def start_scheduler(chat_id: int) -> None:

    existing = scheduler_tasks.get(chat_id)

    if existing and not existing.done():
        return

    scheduler_tasks[chat_id] = asyncio.create_task(
        scheduler_loop(chat_id)
    )


async def stop_scheduler(chat_id: int) -> None:

    task = scheduler_tasks.pop(chat_id, None)

    if task and not task.done():

        current = asyncio.current_task()

        if task is not current:
            task.cancel()

            try:
                await task
            except asyncio.CancelledError:
                pass


# ============================================================
# OWNER KEYBOARD
# ============================================================

def owner_menu() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 My Chats",
                    callback_data="owner_chats",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Current Chat",
                    callback_data="owner_current",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❓ Help",
                    callback_data="owner_help",
                ),
            ],
        ]
    )


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:

        await message.answer(
            "👋 Hello!\n\n"
            "I am a scheduled message bot."
        )

        return

    await message.answer(
        "<b>♛ Cycle Message Bot</b>\n\n"
        "Bot is online.\n\n"
        "Use the buttons below to manage your chats.",
        parse_mode="HTML",
        reply_markup=owner_menu(),
    )


# ============================================================
# HELP
# ============================================================

@dp.message(Command("help"))
async def help_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    text = """
<b>♛ CYCLE MESSAGE BOT</b>

<b>Basic flow:</b>
• 30 minutes message visible
• 10 minutes message deleted
• Repeat

<b>Commands:</b>

/chats
/select CHAT_ID
/status

/setmessage YOUR HTML MESSAGE

/addbutton Button Name | https://example.com
/removebutton NUMBER
/buttons
/clearbuttons

/enable
/disable
/send

<b>Variables:</b>

{chat_title}

<b>HTML example:</b>

&lt;b&gt;Hello&lt;/b&gt;
&lt;i&gt;This is italic&lt;/i&gt;
&lt;u&gt;Underline&lt;/u&gt;

<b>Button example:</b>

<code>/addbutton 🔗 Open Channel | https://t.me/example</code>

The bot only deletes messages that it created itself.
"""

    await message.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ============================================================
# LIST CHATS
# ============================================================

@dp.message(Command("chats"))
async def chats_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    data = await load_database()
    chats = data.get("chats", {})

    if not chats:
        await message.answer(
            "📭 No chats registered yet.\n\n"
            "Add the bot to a group/channel first."
        )
        return

    lines = [
        "<b>📋 Registered Chats</b>\n"
    ]

    for key, config in chats.items():

        status = (
            "🟢 ON"
            if config.get("enabled", True)
            else "🔴 OFF"
        )

        lines.append(
            f"{status} "
            f"<b>{escape_placeholder_title(config.get('title', 'Unknown'))}</b>\n"
            f"<code>{key}</code>\n"
            f"Type: {config.get('type', 'unknown')}\n"
        )

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
    )


# ============================================================
# SELECT CHAT
# ============================================================

@dp.message(Command("select"))
async def select_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:
        await message.answer(
            "Usage:\n"
            "<code>/select CHAT_ID</code>\n\n"
            "Example:\n"
            "<code>/select -1001234567890</code>",
            parse_mode="HTML",
        )
        return

    try:
        chat_id = int(parts[1].strip())
    except ValueError:
        await message.answer("❌ Invalid chat ID.")
        return

    config = await get_chat_config(chat_id)

    if not config:
        await message.answer(
            "❌ This chat is not registered.\n\n"
            "Add the bot to the group/channel first."
        )
        return

    selected_chat[message.from_user.id] = chat_id

    await message.answer(
        f"✅ Selected:\n"
        f"<b>{escape_placeholder_title(config.get('title', 'Unknown'))}</b>\n\n"
        f"Chat ID: <code>{chat_id}</code>",
        parse_mode="HTML",
    )


# ============================================================
# STATUS
# ============================================================

@dp.message(Command("status"))
async def status_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    chat_id = await get_selected_chat(
        message.from_user.id
    )

    if chat_id is None:
        await message.answer(
            "❌ Select a chat first:\n"
            "<code>/select CHAT_ID</code>",
            parse_mode="HTML",
        )
        return

    config = await get_chat_config(chat_id)

    if not config:
        await message.answer("❌ Chat not found.")
        return

    task = scheduler_tasks.get(chat_id)

    scheduler_status = (
        "🟢 Running"
        if task and not task.done()
        else "🔴 Stopped"
    )

    enabled = (
        "🟢 Enabled"
        if config.get("enabled", True)
        else "🔴 Disabled"
    )

    buttons_count = sum(
        len(row)
        for row in config.get("buttons", [])
    )

    text = (
        "<b>⚙️ Chat Status</b>\n\n"
        f"Name: <b>{escape_placeholder_title(config.get('title', 'Unknown'))}</b>\n"
        f"ID: <code>{chat_id}</code>\n"
        f"Type: <code>{config.get('type', 'unknown')}</code>\n\n"
        f"Message: {enabled}\n"
        f"Scheduler: {scheduler_status}\n"
        f"Buttons: {buttons_count}\n\n"
        "Cycle:\n"
        "🟢 30 min visible\n"
        "🔴 10 min deleted"
    )

    await message.answer(
        text,
        parse_mode="HTML",
    )


# ============================================================
# SET MESSAGE
# ============================================================

@dp.message(Command("setmessage"))
async def setmessage_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    chat_id = await get_selected_chat(
        message.from_user.id
    )

    if chat_id is None:
        await message.answer(
            "❌ Select a chat first."
        )
        return

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) != 2:
        await message.answer(
            "<b>Usage:</b>\n\n"
            "<code>/setmessage Your message</code>\n\n"
            "<b>HTML example:</b>\n"
            "<code>/setmessage &lt;b&gt;Hello&lt;/b&gt;</code>\n\n"
            "Available variable:\n"
            "<code>{chat_title}</code>",
            parse_mode="HTML",
        )
        return

    new_message = parts[1].strip()

    if len(new_message) > 4096:
        await message.answer(
            "❌ Message is too long. "
            "Telegram allows up to 4096 characters."
        )
        return

    await update_chat_config(
        chat_id,
        message=new_message,
    )

    await message.answer(
        "✅ Message updated.\n\n"
        "It will be used on the next cycle."
    )


# ============================================================
# BUTTONS
# ============================================================

@dp.message(Command("buttons"))
async def buttons_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    chat_id = await get_selected_chat(
        message.from_user.id
    )

    if chat_id is None:
        await message.answer(
            "❌ Select a chat first."
        )
        return

    config = await get_chat_config(chat_id)

    if not config:
        await message.answer("❌ Chat not found.")
        return

    buttons = config.get("buttons", [])

    if not buttons:
        await message.answer(
            "📭 No buttons configured."
        )
        return

    lines = ["<b>🔘 Buttons</b>\n"]

    number = 1

    for row_index, row in enumerate(buttons, start=1):

        for button in row:

            lines.append(
                f"{number}. "
                f"<b>{escape_placeholder_title(button.get('text', ''))}</b>\n"
                f"   {escape_placeholder_title(button.get('url', ''))}"
            )

            number += 1

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@dp.message(Command("addbutton"))
async def addbutton_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    chat_id = await get_selected_chat(
        message.from_user.id
    )

    if chat_id is None:
        await message.answer(
            "❌ Select a chat first."
        )
        return

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) != 2 or "|" not in parts[1]:

        await message.answer(
            "<b>Usage:</b>\n\n"
            "<code>/addbutton Button Name | https://example.com</code>\n\n"
            "Example:\n"
            "<code>/addbutton 📥 Download | https://example.com</code>",
            parse_mode="HTML",
        )

        return

    button_text, url = parts[1].split(
        "|",
        1,
    )

    button_text = button_text.strip()
    url = url.strip()

    if not button_text:
        await message.answer(
            "❌ Button text cannot be empty."
        )
        return

    if not (
        url.startswith("https://")
        or url.startswith("http://")
        or url.startswith("tg://")
    ):
        await message.answer(
            "❌ Only http://, https:// or tg:// URLs are allowed."
        )
        return

    config = await get_chat_config(chat_id)

    buttons = config.get(
        "buttons",
        [],
    )

    # Add as a new row.
    buttons.append(
        [
            {
                "text": button_text,
                "url": url,
            }
        ]
    )

    await update_chat_config(
        chat_id,
        buttons=buttons,
    )

    await message.answer(
        "✅ Button added as a new row."
    )


@dp.message(Command("removebutton"))
async def removebutton_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    chat_id = await get_selected_chat(
        message.from_user.id
    )

    if chat_id is None:
        await message.answer(
            "❌ Select a chat first."
        )
        return

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) != 2 or not parts[1].strip().isdigit():
        await message.answer(
            "Usage:\n"
            "<code>/removebutton NUMBER</code>",
            parse_mode="HTML",
        )
        return

    target = int(parts[1].strip())

    config = await get_chat_config(chat_id)

    buttons = config.get(
        "buttons",
        [],
    )

    flat_buttons = []

    for row_index, row in enumerate(buttons):

        for button_index, button in enumerate(row):

            flat_buttons.append(
                (
                    row_index,
                    button_index,
                )
            )

    if target < 1 or target > len(flat_buttons):
        await message.answer(
            "❌ Button number doesn't exist."
        )
        return

    row_index, button_index = flat_buttons[
        target - 1
    ]

    buttons[row_index].pop(button_index)

    # Remove empty rows
    buttons = [
        row
        for row in buttons
        if row
    ]

    await update_chat_config(
        chat_id,
        buttons=buttons,
    )

    await message.answer(
        f"✅ Button #{target} removed."
    )


@dp.message(Command("clearbuttons"))
async def clearbuttons_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    chat_id = await get_selected_chat(
        message.from_user.id
    )

    if chat_id is None:
        await message.answer(
            "❌ Select a chat first."
        )
        return

    await update_chat_config(
        chat_id,
        buttons=[],
    )

    await message.answer(
        "✅ All buttons removed."
    )


# ============================================================
# ENABLE / DISABLE
# ============================================================

@dp.message(Command("enable"))
async def enable_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    chat_id = await get_selected_chat(
        message.from_user.id
    )

    if chat_id is None:
        await message.answer(
            "❌ Select a chat first."
        )
        return

    await update_chat_config(
        chat_id,
        enabled=True,
    )

    start_scheduler(chat_id)

    await message.answer(
        "🟢 Scheduler enabled."
    )


@dp.message(Command("disable"))
async def disable_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    chat_id = await get_selected_chat(
        message.from_user.id
    )

    if chat_id is None:
        await message.answer(
            "❌ Select a chat first."
        )
        return

    await update_chat_config(
        chat_id,
        enabled=False,
    )

    await stop_scheduler(chat_id)

    await delete_old_bot_message(
        chat_id
    )

    await message.answer(
        "🔴 Scheduler disabled.\n"
        "Current bot message was removed."
    )


# ============================================================
# MANUAL SEND
# ============================================================

@dp.message(Command("send"))
async def manual_send_handler(message: Message):

    if not message.from_user:
        return

    if message.from_user.id != OWNER_ID:
        return

    chat_id = await get_selected_chat(
        message.from_user.id
    )

    if chat_id is None:
        await message.answer(
            "❌ Select a chat first."
        )
        return

    lock = get_lock(chat_id)

    async with lock:
        result = await send_cycle_message(
            chat_id
        )

    if result:
        await message.answer(
            "✅ Message sent and pinned."
        )
    else:
        await message.answer(
            "❌ Message could not be sent."
        )


# ============================================================
# INLINE MENU CALLBACKS
# ============================================================

@dp.callback_query(F.data == "owner_help")
async def owner_help_callback(
    callback: CallbackQuery,
):

    if callback.from_user.id != OWNER_ID:
        await callback.answer(
            "Not authorized.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        "<b>Quick Help</b>\n\n"
        "1. Add bot to your group/channel.\n"
        "2. Use /chats.\n"
        "3. Select it with /select CHAT_ID.\n"
        "4. Set your message with /setmessage.\n"
        "5. Add buttons using /addbutton.\n\n"
        "Cycle = 30 min ON + 10 min OFF.",
        parse_mode="HTML",
    )

    await callback.answer()


@dp.callback_query(F.data == "owner_chats")
async def owner_chats_callback(
    callback: CallbackQuery,
):

    if callback.from_user.id != OWNER_ID:
        await callback.answer(
            "Not authorized.",
            show_alert=True,
        )
        return

    data = await load_database()
    chats = data.get("chats", {})

    if not chats:
        await callback.message.answer(
            "📭 No chats registered."
        )
        await callback.answer()
        return

    lines = ["<b>📋 Chats</b>\n"]

    for key, config in chats.items():

        status = (
            "🟢"
            if config.get("enabled", True)
            else "🔴"
        )

        lines.append(
            f"{status} "
            f"<b>{escape_placeholder_title(config.get('title', 'Unknown'))}</b>\n"
            f"<code>{key}</code>\n"
        )

    await callback.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
    )

    await callback.answer()


@dp.callback_query(F.data == "owner_current")
async def owner_current_callback(
    callback: CallbackQuery,
):

    if callback.from_user.id != OWNER_ID:
        await callback.answer(
            "Not authorized.",
            show_alert=True,
        )
        return

    chat_id = selected_chat.get(
        callback.from_user.id
    )

    if not chat_id:
        await callback.message.answer(
            "❌ No chat selected."
        )
        await callback.answer()
        return

    config = await get_chat_config(
        chat_id
    )

    if not config:
        await callback.message.answer(
            "❌ Chat no longer exists."
        )
        await callback.answer()
        return

    await callback.message.answer(
        f"<b>Current Chat</b>\n\n"
        f"Name: {escape_placeholder_title(config.get('title', 'Unknown'))}\n"
        f"ID: <code>{chat_id}</code>\n"
        f"Enabled: {config.get('enabled', True)}",
        parse_mode="HTML",
    )

    await callback.answer()


# ============================================================
# AUTO REGISTER WHEN BOT IS ADDED
# ============================================================

@dp.my_chat_member()
async def my_chat_member_handler(
    event: ChatMemberUpdated,
):

    chat = event.chat

    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status

    # Bot was added / promoted
    active_statuses = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }

    if (
        new_status in active_statuses
        and old_status not in active_statuses
    ):

        config = await create_chat_config(
            chat_id=chat.id,
            chat_title=chat.title or "Telegram Chat",
            chat_type=chat.type,
        )

        permission_status = await check_chat_permissions(
            chat.id
        )

        if permission_status != "OK":

            try:
                await bot.send_message(
                    chat.id,
                    "⚠️ <b>Cycle Message Bot Added</b>\n\n"
                    f"{permission_status}\n\n"
                    "Please give me the required admin permissions:\n"
                    "• Send messages\n"
                    "• Delete messages\n"
                    "• Pin messages",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            return

        try:
            await bot.send_message(
                chat.id,
                "✅ <b>Cycle Message Bot Added</b>\n\n"
                "The scheduled message system is now active.\n\n"
                "🟢 Message: 30 minutes\n"
                "🔴 Deleted: 10 minutes\n"
                "🔁 Automatic repeat\n\n"
                "The bot will only delete messages created by itself.",
                parse_mode="HTML",
            )

        except Exception:
            logger.exception(
                "Could not send added message."
            )

        start_scheduler(chat.id)

        # Notify owner
        try:
            await bot.send_message(
                OWNER_ID,
                "➕ <b>New chat added</b>\n\n"
                f"Name: <b>{escape_placeholder_title(chat.title or 'Unknown')}</b>\n"
                f"ID: <code>{chat.id}</code>\n"
                f"Type: <code>{chat.type}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    # Bot removed / kicked
    elif (
        new_status in {
            ChatMemberStatus.LEFT,
            ChatMemberStatus.KICKED,
        }
    ):

        await stop_scheduler(chat.id)

        await delete_chat_config(
            chat.id
        )

        logger.info(
            "Bot removed from %s. Configuration deleted.",
            chat.id,
        )

        try:
            await bot.send_message(
                OWNER_ID,
                "➖ <b>Bot removed</b>\n\n"
                f"Chat ID: <code>{chat.id}</code>\n"
                "Scheduler stopped.",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ============================================================
# HEALTH SERVER
# ============================================================

async def health_handler(
    request: web.Request,
):
    return web.json_response(
        {
            "status": "ok",
            "bot": "online",
            "schedulers": len(
                scheduler_tasks
            ),
        }
    )


async def start_health_server() -> web.AppRunner:

    app = web.Application()

    app.router.add_get(
        "/",
        health_handler,
    )

    app.router.add_get(
        "/health",
        health_handler,
    )

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "8080",
        )
    )

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=port,
    )

    await site.start()

    logger.info(
        "Health server running on port %s",
        port,
    )

    return runner


# ============================================================
# STARTUP
# ============================================================

async def restore_schedulers() -> None:

    data = await load_database()

    chats = data.get(
        "chats",
        {},
    )

    for key, config in chats.items():

        try:
            chat_id = int(key)
        except ValueError:
            continue

        if not config.get(
            "enabled",
            True,
        ):
            continue

        try:

            member = await bot.get_chat_member(
                chat_id,
                (await bot.get_me()).id,
            )

            if member.status in {
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            }:

                start_scheduler(
                    chat_id
                )

                logger.info(
                    "Restored scheduler for %s",
                    chat_id,
                )

            else:
                logger.info(
                    "Bot is no longer active in %s",
                    chat_id,
                )

        except Exception as e:

            logger.warning(
                "Could not restore scheduler for %s: %s",
                chat_id,
                e,
            )


async def main():

    await ensure_database()

    me = await bot.get_me()

    logger.info(
        "Bot authenticated: @%s",
        me.username,
    )

    health_runner = await start_health_server()

    await restore_schedulers()

    try:

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:

        for task in list(
            scheduler_tasks.values()
        ):

            if not task.done():
                task.cancel()

        await asyncio.gather(
            *scheduler_tasks.values(),
            return_exceptions=True,
        )

        await health_runner.cleanup()

        await bot.session.close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info(
            "Bot stopped by user."
        )
