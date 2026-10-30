"""Replay an isolated real ledger; classification alone may change."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from features.social_analytics import facebook, report
from deploy import STATE, FILES, sha


def main():
    state = json.loads((STATE / 'deployment.json').read_text())
    spec = importlib.util.spec_from_file_location('previous_facebook', STATE / 'before' / FILES[0])
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)
    ledger = STATE / 'attribution.snapshot.sqlite3'
    checksum = sha(ledger)
    app = dict(SOCIAL_ANALYTICS_FACEBOOK_ATTRIBUTION_DB=str(ledger))
    old, errors_before, metadata_before = previous.read_facebook(app)
    new, errors_after, metadata_after = facebook.read_facebook(app)
    assert not errors_before and not errors_after
    assert metadata_before == metadata_after
    assert len(old) == len(new)
    restored = copy.deepcopy(new)
    changed = 0
    for before, after, reset in zip(old, new, restored):
        if after['link_type'] in {'pool', 'group_pool'}:
            assert before['link_type'] == 'manual'
            reset.update(link_type=before['link_type'], link_type_label=before['link_type_label'])
            changed += 1
        assert before == reset
    assert changed > 0 and sha(ledger) == checksum
    query = dict(start=['2026-10-29'], end=['2026-10-29'], platform=['facebook'], group_by=['link_type'])
    actor = dict(_system_report=True)
    # Real publications, with a deterministic metric on every identity to prove
    # aggregation conservation. Public HTTP verifies actual MySQL effects later.
    metrics = [dict(platform='facebook', site_id=row['site_id'], campaign_id=row['key'][0],
                    date='2026-10-29', clicks=1, views=2, installs=1, conversions=1,
                    revenue_cents=3, refund_cents=1) for row in new if row.get('publications')]
    def build(catalog, **filters):
        return report.summarize(catalog, metrics, actor, report.parameters(dict(query, **filters)), 'snapshot')
    before, after = build(old), build(new)
    assert before['totals'] == after['totals']
    groups = {row['link_type']: row['published_count'] for row in after['all_rows']}
    assert set(groups) == {'manual', 'auto_template', 'pool', 'group_pool'}
    for kind in groups:
        result = build(new, link_type=[kind])
        assert result['totals']['published_count'] == groups[kind]
        assert len(result['all_rows']) == 1
        assert result['all_rows'][0]['link_type_label'] in report.csv_export(result).decode('utf-8-sig')
    evidence = dict(status='complete', after=state['after'], identities=len(new),
                    reclassified=changed, published_counts=groups,
                    totals_conserved=True, ledger_unchanged=True)
    (STATE / 'canary.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence))


if __name__ == '__main__':
    main()
