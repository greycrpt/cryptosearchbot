import io
import json
import logging
import os
import re
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import firebase_admin
import PIL.Image
from firebase_admin import credentials, firestore
from google import genai
from google.genai import types
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# НАСТРОЙКИ (переменные окружения)
# TELEGRAM_TOKEN        — токен бота из @BotFather
# GEMINI_API_KEY        — ключ из Google AI Studio
# GEMINI_MODEL          — модель Gemini
# FIREBASE_CREDENTIALS  — содержимое JSON-ключа сервисного аккаунта Firebase
# ALLOWED_USER_IDS      — Telegram ID через запятую, кому можно добавлять сделки
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS")
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
# ============================================================

SYSTEM_PROMPT = f"""Ты читаешь скриншоты криптовалютных фьючерсных сделок (Bybit, Binance, BingX, OKX и т.п.).
Верни ТОЛЬКО JSON с полями:
- pair: торговая пара заглавными без разделителей, например "ETHUSDT"
- dir: "long" или "short"
- lev: плечо числом строкой без "x", например "20"; если не видно — ""
- entry: цена входа строкой; если не видно — ""
- tp: цена выхода/тейк-профит строкой; если не видно — ""
- pnl: доходность в процентах (ROI) числом, со знаком минус для убытка, например 41.76 или -12
- status: "closed" если сделка закрыта, "open" если позиция ещё открыта
- date: дата сделки в формате YYYY-MM-DD; если года нет — считай текущий год; если даты нет — "{{today}}"

Правила:
- pnl — это именно процент (ROI %), а не сумма в USDT
- Buy/Long/Покупка = "long", Sell/Short/Продажа = "short"
- ничего не выдумывай: если значения нет на скриншоте, ставь ""
"""

FIELDS = {
    "pair": "Пара",
    "dir": "Тип",
    "lev": "Плечо",
    "entry": "Вход",
    "tp": "TP",
    "pnl": "PnL %",
    "status": "Статус",
    "date": "Дата",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


for name, value in [
    ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
    ("GEMINI_API_KEY", GEMINI_API_KEY),
    ("FIREBASE_CREDENTIALS", FIREBASE_CREDENTIALS),
]:
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}.")

if not ALLOWED_USER_IDS:
    raise RuntimeError("Не задан ALLOWED_USER_IDS. Узнай свой Telegram ID у @userinfobot.")

# Инициализация Gemini и Firestore
client = genai.Client(api_key=GEMINI_API_KEY)


def load_firebase_credentials(raw: str) -> dict:
    """Читает JSON-ключ Firebase и чинит ключ, если при копировании
    дефисы превратились в тире или переносы строк сломались."""
    info = json.loads(raw.strip())
    key = info.get("private_key", "").replace("\\n", "\n")
    match = re.search(r"BEGIN PRIVATE KEY(.*?)END PRIVATE KEY", key, re.S)
    if match:
        body = re.sub(r"[^A-Za-z0-9+/=]", "", match.group(1))
        lines = [body[i:i + 64] for i in range(0, len(body), 64)]
        info["private_key"] = (
            "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"
        )
    return info


firebase_admin.initialize_app(credentials.Certificate(load_firebase_credentials(FIREBASE_CREDENTIALS)))
db = firestore.client()
trades_col = db.collection("trades")


def normalize(raw: dict) -> dict:
    """Приводит распознанные данные к формату сайта grey-tracker."""
    trade = {
        "pair": str(raw.get("pair") or "").upper().replace("/", "").replace("-", "").replace(" ", ""),
        "dir": "short" if str(raw.get("dir", "")).lower() == "short" else "long",
        "lev": str(raw.get("lev") or "").lower().replace("x", "").replace("×", "").strip(),
        "entry": str(raw.get("entry") or "").strip(),
        "tp": str(raw.get("tp") or "").strip(),
        "status": "open" if str(raw.get("status", "")).lower() == "open" else "closed",
        "date": str(raw.get("date") or date.today().isoformat()),
    }
    try:
        trade["pnl"] = round(float(str(raw.get("pnl", 0)).replace("%", "").replace(",", ".").replace("+", "")), 2)
    except ValueError:
        trade["pnl"] = 0.0
    return trade


