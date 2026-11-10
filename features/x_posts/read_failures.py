"""Small allowlist of safe diagnostics for read-only preflight failures."""
import errno
import http.client
import socket
import urllib.error

DB_CONNECTION_ERRNOS = frozenset((2002,2003,2006,2013,2055))
NETWORK_ERRNOS = frozenset((errno.ETIMEDOUT,errno.ECONNRESET,errno.ECONNABORTED,
                           errno.ECONNREFUSED,errno.EPIPE,errno.ENETUNREACH,errno.EHOSTUNREACH))


def read_failure(exc):
    """Never infer from driver messages, SQL, hosts, URLs or general error names."""
    if (type(exc).__name__=='OperationalError'
            and type(exc).__module__.split('.')[0] in ('pymysql','MySQLdb','mysql')
            and exc.args and type(exc.args[0]) is int and exc.args[0] in DB_CONNECTION_ERRNOS):
        return {'kind':'mysql_connection','errno':exc.args[0]}
    if isinstance(exc,urllib.error.HTTPError):return None
    if isinstance(exc,urllib.error.URLError):exc=exc.reason
    if isinstance(exc,TimeoutError):return {'kind':'network_timeout'}
    if isinstance(exc,(http.client.RemoteDisconnected,http.client.IncompleteRead)):
        return {'kind':'network_interrupted'}
    if isinstance(exc,socket.gaierror):
        return {'kind':'temporary_dns'} if exc.errno==socket.EAI_AGAIN else None
    if isinstance(exc,OSError) and exc.errno in NETWORK_ERRNOS:
        return {'kind':'network_connection','errno':exc.errno}
    return None
