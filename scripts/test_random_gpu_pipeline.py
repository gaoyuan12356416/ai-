"""GPU qualification and command invariants; no platform or GPU access."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from features.random_gpu.gpu_pipeline import qualification, source_qualification, fuse_command, kernel_source

VIDEO = dict(width=720, height=1280, codec_name='h264', pix_fmt='yuv420p',
             color_space='bt709', color_range='tv', field_order='progressive')
RECIPE = dict(rotation_millidegrees=1870, scale_bp=9930, tint_opacity_bp=674,
              source_overlay=dict(version=1, opacity_bp=291, scale_bp=11074))

class PipelineTests(unittest.TestCase):
    def test_default_off_and_unsupported_sources(self):
        self.assertEqual(qualification({'video': VIDEO}, {}), 'disabled')
        for change in ({'codec_name':'vp9'}, {'pix_fmt':'yuv420p10le'},
                       {'field_order':'tt'}, {'color_transfer':'smpte2084'},
                       {'width':721}, {'chroma_location':'bottom'}, {'color_range':'pc'},
                       {'start_time':'0.4'}, {'r_frame_rate':'10/1'},
                       {'r_frame_rate':'30/1', 'avg_frame_rate':'22/1'},
                       {'sample_aspect_ratio':'4:3'}):
            self.assertNotEqual(qualification({'video': dict(VIDEO, **change)},
                                {'RANDOM_GPU_PIPELINE_V2':'1'}), 'qualified')

    def test_gpu_command_keeps_audio_and_delivery_contract(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'RANDOM_GPU_PIPELINE_V2':'1'}), patch('features.random_gpu.asset_cache.replace_asset_inputs', side_effect=lambda x:x), patch('features.random_gpu.gpu_pipeline.source_qualification', return_value='qualified'):
            output = str(Path(tmp)/'out.mp4')
            command = ['ffmpeg','-y','-i','source.mp4']
            for name in ('border','opacity','corners','tint'): command += ['-stream_loop','-1','-i',name+'.nut']
            tail = ['-map','[v]','-map','0:a:0','-af','aresample=48000:async=1:first_pts=0,apad','-c:a','aac','-c:v','h264_nvenc','-preset','p5','-cq','21','-pix_fmt','yuv420p','-t','20',output]
            result = fuse_command(command+['-filter_complex','old']+tail, RECIPE, output, {'video':VIDEO})
            graph = result[result.index('-filter_complex')+1]
            self.assertIn('fit_yuv_cover', graph)
            self.assertNotIn('fit_yuv_contain', graph)
            self.assertIn('split=2[source][main]', graph)
            self.assertNotIn('scale=', graph)
            self.assertIn('h264_cuvid', result)
            self.assertEqual(result[result.index('-af')+1], tail[tail.index('-af')+1])
            self.assertEqual(result[result.index('-t')+1], '20')
            self.assertEqual(result[result.index('-pix_fmt')+1], 'yuv420p')
            self.assertNotIn('-rgb_mode', result)
            self.assertIn('setparams=colorspace=bt709', graph)
            self.assertEqual(result[result.index('-preset')+1], 'p5')
            self.assertTrue(list(Path(tmp).glob('gpu-fit-*.cl')))
            other = fuse_command(command+['-filter_complex','old']+tail, RECIPE, output,
                                 {'video':dict(VIDEO,width=1280,height=720)})
            self.assertIn('fit_yuv_contain', other[other.index('-filter_complex')+1])

    def test_color_range_and_chroma_are_explicit(self):
        source=kernel_source(RECIPE, dict(VIDEO, color_range='pc', chroma_location='center'))
        self.assertIn('#define GPU_FULL_RANGE 1',source)
        self.assertIn('#define GPU_BT709 1',source)
        self.assertIn('#define GPU_CHROMA_X 0.5f',source)

    def test_sequence_changes_and_failed_scan_preserve_compatibility_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'input.mp4'; source.write_bytes(b'fixture')
            command = ['ffmpeg', '-i', str(source)]
            formats = {(720,1280,'yuv420p','bt709','tv')}
            with patch('features.random_gpu.gpu_pipeline._keyframe_formats', return_value=formats):
                self.assertEqual(source_qualification(command, {'video':VIDEO}), 'qualified')
            formats.add((1280,720,'yuv420p','bt709','tv'))
            with patch('features.random_gpu.gpu_pipeline._keyframe_formats', return_value=formats):
                self.assertEqual(source_qualification(command, {'video':VIDEO}), 'changing_source_geometry')
            with patch('features.random_gpu.gpu_pipeline._keyframe_formats', side_effect=OSError()):
                self.assertEqual(source_qualification(command, {'video':VIDEO}), 'source_scan_unavailable')

if __name__=='__main__': unittest.main()
