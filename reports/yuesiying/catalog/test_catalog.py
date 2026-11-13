import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import consumer
import store


def row(i,name='EN-123-00:00-a',lang='en',cid='source',duration=0):
    return dict(id=i,name=name,language=lang,data_source_id=cid,video_duration=duration,created_at='2026-11-13 10:00:00')


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def test_streaming_matches_json_for_tiny_unicode_chunk_boundaries(self):
        expected=[row(i,name='字幕\\"}'+str(i)+'中文',duration=i/10) for i in range(1,20)]
        file=self.root/'library.json';file.write_text(json.dumps(expected,ensure_ascii=False),encoding='utf-8')
        for size in (1,3,10,99,10000):
            self.assertEqual(list(store.iter_json_array(file,chunk_size=size)),expected)

    def test_incomplete_or_trailing_json_is_rejected(self):
        file=self.root/'library.json'
        for value in ('[{}','[{},]','[{}] extra','{}'):
            file.write_text(value)
            with self.assertRaises(ValueError):list(store.iter_json_array(file,chunk_size=2))

    def test_extra_fields_cannot_be_silently_dropped(self):
        with self.assertRaisesRegex(ValueError,'fields_changed'):store.normalize({**row(1),'extra':'retained in archived source'})

    def test_consumer_null_zero_and_empty_id_conversions_are_preserved(self):
        for duration in (None,0,'0.0',1.25,'1.2500'):
            source=row(1,duration=duration);source['data_source_id']=None;source['created_at']=None
            normalized=store.normalize(source)
            self.assertEqual(float(normalized['video_duration']),float(duration or 0))
            self.assertEqual(normalized['created_at'],str(source['created_at']))
            self.assertEqual(normalized['data_source_id'],str(source['data_source_id']))

    def test_selection_identity_language_signatures_order_and_key_fields(self):
        catalog={'source':{('123','en')},'french':{('123','fr')},'ambiguous':{('123','fr'),('124','fr')}}
        originals={'123':{'3'}}
        values=[row(7,name='fr-123-00:00-a',lang='fr',cid='french',duration=None),
                row(3,name='en-123-00:00-a',duration='2.0'),
                row(9,lang='fr',cid='ambiguous'),row(10,lang='ja',cid='french')]
        before=consumer.select(catalog,originals,values)
        after=consumer.select(catalog,originals,[store.normalize(value) for value in values])
        self.assertEqual(before,after)
        self.assertEqual(before['order'],[('123',['7','3'])])
        self.assertEqual(before['potential'],['7'])
        self.assertEqual(before['kept']['123']['7']['d'],0.0)

    def test_unverified_generation_never_activates(self):
        with patch.object(store,'metadata',return_value={'status':'staging'}),patch.object(store,'connect') as connect:
            with self.assertRaisesRegex(ValueError,'not_verified'):store.activate('config','report','a'*64)
            connect.assert_not_called()

    def test_sql_source_must_be_original_table_and_single_select(self):
        for query in ('DELETE FROM kunlunads_dev.ads_custom_source','SELECT * FROM ads_ai.other',
                      'SELECT * FROM kunlunads_dev.ads_custom_source; SELECT 1'):
            with self.assertRaisesRegex(ValueError,'source_select_required'):list(store.source_rows('config',query))

    def test_changed_manual_inventory_cannot_claim_old_generation_is_fresh(self):
        (self.root/'catalog-generation.json').write_text(json.dumps({'generation_id':'a'*64,'generated_at':'old',
            'source_kind':'legacy-json','source_sha256':'b'*64}))
        (self.root/'inventory-time.txt').write_text('new')
        with self.assertRaisesRegex(ValueError,'binding_stale'):consumer.load_library(self.root,'config')

    def test_bind_keeps_source_time_and_checksum_and_requires_verified_version(self):
        source=self.root/'library.json';source.write_text(json.dumps([row(1)]))
        record={'generation_id':'a'*64,'status':'verified','generated_at':'source time',
                'source_kind':'legacy-json','source_sha256':store.file_sha(source)}
        with patch.object(consumer,'metadata',return_value=record):
            consumer.bind(self.root,'config','a'*64)
        self.assertEqual((self.root/'inventory-time.txt').read_text().strip(),'source time')
        self.assertEqual(json.loads((self.root/'catalog-generation.json').read_text())['source_sha256'],record['source_sha256'])
        source.write_text('changed')
        with self.assertRaisesRegex(ValueError,'legacy_library_changed'):consumer.load_library(self.root,'config')


if __name__=='__main__':unittest.main()
