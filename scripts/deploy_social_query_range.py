#!/usr/bin/env python3
"""Deploy only the reviewed YouTube range-query planner."""
from pathlib import Path
import deploy_social_web_scope as deploy

deploy.SOURCE = Path(__file__).resolve().parents[1]
deploy.BASELINE = {
    'features/social_analytics/scoped_metrics.py': '36a830df22e473eacfd6ef629f593a328b233c0567120f09de1f37801807de67',
}
deploy.UNCHANGED = {
    'features/social_analytics/routes.py': '03b813cafffdbf2d5f327b61842990f85c228275fc58b0a8ffb2a13625dcbecc',
    'features/social_analytics/adapters.py': 'b511ee11b12476fbe34912534c90c5f62b3bc07397360e0b9b7a97d4f35d3ac5',
    'features/social_analytics/report.py': '69b1461d59d5138d63c1872908298e8fc3f64b15b58a3a30c212c9491a62b90d',
    'features/social_analytics/backend_scope.py': '139912dc795b050d6974772e06698d08904a3e4b0bf5795f05c4dfe93dd79029',
}
deploy.PUBLISHERS += ('youtube-auto-publish-worker.service', 'drama-youtube-unified-writer.service')
deploy.targets = lambda: {str(deploy.MAIN / relative): relative for relative in deploy.BASELINE}

if __name__ == '__main__':
    deploy.main()
