"""
Бот для групи @benidorm_rent_sale.

Як працює:
1. Олена надсилає боту в особисті оголошення: текст + фото (альбомом або окремо,
   можна переслати з іншого чату). Порядок не важливий: спершу фото, потім текст,
   або навпаки.
2. Бот збирає все в одну чернетку і питає «Опублікувати?».
3. Після «✅ Опублікувати» бот постить оголошення в групу, надсилає посилання
   і додає об'єкт у список (закріплене повідомлення-зміст), якщо він увімкнений.

Команди (лише для адміністраторів):
/start, /help — довідка
/cancel       — скасувати поточну чернетку
/list         — показати список об'єктів
/remove 2842  — прибрати об'єкт зі списку (номер поста або посилання)
/index        — створити (або створити заново) закріплений список у групі

Змінні середовища:
BOT_TOKEN   — токен від @BotFather
CHANNEL_ID  — група, напр. @benidorm_rent_sale
ADMIN_IDS   — Telegram ID адмінів через кому (якщо не задано — Олена, 289414548)
PORT        — порт (Render задає сам)
RENDER_EXTERNAL_URL — адреса сервісу (Render задає сам). Якщо є — працюємо
              через webhook, і Render "будить" бота, коли приходить повідомлення.
              Якщо нема — звичайний polling (для запуску на комп'ютері).
"""

import os
import re
import json
import html
import hashlib
import logging
import asyncio

from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("benidorm-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@benidorm_rent_sale")
ADMIN_IDS = {int(x) for x in re.findall(r"-?\d+", os.environ.get("ADMIN_IDS") or "289414548")}
PORT = int(os.environ.get("PORT", 8080))
EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_items.json")
STATE_FILENAME = "state.json"

DRAFT_WAIT_SECONDS = 3      # скільки чекати "тиші", поки докидаються фото/текст
CAPTION_LIMIT = 1024        # ліміт підпису до фото в Telegram
MESSAGE_LIMIT = 4096        # ліміт звичайного повідомлення


def post_url(message_id: int) -> str:
    return f"https://t.me/{CHANNEL_ID.lstrip('@')}/{message_id}"


def is_admin(user_id) -> bool:
    return user_id in ADMIN_IDS


# ── Розбір оголошення ─────────────────────────────────────────────────────────

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿️‍⭐⬆↔-↪]+"
)
PRICE_RE = re.compile(r"(\d{1,3}(?:[  .,]\d{3})+|\d+)\s*(?:€|євро|eur)", re.IGNORECASE)
BEDS_RE = re.compile(r"(\d+)\s*(?:-?\s*х)?\s*(?:спал|-?\s*кімн|кімн)", re.IGNORECASE)

WORD_NUMS = {"одна": 1, "одну": 1, "дві": 2, "два": 2, "три": 3, "чотири": 4, "п'ять": 5, "п’ять": 5}
WORD_BEDS_RE = re.compile(r"(одна|одну|дві|два|три|чотири|п'ять|п’ять)\s+спал")

TYPES = [
    ("вілл", "вілла"),
    ("таунхаус", "таунхаус"),
    ("дуплекс", "дуплекс"),
    ("пентхаус", "пентхаус"),
    ("бунгало", "бунгало"),
    ("будин", "будинок"),
    ("дворівнев", "дворівнева"),
]


def clean_line(s: str) -> str:
    s = EMOJI_RE.sub("", s)
    s = re.sub(r"^\s*(локація|location|адреса)\s*:\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\(?\s*іспанія\s*\)?", "", s, flags=re.IGNORECASE)
    s = s.replace(" | ", ", ").replace("|", ",")
    s = re.sub(r"\s+", " ", s).strip(" ,.-–")
    return s


