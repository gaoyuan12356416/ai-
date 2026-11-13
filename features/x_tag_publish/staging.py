"""Build an isolated runtime from a hash-verified production source snapshot.

No transformation in this module writes to the existing runtime or business DB.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil

REGRESSION_FILES = ('test_x_auto_post_runner.py', 'test_x_auto_post_store.py',
                    'test_x_auto_post_metrics.py', 'test_x_post_manual_runner.py',
                    'test_x_post_manual_sidecar.py', 'test_x_post_schedule_runner.py')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace_once(path, old, new):
    value = path.read_text(encoding='utf-8')
    if value.count(old) != 1:
        raise RuntimeError('source_anchor_changed:' + path.name)
    path.write_text(value.replace(old, new, 1), encoding='utf-8')


def build(source, repo, target, *, verify=True):
    source, repo, target = map(Path, (source, repo, target))
    manifest = json.loads((repo / 'deploy/x-tag-publish-baseline.json').read_text(encoding='utf-8'))
    if verify:
        for name, sha in manifest.items():
            if digest(source / name) != sha:
                raise RuntimeError('runtime_baseline_changed:' + name)
    if target.exists():
        raise RuntimeError('staging_target_must_be_new')
    target.mkdir(parents=True)
    for directory in ('features', 'scripts', 'static'):
        shutil.copytree(source / directory, target / directory, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Maintain regression contracts in Git, not in the immutable production
    # snapshot. Apply the same lane namespace substitutions to these fixtures.
    for name in REGRESSION_FILES:
        shutil.copy2(repo / 'scripts' / name, target / 'scripts' / name)
    (target / 'deploy').mkdir()
    shutil.copy2(repo / 'deploy/x-post-schedule.env.example', target / 'deploy/x-post-schedule.env.example')
    for module in ('x_tag_publish', 'x_tag_revenue', 'social_analytics', 'youtube_analytics'):
        shutil.copytree(repo / 'features' / module, target / 'features' / module,
                        dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (target / 'features/__init__.py').touch()
    # Fixed loopback address validation stays strict, with a new fixed address.
    for name in ('features/x_auto_posts/x_sidecar.py', 'features/x_auto_posts/service.py',
                 'scripts/x_auto_post_runner.py', 'scripts/x_post_daily_runner.py',
                 'scripts/x_post_schedule_runner.py', 'features/x_auto_posts/client.py',
                 'scripts/test_x_auto_post_runner.py'):
        p = target / name
        value = p.read_text(encoding='utf-8').replace('8810', '8821').replace('18833', '18843')
        p.write_text(value, encoding='utf-8')
    # Every runtime lock, working directory and default publishing ledger is separate.
    for directory in ('features/x_posts', 'features/x_auto_posts', 'scripts'):
        for p in (target / directory).glob('*.py'):
            if directory == 'scripts' and not p.name.startswith(('x_post_', 'x_auto_post_')) and p.name not in REGRESSION_FILES:
                continue
            value = p.read_text(encoding='utf-8')
            for old, new in (('/run/x-post-daily', '/run/x-tag-post'),
                             ('/run/x-auto-post', '/run/x-tag-auto'),
                             ('/mnt/data-disk/x-auto-post-publisher', '/mnt/data-disk/x-tag-publisher/auto'),
                             ('/mnt/data-disk/x-post-automation', '/mnt/data-disk/x-tag-publisher'),
                             ('https://gy.g2flow.com/s2l', 'https://gy.g2flow.com/s2l-tag')):
                value = value.replace(old, new)
            p.write_text(value, encoding='utf-8')
    replace_once(target / 'features/x_posts/service.py', 'parsed.path.rstrip("/") != "/s2l"',
                 'parsed.path.rstrip("/") != "/s2l-tag"')
    replace_once(target / 'features/x_posts/selector.py', '"tag": drama["labels"][0],',
                 '"tag": drama["labels"][0],\n            "drama_labels": list(drama["labels"]),')
    replace_once(target / 'features/x_posts/selector.py', '"tag": labels[0],',
                 '"tag": labels[0],\n            "drama_labels": list(labels),')
    service = target / 'features/x_auto_posts/service.py'
    replace_once(service, 'def _public_selection(value: Any) -> Any:\n',
                 'def _public_selection(value: Any) -> Any:\n    if isinstance(value, Mapping) and "policy_version" in value and "candidate_labels" in value:\n        from features.x_tag_revenue.tags import localize_audit\n        value = localize_audit(dict(value))\n')
    replace_once(service, '    store = XAutoPostStore(db_path)\n',
                 '    store = XAutoPostStore(db_path)\n    from features.x_tag_publish.policy import initialize_ids, lane_paths\n    lane_paths()\n    initialize_ids(db_path)\n')
    with service.open('a', encoding='utf-8') as f:
        f.write('\n# This import is installed only in the independent tag runtime.\nfrom features.x_tag_publish.selection import ProfileSelector as TwoStageSelector\n')
    # The executor rebuilds a selector around its claim-token store for each
    # task phase. Preserve the configured profile subclass at that boundary;
    # using the base class would silently bypass preferences and audit freezing.
    replace_once(target / 'features/x_auto_posts/publisher.py',
                 '        return TwoStageSelector(\n            self.selector.source,',
                 '        return type(self.selector)(\n            self.selector.source,')
    # Bind before the function definition: its default loader is evaluated then,
    # so replacing only the module global below the definition would retain FIFO.
    replace_once(target / 'scripts/x_post_schedule_runner.py', 'def execute_schedule_tick(',
                 'from features.x_tag_publish.pool import material_candidates as _material_candidates\n\ndef execute_schedule_tick(')
    # The new source starts empty; it never receives the old account list or schedule.
    for p in (source / 'static').glob('x-auto-publish*'):
        value = p.read_text(encoding='utf-8').replace('x-auto-publish', 'x-tag-auto-publish')
        value = value.replace('X Post 自动发布', 'X 标签画像自动发布').replace('X 自动发布', 'X 标签画像自动发布')
        value = value.replace('以及旧发布池或新系统已经出现的素材', '以及本画像流程已保留或发布过的素材（防重独立于原流程）')
        value = value.replace('按每个 X 账户设置的剧语言筛选和排序，再在入选剧中按素材规则选择。',
                              '按账号剧语言和模板条件筛选，再按剧类型、受众、题材三级收入画像选剧；同组合内按模板排序选择素材。')
        if p.suffix == '.html':
            value = value.replace('</head>', '<link rel="stylesheet" href="/x-tag-publish-ui.css?v=1" /></head>')
            value = value.replace('<script src="/x-tag-auto-publish-common.js', '<script src="/x-tag-publish-ui.js?v=20261109hierarchy1"></script>\n  <script src="/x-tag-auto-publish-common.js')
        (target / 'static' / p.name.replace('x-auto-publish', 'x-tag-auto-publish')).write_text(value, encoding='utf-8')
    p = source / 'static/x-post-material-pool.html'
    value = p.read_text(encoding='utf-8').replace('/api/admin/x-posts/', '/api/admin/x-tag-posts/')
    value = value.replace('x-post-material-pool', 'x-tag-material-pool').replace('/x-post-logs', '/x-tag-post-logs')
    value = value.replace('X Post 素材池', 'X 标签画像素材池').replace('X素材池', 'X标签画像素材池')
    value = value.replace('随机分配', '按频道标签画像分配').replace('最新素材优先', '类型 → 受众 → 题材优先，同组合内最新素材优先')
    value = value.replace('定时任务按加入时间从新到旧选择未发布且未占用的素材，并按语言匹配账号。',
                          '定时任务按频道的标签收入画像选择素材，语言需匹配；同标签内按加入时间从新到旧选择。本页的素材占用和发布量独立计算。')
    value = value.replace('重复或已被历史队列占用的素材不会再次加入。', '本流程内重复或已被本流程历史队列占用的素材不会再次加入。')
    value = re.sub(r'只有这里选中的账号才会从素材池取素材发布；.*?每个账号会在每个发布时间点各发布一篇。',
        '为所选频道按剧类型、受众、题材逐层分配素材，同组合内最新入池优先。语言、时长和文件大小按目标账号资格校验。每个账号在每个发布时间点各发布一篇，只计入本画像流程的发布量。', value, count=1)
    value = re.sub(r'<div class="manual-warning">.*?</div>',
        '<div class="manual-warning">手动发布按你明确选择的素材和账号执行，使用本流程独立账本。X 写入后可能部分成功、部分失败，结果未知的任务须先核对原回执；已发布的帖子不会因关闭页面而撤回。视频时长与文件大小按目标账号当前资格和实际媒体校验执行。</div>', value, count=1)
    value = value.replace('</head>', '<link rel="stylesheet" href="/x-tag-publish-ui.css?v=1" /></head>')
    value = value.replace('</body>', '<script src="/x-tag-publish-ui.js?v=20261109hierarchy1"></script></body>')
    (target / 'static/x-tag-material-pool.html').write_text(value, encoding='utf-8')
    replace_once(target / 'static/x-tag-auto-publish-runs.js', '      row.appendChild(selectionCell);',
                 '      selectionCell.appendChild(ui.element("div", {className:"secondary", text:window.TagPublishUI.explain(selection.tag_profile)}));\n      row.appendChild(selectionCell);')
    p = target / 'static/x-tag-auto-publish-templates.js'
    value = p.read_text(encoding='utf-8')
    value, preview_count = re.subn(
        r'  async function previewTemplate\(item\) \{.*?\n  \}\n\n(?=  async function mutateTemplate)',
        '  async function previewTemplate(item) {\n    await window.TagPublishUI.previewTemplate(item, ui);\n  }\n\n',
        value, flags=re.S)
    if preview_count != 1:
        raise RuntimeError('preview_picker_anchor_changed')
    p.write_text(value, encoding='utf-8')
    for p in (repo / 'static').glob('x-tag-publish-*'):
        shutil.copy2(p, target / 'static' / p.name)
    for p in (repo / 'static').glob('x-tag-post-logs.*'):
        shutil.copy2(p, target / 'static' / p.name)
    for p in (repo / 'static').glob('x-tag-revenue.*'):
        shutil.copy2(p, target / 'static' / p.name)
    # Shared navigation is maintained separately from the legacy runtime snapshot.
    shutil.copy2(repo / 'static/quick-nav.js', target / 'static/quick-nav.js')
    nav_version = digest(repo / 'static/quick-nav.js')[:12]
    for p in (target / 'static').glob('x-tag-*.html'):
        value = p.read_text(encoding='utf-8')
        value = re.sub(r'(/quick-nav\.js)(?:\?[^"\']*)?', r'\1?v=' + nav_version, value)
        for asset in ('x-tag-publish-ui.js', 'x-tag-post-logs.js', 'x-tag-revenue.js', 'x-tag-revenue.css', 'x-tag-auto-publish-templates.js'):
            version=digest(target / 'static' / asset)[:12]
            value=re.sub(r'(/'+re.escape(asset)+r')(?:\?[^"\']*)?',r'\1?v='+version,value)
        p.write_text(value, encoding='utf-8')
    # Later scoped releases own these files after the historical transformations.
    # This keeps rebuilds compatible with explicit material and future-time runs.
    hit_overlay = repo / 'deploy/hit-publish-overlay.json'
    if hit_overlay.exists():
        for name in json.loads(hit_overlay.read_text(encoding='utf-8'))['x']['files']:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / name, destination)
    (target / 'tag-runtime-manifest.json').write_text(json.dumps(dict(baseline=manifest,
        files={str(p.relative_to(target)).replace('\\', '/'): digest(p) for p in target.rglob('*') if p.is_file()}),
        indent=2), encoding='utf-8')
    return target
