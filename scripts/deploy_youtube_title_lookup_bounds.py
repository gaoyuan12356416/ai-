"""Bound title lookup to the current page; reuse the hash guarded installer."""
import deploy_youtube_display_metadata as display
import deploy_youtube_channel_templates as common

EXPECTED = {'features/youtube_analytics/titles.py': '0521d15f587936d42da2430616644c392bd94db0f987d546ced37d2056d32ec1', 'features/youtube_analytics/routes.py': 'ea8592dcfef5e2d67c282e32e810a983a9fa077af60b70ba78878d5d576da0e1'}

if __name__ == '__main__':
    display.EXPECTED = EXPECTED
    common.prepare = display.prepare
    common.main()
