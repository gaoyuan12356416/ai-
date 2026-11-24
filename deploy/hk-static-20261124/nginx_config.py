"""Render only the two includes owned by the HK public static canary."""
import json
import re
from pathlib import Path


def nginx_map(source, variable, rows, default='""'):
    entries = ['map %s $%s {' % (source, variable), '    default %s;' % default]
    entries.extend('    %s %s;' % row for row in rows)
    return '\n'.join(entries + ['}', ''])


def uri_key(url):
    if not re.fullmatch(r'/[a-z0-9-]+\.(?:js|css|html)(?:\?v=[A-Za-z0-9-]+)?', url):
        raise ValueError('invalid public URL')
    # Use a case-sensitive expression. String keys in an nginx map ignore case.
    return '"~^' + url.replace('.', '[.]').replace('?', '[?]') + '$"'


def render(manifest, percent, guard_port=19985, live_root='/usr/share/nginx/html',
           release_public='/var/www/ai-hk-static/current/public'):
    if percent not in (0, 10, 50, 100):
        raise ValueError('unsupported canary stage')
    for path in (live_root, release_public):
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)) or '..' in Path(path).parts:
            raise ValueError('unsafe nginx path')
    files, urls = manifest['files'], manifest['urls']
    if any(record['response_headers'] for record in files.values()):
        raise ValueError('a new origin response policy requires review')
    native, scopes, controls, etags = [], [], [], []
    for url, entry in urls.items():
        name = entry['file']
        if url.split('?')[0] != '/' + name:
            raise ValueError('unexpected URL mapping')
        key = uri_key(url)
        native.append((key, '/' + name))
        scopes.append((key, 'page' if name.endswith('.html') else 'asset'))
        controls.append((key, json.dumps(entry['cache_control'])))
        etags.append((key, json.dumps(files[name]['etag'])))
    prefix = '# Managed by hk-static-20261124.\n'
    maps = [nginx_map('$request_uri', 'hk_asset_kind', scopes),
            nginx_map('$request_uri', 'hk_asset_native', native, '/__hk_no_public_asset__'),
            nginx_map('$request_uri', 'hk_asset_cache', controls),
            nginx_map('$request_uri', 'hk_asset_tag', etags),
            nginx_map('$sent_http_content_encoding', 'hk_encoded_tag',
                      [('gzip', '"W/$hk_asset_tag"')], '$hk_asset_tag'),
            nginx_map('$http_referer', 'hk_page_ref',
                      [('"~^https://ai[.]yingliangads[.]com/(?:pipeline-monitor|social-analytics)[.]html(?:[?].*)?$"', '1')], '0'),
            nginx_map('"$hk_asset_kind|$hk_page_ref"', 'hk_asset_in_scope',
                      [('"page|0"', '1'), ('"page|1"', '1'), ('"asset|1"', '1')], '0')]
    header_vars = ('request_method', 'http_range', 'http_if_range', 'http_if_none_match',
                   'http_if_modified_since', 'http_if_match', 'http_if_unmodified_since', 'http_authorization')
    ordinary = '"' + '|'.join('$' + key for key in header_vars) + '"'
    maps.append(nginx_map(ordinary, 'hk_asset_ordinary',
                          [(json.dumps(method + '|' * (len(header_vars) - 1)), '1')
                           for method in ('GET', 'HEAD')], '0'))
    if percent in (0, 100):
        maps.append(nginx_map('$request_id', 'hk_asset_sample', [], str(percent // 100)))
    else:
        maps.append('split_clients "$request_id" $hk_asset_sample {\n    %s%% 1;\n    * 0;\n}\n' % percent)
    maps.extend([nginx_map('"$remote_addr|$http_x_ai_static_probe"', 'hk_asset_group',
                           [('"127.0.0.1|verify"', '1'), ('"127.0.0.1|native"', '0')], '$hk_asset_sample'),
                 nginx_map('$http_user_agent', 'hk_static_operator', [('"~^Offload-static-validation/"', '1')], '0'),
                 nginx_map('"$host|$hk_asset_in_scope|$hk_asset_ordinary|$hk_asset_group"', 'hk_static_take',
                           [('"ai.yingliangads.com|1|1|1"', '1')], '0'),
                 nginx_map('$uri', 'hk_static_route',
                           [('/' + name, '$hk_static_take') for name in files], '0')])
    server = r'''
if ($hk_static_route) {
    rewrite "^/([a-z0-9-]+[.](?:html|js|css))$" /__ai_hk_check/$1? last;
}
location ^~ /__ai_hk_check/ {
    internal;
    proxy_pass http://127.0.0.1:GUARD_PORT/;
    proxy_pass_request_body off;
    proxy_pass_request_headers off;
    proxy_set_header Content-Length "";
    proxy_set_header Host localhost;
    proxy_set_header X-Static-Operator $hk_static_operator;
    proxy_connect_timeout 200ms;
    proxy_send_timeout 500ms;
    proxy_read_timeout 500ms;
    proxy_next_upstream off;
    proxy_intercept_errors on;
    error_page 400 403 404 405 409 500 502 503 504 = @hk_static_native;
}
location ^~ /__ai_hk_files/ {
    internal;
    alias RELEASE_PUBLIC/;
    gzip_static on;
    gzip_vary on;
    etag off;
    add_header ETag $hk_encoded_tag;
    add_header Cache-Control $hk_asset_cache;
    add_header X-AI-Static-Source hk-release;
    error_page 403 404 = @hk_static_native;
}
location @hk_static_native {
    root LIVE_ROOT;
    try_files $hk_asset_native =404;
    add_header Cache-Control $hk_asset_cache;
    add_header X-AI-Static-Source native-fallback;
}
'''
    return (prefix + '\n'.join(maps), prefix + server.replace('GUARD_PORT', str(int(guard_port)))
            .replace('RELEASE_PUBLIC', str(release_public)).replace('LIVE_ROOT', str(live_root)))
