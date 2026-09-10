#!/usr/bin/env python3
"""One real image-generation check in private scratch; never publish or notify.

Run only from an exact GitHub-verified release. Inspect the saved tool trace and
the source/result images manually: text mentioning image_gen is not call proof.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

from PIL import Image

VALIDATION = Path('/mnt/data-disk/deploy/youtube-auto-publish/validation')
DISK_UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
REQUIREMENTS = '16:9的尺寸，要尽可能奢华一些，保留原剧封面的人物身份、服装与主要视觉特征'


def private_file(path, mode='w'):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, mode, **({'encoding':'utf-8'} if 'b' not in mode else {}))


def save_text(path, value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    with private_file(path) as output:
        output.write(value or '')


def save_json(path, value):
    save_text(path, json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def audit_image_calls(stdout):
    """Keep typed tool events only; never treat quoted prompt text as evidence."""
    entries = []
    direct_names = {'image_gen', 'imagegen', 'image_gen.imagegen', 'image_gen__imagegen', 'tools.image_gen__imagegen'}
    event_types = {'tool_call', 'function_call', 'mcp_tool_call', 'tool_use'}
    def visit(value, event_type, location):
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, event_type, location+[index])
        elif isinstance(value, dict):
            kind = str(value.get('type', ''))
            name = str(value.get('name') or value.get('tool_name') or value.get('tool') or '')
            server = str(value.get('server') or value.get('server_name') or '')
            arguments = value.get('arguments', value.get('input', value.get('parameters')))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (ValueError, TypeError):
                    pass
            if kind in event_types and (name in direct_names or (server == 'image_gen' and name == 'imagegen')):
                safe_args = ({key:arguments[key] for key in ('prompt', 'referenced_image_paths', 'num_last_images_to_include') if key in arguments}
                             if isinstance(arguments, dict) else arguments)
                entries.append({'evidence_kind':'direct_image_tool_event', 'event_type':event_type,
                                'item_type':kind, 'tool':name, 'server':server,
                                'arguments':safe_args, 'json_location':location})
            elif kind in event_types and name in ('functions.exec', 'exec'):
                code = arguments.get('code', arguments.get('source', '')) if isinstance(arguments, dict) else str(arguments or '')
                if re.search(r'\b(?:tools\.)?(?:image_gen__imagegen|image_gen\.imagegen)\s*\(', code):
                    # A wrapper invocation needs full-trace review. It does not
                    # establish that the nested tool actually executed.
                    entries.append({'evidence_kind':'wrapper_requires_manual_review', 'event_type':event_type,
                                    'item_type':kind, 'tool':name, 'json_location':location})
            for key, child in value.items():
                if isinstance(child, (dict, list)) and key not in ('arguments', 'input', 'parameters'):
                    visit(child, event_type, location+[key])
    for line_number, line in enumerate(stdout.splitlines(), 1):
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        visit(value, str(value.get('type', '')) if isinstance(value, dict) else '', [line_number])
    return {'requires_manual_trace_review':True, 'image_tool_events':entries,
            'direct_event_count':sum(item['evidence_kind']=='direct_image_tool_event' for item in entries),
            'note':'Only an actual tool event whose input contains the reference path supports reference-use evidence. Prompt text and output existence alone do not.'}


def prepare_workspace(value, stage):
    if os.name != 'posix' or os.geteuid() != 0:
        raise RuntimeError('linux_root_required')
    marker = stage/'.github-verified-commit'
    commit = marker.read_text(encoding='utf-8').strip()
    if re.fullmatch('[a-f0-9]{40}', commit) is None:
        raise RuntimeError('github_verified_release_required')
    disk = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk'], text=True, timeout=10).strip()
    if disk != DISK_UUID:
        raise RuntimeError('data_disk_uuid_mismatch')
    path = Path(value)
    if (not path.is_absolute() or path.parent != VALIDATION
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', path.name) is None
            or path.resolve() != path or path.exists() or path.is_symlink()):
        raise RuntimeError('fresh_validation_child_required')
    # Check every existing ancestor before creating anything under the mount.
    for parent in (VALIDATION, *VALIDATION.parents):
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise RuntimeError('validation_parent_invalid')
    VALIDATION.mkdir(mode=0o700, parents=False, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=False)
    return path, commit


def load_live_environment(work):
    pid = subprocess.check_output(['systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value'], text=True, timeout=10).strip()
    if re.fullmatch(r'[1-9][0-9]*', pid) is None:
        raise RuntimeError('active_api_required')
    values = dict(item.split('=',1) for item in Path('/proc',pid,'environ').read_bytes().decode().split('\0') if '=' in item)
    os.environ.update(values)
    return values


def forbid(*args, **kwargs):
    raise AssertionError('publishing_and_notifications_forbidden_in_validation')


def validate(material_id, work, stage, commit):
    active_env = load_live_environment(work)
    sys.path.insert(0, str(stage))
    from features.youtube_auto_publish import runtime
    from features.youtube_auto_publish.drama import DramaMetadataResolver
    from features.youtube_auto_publish.reference import fetch_reference_cover_factory
    from features.youtube_auto_publish.service import YouTubeWorkflow
    from features.youtube_auto_publish.source import MaterialSource, DEFAULT_HOSTS

    # Match the live app's two reader settings without importing app or any of
    # its store/publisher globals. The adapter uses only these two attributes.
    app_settings = SimpleNamespace(
        MYSQL_USER=(active_env.get('DRAMA_DB_USER') or active_env.get('ADMIN_MAPPING_MYSQL_USER') or '').strip(),
        MYSQL_PASSWORD=active_env.get('DRAMA_DB_PASSWORD') or active_env.get('ADMIN_MAPPING_MYSQL_PASSWORD') or '')
    reader = runtime.readonly_runner(app_settings)
    hosts = tuple(value.strip().lower() for value in active_env.get('DRAMA_YOUTUBE_SOURCE_HOSTS', ','.join(DEFAULT_HOSTS)).split(',') if value.strip())
    source = MaterialSource(active_env.get('YOUTUBE_AUTO_MATERIAL_SQL_FILE',''), reader,
                            allowed_hosts=hosts, drama_resolver=DramaMetadataResolver(reader))
    material = source.get(material_id)
    actor = {'tenant_key':'isolated-reference-validation', 'user_id':'isolated-reference-validation', 'role':'admin'}
    # This private store has no channel credentials, short-link publisher,
    # production engine, notification callback, or enabled publishing entrypoint.
    workflow = YouTubeWorkflow(work/'shadow.sqlite3', work/'assets', source, forbid, forbid, forbid,
                                notify=forbid, fetch_reference_cover=fetch_reference_cover_factory(), enabled=False)
    task = {'id':uuid.uuid4().hex, 'creator':actor, 'material':material,
            'requirements':REQUIREMENTS, 'versions':[], 'reference_cover':None}
    task['reference_cover'], task['material'] = workflow._prepare_reference(task)
    ref = task['reference_cover']
    reference_path = work/'assets'/(ref['asset_id']+'.jpg')
    real_run = runtime.subprocess.run
    calls = []
    trace_stdout, trace_stderr, audit_path = work/'codex.stdout.jsonl', work/'codex.stderr.log', work/'audit.json'

    def capture(command, **kwargs):
        if calls or 'exec' not in command or '--image' not in command:
            raise RuntimeError('exactly_one_attached_generation_required')
        command = list(command)
        command.insert(command.index('exec')+1, '--json')
        calls.append(command)
        stdout = stderr = ''
        try:
            result = real_run(command, **kwargs)
            stdout, stderr = result.stdout, result.stderr
            return result
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = exc.stdout or '', exc.stderr or ''
            raise
        finally:
            save_text(trace_stdout, stdout)
            save_text(trace_stderr, stderr)
            text = stdout.decode('utf-8','replace') if isinstance(stdout,bytes) else stdout
            audit = audit_image_calls(text or '')
            audit.update({'github_commit':commit, 'material_id':material_id,
                          'reference_sha256':ref['sha256'],
                          'attached_image_path':command[command.index('--image')+1],
                          'trace_stdout':str(trace_stdout), 'trace_stderr':str(trace_stderr)})
            save_json(audit_path, audit)

    started = time.monotonic()
    try:
        runtime.subprocess.run = capture
        output = runtime.generate_cover_factory(work)(task, {'number':1})
    finally:
        runtime.subprocess.run = real_run
    elapsed = round(time.monotonic()-started, 3)
    with Image.open(io.BytesIO(output)) as image:
        image.load()
        dimensions, fmt = image.size, image.format
        if fmt not in ('PNG','JPEG') or abs(image.width/image.height-16/9) > .03:
            raise RuntimeError('generated_image_not_16_9_raster')
    output_path = work/('generated-cover.png' if fmt=='PNG' else 'generated-cover.jpg')
    with private_file(output_path, 'wb') as output_file:
        output_file.write(output)
    if hashlib.sha256(reference_path.read_bytes()).hexdigest() != ref['sha256']:
        raise RuntimeError('reference_changed')
    result = {'material_id':material_id, 'reference_sha256':ref['sha256'],
              'reference_path':str(reference_path), 'output_path':str(output_path),
              'dimensions':list(dimensions), 'generation_elapsed_seconds':elapsed,
              'trace_stdout':str(trace_stdout), 'trace_stderr':str(trace_stderr), 'audit_path':str(audit_path),
              'published':False, 'notifications_sent':False, 'requires_manual_trace_and_visual_review':True}
    save_json(work/'validation-result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--material-id', required=True)
    parser.add_argument('--work-root', required=True)
    args = parser.parse_args()
    if re.fullmatch(r'[1-9][0-9]{0,18}', args.material_id) is None:
        parser.error('--material-id must be a positive numeric material ID')
    previous_umask = os.umask(0o077)
    work = None
    try:
        stage = Path(__file__).resolve().parents[1]
        work, commit = prepare_workspace(args.work_root, stage)
        result = validate(args.material_id, work, stage, commit)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # No raw exception/model/SQL/environment text reaches the console.
        error = {'ok':False, 'error_type':type(exc).__name__, 'work_root':str(work) if work else None,
                 'published':False, 'notifications_sent':False}
        if work is not None:
            save_json(work/'validation-failure.json', error)
        print(json.dumps(error, ensure_ascii=False))
        return 1
    finally:
        os.umask(previous_umask)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
