# X personal publishing frontend contract

All endpoints below use `/api/x-video-publish`, Cookie authentication and module `x_video_publish`. The server derives tenant and owner. IDs are strings. `account_id` is the internal X account record ID; `x_user_id` is the platform ID used in Excel. The admin account list remains admin-only; new publishing actions use the owner-scoped account response, including for admins.

## Read DTOs

- `GET /accounts` -> `{items:[{account_id,x_user_id,display_name,username,profile_image_url,language,status,publish_approved,eligible,blocked_reason,caption_template,config_version,operating_stats,public_metrics,daily_auto_publish_configured}]}`. `config_version` is an integer, 0 before first save. `eligible` is authoritative; omitted means false. Never expose credentials. `POST /accounts/{id}/settings` accepts `{expected_version,caption_template}` and returns `{account:...}`. Language stays in the existing account-language control; this settings form is readonly for language.
- `GET /languages` -> `{items:[{value,label}]}`.
- `GET /materials?q=&product=Drama-社媒专用素材&language=&cursor=&limit=50` -> `{items:[{material_id,material_name,drama_name,drama_description,language,duration_seconds,thumbnail_url,preview_url}],next_cursor,empty_reason}`. Nonempty URLs must be safe HTTP(S) URLs. Browsing and name searches use the selected product (`Dramawave` or default `Drama-社媒专用素材`). Numeric q is an exact ID across products; existing media/drama/language validation still applies. Product changes reset pagination and preserve staged selections.
- `GET /draft` -> `{draft:null|{schema:1,material_ids:[],account_ids:[],pairs:[],material_schedules_local:{},processing_mode,active_pair,materials:[]}}`; `POST /draft` accepts `{draft:...|null}`. `materials` are display snapshots only. Restored material/pair facts must be resolved again on preflight; no prototype localStorage keys are read.

## Manual and Excel operations

`POST /preflight` accepts `{idempotency_key,source:"manual",material_ids:[],account_ids:[],pairs:[{material_id,account_id,caption_template,config_version}],material_schedules:{material_id:null|UTC_ISO},processing_mode:"original"|"random_overlay"}`. No auto pairing. Every selected material needs an explicit pair. Same material shares its time. 200 tasks max.

`POST /excel/preview` accepts `{idempotency_key,source:"excel",filename,rows:[{row_number,x_user_id,material_id,caption_template,scheduled_at_utc:null|UTC_ISO,processing_mode}],client_skipped:[{row_number,message}],total_rows}`. Browser parses the five-column .xlsx locally; no file/base64 upload. Empty caption means current account default; server resolves x_user_id to owned account_id. All facts are independently revalidated server-side.

Both previews return `{preview_id,expires_at_utc,items:[{row_number?,material_id,material_name,account_id,x_user_id,account_name,caption,caption_template,planned_at_utc,schedule_kind,processing_mode,repeat_required?}],skipped:[{row_number,message,code}],repeat_required}`. Preview performs no writes to X. Historical successes need explicit repeat consent; in-progress/unknown pairs remain blocked. Preview can contain zero valid items and must then be non-submittable.

`POST /batches` accepts `{idempotency_key,preview_id,allow_repeat:boolean}`. Same operation key and payload has one durable result. On known rejection return `{ok:false,accepted:false,message,code}`. Network errors/ambiguous accepted state preserve exact request and query `GET /operations/{key}` -> `{batch_id,batch?}` (404 means no known acceptance, not proof no prior write). Retry only the exact frozen body.

- `GET /batches?cursor=&limit=50` -> `{items:[{batch_id,source,status,created_at_utc,total_tasks}],next_cursor}`.
- `GET /batches/{id}` -> `{batch:{batch_id,source,status,blocked_reason,created_at_utc,items:[{task_id,material_id,material_name,account_id,account_name,caption,planned_at_utc,processing_mode,status,error_message,post_url,row_number?}]}}`.

## Account material pools

`GET /accounts/{id}/pool?state=pending|processing|published|failed|slots&cursor=&limit=50` -> `{pool:{version,enabled,times:[],processing_mode,language,pause_reason},items:[{item_id,material_id,material_name,status,task_id,batch_id,reason}],slots:[{slot_id,at_utc,status,reason,batch_id}],counts:{},next_cursor}`.

Writes use `{expected_version,...}` and return the updated pool. `POST .../settings`: `{expected_version,times:["HH:mm"],processing_mode}` (first save paused); `/enable`, `/pause`; `/remove`: `{expected_version,item_ids:[]}`; `/move`: `{expected_version,item_id,direction:"up"|"down"}`. Pausing preserves generated tasks.

`POST .../items/preview`: `{expected_version,material_ids:[]}` -> `{preview_id,expires_at_utc,items:[{material_id,material_name}],skipped:[{material_id,message}]}`; `POST .../items`: `{expected_version,idempotency_key,preview_id}`. Unknown admission retains exact body; `GET .../operations/{key}` resolves acceptance. Frontend never runs pool scheduling or provides the prototype's demonstration slot action. Scheduler selects queue head at real server Beijing slots, does not backfill missed slots, pauses on unknown outcomes.

## UI ownership and status

Production statuses are rendered from server DTO only. Recognized queued/preparing/ready/running/processing/submitted/published/completed/partial/failed/blocked/unknown/cancelled/skipped/stopped; unknown raw statuses remain visible. New batch creation does not depend on previous batch completion. Admin new actions require module access AND account_id in `/accounts`; originals (sync, approval, language) keep current permissions. Regular users manage their owned list within the workspace. No demo data, demo timers or prototype draft migration is permitted.