def parse_listing(text: str):
    """Повертає dict з ціною, кімнатами, районом — або None, якщо ціни не знайшли."""
    if not text:
        return None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None
    head = " ".join(lines[:3]).lower()
    kind = "sale" if "продаж" in head or "продає" in head else "rent"

    # Ціна: беремо перший рядок, де є "ціна", інакше перший рядок з €
    price_line = next((l for l in lines if "ціна" in l.lower() and PRICE_RE.search(l)), None)
    if price_line is None:
        price_line = next((l for l in lines if PRICE_RE.search(l)), None)
    if price_line is None:
        return None
    price = int(re.sub(r"\D", "", PRICE_RE.search(price_line).group(1)))
    if price <= 0:
        return None
    if price >= 50000:  # такі суми — точно продаж, навіть якщо слова "продаж" нема
        kind = "sale"

    # Кімнати
    low = text.lower()
    m = BEDS_RE.search(low)
    beds = None
    if m:
        beds = int(m.group(1))
    elif "студі" in low:
        beds = 0
    else:
        w = WORD_BEDS_RE.search(low)
        if w:
            beds = WORD_NUMS[w.group(1)]
    if beds == 0:
        beds_txt = "студія"
    elif beds:
        beds_txt = f"{beds} " + ("спальня" if beds == 1 else "спальні" if beds < 5 else "спалень")
    else:
        beds_txt = ""
    title_low = " ".join(lines[:2]).lower()
    typ = next((lab for key, lab in TYPES if key in title_low), "")
    rooms = ", ".join(x for x in (typ, beds_txt) if x) or "—"

    # Район: рядок з 📍, інакше рядок з "Іспанія"/"Локація", інакше другий рядок
    loc_line = next((l for l in lines if "📍" in l), None)
    if loc_line is None:
        loc_line = next(
            (l for l in lines if re.search(r"іспанія|локація|costa blanca", l, re.IGNORECASE)),
            None,
        )
    if loc_line is None and len(lines) > 1:
        loc_line = lines[1]
    location = clean_line(loc_line or "")[:70] or "—"
    if PRICE_RE.search(location):
        location = "—"

    return {"kind": kind, "price": price, "rooms": rooms, "location": location}


