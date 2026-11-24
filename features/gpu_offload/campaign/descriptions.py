"""Resume bounded descriptive lookups from an interrupted report refresh."""
from contextlib import closing, contextmanager
import datetime as dt
import sqlite3


@contextmanager
def open_descriptions(path, seed_version):
    with closing(sqlite3.connect(str(path), timeout=3)) as db:
        db.execute("PRAGMA cache_size=-2048")
        db.execute("""CREATE TABLE IF NOT EXISTS description (
            source TEXT, cid TEXT, signature TEXT NOT NULL, checked TEXT NOT NULL,
            name TEXT NOT NULL, product TEXT NOT NULL, countries TEXT NOT NULL,
            languages TEXT NOT NULL, PRIMARY KEY(source, cid)) WITHOUT ROWID""")
        db.execute("CREATE TABLE IF NOT EXISTS base (version TEXT PRIMARY KEY)")
        if db.execute("SELECT version FROM base").fetchone() != (seed_version,):
            with db:
                db.execute("DELETE FROM description")
                db.execute("DELETE FROM base")
                db.execute("INSERT INTO base VALUES (?)", (seed_version,))
        yield Descriptions(db)


class Descriptions:
    def __init__(self, db):
        self.db = db

    def restore(self, source, platform, ids, metadata, now, max_age):
        if not ids:
            return []
        remaining = set(ids)
        marks = ",".join("?" for _ in ids)
        rows = self.db.execute(
            "SELECT cid,signature,checked,name,product,countries,languages FROM description "
            f"WHERE source=? AND cid IN ({marks})", [source, *ids])
        for cid, signature, checked, name, product, countries, languages in rows:
            item = metadata[platform, cid]
            if signature != item["metadata_signature"] or not dt.timedelta(0) <= now - dt.datetime.fromisoformat(checked) < max_age:
                continue
            item.update(campaign_name=name, product=product, countries=countries,
                        languages=languages, metadata_checked_at=checked)
            remaining.remove(cid)
        return [cid for cid in ids if cid in remaining]

    def save(self, source, platform, ids, metadata):
        def rows():
            for cid in ids:
                item = metadata[platform, cid]
                yield (source, cid, item["metadata_signature"], item["metadata_checked_at"],
                       item["campaign_name"], item["product"], item["countries"], item["languages"])
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO description VALUES (?,?,?,?,?,?,?,?)", rows())
