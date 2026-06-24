import logging
import os
import threading
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import PIL.Image
import io
from http.server import BaseHTTPRequestHandler, HTTPServer

# ============================================================
# НАСТРОЙКИ
# Ключи лучше хранить не в коде, а в переменных окружения:
# export TELEGRAM_TOKEN="токен_из_BotFather"
# export GEMINI_API_KEY="ключ_из_Google_AI_Studio"
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
# ============================================================

# Системный промпт — характер и задача бота
SYSTEM_PROMPT = """Ты помощник для траффера в Telegram.

Твоя задача: траффер присылает тебе сообщение лида или скриншот переписки, а ты даешь короткий готовый ответ, который можно сразу отправить лиду.

Главный стиль:
- отвечай как живой человек в Telegram
- коротко, спокойно, уверенно
- без длинных объяснений и лекций
- без официального тона
- не пиши как консультант, преподаватель или банк
- не используй жирный текст, списки и большие абзацы
- максимум 1-3 коротких сообщения
- если диалог уже идет, не начинай с приветствия
- отвечай на языке лида
- можно писать простыми фразами, как в обычной переписке
- не дави на человека
- не спорь с лидом, если он сомневается

Безопасность и честность:
- не обещай гарантированный заработок
- не пиши "100%", "без риска", "точно заработаешь"
- не проси сид-фразы, пароли, коды, доступы к аккаунтам
- не придумывай факты, которых нет в сообщении
- если точной суммы депозита нет, не называй ее
- если вопрос лучше решать с трейдером, предложи перевести на трейдера

Формат ответа всегда такой:

ГОТОВЫЙ ОТВЕТ:
[короткий текст, который траффер отправит лиду]

ПОЧЕМУ:
[одно короткое объяснение для траффера]

Примеры стиля:

Лид: куда мне депать и что я буду торговать?
Ответ: торговать будете со своего личного депозита, я просто переведу вас на трейдера, он уже покажет что и как делать

Лид: я буду личным депом торговать или мне предоставите?
Ответ: вы будете торговать своим личным депозитом

Лид: а какая проходимость сигналов?
Ответ: по сигналам трейдер уже подробнее сориентирует, обычно винрейт хороший, но все равно важно аккуратно заходить в сделки

Лид: меня могут кинуть? откроют лонг, второму шорт и заберут 40%
Ответ: понимаю, почему такой вопрос. чтобы не было таких мыслей, трейдер показывает историю сделок после закрытия, а по открытым сделкам может дать обоснованный анализ

Лид: на Bybit нет таких монет
Ответ: на Bybit много разных пар, но лучше напиши трейдеру, он подскажет какие пары будут для торговли

Лид: депозит какой нужен?
Ответ: по депозиту лучше уже с трейдером обсудить, он скажет от какой суммы комфортно начинать

Лид: форекс или крипто?
Ответ: крипто

Лид: дайте тег трейдера
Ответ: сейчас скину тег трейдера, напиши что ты от меня

Лид: откуда вы меня нашли?
Ответ: в крипто канале, там увидел твою активность

Лид: что за работа?
Ответ: если коротко, ты получаешь сигналы от трейдера, торгуешь по ним со своего депозита, а прибыль с тейков делится 60/40

Лид: дашь на деп?
Ответ: депозит нужен свой, мы как раз ищем людей, кто готов торговать со своего депозита по сигналам трейдера

Лид: можно попробовать?
Ответ: да, можно. я переведу тебя на трейдера, он объяснит процесс и скажет как лучше начать
"""

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

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN. Получи токен у @BotFather и добавь его в переменные окружения.")

if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY. Создай ключ в Google AI Studio и добавь его в переменные окружения.")

# Инициализация Gemini
client = genai.Client(api_key=GEMINI_API_KEY)
generation_config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстового сообщения от трафера"""
    if not update.message:
        return

    user_message = update.message.text
    
    await update.message.reply_text("⏳ Анализирую вопрос лида...")
    
    try:
        prompt = f"СООБЩЕНИЕ ЛИДА:\n{user_message}"
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=generation_config,
        )
        await update.message.reply_text(response.text or "Не получилось сформировать ответ. Попробуй отправить сообщение еще раз.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка скриншота от трафера"""
    if not update.message:
        return

    await update.message.reply_text("⏳ Читаю скриншот...")
    
    try:
        # Скачиваем фото
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        
        # Открываем как изображение
        image = PIL.Image.open(io.BytesIO(file_bytes))
        
        caption = update.message.caption or ""
        prompt = f"Трафер прислал скриншот переписки с лидом. {caption}\nПроанализируй что спрашивает лид и дай готовый ответ."
        
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[image, prompt],
            config=generation_config,
        )
        await update.message.reply_text(response.text or "Не получилось прочитать скриншот. Попробуй отправить более четкое изображение.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при обработке скриншота: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await update.message.reply_text(
        "👋 Привет! Я твой крипто-ассистент для работы с лидами.\n\n"
        "📌 Как пользоваться:\n"
        "• Скинь мне сообщение лида — я скажу как ответить\n"
        "• Скинь скриншот переписки — разберу и дам готовый ответ\n\n"
        "Поехали! 🚀"
    )

def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    print("✅ Бот запущен! Нажми Ctrl+C чтобы остановить.")
    app.run_polling()

if __name__ == '__main__':
    main()
