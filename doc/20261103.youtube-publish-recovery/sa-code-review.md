# Code review

Credential acquisition moved ahead of native write counters; permanent auth failures still fail. Native ownership is rechecked after slow reads. Retry events persist in the existing audit table; old claims/operations/receipts remain. Re-admission requires failed center state, new retry revision and verified zero-attempt native state; unrelated channels and in-flight jobs excluded. Live node capability policy retained.
