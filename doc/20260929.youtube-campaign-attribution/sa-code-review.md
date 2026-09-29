# Code review

Initial review: global candidate collection precedes all filters; names are not a disambiguator; no write-side or publication code is imported by analytics. Revenue aggregation retains integer cents and source grain duplicate detection. Alias money is emitted once, while creation counts are deduplicated by link ID. Deployment is restricted to one known Python file with drift checks and rollback. Final test/review status is recorded in test-report.md.

Final review: installer replacement and checksum validation are now inside the guarded recovery block. Recovery restores only the exact new hash and refuses unknown drift. QA fault-injection coverage passed 12/12; analytics passed 57/57.
