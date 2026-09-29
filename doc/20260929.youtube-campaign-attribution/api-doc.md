# API contract

Existing GET /api/youtube-analytics/options, /report and /export.csv retain permissions and query parameters. report.meta.attribution is unique_frozen_campaign_id_v2. quality.corrected_campaigns counts visible source ID/name combinations resolved through the stable campaign ID despite representation/name differences. Existing warnings explain frozen dimension use; no UI permission or route changes.

Raw channel is not an owner key. The stored long-link campaign name remains evidence only, not a join requirement.
