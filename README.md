# Grey Trade Bot

Telegram-бот: присылаешь скриншот сделки — он распознаёт её через Gemini,
показывает на проверку и после нажатия «✅ Добавить» записывает в Firestore
(коллекция `trades` проекта `grey-tracker`). Сделка сразу появляется на
https://greycrpt.github.io/grey-tracker/

## Переменные окружения

- `TELEGRAM_TOKEN` — токен нового бота из @BotFather
- `GEMINI_API_KEY` — ключ из Google AI Studio
- `GEMINI_MODEL` = `gemini-2.5-flash`
- `FIREBASE_CREDENTIALS` — всё содержимое JSON-ключа сервисного аккаунта
  (Firebase Console → Project settings → Service accounts → Generate new private key)
- `ALLOWED_USER_IDS` — твой Telegram ID (можно несколько через запятую).
  Узнать: напиши боту `/start` — он покажет ID, или спроси у @userinfobot

## Исправление распознанного

Перед подтверждением можно написать боту исправления, по одному на строку:

```
плечо 20
pnl -12.5
тип short
статус open
дата 2026-09-28
```

## Запуск

```bash
pip install -r requirements.txt
python trade_bot.py
```
