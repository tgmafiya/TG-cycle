from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_IDS = {
    int(x.strip())
    for x in os.getenv("OWNER_IDS", "").split(",")
    if x.strip().isdigit()
}

PORT = int(os.getenv("PORT", "8080"))
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

if not OWNER_IDS:
    raise RuntimeError("OWNER_IDS missing")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("network-bot")

router = Router()
db_lock = asyncio.Lock()


DEFAULT_DB = {
    "communities": {},
    "settings": {
        "promotion_enabled": True,
        "rotation_enabled": True,
        "promotion_interval": 3600,
        "links_per_post": 5,
        "welcome_enabled": True,
        "welcome_text": (
            "🔞 <b>WELCOME TO OUR NETWORK</b>\n\n"
            "Choose a private community below."
        ),
    },
    "stats": {
        "promotions": 0,
        "promotion_failures": 0,
        "join_requests": 0,
        "approved_requests": 0,
    },
}


# =========================================================
# DATABASE
# =========================================================

async def load_db() -> dict[str, Any]:
    if not DATA_FILE.exists():
        await save_db(DEFAULT_DB)

    try:
        data = json.loads(
            DATA_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        data = {}

    for key, value in DEFAULT_DB.items():
        if key not in data:
            data[key] = value.copy()

    return data


async def save_db(data: dict[str, Any]):
    tmp = DATA_FILE.with_suffix(".tmp")

    tmp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8",
    )

    tmp.replace(DATA_FILE)


async def update_db(callback):
    async with db_lock:
        db = await load_db()

        result = callback(db)

        if asyncio.iscoroutine(result):
            result = await result

        await save_db(db)

        return result


async def get_db():
    async with db_lock:
        return await load_db()


# =========================================================
# AUTH
# =========================================================

def is_owner(user_id: int | None) -> bool:
    return user_id in OWNER_IDS


# =========================================================
# ADMIN PANEL
# =========================================================

def admin_panel():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="➕ ADD COMMUNITY",
                    callback_data="add_info",
                ),
                InlineKeyboardButton(
                    text="📋 COMMUNITIES",
                    callback_data="communities",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="🔗 INVITE LINKS",
                    callback_data="links",
                ),
                InlineKeyboardButton(
                    text="📢 PROMOTION",
                    callback_data="promotion",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="🔄 ROTATION",
                    callback_data="rotation",
                ),
                InlineKeyboardButton(
                    text="⏰ SCHEDULER",
                    callback_data="scheduler",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="📊 STATISTICS",
                    callback_data="stats",
                ),
                InlineKeyboardButton(
                    text="👥 JOIN REQUESTS",
                    callback_data="join_stats",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="⚙️ SETTINGS",
                    callback_data="settings",
                ),
                InlineKeyboardButton(
                    text="💾 BACKUP",
                    callback_data="backup",
                ),
            ],
        ]
    )


def back_keyboard(target="panel"):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 BACK",
                    callback_data=target,
                )
            ]
        ]
    )


# =========================================================
# PUBLIC NETWORK
# =========================================================

def public_network_keyboard(
    db: dict,
    page: int = 0
):

    communities = [
        (cid, c)
        for cid, c in db["communities"].items()
        if c.get("active", True)
        and c.get("invite_link")
    ]

    per_page = 5

    start = page * per_page
    end = start + per_page

    selected = communities[start:end]

    rows = []

    for cid, community in selected:

        icon = (
            "📢"
            if community["type"] == "channel"
            else "👥"
        )

        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {community['title'][:30]}",
                    url=community["invite_link"],
                )
            ]
        )

    navigation = []

    if page > 0:

        navigation.append(
            InlineKeyboardButton(
                text="◀️",
                callback_data=f"net:{page - 1}",
            )
        )

    if end < len(communities):

        navigation.append(
            InlineKeyboardButton(
                text="▶️",
                callback_data=f"net:{page + 1}",
            )
        )

    if navigation:
        rows.append(navigation)

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


# =========================================================
# START
# =========================================================

@router.message(CommandStart())
async def start(message: Message):

    db = await get_db()

    if is_owner(message.from_user.id):

        await message.answer(
            "━━━━━━━━━━━━━━━━━━\n"
            "      👑 <b>ADMIN PANEL</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Everything can be managed with buttons.",
            reply_markup=admin_panel(),
            parse_mode="HTML",
        )

        return

    await message.answer(
        db["settings"]["welcome_text"],
        reply_markup=public_network_keyboard(db),
        parse_mode="HTML",
    )


