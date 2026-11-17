import sys,tempfile,datetime as dt,json
from pathlib import Path
sys.path.insert(0,'/opt/pipeline-dispatch/current')
from features.pipeline_dispatch.core import DispatchStore
clock=[dt.datetime(2026,11,17,10,tzinfo=dt.timezone.utc)]
actor={'is_admin':True,'user_id':'test'}
with tempfile.TemporaryDirectory(dir='/mnt/data-disk/youtube-cover-lane-20261117') as td:
 s=DispatchStore(Path(td)/'isolated.sqlite3',clock=lambda:clock[0]);node='cpu-youtube-cover'
 e=s.create_enrollment(node,actor);s.consume_enrollment(e['token'],node)
 s.register_node(node,['backend:cpu','prepare:youtube','youtube_prepare'],{'cpu_count':1})
 for stage,n in [('prepare',1),('publish',0)]:s.set_capacity({'expected_version':s.config()['version'],'host_id':node,'stage':stage,'max_workers':n,'scaling_mode':'manual'},actor)
 s.channel_enabled('youtube',True);s.channel_enabled('fb',True);clock[0]+=dt.timedelta(seconds=2)
 for channel,index in [('fb','older-fb'),('youtube','cover1'),('youtube','cover2')]:
  s.offer({'source_key':channel+':preparation:'+index,'source_id':index,'channel':channel,'family':'preparation','stage':'prepare','source_status':'queued_generation','execution_available':True,'resource_keys':[channel+':cover:'+index],'capabilities':['prepare:'+channel]})
 s.worker_heartbeat(node,'one','prepare',123);s.worker_heartbeat(node,'two','prepare',124);s.worker_heartbeat(node,'publish','publish',125)
 claimed=s.claim(node,'one','prepare');assert claimed['job']['channel']=='youtube',claimed
 assert s.claim(node,'two','prepare')['job'] is None
 assert s.claim(node,'publish','publish')['job'] is None
 print(json.dumps({'other_channels_rejected':True,'prepare_concurrency_one':True,'publisher_disabled':True,'isolated_database_only':True}))
