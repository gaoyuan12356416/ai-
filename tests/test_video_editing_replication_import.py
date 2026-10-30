import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from scripts.import_video_editing_material_replication_history import load_evidence, validate_association, import_evidence


class HistoricalImportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = {'operator': 'susiqi', 'resource_id': 28331, 'series_langs': ['en'], 'target_langs': ['es'],
                        'orig_materials': [{'material_id': 7937212, 'name': 'source.mp4',
                                            'url': 'https://media.example/source.mp4', 'duration': 1789867}]}
        self.body = {'code': 200, 'data': {'operator': 'susiqi', 'task_id': 1732014,
                     'task_material_ids': [2080677], 'target_langs': ['es'],
                     'items': [{'material_id': 7937212, 'task_material_id': 2080677}]}}
        self.write_archive()
        self.db_path = self.root / 'state.sqlite3'
        self.artifact = {'duration_ms': 1789867, 'width': 720, 'height': 1280}
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript('''CREATE TABLE ve_task(id TEXT, kind TEXT, status TEXT, owner TEXT, data TEXT);
                CREATE TABLE ve_output(id TEXT, task_id TEXT, status TEXT, data TEXT);
                CREATE TABLE ve_material_upload(output_id TEXT, task_id TEXT, status TEXT, data TEXT);''')
            db.execute('INSERT INTO ve_task VALUES(?,?,?,?,?)', ('task', 'render', 'completed', 'original-owner', json.dumps({'output_id': 'output'})))
            db.execute('INSERT INTO ve_output VALUES(?,?,?,?)', ('output', 'task', 'ready', json.dumps({'artifact': self.artifact})))
            db.execute('INSERT INTO ve_material_upload VALUES(?,?,?,?)', ('output', 'task', 'succeeded', json.dumps({
                'material_id': 7937212, 'artifact': self.artifact,
                'request': {'name': 'source.mp4', 'url': 'https://media.example/source.mp4', 'language': 'EN', 'app_id': 3214}})))

    def write_json(self, name, data):
        (self.root / name).write_text(json.dumps(data), encoding='utf-8')

    def write_archive(self, rejected=False):
        self.write_json('request.json', self.request)
        digest = hashlib.sha256((self.root / 'request.json').read_bytes()).hexdigest()
        self.write_json('attempt_started.json', {'request_sha256': digest,
                        'endpoint': 'https://marketing.mydramawave.com/api/internal/material/a-group-task-create',
                        'started_at_utc': '2026-10-30T02:31:44Z'})
        self.write_json('response.json', {'http_status': 200, 'body': self.body})
        self.write_json('result.json', {'http_status': 200, 'response': self.body, 'request_sha256': digest,
                        'status': 'response_received_not_accepted' if rejected else 'accepted'})

    def test_accepted_archive_matches_frozen_output_without_changing_source_ownership(self):
        before = self.db_path.read_bytes()
        evidence = load_evidence(self.root)
        association = validate_association(self.db_path, 'task', evidence, 28331)
        self.assertEqual(association['task_owner_preserved'], 'original-owner')
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(evidence['submitted_at'], 1793327504)

    def test_business_failure_is_importable_as_historical_receipt(self):
        self.request['operator'] = 'gaoyuan'
        self.body = {'code': 400, 'message': 'operator not bound'}
        self.write_archive(rejected=True)
        self.assertEqual(load_evidence(self.root)['response']['code'], 400)

    def test_tampered_raw_request_and_disagreeing_receipts_are_rejected(self):
        with (self.root / 'request.json').open('a', encoding='utf-8') as f:
            f.write('\n')
        with self.assertRaisesRegex(ValueError, 'request_evidence_hash_mismatch'):
            load_evidence(self.root)
        self.write_archive()
        self.write_json('response.json', {'http_status': 200, 'body': {'code': 400}})
        with self.assertRaisesRegex(ValueError, 'response_evidence_mismatch'):
            load_evidence(self.root)

    def test_wrong_output_or_resource_cannot_attach_receipt(self):
        evidence = load_evidence(self.root)
        with self.assertRaisesRegex(ValueError, 'historical_resource_id_mismatch'):
            validate_association(self.db_path, 'task', evidence, 3214)
        evidence['request']['orig_materials'][0]['duration'] += 1
        with self.assertRaisesRegex(ValueError, 'historical_material_duration_mismatch'):
            validate_association(self.db_path, 'task', evidence, 28331)

    def test_override_only_applies_to_matching_accepted_receipt(self):
        backend = Mock()
        evidence = load_evidence(self.root)
        import_evidence(backend, 'task', evidence, 'susiqi')
        self.assertEqual(backend.import_history.call_args.kwargs['default_operator_override'], 'susiqi')
        import_evidence(backend, 'task', evidence, 'someone_else')
        self.assertIsNone(backend.import_history.call_args.kwargs['default_operator_override'])
        evidence['response'] = {'code': 400}
        import_evidence(backend, 'task', evidence, 'susiqi')
        self.assertIsNone(backend.import_history.call_args.kwargs['default_operator_override'])

    def test_wrong_receipt_operator_or_material_is_rejected(self):
        for field, value, error in [('operator', 'another', 'historical_receipt_operator_mismatch'),
                                     ('items', [{'material_id': 7, 'task_material_id': 2080677}], 'historical_receipt_material_mismatch')]:
            original = self.body['data'][field]
            self.body['data'][field] = value
            self.write_archive()
            with self.assertRaisesRegex(ValueError, error):
                load_evidence(self.root)
            self.body['data'][field] = original


if __name__ == '__main__':
    unittest.main()