# =========================================================
# PUBLIC PAGINATION
# =========================================================

@router.callback_query(F.data.startswith("net:"))
async def network_page(
    callback: CallbackQuery
):

    page = int(callback.data.split(":")[1])

    db = await get_db()

    try:

        await callback.message.edit_reply_markup(
            reply_markup=public_network_keyboard(
                db,
                page
            )
        )

    except TelegramBadRequest:
        pass

    await callback.answer()


# =========================================================
# PANEL
# =========================================================

@router.callback_query(F.data == "panel")
async def panel(callback: CallbackQuery):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    await callback.message.edit_text(
        "━━━━━━━━━━━━━━━━━━\n"
        "      👑 <b>ADMIN PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━",
        reply_markup=admin_panel(),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# ADD COMMUNITY INFORMATION
# =========================================================

@router.callback_query(F.data == "add_info")
async def add_info(callback: CallbackQuery):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    text = (
        "➕ <b>ADD COMMUNITY</b>\n\n"

        "Manual add command ki zarurat nahi hai.\n\n"

        "1️⃣ Bot ko target group/channel me add karo.\n"
        "2️⃣ Bot ko administrator banao.\n"
        "3️⃣ Bot automatically community register karega.\n\n"

        "Invite links create karne ke liye bot ko "
        "invite-link/manage permission dena recommended hai."
    )

    await callback.message.edit_text(
        text,
        reply_markup=back_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# AUTO REGISTRATION
# =========================================================

@router.my_chat_member()
async def community_added(
    event: ChatMemberUpdated
):

    chat = event.chat

    new_status = event.new_chat_member.status

    if new_status != ChatMemberStatus.ADMINISTRATOR:
        return

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
        ChatType.CHANNEL,
    ):
        return

    invite_link = ""

    try:

        invite = await event.bot.create_chat_invite_link(
            chat_id=chat.id,
            name="Network",
        )

        invite_link = invite.invite_link

    except Exception as error:

        log.warning(
            "Invite creation failed for %s: %s",
            chat.id,
            error,
        )

        if getattr(chat, "username", None):

            invite_link = (
                f"https://t.me/{chat.username}"
            )

    community = {

        "id": chat.id,

        "title": chat.title
        or str(chat.id),

        "username": getattr(
            chat,
            "username",
            None,
        ),

        "type": (
            "channel"
            if chat.type == ChatType.CHANNEL
            else "group"
        ),

        "invite_link": invite_link,

        "active": True,

        "registered_at": int(time.time()),

        "last_promotion": 0,
    }

    async def mutate(db):

        db["communities"][
            str(chat.id)
        ] = community

    await update_db(mutate)

    log.info(
        "Registered: %s",
        community["title"],
    )


# =========================================================
# COMMUNITY LIST
# =========================================================

@router.callback_query(F.data == "communities")
async def communities(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    db = await get_db()

    active = [
        c
        for c in db["communities"].values()
        if c.get("active", True)
    ]

    rows = []

    for cid, c in db["communities"].items():

        if not c.get("active", True):
            continue

        icon = (
            "📢"
            if c["type"] == "channel"
            else "👥"
        )

        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {c['title'][:30]}",
                    callback_data=f"community:{cid}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 REFRESH",
                callback_data="communities",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 BACK",
                callback_data="panel",
            )
        ]
    )

    await callback.message.edit_text(
        "📋 <b>COMMUNITIES</b>\n\n"
        f"Active: <b>{len(active)}</b>\n\n"
        "Select a community:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# COMMUNITY DETAILS
# =========================================================

@router.callback_query(
    F.data.startswith("community:")
)
async def community_detail(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    cid = callback.data.split(":", 1)[1]

    db = await get_db()

    community = db["communities"].get(cid)

    if not community:
        return await callback.answer(
            "Community not found.",
            show_alert=True
        )

    status = (
        "🟢 ACTIVE"
        if community.get("active", True)
        else "🔴 DISABLED"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="🔗 GET LINK",
                    callback_data=f"getlink:{cid}",
                ),

                InlineKeyboardButton(
                    text="📢 POST NOW",
                    callback_data=f"post:{cid}",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="⏯️ TOGGLE",
                    callback_data=f"toggle:{cid}",
                ),

                InlineKeyboardButton(
                    text="🗑️ REMOVE",
                    callback_data=f"remove:{cid}",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="🔙 BACK",
                    callback_data="communities",
                )
            ],
        ]
    )

    await callback.message.edit_text(
        f"📌 <b>{community['title']}</b>\n\n"
        f"Type: {community['type']}\n"
        f"Status: {status}\n"
        f"Username: "
        f"@{community.get('username') or 'private'}\n"
        f"Invite: "
        f"{'✅' if community.get('invite_link') else '❌'}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# GET INVITE LINK
# =========================================================

@router.callback_query(
    F.data.startswith("getlink:")
)
async def get_link(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    cid = callback.data.split(":", 1)[1]

    db = await get_db()

    community = db["communities"].get(cid)

    if not community:
        return await callback.answer(
            "Not found.",
            show_alert=True
        )

    link = community.get("invite_link")

    if not link:

        try:

            invite = await callback.bot.create_chat_invite_link(
                community["id"],
                name="Network",
            )

            link = invite.invite_link

            async def mutate(db):
                db["communities"][cid][
                    "invite_link"
                ] = link

            await update_db(mutate)

        except Exception:

            return await callback.answer(
                "Cannot create invite link. "
                "Check admin permissions.",
                show_alert=True,
            )

    await callback.message.answer(
        f"🔗 <b>{community['title']}</b>\n\n"
        f"{link}",
        parse_mode="HTML",
    )

    await callback.answer("Link sent.")


# =========================================================
# TOGGLE COMMUNITY
# =========================================================

@router.callback_query(
    F.data.startswith("toggle:")
)
async def toggle_community(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    cid = callback.data.split(":", 1)[1]

    async def mutate(db):

        if cid in db["communities"]:

            current = db["communities"][cid].get(
                "active",
                True,
            )

            db["communities"][cid][
                "active"
            ] = not current

    await update_db(mutate)

    await callback.answer("Updated.")

    await communities(callback)


# =========================================================
# REMOVE
# =========================================================

@router.callback_query(
    F.data.startswith("remove:")
)
async def remove_community(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    cid = callback.data.split(":", 1)[1]

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="✅ YES, REMOVE",
                    callback_data=f"confirm_remove:{cid}",
                )
            ],

            [
                InlineKeyboardButton(
                    text="❌ CANCEL",
                    callback_data=f"community:{cid}",
                )
            ],
        ]
    )

    await callback.message.edit_text(
        "⚠️ <b>REMOVE COMMUNITY?</b>\n\n"
        "It will be removed from the network database.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith("confirm_remove:")
)
async def confirm_remove(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    cid = callback.data.split(":", 1)[1]

    async def mutate(db):

        db["communities"].pop(
            cid,
            None,
        )

    await update_db(mutate)

    await callback.answer(
        "Community removed."
    )

    await communities(callback)


# =========================================================
# LINKS
# =========================================================

@router.callback_query(F.data == "links")
async def links(callback: CallbackQuery):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    db = await get_db()

    total = len(
        [
            c
            for c in db["communities"].values()
            if c.get("active", True)
        ]
    )

    await callback.message.edit_text(
        "🔗 <b>INVITE LINKS</b>\n\n"
        f"Registered communities: <b>{total}</b>\n\n"
        "Every active community can be opened "
        "through the public network buttons.",
        reply_markup=public_network_keyboard(db),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# PROMOTION PANEL
# =========================================================

@router.callback_query(F.data == "promotion")
async def promotion(callback: CallbackQuery):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    db = await get_db()

    settings = db["settings"]

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="▶️ POST NOW",
                    callback_data="post_all",
                )
            ],

            [
                InlineKeyboardButton(
                    text=(
                        "⏸️ DISABLE"
                        if settings["promotion_enabled"]
                        else "▶️ ENABLE"
                    ),
                    callback_data="toggle_promotion",
                )
            ],

            [
                InlineKeyboardButton(
                    text="🔄 ROTATION",
                    callback_data="rotation",
                )
            ],

            [
                InlineKeyboardButton(
                    text="🔙 BACK",
                    callback_data="panel",
                )
            ],
        ]
    )

    await callback.message.edit_text(
        "📢 <b>PROMOTION</b>\n\n"
        f"Status: "
        f"{'🟢 ON' if settings['promotion_enabled'] else '🔴 OFF'}\n"
        f"Interval: "
        f"{settings['promotion_interval'] // 60} min\n"
        f"Links/post: "
        f"{settings['links_per_post']}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# TOGGLE PROMOTION
# =========================================================

@router.callback_query(
    F.data == "toggle_promotion"
)
async def toggle_promotion(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    async def mutate(db):

        db["settings"]["promotion_enabled"] = (
            not db["settings"]["promotion_enabled"]
        )

    await update_db(mutate)

    await callback.answer("Updated.")

    await promotion(callback)


# =========================================================
# ROTATION
# =========================================================

@router.callback_query(F.data == "rotation")
async def rotation(callback: CallbackQuery):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    db = await get_db()

    active = len(
        [
            c
            for c in db["communities"].values()
            if c.get("active", True)
        ]
    )

    await callback.message.edit_text(
        "🔄 <b>SMART ROTATION</b>\n\n"
        f"Active communities: <b>{active}</b>\n"
        f"Links/post: "
        f"<b>{db['settings']['links_per_post']}</b>\n\n"
        "Each promotion randomly selects other active "
        "communities so the complete network can rotate.",
        reply_markup=back_keyboard("promotion"),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# SCHEDULER
# =========================================================

@router.callback_query(F.data == "scheduler")
async def scheduler(callback: CallbackQuery):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    db = await get_db()

    interval = (
        db["settings"]["promotion_interval"]
        // 60
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="30 MIN",
                    callback_data="interval:1800",
                ),

                InlineKeyboardButton(
                    text="1 HOUR",
                    callback_data="interval:3600",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="3 HOURS",
                    callback_data="interval:10800",
                )
            ],

            [
                InlineKeyboardButton(
                    text="⚙️ SETTINGS",
                    callback_data="settings",
                )
            ],

            [
                InlineKeyboardButton(
                    text="🔙 BACK",
                    callback_data="panel",
                )
            ],
        ]
    )

    await callback.message.edit_text(
        "⏰ <b>SCHEDULER</b>\n\n"
        f"Current interval: <b>{interval} minutes</b>\n\n"
        "Automatic promotion runs according to this interval.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# SETTINGS
# =========================================================

@router.callback_query(F.data == "settings")
async def settings(callback: CallbackQuery):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    db = await get_db()

    s = db["settings"]

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text=(
                        f"📢 Promotion "
                        f"{'🟢' if s['promotion_enabled'] else '🔴'}"
                    ),
                    callback_data="toggle_promotion",
                )
            ],

            [
                InlineKeyboardButton(
                    text=(
                        f"🔄 Rotation "
                        f"{'🟢' if s['rotation_enabled'] else '🔴'}"
                    ),
                    callback_data="toggle_rotation",
                )
            ],

            [
                InlineKeyboardButton(
                    text="30 MIN",
                    callback_data="interval:1800",
                ),
                InlineKeyboardButton(
                    text="1 HOUR",
                    callback_data="interval:3600",
                ),
                InlineKeyboardButton(
                    text="3 HOURS",
                    callback_data="interval:10800",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="5 LINKS",
                    callback_data="count:5",
                ),
                InlineKeyboardButton(
                    text="10 LINKS",
                    callback_data="count:10",
                ),
                InlineKeyboardButton(
                    text="15 LINKS",
                    callback_data="count:15",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="🔙 BACK",
                    callback_data="panel",
                )
            ],
        ]
    )

    await callback.message.edit_text(
        "⚙️ <b>SETTINGS</b>",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(
    F.data == "toggle_rotation"
)
async def toggle_rotation(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    async def mutate(db):

        db["settings"]["rotation_enabled"] = (
            not db["settings"]["rotation_enabled"]
        )

    await update_db(mutate)

    await settings(callback)

    await callback.answer("Updated.")


# =========================================================
# INTERVAL
# =========================================================

@router.callback_query(
    F.data.startswith("interval:")
)
async def interval(callback: CallbackQuery):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    value = int(
        callback.data.split(":")[1]
    )

    async def mutate(db):

        db["settings"]["promotion_interval"] = value

    await update_db(mutate)

    await settings(callback)

    await callback.answer(
        "Interval updated."
    )


# =========================================================
# LINKS PER POST
# =========================================================

@router.callback_query(
    F.data.startswith("count:")
)
async def count_links(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    value = int(
        callback.data.split(":")[1]
    )

    async def mutate(db):

        db["settings"]["links_per_post"] = value

    await update_db(mutate)

    await settings(callback)

    await callback.answer(
        "Link count updated."
    )


# =========================================================
# PROMOTION MESSAGE
# =========================================================

async def build_promotion(
    bot: Bot,
    target_id: int,
    limit: int,
):

    db = await get_db()

    communities = [
        c
        for c in db["communities"].values()
        if c.get("active", True)
        and c["id"] != target_id
    ]

    if not communities:
        return None, None

    random.shuffle(communities)

    selected = communities[:limit]

    buttons = []

    for community in selected:

        link = community.get(
            "invite_link"
        )

        if not link:

            try:

                invite = (
                    await bot.create_chat_invite_link(
                        community["id"],
                        name="Network",
                    )
                )

                link = invite.invite_link

                cid = str(
                    community["id"]
                )

                async def mutate(db):

                    if cid in db["communities"]:
                        db["communities"][cid][
                            "invite_link"
                        ] = link

                await update_db(mutate)

            except Exception:
                continue

        icon = (
            "📢"
            if community["type"] == "channel"
            else "👥"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"{icon} "
                        f"{community['title'][:28]}"
                    ),
                    url=link,
                )
            ]
        )

    if not buttons:
        return None, None

    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ MORE",
                callback_data="more_public",
            )
        ]
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=buttons
    )

    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        "      🔞 <b>ADULT NETWORK</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "🔥 <b>JOIN OUR COMMUNITIES</b>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🔞 18+ ONLY • PRIVATE NETWORK\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    return text, keyboard


