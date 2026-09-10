"""Offline evidence that real image bytes accompany every cover generation."""
import copy
import hashlib
import io
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from features.youtube_auto_publish import reference
from features.youtube_auto_publish.runtime import generate_cover_factory
from features.youtube_auto_publish.templates import WorkflowError


def picture(fmt='JPEG', size=(240, 360)):
    out = io.BytesIO()
    Image.new('RGB', size, '#406090').save(out, format=fmt)
    return out.getvalue()


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.status = status
        self.headers = {'Content-Type': 'image/jpeg', 'Content-Length': str(len(body))} if headers is None else headers
        self.stream = io.BytesIO(body)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        return self.stream.read(size)


class ReferenceDownloadCase(unittest.TestCase):
    url = 'https://static-v1.mydramawave.com/drama/original.jpg'
    endpoints = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]

    def download(self, response, **kwargs):
        connection = Mock(io_socket=Mock())
        connection.getresponse.return_value = response
        with patch.object(reference.socket, 'getaddrinfo', return_value=self.endpoints), patch.object(reference, '_PinnedHTTPSConnection', return_value=connection) as factory:
            value = reference.fetch_reference_cover_factory(**kwargs)(self.url)
        return value, connection, factory

    def assert_download_error(self, response, **kwargs):
        with self.assertRaises(WorkflowError):
            self.download(response, **kwargs)

    def test_http_and_legacy_cdn_are_normalized(self):
        self.assertEqual(reference.normalize_reference_url('http://static.mydramawave.com/a.png?x=1'), 'https://static-v1.mydramawave.com/a.png?x=1')

    def test_invalid_urls_fail_without_network(self):
        bad = ['', 'https://untrusted.example/a.png', 'file:///etc/passwd',
               'https://user:password@static-v1.mydramawave.com/a.jpg',
               'https://@static-v1.mydramawave.com/a.jpg',
               'https://static-v1.mydramawave.com:444/a.jpg',
               'https://static-v1.mydramawave.com/a.jpg#secret',
               'https://static-v1.mydramawave.com/\r\nHost:local',
               'https://static-v1.mydramawave.com\\@example.org/a.jpg']
        with patch.object(reference.socket, 'getaddrinfo') as dns:
            for value in bad:
                with self.subTest(url=value):
                    self.assertEqual(reference.normalize_reference_url(value), '')
                    with self.assertRaises(WorkflowError):
                        reference.fetch_reference_cover_factory()(value)
            dns.assert_not_called()

    def test_download_returns_original_bytes_and_pins_public_ip(self):
        data = picture()
        result, connection, factory = self.download(FakeResponse(data))
        self.assertEqual(result, data)
        self.assertEqual(factory.call_args.args[:2], ('static-v1.mydramawave.com', self.endpoints[0]))
        self.assertEqual(connection.request.call_args.args, ('GET', '/drama/original.jpg'))
        self.assertNotIn('Authorization', connection.request.call_args.kwargs['headers'])
        connection.close.assert_called_once()

    def test_known_length_does_not_touch_socket_after_last_body_read(self):
        data, closed = picture(), False
        connection = Mock(io_socket=Mock())
        response = FakeResponse(data)
        original_read = response.read1
        def read_and_close(size):
            nonlocal closed
            result = original_read(size)
            closed = response.stream.tell() == len(data)
            return result
        def socket_timeout(*_):
            if closed:
                raise OSError('Bad file descriptor')
        response.read1 = read_and_close
        connection.getresponse.return_value = response
        connection.io_socket.settimeout.side_effect = socket_timeout
        with patch.object(reference.socket, 'getaddrinfo', return_value=self.endpoints), patch.object(reference, '_PinnedHTTPSConnection', return_value=connection):
            self.assertEqual(reference.fetch_reference_cover_factory()(self.url), data)

    def test_private_dns_and_mixed_dns_fail_closed(self):
        for records in [[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))], self.endpoints + [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.8', 443))]]:
            with patch.object(reference.socket, 'getaddrinfo', return_value=records), patch.object(reference, '_PinnedHTTPSConnection') as connection:
                with self.assertRaises(WorkflowError):
                    reference.fetch_reference_cover_factory()(self.url)
                connection.assert_not_called()

    def test_redirects_are_rejected(self):
        self.assert_download_error(FakeResponse(b'', status=302, headers={'Location': 'https://127.0.0.1/secret'}))

    def test_announced_oversized_reference_is_rejected(self):
        self.assert_download_error(FakeResponse(picture(), headers={'Content-Length': str(8*1024*1024+1)}))

    def test_stream_oversize_is_rejected_without_content_length(self):
        self.assert_download_error(FakeResponse(picture(), headers={}), max_bytes=128)

    def test_invalid_mime_or_encoding_are_rejected(self):
        self.assert_download_error(FakeResponse(picture(), headers={'Content-Type': 'text/html'}))
        self.assert_download_error(FakeResponse(picture(), headers={'Content-Encoding': 'gzip'}))

    def test_empty_and_incomplete_images_are_rejected(self):
        self.assert_download_error(FakeResponse(b''))
        self.assert_download_error(FakeResponse(picture(), headers={'Content-Length': str(len(picture())+1)}))
        self.assert_download_error(FakeResponse(b'not an image'))
        self.assert_download_error(FakeResponse(picture()[:-64]))

    def test_png_and_webp_are_allowed_but_animation_is_not(self):
        for fmt in ('PNG', 'WEBP'):
            data = picture(fmt)
            value, _, _ = self.download(FakeResponse(data, headers={}))
            self.assertEqual(value, data)
        out = io.BytesIO()
        Image.new('RGB', (240, 360), 'red').save(out, format='WEBP', save_all=True,
            append_images=[Image.new('RGB', (240, 360), 'blue')], duration=100, loop=0)
        self.assert_download_error(FakeResponse(out.getvalue(), headers={}))

    def test_timeout_and_read_errors_close_connection(self):
        connection = Mock(io_socket=Mock())
        connection.request.side_effect = TimeoutError('private connection details')
        with patch.object(reference.socket, 'getaddrinfo', return_value=self.endpoints), patch.object(reference, '_PinnedHTTPSConnection', return_value=connection):
            with self.assertRaises(WorkflowError) as ctx:
                reference.fetch_reference_cover_factory()(self.url)
        self.assertNotIn('private', str(ctx.exception))
        connection.close.assert_called_once()

    def test_dns_wait_obeys_total_deadline(self):
        release, finished = threading.Event(), threading.Event()
        def stalled_dns(*args, **kwargs):
            try:
                release.wait(1)
                return self.endpoints
            finally:
                finished.set()
        with patch.object(reference.socket, 'getaddrinfo', side_effect=stalled_dns), patch.object(reference, '_PinnedHTTPSConnection') as connection:
            started = time.monotonic()
            try:
                with self.assertRaises(WorkflowError):
                    reference.fetch_reference_cover_factory(timeout=.04)(self.url)
                self.assertLess(time.monotonic()-started, .5)
                connection.assert_not_called()
            finally:
                release.set()
                finished.wait(1)

    def test_header_wait_is_interrupted_by_total_deadline(self):
        interrupted = threading.Event()
        connection = Mock(io_socket=Mock())
        connection.io_socket.shutdown.side_effect = lambda *_: interrupted.set()
        def stalled_headers():
            interrupted.wait(1)
            raise TimeoutError('header deadline')
        connection.getresponse.side_effect = stalled_headers
        with patch.object(reference.socket, 'getaddrinfo', return_value=self.endpoints), patch.object(reference, '_PinnedHTTPSConnection', return_value=connection):
            started = time.monotonic()
            with self.assertRaises(WorkflowError):
                reference.fetch_reference_cover_factory(timeout=.04)(self.url)
            self.assertLess(time.monotonic()-started, .5)
            self.assertTrue(interrupted.is_set())
        connection.close.assert_called_once()

    def test_tls_uses_checked_address_and_original_hostname(self):
        raw, tls, context = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = tls
        with patch.object(reference.ssl, 'create_default_context', return_value=context), patch.object(reference.socket, 'socket', return_value=raw):
            connection = reference._PinnedHTTPSConnection('static-v1.mydramawave.com', self.endpoints[0], reference.time.monotonic()+15)
            connection.connect()
        raw.connect.assert_called_once_with(('8.8.8.8', 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname='static-v1.mydramawave.com', do_handshake_on_connect=False)
        tls.do_handshake.assert_called_once()
        self.assertIs(connection.sock, tls)


class GeneratorReferenceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'assets').mkdir()
        self.asset_id = 'a'*32
        self.original = picture()
        self.path = self.root/'assets'/(self.asset_id+'.jpg')
        self.path.write_bytes(self.original)
        self.task = {'id': 'b'*32, 'material': {'macro_name':'Test drama', 'macro_desc':'Synopsis'},
                     'requirements':'Cinematic image with warm light', 'versions':[],
                     'reference_cover': {'asset_id':self.asset_id, 'sha256':hashlib.sha256(self.original).hexdigest()}}
        self.calls = []

    def fake_run(self, command, **kwargs):
        work = Path(command[command.index('-C')+1])
        attached = Path(command[command.index('--image')+1])
        self.calls.append((attached.read_bytes(), kwargs['input'], command, kwargs))
        (work/'cover.png').write_bytes(picture('PNG', (1536,864)))
        return SimpleNamespace(returncode=0)

    def generate(self, task=None, number=1):
        with patch('features.youtube_auto_publish.runtime.subprocess.run', side_effect=self.fake_run):
            return generate_cover_factory(self.root)(task or self.task, {'number':number})

    def test_reference_is_real_attachment_and_imagegen_local_reference_is_required(self):
        output = self.generate()
        data, prompt, cmd, kwargs = self.calls[0]
        self.assertEqual(data, self.original)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertTrue(output.startswith(b'\x89PNG'))
        self.assertIn('view_image', prompt)
        self.assertIn('image_gen', prompt)
        self.assertIn('referenced_image_paths', prompt)
        self.assertIn(str(Path(cmd[cmd.index('--image')+1])), prompt)
        self.assertIn('Never generate from text alone', prompt)
        self.assertIn('16:9', prompt)
        self.assertEqual(kwargs['timeout'], 1200)

    def test_redo_reuses_identical_original_and_all_revision_feedback(self):
        self.generate()
        redo = copy.deepcopy(self.task)
        redo['versions'] = [{'feedback':'Keep original faces'}, {'feedback':'Brighter background'}]
        self.generate(redo, number=3)
        self.assertEqual(self.calls[0][0], self.calls[1][0])
        self.assertIn('Keep original faces', self.calls[1][1])
        self.assertIn('Brighter background', self.calls[1][1])
        self.assertNotEqual(self.calls[0][2], self.calls[1][2])

    def test_missing_reference_never_invokes_generator(self):
        self.task.pop('reference_cover')
        with self.assertRaises(WorkflowError) as ctx:
            self.generate()
        self.assertEqual(ctx.exception.code, 'reference_cover_missing')
        self.assertEqual(self.calls, [])

    def test_changed_hash_or_missing_asset_never_invokes_generator(self):
        self.path.write_bytes(picture(size=(250,360)))
        with self.assertRaises(WorkflowError):
            self.generate()
        self.path.unlink()
        with self.assertRaises(WorkflowError):
            self.generate()
        self.assertEqual(self.calls, [])

    def test_malformed_ids_and_hashes_fail_closed(self):
        for key, value in [('asset_id','../secret'), ('sha256','z'*64)]:
            task = copy.deepcopy(self.task)
            task['reference_cover'][key] = value
            with self.assertRaises(WorkflowError):
                self.generate(task)
        task = copy.deepcopy(self.task)
        task['id'] = '../elsewhere'
        with self.assertRaises(WorkflowError):
            self.generate(task)
        self.assertEqual(self.calls, [])

    def test_asset_symlink_is_not_followed(self):
        original = self.root/'outside.jpg'
        original.write_bytes(self.original)
        self.path.unlink()
        try:
            self.path.symlink_to(original)
        except OSError:
            self.skipTest('OS disallows symlink creation')
        with self.assertRaises(WorkflowError):
            self.generate()
        self.assertEqual(self.calls, [])

    def test_non_jpeg_frozen_file_is_rejected_even_with_matching_hash(self):
        data = picture('PNG')
        self.path.write_bytes(data)
        self.task['reference_cover']['sha256'] = hashlib.sha256(data).hexdigest()
        with self.assertRaises(WorkflowError):
            self.generate()
        self.assertEqual(self.calls, [])

    def test_generator_secrets_stay_out_of_environment(self):
        with patch.dict(os.environ, {'MYSQL_PASSWORD':'should-not-leak','FEISHU_SECRET':'should-not-leak','GOOGLE_REFRESH_TOKEN':'should-not-leak'}):
            self.generate()
        env = self.calls[0][3]['env']
        for key in ('MYSQL_PASSWORD','FEISHU_SECRET','GOOGLE_REFRESH_TOKEN'):
            self.assertNotIn(key, env)

    def test_failed_generator_does_not_return_reference_as_output(self):
        with patch('features.youtube_auto_publish.runtime.subprocess.run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(WorkflowError) as ctx:
                generate_cover_factory(self.root)(self.task, {'number':1})
        self.assertEqual(ctx.exception.code, 'cover_generation_failed')

    def test_generator_cannot_replace_its_reference_copy(self):
        def corrupt_reference(command, **kwargs):
            result = self.fake_run(command, **kwargs)
            Path(command[command.index('--image')+1]).write_bytes(picture(size=(250,360)))
            return result
        with patch('features.youtube_auto_publish.runtime.subprocess.run', side_effect=corrupt_reference):
            with self.assertRaises(WorkflowError) as ctx:
                generate_cover_factory(self.root)(self.task, {'number':1})
        self.assertEqual(ctx.exception.code, 'cover_generation_failed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
