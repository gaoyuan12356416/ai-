# Auto Cover Synthesis

This job submits high-ROAS Dramawave dramas to the cover/screenshot synthesis backend.

Schedule:

- `10:00` and `22:00` server time every day.

Default filter:

- Source table: `kunlunads_dev.ads_custom_source_insight`
- `product in ('dramawave')`
- `dt = DATE_SUB(CURDATE(), INTERVAL 1 DAY)`
- `platform = '0'`
- Group by `data_source_id`
- `af_roas0 >= 45`
- `spend >= 1000`

Submission behavior:

- Uses `data_source_id` as backend `content_id`.
- Uses `AUTO_COVER_TARGET_APP_ID`, default `1479`, as backend `app_id`.
- Verifies the drama exists in `ads_drama_resource` for the target app before submission.
- Skips rows that already exist in SQLite table `drama_screenshot_job` for the same `app_id + content_id`.
- Submits to `POST /api/drama-screenshot-material/jobs` with the configured screenshot API token.
- A submitted screenshot job is marked `done` only after all generated assets have been sent through the AI source callback. Its final `updated_at` and `finished_at` should therefore be later than, or equal to, the material-source ingestion request.

Deployment:

```bash
cd /root/drama_material_service
chmod +x scripts/run_auto_cover_synthesis.sh scripts/install_auto_cover_synthesis_cron.sh
scripts/run_auto_cover_synthesis.sh --dry-run
scripts/install_auto_cover_synthesis_cron.sh
```
