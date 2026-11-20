# Historical report snapshots

A manual migration for dated DramaWave metric rows and ad-set metadata, named HotDrama extracts and the public fields of ad-replacement receipts. The inventory pins each source's path, inode, modification time and byte length. The import checks all business fields it encounters. An unexpected field, envelope or string containing credentials retains the source and records the reason. Raw receipt bodies remain in the original audit trail. The importer never deletes a file.

## Operation

Use the CPU report-material-catalog Python environment with mysql-connector-python 9.2.0. Run the following from this directory with an owner-only working directory on the data disk. The two-table schema is a separate deployment. The normal importer checks the existing schema, writes a staging snapshot, then marks it verified only after reading the ordered data and control totals on the replica. An interrupted import can reuse identical pages. A source with identical bytes can reuse an already verified version of the same dataset. Each pathname keeps its own snapshot identity. There is no timer.

```sh
umask 077
py=/mnt/data-disk/report-material-catalog/venv/bin/python
"$py" -B history.py schema
"$py" -B history.py inventory > "$job/plans/history.json"
"$py" -B history.py import --plan "$job/plans/history.json" --journal "$job/receipts/history.jsonl"
"$py" -B history.py read "$snapshot_id" > "$new_export"
```

Use a new, empty output path for an export. Export preserves rows, order, nulls, missing fields, IDs, metrics and document metadata. JSON whitespace can change. A receipt export contains the selected IDs, statuses, timestamps and the original line hash. Private archives contain the original bytes where a file was retired.

HotDrama positional exports use a matching SQL projection from the source directory when available. For the 17 historical files listed in `reviewed-ordinal-sources.json`, the original labels could not be established. The manifest identifies the reviewed bytes and path. The snapshot preserves scalar cells and row order with ordinal columns, sets `business_column_mapping` to `unavailable`, and leaves typed metric totals and dates empty. A normal `read` reconstructs the original arrays.

## Scope and recovery

63350 serves all searches, page reads and verification. 63353 accepts the fixed DDL and inserts or primary-key status updates on the two named tables. The writer uses one connection and small batches. Each snapshot's ordered `page_ids` may include a page more than once. Use that order for queries and sums. Adding all physical pages would mix unrelated report versions.

Only retire a verified file after checking its actual reader or published report, current use, private recovery copy and content hash. For a file archived by the CPU private-storage tool, obtain the key, original size and SHA-256 from its owner-only receipt and restore it to a vacant path:

```sh
python3 /opt/private-storage-archive/current/scripts/private_storage_archive.py get \
  --key "$object_key" --sha256 "$original_sha256" --size "$original_size" --output "$vacant_path" --json
```

Stop the scoped import unit to pause a migration. Completed verified records remain available and staging records remain hidden from the reader. Roll back a consumer by restoring its archived original input and its previous binding. Fresh deployments keep existing production readers and operational queues in place. Removing database history requires a separate data-retention decision once verified data is present.

For the 2026-11-20 CPU cleanup, `retire_verified.py` reads the original frozen inventory, the import receipts and delivery checks. It rechecks snapshot identity on the replica, the original file and the delivery hashes. With `--archive` it uses the existing private archive, verifies the remote bytes and records the recovery receipt before removing each qualifying local file. Original FB operation receipts stay available. To restore an exact missing report input for an older report builder, run the same tool with its original path relative to the data disk:

```sh
"$py" -B retire_verified.py --restore "$relative_source_path"
```
