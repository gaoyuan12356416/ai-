import base64
import html as html_lib
import json
import mimetypes
import os
import posixpath
import re
from urllib.parse import unquote


META_CTA_HOOK = "FbPlayableAd.onCTAClick"


class PlayableCompatibilityError(ValueError):
    pass


def _read_text(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _mime_type(path):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path.lower().endswith(".wasm"):
        return "application/wasm"
    if path.lower().endswith(".json"):
        return "application/json"
    return mime


def _is_external_url(value):
    text = str(value or "").strip()
    return bool(re.match(r"^(?:https?:)?//", text, re.I))


def _is_passthrough_url(value):
    text = str(value or "").strip()
    return not text or text.startswith(("data:", "blob:", "#", "about:"))


def _resource_key(value, base_dir=""):
    text = html_lib.unescape(str(value or "").strip())
    if _is_external_url(text):
        raise PlayableCompatibilityError("external resource is not Meta-compatible: %s" % text)
    if _is_passthrough_url(text):
        return ""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text):
        raise PlayableCompatibilityError("unsupported resource URL: %s" % text)
    text = unquote(text.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
    text = posixpath.normpath(posixpath.join(base_dir, text)).lstrip("/")
    if text == ".." or text.startswith("../"):
        raise PlayableCompatibilityError("resource escapes playable root: %s" % value)
    return text


def _collect_resources(game_dir, entry_path):
    entry_path = os.path.abspath(entry_path)
    entry_dir = os.path.dirname(entry_path)
    resources = {}
    for current_root, _, filenames in os.walk(game_dir):
        for filename in filenames:
            path = os.path.abspath(os.path.join(current_root, filename))
            if path == entry_path:
                continue
            key = os.path.relpath(path, entry_dir).replace(os.sep, "/")
            if key == ".." or key.startswith("../"):
                raise PlayableCompatibilityError("playable resource is outside entry directory: %s" % key)
            with open(path, "rb") as handle:
                content = handle.read()
            if path.lower().endswith(('.js', '.mjs')):
                content = _patch_javascript(_read_text(path), key).encode('utf-8')
            resources[key] = {
                "path": path,
                "mime": _mime_type(path),
                "base64": base64.b64encode(content).decode("ascii"),
            }
    return resources


def _data_uri(resource):
    return "data:%s;base64,%s" % (resource["mime"], resource["base64"])


def _patch_javascript(source, source_name):
    patched = re.sub(r"\bXMLHttpRequest\b", "__PlayableXHR", source)
    patched = re.sub(r"\bfetch\b", "__playableRead", patched)
    patched = re.sub(
        r"function\s+_dmSysOpenURL\s*\(e,r\)\s*\{.*?\}(?=\s*function\s+_emscripten_)",
        'function _dmSysOpenURL(e,r){window.parent.postMessage({type:"meta-playable-cta"},"*");return!0}',
        patched,
        flags=re.S,
    )
    redirect_patterns = (
        r"\bwindow\s*\.\s*open\s*\(",
        r"\b(?:window|document|top|parent)\s*\.\s*location\s*=",
        r"\blocation\s*\.\s*(?:assign|replace)\s*\(",
        r"\blocation\s*\.\s*href\s*=",
    )
    for pattern in redirect_patterns:
        if re.search(pattern, patched, re.I):
            raise PlayableCompatibilityError(
                "direct JavaScript redirect remains in %s" % source_name
            )
    return patched


def _rewrite_css(source, resources, base_dir, source_name):
    pattern = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", re.I)

    def replace(match):
        raw = match.group(2).strip()
        if _is_passthrough_url(raw):
            return match.group(0)
        key = _resource_key(raw, base_dir)
        resource = resources.get(key)
        if not resource:
            raise PlayableCompatibilityError(
                "missing CSS resource %s referenced by %s" % (key, source_name)
            )
        return 'url("%s")' % _data_uri(resource)

    return pattern.sub(replace, source)


def _attribute_value(attributes, name):
    match = re.search(
        r"\b%s\s*=\s*(['\"])(.*?)\1" % re.escape(name),
        attributes,
        re.I | re.S,
    )
    return html_lib.unescape(match.group(2)) if match else ""


def _remove_attribute(attributes, name):
    return re.sub(
        r"\s+%s\s*=\s*(['\"])(.*?)\1" % re.escape(name),
        "",
        attributes,
        flags=re.I | re.S,
    )


def _inline_scripts(document, resources):
    script_src_pattern = re.compile(
        r"<script(?P<attrs>[^>]*\bsrc\s*=\s*(['\"])(?P<src>.*?)\2[^>]*)>\s*</script>",
        re.I | re.S,
    )

    def replace_external_script(match):
        src = html_lib.unescape(match.group("src"))
        key = _resource_key(src)
        resource = resources.get(key)
        if not resource:
            raise PlayableCompatibilityError("missing script resource: %s" % key)
        code = _patch_javascript(_read_text(resource["path"]), key)
        code = re.sub(r"</script", r"<\\/script", code, flags=re.I)
        attrs = _remove_attribute(match.group("attrs"), "src")
        attrs = _remove_attribute(attrs, "integrity")
        attrs = _remove_attribute(attrs, "crossorigin")
        return "<script%s>%s</script>" % (attrs, code)

    document = script_src_pattern.sub(replace_external_script, document)
    inline_pattern = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.I | re.S)

    def patch_inline_script(match):
        attrs = match.group("attrs")
        script_type = _attribute_value(attrs, "type").lower()
        if script_type and "javascript" not in script_type and script_type != "module":
            return match.group(0)
        code = _patch_javascript(match.group("body"), "inline script")
        return "<script%s>%s</script>" % (attrs, code)

    return inline_pattern.sub(patch_inline_script, document)


