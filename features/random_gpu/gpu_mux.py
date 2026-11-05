"""CUDA-built MP4 sample tables and packet interleaving.

The host supplies codec descriptions and encoded samples, manages file IO and
commits the file. CUDA computes order, durations, offsets, timing runs, index
arrays and serializes all MP4 boxes. This is not an NVENC capability and does
not make filesystem calls GPU-native. No FFmpeg CPU muxer is used here.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
import os
from pathlib import Path
import tempfile

from .gpu_compute import cupy_runtime


@dataclass(frozen=True)
class PacketFile:
    path: Path
    offset: int
    size: int

    def __post_init__(self):
        if self.offset < 0 or self.size <= 0:
            raise MuxError("invalid_packet_file_range")

    def __len__(self):
        return self.size


@dataclass(frozen=True)
class Sample:
    data: bytes | PacketFile
    pts: int
    dts: int
    duration: int
    key: bool = False


@dataclass(frozen=True)
class Track:
    kind: str
    timescale: int
    samples: list[Sample]
    config: bytes  # avcC payload for H264; AudioSpecificConfig for AAC
    width: int = 0
    height: int = 0
    channels: int = 2
    sample_rate: int = 48000
    color: tuple[int,int,int,bool] | None = None
    codec: str = 'h264'


class MuxError(ValueError):
    pass


class CudaMP4:
    implementation = 'cuda_mp4_tables_interleave_box_serialization_v1'
    cpu_work = 'codec description/encoded packet input,box structure planning,file IO'

    def __init__(self):
        self.cp = cupy_runtime()

    def host(self, value):
        return value.get()

    def raw(self, data):
        import numpy as np
        return self.cp.asarray(np.frombuffer(data, dtype=np.uint8))

    def be(self, values, width=4):
        values = self.cp.asarray(values, dtype=self.cp.uint64).reshape(-1)
        shifts = (width-1-self.cp.arange(width,dtype=self.cp.uint64))*8
        return ((values[:, None] >> shifts[None, :]) & 255).astype(self.cp.uint8).reshape(-1)

    def join(self, *parts):
        return self.cp.concatenate([p.reshape(-1) for p in parts])

    def annexb_nals(self, data, *, codec='h264'):
        import numpy as np
        cp=self.cp
        source=self.raw(data)
        if source.size<4:raise MuxError("invalid_annexb_packet")
        code=r'''
        extern "C" __global__ void starts(const unsigned char* x,int n,unsigned char* lens) {
            int i=blockDim.x*blockIdx.x+threadIdx.x;
            if(i>=n)return;
            unsigned char value=0;
            if(i+3<n && x[i]==0 && x[i+1]==0 && x[i+2]==0 && x[i+3]==1)value=4;
            else if(i+2<n && x[i]==0 && x[i+1]==0 && x[i+2]==1 && (i==0||x[i-1]!=0))value=3;
            lens[i]=value;
        }'''
        flags=cp.empty(source.size,dtype=cp.uint8)
        cp.RawKernel(code,'starts')(((source.size+255)//256,),(256,),
                                    (source,np.int32(source.size),flags))
        starts=cp.flatnonzero(flags)
        if not starts.size:raise MuxError("annexb_start_code_missing")
        payload=starts+flags[starts]
        ends=cp.concatenate((starts[1:],cp.asarray([source.size],dtype=starts.dtype)))
        sizes=ends-payload
        if bool(cp.any(sizes<=0)):raise MuxError("empty_annexb_nal")
        if codec not in ('h264','hevc'):raise MuxError('unsupported_video_codec')
        kinds=(source[payload]>>1)&63 if codec=='hevc' else source[payload]&31
        return source,payload,sizes,kinds

    def annexb_sample(self, data, *, with_key=False, codec='h264'):
        import numpy as np
        cp=self.cp
        source,offsets,sizes,kinds=self.annexb_nals(data,codec=codec)
        if codec=='hevc':
            mask=(kinds!=32)&(kinds!=33)&(kinds!=34)&(kinds!=35)
            key=(kinds>=16)&(kinds<=21)
        else:
            mask=(kinds!=7)&(kinds!=8)&(kinds!=9)
            key=kinds==5
        offsets=offsets[mask];sizes=sizes[mask]
        if not offsets.size:raise MuxError("annexb_access_unit_has_no_payload")
        lengths=(sizes+4).astype(cp.uint64)
        dest=cp.cumsum(lengths,dtype=cp.uint64)-lengths
        total=int(cp.sum(lengths,dtype=cp.uint64).get())
        result=cp.empty(total,dtype=cp.uint8)
        code=r'''
        extern "C" __global__ void avcc(const unsigned char* src,const long long* off,
            const long long* sizes,const unsigned long long* dest,unsigned char* out) {
            int i=blockIdx.x;unsigned long long d=dest[i];unsigned int n=sizes[i];
            if(threadIdx.x==0){out[d]=n>>24;out[d+1]=n>>16;out[d+2]=n>>8;out[d+3]=n;}
            for(unsigned int j=threadIdx.x;j<n;j+=blockDim.x)out[d+4+j]=src[off[i]+j];
        }'''
        cp.RawKernel(code,'avcc')((offsets.size,),(256,),
            (source,offsets.astype(cp.int64),sizes.astype(cp.int64),dest,result))
        payload=result.get().tobytes()
        return (payload,bool(cp.any(key))) if with_key else payload

    def _rbsp(self, nal):
        cp=self.cp
        keep=cp.ones(nal.size,dtype=cp.bool_)
        keep[2:]=~((nal[2:]==3)&(nal[1:-1]==0)&(nal[:-2]==0))
        return nal[keep]

    def hvcc_config(self, sequence):
        """GPU HEVC decoder record for one encoder-generated VPS/SPS/PPS.

        ISO/IEC 14496-15 hvcC layout; zero spatial segmentation/parallelism
        declare no additional partition constraint, rather than inventing VUI.
        """
        import numpy as np
        cp=self.cp
        source,offsets,sizes,kinds=self.annexb_nals(sequence,codec='hevc')
        units={};arrays=[]
        for kind in (32,33,34):
            indices=cp.flatnonzero(kinds==kind)
            if indices.size!=1:raise MuxError('unsupported_hevc_parameter_sets')
            index=int(indices[0].get());offset=int(offsets[index].get());size=int(sizes[index].get())
            if not 3<=size<=65535:raise MuxError('invalid_hevc_parameter_set_length')
            nal=source[offset:offset+size]
            layer_id=((nal[0]&1)<<5)|(nal[1]>>3)
            if bool(layer_id!=0):raise MuxError('unsupported_multilayer_hevc')
            units[kind]=self._rbsp(nal)
            arrays.append(self.join(self.be([128|kind],1),self.be([1,size],2),nal))
        sps,vps=units[33],units[32]
        if sps.size<16 or vps.size<18:raise MuxError('truncated_hevc_parameter_set')
        vps_layers=((vps[2]&3)<<4)|(vps[3]>>4)
        if bool(vps_layers!=0):raise MuxError('unsupported_multilayer_hevc')
        fields=cp.zeros(6,dtype=cp.uint32)
        code=r'''
        __device__ unsigned readbits(const unsigned char* p,int n,int* at,int count,unsigned* bad){
            unsigned v=0;for(int i=0;i<count;i++){
                if(*at>=n*8){*bad=1;return 0;}int k=(*at)++;v=(v<<1)|((p[k/8]>>(7-k%8))&1);
            }return v;
        }
        __device__ unsigned readue(const unsigned char* p,int n,int* at,unsigned* bad){
            unsigned z=0;while(!*bad&&!readbits(p,n,at,1,bad)){if(++z>30){*bad=1;return 0;}}
            return ((1U<<z)-1)+readbits(p,n,at,z,bad);
        }
        extern "C" __global__ void hevc_prefix(const unsigned char* p,int n,unsigned* out){
            int at=20;unsigned bad=0;
            unsigned sub=readbits(p,n,&at,3,&bad),nested=readbits(p,n,&at,1,&bad);
            at+=96;unsigned profile[8]={0},level[8]={0};
            for(unsigned i=0;i<sub;i++){profile[i]=readbits(p,n,&at,1,&bad);level[i]=readbits(p,n,&at,1,&bad);}
            if(sub)at+=(8-sub)*2;
            for(unsigned i=0;i<sub;i++){if(profile[i])at+=88;if(level[i])at+=8;}
            readue(p,n,&at,&bad);unsigned chroma=readue(p,n,&at,&bad);
            if(chroma==3)readbits(p,n,&at,1,&bad);
            readue(p,n,&at,&bad);readue(p,n,&at,&bad);
            if(readbits(p,n,&at,1,&bad))for(int i=0;i<4;i++)readue(p,n,&at,&bad);
            unsigned luma=readue(p,n,&at,&bad),color=readue(p,n,&at,&bad);
            out[0]=sub;out[1]=nested;out[2]=chroma;out[3]=luma;out[4]=color;out[5]=bad;
        }'''
        cp.RawKernel(code,'hevc_prefix')((1,),(1,),(sps,np.int32(sps.size),fields))
        sub,nested,chroma,luma,color,bad=map(int,fields.get())
        if bad or sub>6 or chroma>3 or luma>6 or color>6:
            raise MuxError('invalid_hevc_sps_prefix')
        sptl,vptl=sps[3:15],vps[6:18]
        if bool((sptl[0]>>6)!=(vptl[0]>>6)):
            raise MuxError('incompatible_hevc_profile_space')
        profile=(sptl[:1]&192)|(cp.maximum(sptl[:1]&32,vptl[:1]&32))|cp.maximum(sptl[:1]&31,vptl[:1]&31)
        ptl=self.join(profile,sptl[1:11]&vptl[1:11],cp.maximum(sptl[11:12],vptl[11:12]))
        vps_sub=(vps[3]>>1)&7
        if bool(vps_sub>6):raise MuxError('invalid_hevc_temporal_layers')
        temporal=(cp.maximum(cp.asarray(sub,dtype=cp.uint8),vps_sub)+1)*8+(nested<<2)+3
        config=self.join(self.be([1],1),ptl,self.be([0xf000],2),
            self.be([0xfc,0xfc|chroma,0xf8|luma,0xf8|color],1),
            self.be([0],2),self.be(temporal,1),self.be([3],1),*arrays)
        return config.get().tobytes()

    def avcc_config(self, sequence):
        cp=self.cp
        source,offsets,sizes,kinds=self.annexb_nals(sequence)
        sps=cp.flatnonzero(kinds==7);pps=cp.flatnonzero(kinds==8)
        if sps.size!=1 or pps.size!=1:raise MuxError("unsupported_avc_parameter_sets")
        si=int(sps[0].get());pi=int(pps[0].get())
        so=int(offsets[si].get());po=int(offsets[pi].get())
        sn=int(sizes[si].get());pn=int(sizes[pi].get())
        if not 4<=sn<=65535 or not 1<=pn<=65535:raise MuxError("invalid_avc_parameter_set_length")
        config=self.join(self.raw(b'\x01'),source[so+1:so+4],self.raw(b'\xff\xe1'),
            self.be([sn],2),source[so:so+sn],self.raw(b'\x01'),self.be([pn],2),
            source[po:po+pn])
        profile=int(source[so+1].get())
        if profile not in (66,77,88):
            # Parse the short SPS prefix on-device, including emulation
            # prevention removal, instead of inventing bit depth/chroma values.
            nal=source[so:so+sn]
            keep=cp.ones(sn,dtype=cp.bool_)
            keep[2:]=~((nal[2:]==3)&(nal[1:-1]==0)&(nal[:-2]==0))
            rbsp=nal[keep]
            fields=cp.zeros(4,dtype=cp.uint32)
            code=r'''
            __device__ unsigned bit(const unsigned char* p,int n,int* at,unsigned* bad){
                if(*at>=n*8){*bad=1;return 0;}int k=(*at)++;return (p[k/8]>>(7-k%8))&1;
            }
            __device__ unsigned ue(const unsigned char* p,int n,int* at,unsigned* bad){
                unsigned zeros=0;while(!*bad&&!bit(p,n,at,bad)){if(++zeros>30){*bad=1;return 0;}}
                unsigned v=1;for(unsigned k=0;k<zeros;k++)v=(v<<1)|bit(p,n,at,bad);return v-1;
            }
            extern "C" __global__ void sps_prefix(const unsigned char* p,int n,unsigned* out){
                int at=32;unsigned bad=0;ue(p,n,&at,&bad);
                unsigned chroma=ue(p,n,&at,&bad);if(chroma==3)bit(p,n,&at,&bad);
                unsigned luma=ue(p,n,&at,&bad),color=ue(p,n,&at,&bad);
                out[0]=chroma;out[1]=luma;out[2]=color;out[3]=bad;
            }'''
            import numpy as np
            cp.RawKernel(code,'sps_prefix')((1,),(1,),(rbsp,np.int32(rbsp.size),fields))
            chroma,luma,color,bad=map(int,fields.get())
            if bad or chroma>3 or luma>6 or color>6:raise MuxError("invalid_high_profile_sps")
            config=self.join(config,self.be(cp.asarray([chroma,luma,color],dtype=cp.uint64)|cp.asarray([252,248,248],dtype=cp.uint64),1),self.raw(b'\0'))
        return config.get().tobytes()

    def box(self, kind, *parts):
        length = 8 + sum(p.size for p in parts)
        if length >= 2**32:
            raise MuxError("metadata_box_too_large")
        return self.join(self.be([length]), self.raw(kind), *parts)

    def full(self, kind, *parts, version=0, flags=0):
        return self.box(kind, self.be([(version << 24) | flags]), *parts)

    def timing_runs(self, values):
        cp = self.cp
        starts = cp.flatnonzero(cp.concatenate((cp.ones(1,dtype=cp.bool_),values[1:] != values[:-1])))
        ends = cp.concatenate((starts[1:],cp.asarray([values.size],dtype=starts.dtype)))
        table = cp.stack((ends-starts,values[starts]),axis=1)
        return self.join(self.be([starts.size]),self.be(table))

    def sample_entry(self, track):
        reserved = self.raw(b"\0"*6)
        if track.kind == "video":
            if track.codec not in ('h264','hevc'):raise MuxError('unsupported_video_codec')
            color=[]
            if track.color is not None:
                primaries,transfer,matrix,full_range=track.color
                if any(not 0<=v<=65535 for v in (primaries,transfer,matrix)):
                    raise MuxError("invalid_video_color_description")
                color=[self.box(b"colr",self.raw(b"nclx"),
                               self.be([primaries,transfer,matrix],2),
                               self.be([128 if full_range else 0],1))]
            return self.box(b"hvc1" if track.codec=='hevc' else b"avc1", reserved,self.be([1],2),self.raw(b"\0"*16),
                self.be([track.width,track.height],2),self.be([0x480000,0x480000,0]),
                self.be([1],2),self.raw(b"\0"*32),self.be([24,65535],2),
                self.box(b"hvcC" if track.codec=='hevc' else b"avcC",self.raw(track.config)),*color)
        # AAC-LC AudioSpecificConfig is carried unchanged. ES descriptor length
        # encodings are bounded here because this profile uses two-byte ASC.
        if not 2 <= len(track.config) <= 32:
            raise MuxError("unsupported_audio_specific_config")
        dsi = self.join(self.raw(bytes([5,len(track.config)])),self.raw(track.config))
        cp=self.cp
        media_bytes=cp.sum(cp.asarray([len(s.data) for s in track.samples],dtype=cp.uint64))
        media_ticks=cp.sum(cp.asarray([s.duration for s in track.samples],dtype=cp.uint64))
        average=media_bytes*8*track.timescale//media_ticks
        if bool(average>0xffffffff):raise MuxError('audio_bitrate_out_of_range')
        dc = self.join(self.raw(bytes([4,13+dsi.size,0x40,0x15])),
                       self.raw(b"\0"*3),self.be([0]),self.be(average),dsi)
        es = self.join(self.raw(bytes([3,3+dc.size+3])),self.be([2],2),
                       self.raw(b"\0"),dc,self.raw(bytes([6,1,2])))
        return self.box(b"mp4a",reserved,self.be([1],2),self.raw(b"\0"*8),
            self.be([track.channels,16,0,0],2),self.be([track.sample_rate<<16]),
            self.full(b"esds",es))

    def track_box(self, track, number, movie_duration, offsets, use64):
        cp = self.cp
        samples = track.samples
        durations = cp.asarray([s.duration for s in samples],dtype=cp.int64)
        pts = cp.asarray([s.pts for s in samples],dtype=cp.int64)
        dts = cp.asarray([s.dts for s in samples],dtype=cp.int64)
        sizes = cp.asarray([len(s.data) for s in samples],dtype=cp.uint64)
        if bool(cp.any(durations <= 0)) or bool(cp.any(durations > 0xffffffff)):
            raise MuxError("invalid_sample_duration")
        if dts.size>1 and bool(cp.any(dts[1:] != dts[:-1]+durations[:-1])):
            raise MuxError("non_contiguous_decode_timeline")
        if bool(dts[0]>0):
            raise MuxError("positive_decode_origin_requires_empty_edit")
        if bool(cp.any(sizes > 0xffffffff)) or bool(cp.any(sizes == 0)):
            raise MuxError("invalid_sample_size")
        composition = pts-dts
        if bool(cp.any(composition < -2**31)) or bool(cp.any(composition >= 2**31)):
            raise MuxError("composition_offset_out_of_range")
        matrix = self.be([0x10000,0,0,0,0x10000,0,0,0,0x40000000])
        tkhd = self.full(b"tkhd",self.be([0,0],8),self.be([number,0]),
            self.be([movie_duration],8),self.raw(b"\0"*8),
            self.be([0,0,0x100 if track.kind=="audio" else 0,0],2),matrix,
            self.be([track.width<<16,track.height<<16]),version=1,flags=7)
        edit = self.box(b"edts",self.full(b"elst",self.be([1]),
            self.be([movie_duration],8),self.be(-dts[:1],8),self.be([1,0],2),version=1))
        media_duration=cp.max(pts+durations)-pts[0]
        mdhd = self.full(b"mdhd",self.be([0,0],8),self.be([track.timescale]),
            self.be(media_duration,8),self.be([0x55c4,0],2),version=1)
        handler = b"vide" if track.kind=="video" else b"soun"
        hdlr = self.full(b"hdlr",self.be([0]),self.raw(handler),self.raw(b"\0"*12),
                         self.raw(b"VideoHandler\0" if track.kind=="video" else b"SoundHandler\0"))
        tables = [self.full(b"stsd",self.be([1]),self.sample_entry(track)),
                  self.full(b"stts",self.timing_runs(durations))]
        if bool(cp.any(composition != 0)):
            tables.append(self.full(b"ctts",self.timing_runs(composition),version=1))
        if track.kind=="video":
            keys=cp.flatnonzero(cp.asarray([s.key for s in samples],dtype=cp.bool_))+1
            if not keys.size:raise MuxError("video_missing_sync_sample")
            tables.append(self.full(b"stss",self.be([keys.size]),self.be(keys)))
        tables += [self.full(b"stsc",self.be([1,1,1,1])),
                   self.full(b"stsz",self.be([0,len(samples)]),self.be(sizes)),
                   self.full(b"co64" if use64 else b"stco",self.be([len(samples)]),
                             self.be(offsets,8 if use64 else 4))]
        stbl=self.box(b"stbl",*tables)
        dinf=self.box(b"dinf",self.full(b"dref",self.be([1]),self.full(b"url ",flags=1)))
        media_header=(self.full(b"vmhd",self.raw(b"\0"*8),flags=1) if track.kind=="video"
                      else self.full(b"smhd",self.raw(b"\0"*4)))
        return self.box(b"trak",tkhd,edit,self.box(b"mdia",mdhd,hdlr,
                        self.box(b"minf",media_header,dinf,stbl)))

    def mux(self, tracks, output, duration_seconds, check=None):
        import numpy as np
        cp=self.cp
        if not tracks or len(tracks)>2 or tracks[0].kind!="video":
            raise MuxError("unsupported_track_layout")
        if len(tracks)==2 and tracks[1].kind!="audio":raise MuxError("duplicate_video_track")
        for track in tracks:
            if track.kind not in {"video","audio"} or not track.samples or track.timescale<=0:
                raise MuxError("invalid_track")
            if track.kind=="video" and not (0<track.width<65536 and 0<track.height<65536):
                raise MuxError("invalid_video_geometry")
        duration=Fraction(str(duration_seconds))
        if duration<=0:raise MuxError("invalid_movie_duration")
        movie_scale=48000
        # -t is a request to the encoders, not an artificial edit-list end.
        # Encoded video frames and audio samples round on different clocks.
        # Preserve their actual ends, excluding negative AAC priming.
        track_durations=[]
        for track in tracks:
            ends=cp.asarray([s.pts for s in track.samples],dtype=cp.int64)+cp.asarray([s.duration for s in track.samples],dtype=cp.int64)
            end=cp.max(ends)
            if bool(end<=0):raise MuxError("invalid_track_presentation_end")
            whole=end//track.timescale
            remainder=end%track.timescale
            ticks=whole*movie_scale+(remainder*movie_scale+track.timescale-1)//track.timescale
            track_durations.append(int(self.host(ticks)))
        movie_duration=max(track_durations)
        if movie_duration>=2**63:raise MuxError("movie_duration_out_of_range")
        common=math.lcm(*(t.timescale for t in tracks))
        if common>=2**31:raise MuxError("incompatible_track_timescales")
        all_samples=[s for track in tracks for s in track.samples]
        scaled=cp.concatenate([cp.asarray([s.dts for s in t.samples],dtype=cp.int64)*(common//t.timescale) for t in tracks])
        order=cp.lexsort(cp.stack((cp.arange(scaled.size,dtype=cp.int64),scaled)))
        sizes=cp.asarray([len(s.data) for s in all_samples],dtype=cp.uint64)
        lengths=sizes[order]
        locations=cp.cumsum(lengths,dtype=cp.uint64)-lengths
        original=cp.empty_like(locations);original[order]=locations
        media_size=int(self.host(cp.sum(sizes,dtype=cp.uint64)))
        brands=b"isomiso2mp41" if tracks[0].codec=='hevc' else b"isomiso2avc1mp41"
        ftyp=self.box(b"ftyp",self.raw(b"isom"),self.be([512]),self.raw(brands))
        mdat_header=self.join(self.be([1]),self.raw(b"mdat"),self.be([media_size+16],8))
        matrix=self.be([0x10000,0,0,0,0x10000,0,0,0,0x40000000])
        mvhd=self.full(b"mvhd",self.be([0,0],8),self.be([movie_scale]),self.be([movie_duration],8),
            self.be([0x10000]),self.be([0x100,0],2),self.raw(b"\0"*8),matrix,
            self.raw(b"\0"*24),self.be([len(tracks)+1]),version=1)
        def make_moov(base, wide):
            parts=[];start=0
            for number,track in enumerate(tracks,1):
                count=len(track.samples)
                parts.append(self.track_box(track,number,track_durations[number-1],original[start:start+count]+base,wide))
                start+=count
            return self.box(b"moov",mvhd,*parts)
        # Length changes when stco widens to co64. Rebuild before computing the
        # final offsets; never reinterpret 32-bit wraparound as a valid file.
        moov=make_moov(0,False)
        base=ftyp.size+moov.size+mdat_header.size
        wide=bool(cp.any(original+base>0xffffffff))
        if wide:
            moov=make_moov(0,True);base=ftyp.size+moov.size+mdat_header.size
        moov=make_moov(base,wide)
        output=Path(output)
        for sample in all_samples:
            if isinstance(sample.data,PacketFile) and Path(sample.data.path).resolve()==output.resolve():
                raise MuxError("output_would_replace_packet_source")
        descriptor,name=tempfile.mkstemp(prefix="."+output.name+"-",suffix=".mux-tmp",dir=output.parent)
        temporary=Path(name)
        inputs={}
        try:
            with os.fdopen(descriptor,"wb") as handle:
                for part in (ftyp,moov,mdat_header):handle.write(self.host(part).tobytes())
                for index in self.host(order).tolist():
                    if check is not None:check()
                    data=all_samples[index].data
                    if isinstance(data,PacketFile):
                        path=Path(data.path)
                        if path not in inputs:inputs[path]=path.open("rb")
                        source=inputs[path];source.seek(data.offset);remaining=data.size
                        while remaining:
                            chunk=source.read(min(1024*1024,remaining))
                            if not chunk:raise MuxError("packet_source_truncated")
                            handle.write(chunk);remaining-=len(chunk)
                    else:
                        handle.write(data)
                handle.flush();os.fsync(handle.fileno())
            os.replace(temporary,output)
        finally:
            for stream in inputs.values():stream.close()
            temporary.unlink(missing_ok=True)
        return dict(implementation=self.implementation,
                    samples=len(all_samples),tracks=len(tracks),co64=wide,
                    bytes=output.stat().st_size,raw_video_frames_processed=0,
                    cpu_work=self.cpu_work)


def native_video_track(elementary, record, avcc_spool, *, width=720, height=1280, check=None, mux=None):
    """Convert NVENC AUs on CUDA, retaining only spool references in memory."""
    mux=CudaMP4() if mux is None else mux
    codec=record.get('codec',record.get('encoder_options',{}).get('codec','h264'))
    sequence=bytes.fromhex(record['sequence_header_hex'])
    config=mux.hvcc_config(sequence) if codec=='hevc' else mux.avcc_config(sequence)
    samples=[];offset=0
    with Path(elementary).open('rb') as source,Path(avcc_spool).open('xb') as target:
        for packet in record['packets']:
            if check is not None:check()
            source.seek(packet['offset']);data=source.read(packet['size'])
            if len(data)!=packet['size']:raise MuxError('native_packet_truncated')
            converted,key=mux.annexb_sample(data,with_key=True,codec=codec)
            target.write(converted)
            samples.append(Sample(PacketFile(Path(avcc_spool),offset,len(converted)),
                packet['pts'],packet['dts'],packet['duration'],key))
            offset+=len(converted)
        target.flush();os.fsync(target.fileno())
    return Track('video',30,samples,config,width=width,height=height,
                 color=(2,2,5,False),codec=codec)
