"""Allowlisted numeric facts only; response text and credentials are never stored."""
def number(value):
    return value if type(value) is int and value>=0 else None

def extract(post,media):
    public=post.get('public_metrics') or {}
    organic=post.get('organic_metrics') or {}
    private=post.get('non_public_metrics') or {}
    promoted=post.get('promoted_metrics') or {}
    result={key:number(source.get(field)) for key,source,field in [
        ('impressions',public,'impression_count'),('organic_impressions',organic,'impression_count'),
        ('promoted_impressions',promoted,'impression_count'),('likes',public,'like_count'),
        ('reposts',public,'retweet_count'),('organic_reposts',organic,'retweet_count'),
        ('bookmarks',public,'bookmark_count'),('replies',public,'reply_count'),('quotes',public,'quote_count'),
        ('profile_clicks',organic if 'user_profile_clicks' in organic else private,'user_profile_clicks'),
        ('url_clicks',organic if 'url_link_clicks' in organic else private,'url_link_clicks')]}
    keys=post.get('attachments',{}).get('media_keys',[])
    videos=[m for m in media if m.get('media_key') in keys and m.get('type')=='video']
    result['media_keys']=keys
    result['video_metrics_scope']='media_aggregate_not_post_attribution'
    if len(videos)==1:
        m=videos[0]
        op=m.get('organic_metrics') or {}
        np=m.get('non_public_metrics') or {}
        result['video_views']=number((op if 'view_count' in op else m.get('public_metrics',{})).get('view_count'))
        result['watch_time_ms']=number(op.get('watch_time_ms'))
        result['duration_ms']=number(m.get('duration_ms'))
        for percent in (0,25,50,75,100):
            field='playback_%d_count'%percent
            result[field]=number((op if field in op else np).get(field))
    return result
