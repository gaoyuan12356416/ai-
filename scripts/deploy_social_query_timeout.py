#!/usr/bin/env python3
"""SHA-guarded, code-only deployment using the verified social web installer."""
from pathlib import Path
import deploy_social_web_scope as deploy

deploy.SOURCE = Path(__file__).resolve().parents[1]
deploy.BASELINE = {
    'features/social_analytics/routes.py': 'ecf000be55d33395b40092945b8a4412fc6e18f7fe4e6280f8e089ceda6735fa',
    'features/social_analytics/report.py': '4055fda2350e030f6416649063e2faaa1f0f4484270471dd34fe45c22e33c30e',
    'features/social_analytics/scoped_metrics.py': None,
}
deploy.UNCHANGED = {
    'features/social_analytics/adapters.py': 'b511ee11b12476fbe34912534c90c5f62b3bc07397360e0b9b7a97d4f35d3ac5',
    'features/social_analytics/backend_scope.py': '139912dc795b050d6974772e06698d08904a3e4b0bf5795f05c4dfe93dd79029',
    'static/social-analytics.js': 'bfe0a1bc79d172bc25f3ceb13a7dd8bd268d38ccd64782d6270567c945956fb3',
}
deploy.PUBLISHERS += ('youtube-auto-publish-worker.service', 'drama-youtube-unified-writer.service')


def targets():
    return {str(deploy.MAIN / relative): relative for relative in deploy.BASELINE}


deploy.targets = targets

if __name__ == '__main__':
    deploy.main()
