import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.fb_page_assignment.sources import Reader, SourceError, Sources, literal, load_profiles


def language(value):
    value = str(value or '').strip().lower().replace('_', '-')
    return {'jp': 'ja', 'pt': 'pt-br'}.get(value, value)


def info(cid='Drama', lang='en'):
    return dict(kind='info', content_id=cid, app_id=1479, language=lang, series_code='S', resource_type=1)


def resource(cid='Drama', lang='en'):
    return dict(content_id=cid, app_id=1479, language=lang, series_code='S', labels='female,sweet')


def page(pid='101', lang='en', source_id=1, updated='2026-11-11 10:00:00'):
    return dict(kind='page', member_id=1, page_id=pid, language=lang, source_id=source_id,
                status=0, forbidden_status=0, is_permission=1, active_ad_count=5,
                fan_ccount=7, updated_at=updated)


def classify(resources, infos):
    labeled = {(r['content_id'], language(r['language'])) for r in resources}
    return {(r['content_id'], language(r['language'])):
            dict(audience='female' if (r['content_id'], language(r['language'])) in labeled else '__unknown__',
                 themes=['sweet_romance'] if (r['content_id'], language(r['language'])) in labeled else [])
            for r in infos}


class FakeReader:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def query(self, statements):
        self.calls.append(list(statements))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class SourcesTests(unittest.TestCase):
    def source(self, reader, classifier=classify):
        return Sources({}, reader=reader, classifier=classifier, normalize_language=language)

    def test_queries_read_current_exact_group_and_product_in_a_batch(self):
        reader = FakeReader([page(), info('A'), info('B')], [resource('A'), resource('B')])
        result = self.source(reader).load(['B', 'A', 'A'])
        self.assertEqual(set(result['dramas']), {'A', 'B'})
        self.assertEqual(len(reader.calls), 2)
        page_sql, info_sql = reader.calls[0]
        label_sql = reader.calls[1][0]
        self.assertIn('g.id=62', page_sql)
        self.assertIn('g.app_id=1479', page_sql)
        self.assertIn('g.is_delete=0', page_sql)
        self.assertIn('i.deleted_at=0', page_sql)
        self.assertIn("'language',i.language", page_sql)
        self.assertIn("'active_ad_count',p.active_ad_count", page_sql)
        self.assertIn('ads_facebook_page_group_ins', page_sql)
        self.assertIn('ads_drama_info', info_sql)
        self.assertIn('ads_drama_resource', label_sql)
        for sql in (info_sql, label_sql):
            self.assertIn('app_id=1479', sql)
            self.assertIn('content_id IN (0x41,0x42)', sql)
        # A second call must read current membership instead of caching Pages.
        reader.answers.extend([[page('102', 'fr'), info('A')], []])
        next_result = self.source(reader).load(['A'])
        self.assertEqual([p['page_id'] for p in next_result['pages']], ['102'])

    def test_id_encoding_does_not_put_raw_sql_into_queries(self):
        cid = "A'); DROP TABLE x; --"
        reader = FakeReader([info(cid)], [resource(cid)])
        self.source(reader).load([cid])
        for call in reader.calls:
            for sql in call:
                self.assertNotIn(cid, sql)
        self.assertEqual(literal(cid), '0x' + cid.encode().hex())

    def test_case_insensitive_sql_results_are_filtered_before_classification(self):
        captured = {}
        def classifier(resources, infos):
            captured.update(resources=resources, infos=infos)
            return classify(resources, infos)
        reader = FakeReader([info('Drama'), info('drama'), info('Other')],
                            [resource('Drama'), resource('drama'), resource('Other')])
        result = self.source(reader, classifier).load(['Drama'])
        self.assertEqual([r['content_id'] for r in captured['infos']], ['Drama'])
        self.assertEqual([r['content_id'] for r in captured['resources']], ['Drama'])
        self.assertEqual(set(result['dramas']), {'Drama'})

    def test_case_variant_does_not_make_a_missing_drama_exist(self):
        result = self.source(FakeReader([info('drama')], [resource('drama')])).load(['Drama'])
        self.assertFalse(result['dramas']['Drama']['exists'])

    def test_language_uses_exact_metadata_version_not_resource_language(self):
        result = self.source(FakeReader([info()], [resource(lang='fr')])).load(['Drama'])
        self.assertEqual(result['dramas']['Drama']['language'], 'en')
        self.assertEqual(result['dramas']['Drama']['audience'], '__unknown__')

    def test_conflicting_drama_languages_remain_unresolved(self):
        result = self.source(FakeReader([info(lang='en'), info(lang='fr')], [resource()])).load(['Drama'])
        drama = result['dramas']['Drama']
        self.assertTrue(drama['exists'])
        self.assertEqual((drama['language'], drama['audience']), ('', '__unknown__'))
        self.assertIn('drama_language_conflict', drama['warnings'])

    def test_normalized_aliases_do_not_create_a_false_language_conflict(self):
        result = self.source(FakeReader([info(lang='JP'), info(lang='ja')], [resource(lang='ja')])).load(['Drama'])
        self.assertEqual(result['dramas']['Drama']['language'], 'ja')
        self.assertEqual(result['dramas']['Drama']['warnings'], [])

    def test_label_read_failure_preserves_drama_language_and_pages(self):
        reader = FakeReader([page(), info()], SourceError('source_query_failed_3024'))
        result = self.source(reader).load(['Drama'])
        self.assertEqual(result['dramas']['Drama']['language'], 'en')
        self.assertTrue(result['dramas']['Drama']['exists'])
        self.assertEqual(result['dramas']['Drama']['audience'], '__unknown__')
        self.assertEqual(result['pages'][0]['page_id'], '101')
        self.assertIn('drama_labels_source_query_failed_3024', result['warnings'])

    def test_metadata_or_membership_failure_is_not_reported_as_empty_pool(self):
        reader = FakeReader(SourceError('source_query_timeout'))
        with self.assertRaisesRegex(SourceError, 'source_query_timeout'):
            self.source(reader).load(['Drama'])

    def test_conflicting_page_member_languages_are_excluded_not_blank_fallback(self):
        reader = FakeReader([page(lang='en'), page(lang='fr', source_id=2), info()], [])
        result = self.source(reader).load(['Drama'])
        self.assertEqual(len(result['pages']), 1)
        self.assertEqual(result['pages'][0]['status'], 1)
        self.assertEqual(result['pages'][0]['source_error'], 'page_language_invalid_or_conflicting')

    def test_blank_and_labeled_page_conflict_is_not_an_available_backup(self):
        reader = FakeReader([page(lang=''), page(lang='en', source_id=2), info()], [])
        result = self.source(reader).load(['Drama'])
        self.assertNotEqual(result['pages'][0]['status'], 0)

    def test_page_aliases_normalize_and_latest_metrics_win(self):
        older = page(lang='JP', source_id=5, updated='2026-11-10 10:00:00')
        newer = page(lang='ja', source_id=2)
        newer['active_ad_count'] = 9
        reader = FakeReader([older, newer, info()], [])
        result = self.source(reader).load(['Drama'])
        self.assertEqual(len(result['pages']), 1)
        self.assertEqual(result['pages'][0]['language'], 'ja')
        self.assertEqual(result['pages'][0]['status'], 0)
        self.assertEqual(result['pages'][0]['active_ad_count'], 9)


class ProfileSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def snapshot(self, version='v1', profiles=None, meta=None):
        path = self.root / (version + '.sqlite3')
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('CREATE TABLE profiles(page_id TEXT PRIMARY KEY,tree TEXT); CREATE TABLE metadata(payload TEXT);')
            db.execute('INSERT INTO metadata VALUES(?)', (json.dumps(meta or {'version': version}),))
            for pid, value in (profiles or {}).items():
                db.execute('INSERT INTO profiles VALUES(?,?)', (pid, value if isinstance(value, str) else json.dumps(value)))
        return path

    def point(self, version='v1', database=None):
        (self.root / 'current.json').write_text(json.dumps({'version': version, 'database': database or version + '.sqlite3'}), encoding='utf-8')

    def test_missing_profile_is_allowed_and_other_page_profile_is_not_returned(self):
        self.snapshot(profiles={'101': {'page_id': '101', 'status': 'ready'}, '102': {'page_id': '102'}})
        self.point()
        found, meta, warnings = load_profiles(['101', '999'], self.root)
        self.assertEqual(set(found), {'101'})
        self.assertEqual(meta['version'], 'v1')
        self.assertEqual(warnings, [])

    def test_missing_or_bad_snapshot_returns_visible_unavailable(self):
        for contents in (None, '{broken', json.dumps({'version': 'v1', 'database': 'missing.sqlite3'})):
            if contents is not None:
                (self.root / 'current.json').write_text(contents, encoding='utf-8')
            self.assertEqual(load_profiles(['101'], self.root), ({}, {}, ['profile_snapshot_unavailable']))

    def test_bad_sqlite_file_returns_visible_unavailable(self):
        (self.root / 'v1.sqlite3').write_text('not sqlite', encoding='utf-8')
        self.point()
        self.assertEqual(load_profiles(['101'], self.root), ({}, {}, ['profile_snapshot_unavailable']))

    def test_invalid_page_json_does_not_hide_other_profiles(self):
        self.snapshot(profiles={'101': '{broken', '102': {'page_id': '102', 'status': 'ready'}, '103': '[]'})
        self.point()
        found, meta, warnings = load_profiles(['101', '102', '103'], self.root)
        self.assertEqual(set(found), {'102'})
        self.assertEqual(set(warnings), {'profile_invalid:101', 'profile_invalid:103'})
        self.assertEqual(meta['version'], 'v1')

    def test_snapshot_version_mismatch_is_unavailable(self):
        self.snapshot(meta={'version': 'different'})
        self.point()
        self.assertEqual(load_profiles(['101'], self.root), ({}, {}, ['profile_snapshot_unavailable']))

    def test_one_call_pins_current_version_across_multiple_id_chunks(self):
        ids = [str(i) for i in range(1, 102)]
        self.snapshot('v1', {pid: {'page_id': pid, 'marker': 'old'} for pid in ids})
        self.snapshot('v2', {pid: {'page_id': pid, 'marker': 'new'} for pid in ids})
        self.point('v1')
        original = Path.read_text
        reads = []
        def read_text(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path.name == 'current.json':
                reads.append(path)
                self.point('v2')
            return value
        with patch.object(Path, 'read_text', read_text):
            found, meta, warnings = load_profiles(ids, self.root)
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(found), 101)
        self.assertEqual(meta['version'], 'v1')
        self.assertEqual({p['marker'] for p in found.values()}, {'old'})
        self.assertEqual(warnings, [])

    def test_snapshot_path_cannot_escape_root(self):
        self.point(database='../elsewhere.sqlite3')
        self.assertEqual(load_profiles(['101'], self.root), ({}, {}, ['profile_snapshot_unavailable']))

    def test_profile_identity_must_match_its_index_row(self):
        self.snapshot(profiles={'101': {'page_id': '999', 'status': 'ready'}, '102': {'status': 'ready'}})
        self.point()
        found, meta, warnings = load_profiles(['101', '102'], self.root)
        self.assertEqual(found, {})
        self.assertEqual(set(warnings), {'profile_invalid:101', 'profile_invalid:102'})

    def test_snapshot_connection_is_closed_before_return(self):
        path = self.snapshot(profiles={'101': {'page_id': '101'}})
        self.point()
        connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
        self.addCleanup(connection.close)
        with patch('features.fb_page_assignment.sources.sqlite3.connect', return_value=connection):
            load_profiles(['101'], self.root)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')


