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
    def test_material_source_uses_cookie_origin_and_server_actor(self):
        h=Handler('/api/video-editing/tasks/ve_'+'a'*32+'/material-source')
        h.session['email']='editor@example.test'
        upload=Mock(return_value={'status':'queued','error':''})
        service=SimpleNamespace(material_source=SimpleNamespace(enqueue=upload))
        with patch('features.video_editing.routes.get_service',return_value=service):
            self.assertTrue(handle(h,'POST',self.app))
        self.assertEqual(self.replies,[(200,{'item':{'status':'queued','error':''}})])
        self.assertEqual(upload.call_args.args[0],'ve_'+'a'*32)
        self.assertEqual(upload.call_args.args[1]['email'],'editor@example.test')
        h=Handler('/api/video-editing/tasks/ve_'+'a'*32+'/material-source')
        h._require_same_origin_json.return_value=False
        with patch('features.video_editing.routes.get_service') as factory:
            self.assertTrue(handle(h,'POST',self.app));factory.assert_not_called()

    def test_resource_id_with_slashes_is_passed_as_query_value(self):
        lookup=Mock(return_value=[])
        service=SimpleNamespace(catalog=SimpleNamespace(resource_variants=lookup))
        with patch('features.video_editing.routes.get_service',return_value=service):
            handle(Handler('/api/video-editing/catalog/resource-variants?series_code=%7Bv3404%2Fl144250%2F%7D'),'GET',self.app)
        lookup.assert_called_once_with('{v3404/l144250/}')
        self.assertEqual(self.replies[0][0],200)

    def test_asset_filters_and_retag_fields_forwarded(self):
        listing=Mock(return_value={'items':[],'total':0});mutate=Mock(return_value={})
        service=SimpleNamespace(assets=listing,mutate_asset=mutate)
        with patch('features.video_editing.routes.get_service',return_value=service):
            handle(Handler('/api/video-editing/assets?library_kind=free_episode&series_code=21481'),'GET',self.app)
            handle(Handler('/api/video-editing/assets/va_'+'a'*32,{'series_code':'21481'}),'PATCH',self.app)
        self.assertEqual(listing.call_args.args[-2:],('free_episode','21481'))
        self.assertEqual(mutate.call_args.kwargs['series_code'],'21481')
        self.assertIsNone(mutate.call_args.kwargs['name'])

    def test_material_source_variant_payload_forwarded(self):
        enqueue=Mock(return_value={})
        service=SimpleNamespace(material_source=SimpleNamespace(enqueue=enqueue))
        payload={'app_id':'1479','content_id':'abc123'}
        with patch('features.video_editing.routes.get_service',return_value=service):
            handle(Handler('/api/video-editing/tasks/ve_'+'b'*32+'/material-source',payload),'POST',self.app)
        self.assertEqual(enqueue.call_args.args[2],payload)


if __name__=='__main__':unittest.main()
