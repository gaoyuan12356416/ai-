"""Read H.264 SPS dimensions to recreate NVDEC at a resolution boundary.

This parses only the bounded SPS header; pixels are never decoded on the CPU.
The normalized input contract is progressive 8-bit 4:2:0 H.264.
"""
import re


class Bits:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def read(self, count=1):
        if count > 32 or self.offset + count > len(self.data)*8:
            raise ValueError("native_h264_header_invalid")
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.data[self.offset//8] >> (7-self.offset%8)) & 1)
            self.offset += 1
        return value

    def ue(self):
        zeros = 0
        while not self.read():
            zeros += 1
            if zeros > 24:
                raise ValueError("native_h264_header_invalid")
        return (1 << zeros)-1 + self.read(zeros)

    def se(self):
        value = self.ue()
        return (value+1)//2 if value & 1 else -value//2


def sps_dimensions(nal):
    if not 4 <= len(nal) <= 4096 or nal[0] & 31 != 7:
        raise ValueError("native_h264_sps_invalid")
    bits = Bits(nal[1:].replace(b"\x00\x00\x03", b"\x00\x00"))
    profile = bits.read(8)
    bits.read(16)  # constraint flags and level
    bits.ue()  # seq_parameter_set_id
    chroma = 1
    if profile in {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}:
        chroma = bits.ue()
        if chroma != 1 or bits.ue() != 0 or bits.ue() != 0:
            raise ValueError("native_h264_format_unsupported")
        bits.read()  # qpprime_y_zero_transform_bypass_flag
        if bits.read():
            for index in range(8):
                if bits.read():
                    last, following = 8, 8
                    for _ in range(16 if index < 6 else 64):
                        if following:
                            following = (last + bits.se() + 256) % 256
                        last = following or last
    bits.ue()  # log2_max_frame_num_minus4
    poc = bits.ue()
    if poc == 0:
        bits.ue()
    elif poc == 1:
        bits.read()
        bits.se()
        bits.se()
        count = bits.ue()
        if count > 256:
            raise ValueError("native_h264_header_invalid")
        for _ in range(count):
            bits.se()
    elif poc != 2:
        raise ValueError("native_h264_header_invalid")
    bits.ue()  # max_num_ref_frames
    bits.read()  # gaps_in_frame_num_value_allowed_flag
    width, height = (bits.ue()+1)*16, (bits.ue()+1)*16
    if bits.read() != 1:
        raise ValueError("native_h264_interlace_unsupported")
    bits.read()  # direct_8x8_inference_flag
    if bits.read():
        left, right, top, bottom = (bits.ue() for _ in range(4))
        width -= 2*(left+right)
        height -= 2*(top+bottom)
    if not 16 <= width <= 4096 or not 16 <= height <= 4096 or width % 2 or height % 2:
        raise ValueError("native_h264_dimensions_invalid")
    return width, height


def packet_dimensions(data):
    starts = list(re.finditer(b"\x00\x00(?:\x00)?\x01", data))
    result = None
    for index, start in enumerate(starts):
        end = starts[index+1].start() if index+1 < len(starts) else len(data)
        nal = data[start.end():end]
        if nal and nal[0] & 31 == 7:
            result = sps_dimensions(nal)
    return result
