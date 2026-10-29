# Meta cover throughput and recovery

The continuously running worker could reduce its Graph concurrency from 32 to 1 without ever recovering, then prepare 64 tasks with one 30-second freshness deadline. Most tasks expired before writing and returned to the queue. Every cycle also slept 60 seconds despite ready work. A cooperative 09:55–10:05 legacy-job handoff could escape as InterruptedError and terminate the worker with no service restart.

The cover worker now enables the existing bounded gradual concurrency recovery, prepares only a current-capacity wave (at most two tasks per account), waits for global cooldown before preparing, and retains source freshness checks immediately before each write. Progressing cycles continue immediately, all-deferred cycles back off two seconds, and empty cycles idle 30 seconds. LegacyPriorityWindow is handled as a cooperative lock reacquisition. Other unexpected exits can restart after 15 seconds; durable write-pause failures return status 78 and stay stopped.

No definite failed mutation is retried and no unknown CREATE/bind/DELETE is replayed. Startup still recovers intents as unknown and reconciles read-only. Old ad deletion scheduling remains disabled. The daily notification remains at 09:30 Asia/Shanghai.

## Measurements

Each cycle stores a payload-free JSON line under runtime/performance/YYYYMMDD.jsonl, also emitted to the worker journal. The result includes counts of actual attempts and deferred reasons, wall time, and count/total/mean/max seconds for source preparation, preflight, Creative preparation/readback, binding/readback, reference scan, deletion, HTTP methods and shared governor waits. Successful Ad execution is measured separately from all attempts. Concurrent and nested phase times must not be summed as wall time; old ledger enqueue latency includes historical backlog and cannot be compared with new worker execution time as if they were the same metric.

The baseline ten cycles completed 98 replacements over 4,484.116 seconds inside run_once, including cleanup (78.68 successful replacements/hour). They also slept between cycles. Of 2,560 attempts, 2,445 returned pending (95.51%). New throughput comparisons must use the same run_once denominator, and separately report observed wall time including credentials, reconciliation and waits.

## Validation and deployment

Behavior coverage includes a reduced-capacity simulated slow run that completes eight tasks with fresh per-task source contexts, changing governor capacity and per-account admission, pressure recovery and enforced cooldown, idle decisions, typed priority handoff/resumption without a pause file, and nonrestartable write pauses. Existing cover/unknown/write-once and shared runtime regression tests remain required.

Commit/push, fetch the exact GitHub SHA into releases, then use deploy/install_cover.py prepare <release> --active-ledgers-only for consistent backup. With scan/worker/notify idle, deploy/install_cover_worker.py apply <backup> switches the code and worker unit without restarting the main API. Run work --once --max-ads 64 against naturally queued tasks, inspect receipts/metrics and fences, then systemctl enable --now meta-cover-worker.service. Capture pre/post counts and the bounded original pending cohort so waiting-review/failed/unknown rows are not represented as successfully cleared.

Rollback: stop and drain the worker, then run deploy/install_cover_worker.py rollback <backup>. This restores previous code/worker/notification units but leaves execution stopped and preserves all current ledgers and unknown outcomes. Live SHA, backup path, metrics and receipts belong to the deployment audit.
