"""Page-owned language routing, independent of legacy template language."""
from .validation import LANGUAGE_ALIASES,LANGUAGE_RE
from .repositories import CandidateSnapshot


def page_language(value):
    text=str(value or '').strip().lower().replace('_','-')
    text=LANGUAGE_ALIASES.get(text,text)
    return text if LANGUAGE_RE.fullmatch(text) else ''


def candidate_snapshots_for_pages(config,pages,materials):
    result={}
    for language in sorted({page_language(page.language) for page in pages}-{''}):
        snapshot=materials.candidate_snapshot({**config,'language':language})
        # Defend against a stale or incorrectly scoped adapter response too.
        candidates=tuple(item for item in snapshot.candidates if page_language(item.language)==language)
        result[language]=CandidateSnapshot(candidates,snapshot.metric_generation_ids,snapshot.metric_dates)
    return result
