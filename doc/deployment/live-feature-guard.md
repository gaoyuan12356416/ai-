# Live Feature Deployment Guard

`ai.yingliangads.com` is deployed as one shared monolithic service. Git branches are isolated, but production files are not: every deployment overwrites the same `/root/drama_material_service/app.py` and the same nginx static files under `/usr/share/nginx/html`.

Before deploying any branch, the candidate must still include all active live feature tokens listed in `deploy/live_feature_guard.json`.

## Required Checks

Run locally before upload:

```bash
python scripts/verify_live_feature_guard.py --root .
python -m py_compile app.py
node --check static/quick-nav.js
```

Run on the server after upload and before restart when the script is present:

```bash
cd /root/drama_material_service
python3 scripts/verify_live_feature_guard.py --root /root/drama_material_service --public-root /usr/share/nginx/html
```

If the guard fails, stop the deployment and merge the missing live feature branch into the deployment branch. Do not copy a branch's `app.py` over production just because that branch's own feature works locally.

## Updating the Guard

Add a new feature to `deploy/live_feature_guard.json` when it becomes live and user-facing. Retire a feature from the manifest only when it has been intentionally removed from production.
