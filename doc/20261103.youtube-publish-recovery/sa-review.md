# Design review

Accepted: obtain and verify credentials before native write intent. Retry only two transient credential-preflight codes, twice maximum; no platform upload/comment retry. Central re-admission requires an explicit native retry event and a zero-attempt, unowned, non-unknown queued task. Unknown and in-flight jobs remain fenced.

Deployment must preserve the live coordinator capability policy missing from the development branch. GPU worker upgrade is owned by the other ongoing task; observe it without changing that rollout.
