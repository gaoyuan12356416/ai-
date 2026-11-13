"""Shared account-scope bound for pool schedules and their publishers."""

# Keep account scopes consistent from settings through frozen-plan execution.
# 200 accounts also fit the existing two-scope queries below 999 SQLite binds.
MAX_PUBLISH_ACCOUNTS = 200
