#!/usr/bin/env bash
# Nightly database backup. Run on the server from cron, as root:
#
#   15 3 * * * /root/food-tracker/scripts/backup.sh >> /var/log/food-backup.log 2>&1
#
# Keeps the last KEEP copies in data/backups. On SEND_WEEKDAY (1 = Monday ... 7 = Sunday)
# it also sends the copy to Telegram through tg-assistant's panel bot, since this VPS is
# the only server: a copy on the same disk doesn't survive losing the disk.
#
# Settings in .env: BACKUP_TG_CHAT_ID (who gets it; empty = no Telegram), and optionally
# BACKUP_TG_ENV (where to read BOT_TOKEN from, default /root/tg-assistant/.env).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

KEEP=3
SEND_WEEKDAY=7
FORCE_SEND=${1:-}  # "--send" sends today regardless of the weekday

env_value() {  # env_value FILE NAME: read one value without sourcing the whole file
    { grep -E "^$2=" "$1" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | tr -d '"'"'"'\r'
}

stamp=$(date +%F)
name="food-$stamp.db"
mkdir -p data/backups
# sqlite's online backup: consistent even while the app is writing.
docker exec food-tracker python -c "
import sqlite3
src = sqlite3.connect('/app/data/food.db')
dst = sqlite3.connect('/app/data/backups/$name')
src.backup(dst)
dst.execute('PRAGMA integrity_check').fetchone()
dst.close(); src.close()
"
echo "$(date '+%F %T') saved data/backups/$name ($(du -h "data/backups/$name" | cut -f1))"

# Keep only the newest KEEP copies.
ls -1t data/backups/food-*.db | tail -n +$((KEEP + 1)) | xargs -r rm -f

chat_id=$(env_value .env BACKUP_TG_CHAT_ID)
if [ -z "$chat_id" ]; then
    exit 0
fi
if [ "$FORCE_SEND" != "--send" ] && [ "$(date +%u)" != "$SEND_WEEKDAY" ]; then
    exit 0
fi
tg_env=$(env_value .env BACKUP_TG_ENV)
token=$(env_value "${tg_env:-/root/tg-assistant/.env}" BOT_TOKEN)
if [ -z "$token" ]; then
    echo "BOT_TOKEN not found, nothing sent" >&2
    exit 1
fi
gzip -c "data/backups/$name" > "/tmp/$name.gz"
trap 'rm -f "/tmp/$name.gz"' EXIT
# The token goes in the URL, as the Bot API wants; curl never prints it.
curl -sS --fail-with-body -o /dev/null \
    --form-string "chat_id=$chat_id" \
    --form-string "caption=🥗 Бэкап базы food-tracker за $stamp" \
    -F "document=@/tmp/$name.gz" \
    "https://api.telegram.org/bot$token/sendDocument"
echo "$(date '+%F %T') sent to Telegram"
