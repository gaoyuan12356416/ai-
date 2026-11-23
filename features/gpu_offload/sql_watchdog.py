"""Retain the existing watchdog and reconcile leases on its one connection."""
import os
import time
from . import legacy_sql_watchdog as original
from .sql_reconciliation import watchdog_reconcile,REQUESTS,PROOFS


class QueryWatchdog(original.QueryWatchdog):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.last_lease_reconcile=0

    def scan_once(self):
        super().scan_once()
        now=time.monotonic()
        if now-self.last_lease_reconcile<60:return
        self.last_lease_reconcile=now
        try:
            count=watchdog_reconcile(self.connection,
                requests=os.environ.get('SQL_OFFLOAD_RECONCILE_REQUESTS',str(REQUESTS)),
                proofs=os.environ.get('SQL_OFFLOAD_RECONCILE_PROOFS',str(PROOFS)))
            self.state['offload_reconciliation']={'ok':True,'checked_sessions':count}
        except Exception as error:
            self.state['offload_reconciliation']={'ok':False,'error_type':type(error).__name__}
            self.log('offload_reconciliation_failed',error_type=type(error).__name__)
        self.write_state()


def main():
    original.QueryWatchdog=QueryWatchdog
    return original.main()


if __name__=='__main__':raise SystemExit(main())
