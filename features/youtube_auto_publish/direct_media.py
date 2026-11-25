"""The custom library's HTTPS Tianmai video origin for YouTube only."""
from urllib.parse import unquote, urlsplit

TIANMAI_HOST = 'marketing.tianmai.cn'


def is_tianmai_video(url):
    try:
        parsed = urlsplit(str(url or ''))
        path = unquote(parsed.path)
        return (parsed.scheme == 'https' and parsed.netloc.lower() in (TIANMAI_HOST, TIANMAI_HOST + ':443')
                and not parsed.fragment and path.startswith('/materialcopy/')
                and path.lower().endswith('.mp4') and '\\' not in path
                and not any(part in ('.', '..') for part in path.split('/')))
    except ValueError:
        return False


def youtube_download_hosts(url, hosts):
    return (*hosts, TIANMAI_HOST) if is_tianmai_video(url) else hosts
