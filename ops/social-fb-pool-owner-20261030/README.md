# Facebook pool contribution ownership

The social report used the configured group-pool publishing operator as the
contributor. A material submitted by another user therefore appeared as zero
for that user while the operator received its publications and effects.

For group and Page pools, use the exact bound item's `added_by` for reporting.
Validate task, tenant, batch, material and the frozen pool/run reservation;
Page pools additionally match Page/config/item. Freeze this evidence in the
separate report-owned `social_facebook_pool_owner` table. Existing immutable
publisher identities, URLs, campaign IDs and publication facts remain intact.
Ambiguous or changed evidence stays unassigned and visible as incomplete.
Manual/Excel and legacy automatic-template attribution are unchanged.

The overlay applies equally to counts, performance and bill-based financials
because each uses the shared catalog. Only confirmed published facts count;
failed and unknown tasks retain their states. UTC reporting dates are unchanged.

Deploy the exact pushed GitHub branch into an isolated source checkout, then:

```
python3 ops/social-fb-pool-owner-20261030/deploy.py stage
# Run the isolated source/ledger canary and save canary.json.
python3 ops/social-fb-pool-owner-20261030/deploy.py apply
bash /root/drama_material_service/scripts/safe_restart_drama_api.sh
```

The manifest is `/mnt/data-disk/social-fb-pool-owner-20261030/deployment.json`.
Only the shared report adapter and daily-report release pointer change. Keep
publisher workers, schedules, source databases and historical sent cards intact.
The original sent report is a frozen snapshot; create a separate local preview
for verification, without sending or editing a Feishu message.

Rollback from this source checkout:

```
python3 ops/social-fb-pool-owner-20261030/deploy.py rollback
bash /root/drama_material_service/scripts/safe_restart_drama_api.sh
```

Do not restore either source or reporting databases. The additive pool evidence
can remain; old code ignores it. Code rollback restores the former wrong owner
display and is only an operational fallback.
