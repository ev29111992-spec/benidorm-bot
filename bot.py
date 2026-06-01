import os
import re
import json
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
PINNED_MSG_ID_FILE = "pinned_msg_id.json"
PORT = int(os.environ.get("PORT", 8080))

# ── Простий веб-сервер щоб Render не вбивав процес ───────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass  # вимикаємо логи HTTP

def run_web_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# ── Зберігання стану ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(PINNED_MSG_ID_FILE):
        with open(PINNED_MSG_ID_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"pinned_id": None, "items": []}

def save_state(state: dict):
    with open(PINNED_MSG_ID_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

LINE_RE = re.compile(r"^(\d+)\s*€\s*\|(.+)$", re.MULTILINE)

def parse_post(text: str, post_url: str):
    if not text:
        return None
    m = LINE_RE.search(text)
    if not m:
        return None
    price = int(m.group(1))
    description = m.group(2).strip().split("\n")[0].strip()
    label = f"{price} € | {description}"
    return {"price": price, "label": label, "url": post_url}

def build_pinned_text(items: list) -> str:
    sorted_items = sorted(items, key=lambda x: x["price"])
    lines = ["🏠 <b>Актуальні оголошення</b>\n"]
    for item in sorted_items:
        lines.append(f'• <a href="{item["url"]}">{item["label"]}</a>')
    lines.append(f"\n<i>Всього: {len(sorted_items)} об'єктів</i>")
    return "\n".join(lines)

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
            logger.warning(f"Не вдалось відредагувати: {e}")
    msg = await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    await bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=msg.message_id, disable_notification=True)
    state["pinned_id"] = msg.message_id
    save_state(state)

async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return
    state = load_state()
    if msg.message_id == state.get("pinned_id"):
        return
    channel_username = CHANNEL_ID.lstrip("@")
    post_url = f"https://t.me/{channel_username}/{msg.message_id}"
    item = parse_post(msg.text or msg.caption or "", post_url)
    if not item:
        return
    if any(i["url"] == post_url for i in state["items"]):
        return
    state["items"].append(item)
    save_state(state)
    await update_pinned(context.bot, state)
    logger.info(f"Додано: {item['label']}")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /remove <номер>\nНаприклад: /remove 785")
        return
    post_num = context.args[0].strip()
    channel_username = CHANNEL_ID.lstrip("@")
    target_url = f"https://t.me/{channel_username}/{post_num}"
    state = load_state()
    before = len(state["items"])
    state["items"] = [i for i in state["items"] if i["url"] != target_url]
    after = len(state["items"])
    if before == after:
        await update.message.reply_text(f"❌ Об'єкт #{post_num} не знайдено.")
        return
    save_state(state)
    await update_pinned(context.bot, state)
    await update.message.reply_text(f"✅ Об'єкт #{post_num} видалено. ({after} об'єктів).")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def main():
    # Запускаємо веб-сервер у окремому потоці
    t = threading.Thread(target=run_web_server, daemon=True)
    t.start()
    logger.info(f"Веб-сервер запущено на порту {PORT}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("help", cmd_help))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Бот запущено!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
