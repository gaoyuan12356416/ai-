# FB material pool breakdown

The link-type group labeled every 2310 publication as manual. Use the already
verified, frozen pool provenance to expose `pool` (Page 素材池) and `group_pool`
(组素材池). Manual/Excel remains `manual`; legacy templates remain `auto_template`.
The common catalog supplies grouping, filters, CSV, links, publications and all
effect metrics. Classification is applied at read time, including historical
rows; no publisher or attribution database migration is needed.

Local validation: 122 tests, three Windows-only skips. Coverage includes frozen
history, exact pool source, aggregation conservation, CSV, filters and tenant/
owner visibility. Linux validation and production evidence follow deployment.

GitHub-first deployment uses `deploy.py stage`, `canary.py`, then `deploy.py apply`
from this directory in the exact pushed CPU checkout. Only `facebook.py` and
`report.py` change in the main API and a copied independent daily report release.
The daily report's other code differences are preserved. Then run:

```bash
bash /root/drama_material_service/scripts/safe_restart_drama_api.sh
```

State and code backups: `/mnt/data-disk/social-fb-pool-breakdown-20261030/`.
No changes to FB publishing workers, schedules, source records, immutable links,
or previously sent reports. No test Posts or report messages are sent.

Rollback retains all databases and restores only these code files and the prior
daily release pointer:

```bash
python3 /mnt/data-disk/social-fb-pool-breakdown-20261030/source/ops/social-fb-pool-breakdown-20261030/deploy.py rollback
bash /root/drama_material_service/scripts/safe_restart_drama_api.sh
```
