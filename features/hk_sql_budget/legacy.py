"""Compatibility with the two live HK consumers of gpu_offload.sql_client."""
from contextlib import contextmanager
import time

from .connection import session_connection


class Broker:
    """Existing entry points construct a Broker; session admission is now local."""
    def __init__(self, port, token, receipt_root):
        pass


@contextmanager
def read_session(broker, task, *, user, password, database=None, budget_seconds=840,
                 statement_timeout_ms=15000, report_options=False, compressed=False):
    if not 0 < budget_seconds <= 840 or not 0 < statement_timeout_ms <= 15000:
        raise ValueError("query_budget_cannot_exceed_existing_report_limits")
    # 'compressed' selected the old transcontinental transport. The new private
    # MySQL proxy does not advertise CLIENT_COMPRESS, including on the writer.
    # Both variants return buffered dict rows on a single read-only snapshot.
    with session_connection("read", time.monotonic()+budget_seconds, database=database,
                            user=user, password=password, dict_cursor=True,
                            statement_timeout_ms=statement_timeout_ms, report_options=report_options) as connection:
        yield connection
