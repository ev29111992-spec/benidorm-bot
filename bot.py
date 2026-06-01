import os
import re
import json
import logging
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]          # напр. @benidorm_rent_sale або -100xxxxxxxxxx
PINNED_MSG_ID_FILE = "pinned_msg_id.json"

# ── Зберігання стану ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(PINNED_MSG_ID_FILE):
        with open(PINNED_MSG_ID_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"pinned_id": None, "items": []}

def save_state(state: dict):
    with open(PINNED_MSG_ID_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ── Парсинг рядка оголошення ──────────────────────────────────────────────────
# Формат: 950 € | 1-кімн. кв. 65 м² | Benidorm, Levante

LINE_RE = re.compile(r"^(\d+)\s*€\s*\|(.+)$", re.MULTILINE)

def parse_post(text: str, post_url: str) -> dict | None:
    """Розпізнає перший рядок оголошення та повертає item-dict або None."""
    if not text:
        return None
    m = LINE_RE.search(text)
    if not m:
        return None
    price = int(m.group(1))
    description = m.group(2).strip()
    # Беремо тільки перший рядок опису (до першого \n)
    description = description.split("\n")[0].strip()
    label = f"{price} € | {description}"
    return {"price": price, "label": label, "url": post_url}

# ── Формування тексту закріпленого посту ─────────────────────────────────────

def build_pinned_text(items: list) -> str:
    sorted_items = sorted(items, key=lambda x: x["price"])
    lines = ["🏠 <b>Актуальні оголошення</b>\n"]
    for item in sorted_items:
        lines.append(f'• <a href="{item["url"]}">{item["label"]}</a>')
    lines.append(f"\n<i>Всього: {len(sorted_items)} об'єктів</i>")
    return "\n".join(lines)

# ── Оновлення або створення закріпленого посту ───────────────────────────────

async def update_pinned(bot: Bot, state: dict):
    text = build_pinned_text(state["items"])
    if state.get("pinned_id"):
        try:
            await bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=state["pinned_id"],
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except Exception as e:
            logger.warning(f"Не вдалось відредагувати закріплений пост: {e}")

    # Якщо посту ще немає або він видалений — створюємо новий
    msg = await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    await bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=msg.message_id, disable_notification=True)
    state["pinned_id"] = msg.message_id
    save_state(state)

# ── Обробники ─────────────────────────────────────────────────────────────────

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Спрацьовує на кожен новий пост у каналі."""
    msg = update.channel_post
    if not msg:
        return

    # Пропускаємо власний закріплений пост (щоб не додати його до списку)
    state = load_state()
    if msg.message_id == state.get("pinned_id"):
        return

    channel_username = CHANNEL_ID.lstrip("@")
    post_url = f"https://t.me/{channel_username}/{msg.message_id}"
    item = parse_post(msg.text or msg.caption or "", post_url)
    if not item:
        return  # Не схоже на оголошення

    # Перевіряємо дублікат за URL
    if any(i["url"] == post_url for i in state["items"]):
        return

    state["items"].append(item)
    save_state(state)
    await update_pinned(context.bot, state)
    logger.info(f"Додано: {item['label']}")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/remove <номер_посту>  — видаляє об'єкт зі списку."""
    if not context.args:
        await update.message.reply_text("Використання: /remove <номер_посту>\nНаприклад: /remove 785")
        return

    post_num = context.args[0].strip()
    channel_username = CHANNEL_ID.lstrip("@")
    target_url = f"https://t.me/{channel_username}/{post_num}"

    state = load_state()
    before = len(state["items"])
    state["items"] = [i for i in state["items"] if i["url"] != target_url]
    after = len(state["items"])

    if before == after:
        await update.message.reply_text(f"❌ Об'єкт з постом #{post_num} не знайдено в списку.")
        return

    save_state(state)
    await update_pinned(context.bot, state)
    await update.message.reply_text(f"✅ Об'єкт #{post_num} видалено. Список оновлено ({after} об'єктів).")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list — показує поточний список у чаті з ботом."""
    state = load_state()
    if not state["items"]:
        await update.message.reply_text("Список порожній.")
        return
    text = build_pinned_text(state["items"])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команди:\n"
        "/list — показати поточний список\n"
        "/remove <номер> — видалити об'єкт\n"
        "Наприклад: /remove 785"
    )

# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("help", cmd_help))
    logger.info("Бот запущено...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
