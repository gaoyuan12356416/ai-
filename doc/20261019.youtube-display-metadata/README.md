# YouTube channel language and English drama names

- Channel list: Edit template includes an optional channel language selector. The list and new-publication picker show the Chinese language label. Reopening the picker reads the latest saved value while OAuth checks retain their existing server cache.
- Language is tenant/channel metadata in the additive `youtube_channel_template.language` column. Old clients omitting it preserve the value; explicit empty clears it. Existing version CAS, ownership and permission checks remain. This is descriptive metadata, not a publication eligibility rule. No historical tasks are rewritten.
- Reports grouped by drama add English drama name immediately after the original drama column, including CSV. English titles are current metadata from `kunlunads_dev.ads_drama_info`, exact content ID to unique series code to unique `en` title, always `app_id=1479`. Ambiguous/missing/unavailable values are labelled, never guessed or translated.
- Title enrichment runs after existing attribution, permission filtering and aggregation. It cannot change financial totals, grouping or ownership. Read-only SQL on 63350 uses the existing FIFO gate, 20-ID batches, 8-second statement limit and bounded cache. No query when drama is not grouped.

## Validation and release

Run `PYTHONPATH=.:scripts python3 -m unittest tests.test_youtube_analytics tests.test_youtube_display_metadata scripts.test_youtube_channel_templates scripts.test_youtube_channel_cache scripts.test_youtube_channel_auth_retry scripts.test_youtube_channel_cover_deploy -q` and `node --check` on the three changed JS files.

Deploy only the manifest in `scripts/deploy_youtube_display_metadata.py` from the exact GitHub commit. `--check` validates live hashes. The installer backs up source and the online SQLite before replacing files, publishes static assets to both runtime and Nginx roots, and restarts only `drama-material-api.service`. Publisher/writer PIDs are checked. No test publication or messages.

Rollback: `python3 <release>/scripts/deploy_youtube_display_metadata.py --rollback <backup>`. It rejects later file drift and preserves the current SQLite, additive language field, templates, links, images and publishing facts. Do not restore the backup database.

The imported service/authentication/analytics baseline reflects separately deployed production patches; unrelated branch files must not be installed wholesale. Skill context is updated after release acceptance.
