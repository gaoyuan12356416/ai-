"""Import pre-reviewed clip/episode summaries by exact identity; never infer them from titles."""

import argparse
import json

from . import store
from .model import canonical_lang, product


def save(c, row):
    values = (
        product(row["product"]),
        str(row["content"]).strip(),
        canonical_lang(row["language"]),
        str(row["material_id"]).strip(),
        int(row.get("episode") or 0),
        str(row["source_ref"]).strip(),
        row["summary"].strip(),
    )
    if any(not v for i, v in enumerate(values) if i != 4) or values[4] < 0:
        raise ValueError("reviewed evidence requires exact identities and provenance")
    c.execute("INSERT OR REPLACE INTO reviewed_facts VALUES(?,?,?,?,?,?,?)", values)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl")
    args = parser.parse_args()
    with open(args.jsonl, encoding="utf-8-sig") as f, store.connection() as c:
        for line in f:
            if line.strip():
                save(c, json.loads(line))
