"""Single-run clone of the audit table for HK QA over the normal shared SQL budget."""
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import threading
import time
import uuid

from .audit import COLS, MySQLSink, TABLE, _row
from .ledger import digest, encoded


def qa_name(value):
    name = str(value)
    if name.startswith("ads_ai."):
        name = name[7:]
    if not re.fullmatch(r"fb_page_assignment_qa_[0-9]{8}(?:_[a-z0-9]{1,16})?", name):
        raise ValueError("invalid_isolated_qa_table_name")
    return name


@contextmanager
def run_lock(root):
    # Only used for a real HK run. Never remove a table while this QA service is accepting work.
    import fcntl
    with (Path(root)/"qa-mysql.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("QA service or cleanup is still using this instance") from None
        yield


def save_manifest(path, data):
    temp = path.with_name(path.name+"."+uuid.uuid4().hex)
    fd = os.open(str(temp), os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    with os.fdopen(fd,"w",encoding="utf8") as f:
        f.write(encoded(data))
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp,path)


class QATable(MySQLSink):
    def __init__(self, root, name, connection=None):
        super().__init__(connection)
        self.budget_status = None
        if connection is None:
            from features.hk_sql_budget.ledger import local_ledger
            self.budget_status = local_ledger().status
        self.foreground_busy = lambda: False
        self._overlap_lock = threading.Lock()
        self._last_budget_probe = 0.
        self._overlap = {"before_write":0, "readback":0, "samples":0, "read_and_write":0,
                         "read_and_write_during_foreground":0, "peak_reads":0, "peak_used":0}
        self.root = Path(root)
        self.name = qa_name(name)
        self.table = "\x60ads_ai\x60.\x60" + self.name + "\x60"
        self.instance = uuid.UUID((self.root/"qa-instance").read_text().strip()).hex
        self.manifest = self.root/"qa-mysql-target.json"
        self.marker = digest(("fb-page-qa/table-owner/"+self.name).encode())
        self.identity = None
        self._stats_lock = threading.Lock()
        self._stats = None
        self._sampled = (None, 0.)

    def _load(self):
        if not self.manifest.is_file():
            return None
        saved = json.loads(self.manifest.read_text(encoding="utf8"))
        if saved["table"] != self.name or saved["instance"] != self.instance:
            raise ValueError("this QA directory already belongs to another table")
        return saved

    def _exists(self, c):
        c.execute("SELECT table_type,engine FROM information_schema.tables WHERE "
                  "table_schema='ads_ai' AND table_name=%s",(self.name,))
        meta = c.fetchone()
        if meta and tuple(meta) != ("BASE TABLE","InnoDB"):
            raise ValueError("QA target must be an InnoDB base table")
        return bool(meta)

    @staticmethod
    def _schema_hash(c, table):
        c.execute("SHOW CREATE TABLE "+table)
        ddl = c.fetchone()[1]
        return digest(ddl[ddl.index("("):].encode())

    def _marker_value(self, c, lock=False):
        c.execute("SELECT diagnostics_json,payload_hash FROM "+self.table+
                  " WHERE task_id=%s AND item_index=0 AND external_task_id='__v2_qa_instance__'"+
                  (" LOCK IN SHARE MODE" if lock else ""), (self.marker,))
        return c.fetchone()

    def _marker_row(self):
        return _row(self.marker, 0, "__v2_qa_instance__", 0, 0, None,None,
                    "qa_instance","frozen","",self.identity)

    def _verify_owner(self, c, lock=False):
        marker = self._marker_row()
        actual = self._marker_value(c,lock)
        if actual is None or tuple(actual) != (marker[10],marker[12]):
            raise ValueError("QA table owner or identity differs")

    def prepare(self, *, claim_empty=False):
        saved = self._load()
        if saved and saved.get("state") == "dropped":
            raise ValueError("choose a new QA instance for a new run")
        if not saved and (self.root/"state"/"assignment-v2.sqlite3").exists():
            raise ValueError("use a fresh QA directory when changing audit targets")
        with self.connection(role="write",deadline=time.monotonic()+25) as conn, conn.cursor() as c:
            existed = self._exists(c)
            if not existed:
                if saved and saved["state"]=="active":
                    raise ValueError("previously confirmed QA table is missing")
                c.execute("CREATE TABLE "+self.table+" LIKE "+TABLE)
            sha = self._schema_hash(c,self.table)
            if sha != self._schema_hash(c,TABLE):
                raise ValueError("QA table differs from the production audit schema")
            c.execute("SELECT @@server_uuid")
            server = str(c.fetchone()[0])
            self.identity = {"instance":self.instance,"schema":"ads_ai","table":self.name,
                             "schema_sha256":sha,"server_uuid":server}
            if saved and any(saved.get(k)!=v for k,v in self.identity.items()):
                raise ValueError("server, table or schema differs from the saved QA identity")
            if self._marker_value(c) is None:
                # LIKE doesn't copy triggers. Also check any independently provisioned empty table.
                c.execute("SELECT COUNT(*) FROM information_schema.triggers WHERE "
                          "event_object_schema='ads_ai' AND event_object_table=%s",(self.name,))
                if c.fetchone()[0]:
                    raise ValueError("QA table must have no triggers")
                c.execute("SELECT 1 FROM "+self.table+" LIMIT 1")
                if c.fetchone() or (existed and (saved or not claim_empty)):
                    raise ValueError("existing QA table is not owned by this run")
                row = self._marker_row()
                c.execute("INSERT INTO "+self.table+" ("+",".join(COLS)+") VALUES ("+
                          ",".join(["%s"]*len(COLS))+")",row)
                conn.commit()
            self._verify_owner(c)
        save_manifest(self.manifest,{**self.identity,"state":"active"})
        return self

    def _read(self, conn, expected, lock=False):
        # Hold the table's normal transaction metadata lock through each data operation.
        # Multiple writers can coexist on the immutable owner row.
        with conn.cursor() as c:
            self._verify_owner(c,lock=True)
        with self._overlap_lock:
            self._overlap["readback" if lock else "before_write"] += 1
            now = time.monotonic()
            take_sample = self.budget_status is not None and now-self._last_budget_probe>=.15
            if take_sample:
                self._last_budget_probe = now
        if take_sample:
            status = self.budget_status()
            reads = status["roles"].get("read",0)
            with self._overlap_lock:
                self._overlap["samples"] += 1
                self._overlap["read_and_write"] += int(reads>0)
                self._overlap["read_and_write_during_foreground"] += int(reads>0 and self.foreground_busy())
                self._overlap["peak_reads"] = max(reads,self._overlap["peak_reads"])
                self._overlap["peak_used"] = max(status["in_use"],self._overlap["peak_used"])
        return super()._read(conn,expected,lock)

    def overlap(self):
        with self._overlap_lock:
            return dict(self._overlap)

    def counts(self, confirmed, pending):
        # Sample the actual isolated table under the common 30-connection budget.
        # A drained queue with a new confirmed count always receives a fresh sample.
        with self._stats_lock:
            now = time.monotonic()
            count, sampled = self._sampled
            if self._stats is not None and now-sampled < 10 and (pending or confirmed==count):
                return self._stats
            with self.connection(role="read",deadline=now+6) as conn,conn.cursor() as c:
                self._verify_owner(c)
                c.execute("SELECT selection_stage,error_code,item_index=0,page_id IS NULL,count(*) FROM "+
                          self.table+" WHERE task_id<>%s GROUP BY selection_stage,error_code,item_index=0,page_id IS NULL",
                          (self.marker,))
                stages, causes = {}, {}
                headers = items = 0
                for stage, error, header, no_page, total in c.fetchall():
                    stages[stage] = stages.get(stage,0)+total
                    if header:
                        headers += total if stage=="v2_request" else 0
                    else:
                        items += total
                        if no_page:
                            causes[error] = causes.get(error,0)+total
            self._sampled = confirmed, now
            self._stats = (headers,items,stages,causes)
            return self._stats

    def drop(self, instance):
        saved = self._load()
        if not saved or uuid.UUID(instance).hex != self.instance:
            raise ValueError("supply the exact recorded QA instance")
        self.identity = {k:v for k,v in saved.items() if k!="state"}
        with self.connection(role="write",deadline=time.monotonic()+20) as conn, conn.cursor() as c:
            c.execute("SELECT @@server_uuid")
            if str(c.fetchone()[0])!=saved["server_uuid"]:
                raise ValueError("cleanup server differs from recorded QA server")
            if self._exists(c):
                if saved["state"]=="dropped":
                    raise ValueError("a table was recreated after this QA run")
                if self._schema_hash(c,self.table)!=saved["schema_sha256"]:
                    raise ValueError("cleanup table schema differs")
                self._verify_owner(c)
                c.execute("DROP TABLE "+self.table)
            if self._exists(c):
                raise ValueError("QA table still present")
        save_manifest(self.manifest,{**self.identity,"state":"dropped"})
        return {**self.identity,"state":"dropped"}
