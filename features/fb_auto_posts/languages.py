"""Page-owned language routing, independent of legacy template language."""
from concurrent.futures import ThreadPoolExecutor
from .validation import LANGUAGE_ALIASES,LANGUAGE_RE
from .repositories import CandidateSnapshot


def page_language(value):
    text=str(value or '').strip().lower().replace('_','-')
    text=LANGUAGE_ALIASES.get(text,text)
    return text if LANGUAGE_RE.fullmatch(text) else ''


def candidate_snapshots_for_pages(config,pages,materials):
    result={}
    languages=sorted({page_language(page.language) for page in pages}-{''})
    if not languages:return result
    # Each repository SELECT opens its own read-only connection. Bound catalog
    # fan-out independently of Page count so seven languages fit the plan lease.
    with ThreadPoolExecutor(max_workers=min(3,len(languages))) as pool:
        snapshots=list(pool.map(lambda language:materials.candidate_snapshot({**config,'language':language}),languages))
    for language,snapshot in zip(languages,snapshots):
        # Defend against a stale or incorrectly scoped adapter response too.
        candidates=tuple(item for item in snapshot.candidates if page_language(item.language)==language)
        result[language]=CandidateSnapshot(candidates,snapshot.metric_generation_ids,snapshot.metric_dates)
    return result
