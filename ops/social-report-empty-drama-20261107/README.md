# Social daily report: manual drama display labels

A manual FB source may store an empty drama_name while the frozen attribution
URL contains "none". The publisher also replaces asterisks with spaces and
bounds this display label at 512 characters. Match that exact transformation
without weakening task/Page/material/user/tenant/link/publication identity.

Before deployment run the Facebook attribution, daily report, billing and
backend scope suites. Push this branch, then use deploy.py stage and canary.
The canary must preserve every frozen owner and publication fact, recover only
the identified unresolved record, and leave publisher data/history unchanged.
Preview against the canary, require complete data, then deploy.py apply.
Run the existing collector and daily service; existing delivery keys prevent
duplicate messages. Verify actual Feishu receipts. No publisher restart.

Rollback: python3 /mnt/data-disk/social-report-repair-20261107/source/ops/social-report-empty-drama-20261107/deploy.py rollback
This restores only the code pointer, never publisher/delivery databases.
