# Shared material catalog

The old manual report refresh exported a full `library.json` into every work
directory. There is no scheduled producer in this report. The compatibility
entry now reads that directory's fixed `catalog-generation.json` and queries
only content IDs needed by its existing catalog and material shards. Pages are
bounded, and original source order is restored before the existing consumer.
The complete original file is archived privately; no video/image blobs enter
the catalog tables.

`schema.sql` creates three dedicated `ads_ai` tables once as a reviewed deploy.
Normal commands never run DDL. Imports write immutable staging rows and verify
every normalized field and ordinal through the read-only endpoint before
marking a generation verified. The explicit pointer records current and previous
versions. No generation is automatically deleted. Live report inputs pin a
generation rather than following a moving pointer during a build.

Run using `/opt/report-material-catalog/current/reports/yuesiying/catalog/cli.py`:
Use `/mnt/data-disk/report-material-catalog/venv/bin/python` with the pinned
`mysql-connector-python==9.2.0` dependency. Connections use MySQL protocol
compression and reuse the writer cursor across batches. An interrupted staging
import can resume only after its complete stored prefix matches the immutable
source checksum and normalized ordered rows; no committed prefix is overwritten.

```
python3 cli.py import-json /REPORT/library.json --generated-at 'SOURCE INVENTORY TIME'
python3 cli.py validate /REPORT GENERATION_SHA256
python3 cli.py bind /REPORT GENERATION_SHA256
python3 cli.py activate yuesiying-material-library GENERATION_SHA256
```

After validation, `bind` records the source timestamp/checksum and fixed version
in the work directory's `catalog-generation.json` and inventory timestamp.
A later legacy manual export cannot silently use an older bound generation:
changed inventory time or legacy-file checksum stops the build with the refresh
instruction. The existing build entry is retained;
it dispatches to the verified shared builder with the same work directory.
The compatibility fallback reads the legacy JSON only when no binding exists.
Rollback restores the small entry script and removes that binding only after
the original JSON has been restored and verified from private archive.

For future manual refreshes, use `refresh-source /PATH/original-library-select.sql
--generated-at 'SOURCE INVENTORY TIME'`. This streams the original
`kunlunads_dev.ads_custom_source` SELECT directly from port 63350 in primary-ID
pages into the catalog; no `library.json` is created. Supply the exact previously
approved product/deletion/type/date predicates, projected as the six fields
`id,name,language,data_source_id,video_duration,created_at`. This change does not
invent new source filters. The connection verifies `@@read_only=1`; arbitrary
statements and other source tables are rejected. Then validate the chosen report
scope, pin the returned generation, and run the existing report builder. Data
generation does not publish a site or send a message.

All SQL verification/reads use 63350. Writes use 63353 only for these three tables
with one connection and bounded batches. Credentials remain in the established
server-only configuration and are never logged or stored in report artifacts.

The full import check compares ordered normalized rows. The consumer check also
compares the filtered material IDs and their order, names, language, durations,
normalization keys, source IDs, creation times, kept materials and candidate IDs.
Unsupported additional source fields fail import instead of being dropped.
