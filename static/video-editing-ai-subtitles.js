/* Subtitle files stay in the browser. The same canvas draws preview and export. */
(() => {
  'use strict';
  function stamp(text) {
    const m = /^(?:(\d+):)?(\d{2}):([0-5]\d)[.,](\d{1,3})$/.exec(text);
    if (!m || (m[1] && +m[2]>59)) throw new Error('字幕时间格式无效：'+text);
    return ((+(m[1]||0)*60 + +m[2])*60 + +m[3])*1000 + +(m[4].padEnd(3,'0'));
  }
  function time(ms,dot=',') {
    const t=Math.round(Math.max(0,ms)),pad=n=>String(n).padStart(2,'0');
    return pad(Math.floor(t/3600000))+':'+pad(Math.floor(t/60000)%60)+':'+pad(Math.floor(t/1000)%60)+dot+String(t%1000).padStart(3,'0');
  }
  function parse(text,label) {
    const clean=text.replace(/^\uFEFF/,'').replace(/\r\n?/g,'\n').trim();
    if(!clean) return [];
    const cues=[];
    for(const chunk of clean.split(/\n[ \t]*\n/g)) {
      const lines=chunk.split('\n');
      if(/^WEBVTT(?:\s|$)|^(?:NOTE|STYLE|REGION)(?:\s|$)/.test(lines[0])) continue;
      const ix=lines[0].includes('-->')?0:1;
      const range=/^(\S+)\s+-->\s+(\S+)(?:\s+.*)?$/.exec((lines[ix]||'').trim());
      if(!range) throw new Error(label+'第 '+(cues.length+1)+' 段缺少时间行');
      const start=stamp(range[1]),end=stamp(range[2]);
      const content=lines.slice(ix+1).join('\n').replace(/<\/?(?:b|i|u|ruby|rt|c|v|lang)(?:[.\s][^>]*)?>/gi,'')
        .replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').trim();
      if(end<=start || !content) throw new Error(label+'第 '+(cues.length+1)+' 段须有文字，且结束时间晚于开始时间');
      cues.push({start,end,text:content});
    }
    return cues.sort((a,b)=>a.start-b.start);
  }
  function merge(a,b,mode) {
    const events=new Map();
    function collect(cues,line) {
      for(const c of cues) for(const [at,enter] of [[c.start,true],[c.end,false]]) {
        if(!events.has(at)) events.set(at,[]);
        events.get(at).push({c,line,enter});
      }
    }
    if(mode!=='target') collect(a,0);
    if(mode!=='source') collect(b,1);
    const starts=[...events.keys()].sort((p,q)=>p-q), active=[new Set(),new Set()],result=[];
    starts.forEach((at,i)=>{
      events.get(at).forEach(e=>e.enter?active[e.line].add(e.c):active[e.line].delete(e.c));
      const next=starts[i+1], text=active.flatMap(s=>Array.from(s,c=>c.text)).join('\n');
      if(next===undefined || !text) return;
      const prev=result[result.length-1];
      if(prev && prev.end===at && prev.text===text) prev.end=next;
      else result.push({start:at,end:next,text});
    });
    return result;
  }
  function toSrt(cues) {
    return cues.map((c,i)=>String(i+1)+'\r\n'+time(c.start)+' --> '+time(c.end)+'\r\n'+c.text.replace(/\n/g,'\r\n')+'\r\n').join('\r\n');
  }
  function activeAt(cues,ms) {
    let lo=0,hi=cues.length;
    while(lo<hi){const mid=(lo+hi)>>1;if(cues[mid].end<=ms)lo=mid+1;else hi=mid;}
    return (cues[lo] && cues[lo].start<=ms) ? cues[lo].text : '';
  }
  function wrap(ctx,text,maxWidth) {
    const lines=[];
    for(const block of text.split('\n')) {
      let line='';
      const tokens=block.match(/\s+|[\u2E80-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF]|[^\s\u2E80-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF]+/gu)||[];
      for(const part of tokens) {
        if(line && ctx.measureText(line+part).width>maxWidth){lines.push(line.trimEnd());line='';}
        if(ctx.measureText(part).width<=maxWidth){line+=line?part:part.trimStart();continue;}
        for(const ch of Array.from(part)) {
          if(line && ctx.measureText(line+ch).width>maxWidth){lines.push(line.trimEnd());line='';}
          line+=ch;
        }
      }
      if(line.trim()) lines.push(line.trimEnd());
    }
    return lines;
  }
  function element(tag,label,cls) {
    const n=document.createElement(tag);if(label!=null)n.textContent=label;if(cls)n.className=cls;return n;
  }
  function open(taskOrId, options={}) {
    const taskId=typeof taskOrId==='string'?taskOrId:taskOrId?.id;
    const dlg=element('dialog',null,'vel-dialog');dlg.setAttribute('aria-label','字幕、水印与缩放');
    dlg.innerHTML='<header><h2>字幕、水印与缩放</h2><button type="button" data-action="close" class="vel-secondary">关闭</button></header>'+
      '<div class="vel-body"><div class="vel-grid"><label class="vel-field"><span>本地视频</span><input data-field="file" type="file" accept="video/*"></label>'+
      '<label class="vel-field"><span>字幕显示</span><select data-field="mode"><option value="source">原文字幕</option><option value="target">译文字幕</option><option value="both">双语字幕</option></select></label></div>'+
      '<video hidden playsinline preload="auto"></video><canvas class="vel-sub-preview" hidden aria-label="字幕合成预览画面"></canvas>'+
      '<div class="vel-actions"><button type="button" data-action="play" class="vel-secondary">播放</button><input type="range" data-field="seek" min="0" max="0" step="0.01" value="0" aria-label="播放进度" style="flex:1"><output data-field="position">00:00:00.000</output></div>'+
      '<div class="vel-grid" data-block="editors"></div><div class="vel-grid"><label class="vel-field"><span>字号（占画面宽度）</span><select data-field="size"><option value="0.035">3.5%</option><option value="0.05" selected>5%</option><option value="0.07">7%</option></select></label>'+
      '<label class="vel-field"><span>位置</span><select data-field="height"><option value=".2">顶部</option><option value=".5">居中</option><option value=".82" selected>底部</option><option value=".94">贴近底边</option></select></label>'+
      '<label class="vel-field"><span>对齐</span><select data-field="align"><option value="center">居中</option><option value="left">居左</option><option value="right">居右</option></select></label>'+
      '<label class="vel-field"><span>文字颜色</span><input type="color" value="#ffffff" data-field="color"></label></div>'+
      '<div class="vel-actions"><label class="vel-check"><input type="checkbox" data-field="outline" checked>深色描边</label><label class="vel-check"><input type="checkbox" data-field="bold" checked>粗体</label></div>'+
      '<div class="vel-grid"><label class="vel-field"><span>添加配音音轨</span><input type="file" accept="audio/*" data-field="audioFile"></label>'+
      '<label class="vel-field"><span>原片音量</span><select data-field="originalVolume"><option value="1">100%</option><option value=".3">30%</option><option value=".15">15%</option><option value="0">静音</option></select></label>'+
      '<label class="vel-field"><span>配音开始位置（秒）</span><input data-field="voiceStart" type="number" min="0" value="0" step=".1"></label></div>'+
      '<p data-field="voiceName" class="vel-hint"></p><div class="vel-grid"><label class="vel-field"><span>文字水印</span><input type="text" data-field="watermark" maxlength="80" placeholder="可选，例如品牌名"></label>'+
      '<label class="vel-field"><span>水印位置</span><select data-field="markPosition"><option value="tl">左上</option><option value="tr">右上</option><option value="bl">左下</option><option value="br">右下</option></select></label>'+
      '<label class="vel-field"><span>水印透明度</span><select data-field="markOpacity"><option value="1">100%</option><option value=".75">75%</option><option value=".50">50%</option></select></label>'+
      '<label class="vel-field"><span>画面缩放</span><select data-field="zoom"><option value="1">100%</option><option value="1.05">105%</option><option value="1.1">110%</option><option value="1.15">115%</option><option value="1.2">120%</option><option value="1.25">125%</option><option value="1.3">130%</option><option value="1.5">150%</option></select></label></div>'+
      '<div class="vel-actions"><button type="button" data-action="srt" class="vel-secondary">下载当前字幕 SRT</button><button type="button" data-action="burn" class="vel-primary">导出加工视频</button><button type="button" data-action="cancel" class="vel-secondary" hidden>取消导出</button></div>'+
      '<p class="vel-hint">导出会在当前浏览器实时播放并录制成片，保留原声。请保持此标签页打开。</p><div class="vel-result" data-block="result"></div></div><p role="status" class="vel-message"></p>';
    document.body.append(dlg);
    const q=(s)=>dlg.querySelector(s),f=(name)=>q('[data-field="'+name+'"]'),action=(name)=>q('[data-action="'+name+'"]');
    const video=q('video'),canvas=q('canvas'),ct=canvas.getContext('2d'),msg=q('.vel-message');
    let cues=[],fault='',sourceObject='',resultObject='',tick=0,audio=null,soundSource=null,rec=null,stream=null,cancelled=false,encoding=false;
    let master=null,originalGain=null,voiceBuffer=null,voiceNode=null,voiceLoad=0;
    let chosenSource=0,name='视频',previousFocus=document.activeElement;
    const textFields={};
    function revoke(url){if(url)URL.revokeObjectURL(url);}
    function report(text){msg.textContent=text||'';}
    function download(url,filename) {
      const a=element('a');a.href=url;a.download=filename;dlg.append(a);a.click();a.remove();
    }
    function style() {
      if(video.readyState<2 || !video.videoWidth) return;
      const w=canvas.width,h=canvas.height;
      const z=Number(f('zoom').value),sw=w/z,sh=h/z;
      ct.drawImage(video,(w-sw)/2,(h-sh)/2,sw,sh,0,0,w,h);
      const mark=f('watermark').value.trim();
      if(mark) {
        const where=f('markPosition').value,ratio=.055,size=Math.round(w*.042);
        ct.save();ct.globalAlpha=Number(f('markOpacity').value);ct.font='600 '+size+'px system-ui, sans-serif';
        ct.textAlign=where[1]==='l'?'left':'right';ct.textBaseline=where[0]==='t'?'top':'bottom';
        ct.strokeStyle='rgba(0,0,0,.85)';ct.fillStyle='#fff';ct.lineWidth=Math.max(1,size*.1);ct.lineJoin='round';
        const x=w*(where[1]==='l'?ratio:1-ratio),y=h*(where[0]==='t'?ratio:1-ratio);
        ct.strokeText(mark,x,y,w*.89);ct.fillText(mark,x,y,w*.89);ct.restore();
      }
      const line=activeAt(cues,video.currentTime*1000);
      if(!line) return;
      let fs=w*Number(f('size').value),lines=[];
      function measure() {
        ct.font=(f('bold').checked?'bold ':'')+Math.round(fs)+'px system-ui, sans-serif';
        lines=wrap(ct,line,w*.88);
      }
      measure();
      while(lines.length*fs*1.22>h*.9 && fs>10){fs*=.9;measure();}
      ct.textAlign=f('align').value;ct.textBaseline='middle';ct.lineJoin='round';
      ct.fillStyle=f('color').value;ct.strokeStyle='rgba(0,0,0,.88)';ct.lineWidth=Math.max(1,fs*.11);
      const gap=fs*1.22,block=lines.length*gap;
      const bottom=Math.min(h*.98,Math.max(block+fs*.2,h*Number(f('height').value)));
      const x=ct.textAlign==='left'?w*.06:ct.textAlign==='right'?w*.94:w/2;
      lines.forEach((s,i)=>{const y=bottom-block+i*gap+fs/2;if(f('outline').checked)ct.strokeText(s,x,y);ct.fillText(s,x,y);});
    }
    function redraw(){
      tick=0;if(!dlg.open)return;style();
      f('seek').value=video.currentTime||0;f('position').textContent=time((video.currentTime||0)*1000,'.');
      if(encoding)report('正在导出 '+f('position').textContent+' / '+time(video.duration*1000,'.'));
      if(!video.paused && !video.ended)tick=requestAnimationFrame(redraw);
    }
    function sync(){
      try{
        const a=parse(textFields.source.value,'原文'),b=parse(textFields.target.value,'译文');
        cues=merge(a,b,f('mode').value);fault='';
      }catch(err){cues=[];fault=err.message;}
      if(!encoding)report(fault|| (cues.length?'当前字幕共 '+cues.length+' 段':''));
      style();
    }
    function lock(on){
      encoding=on;
      dlg.querySelectorAll('.vel-body input,.vel-body textarea,.vel-body select,.vel-body button').forEach(x=>x.disabled=on);
      action('cancel').disabled=false;action('cancel').hidden=!on;
    }
    function attach(url,filename){
      name=(filename||'视频').replace(/\.[a-z0-9]{1,5}$/i,'').replace(/[\\/:*?"<>|]/g,'_');
      video.pause();video.crossOrigin='anonymous';video.src=url;video.load();canvas.hidden=true;
    }
    for(const [key,label] of [['source','原文字幕'],['target','译文字幕']]){
      const group=element('div',null,'vel-field'),lab=element('label',label),ta=element('textarea');
      ta.id='ve-sub-'+key+'-'+Math.random().toString(36).slice(2);
      ta.placeholder='1\n00:00:00,000 --> 00:00:02,000\n字幕文字';lab.htmlFor=ta.id;
      const file=element('input');file.type='file';file.accept='.srt,.vtt,text/vtt,text/plain';file.setAttribute('aria-label','导入'+label+' SRT 或 VTT');
      file.addEventListener('change',async()=>{if(!file.files[0])return;try{const text=await file.files[0].text();parse(text,label);ta.value=text;if(key==='target'&&f('mode').value==='source')f('mode').value=textFields.source.value.trim()?'both':'target';sync();}catch(err){report(err.message);}});
      ta.addEventListener('input',sync);group.append(lab,ta,file);textFields[key]=ta;q('[data-block="editors"]').append(group);
    }
    f('file').addEventListener('change',()=>{const file=f('file').files[0];if(!file)return;chosenSource++;revoke(sourceObject);sourceObject=URL.createObjectURL(file);attach(sourceObject,file.name);});
    for(const k of ['mode','size','height','align','color','outline','bold','zoom','watermark','markPosition','markOpacity']) f(k).addEventListener('input',sync);
    f('seek').addEventListener('input',()=>{if(!encoding && Number.isFinite(video.duration))video.currentTime=Number(f('seek').value);});
    video.addEventListener('loadedmetadata',()=>{canvas.width=video.videoWidth;canvas.height=video.videoHeight;canvas.hidden=false;f('seek').max=video.duration||0;redraw();});
    video.addEventListener('loadeddata',redraw);video.addEventListener('seeked',redraw);
    video.addEventListener('play',()=>{action('play').textContent='暂停';if(!tick)redraw();});
    video.addEventListener('pause',()=>action('play').textContent='播放');
    video.addEventListener('error',()=>{if(!dlg.open)return;cancelled=true;if(rec?.state==='recording')rec.stop();report('视频读取失败，请选择已下载的本地成片或其他浏览器支持的视频文件');});
    function setupAudio(){
      if(audio)return;
      const AudioCtor=window.AudioContext||window.webkitAudioContext;
      if(!AudioCtor)throw new Error('当前浏览器无法处理音轨');
      audio=new AudioCtor();soundSource=audio.createMediaElementSource(video);master=audio.createGain();originalGain=audio.createGain();
      soundSource.connect(originalGain);originalGain.connect(master);master.connect(audio.destination);
      originalGain.gain.value=Number(f('originalVolume').value);
    }
    function stopVoice(){if(voiceNode){try{voiceNode.stop();}catch(_){}voiceNode.disconnect();voiceNode=null;}}
    function playVoice(){
      stopVoice();if(!voiceBuffer||video.paused||video.ended||audio.state!=='running')return;
      const when=Math.max(0,Number(f('voiceStart').value)||0),offset=Math.max(0,video.currentTime-when);
      if(offset>=voiceBuffer.duration)return;
      voiceNode=audio.createBufferSource();voiceNode.buffer=voiceBuffer;voiceNode.connect(master);
      voiceNode.start(audio.currentTime+Math.max(0,when-video.currentTime),offset);
    }
    async function useVoice(blob,label){
      const version=++voiceLoad;stopVoice();
      if(!blob){voiceBuffer=null;f('voiceName').textContent='';return;}
      try{
        setupAudio();const buffer=await audio.decodeAudioData(await blob.arrayBuffer());
        if(!dlg.open||version!==voiceLoad)return;
        voiceBuffer=buffer;const initialVolume=options.voice===blob && options.voiceMix===0?'0':'.15';f('originalVolume').value=initialVolume;originalGain.gain.value=Number(initialVolume);
        f('voiceName').textContent=label+' · '+buffer.duration.toFixed(1)+' 秒';playVoice();
      }catch(_){if(version===voiceLoad){voiceBuffer=null;f('voiceName').textContent='';report('音轨解码失败，请使用 WAV、MP3 或 AAC 音频');}}
    }
    f('audioFile').onchange=()=>useVoice(f('audioFile').files[0],f('audioFile').files[0]?.name||'音频');
    f('originalVolume').oninput=()=>{setupAudio();originalGain.gain.value=Number(f('originalVolume').value);};
    f('voiceStart').oninput=playVoice;
    video.addEventListener('playing',playVoice);video.addEventListener('waiting',stopVoice);
    video.addEventListener('pause',stopVoice);video.addEventListener('seeking',stopVoice);
    video.addEventListener('seeked',()=>{if(!video.paused)playVoice();});
    action('play').onclick=async()=>{if(video.readyState<2)return report('请先选择可播放的视频');try{if(video.paused){if(audio)await audio.resume();await video.play();}else video.pause();}catch(err){report(err.message);}};
    action('srt').onclick=()=>{sync();if(fault||!cues.length)return report(fault||'请导入或填写当前显示模式的字幕');const url=URL.createObjectURL(new Blob(['\uFEFF',toSrt(cues)],{type:'application/x-subrip;charset=utf-8'}));download(url,name+'-字幕.srt');setTimeout(()=>revoke(url),30000);};
    function cleanStream(){
      stream?.getTracks().forEach(t=>t.stop());stream=null;
      if(master && audio?.state!=='closed'){master.disconnect();master.connect(audio.destination);}
    }
    function abortExport() {
      cancelled=true;video.pause();
      if(rec && rec.state!=='inactive')rec.stop();
    }
    action('cancel').onclick=abortExport;
    action('burn').onclick=async()=>{
      if(encoding)return;
      sync();if(fault)return report(fault);
      if(!cues.length && !voiceBuffer && !f('watermark').value.trim() && Number(f('zoom').value)===1 && Number(f('originalVolume').value)===1)return report('请添加字幕、配音、水印或选择画面缩放');
      if(voiceBuffer && Number(f('voiceStart').value)>=video.duration)return report('配音开始位置超出视频时长');
      if(video.readyState<2 || !Number.isFinite(video.duration))return report('请先载入可播放的视频');
      if(cues.length && !cues.some(c=>c.start<video.duration*1000))return report('字幕时间全部超出视频时长');
      if(!canvas.captureStream || !window.MediaRecorder || !(window.AudioContext||window.webkitAudioContext))return report('当前浏览器不支持视频录制，请用桌面 Chrome 或 Edge');
      const mime=['video/mp4;codecs=avc1.42001E,mp4a.40.2','video/mp4','video/webm;codecs=vp9,opus','video/webm;codecs=vp8,opus','video/webm'].find(x=>MediaRecorder.isTypeSupported(x));
      if(!mime)return report('当前浏览器没有可用的视频编码器');
      const chunks=[];cancelled=false;lock(true);report('正在启动导出…');
      try{
        video.pause();
        setupAudio();
        await audio.resume();
        if(video.currentTime>.001){
          await new Promise((resolve,reject)=>{
            const timer=setTimeout(()=>{video.removeEventListener('seeked',done);reject(new Error('无法定位到片头，请重新载入视频'));},6000);
            function done(){clearTimeout(timer);resolve();}
            video.addEventListener('seeked',done,{once:true});video.currentTime=0;
          });
        }
        if(cancelled||!dlg.open)throw new Error('导出已取消');
        style();ct.getImageData(0,0,1,1);
        const sink=audio.createMediaStreamDestination();master.disconnect();master.connect(sink);
        stream=canvas.captureStream(30);
        for(const track of sink.stream.getAudioTracks())stream.addTrack(track);
        rec=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:Math.max(2500000,Math.min(12000000,canvas.width*canvas.height*4))});
        let failure='';
        rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
        rec.onerror=()=>{failure='浏览器编码失败，请重试或更换浏览器';abortExport();};
        rec.onstop=()=>{
          cleanStream();rec=null;lock(false);
          if(!dlg.open)return;
          if(cancelled || !chunks.length){report(failure||'导出已取消');return;}
          const type=chunks[0].type||mime,ext=type.includes('mp4')?'mp4':'webm';
          const blob=new Blob(chunks,{type});revoke(resultObject);resultObject=URL.createObjectURL(blob);
          const a=element('a','下载加工视频（'+ext.toUpperCase()+'）');a.href=resultObject;a.download=name+'-加工.'+ext;
          q('[data-block="result"]').replaceChildren(a);report('加工成片已生成');
        };
        rec.start(500);video.playbackRate=1;await video.play();
      }catch(err){
        cancelled=true;if(rec?.state==='recording')rec.stop();else {cleanStream();lock(false);}
        report(err.name==='SecurityError'?'该视频地址不允许浏览器合成，请下载原片后选择本地视频':err.message);
      }
    };
    video.addEventListener('ended',()=>{if(rec?.state==='recording')rec.stop();});
    action('close').onclick=()=>dlg.close();
    dlg.addEventListener('close',()=>{abortExport();cancelAnimationFrame(tick);video.removeAttribute('src');video.load();cleanStream();if(audio)audio.close().catch(()=>{});revoke(sourceObject);revoke(resultObject);dlg.remove();previousFocus?.focus?.();},{once:true});
    dlg.showModal();
    textFields.source.value=options.source||'';textFields.target.value=options.target||'';
    if(textFields.target.value)f('mode').value=textFields.source.value?'both':'target';
    sync();if(options.voice)useVoice(options.voice,'生成的配音');
    if(taskId && typeof taskId==='string') {
      const version=++chosenSource;report('正在载入工作台成片…');
      const media='/api/ai-video-editing/tasks/'+encodeURIComponent(taskId)+'/media';
      if(version===chosenSource){report('');attach(media,taskOrId?.title||taskId);}
    }
  }
  window.VideoEditingSubtitles={open,parse,toSrt};
})();
