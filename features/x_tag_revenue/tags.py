"""Versioned semantic aliases shared by revenue allocation and publishing.

No fuzzy matching, machine translation, or positional inference occurs at runtime.
Unknown/ambiguous labels retain separate identities and cannot drive preferences.
"""
import hashlib
import json
from pathlib import Path
import re
import unicodedata

DATA = json.loads(Path(__file__).with_name('tag_dictionary.json').read_text(encoding='utf-8'))
VERSION = DATA['version']
BY_ID = {row['id']: row for row in DATA['tags']}


def normalized(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip().casefold()


ALIASES = {}
for _row in BY_ID.values():
    for _alias in [_row['id'], _row['name_zh']] + _row['aliases']:
        _key = normalized(_alias)
        if _key in ALIASES and ALIASES[_key] != _row['id']:
            raise ValueError('conflicting_tag_alias')
        ALIASES[_key] = _row['id']
AMBIGUOUS = DATA['ambiguous_aliases']
AMBIGUOUS_IDS = {row['id']: row for row in AMBIGUOUS.values()}
AMBIGUOUS_NAMES = {normalized(row['name_zh']): row for row in AMBIGUOUS.values()}


def lookup(value):
    key = normalized(value)
    if key == '__unknown__':
        return dict(id=key, name_zh='未识别标签', status='missing', matchable=False)
    ambiguous = AMBIGUOUS.get(key) or AMBIGUOUS_IDS.get(key) or AMBIGUOUS_NAMES.get(key)
    if ambiguous:
        return dict(ambiguous, status='ambiguous', matchable=False)
    known = BY_ID.get(ALIASES.get(key, ''))
    if known:
        return dict(id=known['id'], name_zh=known['name_zh'], status=known['status'],
                    matchable=known['status'] == 'reviewed')
    digest = key[9:] if re.fullmatch(r'unmapped_[0-9a-f]{24}', key) else hashlib.sha256(key.encode()).hexdigest()[:24]
    return dict(id='unmapped_'+digest, name_zh='待映射标签（编号%s）' % int(digest[:8],16),
                status='unmapped', matchable=False)


def display(value):
    if re.fullmatch(r'待映射标签（编号[0-9]+）', str(value or '')):
        return value
    return lookup(value)['name_zh'] if value else ''


def localize_audit(value):
    """Only a response projection. Never mutate a frozen selection or its IDs."""
    if not isinstance(value, dict):
        return value
    if value.get('hierarchy_version'):
        # These names are frozen from the hierarchy's own controlled taxonomy.
        # Reinterpreting them through the old flat-label dictionary corrupts them.
        return dict(value)
    result = dict(value)
    for field in ('preferred_tag', 'selected_tag'):
        result[field] = display(value.get(field, ''))
    result['candidate_labels'] = [display(v) for v in value.get('candidate_labels', [])]
    result['weights'] = [dict(row, name=display(row.get('key') or row.get('name'))) for row in value.get('weights', [])]
    result['display_mapping_version'] = VERSION
    return result
