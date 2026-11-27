# HotDrama classification repair

HotDrama income remains app 3302. Its classification may use DramaWave (1479)
only when both catalogs contain the exact content ID, normalized language and
one identical nonempty series_code. Conflicting or missing identities stay
unknown. HotDrama's own labels take precedence. Donor labels also require the
same resource series. An absent or unmapped theme remains unknown.

The normal portrait collector now includes corresponding DramaWave metadata
for HotDrama income titles. The V2 catalog exporter uses the same verified
mapping, including language-specific direction coverage. No assignment receipt
is replayed or cleared.

## Classification-only rebuild

`python3 -m features.fb_page_portrait.rebuild_classification prepare --work DIR`
freezes the published current-row portraits, validates every receipt hash and
financial total, reads missing donor metadata through the existing SQL FIFO,
and creates a disk-backed candidate. `publish --work DIR` uses the normal
staging, replica verification, atomic commit and selection-cache publisher.
Both steps share the portrait refresh lock. An interrupted publication resumes
from the same directory and existing durable publication receipt.

The new metadata retains the original income dates, social source timestamps,
financial collection identity and original income generation time. Only
classification is recalculated; the ordinary source freshness check remains.
Frozen classification inputs are hashed and retained next to the version so
the V2 exporter can reconstruct its classification catalog.

Deploy only the changed portrait modules over the current writer's complete
runtime, preserving its newer priority and current-row modules. Deploy the
exporter independently over its current runtime. Select these two oneshot
working directories with scoped systemd drop-ins; no API restart is required.
The deployment directory retains the original runtime hashes, unit definitions,
original frozen profiles, candidate, receipts and an executable rollback.
Code rollback removes only these deployment drop-ins, retaining committed
portraits and assignment business facts.

Validation: verified/mismatched/missing identity, product isolation, own-label
precedence, conflicting resources, language coverage, multi-theme allocation,
signed corrections, money and publication-count conservation, existing V2
catalog tests, server-side import checks, publication readback and offline
selection using the installed catalog. Network validation uses an exact
10.89.0.9 caller probe without sending API credentials through cloud commands.
