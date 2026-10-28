import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from features.social_daily_report.collector import present
from features.social_daily_report.delivery import SocialDeliveryStore
from features.post_daily_report.delivery import DeliveryUnknown
from tests.test_social_daily_report import fixture,DAY
from scripts import social_daily_correction as cli
from features.social_analytics import routes

class CorrectionTests(unittest.TestCase):
    def test_current_web_collector_accepts_daily_frozen_catalog_only_in_system_scope(self):
        query=dict(start=[DAY],end=[DAY],platform=['x'],group_by=['owner,platform'])
        with patch.object(routes,'read_catalogs',side_effect=AssertionError('catalog must stay frozen')),patch.object(routes,'source',return_value=([],DAY)):
            value=routes.build_report({},dict(role='admin',_system_report=True),query,catalog_snapshot=([],{}))
            self.assertEqual(value['totals']['published_count'],0)
            with self.assertRaises(routes.ReportError):
                routes.build_report({},dict(role='admin'),query,catalog_snapshot=([],{}))

    def test_preview_send_and_repeat_preserve_original_and_frozen_payload(self):
        with tempfile.TemporaryDirectory() as root:
            original=Path(root)/'original.json';original.write_text('original')
            args=['--date',DAY,'--revision','pool-owner-v1','--state-dir',root]
            report=present(fixture(),DAY)
            with patch.object(cli,'ensure_storage'),patch.object(cli,'collect',return_value=report) as collect,patch.object(cli,'send_card',return_value={'message_id':'correction1'}) as send:
                cli.main(args);send.assert_not_called()
                cli.main(args+['--send']);cli.main(args+['--send'])
                self.assertEqual(collect.call_count,1);self.assertEqual(send.call_count,1)
                self.assertIn('归属更正',send.call_args.args[2]['header']['title']['content'])
                self.assertEqual(original.read_text(),'original')

    def test_unknown_receipt_never_resends(self):
        with tempfile.TemporaryDirectory() as root:
            args=['--date',DAY,'--revision','pool-owner-v1','--state-dir',root]
            with patch.object(cli,'ensure_storage'),patch.object(cli,'collect',return_value=present(fixture(),DAY)),patch.object(cli,'send_card',side_effect=DeliveryUnknown('timeout')) as send:
                cli.main(args)
                for _ in range(2):
                    with self.assertRaises(DeliveryUnknown):cli.main(args+['--send'])
                self.assertEqual(send.call_count,1)

if __name__=='__main__':unittest.main()
