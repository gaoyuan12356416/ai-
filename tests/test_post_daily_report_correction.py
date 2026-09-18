import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from features.post_daily_report.common import Window
from features.post_daily_report.delivery import DeliveryStore, DeliveryUnknown
from scripts import post_daily_report_correction as correction


class TTCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.date, self.chat, self.revision = '2026-09-17', 'fixture-chat', 'tt-fix-20260918'
        self.window = Window.for_date(self.date)
        self.folder = self.state / 'reports' / self.date
        self.folder.mkdir(parents=True)
        self.base = {
            'date': self.date, 'cutoff_utc': self.window.cutoff.isoformat(),
            'generated_at_utc': '2026-09-18T02:00:01+00:00',
            'channels': [
                {'channel':'TT','rows':[{'source':'unavailable:tt_auto','expected':None,'published':0,'late':0}]},
                {'channel':'FB','rows':[{'source':'auto','expected':115,'published':100,'late':0}], 'evidence':['frozen-fb']},
                {'channel':'X','rows':[{'source':'material','expected':76,'published':76,'late':0}], 'evidence':['frozen-x']},
            ],
        }
        (self.folder / 'report.json').write_text(json.dumps(self.base), encoding='utf-8')
        (self.folder / 'receipt.json').write_text(json.dumps({'code':0,'message_id':'original-message'}), encoding='utf-8')
        with self.original_store() as store:
            self.original_uuid = store.claim(self.date, self.chat)
            store.finish(self.date, self.chat, 'sent', 'original-message')
        self.original_files = {p.name:p.read_bytes() for p in self.folder.iterdir()}
        self.original_delivery = (self.state / 'delivery.sqlite3').read_bytes()
        self.tt = {'channel':'TT','rows':[{'source':'auto','label':'自动模板','expected':52,'published':48,'late':0,'pending':4}]}
        self.args = ['--date',self.date,'--revision',self.revision,'--chat-id',self.chat,'--state-dir',str(self.state)]

    def tearDown(self):
        self.temp.cleanup()

    def original_store(self):
        from contextlib import closing
        return closing(DeliveryStore(self.state / 'delivery.sqlite3'))

    def invoke(self, mode):
        with redirect_stdout(io.StringIO()):
            return correction.main(self.args + [mode])

    def assert_original_unchanged(self):
        self.assertEqual({p.name:p.read_bytes() for p in self.folder.iterdir()}, self.original_files)
        self.assertEqual((self.state / 'delivery.sqlite3').read_bytes(), self.original_delivery)

    def test_preview_changes_only_tt_and_preserves_cutoff_and_original(self):
        with patch.object(correction, 'collect_tt', return_value=self.tt) as collect, patch.object(correction, 'send_card') as send:
            self.assertEqual(self.invoke('--preview'), 0)
        send.assert_not_called()
        self.assertEqual(collect.call_args.args[1], self.window)
        preview = self.state / 'previews' / self.date / 'corrections' / self.revision
        report = json.loads((preview / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['channels'][0], self.tt)
        self.assertEqual(report['channels'][1:], self.base['channels'][1:])
        self.assertEqual(report['cutoff_utc'], self.base['cutoff_utc'])
        self.assertEqual(report['correction']['original_report_sha256'], hashlib.sha256(self.original_files['report.json']).hexdigest())
        card = json.loads((preview / 'card.json').read_text(encoding='utf-8'))
        self.assertIn('TT 修正版', card['header']['title']['content'])
        self.assertFalse((self.state / 'corrections').exists())
        self.assert_original_unchanged()

    def test_send_once_keeps_original_and_uses_distinct_stable_uuid(self):
        with patch.object(correction, 'collect_tt', return_value=self.tt) as collect, patch.object(correction, 'send_card', return_value={'code':0,'message_id':'corrected-message'}) as send:
            self.assertEqual(self.invoke('--send'), 0)
            self.assertEqual(self.invoke('--send'), 0)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(collect.call_count, 1)
            self.assertNotEqual(send.call_args.args[3], self.original_uuid)
            sent_card = send.call_args.args[2]
        folder = self.state / 'corrections' / self.date / self.revision
        self.assertEqual((folder / 'card.json').read_text(encoding='utf-8'), json.dumps(sent_card, ensure_ascii=False, separators=(',', ':')))
        self.assertEqual(json.loads((folder / 'receipt.json').read_text())['message_id'], 'corrected-message')
        self.assert_original_unchanged()

    def test_ambiguous_correction_blocks_retries_without_overwriting_artifacts(self):
        with patch.object(correction, 'collect_tt', return_value=self.tt), patch.object(correction, 'send_card', side_effect=TimeoutError()) as send:
            with self.assertRaises(DeliveryUnknown):
                self.invoke('--send')
            folder = self.state / 'corrections' / self.date / self.revision
            saved = (folder / 'report.json').read_bytes()
            with self.assertRaises(DeliveryUnknown):
                self.invoke('--send')
            self.assertEqual(send.call_count, 1)
            self.assertEqual((folder / 'report.json').read_bytes(), saved)
        self.assert_original_unchanged()

    def test_unavailable_or_uncertain_tt_cannot_be_sent_as_a_correction(self):
        for unavailable in (True, False):
            tt = copy.deepcopy(self.tt)
            if unavailable:
                tt['rows'][0]['data_available'] = False
            else:
                tt['rows'][0]['expected'] = None
            with patch.object(correction, 'collect_tt', return_value=tt), patch.object(correction, 'send_card') as send:
                with self.assertRaisesRegex(RuntimeError, 'tt_correction_incomplete'):
                    self.invoke('--send')
                send.assert_not_called()
        self.assert_original_unchanged()

    def test_unconfirmed_original_cannot_be_corrected(self):
        with self.original_store() as store:
            store.finish(self.date, self.chat, 'unknown')
        with patch.object(correction, 'collect_tt') as collect:
            with self.assertRaisesRegex(RuntimeError, 'confirmed_original'):
                self.invoke('--preview')
            collect.assert_not_called()

    def test_wrong_original_cutoff_is_rejected(self):
        self.base['cutoff_utc'] = '2026-09-18T03:00:00+00:00'
        (self.folder / 'report.json').write_text(json.dumps(self.base), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'original_window_mismatch'):
            self.invoke('--preview')

    def test_receipt_must_match_confirmed_original_delivery(self):
        (self.folder / 'receipt.json').write_text('{"message_id":"different"}', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'original_receipt_mismatch'):
            self.invoke('--preview')


if __name__ == '__main__':
    unittest.main()
