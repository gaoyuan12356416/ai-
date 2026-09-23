# YouTube manual short links

Approved 2026-09-23: add Generate short link immediately before Default description settings. Choose an eligible channel and a drama from the complete DramaWave catalog, then generate/copy. Repeated explicit generations allocate new links. Transport retries retain the operation UUID. No video preparation or publication is created.

Channels use the existing directory and fresh validate() before allocation. Actors come from the existing Cookie/module/navigation gate. Catalog keys are exact content_id + normalized language. Episode duplicates collapse; ambiguous names block submission. Existing material SQL is not a prerequisite.

Catalog inspection found ~35 million episode records and usable name/content_id indexes. Empty dialog does not scan; search accepts >=2 characters of title prefix or an exact content ID, with 50-item pages. Literal percent/underscore are escaped. Runtime uses existing 63350 SQL Gate, 8-second query/25-second subprocess limits, max two reads.

Manual context is frozen separately in youtube_manual_short_link. Global IDs come from drama_material_short_link with material_kind=youtube_manual. Immutable filesystem publication must read back matching bytes before returning a URL. Eight attribution fields retain channel and email -> unique sub_user_id; manual namespace replaces nonexistent task/material/publication IDs. Historical links and video workflows stay unchanged.

No history page or daily-report changes in this release. Manual links do not count as video publications. New SQLite table is additive; no external MySQL writes.