def _inline_links(document, resources):
    link_pattern = re.compile(r"<link(?P<attrs>[^>]*)>", re.I | re.S)

    def replace_link(match):
        attrs = match.group("attrs")
        href = _attribute_value(attrs, "href")
        rel = _attribute_value(attrs, "rel").lower()
        if not href:
            return match.group(0)
        if _is_passthrough_url(href):
            return match.group(0)
        key = _resource_key(href)
        resource = resources.get(key)
        if not resource:
            raise PlayableCompatibilityError("missing linked resource: %s" % key)
        if "stylesheet" in rel:
            css = _rewrite_css(_read_text(resource["path"]), resources, posixpath.dirname(key), key)
            return "<style>%s</style>" % css
        if any(item in rel for item in ("manifest", "preload", "prefetch", "modulepreload")):
            return ""
        return match.group(0).replace(href, _data_uri(resource), 1)

    return link_pattern.sub(replace_link, document)


def _inline_style_blocks(document, resources):
    pattern = re.compile(r"<style(?P<attrs>[^>]*)>(?P<body>.*?)</style>", re.I | re.S)

    def replace(match):
        body = _rewrite_css(match.group("body"), resources, "", "inline style")
        return "<style%s>%s</style>" % (match.group("attrs"), body)

    return pattern.sub(replace, document)


def _inline_media_attributes(document, resources):
    tag_pattern = re.compile(r"<(?P<tag>img|audio|video|source|track)\b(?P<attrs>[^>]*)>", re.I | re.S)

    def replace_tag(match):
        attrs = match.group("attrs")
        for attr_name in ("src", "poster"):
            value = _attribute_value(attrs, attr_name)
            if not value or _is_passthrough_url(value):
                continue
            key = _resource_key(value)
            resource = resources.get(key)
            if not resource:
                raise PlayableCompatibilityError("missing media resource: %s" % key)
            attrs = attrs.replace(value, _data_uri(resource), 1)
        return "<%s%s>" % (match.group("tag"), attrs)

    return tag_pattern.sub(replace_tag, document)


def _reject_external_markup(document):
    external = re.search(
        r"<(?:script|link|img|audio|video|source|track|iframe|a)\b[^>]*\b(?:src|href|poster)\s*=\s*(['\"])((?:https?:)?//.*?)\1",
        document,
        re.I | re.S,
    )
    if external:
        raise PlayableCompatibilityError("external markup URL remains: %s" % external.group(2))


