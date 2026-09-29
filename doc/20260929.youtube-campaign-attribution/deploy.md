# Deployment and rollback

Production: 43.166.187.96:/root/drama_material_service. Source branch codex/youtube-campaign-attribution-20260929. Runtime target only features/youtube_analytics/report.py. Required previous SHA256 f23fbdbcfab566782a41f05e0bb802e8b35c027e345750a8f7324b2ef2bd56ae.

Push validated commit, fetch it into a clean release under /mnt/data-disk/deploy/youtube-auto-publish/releases. Run python3 scripts/deploy_youtube_campaign_attribution.py --check, then run without --check. The deployer verifies the data mount, checks live hash, backs up the file and manifest, atomically installs, restarts API and verifies health. On restart/health failure it restores the saved file. No database backup restoration or publisher restart.

Rollback: python3 <exact-release>/scripts/deploy_youtube_campaign_attribution.py --rollback <returned-backup>. Later file drift refuses rollback. Final release hash, backup path and actual verification are recorded in deployment-evidence.md after deployment.
