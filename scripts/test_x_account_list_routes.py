"""Execute the exact main-API route blocks without booting unrelated services."""
from pathlib import Path
import re
import textwrap
import types
import unittest
from unittest import mock
from features.x_accounts.client import XAccountsClientError

SOURCE = (Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8')


def route(start, end, **namespace):
    code = textwrap.dedent(SOURCE[SOURCE.index(start):SOURCE.index(end, SOURCE.index(start))])
    globals_ = dict(re=re, XAccountsClientError=XAccountsClientError, **namespace)
    exec('def handle(self, parsed):\n'+textwrap.indent(code, '    '), globals_)
    return globals_['handle']


class RoutesTests(unittest.TestCase):
    def test_delete_requires_admin_and_same_origin_before_any_mutation(self):
        send, delete, audit = mock.Mock(), mock.Mock(return_value={'deleted':True}), mock.Mock()
        handle = route('        x_account_delete_match = re.fullmatch(', '        x_pool_delete_match = re.fullmatch(',
            json_response=send, delete_x_account=delete, append_audit_log=audit,
            x_accounts_actor=lambda session:session, x_accounts_error_payload=lambda e:(e.status_code,{'error':e.code}))
        handler = types.SimpleNamespace(_require_cookie_admin=mock.Mock(return_value=False),
            _require_same_origin_json=mock.Mock(return_value=True), _session=lambda:{'role':'admin'})
        path = types.SimpleNamespace(path='/api/admin/x-accounts/12')
        handle(handler,path);delete.assert_not_called();handler._require_same_origin_json.assert_not_called()
        handler._require_cookie_admin.return_value=True;handler._require_same_origin_json.return_value=False
        handle(handler,path);delete.assert_not_called()
        handler._require_same_origin_json.return_value=True
        handle(handler,path);delete.assert_called_once_with('12',{'role':'admin'});audit.assert_called_once()
        self.assertEqual(send.call_args.args[1],200)
        delete.side_effect=XAccountsClientError('x_account_busy','busy',409)
        handle(handler,path);self.assertEqual(send.call_args.args[1],409)

    def test_balance_requires_module_and_resolved_platform_identity(self):
        send, query = mock.Mock(), mock.Mock(return_value={'status':'unavailable'})
        resolve = mock.Mock(return_value={'user_id':'resolved','x_platform_access':True})
        handle = route('        if parsed.path == "/api/admin/x-accounts/credits":',
                       '        if parsed.path == "/api/admin/x-accounts":',
            json_response=send,query_x_api_credits=query,x_platform_accounts_actor=resolve,
            x_accounts_error_payload=lambda e:(e.status_code,{'error':e.code}))
        handler=types.SimpleNamespace(_require_cookie_module=mock.Mock(return_value=False),_session=lambda:{'user_id':'raw'})
        path=types.SimpleNamespace(path='/api/admin/x-accounts/credits')
        handle(handler,path);query.assert_not_called();resolve.assert_not_called()
        handler._require_cookie_module.return_value=True
        handle(handler,path);query.assert_called_once_with(resolve.return_value)
        self.assertTrue(send.call_args.kwargs['no_store'])


if __name__=='__main__': unittest.main()
