async (page) => {
  // Run with playwright-cli run-code --filename against an isolated static server.
  // All API responses are fixtures; no production requests or writes are made.
  await page.unroute("**/api/**");
  await page.unroute("**/navigation.json");
  page.removeAllListeners("pageerror");
  await page.evaluate(()=>sessionStorage.clear());
  const checks=[],errors=[],calls=[],materialQueries=[];
  const check=(value,name)=>{if(!value)throw new Error(name);checks.push(name);};
  const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
  const base=new URL(page.url()).origin;
  const config={page_id:"101",page_name:"Performance Page",language:"zh",eligible_token_count:1,
    blocked_reason:"",caption_template:"{name} {url}",version:1,config_instance_id:"config-1",
    created_by_name:"Fixture User",updated_at_utc:"2026-10-20T01:00:00Z"};
  let pageGate=deferred(),languageGate=deferred(),navigationGate=deferred();
  let failPages=false,failLanguage=false,authenticated=true,permitted=true,topbarFailure=false;
  let navigationFinished=false,languageFinished=false;
  page.on("pageerror",error=>errors.push(error.message));
  await page.route("**/navigation.json",async route=>{
    calls.push("navigation");await navigationGate.promise;
    navigationFinished=true;return route.abort("failed");
  });
  await page.route("**/api/**",async route=>{
    const path=new URL(route.request().url()).pathname;calls.push(path);
    const respond=(data,status=200)=>route.fulfill({status,contentType:"application/json",
      body:JSON.stringify({ok:status<400,...data})});
    if(path==="/api/ui/topbar")return topbarFailure?respond({message:"登录读取失败"},503):
      respond({authenticated,user:{user_id:"ui-perf",tenant_key:"fixture",name:"Fixture",
        is_admin:false,permissions:{fb_video_publish:permitted}}});
    if(path==="/api/fb-video-publish/languages"){
      await languageGate.promise;languageFinished=true;
      return failLanguage?respond({message:"语言选项暂不可用"},503):respond({items:[{value:"zh",label:"中文"}]});
    }
    if(path==="/api/fb-video-publish/pages"){
      await pageGate.promise;
      return failPages?respond({message:"Page 源暂不可用"},503):respond({items:[config]});
    }
    if(path==="/api/fb-video-publish/pages/101/pool")return respond({
      pool:{config_instance_id:config.config_instance_id,version:0,status:"paused",times:[],processing_mode:"original"},
      items:[],counts:{},next_cursor:""});
    if(path==="/api/fb-video-publish/pages/101/pool/items/resolve"){
      materialQueries.push(route.request().postDataJSON());
      return respond({items:[],material_ids:[],skipped:[{row_number:1,material_id:"201",code:"language_mismatch",message:"素材语言与 Page 不符"}],summary:{total:1,valid:0,skipped:1}});
    }
    return respond({message:"unexpected fixture route"},404);
  });
  await page.goto(base+"/fb-pages.html");
  await page.getByText("正在加载 Page 列表…",{exact:true}).waitFor();
  check(!await page.getByText(/尚未配置 Page，点击/).count(),"loading never flashes empty configuration");
  check(await page.locator("#createPage").isDisabled(),"create disabled until Page and language state are ready");
  check(await page.locator("#pageRows").getAttribute("aria-busy")==="true","list exposes loading status");
  check(calls.includes("/api/fb-video-publish/pages"),"Page request starts while navigation is pending");
  check(calls.includes("/api/fb-video-publish/languages"),"language request runs concurrently with Page request");
  pageGate.resolve();
  await page.getByText("Performance Page",{exact:true}).waitFor({timeout:1500});
  check(!navigationFinished&&!languageFinished,"real Page rows render before navigation and languages complete");
  check(await page.locator("[data-edit-page]").isDisabled(),"editing waits for valid language options");
  check(!await page.locator("[data-pool-page]").isDisabled(),"existing pool remains accessible while language options load");
  check(await page.locator("#pageRows").getAttribute("aria-busy")==="false","list completes independently");
  async function verifyFixedPoolLanguage(label){
    await page.locator("[data-pool-page]").click();
    await page.waitForFunction(()=>!document.getElementById("poolControls").disabled);
    await page.locator("#poolMaterialIds").fill("201");await page.locator("#poolAddMaterials").click();
    await page.getByText("成功添加 0 条，跳过 1 条",{exact:true}).waitFor();
    check(materialQueries.at(-1).config_instance_id===config.config_instance_id&&!Object.hasOwn(materialQueries.at(-1),"language"),label+" ID validation uses the configured Page without an editable language override");
    check(await page.locator("#poolStatus").innerText().then(text=>text.includes("剧语言 zh"))&&await page.locator("#poolAdmissionResult").innerText().then(text=>text.includes("素材语言与 Page 不符")),
      label+" pool shows fixed Page language and server language validation");
    await page.locator("#pagePool [data-close]").first().click();
  }
  await verifyFixedPoolLanguage("pending language dictionary");
  languageGate.resolve();
  await page.getByRole("cell",{name:"中文",exact:true}).waitFor();
  await page.locator("#createPage:enabled").waitFor();
  check(!navigationFinished,"creation becomes ready without waiting for navigation refresh");
  check(!await page.locator("[data-edit-page]").isDisabled(),"editing enabled after language options load");
  check(await page.locator("#pageLanguageNotice").isHidden(),"language loading notice clears");
  navigationGate.resolve();

  failPages=true;
  await page.locator("#reloadPages").click();
  await page.locator("#pageError").filter({hasText:"Page 源暂不可用"}).waitFor();
  check(await page.getByText("Performance Page",{exact:true}).isVisible(),"failed refresh retains visible list");
  check(!await page.locator("#reloadPages").isDisabled(),"failed refresh can be retried");
  failPages=false;
  await page.locator("#reloadPages").click();
  await page.waitForFunction(()=>document.getElementById("pageError").hidden);
  check(await page.getByText("Performance Page",{exact:true}).isVisible(),"list refresh recovers");

  failPages=true;
  await page.reload();
  await page.getByText("Page 列表加载失败，请点击“刷新列表”重试。",{exact:true}).waitFor();
  check(!await page.getByText(/尚未配置 Page，点击/).count(),"initial failure is not represented as an empty configuration");
  check(await page.locator("#createPage").isDisabled(),"initial source failure keeps create disabled");
  failPages=false;
  await page.locator("#reloadPages").click();
  await page.getByText("Performance Page",{exact:true}).waitFor();
  check(!await page.locator("#createPage").isDisabled(),"retry restores configuration actions");

  failLanguage=true;
  await page.reload();
  await page.getByText("Performance Page",{exact:true}).waitFor();
  await page.locator("#retryPageLanguages").waitFor();
  check(await page.locator("#createPage").isDisabled(),"failed language request does not enable an empty language editor");
  check(await page.locator("[data-edit-page]").isDisabled(),"failed language request disables edit");
  check(!await page.locator("[data-pool-page]").isDisabled(),"language failure does not hide the loaded Page list or pool");
  await verifyFixedPoolLanguage("failed language dictionary");
  failLanguage=false;
  await page.locator("#retryPageLanguages").click();
  await page.locator("#createPage:enabled").waitFor();
  check(await page.getByRole("cell",{name:"中文",exact:true}).isVisible(),"language retry updates labels");

  for(const mode of ["logged_out","no_permission","topbar_failure"]){
    authenticated=mode!=="logged_out";permitted=mode!=="no_permission";topbarFailure=mode==="topbar_failure";
    const start=calls.length;
    await page.reload();
    const message=mode==="logged_out"?"请先登录 AI 自动后台":mode==="no_permission"?
      "当前账号没有 Facebook 视频发布权限":"登录读取失败";
    await page.getByText(message,{exact:true}).waitFor();
    check(!calls.slice(start).some(path=>path.startsWith("/api/fb-video-publish/")),mode+" does not start module requests");
    check(await page.locator("#content").isHidden(),mode+" does not expose content");
  }
  check(errors.length===0,"background failures have no unhandled JavaScript errors: "+errors.join("; "));
  return {ok:true,checks:checks.length,details:checks};
}
