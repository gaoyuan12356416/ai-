"""Preserve MySQL packets split across any number of compressed frames.

Connector 9.4's pure transport assumes a complete four-byte MySQL header in
each decompressed fragment and joins at most one subsequent fragment. MySQL
allows either the header or payload to span arbitrary compression frames.
This small receive override retains the driver's authentication/send/SQL code.
"""
import zlib
from mysql.connector.network import NetworkBrokerCompressed,NetworkBrokerPlain
from mysql.connector.errors import InterfaceError


class FragmentSafeCompressedBroker(NetworkBrokerCompressed):
    def __init__(self):
        super().__init__()
        self.fragment=bytearray()

    def _recv_compressed_pkt(self,sock,compressed_pll,uncompressed_pll):
        while True:
            chunk=NetworkBrokerPlain._recv_chunk(self,sock,size=compressed_pll)
            if uncompressed_pll:
                # The frame's 24-bit advertised size bounds decompression.
                decoder=zlib.decompressobj()
                chunk=decoder.decompress(chunk,uncompressed_pll+1)
                if len(chunk)!=uncompressed_pll or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise InterfaceError('invalid compressed MySQL frame length')
            self.fragment.extend(chunk)
            offset=0
            while len(self.fragment)-offset>=4:
                size=int.from_bytes(self.fragment[offset:offset+3],'little')+4
                if len(self.fragment)-offset<size:break
                self._queue_read.append(self.fragment[offset:offset+size])
                offset+=size
            if offset:del self.fragment[:offset]
            if self._queue_read:return
            # A partial header or row requires another compression frame.
            header=NetworkBrokerPlain._recv_chunk(self,sock,size=7)
            compressed_pll=int.from_bytes(header[:3],'little')
            self._compressed_pktnr=header[3]
            uncompressed_pll=int.from_bytes(header[4:7],'little')