def _loader_shim(resources):
    encoded = {
        key: [item["mime"], item["base64"]]
        for key, item in sorted(resources.items())
    }
    files_json = json.dumps(encoded, ensure_ascii=True, separators=(",", ":"))
    return """<script>
(function(){
  var __playableEncodedFiles=%s;
  var __playableByteCache={};
  function __playableKey(value){
    var text=String(value||'').split(String.fromCharCode(92)).join('/').split('#')[0].split('?')[0];
    if(/^[A-Za-z][A-Za-z0-9+.-]*:/.test(text)||text.indexOf('//')===0){throw new Error('external resource blocked: '+text);}
    while(text.indexOf('./')===0){text=text.slice(2);}
    while(text.indexOf('/')===0){text=text.slice(1);}
    var parts=[];
    text.split('/').forEach(function(part){
      if(!part||part==='.'){return;}
      if(part==='..'){if(!parts.length){throw new Error('resource path escapes root');}parts.pop();return;}
      parts.push(part);
    });
    try{return decodeURIComponent(parts.join('/'));}catch(err){return parts.join('/');}
  }
  function __playableFile(value){
    var key=__playableKey(value);
    var meta=__playableEncodedFiles[key];
    if(!meta){throw new Error('embedded resource not found: '+key);}
    var bytes=__playableByteCache[key];
    if(!bytes){
      var binary=atob(meta[1]);
      bytes=new Uint8Array(binary.length);
      for(var i=0;i<binary.length;i+=1){bytes[i]=binary.charCodeAt(i);}
      __playableByteCache[key]=bytes;
    }
    return {key:key,mime:meta[0],bytes:bytes};
  }
  function __playableText(bytes){
    if(typeof TextDecoder==='function'){return new TextDecoder('utf-8').decode(bytes);}
    var binary='';
    for(var i=0;i<bytes.length;i+=1){binary+=String.fromCharCode(bytes[i]);}
    try{return decodeURIComponent(escape(binary));}catch(err){return binary;}
  }
  function __PlayableXHR(){
    this.method='GET';this.url='';this.async=true;this.readyState=0;this.status=0;
    this.responseType='';this.response=null;this.responseText='';this.responseURL='';
    this.onload=null;this.onerror=null;this.onprogress=null;this.onreadystatechange=null;
    this._listeners={};this._responseHeaders={};this.upload={};
  }
  __PlayableXHR.prototype.open=function(method,url,async){this.method=String(method||'GET').toUpperCase();this.url=url;this.async=async!==false;this.readyState=1;};
  __PlayableXHR.prototype.setRequestHeader=function(){};
  __PlayableXHR.prototype.overrideMimeType=function(){};
  __PlayableXHR.prototype.abort=function(){};
  __PlayableXHR.prototype.addEventListener=function(type,handler){(this._listeners[type]||(this._listeners[type]=[])).push(handler);};
  __PlayableXHR.prototype.removeEventListener=function(type,handler){var list=this._listeners[type]||[];this._listeners[type]=list.filter(function(item){return item!==handler;});};
  __PlayableXHR.prototype._emit=function(type,event){
    var handler=this['on'+type];if(typeof handler==='function'){handler.call(this,event);}
    (this._listeners[type]||[]).slice().forEach(function(item){item.call(this,event);},this);
  };
  __PlayableXHR.prototype.getResponseHeader=function(name){return this._responseHeaders[String(name||'').toLowerCase()]||null;};
  __PlayableXHR.prototype.getAllResponseHeaders=function(){var self=this;return Object.keys(this._responseHeaders).map(function(key){return key+': '+self._responseHeaders[key];}).join(String.fromCharCode(13,10));};
  __PlayableXHR.prototype.send=function(){
    var self=this;
    function complete(){
      try{
        var file=__playableFile(self.url);
        var bytes=file.bytes;
        self.status=200;self.readyState=4;self.responseURL='embedded://'+file.key;
        self._responseHeaders={'content-length':String(bytes.length),'content-type':file.mime};
        if(self.method==='HEAD'){self.response='';self.responseText='';}
        else if(self.responseType==='arraybuffer'){self.response=bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength);}
        else if(self.responseType==='blob'&&typeof Blob==='function'){self.response=new Blob([bytes],{type:file.mime});}
        else if(self.responseType==='json'){self.response=JSON.parse(__playableText(bytes));}
        else{self.responseText=__playableText(bytes);self.response=self.responseText;}
        self._emit('progress',{lengthComputable:true,loaded:bytes.length,total:bytes.length,target:self});
        if(typeof self.onreadystatechange==='function'){self.onreadystatechange();}
        self._emit('load',{target:self});
        self._emit('loadend',{target:self});
      }catch(err){
        self.status=404;self.readyState=4;
        if(typeof self.onreadystatechange==='function'){self.onreadystatechange();}
        self._emit('error',{target:self,error:err});self._emit('loadend',{target:self});
      }
    }
    if(this.async){setTimeout(complete,0);}else{complete();}
  };
  function __playableRead(value){
    try{
      var file=__playableFile(value);
      if(typeof Response==='function'){
        return Promise.resolve(new Response(file.bytes,{status:200,headers:{'Content-Type':file.mime,'Content-Length':String(file.bytes.length)}}));
      }
      return Promise.resolve({ok:true,status:200,headers:{get:function(name){return String(name).toLowerCase()==='content-length'?String(file.bytes.length):file.mime;}},arrayBuffer:function(){return Promise.resolve(file.bytes.buffer.slice(file.bytes.byteOffset,file.bytes.byteOffset+file.bytes.byteLength));},text:function(){return Promise.resolve(__playableText(file.bytes));},json:function(){return Promise.resolve(JSON.parse(__playableText(file.bytes)));}});
    }catch(err){return Promise.reject(err);}
  }
  window.__PlayableXHR=__PlayableXHR;
  window.__playableRead=__playableRead;
  window.__playableFiles=__playableEncodedFiles;
})();
</script>""" % files_json


