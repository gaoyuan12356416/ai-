"""Pure clock/ownership regressions; GPU behavior has isolated real probes."""
from fractions import Fraction
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.random_gpu.native_video import CudaView,cfr_frames,round_near_inf,create_encoder
from features.random_gpu import native_video


class ClockTests(unittest.TestCase):
    def sequence(self,pts,durations,limit,origin=0):
        frames=[(p,SimpleNamespace(timestamp=p,duration=Fraction(d))) for p,d in zip(pts,durations)]
        return [f.timestamp for _,f in cfr_frames(frames,'1/30',origin,limit,lambda:None)]

    def test_nearest_negative_ties(self):
        self.assertEqual([round_near_inf(Fraction(n,2)) for n in (-3,-1,1,3)],[-2,-1,1,2])

    def test_fractional_output_bound_matches_ffmpeg_nvenc(self):
        for duration,count in [('0.01',1),('0.10001',3),('1.00001',30)]:
            got=self.sequence(range(60),[Fraction(1,30)]*60,duration)
            self.assertEqual(len(got),count)

    def test_video_eof_does_not_extend_to_audio_end(self):
        self.assertEqual(self.sequence([0,1,2],[Fraction(1,30)]*3,2),[0,1,2])

    def test_initial_video_gap_is_padded_without_moving_audio_origin(self):
        self.assertEqual(self.sequence([3,4],[Fraction(1,30)]*2,1),[3,3,3,3,4])

    def test_sparse_frames_hold_until_next_rounded_timestamp(self):
        self.assertEqual(self.sequence([0,2,5],[Fraction(1,30)]*3,1),[0,0,2,2,2,5])

    def test_cancel_checked_inside_long_duplicate_run(self):
        calls=0
        def check():
            nonlocal calls
            calls+=1
            if calls==6: raise RuntimeError('cancelled')
        frames=[(0,SimpleNamespace(timestamp=0)),(300,SimpleNamespace(timestamp=300))]
        with self.assertRaisesRegex(RuntimeError,'cancelled'):
            list(cfr_frames(frames,'1/30',0,20,check))


class CudaViewTests(unittest.TestCase):
    def test_sdk_p016_element_strides_are_normalized_to_bytes(self):
        owner=object()
        a=SimpleNamespace(__cuda_array_interface__={'version':3,'shape':[160,96,2],
            'strides':[192,2,1],'typestr':'|u2','stream':0,'data':(1024,False)})
        view=CudaView(a,owner)
        self.assertIs(view.owner,owner)
        self.assertEqual(view.__cuda_array_interface__['strides'],(384,4,2))
        self.assertEqual(view.__cuda_array_interface__['stream'],1)

    def test_owned_uint16_byte_strides_are_not_doubled(self):
        a=SimpleNamespace(__cuda_array_interface__={'version':3,'shape':(160,96,2),
            'strides':(384,4,2),'typestr':'<u2','data':(1024,False)})
        self.assertEqual(CudaView(a).__cuda_array_interface__['strides'],(384,4,2))


class EncoderBudgetTests(unittest.TestCase):
    def sdk(self,reconfigure=True,rate_error=False):
        calls=[]
        class Encoder:
            def __init__(self,options):
                self.config=SimpleNamespace(averageBitrate=options.get('bitrate',0)+(1 if rate_error else 0),
                    maxBitRate=options.get('maxbitrate',0),vbvBufferSize=options.get('vbvbufsize',0),
                    vbvInitialDelay=0,targetQuality=options.get('cq',0))
            def GetEncodeReconfigureParams(self):
                # The real SDK returns a snapshot, not a live NVENC config.
                return SimpleNamespace(**vars(self.config))
            def Reconfigure(self,config):
                calls.append(('reconfigure',dict(vars(config))))
                return reconfigure
        def create(*args,**kwargs):
            calls.append(('create',kwargs));return Encoder(kwargs)
        return SimpleNamespace(CreateEncoder=create),calls

    def test_budget_and_cq_survive_sdk_constructor_shortcut(self):
        sdk,calls=self.sdk()
        enc,record=create_encoder(sdk,720,1280,1,0,
            dict(codec='h264',cq=21,bitrate=5000000,maxbitrate=6000000,vbvbufsize=10000000))
        self.assertNotIn('cq',calls[0][1])
        self.assertEqual(calls[1][1]['targetQuality'],21)
        self.assertEqual(calls[1][1]['maxBitRate'],6000000)
        self.assertEqual(record['initial']['targetQuality'],0)
        self.assertEqual(record['applied_reconfigure']['targetQuality'],21)
        self.assertEqual(enc.GetEncodeReconfigureParams().targetQuality,0)

    def test_unbounded_fb_keeps_constructor_cq(self):
        sdk,calls=self.sdk()
        _,record=create_encoder(sdk,720,1280,1,0,dict(codec='h264',cq=21,bitrate=0))
        self.assertEqual(calls[0][1]['cq'],21)
        self.assertEqual(len(calls),1)
        self.assertIsNone(record['applied_reconfigure'])

    def test_reconfiguration_failure_is_not_silently_accepted(self):
        sdk,_=self.sdk(reconfigure=False)
        with self.assertRaisesRegex(RuntimeError,'reconfigure_failed'):
            create_encoder(sdk,720,1280,1,0,dict(cq=21,bitrate=5000000))

    def test_initial_budget_mismatch_stops_before_render(self):
        sdk,calls=self.sdk(rate_error=True)
        with self.assertRaisesRegex(RuntimeError,'rejected_bitrate'):
            create_encoder(sdk,720,1280,1,0,dict(cq=21,bitrate=5000000))
        self.assertEqual(len(calls),1)


class OutputColorLifetimeTests(unittest.TestCase):
    def render(self,codec,fail=False):
        events=[]
        class Contract:
            encoder_options={'source_av_format_context':123}
            def __init__(self,*args): events.append('create')
            def __enter__(self): events.append('open');return self
            def __exit__(self,*exc): events.append('close')
        def encode(renderer,source,output,info,assets,check,duration,options,construction):
            self.assertNotIn('source_av_format_context',options)
            if codec=='hevc':
                self.assertEqual(events,['create','open'])
                self.assertEqual(construction['source_av_format_context'],123)
            else:
                self.assertFalse(events)
                self.assertNotIn('source_av_format_context',construction)
            events.append('encode_finished')
            if fail: raise RuntimeError('cancelled')
            return {'encoder_options':options}
        with patch.object(native_video,'NativeRenderer',return_value=SimpleNamespace(nvc=object())), \
             patch('features.random_gpu.color_contract.OutputColorContract',Contract), \
             patch.object(native_video,'_encode_video',encode):
            try:
                native_video.encode_video('input','output',{'video':{}},{},None,lambda:None,
                    encoder_options={'codec':codec,'fps':30})
            except RuntimeError:
                if not fail: raise
        return events

    def test_hevc_context_outlives_encode_and_is_not_serialized(self):
        for fail in (False,True):
            self.assertEqual(self.render('hevc',fail),['create','open','encode_finished','close'])

    def test_h264_does_not_create_or_receive_output_context(self):
        self.assertEqual(self.render('h264'),['encode_finished'])


if __name__=='__main__': unittest.main()