def fmt_price(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def item_label(item: dict) -> str:
    suffix = "/міс" if item.get("kind") == "rent" else ""
    return f"{fmt_price(item['price'])} €{suffix} | {item['rooms']} | {item['location']}"


def build_index_text(items: list) -> str:
    parts = ["🏠 <b>Актуальні об'єкти</b>"]
    for kind, title in (("rent", "🔑 <b>Оренда</b>"), ("sale", "🏡 <b>Продаж</b>")):
        group = sorted((i for i in items if i.get("kind", "rent") == kind), key=lambda i: i["price"])
        if not group:
            continue
        parts.append("")
        parts.append(title)
        for i in group:
            parts.append(f'• <a href="{i["url"]}">{html.escape(item_label(i))}</a>')
    parts.append("")
    parts.append(f"<i>Всього: {len(items)}. Натисніть на рядок, щоб відкрити оголошення.</i>")
    text = "\n".join(parts)
    # Захист від ліміту Telegram: якщо задовго — відрізаємо найдорожчі
    while len(re.sub(r"<[^>]+>", "", text)) > MESSAGE_LIMIT - 100 and len(items) > 1:
        items = sorted(items, key=lambda i: i["price"])[:-1]
        text = build_index_text(items)
    return text


# ── Стан (список об'єктів) ────────────────────────────────────────────────────
# Диск на Render безкоштовному тарифі стирається при кожному перезапуску,
# тому стан зберігаємо в самому Telegram: файлом state.json, закріпленим
# в особистому чаті бота з першим адміністратором.

def storage_chat_id():
    return min(ADMIN_IDS) if ADMIN_IDS else None


def load_seed() -> dict:
    try:
        with open(SEED_FILE, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        items = []
    return {"index_id": None, "items": items}


async def load_state(app: Application) -> dict:
    chat_id = storage_chat_id()
    if chat_id:
        try:
            chat = await app.bot.get_chat(chat_id)
            pm = chat.pinned_message
            if pm and pm.document and pm.document.file_name == STATE_FILENAME:
                f = await pm.document.get_file()
                data = await f.download_as_bytearray()
                state = json.loads(bytes(data).decode("utf-8"))
                app.bot_data["state_msg_id"] = pm.message_id
                logger.info("Стан завантажено з Telegram: %d об'єктів", len(state.get("items", [])))
                return state
        except Exception as e:
            logger.warning("Не вдалося завантажити стан з Telegram: %s", e)
    state = load_seed()
    logger.info("Стан узято зі стартового файлу: %d об'єктів", len(state["items"]))
    return state


async def save_state(context: ContextTypes.DEFAULT_TYPE):
    state = context.bot_data["state"]
    chat_id = storage_chat_id()
    if not chat_id:
        return
    payload = json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8")
    try:
        msg = await context.bot.send_document(
            chat_id=chat_id,
            document=payload,
            filename=STATE_FILENAME,
            caption="🗂 Службовий файл бота зі списком об'єктів. Не видаляйте і не відкріплюйте.",
            disable_notification=True,
        )
        await context.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        old = context.bot_data.get("state_msg_id")
        context.bot_data["state_msg_id"] = msg.message_id
        if old:
            try:
                await context.bot.delete_message(chat_id, old)
            except Exception:
                pass
    except Exception as e:
        logger.warning("Не вдалося зберегти стан: %s", e)


async def refresh_index(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Оновлює закріплений список у групі. False — якщо його нема або не вийшло."""
    state = context.bot_data["state"]
    if not state.get("index_id"):
        return False
    try:
        await context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=state["index_id"],
            text=build_index_text(state["items"]),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        if "not modified" in str(e).lower():
            return True
        logger.warning("Не вдалося оновити список: %s", e)
        return False


async def add_item(context, parsed: dict, message_id: int) -> dict:
    state = context.bot_data["state"]
    url = post_url(message_id)
    state["items"] = [i for i in state["items"] if i["url"] != url]
    item = dict(parsed, url=url)
    state["items"].append(item)
    await save_state(context)
    await refresh_index(context)
    return item


# ── Чернетка оголошення ───────────────────────────────────────────────────────

def get_draft(context, user_id) -> dict:
    drafts = context.bot_data.setdefault("drafts", {})
    return drafts.setdefault(user_id, {"media": [], "html": "", "plain": "", "prompt_id": None})


def drop_draft(context, user_id):
    context.bot_data.setdefault("drafts", {}).pop(user_id, None)


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not is_admin(user.id):
        await msg.reply_text(
            "Вибачте, цей бот лише для адміністраторів групи.\n"
            f"Ваш Telegram ID: {user.id}"
        )
        return

    draft = get_draft(context, user.id)
    if msg.photo:
        draft["media"].append(("photo", msg.photo[-1].file_id))
    elif msg.video:
        draft["media"].append(("video", msg.video.file_id))

    text_html = msg.caption_html if (msg.photo or msg.video) else msg.text_html
    text_plain = msg.caption if (msg.photo or msg.video) else msg.text
    if text_plain and text_plain.strip():
        if draft["plain"]:
            draft["html"] += "\n\n" + text_html
            draft["plain"] += "\n\n" + text_plain
        else:
            draft["html"], draft["plain"] = text_html, text_plain

    # Чекаємо, поки долетять усі фото з альбому, і тоді показуємо підсумок
    for job in context.job_queue.get_jobs_by_name(f"draft_{user.id}"):
        job.schedule_removal()
    context.job_queue.run_once(
        show_draft_prompt, DRAFT_WAIT_SECONDS, chat_id=msg.chat_id, user_id=user.id,
        name=f"draft_{user.id}",
    )


async def show_draft_prompt(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    draft = get_draft(context, job.user_id)
    if draft.get("prompt_id"):
        try:
            await context.bot.delete_message(job.chat_id, draft["prompt_id"])
        except Exception:
            pass

    n_photo = sum(1 for t, _ in draft["media"] if t == "photo")
    n_video = sum(1 for t, _ in draft["media"] if t == "video")
    parsed = parse_listing(draft["plain"])
    lines = ["📝 <b>Чернетка оголошення</b>"]
    media_bits = []
    if n_photo:
        media_bits.append(f"{n_photo} фото")
    if n_video:
        media_bits.append(f"{n_video} відео")
    lines.append("Медіа: " + (", ".join(media_bits) if media_bits else "немає"))
    if not draft["plain"]:
        lines.append("Текст: ще немає — можна докинути наступним повідомленням")
    elif parsed:
        lines.append("У список піде: <b>" + html.escape(item_label(parsed)) + "</b>")
    else:
        lines.append("⚠️ Не знайшла ціну в тексті — опублікую, але в список не додам")
    if len(draft["media"]) > 1 and len(draft["plain"]) > CAPTION_LIMIT:
        lines.append("ℹ️ Текст довгий для підпису до фото, тож опублікую його окремим повідомленням під фото")
    lines.append("")
    lines.append("Можна ще докинути фото або текст — я оновлю чернетку.")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Опублікувати", callback_data="publish"),
        InlineKeyboardButton("❌ Скасувати", callback_data="cancel"),
    ]])
    m = await context.bot.send_message(
        job.chat_id, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb
    )
    draft["prompt_id"] = m.message_id


async def publish_draft(context, draft: dict) -> list:
    """Публікує чернетку в групу. Повертає список надісланих повідомлень."""
    media, text_html, plain = draft["media"], draft["html"], draft["plain"]
    fits_caption = bool(plain) and len(plain) <= CAPTION_LIMIT
    sent = []

    if not media:
        if not plain:
            raise ValueError("Чернетка порожня")
        sent.append(await context.bot.send_message(
            CHANNEL_ID, text_html, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        ))
        return sent

    def make(kind, file_id, caption=None):
        cls = InputMediaPhoto if kind == "photo" else InputMediaVideo
        if caption:
            return cls(file_id, caption=caption, parse_mode=ParseMode.HTML)
        return cls(file_id)

    if len(media) == 1:
        kind, fid = media[0]
        send = context.bot.send_photo if kind == "photo" else context.bot.send_video
        sent.append(await send(
            CHANNEL_ID, fid,
            caption=text_html if fits_caption else None,
            parse_mode=ParseMode.HTML if fits_caption else None,
        ))
    else:
        for start in range(0, len(media), 10):  # Telegram: максимум 10 у альбомі
            chunk = media[start:start + 10]
            group = [
                make(k, f, text_html if (start == 0 and i == 0 and fits_caption) else None)
                for i, (k, f) in enumerate(chunk)
            ]
            sent.extend(await context.bot.send_media_group(CHANNEL_ID, group))

    if plain and not fits_caption:
        sent.append(await context.bot.send_message(
            CHANNEL_ID, text_html, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        ))
    return sent


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not is_admin(user.id):
        await query.answer("Немає доступу", show_alert=True)
        return
    drafts = context.bot_data.setdefault("drafts", {})
    draft = drafts.get(user.id)

    if query.data == "cancel":
        drop_draft(context, user.id)
        await query.answer("Скасовано")
        await query.edit_message_text("❌ Чернетку скасовано.")
        return

    if query.data == "publish":
        if not draft or (not draft["media"] and not draft["plain"]):
            await query.answer("Чернетка порожня", show_alert=True)
            return
        await query.answer("Публікую…")
        drop_draft(context, user.id)  # щоб подвійне натискання не опублікувало двічі
        try:
            sent = await publish_draft(context, draft)
        except Exception as e:
            logger.exception("Помилка публікації")
            drafts[user.id] = draft
            await query.edit_message_text(
                f"⚠️ Не вдалося опублікувати: {html.escape(str(e))}\n\n"
                "Перевірте, що бот — адміністратор групи з правом публікувати повідомлення, "
                "і натисніть кнопку ще раз.",
                parse_mode=ParseMode.HTML,
                reply_markup=query.message.reply_markup,
            )
            return

        first_id = sent[0].message_id
        lines = [f'✅ Опубліковано: <a href="{post_url(first_id)}">{post_url(first_id)}</a>']
        parsed = parse_listing(draft["plain"])
        if parsed:
            item = await add_item(context, parsed, first_id)
            lines.append("Додано в список: " + html.escape(item_label(item)))
            if not context.bot_data["state"].get("index_id"):
                lines.append("ℹ️ Закріпленого списку в групі ще нема — створіть його командою /index")
        else:
            lines.append("⚠️ Ціну не знайшла, тож у список не додала.")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)


# ── Пости, опубліковані в групі вручну ───────────────────────────────────────

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Якщо адмін постить оголошення в групу сам — теж додаємо його в список."""
    msg = update.effective_message
    sender_ok = (
        (msg.from_user and is_admin(msg.from_user.id))
        or (msg.sender_chat and msg.sender_chat.id == msg.chat_id)  # анонімний адмін
    )
    if not sender_ok:
        return
    text = msg.caption or msg.text or ""
    if len(text) < 80:  # короткі репліки на кшталт "ціна знижена" пропускаємо
        return
    parsed = parse_listing(text)
    if not parsed:
        return
    await add_item(context, parsed, msg.message_id)
    logger.info("Додано з групи: %s", item_label(parsed))


# ── Команди ───────────────────────────────────────────────────────────────────

HELP = (
    "Привіт! Я публікую оголошення в групу {chan}.\n\n"
    "Як користуватись:\n"
    "1. Надішліть мені фото (можна альбомом) і текст оголошення — разом або окремо, "
    "можна переслати з іншого чату.\n"
    "2. Я покажу чернетку і спитаю «Опублікувати?».\n"
    "3. Натисніть ✅ — і оголошення з'явиться в групі.\n\n"
    "Команди:\n"
    "/list — список об'єктів\n"
    "/remove 2842 — прибрати об'єкт зі списку (номер поста або посилання)\n"
    "/index — створити закріплений список у групі\n"
    "/cancel — скасувати чернетку"
)


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_admin(user.id):
            if update.effective_chat.type == "private":
                await update.effective_message.reply_text(
                    "Цей бот лише для адміністраторів.\n"
                    f"Ваш Telegram ID: {user.id if user else '?'}\n"
                    "Додайте його в налаштування бота (ADMIN_IDS), щоб отримати доступ."
                )
            return
        return await func(update, context)
    return wrapper


@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP.format(chan=CHANNEL_ID))


@admin_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    drop_draft(context, update.effective_user.id)
    await update.effective_message.reply_text("Чернетку скасовано.")


@admin_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = context.bot_data["state"]["items"]
    if not items:
        await update.effective_message.reply_text("Список порожній.")
        return
    await update.effective_message.reply_text(
        build_index_text(items), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


@admin_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Напишіть номер поста, напр.: /remove 2842")
        return
    nums = re.findall(r"(\d+)\s*$", context.args[0])
    if not nums:
        await update.effective_message.reply_text("Не бачу номера поста. Приклад: /remove 2842")
        return
    url = post_url(int(nums[0]))
    state = context.bot_data["state"]
    before = len(state["items"])
    state["items"] = [i for i in state["items"] if i["url"] != url]
    if len(state["items"]) == before:
        await update.effective_message.reply_text(f"❌ Об'єкта №{nums[0]} у списку нема.")
        return
    await save_state(context)
    await refresh_index(context)
    await update.effective_message.reply_text(
        f"✅ Прибрала №{nums[0]}. У списку тепер {len(state['items'])}."
    )


@admin_only
async def cmd_index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.bot_data["state"]
    try:
        msg = await context.bot.send_message(
            CHANNEL_ID, build_index_text(state["items"]),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Не вдалося надіслати в групу: {e}")
        return
    pinned = True
    try:
        await context.bot.pin_chat_message(CHANNEL_ID, msg.message_id, disable_notification=True)
    except Exception as e:
        pinned = False
        logger.warning("Не вдалося закріпити: %s", e)
    old = state.get("index_id")
    state["index_id"] = msg.message_id
    await save_state(context)
    if old:
        try:
            await context.bot.unpin_chat_message(CHANNEL_ID, old)
            await context.bot.delete_message(CHANNEL_ID, old)
        except Exception:
            pass
    note = "і закріпила" if pinned else "але закріпити не вийшло — дайте боту право закріплювати повідомлення"
    await update.effective_message.reply_text(
        f"✅ Список надіслала в групу {note}: {post_url(msg.message_id)}\n"
        "Далі я оновлюватиму його сама після кожного нового оголошення."
    )


# ── Запуск ────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    app.bot_data["state"] = await load_state(app)
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не задано — бот відповідатиме всім лише їхнім Telegram ID")


def build_app() -> Application:
    builder = Application.builder().token(BOT_TOKEN).post_init(post_init)
    if EXTERNAL_URL:
        builder = builder.updater(None)
    app = builder.build()

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler(["start", "help"], cmd_start, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CommandHandler("list", cmd_list, filters=private))
    app.add_handler(CommandHandler("remove", cmd_remove, filters=private))
    app.add_handler(CommandHandler("index", cmd_index, filters=private))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        private & (filters.PHOTO | filters.VIDEO | (filters.TEXT & ~filters.COMMAND)),
        on_private_message,
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND & (filters.TEXT | filters.CAPTION),
        on_group_message,
    ))
    return app


async def run_webhook(app: Application):
    """Власний маленький веб-сервер: / — для перевірки Render, /tg — для Telegram."""
    secret = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]

    async def health(_request):
        return web.Response(text="OK")

    async def telegram(request: web.Request):
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return web.Response(status=403)
        data = await request.json()
        await app.update_queue.put(Update.de_json(data, app.bot))
        return web.Response(text="ok")

    webapp = web.Application()
    webapp.router.add_get("/", health)
    webapp.router.add_get("/health", health)
    webapp.router.add_post("/tg", telegram)

    async with app:
        await post_init(app)  # у власному webhook-режимі PTB сам post_init не викликає
        await app.start()
        await app.bot.set_webhook(
            url=f"{EXTERNAL_URL}/tg",
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
        )
        runner = web.AppRunner(webapp)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        logger.info("Бот працює через webhook: %s/tg (порт %d)", EXTERNAL_URL, PORT)
        try:

            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            await app.stop()


def main():
    app = build_app()
    if EXTERNAL_URL:

        asyncio.run(run_webhook(app))
    else:
        logger.info("RENDER_EXTERNAL_URL не задано — запускаю polling")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
