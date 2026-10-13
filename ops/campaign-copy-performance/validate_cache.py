"""Verify a separately warmed report against its seed without querying MySQL."""
import argparse
import hashlib
import json
from pathlib import Path


def validate(seed, candidate):
    assert candidate["v"] == 2 and candidate["m"]["read_only_verified"]
    dates = set(candidate["m"]["refreshed_dates"])
    assert dates and candidate["m"]["refreshed_through"] >= seed["m"]["refreshed_through"]
    campaign_fields = candidate["cf"]
    campaign_keys = {
        (row[campaign_fields.index("platform")], row[campaign_fields.index("campaign_id")])
        for row in candidate["c"]
    }
    entity_fields = candidate["xf"]
    entity_keys = {row[entity_fields.index("entity_key")] for row in candidate["x"]}
    assert len(campaign_keys) == len(candidate["c"])
    assert len(entity_keys) == len(candidate["x"])
    result = {}
    for fields_key, rows_key, key_fields, allowed in (
        ("df", "d", ("platform", "campaign_id"), campaign_keys),
        ("xdf", "xd", ("entity_key",), {(key,) for key in entity_keys}),
    ):
        fields = candidate[fields_key]
        assert fields == seed[fields_key]
        date_index = fields.index("dt")
        key_indices = [fields.index(field) for field in key_fields]
        def key(row):
            return tuple(row[index] for index in key_indices)
        current = {}
        for row in candidate[rows_key]:
            row_key = (*key(row), row[date_index])
            assert row_key not in current, ("duplicate", rows_key, row_key)
            assert key(row) in allowed, ("unbound", row_key)
            current[row_key] = row
        retained = 0
        for row in seed[rows_key]:
            if row[date_index] not in dates and key(row) in allowed:
                assert current.get((*key(row), row[date_index])) == row, ("history changed", key(row))
                retained += 1
        result[rows_key] = {"rows": len(current), "unchanged_history_rows": retained}
    result.update(campaigns=len(campaign_keys), extra_entities=len(entity_keys),
                  generated_at=candidate["m"]["generated_at"], stat_end=candidate["m"]["stat_end"],
                  refreshed_dates=sorted(dates), duplicate_keys=0)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("seed", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    seed = json.loads(args.seed.read_bytes())
    body = args.candidate.read_bytes()
    candidate = json.loads(body)
    print(json.dumps({"ok": True, "sha256": hashlib.sha256(body).hexdigest(), **validate(seed, candidate)}))
