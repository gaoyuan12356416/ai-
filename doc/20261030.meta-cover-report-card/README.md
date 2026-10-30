# Meta cover summary card

The four-hour summary previously displayed a 1,044-character text report with
stage averages, P95, samples, lane timings and accounting notes mixed together.
New windows send a Feishu interactive card: replacement/deletion totals first,
current progress next, then four average timings in two rows. The 09:30 fixture
has roughly 360 visible characters. Scan/worker/measurement errors and an
unavailable or stopped worker remain visible. The complete previous text report
is available inside a closed details panel and in the frozen audit artifact.

Collection, scheduling, recipients, outcome accounting and execution behavior
are unchanged. New artifacts freeze both the card JSON and full text. Historical
artifacts retain their original delivery format. Card dispatch uses the existing
durable UUID and claim/receipt path; an uncertain send is never replayed or
automatically resent as text.

The component structure follows the official Feishu [collapsible panel docs](https://open.feishu.cn/document/feishu-cards/card-components/containers/collapsible-panel).
Collapsible panels require Feishu 7.9 or later. Essential results and warnings
are outside the panel.

## Validation

From `server-monitors/meta-violation-material-cleanup`:

```sh
python -m unittest discover -s tests
```

Local result: 740 tests, all passing, one platform-specific skip. Tests cover
card retries preserving frozen JSON/UUID, unknown outcomes without fallback,
historical text retry compatibility, visible warnings and collapsed timings.
The HTML approximation was checked visually, including expansion; it is not a
Feishu client screenshot. The original 10/30 09:30 snapshot supplies preview
numbers, not new or fabricated live results. No test message was sent.

## Narrow deployment

Fetch this branch from GitHub into the existing runtime repository, create an
immutable release worktree at the verified commit, and run server validation.
Stop only `meta-cover-notify.timer` briefly and confirm its oneshot is idle.
Back up `/etc/systemd/system/meta-cover-notify.service` and the previous service
state. Replace only its WorkingDirectory with the new release's module path,
then `systemctl daemon-reload` and restore the timer's previous active state.
Do not start the notify oneshot: the next normal window sends the new card.

This intentionally leaves `current` and the running worker/scan release at
`23b3d606c12ea15bc2c5104cf041e45648e1ef8e`. No worker restart, database restore,
config change, recipient change or extra message is required. A later full cover
deployment may replace the notify unit normally; there is no lingering drop-in.

Rollback: stop only the notify timer, ensure the notify oneshot is idle, restore
the backed-up unit, reload systemd and restore the timer's prior active state.
Preserve all current ledgers and historical frozen notices. Record the absolute
backup/release paths and commands in the deployment receipt.

Skill context remains unchanged; the implementation and rollback are recorded
here because this is a report presentation change, not a new execution rule.
