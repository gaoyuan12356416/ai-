"""Offline corrupt-output and generation-error regression checks."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageFile
from features.youtube_auto_publish.images import decode_cover
from features.youtube_auto_publish.runtime import generate_cover_factory
from features.youtube_auto_publish.templates import WorkflowError


def picture(fmt='PNG', size=(640,360), mode='RGB'):
    out=io.BytesIO();Image.new(mode,size).save(out,format=fmt);return out.getvalue()


def chunk(kind, payload):
    return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)


def png(width=640,height=360,*,interlace=0,filter_byte=0,extra=b'',depth=8,color=2):
    passes=[(width,height)] if not interlace else [(max(0,(width-x+dx-1)//dx),max(0,(height-y+dy-1)//dy)) for x,y,dx,dy in ((0,0,8,8),(4,0,8,8),(0,4,4,8),(2,0,4,4),(0,2,2,4),(1,0,2,2),(0,1,1,2))]
    channels={0:1,2:3,6:4}[color]
    raw=b''.join((bytes([filter_byte])+b'\0'*((w*depth*channels+7)//8))*h for w,h in passes if w and h)
    header=chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,depth,color,0,0,interlace))
    return b'\x89PNG\r\n\x1a\n'+header+extra+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')


class DecodeCase(unittest.TestCase):
    def assert_code(self, data, code, **kwargs):
        with self.assertRaises(WorkflowError) as ctx:decode_cover(data,**kwargs)
        self.assertEqual(ctx.exception.code,code)
        return ctx.exception

    def test_valid_generated_png_and_jpeg_decode_rgb(self):
        for fmt in ('PNG','JPEG'):
            decoded=decode_cover(picture(fmt),generated=True)
            self.assertEqual((decoded.mode,decoded.size),('RGB',(640,360)))

    def test_valid_adam7_png_is_actually_decoded(self):
        decoded=decode_cover(png(interlace=1),generated=True)
        self.assertEqual(decoded.size,(640,360))
        self.assertEqual(decoded.getpixel((639,359)),(0,0,0))

    def test_valid_subbyte_png_scanlines(self):
        self.assertEqual(decode_cover(png(depth=1,color=0),generated=True).size,(640,360))

    def test_ancillary_crc_failure_is_corrupt_not_ratio(self):
        extra=bytearray(chunk(b'caBX',b'metadata'));extra[-1]^=1
        self.assert_code(png(extra=bytes(extra)),'generated_cover_corrupt',generated=True)

    def test_valid_ancillary_chunk_is_allowed(self):
        self.assertEqual(decode_cover(png(extra=chunk(b'caBX',b'metadata')),generated=True).size,(640,360))

    def test_chunk_boundary_iend_and_trailing_data_are_strict(self):
        value=bytearray(png());value[33:37]=struct.pack('>I',0x7fffffff)
        for bad in (bytes(value),png()[:-12],png()+b'garbage',png()[:-1]):
            self.assert_code(bad,'generated_cover_corrupt',generated=True)

    def test_repaired_ihdr_crc_cannot_hide_wrong_scanline_length(self):
        value=png(height=400)
        forged=chunk(b'IHDR',struct.pack('>IIBBBBB',640,360,8,2,0,0,0))
        self.assert_code(value[:8]+forged+value[33:],'generated_cover_corrupt',generated=True)

    def test_adam7_header_size_forgery_is_corrupt(self):
        value=png(height=400,interlace=1)
        forged=chunk(b'IHDR',struct.pack('>IIBBBBB',640,360,8,2,0,0,1))
        self.assert_code(value[:8]+forged+value[33:],'generated_cover_corrupt',generated=True)

    def test_invalid_filter_is_corrupt(self):
        self.assert_code(png(filter_byte=5),'generated_cover_corrupt',generated=True)

    def test_idat_must_be_contiguous(self):
        value=png();length=struct.unpack_from('>I',value,33)[0];compressed=value[41:41+length]
        malformed=value[:33]+chunk(b'IDAT',compressed[:4])+chunk(b'tEXt',b'k\0v')+chunk(b'IDAT',compressed[4:])+chunk(b'IEND',b'')
        self.assert_code(malformed,'generated_cover_corrupt',generated=True)

    def test_extra_compressed_stream_is_rejected(self):
        value=png();length=struct.unpack_from('>I',value,33)[0];compressed=value[41:41+length]
        malformed=value[:33]+chunk(b'IDAT',compressed+zlib.compress(b'fake'))+chunk(b'IEND',b'')
        self.assert_code(malformed,'generated_cover_corrupt',generated=True)

    def test_truncated_global_setting_cannot_allow_corrupt_png(self):
        with patch.object(ImageFile,'LOAD_TRUNCATED_IMAGES',True):
            self.assert_code(png()[:-30],'generated_cover_corrupt',generated=True)
            self.assertTrue(ImageFile.LOAD_TRUNCATED_IMAGES)

    def test_dimensions_ratio_and_formats_are_distinct(self):
        self.assert_code(picture(size=(319,180)),'generated_cover_dimensions',generated=True)
        self.assert_code(picture(size=(640,400)),'generated_cover_ratio',generated=True)
        self.assert_code(picture('GIF'),'generated_cover_format',generated=True)
        huge=chunk(b'IHDR',struct.pack('>IIBBBBB',6000,6000,8,2,0,0,0))
        value=png();self.assert_code(value[:8]+huge+value[33:],'generated_cover_dimensions',generated=True)

    def test_reference_and_local_errors_have_correct_subject_and_limit(self):
        self.assertEqual(decode_cover(picture('WEBP',size=(64,64)),reference=True).size,(64,64))
        self.assert_code(picture('WEBP'),'cover_format')
        self.assert_code(picture(size=(63,64)),'reference_cover_dimensions',reference=True)
        self.assert_code(picture(size=(100,100)),'cover_dimensions')
        self.assertIn('原剧参考封面',str(self.assert_code(png()[:-20],'reference_cover_corrupt',reference=True)))

    def test_animated_png_is_format_error(self):
        self.assert_code(png(extra=chunk(b'acTL',struct.pack('>II',1,0))),'generated_cover_format',generated=True)

    def test_alpha_is_flattened_and_metadata_cleared(self):
        image=decode_cover(picture(mode='RGBA'))
        self.assertEqual(image.getpixel((0,0)),(255,255,255))
        self.assertEqual(image.info,{})

    def test_real_failed_v2_and_good_control_images(self):
        default=Path(__file__).resolve().parents[2]/'youtube-flicker-fix-20260910'/'output'/'failure-audit-7448081'
        folder=Path(os.environ.get('YOUTUBE_FAILURE_IMAGE_FIXTURE_DIR',str(default)))
        if not folder.is_dir():self.skipTest('Production failure fixtures not available in this checkout')
        failed=folder/'v2-failed-cover.png';before=hashlib.sha256(failed.read_bytes()).hexdigest()
        self.assert_code(failed.read_bytes(),'generated_cover_corrupt',generated=True)
        for name in ('v1-cover.png','v2-retry-accepted.jpg'):
            self.assertEqual(decode_cover((folder/name).read_bytes(),generated=True).mode,'RGB')
        self.assertEqual(hashlib.sha256(failed.read_bytes()).hexdigest(),before)


class GenerationFailureCase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'assets').mkdir()
        data=picture('JPEG',size=(240,360));asset_id='a'*32
        (self.root/'assets'/(asset_id+'.jpg')).write_bytes(data)
        self.task={'id':'b'*32,'material':{'macro_name':'Original title','macro_desc':'Synopsis'},'requirements':'Keep the original request',
                   'versions':[{'feedback':'Keep the original face'},{'feedback':'More cinematic'}],
                   'reference_cover':{'asset_id':asset_id,'sha256':hashlib.sha256(data).hexdigest()}}

    def run_generator(self, effect):
        with patch('features.youtube_auto_publish.runtime.subprocess.run',side_effect=effect):
            return generate_cover_factory(self.root)(self.task,{'number':3})

    def assert_failure(self,effect,code):
        with self.assertRaises(WorkflowError) as ctx:self.run_generator(effect)
        self.assertEqual(ctx.exception.code,code)
        self.assertNotIn('private-trace-secret',str(ctx.exception))

    def test_timeout_has_specific_error(self):
        def timeout(*args,**kwargs):raise subprocess.TimeoutExpired(args[0],1200,output='private-trace-secret')
        self.assert_failure(timeout,'cover_generation_timeout')

    def test_process_failure_and_missing_output_are_distinct(self):
        self.assert_failure(lambda *a,**k:SimpleNamespace(returncode=1),'cover_generation_failed')
        self.assert_failure(lambda *a,**k:SimpleNamespace(returncode=0),'cover_generation_output_missing')

    def test_oversized_output_is_not_misreported_as_ratio(self):
        def run(command,**kwargs):
            path=Path(command[command.index('-C')+1])/'cover.png'
            with path.open('wb') as target:target.truncate(32*1024*1024+1)
            return SimpleNamespace(returncode=0)
        self.assert_failure(run,'cover_generation_output_size')

    def test_audit_preserves_original_requirements_feedback_and_reference(self):
        def run(command,**kwargs):
            work=Path(command[command.index('-C')+1]);(work/'cover.png').write_bytes(picture())
            self.assertIn('Never alter PNG IHDR',kwargs['input'])
            self.assertIn('Keep the original request',kwargs['input'])
            return SimpleNamespace(returncode=0)
        with patch.dict(os.environ,{'MYSQL_PASSWORD':'private-trace-secret','FEISHU_SECRET':'private-trace-secret'}):self.run_generator(run)
        path=next((self.root/'generation').rglob('generation-request.json'));audit=json.loads(path.read_text(encoding='utf8'))
        self.assertEqual(audit['requirements'],self.task['requirements'])
        self.assertEqual(audit['revision_feedback'],['Keep the original face','More cinematic'])
        self.assertEqual(audit['reference_sha256'],self.task['reference_cover']['sha256'])
        self.assertNotIn('private-trace-secret',path.read_text())
        if os.name!='nt':self.assertEqual(path.stat().st_mode&0o777,0o600)



if __name__=='__main__':unittest.main(verbosity=2)