class ReaderTests(unittest.TestCase):
    ENV = dict(ADMIN_MAPPING_MYSQL_HOST='101.32.56.53', ADMIN_MAPPING_MYSQL_PORT='63350',
               ADMIN_MAPPING_MYSQL_USER='test-user', ADMIN_MAPPING_MYSQL_PASSWORD='test-password')

    def test_writer_endpoint_is_rejected(self):
        with self.assertRaisesRegex(SourceError, 'invalid_reader_endpoint'):
            Reader({**self.ENV, 'ADMIN_MAPPING_MYSQL_PORT': '63353'})

    def test_reader_preserves_fifo_gate_and_readonly_boundary(self):
        encoded = json.dumps({'ok': 1}).encode().hex()
        result = SimpleNamespace(returncode=0, stdout='1\n' + encoded + '\n', stderr='')
        with patch.dict('os.environ', {'SQL_GATE_BYPASS': '1'}), patch('subprocess.run', return_value=result) as run:
            self.assertEqual(Reader(self.ENV).query(['SELECT 1']), [{'ok': 1}])
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], '/usr/bin/mysql')
        self.assertNotIn('SQL_GATE_BYPASS', kwargs['env'])
        self.assertIn('START TRANSACTION READ ONLY', kwargs['input'])
        self.assertIn('MAX_EXECUTION_TIME=8000', kwargs['input'])
        self.assertNotIn('test-password', str(args))

    def test_non_readonly_endpoint_response_and_timeout_are_errors(self):
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='0\n', stderr='')):
            with self.assertRaisesRegex(SourceError, 'source_query_failed'):
                Reader(self.ENV).query(['SELECT 1'])
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('mysql', 42)):
            with self.assertRaisesRegex(SourceError, 'source_query_timeout'):
                Reader(self.ENV).query(['SELECT 1'])


if __name__ == '__main__':
    unittest.main()
