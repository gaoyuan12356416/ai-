"""CPU packet framing and NumPy MP4 tables; encoded media bytes stay unchanged.

Shares the validated container/timeline/file lifecycle with CudaMP4. Only the
array backend and Annex B/header parsing differ; no video or audio is decoded.
"""
import re
import struct

import numpy as np

from .gpu_mux import CudaMP4, MuxError


class Bits:
    def __init__(self, data, offset=0):
        # Match the existing CUDA RBSP filter, including original neighbours.
        self.data = bytes(b for i, b in enumerate(data)
                          if not (i >= 2 and b == 3 and data[i-2:i] == b'\0\0'))
        self.offset = offset

    def read(self, count):
        if self.offset + count > len(self.data)*8:
            raise MuxError('truncated_parameter_set')
        result = 0
        for _ in range(count):
            result = (result << 1) | ((self.data[self.offset//8] >> (7-self.offset % 8)) & 1)
            self.offset += 1
        return result

    def ue(self):
        zeros = 0
        while not self.read(1):
            zeros += 1
            if zeros > 30:
                raise MuxError('invalid_exp_golomb')
        return (1 << zeros)-1+self.read(zeros)


class CpuMP4(CudaMP4):
    implementation = 'cpu_packet_framing_numpy_mp4_v1'
    cpu_work = 'encoded packet framing,MP4 tables/interleave/serialization,file IO; no re-encoding'

    def __init__(self):
        self.cp = np

    def host(self, value):
        return value

    def raw(self, data):
        return np.frombuffer(data, dtype=np.uint8)

    def be(self, values, width=4):
        # Signed ctts values must serialize as two's complement, including
        # NumPy 2 where constructing unsigned arrays from negatives raises.
        values = np.asarray(values).astype(np.uint64).reshape(-1)
        shifts = (width-1-np.arange(width, dtype=np.uint64))*8
        return ((values[:, None] >> shifts[None, :]) & 255).astype(np.uint8).reshape(-1)

    def nals(self, data, codec):
        if codec not in ('h264', 'hevc'):
            raise MuxError('unsupported_video_codec')
        if len(data) < 4:
            raise MuxError('invalid_annexb_packet')
        starts = list(re.finditer(b'\x00\x00\x00\x01|(?<!\x00)\x00\x00\x01', data))
        if not starts:
            raise MuxError('annexb_start_code_missing')
        for i, start in enumerate(starts):
            end = starts[i+1].start() if i+1 < len(starts) else len(data)
            nal = data[start.end():end]
            if not nal:
                raise MuxError('empty_annexb_nal')
            yield ((nal[0] >> 1) & 63 if codec == 'hevc' else nal[0] & 31), nal

    def annexb_sample(self, data, *, with_key=False, codec='h264'):
        parts, key = [], False
        excluded = {32, 33, 34, 35} if codec == 'hevc' else {7, 8, 9}
        for kind, nal in self.nals(data, codec):
            key |= 16 <= kind <= 21 if codec == 'hevc' else kind == 5
            if kind not in excluded:
                parts.extend((struct.pack('>I', len(nal)), nal))
        if not parts:
            raise MuxError('annexb_access_unit_has_no_payload')
        result = b''.join(parts)
        return (result, key) if with_key else result

    def avcc_config(self, sequence):
        nals = list(self.nals(sequence, 'h264'))
        sps = [v for k, v in nals if k == 7]
        pps = [v for k, v in nals if k == 8]
        if len(sps) != 1 or len(pps) != 1:
            raise MuxError('unsupported_avc_parameter_sets')
        sps, pps = sps[0], pps[0]
        if not 4 <= len(sps) <= 65535 or not 1 <= len(pps) <= 65535:
            raise MuxError('invalid_avc_parameter_set_length')
        config = b'\x01'+sps[1:4]+b'\xff\xe1'+struct.pack('>H', len(sps))+sps+b'\x01'+struct.pack('>H', len(pps))+pps
        if sps[1] not in (66, 77, 88):
            try:
                bits = Bits(sps, 32)
                bits.ue()
                chroma = bits.ue()
                if chroma == 3:
                    bits.read(1)
                luma, color = bits.ue(), bits.ue()
                if chroma > 3 or luma > 6 or color > 6:
                    raise MuxError('invalid_high_profile_sps')
            except MuxError as exc:
                raise MuxError('invalid_high_profile_sps') from exc
            config += bytes([252 | chroma, 248 | luma, 248 | color, 0])
        return config

    def hvcc_config(self, sequence):
        nals = list(self.nals(sequence, 'hevc'))
        units, arrays = {}, []
        for kind in (32, 33, 34):
            matches = [v for k, v in nals if k == kind]
            if len(matches) != 1:
                raise MuxError('unsupported_hevc_parameter_sets')
            nal = matches[0]
            if not 3 <= len(nal) <= 65535:
                raise MuxError('invalid_hevc_parameter_set_length')
            if ((nal[0] & 1) << 5) | (nal[1] >> 3):
                raise MuxError('unsupported_multilayer_hevc')
            units[kind] = Bits(nal).data
            arrays.append(bytes([128 | kind])+struct.pack('>HH', 1, len(nal))+nal)
        sps, vps = units[33], units[32]
        if len(sps) < 16 or len(vps) < 18:
            raise MuxError('truncated_hevc_parameter_set')
        if ((vps[2] & 3) << 4) | (vps[3] >> 4):
            raise MuxError('unsupported_multilayer_hevc')
        try:
            # units are already RBSP: do not remove emulation bytes twice.
            bits = Bits(b'')
            bits.data, bits.offset = sps, 20
            sub, nested = bits.read(3), bits.read(1)
            bits.read(96)
            flags = [(bits.read(1), bits.read(1)) for _ in range(sub)]
            if sub:
                bits.read((8-sub)*2)
            for profile, level in flags:
                bits.read(88 if profile else 0)
                bits.read(8 if level else 0)
            bits.ue()
            chroma = bits.ue()
            if chroma == 3:
                bits.read(1)
            bits.ue(); bits.ue()
            if bits.read(1):
                for _ in range(4): bits.ue()
            luma, color = bits.ue(), bits.ue()
            if sub > 6 or chroma > 3 or luma > 6 or color > 6:
                raise MuxError('invalid_hevc_sps_prefix')
        except MuxError as exc:
            raise MuxError('invalid_hevc_sps_prefix') from exc
        sptl, vptl = sps[3:15], vps[6:18]
        if (sptl[0] >> 6) != (vptl[0] >> 6):
            raise MuxError('incompatible_hevc_profile_space')
        profile = (sptl[0] & 192) | max(sptl[0] & 32, vptl[0] & 32) | max(sptl[0] & 31, vptl[0] & 31)
        ptl = bytes([profile])+bytes(a & b for a, b in zip(sptl[1:11], vptl[1:11]))+bytes([max(sptl[11], vptl[11])])
        vps_sub = (vps[3] >> 1) & 7
        if vps_sub > 6:
            raise MuxError('invalid_hevc_temporal_layers')
        temporal = (max(sub, vps_sub)+1)*8+(nested << 2)+3
        return (b'\x01'+ptl+b'\xf0\x00'+bytes([252, 252 | chroma, 248 | luma, 248 | color])+
                b'\x00\x00'+bytes([temporal, 3])+b''.join(arrays))
