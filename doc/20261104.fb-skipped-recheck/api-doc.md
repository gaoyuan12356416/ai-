# Operator interface

No public HTTP endpoint added. scripts/recover_fb_skipped_runs.py --run-ids 316 317 --operation-id <32hex> --folder <mounted-data-disk-path> previews; --apply uses <=15min source snapshot and revalidates current Page scope, template, and reservations. Do not use an old recovery cache to retry an unknown publication.

Outputs: candidate-snapshot.json (internal source metadata, no tokens), preview.json, before-recovery.sqlite3, applied.json. Original skipped reason/time retained in selection_json.recovery. Production source credentials remain in memory.

CLI also accepts --candidate-limit 50..5000 (default1000), only as an operator catalog bound; source eligibility and all reservation checks remain. Preview queries catalogs only for remaining target Pages with eligible tokens and nonzero frequency. Language checkpoints retain successful results through transient SELECT failures; repeated same operation skips already recovered task IDs.
