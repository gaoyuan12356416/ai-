"""Choose the Page's preferred drama before applying its material rules."""
from collections import defaultdict
from features.x_tag_revenue.core import language as profile_language

from .effects import choose_material
from .strategy import diverse_candidates


def metadata_for(item, classifications):
    return classifications.get((item.content_id, profile_language(item.language)), {})


def retain_profile_candidates(candidates, classifications, limit, per_drama):
    """Keep original order and an independent top-N for each equal-rank class."""
    groups = defaultdict(list)
    for item in candidates:
        metadata = metadata_for(item, classifications)
        key = (metadata.get('audience', 'unmarked'), tuple(sorted(metadata.get('themes') or [])))
        groups[key].append(item)
    retained = {item.material_id for group in groups.values()
                for item in diverse_candidates(group, limit, per_drama)}
    return [item for item in candidates if item.material_id in retained]


def choose_profile_material(candidates, excluded, *, decision, snapshot, candidate_limit, **kwargs):
    classifications = snapshot.profile_classifications
    fallback = snapshot.profile_error
    config = kwargs['config']
    if str(config.get('app_id')) != '1479':
        fallback = 'unsupported_profile_product'
    if decision.reason or fallback:
        # Recreate the original global top-N when this Page cannot use the
        # expanded profile buckets (another Page in the batch still may).
        original = diverse_candidates(candidates, candidate_limit,
                                      20 if int(config.get('drama_cooldown_hours', 0)) else 0)
        material, evidence = choose_material(original, excluded, **kwargs)
    else:
        blocked = {str(value) for value in excluded}
        available = [item for item in candidates if item.material_id not in blocked]
        if available:
            # Stable ties use the configured drama/material order. Restrict to
            # one drama so material randomization and feedback cannot undo the
            # Page's audience/theme choice.
            first = min(available, key=lambda item: decision.rank(metadata_for(item, classifications)))
            drama = [item for item in available if item.content_id == first.content_id]
            material, evidence = choose_material(drama, (), **kwargs)
        else:
            material, evidence = choose_material([], (), **kwargs)
    audit = decision.audit(metadata_for(material, classifications) if material else None,
                           fallback=fallback or ('' if material else 'no_eligible_material'))
    audit['selected_content_id'] = material.content_id if material else ''
    audit['selected_material_id'] = material.material_id if material else ''
    return material, dict(evidence, tag_profile=audit)
