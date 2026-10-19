"""Hash-guarded display-only deployment with existing backup/rollback installer."""
import sys
from pathlib import Path
import deploy_youtube_channel_templates as common

EXPECTED = {
    'features/youtube_auto_publish/service.py': '5deca1bcd60bead9ce39d2928ae9417b613bbb0a2eeaff79824a867af34b8df4',
    'features/youtube_auto_publish/channel_templates.py': '1b027939e6ee8b0da5c1d6885173f75bfb5754bc9bf3796b869e7504d2bfb609',
    'features/youtube_auto_publish/channel_languages.py': None,
    'features/youtube_analytics/titles.py': None,
    'features/youtube_analytics/report.py': '5143768de7d854e6049f5830b87a71362f16d0b4238a91e40fdd7f4fcd40ac17',
    'features/youtube_analytics/routes.py': 'ef8c639cfd8baad2969f6305db5c5c554526896202e4d0e9e08cf5ee88cf897f',
    'static/youtube-publish.js': 'fff5c25c260380f00072a0c47ad81775e26f35ad882cc3292d79c8a21ddafd28',
    'static/youtube-publish.html': '21ac7ba0d2a5b21fc92a91038e520b0713a6c13adf4aa66fdde7190d1b801134',
    'static/youtube-analytics.js': 'eb6103050312cf6fa3daa6a4449c0980a518f7f659f42210f612d32bb6b632bc',
    'static/youtube-analytics.html': '2d10c505efa927cc3dc80093465387ae042a52bb889ff84390c210f9ef7f225a',
    'static/youtube-channels.js': '209128d42efcd43913629d834f5e1c761db93723bcf704e5235125f7aa2da9f7',
    'static/youtube-channels.html': '5d6b00e02e2b15dcd60dd373894a1391f59fe88f0c3dcec9c3942d4fd4a86fc3',
}


def prepare(stage):
    plans = {}
    for rel, old in EXPECTED.items():
        data = (stage / rel).read_bytes()
        if rel.endswith('.py'):
            compile(data, rel, 'exec')
        targets = [common.ROOT / rel]
        if rel.startswith('static/'):
            targets.append(common.PUBLIC / Path(rel).name)
        for path in targets:
            if common.sha(path) != old:
                raise RuntimeError('Live file drift: ' + str(path))
            plans[path] = dict(data=data, old=old, new=common.digest(data),
                               mode=path.stat().st_mode & 0o777 if path.exists() else 0o644)
    return plans


if __name__ == '__main__':
    protected = {unit: common.run('systemctl','show',unit,'-p','MainPID','-p','ActiveState') for unit in
                 ('youtube-auto-publish-worker.service','drama-youtube-unified-writer.service')}
    common.prepare = prepare
    common.main()
    for unit, state in protected.items():
        if common.run('systemctl','show',unit,'-p','MainPID','-p','ActiveState') != state:
            raise RuntimeError('Publisher process changed: ' + unit)