def format_trade(trade: dict) -> str:
    pnl = trade["pnl"]
    lines = [
        f"Пара: {trade['pair'] or '—'}",
        f"Тип: {trade['dir'].upper()}",
        f"Плечо: ×{trade['lev'] or '—'}",
        f"Вход / TP: {trade['entry'] or '—'} → {trade['tp'] or '—'}",
        f"PnL: {'+' if pnl >= 0 else ''}{pnl:.2f}%",
        f"Статус: {'Открыта' if trade['status'] == 'open' else 'Закрыта'}",
        f"Дата: {trade['date']}",
    ]
    return "\n".join(lines)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Добавить", callback_data="save"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
        ]
    ])


def is_allowed(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ALLOWED_USER_IDS)


async def show_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trade = context.user_data["pending"]
    await update.effective_message.reply_text(
        "📋 Распознал сделку:\n\n"
        f"{format_trade(trade)}\n\n"
        "Если что-то не так — напиши исправление, например:\n"
        "плечо 20\npnl -12.5\nтип short\nстатус open\nдата 2026-09-28",
        reply_markup=confirm_keyboard(),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скриншот сделки → распознать → показать на подтверждение."""
    if not update.message or not is_allowed(update):
        return

    await update.message.reply_text("⏳ Читаю скриншот сделки...")

    try:
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        else:
            file_id = update.message.document.file_id
        file = await context.bot.get_file(file_id)
        file_bytes = await file.download_as_bytearray()
        image = PIL.Image.open(io.BytesIO(file_bytes))

        caption = update.message.caption or ""
        prompt = "Распознай сделку на скриншоте."
        if caption:
            prompt += f"\nПодсказка от пользователя: {caption}"

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[image, prompt],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT.replace("{today}", date.today().isoformat()),
                response_mime_type="application/json",
            ),
        )
        raw = json.loads(response.text or "{}")
        if isinstance(raw, list):
            raw = raw[0] if raw else {}

        context.user_data["pending"] = normalize(raw)
        await show_pending(update, context)
    except Exception as e:
        logger.exception("Ошибка распознавания")
        await update.message.reply_text(f"❌ Не получилось распознать скриншот: {e}")


EDIT_ALIASES = {
    "пара": "pair", "pair": "pair", "монета": "pair",
    "тип": "dir", "dir": "dir", "направление": "dir",
    "плечо": "lev", "lev": "lev", "leverage": "lev",
    "вход": "entry", "entry": "entry",
    "tp": "tp", "тп": "tp", "выход": "tp", "тейк": "tp",
    "pnl": "pnl", "пнл": "pnl", "профит": "pnl",
    "статус": "status", "status": "status",
    "дата": "date", "date": "date",
}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Исправление полей ожидающей сделки: «плечо 20», «pnl -12» и т.д."""
    if not update.message or not is_allowed(update):
        return

    trade = context.user_data.get("pending")
    if not trade:
        await update.message.reply_text("📸 Скинь скриншот сделки — я её распознаю и добавлю на сайт.")
        return

    changed = False
    for line in update.message.text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        key = EDIT_ALIASES.get(parts[0].lower().rstrip(":"))
        if not key:
            continue
        value = parts[1].strip()
        if key == "dir":
            value = "short" if value.lower() in ("short", "шорт", "sell") else "long"
        elif key == "status":
            value = "open" if value.lower() in ("open", "открыта", "открытая") else "closed"
        trade.update(normalize({**trade, key: value}))
        changed = True

    if not changed:
        await update.message.reply_text("Не понял, что исправить. Пример: плечо 20 или pnl -12.5")
        return

    await show_pending(update, context)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(update):
        return

    trade = context.user_data.pop("pending", None)
    if query.data == "cancel":
        await query.edit_message_text("❌ Отменено.")
        return

    if not trade:
        await query.edit_message_text("Эта сделка уже обработана. Скинь новый скриншот.")
        return

    if not trade["pair"]:
        context.user_data["pending"] = trade
        await query.message.reply_text("Не хватает пары. Напиши, например: пара ETHUSDT")
        return

    try:
        trades_col.add(trade)
        await query.edit_message_text(f"✅ Добавлено на сайт:\n\n{format_trade(trade)}")
    except Exception as e:
        logger.exception("Ошибка записи в Firestore")
        context.user_data["pending"] = trade
        await query.message.reply_text(f"❌ Не получилось сохранить: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if not is_allowed(update):
        await update.message.reply_text(
            f"⛔ Нет доступа. Твой Telegram ID: {update.effective_user.id}"
        )
        return

    await update.message.reply_text(
        "👋 Привет! Скинь скриншот сделки — я распознаю пару, тип, плечо и PnL, "
        "покажу тебе на проверку и после подтверждения добавлю её в Grey Tracker."
    )


def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_button))

    print("✅ Бот сделок запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
