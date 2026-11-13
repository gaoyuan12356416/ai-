"""Incremental task counters; installation and each transition are atomic."""

FIELDS = ("channel", "owner_tenant_key", "owner_user_id", "required_module", "stage", "status", "finished_at")
VERSION = 2
TRIGGERS = ("pipeline_monitor_count_insert", "pipeline_monitor_count_update", "pipeline_monitor_count_delete")


def install(connection):
    names = ",".join(FIELDS)
    connection.execute("SAVEPOINT pipeline_monitor_install")
    try:
        existed = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_monitor_counts'").fetchone()
        connection.execute("CREATE TABLE IF NOT EXISTS pipeline_monitor_counts (" + ",".join(name + " TEXT NOT NULL" for name in FIELDS) + ", n INTEGER NOT NULL, PRIMARY KEY(" + names + ")) WITHOUT ROWID")
        connection.execute("CREATE TABLE IF NOT EXISTS pipeline_monitor_snapshot_schema (id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)")
        version = connection.execute("SELECT version FROM pipeline_monitor_snapshot_schema WHERE id=1").fetchone()
        installed = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'pipeline_monitor_count_%'")}
        rebuild = not existed or not version or version[0] != VERSION or not set(TRIGGERS).issubset(installed)
        connection.execute("CREATE INDEX IF NOT EXISTS pipeline_monitor_counts_time ON pipeline_monitor_counts(channel,status,finished_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS pipeline_monitor_counts_owner_time ON pipeline_monitor_counts(owner_tenant_key,owner_user_id,channel,status,finished_at)")
        if rebuild:
            for trigger in TRIGGERS:
                connection.execute("DROP TRIGGER IF EXISTS " + trigger)
            connection.execute("DELETE FROM pipeline_monitor_counts")
            connection.execute("INSERT INTO pipeline_monitor_counts SELECT " + names + ",COUNT(*) FROM pipeline_job WHERE hold_reason<>'metadata_only' GROUP BY " + names)
            predicate = " AND ".join(name + "=OLD." + name for name in FIELDS)
            remove = "UPDATE pipeline_monitor_counts SET n=n-1 WHERE OLD.hold_reason<>'metadata_only' AND " + predicate + "; DELETE FROM pipeline_monitor_counts WHERE n=0 AND " + predicate + ";"
            insert = "INSERT INTO pipeline_monitor_counts(" + names + ",n) SELECT " + ",".join("NEW." + name for name in FIELDS) + ",1 WHERE NEW.hold_reason<>'metadata_only' ON CONFLICT(" + names + ") DO UPDATE SET n=n+1;"
            connection.execute("CREATE TRIGGER " + TRIGGERS[0] + " AFTER INSERT ON pipeline_job BEGIN " + insert + " END")
            connection.execute("CREATE TRIGGER " + TRIGGERS[1] + " AFTER UPDATE OF " + names + ",hold_reason ON pipeline_job BEGIN " + remove + insert + " END")
            connection.execute("CREATE TRIGGER " + TRIGGERS[2] + " AFTER DELETE ON pipeline_job BEGIN " + remove + " END")
            connection.execute("INSERT INTO pipeline_monitor_snapshot_schema VALUES(1,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version", (VERSION,))
        connection.execute("RELEASE SAVEPOINT pipeline_monitor_install")
    except BaseException:
        connection.execute("ROLLBACK TO SAVEPOINT pipeline_monitor_install")
        connection.execute("RELEASE SAVEPOINT pipeline_monitor_install")
        raise
