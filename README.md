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

Повторный запуск заменяет прежние USDA-строки. Свои продукты не трогаются.

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
