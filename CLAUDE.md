# Правила проекта

Что это и как запускать — в `README.md`. Здесь только то, что легко сломать.

## Репозиторий публичный

`github.com/Botaliy/food-tracker` открыт всем. В git не должны попадать ключи, пароли, токены, `.env`, содержимое `data/` и ключ подписи APK.

## APK для Android

APK — оболочка (Trusted Web Activity) вокруг сайта. Сайт обновляется сам, **пересобирать APK при изменениях приложения не нужно**. Пересборка нужна только если меняется `android/twa-manifest.json`: название, иконка, цвета, домен.

Ключ подписи:
- `android/release.keystore` и `android/keystore-password.txt` — в `.gitignore`, в git их не добавлять.
- Копия лежит на de-vpn: `/root/food-tracker/data/android-key/`.
- Новый ключ не генерировать. APK с другим ключом не ставится поверх старого, и сайт перестанет его подтверждать (`ANDROID_CERT_SHA256` в `.env` на сервере). Если локального ключа нет — забрать копию с сервера.

Выпуск новой версии APK:
1. Поднять `appVersionCode` (и `appVersionName`) в `android/twa-manifest.json` — иначе Android не обновит установленное приложение.
2. Собрать по инструкции из README (`npm run build` в `android/`).
3. Положить файл на сервер — ссылка «Приложение для Android» в профиле и `https://<домен>/app.apk` отдают именно его:
   ```bash
   scp android/app-release-signed.apk de-vpn:/root/food-tracker/data/android/kbju.apk
   ```
   Деплой для этого не нужен. Пока файла нет, ссылка в профиле скрыта.
4. Проверить: `curl -sI https://<домен>/app.apk` → 200, и `/.well-known/assetlinks.json` отдаёт отпечаток ключа.
