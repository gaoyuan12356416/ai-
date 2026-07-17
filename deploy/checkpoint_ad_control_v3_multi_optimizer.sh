#!/usr/bin/env bash
set -euo pipefail

backup=${1:?usage: checkpoint_ad_control_v3_multi_optimizer.sh BACKUP_DIR}
case "$backup" in
  /mnt/data-disk/ai-ad-control-v3/backups/*) ;;
  *)
    echo "backup directory must stay under the V3 data-disk backup root" >&2
    exit 97
    ;;
esac
if [[ -e "$backup" ]]; then
  echo "backup directory already exists: $backup" >&2
  exit 98
fi

install -d -m 700 \
  "$backup/live" \
  "$backup/live/static" \
  "$backup/live/public-static" \
  "$backup/systemd" \
  "$backup/nginx" \
  "$backup/cron" \
  "$backup/config" \
  "$backup/sqlite" \
  "$backup/database"
touch "$backup/INCOMPLETE"

cp -a /root/drama_material_service/app.py "$backup/live/"
cp -a /root/drama_material_service/features/ad_control_v3 "$backup/live/"
cp -a /root/drama_material_service/scripts/ad_control_v3_runner.py "$backup/live/"
for filename in quick-nav.js ui-topbar.js ui-topbar.css; do
  if [[ -f "/root/drama_material_service/static/$filename" ]]; then
    cp -a "/root/drama_material_service/static/$filename" "$backup/live/static/"
  fi
  if [[ -f "/usr/share/nginx/html/$filename" ]]; then
    cp -a "/usr/share/nginx/html/$filename" "$backup/live/public-static/"
  fi
done

cp -a /root/drama_material_service/.env "$backup/config/app.env"
cp -a /mnt/data-disk/ai-ad-control-v3/config/runtime.env "$backup/config/runtime.env"
cp -a /etc/systemd/system/drama-material-api.service "$backup/systemd/"
cp -a /etc/systemd/system/drama-material-api.service.d "$backup/systemd/"
cp -a /etc/systemd/system/ad-control-v3-runner.service "$backup/systemd/"
cp -a /etc/systemd/system/ad-control-v3-runner.timer "$backup/systemd/"
cp -a /etc/nginx/nginx.conf "$backup/nginx/"
cp -a /etc/nginx/default.d/x-oauth.conf "$backup/nginx/"
cp -a /etc/crontab "$backup/cron/"
cp -a /etc/cron.d "$backup/cron/"
crontab -l > "$backup/cron/root.crontab" 2>&1 || true

sqlite3 /root/drama_material_service/data/drama_material_jobs.sqlite3 \
  ".backup $backup/sqlite/data-drama_material_jobs.sqlite3"
cp -a /root/drama_material_service/drama_material_jobs.sqlite3 \
  "$backup/sqlite/root-drama_material_jobs.sqlite3"

systemctl status drama-material-api.service --no-pager > "$backup/systemd/api-status-before.txt"
systemctl status ad-control-v3-runner.timer --no-pager > "$backup/systemd/timer-status-before.txt"
git -C /mnt/data-disk/ai-ad-control-v3/staging/repo-9450cc605a12 rev-parse HEAD \
  > "$backup/source-commit.txt"
date -Is > "$backup/created-at.txt"

# shellcheck disable=SC1091
source /mnt/data-disk/ai-ad-control-v3/config/runtime.env
MYSQL_PWD="$AD_CONTROL_V3_STORE_WRITER_MYSQL_PASSWORD" mysql \
  --host="$AD_CONTROL_V3_STORE_WRITER_MYSQL_HOST" \
  --port="$AD_CONTROL_V3_STORE_WRITER_MYSQL_PORT" \
  --user="$AD_CONTROL_V3_STORE_WRITER_MYSQL_USER" \
  --database=ads_ai \
  --batch --raw --execute="
    SELECT DATABASE() AS database_name, @@read_only AS read_only;
    SHOW CREATE TABLE ad_control_v3_rule_group;
    SELECT COUNT(*) AS rule_group_count FROM ad_control_v3_rule_group;
    SELECT COUNT(*) AS optimizer_scope_table_count
      FROM information_schema.tables
      WHERE table_schema=DATABASE()
        AND table_name='ad_control_v3_rule_group_optimizer';
    SHOW TABLE STATUS;
  " > "$backup/database/ads-ai-before.txt"

chmod -R go-rwx "$backup"
mv "$backup/INCOMPLETE" "$backup/COMPLETE"
find "$backup" -type f \
  ! -name manifest.sha256 \
  ! -name manifest-check.txt \
  -print0 | sort -z | xargs -0 sha256sum > "$backup/manifest.sha256"
sha256sum -c "$backup/manifest.sha256" > "$backup/manifest-check.txt"

echo "$backup"
wc -l "$backup/manifest.sha256"
