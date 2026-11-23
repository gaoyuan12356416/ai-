"""Compressed MySQL transport with the same admission and close proof as reads."""


class CompressedConnection:
    def __init__(self,connection):self.connection=connection

    @property
    def _sock(self):return self.connection._socket.sock if self.connection._socket else None

    def cursor(self):return self.connection.cursor(dictionary=True,buffered=True)

    def _write_bytes(self,packet):
        # Only Session.close calls this, with COM_QUIT. Let the negotiated
        # protocol frame and compress it; never inject uncompressed bytes.
        if packet!=b'\x01\x00\x00\x00\x01':raise ValueError('only_quit_allowed')
        self.connection.cmd_quit()

    def _force_close(self):self.connection.shutdown()


def connection_factory(session):
    from mysql.connector.connection import MySQLConnection
    from mysql.connector.constants import ClientFlag
    from mysql.connector.network import NetworkBrokerCompressed

    class Connection(MySQLConnection):
        def cmd_query(self,query,*args,**kwargs):
            session.admit_sql(query)
            return super().cmd_query(query,*args,**kwargs)

    def connect(**options):
        connection=Connection()
        adapter=CompressedConnection(connection)
        # Retain the object even if authentication/setup fails, so its socket
        # is closed and the lease conservatively awaits reconciliation.
        session.connection=adapter
        connection.connect(**options,compress=True,connection_timeout=4,
                           allow_local_infile=False,
                           conn_attrs={'program_name':'ai-offload/'+session.key})
        if not connection.client_flags & ClientFlag.COMPRESS or not isinstance(connection._socket._netbroker,NetworkBrokerCompressed):
            raise RuntimeError('campaign_protocol_compression_not_negotiated')
        return adapter
    return connect
