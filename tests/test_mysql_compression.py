import struct
import unittest
import zlib
from mysql.connector.network import NetworkBrokerCompressed
from mysql.connector.errors import InterfaceError
from features.gpu_offload.mysql_compression import FragmentSafeCompressedBroker


def packet(payload,sequence=1):return len(payload).to_bytes(3,'little')+bytes([sequence])+payload


def frame(payload,sequence=0,compress=True):
    body=zlib.compress(payload) if compress else payload
    return len(body).to_bytes(3,'little')+bytes([sequence])+(len(payload) if compress else 0).to_bytes(3,'little')+body


class Socket:
    def __init__(self,data):self.data=data;self.offset=0
    def recv_into(self,target,size):
        size=min(size,3,len(self.data)-self.offset)
        target[:size]=self.data[self.offset:self.offset+size];self.offset+=size;return size


class CompressionTests(unittest.TestCase):
    def test_original_driver_reproduces_split_header_failure(self):
        first,second=packet(b'a'*10),packet(b'b'*20,2)
        wire=Socket(frame(first+second[:2])+frame(second[2:],1))
        with self.assertRaises(struct.error):NetworkBrokerCompressed().recv(wire,'fixture')

    def test_every_header_and_payload_split_preserves_all_rows(self):
        packets=[packet(b'abc'),packet(b'b'*71,2),packet(b'',3),packet(b'last',4)]
        stream=b''.join(packets)
        for split in range(1,len(stream)):
            with self.subTest(split=split):
                wire=Socket(frame(stream[:split])+frame(stream[split:],1,False));reader=FragmentSafeCompressedBroker()
                self.assertEqual([bytes(reader.recv(wire,'fixture')) for _ in packets],packets)
                self.assertFalse(reader.fragment)

    def test_single_row_spans_many_frames_and_sequence_rollover(self):
        packets=[packet(b'first',255),packet(b'payload'*100,0),packet(b'end',1)]
        stream=b''.join(packets);parts=[stream[i:i+13] for i in range(0,len(stream),13)]
        wire=Socket(b''.join(frame(part,i%256,i%2==0) for i,part in enumerate(parts)))
        reader=FragmentSafeCompressedBroker()
        self.assertEqual([bytes(reader.recv(wire,'fixture')) for _ in packets],packets)
        self.assertFalse(reader.fragment)

    def test_truncated_frame_is_never_accepted_as_a_complete_row(self):
        body=packet(b'payload'*20);wire=Socket(frame(body[:8])+frame(body[8:],1)[:-5])
        with self.assertRaises(InterfaceError):FragmentSafeCompressedBroker().recv(wire,'fixture')

    def test_incorrect_decompressed_length_is_rejected(self):
        body=bytearray(frame(packet(b'payload')));body[4:7]=(12).to_bytes(3,'little')
        with self.assertRaises(InterfaceError):FragmentSafeCompressedBroker().recv(Socket(body),'fixture')


if __name__=='__main__':unittest.main()
