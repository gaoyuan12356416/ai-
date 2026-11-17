"""Copy the exact readable Meta video source with resumable, receipted upload."""
import hashlib
from pathlib import Path
import time
from urllib.parse import urlsplit

import requests

from .meta import MetaError


def copy_video(graph,account_id,video_id,root,write):
    root=Path(root);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    video=graph.get(video_id,fields='id,source')
    source=video.get('source')
    if not source or urlsplit(source).scheme not in {'http','https'}:
        raise ValueError('original_video_source_unavailable')
    target=root/(str(video_id)+'.mp4')
    part=target.with_suffix('.part')
    if not target.exists():
        session=requests.Session();session.trust_env=False
        try:
            with session.get(source,stream=True,timeout=(5,60)) as response:
                response.raise_for_status()
                with part.open('wb') as out:
                    for block in response.iter_content(1024*1024):out.write(block)
            if not part.stat().st_size:raise ValueError('original_video_empty')
            part.replace(target)
        except requests.RequestException:
            raise MetaError('original_video_download_failed',transient=True) from None
        finally:session.close()
    size=target.stat().st_size
    with target.open('rb') as source_file:
        checksum=hashlib.sha256()
        for chunk in iter(lambda:source_file.read(1024*1024),b''):checksum.update(chunk)
    start=write('video_start',str(account_id)+'/advideos',{'upload_phase':'start','file_size':size},
                record={'upload_phase':'start','file_size':size,'original_video_id':str(video_id),'sha256':checksum.hexdigest()})
    if not start.get('video_id') or not start.get('upload_session_id'):
        raise MetaError('video_start_receipt_missing',uncertain=True)
    sid,new_id=str(start['upload_session_id']),str(start['video_id'])
    offset,end=int(start.get('start_offset',0)),int(start['end_offset'])
    with target.open('rb') as source_file:
        while offset<end:
            source_file.seek(offset);chunk=source_file.read(end-offset)
            if len(chunk)!=end-offset:raise ValueError('video_source_chunk_short')
            response=write('video_transfer',str(account_id)+'/advideos',
                {'upload_phase':'transfer','upload_session_id':sid,'start_offset':offset},
                files={'video_file_chunk':('chunk',chunk,'application/octet-stream')},
                record={'video_id':new_id,'upload_session_id':sid,'start_offset':offset,'end_offset':end,
                        'chunk_sha256':hashlib.sha256(chunk).hexdigest()})
            next_offset,next_end=int(response['start_offset']),int(response['end_offset'])
            if next_offset<end or next_end<next_offset or next_end>size:
                raise ValueError('video_transfer_offsets_invalid')
            offset,end=next_offset,next_end
    if offset!=size:raise ValueError('video_transfer_incomplete')
    result=write('video_finish',str(account_id)+'/advideos',{'upload_phase':'finish','upload_session_id':sid},
                 record={'video_id':new_id,'upload_session_id':sid,'file_size':size,'sha256':checksum.hexdigest()})
    if result.get('success') is not True:raise MetaError('video_finish_receipt_missing',uncertain=True)
    for _ in range(60):
        status=graph.get(new_id,fields='id,status').get('status') or {}
        if status.get('video_status')=='ready':return new_id
        if status.get('video_status')=='error':raise ValueError('copied_video_processing_failed')
        time.sleep(2)
    # The media was accepted: never restart the upload just because processing
    # exceeded this observation interval. Preserve the video/session receipts.
    raise MetaError('copied_video_processing_pending',uncertain=True)
