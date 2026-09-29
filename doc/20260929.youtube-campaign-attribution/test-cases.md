# Test cases

Regression cases live in tests/test_youtube_analytics.py. Synthetic data covers ID case sensitivity; changed/blank campaign name and raw channel; manual UUID truncation; same-prefix collisions; exact 32-character ID versus long alias; full long ID; identity conflicts across tenant/owner/drama/channel/language; filter isolation; sum of split source names; one-time link counts; duplicate source rejection; missing date semantics; report/CSV and SQL aggregation chain.

Live acceptance compares source totals to matched plus unmatched totals, checks each tenant and owner, then exercises options/report/CSV with existing authorized sessions (never prints credentials). Validate incident dates and default seven-day range after API restart. Preserve publisher worker PIDs and frozen link fingerprints.
