#!/usr/bin/env python3
"""Import frozen A-group receipts into the replica ledger without network writes.

Default is read-only validation. --apply calls only the historical-import API;
it never creates a pending submission or loads the B-group credential file.
"""
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MAX_EVIDENCE_BYTES = 512 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    payload = path.read_bytes()
    require(0 < len(payload) <= MAX_EVIDENCE_BYTES, 'evidence_size_invalid')
    result = json.loads(payload.decode('utf-8'))
    require(isinstance(result, dict), 'evidence_object_required')
    return payload, result


def normalized_language(value):
    return str(value or '').strip().lower().replace('_', '-')


def load_evidence(directory):
    directory = Path(directory).resolve(strict=True)
    require(directory.is_dir(), 'audit_directory_required')
    raw_request, request = read_json(directory / 'request.json')
    _, started = read_json(directory / 'attempt_started.json')
    _, response = read_json(directory / 'response.json')
    _, result = read_json(directory / 'result.json')
    digest = hashlib.sha256(raw_request).hexdigest()
    require(started.get('request_sha256') == digest == result.get('request_sha256'),
            'request_evidence_hash_mismatch')
    require(started.get('endpoint') == 'https://marketing.mydramawave.com/api/internal/material/a-group-task-create',
            'unexpected_evidence_endpoint')
    require(response.get('http_status') == result.get('http_status') == 200, 'unexpected_historical_http_status')
    body = response.get('body')
    require(isinstance(body, dict) and body == result.get('response'), 'response_evidence_mismatch')
    require(type(body.get('code')) is int and body['code'] in (200, 400, 401), 'unsupported_historical_outcome')
    require(result.get('status') in ('accepted', 'rejected', 'response_received_not_accepted'), 'historical_outcome_not_final')
    require((body['code'] == 200) == (result['status'] == 'accepted'), 'historical_status_mismatch')
    materials = request.get('orig_materials')
    require(isinstance(materials, list) and len(materials) == 1 and isinstance(materials[0], dict),
            'single_historical_material_required')
    require(isinstance(request.get('operator'), str) and request['operator'].strip(), 'historical_operator_missing')
    timestamp = datetime.fromisoformat(str(started.get('started_at_utc', '')).replace('Z', '+00:00'))
    require(timestamp.tzinfo is not None, 'historical_timestamp_needs_timezone')
    if body['code'] == 200:
        data = body.get('data')
        require(isinstance(data, dict) and data.get('operator') == request['operator'], 'historical_receipt_operator_mismatch')
        require(type(data.get('task_id')) is int and data['task_id'] > 0, 'historical_task_id_missing')
        items = data.get('items')
        require(isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict), 'historical_receipt_item_missing')
        require(str(items[0].get('material_id')) == str(materials[0].get('material_id')), 'historical_receipt_material_mismatch')
        require(data.get('task_material_ids') == [items[0].get('task_material_id')], 'historical_receipt_task_material_mismatch')
        require(set(data.get('target_langs') or []).issubset(set(request.get('target_langs') or [])), 'historical_receipt_language_mismatch')
    return {'directory': str(directory), 'request': request, 'response': body,
            'request_sha256': digest, 'submitted_at': int(timestamp.timestamp()),
            'import_source': str(directory) + '#sha256=' + digest}


def validate_association(db_path, task_id, evidence, expected_resource_id=None):
    db_path = Path(db_path).resolve(strict=True)
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        task = db.execute('SELECT * FROM ve_task WHERE id=?', (task_id,)).fetchone()
        require(task is not None and task['kind'] == 'render' and task['status'] == 'completed', 'completed_task_required')
        task_data = json.loads(task['data'])
        output_id = task_data.get('output_id')
        output = db.execute('SELECT * FROM ve_output WHERE id=? AND task_id=?', (output_id, task_id)).fetchone()
        upload = db.execute('SELECT * FROM ve_material_upload WHERE output_id=? AND task_id=?', (output_id, task_id)).fetchone()
        require(output is not None and output['status'] == 'ready', 'verified_output_required')
        require(upload is not None and upload['status'] == 'succeeded', 'successful_material_upload_required')
        output_data, upload_data = json.loads(output['data']), json.loads(upload['data'])
        frozen = upload_data.get('request') or {}
        artifact = output_data.get('artifact') or {}
        require(artifact and artifact == upload_data.get('artifact'), 'historical_artifact_mismatch')
        request = evidence['request']
        material = request['orig_materials'][0]
        require(str(material.get('material_id')) == str(upload_data.get('material_id')), 'historical_material_id_mismatch')
        require(material.get('url') == frozen.get('url') and material.get('name') == frozen.get('name'),
                'historical_material_identity_mismatch')
        require(type(material.get('duration')) is int and material['duration'] == artifact.get('duration_ms'),
                'historical_material_duration_mismatch')
        require([normalized_language(x) for x in request.get('series_langs', [])] ==
                [normalized_language(frozen.get('language'))], 'historical_source_language_mismatch')
        if expected_resource_id is not None:
            require(request.get('resource_id') == expected_resource_id, 'historical_resource_id_mismatch')
        return {'task_id': task_id, 'output_id': output_id, 'material_id': str(upload_data['material_id']),
                'task_owner_preserved': task['owner']}


def import_evidence(replication, task_id, evidence, default_operator_override=None):
    override = default_operator_override if evidence['response']['code'] == 200 and evidence['request']['operator'] == default_operator_override else None
    return replication.import_history(task_id, evidence['request'], evidence['response'],
        import_source=evidence['import_source'], submitted_at=evidence['submitted_at'],
        default_operator_override=override)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='/mnt/data-disk/video-editing/state.sqlite3')
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--audit-dir', action='append', required=True)
    parser.add_argument('--expected-resource-id', type=int)
    parser.add_argument('--default-operator-override')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    entries = sorted((load_evidence(path) for path in args.audit_dir), key=lambda item: item['submitted_at'])
    require(len({item['import_source'] for item in entries}) == len(entries), 'duplicate_audit_source')
    if args.default_operator_override:
        require(any(e['response']['code'] == 200 and e['request']['operator'] == args.default_operator_override for e in entries),
                'operator_override_requires_matching_success_receipt')
    associations = [validate_association(args.database, args.task_id, item, args.expected_resource_id) for item in entries]
    report = {'ok': True, 'applied': args.apply, 'task_id': args.task_id,
              'network_posts': 0, 'default_operator_override': args.default_operator_override, 'records': []}
    replication = None
    if args.apply:
        import os
        os.environ['VIDEO_EDITING_DB'] = str(Path(args.database).resolve())
        import app
        from features.video_editing.service import get_service
        replication = get_service(app).material_replication
    for item, association in zip(entries, associations):
        row = {'import_source': item['import_source'], 'request_sha256': item['request_sha256'],
               'operator': item['request']['operator'], 'code': item['response']['code'],
               'submitted_at': item['submitted_at'], 'material_id': association['material_id']}
        if replication is not None:
            saved = import_evidence(replication, args.task_id, item, args.default_operator_override)
            require(saved['status'] in ('accepted', 'partial', 'rejected'), 'imported_record_must_not_be_pending')
            row.update(record_id=saved['id'], status=saved['status'], b_task_id=saved.get('b_task_id'))
        report['records'].append(row)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never dump archive contents, raw HTTP details or app configuration.
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).replace('_', '').isalnum() else type(exc).__name__
        print(json.dumps({'ok': False, 'error': reason}, ensure_ascii=False))
        raise SystemExit(1)
