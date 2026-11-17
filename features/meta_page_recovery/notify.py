"""One frozen summary per completed hourly run, sent only to the agreed group."""
from datetime import datetime,timedelta,timezone
import json

from .domain import CHAT_ID,encoded,utcnow


def card(summary,acceptance=False):
    cst=timezone(timedelta(hours=8))
    def stamp(value):return datetime.fromisoformat(value).astimezone(cst).strftime('%m-%d %H:%M:%S')
    started,finished=summary['started'],summary['finished']
    elapsed=round((datetime.fromisoformat(finished)-datetime.fromisoformat(started)).total_seconds())
    lines=['批次：'+summary['run_id'], '北京时间：'+stamp(started)+' → '+stamp(finished)+'（'+str(elapsed)+'秒）']
    if acceptance:lines.append('这是上线验收播报；未执行真实广告换页。')
    if summary.get('kind')=='baseline':lines.append('首次建立基线，既有异常 Page 不补恢复。')
    if summary.get('error'):lines.append('本轮存在扫描失败：'+str(summary['error'])[:500])
    total_new=0
    for gid,p in summary['pools'].items():
        total_new+=p['new_abnormal_pages']
        lines.extend(['', '**'+p['name']+'（池 '+gid+'）**',
            '新增异常Page '+str(p['new_abnormal_pages'])+'｜扫描Ad '+str(p['scanned_ads'])+'｜PUR合格 '+str(p['pur_eligible']),
            '已替换待确认 '+str(p['changed'])+'｜审核中 '+str(p['pending_review'])+'｜可投放 '+str(p['deliverable']),
            '失败 '+str(p['failed'])+'｜未知 '+str(p['unknown'])+'｜待处理 '+str(p['waiting'])+'｜跳过 '+str(p['skipped'])])
    if total_new==0 and not summary.get('error'):lines.insert(2,'本轮无新增异常 Page。')
    if summary.get('details'):
        lines.extend(['','**部分明细**'])
        for row in summary['details']:
            lines.append('Ad '+row['ad_id']+'：'+row['origin_page_id']+' → '+str(row.get('new_page_id') or '未选中')+'；'+row['status']+('；'+row['reason'] if row.get('reason') else ''))
    return dict(config={'wide_screen_mode':True},header={'template':'red' if summary.get('error') else 'blue',
                'title':{'tag':'plain_text','content':'FB自动换Page｜'+('上线验收' if acceptance else '本轮结果')}},
                elements=[{'tag':'markdown','content':'\n'.join(lines)[:16000]}])


class Sender:
    def __init__(self,env,http=None):
        from features.fb_page_assignment.notify import Notifier
        self.transport=Notifier(env,session=http)

    def send(self,row,acceptance=False):
        from features.fb_page_assignment.notify import DeliveryError
        token=self.transport.token()
        try:
            response=self.transport.http.post('https://open.feishu.cn/open-apis/im/v1/messages',
                params={'receive_id_type':'chat_id'},headers={'Authorization':'Bearer '+token},
                json={'receive_id':CHAT_ID,'msg_type':'interactive','content':encoded(card(json.loads(row['body']),acceptance)),
                      'uuid':row['uuid']},timeout=15)
            payload=response.json()
        except Exception:
            raise DeliveryError('feishu_send_unknown',uncertain=True) from None
        if payload.get('code')!=0:
            raise DeliveryError('feishu_rejected_'+str(payload.get('code')),uncertain=response.status_code>=500)
        message_id=str((payload.get('data') or {}).get('message_id') or '')
        if not message_id:raise DeliveryError('feishu_receipt_missing',uncertain=True)
        return message_id

    def deliver(self,store,acceptance=False):
        from features.fb_page_assignment.notify import DeliveryError
        for row in store.notifications():
            # Persist the intent before networking. A crash here is unknown,
            # not a license to send another group message on process restart.
            store.notification_result(row['run_id'],'sending')
            try:
                message_id=self.send(row,acceptance)
                store.notification_result(row['run_id'],'sent',message_id=message_id)
            except DeliveryError as exc:
                retry=(datetime.now(timezone.utc)+timedelta(seconds=min(3600,60*2**min(row['attempts'],6)))).isoformat()
                store.notification_result(row['run_id'],'unknown' if exc.uncertain else 'failed',error=exc.code,next_at=retry)
