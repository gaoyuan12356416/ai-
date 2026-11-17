"""Credential-safe Meta transport. Accepted/unknown writes are never auto-replayed."""
from __future__ import annotations

from copy import deepcopy
import json
import time
import requests

from .domain import CREATE_FIELDS, creative_page

CREATIVE_FIELDS = ','.join(sorted(CREATE_FIELDS | {'id','name','object_story_id','effective_object_story_id','instagram_user_id'}))
AD_FIELDS = 'id,account_id,status,configured_status,effective_status,issues_info,creative{id},campaign{id,status,configured_status},adset{id,status,configured_status}'


class MetaError(Exception):
    def __init__(self, code, subcode=0, *, uncertain=False, transient=False):
        self.code,self.subcode,self.uncertain,self.transient=code,subcode,uncertain,transient
        super().__init__('meta_%s_%s' % (code,subcode))


class Graph:
    def __init__(self, token, version='v25.0', session=None):
        self.token,self.version=token,version
        self.http=session or requests.Session()
        self.http.trust_env=False
        self.last_write=0.0

    def request(self, method, path, params=None, files=None):
        if method=='POST':
            delay=1-(time.monotonic()-self.last_write)
            if delay>0:time.sleep(delay)
            self.last_write=time.monotonic()
        headers={'Authorization':'Bearer '+self.token}
        data={k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in (params or {}).items()}
        try:
            response=self.http.request(method,'https://graph.facebook.com/'+self.version+'/'+path.lstrip('/'),
                headers=headers,params=data if method=='GET' else None,data=data if method=='POST' else None,
                files=files,timeout=(5,120 if files else 40),allow_redirects=False)
            payload=response.json()
        except (requests.RequestException,ValueError):
            raise MetaError('transport',uncertain=method=='POST',transient=True) from None
        if not isinstance(payload,dict):
            raise MetaError('invalid_response',uncertain=method=='POST')
        if payload.get('error') or not 200<=response.status_code<300:
            error=payload.get('error') or {}
            code,sub=error.get('code',response.status_code),error.get('error_subcode',0)
            raise MetaError(code,sub,uncertain=method=='POST' and (response.status_code>=500 or code in {1,2,960}),
                            transient=bool(error.get('is_transient')) or code in {1,2,4,17,32,613,80004} or response.status_code>=500)
        return payload

    def get(self,path,**params):return self.request('GET',path,params)
    def post(self,path,**params):return self.request('POST',path,params)
    def ad(self,ad_id):return self.get(ad_id,fields=AD_FIELDS)
    def creative(self,cid):
        # The base contract avoids requesting optional new-version fields that
        # would make otherwise supported old creatives unreadable.
        fields='id,name,object_story_spec,asset_feed_spec,url_tags,applink_treatment,object_store_url,object_story_id,effective_object_story_id,degrees_of_freedom_spec,platform_customizations,product_set_id'
        return self.get(cid,fields=fields)

    def pages(self,path,max_pages=None,**params):
        result=[]
        seen=set()
        count=0
        while True:
            page=self.get(path,**params)
            count+=1
            if not isinstance(page.get('data'),list):raise MetaError('pagination_invalid')
            result.extend(page['data'])
            paging=page.get('paging') or {}
            if not paging.get('next'):return result
            if max_pages and count>=max_pages:return result
            after=(paging.get('cursors') or {}).get('after')
            if not after or after in seen:raise MetaError('pagination_invalid')
            seen.add(after);params['after']=after

    def story_content(self,creative):
        """Recover a referenced post if and only if its exact content is readable."""
        story=creative.get('effective_object_story_id') or creative.get('object_story_id')
        if not story:return creative
        post=self.get(story,fields='id,message,attachments{media_type,target,url,title,description,subattachments},call_to_action')
        attachments=(post.get('attachments') or {}).get('data') or []
        if len(attachments)!=1:raise ValueError('original_story_content_missing')
        attachment=attachments[0]
        if attachment.get('subattachments'):raise ValueError('original_carousel_story_not_reconstructable')
        cta=post.get('call_to_action')
        spec={'page_id':creative_page(creative)}
        if str(attachment.get('media_type','')).lower()=='video' and (attachment.get('target') or {}).get('id') and cta:
            spec['video_data']={'video_id':str(attachment['target']['id']),'message':post.get('message',''),'call_to_action':cta}
            if attachment.get('title'):spec['video_data']['title']=attachment['title']
            # A readable persistent thumbnail is required before reconstructing.
            video=self.get(attachment['target']['id'],fields='picture')
            if not video.get('picture'):raise ValueError('original_video_cover_missing')
            spec['video_data']['image_url']=video['picture']
        else:
            raise ValueError('original_story_content_missing')
        result=deepcopy(creative);result['object_story_spec']=spec
        return result

    def new_instagram(self,page_id):
        # Page-backed identity is permitted when no explicit Instagram identity
        # is available. Never carry the old Page's Instagram actor across.
        try:
            page=self.get(page_id,fields='id,is_published')
        except MetaError as exc:
            if exc.transient or exc.code==190:raise
            # Page metadata scopes are not a substitute for the actual creative
            # creation/validation authorization. Some ad tokens cannot read it.
            return None
        if str(page.get('id'))!=str(page_id) or page.get('is_published') is False:
            raise MetaError('page_unpublished')
        try:
            account=self.get(page_id,fields='instagram_business_account')
            return str((account.get('instagram_business_account') or {}).get('id') or '') or None
        except MetaError as exc:
            if exc.transient or exc.code==190:raise
            return None

    def insights(self,ad_id,dates):
        return self.pages(str(ad_id)+'/insights',fields='ad_id,actions,date_start,date_stop',time_range=dates,limit=100)
