# Complete current material-family aliases

The read-only product-directory audit found MoboShort W2A apps 2430/2432/2471,
MoboReels W2A app 3442, and social Mobo app 3642 in addition to 2373/3360.
All seven current Mobo product identities use the existing AI Moboreels story
lookup. Product names remain the primary classification where readers expose
them, including the social alias. Attribution IDs remain unchanged.
DramaWave's existing W2A drama-double alias also uses drama catalog 1479.

This completes f4310f1315c08a499daf5dc69b5099bb98c2af12 after concurrent FB
query changes invalidated its original file fences. Fresh snapshots retain
those changes and the already-installed main/TT mapping work. The manifest
contains only the remaining catalog deltas. Exact file hashes fence the
baseline; activation also checks the full copied runtime tree.

Keep first-release admission drains held. After its activator exits, use
`adopt-drains --previous ORIGINAL_DEPLOYMENT` to transfer its three still-held
receipts and original scheduler baselines to this deployment. The operation
requires exact original ownership, disabled lanes and unchanged candidate
baselines; it does not change coordinator configuration. Pause TT normally.
Activate, verify and resume each group, reviewing the current config revision
before reopening each inherited lane. Never clear other deployment holds,
force task completion, or restore publishing databases.

FB waits for every active coordinator claim and local publication/preparation
except parked COS preparation records. For those records it checks the exact
cloud owner, absent active coordinator lease/external publication intent, and
the matching generation's intent/receipt (or queued lease-expired state).
The source records, leases, quarantines and cloud receipts remain untouched;
they are pending cloud reconciliation rather than local HTTP execution.
