"""Provisional topic recommendations from measured posts, kept apart from mature policy."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from statistics import mean, median

from features.x_tag_revenue.core import language as canonical_language
from features.x_tag_revenue.taxonomy import THEMES, UNKNOWN

STAGE_HOURS = {'72h': 72, '24h': 24, '6h': 6}


def midrank(value, sorted_values):
    return 50 * (bisect_left(sorted_values, value) + bisect_right(sorted_values, value)) / len(sorted_values)


def balanced_score(channel_dramas):
    return mean(mean(median(scores) for scores in by_drama.values()) for by_drama in channel_dramas.values())


def estimate(samples, source):
    """Compare like-aged posts in the same language. Balance channels and dramas."""
    if len(samples) < 2:
        return None
    metrics_by_language = defaultdict(lambda: [[], []])
    metrics_by_channel = defaultdict(lambda: [[], []])
    titles_by_channel = defaultdict(set)
    for row in samples:
        titles_by_channel[row['aid']].add(row['drama'])
        for i, value in enumerate(row['values']):
            if value is not None:
                metrics_by_language[row['language']][i].append(value)
                metrics_by_channel[row['aid']][i].append(value)
    for groups in (metrics_by_language, metrics_by_channel):
        for distributions in groups.values():
            for values in distributions:
                values.sort()

    by_theme = defaultdict(list)
    reference_scores = defaultdict(lambda: defaultdict(list))
    for row in samples:
        reach, click_rate = row['values']
        reaches, rates = metrics_by_language[row['language']]
        # Compare within a channel when there is a varied local baseline. A big
        # account's typical reach is a poor target for a newly opened channel.
        own_reaches, own_rates = metrics_by_channel[row['aid']]
        if len(own_reaches) >= 4 and len(titles_by_channel[row['aid']]) >= 2:
            reaches = own_reaches
            if len(own_rates) >= 4:
                rates = own_rates
        score = midrank(reach, reaches)
        if click_rate is not None:
            score = .75 * score + .25 * midrank(click_rate, rates)
        reference_scores[row['aid']][row['drama']].append(score)
        for theme in row['themes']:
            by_theme[theme].append((row, score))

    topics = []
    baseline = balanced_score(reference_scores)
    for theme, evidence in by_theme.items():
        channel_dramas = defaultdict(lambda: defaultdict(list))
        dramas = set()
        positive_channels = set()
        positive_dramas = set()
        for row, score in evidence:
            channel_dramas[row['aid']][row['drama']].append(score)
            dramas.add(row['drama'])
            if row['values'][0] > 0:
                positive_channels.add(row['aid'])
                positive_dramas.add(row['drama'])
        required_channels = 1 if source == 'own' else 2
        if len(positive_dramas) < 2 or len(positive_channels) < required_channels:
            continue
        # A multi-tagged post splits its support among its distinct topics.
        # One repeatedly posted title cannot increase confidence indefinitely.
        credit = min(len(dramas), sum(1 / len(row['themes']) for row, _ in evidence))
        score = 50 + credit / (credit + 4) * (balanced_score(channel_dramas) - baseline)
        if score >= 50 - 1e-8:
            topics.append(dict(theme=theme, score=round(min(100, score), 2), posts=len(evidence),
                               accounts=len(channel_dramas), dramas=len(dramas)))
    topics.sort(key=lambda item: (-item['score'], -item['dramas'], -item['accounts'], item['theme']))
    topics = topics[:3]
    if not topics:
        return None
    # Weights express relative priority inside the provisional allocation.
    total = sum(topic['score'] for topic in topics)
    for topic in topics:
        topic['weight'] = round(topic['score'] / total, 6)
    themes = {topic['theme'] for topic in topics}
    supporting = [row for row in samples if themes.intersection(row['themes'])]
    return dict(source=source, recommendations=topics, posts=len(supporting),
                accounts=len({row['aid'] for row in supporting}), dramas=len({row['drama'] for row in supporting}),
                languages=sorted({row['language'] for row in supporting}))


class ColdStartEvidence:
    def __init__(self):
        self.by_catalog = defaultdict(list)
        self.catalog_accounts = defaultdict(set)
        self.by_language = defaultdict(list)
        self.by_account = defaultdict(list)
        self.catalogs = set()
        self.cache = {}

    def add(self, post, age, impressions):
        stage = post.get('stage')
        if stage not in STAGE_HOURS or not STAGE_HOURS[stage] <= age <= STAGE_HOURS[stage] + 6:
            return
        meta = post.get('metadata') or {}
        themes = sorted(set(meta.get('themes') or []) & (THEMES.keys() - {UNKNOWN}))
        language = canonical_language(post.get('language'))
        catalog = post.get('catalog')
        drama = post.get('content_id')
        if meta.get('catalog') and meta['catalog'] != catalog:
            return
        if (not themes or not language or not drama or catalog not in ('dramawave', 'moboreels')
                or type(impressions) is not int or impressions < 0):
            return
        clicks = post['metrics'].get('profile_clicks')
        click_rate = clicks / (impressions + 200) if type(clicks) is int and clicks >= 0 else None
        aid = int(post['account_id'])
        row = dict(aid=aid, drama=str(drama), language=language, themes=themes, values=(impressions, click_rate))
        self.by_catalog[catalog, stage].append(row)
        self.catalog_accounts[catalog, stage].add(aid)
        self.by_language[catalog, stage, language].append(row)
        self.by_account[catalog, stage, language, aid].append(row)
        self.catalogs.add(catalog)

    def peer_estimate(self, source, catalog, stage, language, aid):
        if source == 'language_peers':
            rows = self.by_language.get((catalog, stage, language), ())
            own = self.by_account.get((catalog, stage, language, aid))
        else:
            rows = self.by_catalog.get((catalog, stage), ())
            own = aid in self.catalog_accounts.get((catalog, stage), ())
        # Channels with no evidence in this group share the exact same estimate.
        key = (source, catalog, stage, language if source == 'language_peers' else None, aid if own else None)
        if key not in self.cache:
            peers = [r for r in rows if r['aid'] != aid] if own else rows
            self.cache[key] = estimate(peers, source)
        return self.cache[key]

    def profile(self, aid, target_language, mature_catalogs):
        language = canonical_language(target_language)
        by_catalog = {}
        observations = defaultdict(lambda: {'posts': 0, 'dramas': set()})
        for catalog in sorted(self.catalogs):
            for stage in STAGE_HOURS:
                own = self.by_account.get((catalog, stage, language, aid), ())
                if own:
                    observations[stage]['posts'] += len(own)
                    observations[stage]['dramas'].update((catalog, row['drama']) for row in own)
            if catalog in mature_catalogs:
                continue
            for source in ('own', 'language_peers', 'catalog_peers'):
                for stage in STAGE_HOURS:
                    if source == 'own':
                        group = estimate(self.by_account.get((catalog, stage, language, aid), ()), source)
                    else:
                        group = self.peer_estimate(source, catalog, stage, language, aid)
                    if group:
                        by_catalog[catalog] = dict(group, language=language, stage=stage)
                        break
                if catalog in by_catalog:
                    break
        return dict(by_catalog=by_catalog, own_observations={
            stage: dict(posts=row['posts'], dramas=len(row['dramas'])) for stage, row in observations.items()})
