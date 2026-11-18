/* Translation, narration and speech belong to the independent editor. */
(() => {
  'use strict';
  const root='/api/ai-video-editing/language';
  async function request(path, body) {
    const options={credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}};
    if(body){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
    const response=await fetch(root+path,options);
    const result=await response.json().catch(()=>null);
    if(!response.ok || !result)throw new Error(result?.error||('语言服务暂时不可用（'+response.status+'）'));
    return result.item || result;
  }
  function open(taskOrId, context={}) {
    const taskId=typeof taskOrId==='string'?taskOrId:taskOrId?.id;
    const d=document.createElement('dialog');d.className='vel-dialog';d.setAttribute('aria-label','解说、翻译与配音');
    d.innerHTML='<header><h2>解说、翻译与配音</h2><button type="button" data-action="close" class="vel-secondary">关闭</button></header>'+
      '<div class="vel-body"><div class="vel-grid"><label class="vel-field"><span>源语种</span><select data-field="sourceLanguage"><option value="auto">自动识别</option></select></label>'+
      '<label class="vel-field"><span>目标语种</span><select data-field="language"></select></label></div>'+
      '<label class="vel-field"><span>原文字幕（SRT / VTT）</span><textarea data-field="source" placeholder="导入字幕，也可以直接粘贴带时间码的对白"></textarea></label>'+
      '<div class="vel-actions"><label class="vel-field"><span>导入原文字幕</span><input type="file" data-field="file" accept=".srt,.vtt,text/plain,text/vtt"></label>'+
      '<button type="button" data-action="extract" class="vel-secondary">从当前成片读取字幕</button><button type="button" data-action="translate" class="vel-primary">翻译字幕</button></div>'+
      '<label class="vel-field"><span>译文字幕（可校对）</span><textarea data-field="target"></textarea></label>'+
      '<label class="vel-field"><span>剧情或对白补充</span><textarea data-field="notes" placeholder="填写写解说稿所需的情节、角色或对白。选择成片时会一并读取实际画面。"></textarea></label>'+
      '<div class="vel-grid"><label class="vel-field"><span>目标解说时长（秒）</span><input type="number" min="5" max="180" value="30" data-field="seconds"></label>'+
      '<label class="vel-field"><span>配音语速（词/分钟）</span><input type="number" min="100" max="240" value="165" data-field="speed"></label></div>'+
      '<div class="vel-actions"><button type="button" data-action="narrate" class="vel-primary">撰写解说稿</button></div>'+
      '<label class="vel-field"><span>解说或配音文本（可编辑）</span><textarea data-field="speechText" maxlength="12000"></textarea></label>'+
      '<label class="vel-field"><span>配音内容</span><select data-field="voiceType"><option value="text">解说或配音文本</option><option value="cues">按译文字幕时间逐句配音</option></select></label>'+
      '<div class="vel-actions"><button type="button" data-action="voice" class="vel-secondary">合成配音</button><button type="button" data-action="use" class="vel-primary">带入字幕、音轨与导出</button></div>'+
      '<div class="vel-result" data-field="audio"></div></div><p role="status" class="vel-message"></p>';
    const q=s=>d.querySelector(s),field=k=>q('[data-field="'+k+'"]'),act=k=>q('[data-action="'+k+'"]');
    const message=q('.vel-message'),subs=window.VideoEditingSubtitles;
    let busy=false,speechEnabled=false,voiceBlob=null,voiceKey='',voiceUrl='',readiness=false;
    const say=s=>{if(d.open)message.textContent=s;};
    function clearVoice(){
      voiceBlob=null;voiceKey='';
      if(voiceUrl)URL.revokeObjectURL(voiceUrl);voiceUrl='';
      field('audio').replaceChildren();
    }
    function update(){
      for(const k of ['extract','translate','narrate','voice'])act(k).disabled=busy||!readiness;
      act('extract').disabled=busy||!taskId;
      act('voice').disabled=busy||!readiness||!speechEnabled;
      act('use').disabled=busy;
    }
    function decorate(key,label,fn){
      act(key).onclick=async()=>{
        if(busy)return;busy=true;update();say(label+'…');
        try{await fn();}catch(err){say(err.message||String(err));}
        finally{busy=false;update();}
      };
    }
    act('close').onclick=()=>d.close();
    d.addEventListener('close',()=>{clearVoice();d.remove();},{once:true});
    field('file').onchange=async()=>{
      try{const f=field('file').files[0];if(!f)return;const text=await f.text();subs.parse(text,'原文');field('source').value=text;say('原文字幕已载入');}catch(e){say(e.message);}
    };
    const voiceFingerprint=()=>[field('language').value,field('speed').value,field('voiceType').value,field(field('voiceType').value==='cues'?'target':'speechText').value].join('\n');
    for(const k of ['language','speed','speechText','target','voiceType'])field(k).addEventListener('input',clearVoice);
    decorate('extract','正在读取内嵌字幕、原声或画面文字',async()=>{
      const result=await request('/generate',{mode:'transcribe',task_id:taskId,source_language:field('sourceLanguage').value});
      field('source').value=subs.toSrt(result.cues);say(result.note+' 共 '+result.cues.length+' 段。');
    });
    decorate('translate','正在翻译字幕',async()=>{
      const cues=subs.parse(field('source').value,'原文');
      if(!cues.length)throw new Error('请先读取或导入原文字幕');
      const batches=[];let group=[],chars=0;
      for(const cue of cues){
        if(cue.text.length>12000)throw new Error('单段字幕过长，请拆分后重试');
        if(group.length && (group.length>=120 || chars+cue.text.length>10000)){batches.push(group);group=[];chars=0;}
        group.push(cue);chars+=cue.text.length;
      }
      if(group.length)batches.push(group);
      const translated=[],language=field('language').value;
      for(const [index,part] of batches.entries()){
        if(!d.open)return;
        say('正在翻译 '+(index+1)+' / '+batches.length+' 批');
        const result=await request('/generate',{mode:'translate',language,cues:part});
        translated.push(...result.cues);
      }
      clearVoice();field('voiceType').value='cues';field('target').value=subs.toSrt(translated);
      say('已翻译 '+translated.length+' 段，保留原字幕时间码。');
    });
    decorate('narrate','正在编写解说稿',async()=>{
      const original=field('source').value.trim();
      const dialogue=original?subs.parse(original,'原文').map(c=>c.text).join('\n'):'';
      const text=[field('notes').value,dialogue].filter(Boolean).join('\n');
      if(!taskId && !text.trim())throw new Error('请先选择成片，或填写剧情与对白');
      const data=await request('/generate',{mode:'narrate',language:field('language').value,seconds:Number(field('seconds').value),text,...(taskId?{task_id:taskId}:{})});
      clearVoice();field('voiceType').value='text';field('speechText').value=data.text;say('解说稿已生成，可校对后合成配音');
    });
    decorate('voice','正在合成配音',async()=>{
      const text=field('speechText').value.trim();
      const timed=field('voiceType').value==='cues';
      let payload;
      if(timed) {
        const cues=subs.parse(field('target').value,'译文');
        if(!cues.length)throw new Error('请先翻译或填写译文字幕');
        payload={cues};
      } else {
        if(!text||Array.from(text).length>1600)throw new Error('请输入 1–1600 字配音文本');
        payload={text};
      }
      const key=voiceFingerprint(),result=await request('/speech',{language:field('language').value,...payload,speed:Number(field('speed').value)});
      if(!d.open || key!==voiceFingerprint())return;
      const bytes=Uint8Array.from(atob(result.audio_base64),c=>c.charCodeAt(0));
      clearVoice();voiceKey=key;voiceBlob=new Blob([bytes],{type:result.content_type});voiceUrl=URL.createObjectURL(voiceBlob);
      const audio=document.createElement('audio');audio.controls=true;audio.src=voiceUrl;
      const a=document.createElement('a');a.href=voiceUrl;a.download='解说配音.wav';a.textContent='下载配音 WAV';
      field('audio').append(audio,document.createElement('br'),a);say('配音已生成，时长 '+(result.duration_ms/1000).toFixed(1)+' 秒');
    });
    act('use').onclick=()=>{
      try{
        const a=field('source').value,b=field('target').value;subs.parse(a,'原文');subs.parse(b,'译文');
        const audio=voiceKey && voiceKey===voiceFingerprint()?voiceBlob:null;
        subs.open(taskOrId,{source:a,target:b,voice:audio,voiceMix:field('voiceType').value==='cues'?0:.15});d.close();
      }catch(err){say(err.message);}
    };
    document.body.append(d);d.showModal();update();
    if(context.notes)field('notes').value=String(context.notes).slice(0,12000);
    request('/capabilities').then(data=>{
      if(!d.open)return;
      for(const {code,name} of data.languages){
        field('language').add(new Option(name,code));field('sourceLanguage').add(new Option(name,code));
      }
      field('language').value='en';speechEnabled=!!data.speech;readiness=true;update();
      if(!speechEnabled)say('本机配音引擎尚未就绪，可导入已有配音音轨');
    }).catch(err=>say(err.message));
  }
  window.VideoEditingLanguage={open,subtitles:(value,opts)=>window.VideoEditingSubtitles.open(value,opts)};
})();