def _inject_head(document, content):
    if re.search(r"<head\b[^>]*>", document, re.I):
        return re.sub(r"(<head\b[^>]*>)", r"\1" + content, document, count=1, flags=re.I)
    return content + document


def _inner_document(game_dir, entry_relative):
    game_dir = os.path.abspath(game_dir)
    entry_path = os.path.abspath(os.path.join(game_dir, entry_relative.replace("/", os.sep)))
    if not entry_path.startswith(game_dir + os.sep) or not os.path.isfile(entry_path):
        raise PlayableCompatibilityError("playable entry is missing or unsafe: %s" % entry_relative)
    resources = _collect_resources(game_dir, entry_path)
    document = _read_text(entry_path)
    document = _inline_links(document, resources)
    document = _inline_style_blocks(document, resources)
    document = _inline_media_attributes(document, resources)
    document = _inline_scripts(document, resources)
    _reject_external_markup(document)
    document = _inject_head(document, _loader_shim(resources))
    return document, resources


def _safe_json_for_script(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _outer_document(inner_document, title, play_count, trial_seconds, translations):
    escaped_inner = html_lib.escape(inner_document, quote=False)
    safe_title = html_lib.escape(str(title or "Playable Preview"))
    config_json = _safe_json_for_script({
        "playCount": max(1, int(play_count or 1)),
        "trialSeconds": max(1, min(120, int(trial_seconds or 20))),
        "translations": translations or {},
    })
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
  <title>%s</title>
  <style>
    html,body{margin:0;width:100%%;height:100%%;overflow:hidden;background:#05070a;font-family:Arial,Helvetica,sans-serif;color:#fff}
    #game-frame{position:fixed;inset:0;width:100%%;height:100%%;border:0;background:#000}
    #timer{position:fixed;top:12px;right:12px;z-index:5;padding:7px 10px;border-radius:999px;background:rgba(0,0,0,.55);font-size:13px}
    #plays{position:fixed;top:12px;left:12px;z-index:5;padding:7px 10px;border-radius:999px;background:rgba(0,0,0,.55);font-size:13px}
    #overlay{position:fixed;inset:0;z-index:10;display:none;align-items:center;justify-content:center;padding:24px;background:rgba(5,7,10,.82)}
    #overlay.show{display:flex}.panel{width:min(380px,100%%);text-align:center}.headline{margin:0 0 8px;font-size:25px}.subcopy{margin:0 0 18px;color:rgba(255,255,255,.78)}
    .actions{display:flex;flex-direction:column;gap:10px}button{border:0;border-radius:10px;padding:15px 18px;font-size:17px;font-weight:800;cursor:pointer}
    #install-button{color:#050505;background:#fff}#replay-button{color:#fff;background:rgba(255,255,255,.16)}
  </style>
</head>
<body>
  <template id="game-source">%s</template>
  <iframe id="game-frame" title="Playable game" allow="autoplay; fullscreen; gamepad; accelerometer; gyroscope"></iframe>
  <div id="plays">Plays: 1</div><div id="timer">20s</div>
  <div id="overlay" aria-live="polite"><div class="panel">
    <h1 class="headline" data-i18n="headline">Install to Play More</h1>
    <p class="subcopy" data-i18n="subtitle">Your playable preview has ended.</p>
    <div class="actions"><button id="install-button" type="button">Install to Play More</button><button id="replay-button" type="button">Play Again</button></div>
  </div></div>
  <script>
  (function(){
    var cfg=%s,frame=document.getElementById('game-frame'),timer=document.getElementById('timer'),plays=document.getElementById('plays'),overlay=document.getElementById('overlay'),replay=document.getElementById('replay-button');
    var innerSource=document.getElementById('game-source').content.textContent;
    var attempts=0,timeoutId=0,tickId=0;
    function langCopy(){var table=cfg.translations||{},langs=(navigator.languages&&navigator.languages.length?navigator.languages:[navigator.language||'en']);for(var i=0;i<langs.length;i+=1){var key=String(langs[i]||'').toLowerCase().replace(/_/g,'-');if(table[key]){return table[key];}var base=key.split('-')[0];if(table[base]){return table[base];}}return table.en||{};}
    var copy=langCopy();document.querySelector('[data-i18n="headline"]').textContent=copy.headline||'Install to Play More';document.querySelector('[data-i18n="subtitle"]').textContent=copy.subtitle||'Your playable preview has ended.';document.getElementById('install-button').textContent=copy.cta||'Install to Play More';
    function stopTimers(){clearTimeout(timeoutId);clearInterval(tickId);}
    function endTrial(){stopTimers();timer.textContent='0s';replay.style.display=attempts<cfg.playCount?'block':'none';overlay.classList.add('show');frame.style.pointerEvents='none';}
    function startTrial(){stopTimers();attempts+=1;plays.textContent=(copy.plays||'Plays')+': '+attempts+'/'+cfg.playCount;overlay.classList.remove('show');frame.style.pointerEvents='auto';frame.srcdoc=innerSource;var remaining=cfg.trialSeconds;timer.textContent=remaining+'s';tickId=setInterval(function(){remaining=Math.max(0,remaining-1);timer.textContent=remaining+'s';},1000);timeoutId=setTimeout(endTrial,cfg.trialSeconds*1000);}
    function install(){if(window.FbPlayableAd&&typeof window.FbPlayableAd.onCTAClick==='function'){window.FbPlayableAd.onCTAClick();}}
    window.addEventListener('message',function(event){if(!event||!event.data){return;}if(event.data.type==='meta-playable-cta'){install();}else if(event.data.type==='meta-playable-game-over'){endTrial();}});
    document.getElementById('install-button').addEventListener('click',install);replay.addEventListener('click',startTrial);startTrial();
  })();
  </script>
</body>
</html>
""" % (safe_title, escaped_inner, config_json)


def validate_meta_playable_html(document):
    checks = {
        "native_xhr": r"\bXMLHttpRequest\b",
        "native_fetch": r"\bfetch\s*\(",
        "window_open": r"\bwindow\s*\.\s*open\s*\(",
        "direct_location": r"\b(?:window|document|top|parent)\s*\.\s*location\s*=|\blocation\s*\.\s*(?:href\s*=|assign\s*\(|replace\s*\()",
        "external_markup": r"<(?:script|link|img|audio|video|source|track|iframe|a)\b[^>]*\b(?:src|href|poster)\s*=\s*(['\"])(?:https?:)?//",
    }
    failures = [name for name, pattern in checks.items() if re.search(pattern, document, re.I | re.S)]
    if META_CTA_HOOK not in document:
        failures.append("missing_meta_cta_hook")
    if failures:
        raise PlayableCompatibilityError("Meta compatibility validation failed: %s" % ", ".join(failures))
    return {
        "single_file": True,
        "native_network_requests": 0,
        "direct_redirects": 0,
        "cta_hook": META_CTA_HOOK,
    }


def build_meta_playable_html(game_dir, entry_relative, title, play_count, trial_seconds, translations):
    inner, resources = _inner_document(game_dir, entry_relative)
    document = _outer_document(inner, title, play_count, trial_seconds, translations)
    compatibility = validate_meta_playable_html(document)
    compatibility["embedded_file_count"] = len(resources)
    compatibility["source_entry"] = entry_relative.replace("\\", "/")
    return document, compatibility
