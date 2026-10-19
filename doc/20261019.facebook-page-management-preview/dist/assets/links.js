/* Offline preview of the YouTube eight-field contract mapped to Facebook. */
(() => {
  'use strict';
  const LANDING_BASE = 'https://www.dramawavew2a.com/ads/101/2310/view';
  const tokenPattern = /\{+[^{}\r\n]*\}+/g;
  const bytes = value => new TextEncoder().encode(value).length;
  const encode = value => encodeURIComponent(String(value)).replace(/[!'()]/g, c => '%'+c.charCodeAt(0).toString(16).toUpperCase());
  function validateTemplate(template) {
    if (typeof template !== 'string' || !template.trim()) return '请填写发布文案。';
    if (bytes(template) > 16000) return '文案模板超过 16,000 UTF-8 字节，请缩短文案。';
    for (const token of template.match(tokenPattern) || []) {
      if (!['{name}','{desc}','{url}'].includes(token)) return '仅支持 {name}、{desc}、{url} 宏参数，请检查 '+token;
    }
    if (/[{}]/.test(template.replace(tokenPattern,''))) return '宏参数花括号不完整，请检查文案。';
    return '';
  }
  function buildLink({page,material,taskId,ledgerId,createdMs,subUserId='900001'}) {
    if (!material.contentId || material.local) return null;
    if (!/^\d+$/.test(String(ledgerId)) || Number(ledgerId)<=0 || !/^[1-9]\d*$/.test(subUserId)) throw new Error('示例归因编号无效');
    const timestamp=Math.floor(createdMs/1000);
    const campaign=`yingliang_post_CLV_VL_facebook_${page.id}*${timestamp}none${material.language}*${material.series}*${material.tag||'none'}*${ledgerId}`;
    const params=[['c',campaign],['af_adset',page.name],['af_adset_id',page.id],['af_ad',`${material.name}_contentid[${material.contentId}]`],['af_ad_id',material.id],['af_channel',subUserId],['af_c_id',taskId],['af_dp',material.contentId]];
    return {
      shortUrl:`https://gy.g2flow.com/s2l/fb/demo-${taskId}.html`,
      longUrl:LANDING_BASE+'?'+params.map(([k,v])=>encode(k)+'='+encode(v)).join('&'),
      params,taskId,ledgerId:String(ledgerId),createdMs,landingId:2310,simulated:true
    };
  }
  function render(template, material, link) {
    let error=validateTemplate(template);
    const values={name:material.local?'':material.series,desc:material.local?'':material.description,url:link?.shortUrl};
    const text=String(template||'').replace(tokenPattern,token=>{
      const key=token.slice(1,-1);
      if (!Object.hasOwn(values,key)) return token;
      if (!values[key]) {error=error||'所选素材缺少 '+token+' 对应的剧集信息，请更换素材或移除此宏。';return token;}
      return values[key];
    }).trim();
    if (bytes(text)>5000) error='替换后的发布文案超过 5,000 UTF-8 字节，请缩短文案。';
    return {text,error,bytes:bytes(text)};
  }
  globalThis.FBPreviewLinks=Object.freeze({LANDING_BASE,validateTemplate,buildLink,render,bytes});
})();
