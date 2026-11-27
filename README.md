# КБЖУ по фото

PWA для себя и пары друзей: фотографируешь еду, Claude определяет продукты и вес порции, калории и БЖУ берутся из базы продуктов, а не выдумываются моделью.

## Как устроено

- **Распознавание** (`app/recognize.py`): один запрос к Claude (по умолчанию Sonnet 5.5, effort `low`, ~2–3 цента за фото). Ответ — JSON-схема: продукты, граммы, оценка КБЖУ на 100 г и уверенность.
- **Числа** (`app/foods.py`, `app/drafts.py`): каждый продукт ищется в SQLite FTS5 (USDA, свои продукты). Из близких по тексту выбирается тот, чья калорийность ближе к оценке модели. Если совпадения нет или оно отличается больше чем вдвое, остаётся оценка модели с пометкой «оценка ИИ».
- **Черновик**: перед записью можно поправить граммы, выбрать другой продукт из базы, добавить или убрать продукты.
- **Лимиты** (`app/usage.py`): каждый вызов логируется с токенами и ценой. У каждого пользователя дневной лимит в $, плюс общий месячный потолок.
- **Пользователи**: вход по приглашениям. Админ создаёт ссылку в настройках, по ней друг регистрируется.

## Локальная разработка

```bash
uv sync
cp .env.example .env        # для распознавания нужен ANTHROPIC_API_KEY
.venv/bin/python -m scripts.dev_seed   # пользователь dev / devpassword и несколько продуктов
.venv/bin/uvicorn app.main:app --port 8010 --reload
.venv/bin/pytest -q
```

## База продуктов USDA

Скачай CSV «SR Legacy» (и по желанию «Foundation Foods») на https://fdc.nal.usda.gov/download-datasets и загрузи:

```bash
python -m scripts.import_usda FoodData_Central_sr_legacy_food_csv_*.zip
```

Повторный запуск обновляет прежние USDA-строки на месте (по FDC id), так что записанные приёмы пищи остаются привязаны к продуктам. Свои продукты не трогаются.

## Деплой на de-vpn

TLS для всех приложений на сервере терминирует nginx из `/root/expense-tracker`. Это приложение подключается к его docker-сети `expense-tracker_default` как контейнер `food-tracker`.

1. DNS: A-запись поддомена → IP сервера.
2. Код и настройки:
   ```bash
   git clone <repo> /root/food-tracker && cd /root/food-tracker
   cp .env.example .env   # ключ API, SECRET_KEY, лимиты
   ./deploy.sh
   ```
3. Сертификат: вставить только 80-й блок из `deploy/nginx-food.conf` в `/root/expense-tracker/nginx.conf.template`, перезапустить nginx, выпустить сертификат:
   ```bash
   cd /root/expense-tracker
   docker compose run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot -d <поддомен>
   ```
   Затем добавить 443-й блок и `docker compose restart nginx`.
4. База и первый пользователь:
   ```bash
   docker compose -f /root/food-tracker/docker-compose.yml exec food-tracker python -m scripts.import_usda data/sr_legacy.zip
   docker compose -f /root/food-tracker/docker-compose.yml exec food-tracker python -m scripts.create_user <логин> --admin
   ```

## Бэкапы

`scripts/backup.sh` каждую ночь снимает копию базы в `data/backups` (хранятся 3 последние), а по воскресеньям отправляет её в Telegram ботом tg-assistant. Получатель — `BACKUP_TG_CHAT_ID` в `.env`, токен бота берётся из `/root/tg-assistant/.env`.

```bash
crontab -e   # 15 3 * * * /root/food-tracker/scripts/backup.sh >> /var/log/food-backup.log 2>&1
./scripts/backup.sh --send   # проверить отправку прямо сейчас
```

Восстановление: остановить контейнер, `gunzip` копию в `data/food.db`, удалить `food.db-wal` и `food.db-shm`, запустить.

Миниатюры фото еды (~20 КБ, 320 px) лежат в `data/photos` и хранятся `PHOTO_KEEP_DAYS` дней (по умолчанию 30, `0` — не сохранять совсем); черновики, которые так и не записали, удаляются через сутки. В бэкап для Telegram фото не входят — после восстановления базы карточки просто покажут иконки.

## Напоминания (Web Push)

Один раз на сервере сгенерировать ключи и дописать в `.env` вместе с `VAPID_SUBJECT` (`mailto:` или `https:` для связи):

```bash
docker compose exec food-tracker python -m scripts.vapid
```

На iPhone уведомления приходят только приложению, добавленному на экран «Домой» (iOS 16.4+). Включаются в настройках приложения. Новые ключи отменяют все подписки устройств.

## APK для Android

На Android приложение ставится и без APK: Chrome → «Установить приложение». APK нужен, чтобы просто переслать файл. Это Trusted Web Activity — оболочка на 1 МБ, которая открывает сайт через Chrome без адресной строки, так что обновления сайта приходят сами и пересобирать APK не надо. Нужен Android 7+ и Chrome.

Рецепт сборки — `android/twa-manifest.json`. Ключ подписи `android/release.keystore` и пароль `android/keystore-password.txt` в git не попадают: **сохрани их отдельно**, без этого ключа новую версию APK нельзя поставить поверх старой.

Сборка (нужны JDK 17 и Android SDK, пути к ним — в `~/.bubblewrap/config.json`):

```bash
cd android && npm ci
export BUBBLEWRAP_KEYSTORE_PASSWORD="$(cat keystore-password.txt)" BUBBLEWRAP_KEY_PASSWORD="$BUBBLEWRAP_KEYSTORE_PASSWORD"
npm run build        # → app-release-signed.apk
```

Адресную строку Chrome убирает, только если сайт подтверждает APK: в `.env` на сервере должны быть `ANDROID_PACKAGE` (как `packageId` в рецепте) и `ANDROID_CERT_SHA256` (отпечаток ключа: `keytool -list -v -keystore android/release.keystore`), они отдаются в `/.well-known/assetlinks.json`.