# =========================================================
# POST ONE
# =========================================================

async def promote_one(
    bot: Bot,
    cid: str,
):

    db = await get_db()

    target = db["communities"].get(cid)

    if not target:
        return False

    text, keyboard = await build_promotion(
        bot,
        target["id"],
        db["settings"]["links_per_post"],
    )

    if not text:
        return False

    try:

        await bot.send_message(
            target["id"],
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

        async def mutate(db):

            db["stats"]["promotions"] += 1

            if cid in db["communities"]:

                db["communities"][cid][
                    "last_promotion"
                ] = int(time.time())

        await update_db(mutate)

        return True

    except TelegramRetryAfter as error:

        await asyncio.sleep(
            error.retry_after
        )

    except (
        TelegramForbiddenError,
        TelegramBadRequest,
    ) as error:

        log.warning(
            "Promotion failed in %s: %s",
            target["title"],
            error,
        )

    except Exception as error:

        log.exception(
            "Promotion error: %s",
            error,
        )

    async def fail(db):

        db["stats"][
            "promotion_failures"
        ] += 1

    await update_db(fail)

    return False


# =========================================================
# POST ALL
# =========================================================

async def promote_all(bot: Bot):

    db = await get_db()

    ids = [
        cid
        for cid, community
        in db["communities"].items()
        if community.get("active", True)
    ]

    for cid in ids:

        await promote_one(
            bot,
            cid,
        )

        await asyncio.sleep(1.5)


@router.callback_query(
    F.data == "post_all"
)
async def post_all(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    await callback.answer(
        "Promotion started."
    )

    asyncio.create_task(
        promote_all(callback.bot)
    )

    await callback.message.answer(
        "📢 Network promotion started."
    )


# =========================================================
# POST SINGLE
# =========================================================

@router.callback_query(
    F.data.startswith("post:")
)
async def post_single(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    cid = callback.data.split(
        ":",
        1
    )[1]

    await callback.answer(
        "Posting..."
    )

    success = await promote_one(
        callback.bot,
        cid,
    )

    if success:

        await callback.message.answer(
            "✅ Promotion posted."
        )

    else:

        await callback.message.answer(
            "❌ Promotion failed."
        )


# =========================================================
# MORE BUTTON
# =========================================================

@router.callback_query(
    F.data == "more_public"
)
async def more_public(
    callback: CallbackQuery
):

    db = await get_db()

    await callback.message.edit_text(
        "🔞 <b>MORE COMMUNITIES</b>",
        reply_markup=public_network_keyboard(
            db,
            1,
        ),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# STATISTICS
# =========================================================

@router.callback_query(F.data == "stats")
async def statistics(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    db = await get_db()

    communities = list(
        db["communities"].values()
    )

    active = [
        c
        for c in communities
        if c.get("active", True)
    ]

    groups = [
        c
        for c in active
        if c["type"] == "group"
    ]

    channels = [
        c
        for c in active
        if c["type"] == "channel"
    ]

    stats = db["stats"]

    text = (
        "━━━━━━━━━━━━━━━━━━\n"
        "       📊 <b>STATISTICS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"Communities : <b>{len(communities)}</b>\n"
        f"Active      : <b>{len(active)}</b>\n"
        f"Groups      : <b>{len(groups)}</b>\n"
        f"Channels    : <b>{len(channels)}</b>\n\n"

        f"Promotions  : <b>{stats['promotions']}</b>\n"
        f"Failures    : <b>{stats['promotion_failures']}</b>\n"
        f"Join reqs   : <b>{stats['join_requests']}</b>\n"
        f"Approved    : <b>{stats['approved_requests']}</b>"
    )

    await callback.message.edit_text(
        text,
        reply_markup=back_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# JOIN REQUEST
# =========================================================

@router.chat_join_request()
async def join_request(event):

    async def mutate(db):

        db["stats"]["join_requests"] += 1

    await update_db(mutate)

    try:

        await event.bot.approve_chat_join_request(
            chat_id=event.chat.id,
            user_id=event.from_user.id,
        )

        async def mutate2(db):

            db["stats"][
                "approved_requests"
            ] += 1

        await update_db(mutate2)

    except Exception as error:

        log.warning(
            "Join approval failed: %s",
            error,
        )

        return

    db = await get_db()

    if not db["settings"].get(
        "welcome_enabled",
        True,
    ):
        return

    try:

        await event.bot.send_message(
            event.from_user.id,
            db["settings"]["welcome_text"],
            reply_markup=public_network_keyboard(
                db
            ),
            parse_mode="HTML",
        )

    except Exception:

        # User may have blocked the bot
        pass


# =========================================================
# JOIN REQUEST STATS
# =========================================================

@router.callback_query(
    F.data == "join_stats"
)
async def join_stats(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    db = await get_db()

    stats = db["stats"]

    await callback.message.edit_text(
        "👥 <b>JOIN REQUESTS</b>\n\n"
        f"Requests: <b>{stats['join_requests']}</b>\n"
        f"Approved: <b>{stats['approved_requests']}</b>\n\n"
        "Auto approval works where the bot has "
        "the required administrator permission.",
        reply_markup=back_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


# =========================================================
# BACKUP
# =========================================================

@router.callback_query(F.data == "backup")
async def backup(
    callback: CallbackQuery
):

    if not is_owner(callback.from_user.id):
        return await callback.answer(
            "⛔ Not authorized.",
            show_alert=True
        )

    await callback.message.answer_document(
        document=__import__(
            "aiogram"
        ).types.FSInputFile(
            str(DATA_FILE)
        ),
        caption="💾 Network database backup",
    )

    await callback.answer(
        "Backup sent."
    )


# =========================================================
# HEALTH SERVER
# =========================================================

async def health(
    request: web.Request
):

    return web.json_response(
        {
            "ok": True,
            "service": "telegram-network-bot",
        }
    )


async def start_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_get(
        "/health",
        health,
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    log.info(
        "Health server started on %s",
        PORT,
    )

    return runner


# =========================================================
# SCHEDULER
# =========================================================

async def scheduler_loop(
    bot: Bot
):

    while True:

        try:

            db = await get_db()

            enabled = db["settings"].get(
                "promotion_enabled",
                True,
            )

            interval = int(
                db["settings"].get(
                    "promotion_interval",
                    3600,
                )
            )

            if enabled:

                await promote_all(bot)

            await asyncio.sleep(
                max(
                    60,
                    interval,
                )
            )

        except asyncio.CancelledError:

            raise

        except Exception:

            log.exception(
                "Scheduler error"
            )

            await asyncio.sleep(60)


# =========================================================
# MAIN
# =========================================================

async def main():

    bot = Bot(BOT_TOKEN)

    dp = Dispatcher()

    dp.include_router(router)

    server = await start_server()

    scheduler = asyncio.create_task(
        scheduler_loop(bot)
    )

    try:

        me = await bot.get_me()

        log.info(
            "Bot started: @%s",
            me.username,
        )

        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "callback_query",
                "my_chat_member",
                "chat_join_request",
            ],
        )

    finally:

        scheduler.cancel()

        await server.cleanup()

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
