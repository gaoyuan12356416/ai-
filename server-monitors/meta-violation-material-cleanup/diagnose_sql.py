"""Replay saved violation materials through SQL only; no Graph, Store or notices."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from meta_violation_cleanup.indexed_source import material_rows
from meta_violation_cleanup.policy import product_scope
from meta_violation_cleanup.runner import Runner, chunks
from meta_violation_cleanup.runtime import validate_run_id
from meta_violation_cleanup.sql_source import SqlSource, SqlSourceError


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--source-run', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--reuse-inputs')
    parser.add_argument('--batch-index', type=int)
    args = parser.parse_args()
    run_id = validate_run_id(args.source_run)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    config = json.loads(Path(args.config).read_text())
    frozen = Path(config['runtime_dir']) / 'runs' / run_id / 'violation-evidence.json'
    evidence_bytes = frozen.read_bytes()
    evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
    config['sql_diagnostic_path'] = str(output / 'mysql-errors.jsonl')
    source = SqlSource(config)
    runner = Runner(config, source, None, None, None, None)
    began = time.monotonic()
    progress = {'source_run': run_id, 'evidence_sha256': evidence_sha, 'meta_requests': 0,
                'batches_completed': 0, 'ads_completed': 0, 'status': 'running'}
    def update(**kw):
        progress.update(kw)
        progress.update(sql_metrics=source.safe_metrics, elapsed_seconds=round(time.monotonic()-began, 3))
        save(output / 'progress.json', progress)
    try:
        if args.reuse_inputs:
            inputs = json.loads(Path(args.reuse_inputs).read_text())
            if inputs['source_run'] != run_id or inputs['evidence_sha256'] != evidence_sha:
                raise ValueError('diagnostic_input_mismatch')
        else:
            evidence = json.loads(evidence_bytes)
            scope = product_scope(source.products())
            rows, metrics = material_rows(source, evidence['evidence']['materials'], scope, update)
            inputs = dict(source_run=run_id, evidence_sha256=evidence_sha, scope=scope,
                          ad_ids=sorted({str(row['ad_id']) for row in rows}), lookup_metrics=metrics)
        save(output / 'inputs.json', inputs)
        update(stage='publication_context', total_ads=len(inputs['ad_ids']))
        selected = False
        for index, part in enumerate(chunks(inputs['ad_ids'])):
            if args.batch_index is not None and index != args.batch_index:
                continue
            selected = True
            save(output / 'current-batch.json', {'index': index, 'ad_ids': part})
            update(batch_index=index, batch_ads=len(part))
            with source.snapshot():
                rows, sources = runner.lineage_data(part, scope=inputs['scope'])
                relevant = [row for values in rows.values() for row in values
                            if str(row.get('product')) in inputs['scope']]
                routes = source.selected_credentials(relevant)
            update(batches_completed=progress['batches_completed']+1,
                   ads_completed=progress['ads_completed']+len(part),
                   last_batch_rows=sum(map(len, rows.values())), last_batch_routes=len(routes))
        if args.batch_index is not None and not selected:
            raise ValueError('diagnostic_batch_not_found')
        update(status='completed', stage='sql_complete')
        return 0
    except Exception as exc:
        update(status='failed', error_type=type(exc).__name__,
               error_code=exc.code if isinstance(exc, SqlSourceError) else 'diagnostic_failed',
               diagnostic_id=(source.last_diagnostic or {}).get('id'))
        return 1
    finally:
        source.close()
        print(json.dumps(progress, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
