# Implementation

New manual_links.py contains catalog, URL construction, scoped reservation and immutable publication. manual_link_routes.py dispatches through the existing authenticated handler. The page adds one button and versioned standalone JS/CSS. Native dialog stays mounted and independent of task-list polling.

Validation: python -m unittest discover -s scripts -p 'test_youtube*.py'; node --check static/youtube-short-links.js; git diff --check. Browser: playwright-cli run-code --filename tests/youtube_manual_links_qa.js, plus existing channel-template, schedule and loading-race cases. Production only read-only search/channel/auth checks; no test video, comment, notification or manual link is generated there.
