# Meta cover four-hour group summary

The report now runs at 01:30, 05:30, 09:30, 13:30, 17:30 and 21:30 Asia/Shanghai, retaining the previous 09:30 anchor. Each run summarizes the rolling 24-hour half-open window ending at that scheduled cutoff, preserving the user's previous one-day reporting scope. There is no boot/catch-up trigger and scanner/worker batch completions stay silent.

Set these report-only runtime configuration fields before installing the new notification timer:

```json
{
  "cover_report_hours": 4,
  "cover_report_lookback_hours": 24,
  "cover_report_recipient": {
    "receive_id_type": "chat_id",
    "receive_id": "oc_b633b6fba2c78f43ef1ebacd314d518c"
  },
  "cover_report_feishu_config": "/mnt/data-disk/meta-violation-material-cleanup/feishu-cover-report.json"
}
```

The dedicated Feishu file contains the existing app credentials and the same recipient fields. Copy credentials privately from the current approved configuration; never print or commit them. Keep the existing shared `feishu_config` file and its original personal recipient unchanged. Missing report-only fields retain the previous daily/personal behavior, and old releases ignore these additive fields on rollback.

`cover_report_hours` controls frequency; `cover_report_lookback_hours` controls the statistics span and defaults to 24. Both accept 4 or 24 for compatibility, but production uses frequency 4 and lookback 24. `cover.py --config <runtime>/config.json notify --dry-run` previews the window without recording or sending. An explicitly authorized deployment acceptance can run `notify` once for the same completed cutoff. That cutoff's normal scheduled/manual repeat will not send again. Every cutoff has its own `cover-periodic-4h-YYYYMMDDTHHMM` run ID and deterministic notice UUID, independent of preserved `cover-daily-YYYYMMDD` and legacy batch notices. Consecutive reports intentionally overlap 20 hours; do not sum them into a day total.

The frozen summary includes its exact destination as well as data/body. Failed or pending sends cannot be rerouted by changing configuration. The notifier checks the dedicated file against the frozen destination before network I/O. Sent, sending and unknown outcomes are not replayed. Existing daily summaries without destination metadata remain bound to the original personal recipient. Multipart messages retain their existing per-part ledger and frozen text, with payload sizes measured for the actual recipient ID.

The report keeps outcome events in the rolling 24-hour event window separate from current queue snapshots and historical ledger latency. A new optional execution section reads persisted performance JSONL and aggregates source preparation, reference scan and successful worker timings by sample counts, plus successful replacements divided by run_once time including cleanup. It includes nonempty cycles completed in the reporting window; a cycle can begin before the window, so these success counts are explicitly distinct from event-window outcomes. Empty idle polls are excluded. Missing timing data is reported as no sample; malformed/unreadable telemetry is labelled as incomplete and does not stop the main outcome report.

Tests cover all six Beijing boundaries and UTC conversion, day rollover, half-open counts, frozen destination/config routing, same-window deduplication, preserved daily/unknown notices, weighted completed-cycle timing, and unchanged default personal notifications. Use:

```text
python -m unittest discover -s tests -p test_cover_daily.py
python -m unittest discover -s tests -p test_notifications.py
python -m unittest discover -s tests -p test_cover_cli.py
```

Deploy through the existing exact-GitHub-release backup and `install_cover_worker.py`/`install_cover_report.py` workflow. The deployment operator updates only these report settings and the dedicated private credentials file. Rollback restores prior code and notification units, keeps current ledgers/receipts, and leaves the original shared Feishu configuration available to the old daily release.
