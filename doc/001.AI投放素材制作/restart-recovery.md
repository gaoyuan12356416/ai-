# Service Restart Recovery

The API startup path recovers in-flight work so production tasks do not stay stuck after a service restart.

## Covered Jobs

- Drama jobs: recovered by the external worker path or reconciled from public artifacts.
- Screenshot jobs: returned to queued state and resumed.
- Ad material tasks: recovered on API startup.

## Ad Material Recovery Rules

- `generating_demand` tasks are re-enqueued for demand generation.
- `generating_material` tasks first reuse any existing generation output JSON and ready asset rows.
- Only missing asset indexes are regenerated.
- Existing ready assets are preserved and are not remade unless their output is missing or unreadable.
