# Cancel an unavailable unfinished X drama owner on schedule save

Removing an account now prompts for an explicit restart confirmation. The PUT
adds restart_removed_dramas=true; the existing internal endpoint passes settings
through unchanged. Only owners removed from the saved configuration are eligible,
and only persisted authorization/approval failures, X account locks or verification
rejections permit cancellation. Healthy unfinished owners remain required.

The same SQLite transaction validates each current generation, rejects queued,
reserved, publishing, relay and unknown outcomes, verifies complete contiguous
success receipts and definite failures, writes x_post_drama_cancel_audit and
increments replay_generation. It then clears the binding, resets progress to
first episode, prioritizes the drama, and saves schedule version atomically.
Existing queues, links and receipts are never removed or rewritten. The old
queue generation fence prevents later recovery from publishing a cancelled run.
No eligible same-language account means the drama remains pending.

The cancellation audit mirrors the immutable replay audit but accepts zero
published episodes. Assignment/progress triggers accept either exact audit source.
Legacy replay restrictions are unchanged. The full store suite and UI syntax
must pass, then rehearse on an online backup of current accounts.sqlite3 without
sending or modifying the original database.

Deploy: push GitHub commit; fetch and verify exact commit; run
scripts/deploy_x_drama_cancel.py prepare; validate composed release; drain X only;
switch using existing guarded deployer. It backs up DB and touched files, preserves
all live composite modules and running work, restarts only X sidecar dependencies,
verifies health, and restores original timer/channel settings. If draining cannot
finish promptly, abort it and retain the staged release. No pending task is counted
as a successful post. Rollback uses the same deployer rollback/drain/switch, restores
only code, never old databases/tokens, and retains the cancellation audit table.
