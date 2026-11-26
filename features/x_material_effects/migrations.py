"""Preserve all auto-ledger rows while allowing explicitly governed reuse."""
def auto_ledger(db):
    columns=list(db.execute('PRAGMA table_info(x_auto_material_ledger)'))
    if not any(r[1]=='material_id' and r[5] for r in columns):return False
    if db.in_transaction:raise ValueError('migration_requires_transaction_boundary')
    definition=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='x_auto_material_ledger'").fetchone()[0]
    objects=[r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='x_auto_material_ledger' AND type IN ('trigger','index') AND sql IS NOT NULL")]
    definition=definition.replace('x_auto_material_ledger','x_effect_ledger_migration',1)
    definition=definition.replace('material_id TEXT PRIMARY KEY COLLATE BINARY','material_id TEXT NOT NULL COLLATE BINARY')
    definition=definition.replace('task_id INTEGER NOT NULL UNIQUE','task_id INTEGER PRIMARY KEY')
    expected=[tuple(r) for r in db.execute('SELECT * FROM x_auto_material_ledger ORDER BY task_id')]
    fk=db.execute('PRAGMA foreign_keys').fetchone()[0]
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE');db.execute(definition)
        db.execute('INSERT INTO x_effect_ledger_migration SELECT * FROM x_auto_material_ledger')
        actual=[tuple(r) for r in db.execute('SELECT * FROM x_effect_ledger_migration ORDER BY task_id')]
        if actual!=expected:raise ValueError('ledger_migration_changed_rows')
        db.execute('DROP TABLE x_auto_material_ledger')
        db.execute('ALTER TABLE x_effect_ledger_migration RENAME TO x_auto_material_ledger')
        for sql in objects:db.execute(sql)
        db.execute('CREATE INDEX IF NOT EXISTS idx_x_auto_material_identity ON x_auto_material_ledger(material_id,task_id)')
        if list(db.execute('PRAGMA foreign_key_check')):raise ValueError('ledger_foreign_key_check_failed')
        db.commit()
    except Exception:db.rollback();raise
    finally:db.execute('PRAGMA foreign_keys='+str(fk))
    return True
