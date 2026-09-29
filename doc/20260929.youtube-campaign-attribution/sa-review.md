# Design review

Accepted: build the ID index globally before tenant/owner filtering. The only storage alias is the known first 32 characters; no case folding, punctuation removal, name fallback or arbitrary prefix matching. Resolve aliases against all full IDs before recognizing any owner. Frozen dimensions are authoritative. Duplicate (date, source ID, source campaign) is still rejected because upstream queries already aggregate that grain.

Historical source metadata is not rewritten. Missing dates remain unknown. Global unknown source rows cannot be exposed to ordinary users without tenant evidence. Money is aggregated in integer cents.
