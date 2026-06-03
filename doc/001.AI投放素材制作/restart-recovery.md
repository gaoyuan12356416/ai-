# Service Restart Recovery

The API startup path recovers in-flight work so production tasks do not stay stuck after a service restart.

## Covered Jobs

- Drama jobs: recovered by the external worker path or reconciled from public artifacts.
- Screenshot jobs: returned to queued state and resumed.
- Ad material tasks: recovered on API startup.

## Drama Job Timing Rules

- Drama material jobs persist `finished_at` separately from `updated_at`.
- `finished_at` is set the first time a job enters `done`; later notification writes, cover repair, URL repair, or other maintenance updates may refresh `updated_at` but must not overwrite `finished_at`.
- If a retry or resume moves a job out of `done`, `finished_at` is cleared and will be set again only when the regenerated job completes.
- The UI elapsed time uses `active_finished_at` derived from `finished_at`, so post-completion repairs do not inflate "total elapsed" time.

## Screenshot Job Completion Order

- Screenshot jobs should not enter `done` immediately after image generation. The final AI source callback must complete first, then the API updates `finished_at`, `updated_at`, and the final `done` progress.
- If the callback fails, keep the job non-terminal until the failure handler marks it failed. Retrying the job can then reuse already generated public assets and retry the callback instead of silently showing completion before material-source ingestion.

## Ad Material Recovery Rules

- `generating_demand` tasks are re-enqueued for demand generation.
- `generating_material` tasks first reuse any existing generation output JSON and ready asset rows.
- Only missing asset indexes are regenerated.
- Existing ready assets are preserved and are not remade unless their output is missing or unreadable.
- `material_review` tasks with `regenerating` assets are also recovered. Output JSON is reused only when it is newer than the latest generation input and the asset regeneration marker; otherwise the interrupted asset indexes are regenerated so stale pre-rejection images are not restored.
