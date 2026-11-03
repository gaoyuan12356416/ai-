# X upload efficiency

Distributed node upload used one MiB per segment. Change to two MiB under the existing media-only 120 second timeout; keep Post and Repost at 30 seconds, exact request permits, rate limit cooldowns and unknown-result fences. The official limit guidance supports at most 5 MB: https://docs.x.com/x-api/media/quickstart/media-upload-chunked .

Back up and byte-match the deployed executor against its GitHub base before atomically replacing this single file with the fetched commit. Gracefully SIGTERM only publication child processes after checking parent PID; their handler finishes execute_claim and the durable receipt before exiting. Leave the supervisor and preparation workers running. Spawned replacement children import the new module. Never change chunk boundaries in an initialized upload or restart a node during a write.

Capacity change: X publish limit 8 to 12, global publish limit 10 to 14, using expected_version. Preserve other channels. Rollback those two fields only against the fresh config version; restore the saved executor file only if its current SHA matches this commit and then gracefully drain publication children. Never restore task databases or clear unknown fences.
