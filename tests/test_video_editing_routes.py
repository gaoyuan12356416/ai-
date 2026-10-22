import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from features.video_editing.routes import handle


class Handler:
    def __init__(self,path='/api/video-editing/config',body=None):
        self.path=path;self.headers={'Content-Length':'0'};self.body=body or {};self.session={'tenant_key':'tenant','user_id':'user','role':'user'}
        self._require_cookie_module=Mock(return_value=True);self._require_same_origin_json=Mock(return_value=True)
    def _session(self):return self.session
    def _read_json(self):return self.body


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.replies=[];self.app=SimpleNamespace(json_response=lambda h,s,b,**kwargs:self.replies.append((s,b)))
    def test_unrelated_routes_do_not_instantiate_module(self):
        h=Handler('/api/drama-material/jobs')
        with patch('features.video_editing.routes.get_service') as factory:
            self.assertFalse(handle(h,'GET',self.app));factory.assert_not_called();h._require_cookie_module.assert_not_called()
    def test_cookie_permission_denial_stops_before_database(self):
        h=Handler();h._require_cookie_module.return_value=False
        with patch('features.video_editing.routes.get_service') as factory:
            self.assertTrue(handle(h,'GET',self.app));factory.assert_not_called()
        h._require_cookie_module.assert_called_once_with('video_editing')
    def test_write_origin_gate_precedes_body_or_storage(self):
        h=Handler('/api/video-editing/uploads');h._require_same_origin_json.return_value=False
        with patch('features.video_editing.routes.get_service') as factory:
            self.assertTrue(handle(h,'POST',self.app));factory.assert_not_called()
    def test_oversized_metadata_request_rejected(self):
        h=Handler('/api/video-editing/uploads');h.headers['Content-Length']=str(3*1024**2)
        handle(h,'POST',self.app);self.assertEqual(self.replies[0][0],413)
    def test_missing_user_identity_rejected(self):
        h=Handler();h.session={}
        handle(h,'GET',self.app);self.assertEqual(self.replies[0][0],401)
    def test_config_json_shape(self):
        h=Handler();service=SimpleNamespace(config=lambda:{'enabled':False})
        with patch('features.video_editing.routes.get_service',return_value=service):handle(h,'GET',self.app)
        self.assertEqual(self.replies,[(200,{'item':{'enabled':False}})])


if __name__=='__main__':unittest.main()
