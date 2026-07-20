# Campaign Copy Performance Dynamic Report

This standalone read-only report is published at:

`https://ai.yingliangads.com/reports/campaign-copy-performance/`

The service reads successful Meta Campaign copy logs and matched insight rows from the existing read-only MySQL endpoint. It refuses any configured port other than `63350`, verifies `@@read_only=1`, and caches the compact JSON payload in memory for 15 minutes. If a refresh fails, the previous payload may be served for up to 24 hours with `X-Report-Cache: stale`.

The frontend loads `./api/data` on page open. Its statistics-date presets are `全部`, `当天`, `昨天`, `近三天`, and `近七天`, based on the MySQL server date in timezone `+08:00`.

## Local checks

```bash
python3 -m py_compile service.py
python3 service.py --self-test
python3 test_contract.py
node --check < extracted-inline-script.js
```

## Production files

- release directory: `/opt/campaign-copy-performance/releases/<commit>/`
- current symlink: `/opt/campaign-copy-performance/current`
- systemd unit: `/etc/systemd/system/campaign-copy-performance.service`
- Nginx include: `/etc/nginx/default.d/campaign-copy-performance.conf`
- loopback listener: `127.0.0.1:8831`

Deploy by checking out the exact GitHub commit into a new release directory, copying only this directory into that release, validating the self-test, switching the `current` symlink, and restarting only `campaign-copy-performance.service`. Validate `nginx -t` before reloading Nginx.

Rollback by switching `/opt/campaign-copy-performance/current` to the previous release, restoring the timestamped unit/Nginx backups, then running:

```bash
systemctl daemon-reload
systemctl restart campaign-copy-performance.service
nginx -t && systemctl reload nginx
```
