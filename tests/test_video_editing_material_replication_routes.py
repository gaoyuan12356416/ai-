from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from features.video_editing import VideoEditingError
from features.video_editing.routes import handle


class Handler:
    def __init__(self,path,body=None):
        self.path=path;self.body=body or {};self.headers={'Content-Length':'0'}
        self._require_cookie_module=Mock(return_value=True)
        self._require_same_origin_json=Mock(return_value=True)
    def _session(self):return {'tenant_key':'tenant','user_id':'creator','email':'test@example.test','role':'user'}
    def _read_json(self):return self.body


class ReplicationRoutesTests(unittest.TestCase):
    def setUp(self):
        self.path='/api/video-editing/tasks/ve_'+'a'*32+'/material-replication'
        self.replies=[]
        self.app=SimpleNamespace(json_response=lambda h,status,body,**kw:self.replies.append((status,body)))
        self.material=SimpleNamespace(context=Mock(return_value={'history':[]}),enqueue=Mock(return_value={'status':'pending'}))
        self.service=SimpleNamespace(material_replication=self.material)

    def test_get_history_only_passed_with_server_actor(self):
        handler=Handler(self.path+'?history_only=1')
        with patch('features.video_editing.routes.get_service',return_value=self.service):handle(handler,'GET',self.app)
        self.assertEqual(self.replies,[(200,{'item':{'history':[]}})])
        self.assertTrue(self.material.context.call_args.kwargs['history_only'])
        self.assertFalse(self.material.context.call_args.args[1]['is_admin'])

    def test_post_origin_gate_and_module_permission_precede_dispatch(self):
        handler=Handler(self.path,{'operation_id':'test-operation'})
        handler._require_same_origin_json.return_value=False
        with patch('features.video_editing.routes.get_service') as factory:
            handle(handler,'POST',self.app);factory.assert_not_called()
        handler._require_same_origin_json.return_value=True
        handler._require_cookie_module.return_value=False
        with patch('features.video_editing.routes.get_service') as factory:
            handle(handler,'POST',self.app);factory.assert_not_called()
        handler._require_cookie_module.assert_called_with('video_editing')

    def test_post_returns_item_and_business_conflicts_remain_200(self):
        self.material.enqueue.return_value={'id':'record','conflict':True,'conflict_fields':[]}
        handler=Handler(self.path,{'operation_id':'test-operation'})
        with patch('features.video_editing.routes.get_service',return_value=self.service):handle(handler,'POST',self.app)
        self.assertEqual(self.replies[0][0],200);self.assertTrue(self.replies[0][1]['item']['conflict'])
        self.assertEqual(self.material.enqueue.call_args.args[2],handler.body)

    def test_operation_payload_conflict_returns_409(self):
        self.material.enqueue.side_effect=VideoEditingError('replication_operation_conflict','conflict',409)
        with patch('features.video_editing.routes.get_service',return_value=self.service):handle(Handler(self.path),'POST',self.app)
        self.assertEqual(self.replies[0][0],409)

    def test_missing_identity_cannot_read_history(self):
        handler=Handler(self.path+'?history_only=1');handler._session=lambda:{}
        with patch('features.video_editing.routes.get_service') as factory:
            handle(handler,'GET',self.app);factory.assert_not_called()
        self.assertEqual(self.replies[0][0],401)

    def test_non_admin_operator_change_returns_service_403(self):
        self.material.enqueue.side_effect=VideoEditingError('replication_operator_forbidden','只有管理员可以修改归属账号',403)
        with patch('features.video_editing.routes.get_service',return_value=self.service):
            handle(Handler(self.path,{'operator':'other'}),'POST',self.app)
        self.assertEqual(self.replies[0][0],403)


if __name__=='__main__':unittest.main()
