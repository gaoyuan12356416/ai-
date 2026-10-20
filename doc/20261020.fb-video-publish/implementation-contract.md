# FB video publishing implementation contract

The new module is `features.fb_video_publish`, mounted on the existing FB sidecar.
It owns additive `fb_video_*` tables in the operational SQLite. Existing automatic
template, GPU, links and history contracts are preserved. Main API authenticates
and supplies trusted actor `{tenant_key,user_id,user_name,is_admin,business_owner_id,sub_user_id}`.

## Public API

All paths start `/api/fb-video-publish`. JSON response includes `ok`.

- `GET /pages/available?q=` → `{items:[Page]}` (source access scope).
- `GET /pages` → `{items:[PageConfig]}`; first use is empty. PageConfig contains
  `page_id,page_name,language,caption_template,config_instance_id,version,updated_at_utc,eligible_token_count,blocked_reason`.
- `POST /pages` with `{page_ids,language,caption_template}` atomically creates all.
- `POST /pages/{page_id}` with `{config_instance_id,expected_version,language,caption_template}` updates one.
- `POST /pages/{page_id}/delete` with `{config_instance_id,expected_version}` soft deletes the config only.
- `GET /materials?q=&language=&cursor=&limit=50` → `{items,next_cursor,has_more}`.
- `GET /languages` → `{items:[{value,label}]}`; canonical language codes.
- `GET /draft` → `{draft:object|null}`; `POST /draft` with `{draft:object|null}`.
- `POST /preflight` with the batch payload below → `{preview_token,expires_at_utc,preview_hash,items,repeat_required,repeat_fingerprint}`.
- `POST /batches` with `{...batch,preview_token,allow_repeat:boolean}` → `{batch_id,operation_id,idempotent,batch}`.
- `GET /operations/{operation_id}` → same batch response, or 404 `operation_not_found`.
- `GET /batches?limit=50&cursor=` → `{items,next_cursor}`; `GET /batches/{batch_id}` → `{batch}`.
- `GET /tasks?limit=50&cursor=&status=` → `{items,next_cursor,has_more}`;
  `GET /tasks/{task_id}` → `{task}`. Both enforce tenant and owner/admin scope.
- `POST /tasks/{task_id}/cancel` with `{}` → `{task}`. Only unsent tasks cancel.

Batch payload:

```json
{
  "operation_id":"ea152f589e794a6c90dc2520f9d4f2fa",
  "material_ids":["123","456"],
  "page_ids":["789"],
  "pairs":[
    {"material_id":"123","page_id":"789","config_instance_id":"647a69a1d1ce423ca29d2c6538ca3b00","config_version":1,"caption_template":"{name}\n{url}"},
    {"material_id":"456","page_id":"789","config_instance_id":"647a69a1d1ce423ca29d2c6538ca3b00","config_version":1,"caption_template":"{name}\n{url}"}
  ],
  "processing_mode":"original",
  "schedule_mode":"now",
  "scheduled_at_utc":"",
  "interval_minutes":0
}
```

`processing_mode=original|random_overlay`, `schedule_mode=now|scheduled`. Explicit
pair order follows `material_ids`, then `page_ids`; one selected material must have
at least one same-language Page. Maximum 200 pairs, interval 0–1440 integer minutes.
Preflight item includes material/Page names, pair IDs, caption, short_url marked
placeholder, config version and planned time. No real ID, link or GPU allocation.
`long_url_parameters` shows the eight attribution values with placeholders only
for new timestamp/ledger/task IDs. Preview expansion reserves the real URL length
of a maximum 19-digit ledger ID. Operation IDs are lowercase UUID32 or UUID36.
Submit preserves operation_id across ambiguous failures. Same operation plus
different semantic payload is 409. Only published history can be explicitly
repeated; active/unknown is never overridden. Signed token binds trusted actor,
full normalized payload, source/config metadata and repeated-history fingerprint.
GET operation 404 does not prove that an in-flight submit cannot still commit.
Only explicit validation errors carry `accepted:false`; unexpected transport
failure retains the same frozen payload/operation. Batch lists contain compact
counts and summaries; only the batch detail loads full task snapshots.

Batch detail has batch_id, operation_id, owner_user_id, created_at_utc, status,
processing_mode, items. Task items include task_id (32hex), ledger_id (integer),
page/material snapshots, caption_template, caption, template_version, short_url,
long_url, planned_at_utc, page_sequence, status, error_code/error_message,
graph_post_id and prepared metadata. Config/draft changes never rewrite items.

## Internal interfaces

`service.Service(store,sources,signing_key,live_enabled=False,prepare_enabled=True)` implements
`handle(method,path,actor,payload=None,query=None)`. `DomainError` has code/status.
`core.Store(path,now_fn=...,daily_limit=1000)` owns transactions and schema.
Worker uses this same Store plus existing GPUPrepareClient/RequestsGraphTransport.
Internal POST paths `/internal/fb-video-publish/{prepare-next,link-next,execute-next,reconcile-next,tick}`.

Sources: `SourceRepository(page_repository,material_repository)`,
`list_pages(actor,q='')`, `get_pages(actor,page_ids)`,
`search_materials(query)`, `get_materials(material_ids,need_desc=False)`.
Page metadata: page_id,page_name,eligible_token_count,group_ids,language,blocked_reason.
Material metadata: material_id,material_name,content_id,language,drama_name,
drama_description,material_tag,media_url,duration_seconds.
All new source/Page/credential queries use `BrokerReadOnlyMySQL` through the root
API's authenticated loopback broker and the host FIFO SQL gate. The original
automatic repositories are unchanged. Browsing the fixed shared material pool
does not require attribution mapping; Page access and preflight/submit resolve
the actor's current unique mapping.

Templates: `validate_template(text)`, `render_template(template,material,short_url)`.
Links: `short_url(ledger_id)`, `build_attribution_url(context,ledger_id,created_at_utc)`,
`publish_link(root,ledger_id,long_url,owner_user='')`.
Attribution context keys page_id,page_name,material_id,material_name,content_id,
language,drama_name,tag,sub_user_id,task_id.

## Execution invariants

Task IDs and link ledger IDs, real expanded captions and snapshots are frozen in
one submission transaction. Async link publication/readback gates Meta execution.
Global real Page and Page/material gates span tenants and old/new task families.
Same Page's earlier unfinished task cannot be overtaken; unknown holds persist.
Within one batch, a positive Page interval also delays execution until at least
that many minutes after the latest earlier successful Page task was confirmed
published by Graph reconciliation. Failed/cancelled rows do not create an extra
interval; zero remains immediate after predecessor completion. Frozen planned
times and planned Beijing quota dates are never rewritten by this delay.
Claim completion requires the exact lease token. Preparation retries reuse the
same stable GPU job and recipe; stale Meta writes become unknown, never replay.
Preparation/link callbacks also require an unexpired lease. An expired result
cannot revive a stage even when no replacement worker has claimed it yet.
An attempt with `result_kind=started` is committed before each Graph POST. Only
explicit credential codes 10/102/190/200 may rotate, at most eight credentials;
unknown and empty success receipts stop immediately. A cancelled in-flight GPU
task retains its capacity lease until completion/expiry and can never publish.
Daily quota reserves planned Beijing dates separately from the automatic quota.
No actual Graph publication in tests.
