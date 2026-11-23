"""Exchange bounded session proofs with the one existing SQL watchdog."""
import json
import os
from pathlib import Path
import tempfile
import time

REQUESTS=Path('/run/sql-query-watchdog/offload-requests.json')
PROOFS=Path('/var/lib/sql-query-watchdog/offload-reconciliations.json')


def atomic_json(path,value,mode=0o600):
    path=Path(path)
    fd,name=tempfile.mkstemp(prefix='.offload-',dir=str(path.parent))
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,separators=(',',':'));out.flush();os.fsync(out.fileno())
        os.replace(name,str(path))
    finally:
        if os.path.exists(name):os.unlink(name)


class GateReconciliation:
    def __init__(self,book,requests=REQUESTS,proofs=PROOFS):
        self.book,self.requests,self.proofs=book,Path(requests),Path(proofs)
        self.signature=None

    def tick(self):
        if self.proofs.is_file():
            payload=json.loads(self.proofs.read_text())
            if time.time()-float(payload.get('observed_at',0))<120:
                for proof in payload.get('sessions',[])[:2]:
                    try:
                        row=self.book.get(proof['lease_id']);identity=row['identity'] or {}
                        if proof.get('database_user')!=identity.get('database_user'):continue
                        if not proof.get('database_user') or float(payload['observed_at'])<row['heartbeat']:continue
                        self.book.reconcile_absence(proof['lease_id'],observed_server_uuid=proof['server_uuid'],
                            observed_connection_id=int(proof['connection_id']),session_present=proof['session_present'])
                    except (KeyError,ValueError,RuntimeError):continue
        pending=[]
        for row in self.book.rows():
            identity=json.loads(row['identity']) if row['identity'] else None
            if row['state']=='unknown' and identity and identity.get('database_user'):
                pending.append(dict(lease_id=row['id'],**identity))
        signature=json.dumps(pending,sort_keys=True)
        if (signature!=self.signature or not self.requests.is_file()) and self.requests.parent.is_dir():
            atomic_json(self.requests,{'sessions':pending[:2]},0o644);self.signature=signature


def watchdog_reconcile(connection,requests=REQUESTS,proofs=PROOFS):
    """Called after the watchdog's normal scan, on that same control connection.

    No connection is created here. Matching server UUID and authenticated user
    are required: a different replica or an invisible user's absence is not
    evidence. Never KILL or close a business session during reconciliation.
    """
    path=Path(requests)
    if not path.is_file():return 0
    rows=json.loads(path.read_text()).get('sessions',[])
    if not rows:return 0
    results=[]
    with connection.cursor() as cursor:
        for row in rows[:2]:
            if not row.get('database_user') or int(row.get('connection_id',0))<=0:continue
            cursor.execute("SELECT /*+ MAX_EXECUTION_TIME(1000) */ @@server_uuid AS server_uuid, "
                "SUBSTRING_INDEX(CURRENT_USER(),'@',1) AS database_user, "
                'EXISTS(SELECT 1 FROM information_schema.PROCESSLIST WHERE ID=%s AND USER=%s) AS session_present',
                (int(row['connection_id']),row['database_user']))
            observation=cursor.fetchone()
            results.append(dict(lease_id=row['lease_id'],server_uuid=observation['server_uuid'],
                                database_user=observation['database_user'],connection_id=int(row['connection_id']),
                                session_present=bool(observation['session_present'])))
    atomic_json(proofs,{'observed_at':time.time(),'sessions':results})
    return len(results)
