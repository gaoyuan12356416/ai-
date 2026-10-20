async (page) => {
  // Isolated delayed API fixture; never touches production or publishes content.
  await page.unroute("**/api/**");
  await page.unroute("**/navigation.json");
  page.removeAllListeners("pageerror");
  await page.evaluate(()=>sessionStorage.clear());
  const checks=[],errors=[],requests=[],held=[],waiters=[];
  let operationQueries=0;
  const check=(value,name)=>{if(!value)throw new Error(name);checks.push(name);};
  const waitRequests=n=>requests.length>=n?Promise.resolve():new Promise(resolve=>waiters.push({n,resolve}));
  const release=(options={})=>held.shift().resolve(options);
  const cfg={page_id:"101",page_name:"Pool loading fixture",language:"zh",caption_template:"{name}",
    config_instance_id:"pool-fixture",version:1,eligible_token_count:1,created_by_name:"Fixture"};
  const base=new URL(page.url()).origin;
  page.on("pageerror",error=>errors.push(error.message));
  await page.route("**/navigation.json",route=>route.fulfill({contentType:"application/json",body:"[]"}));
  await page.route("**/api/**",async route=>{
    const url=new URL(route.request().url()),path=url.pathname;
    const reply=(data,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify({ok:status<400,...data})});
    if(path==="/api/ui/topbar")return reply({authenticated:true,user:{user_id:"fixture",tenant_key:"fixture",name:"Fixture",permissions:{fb_video_publish:true}}});
    if(path==="/api/fb-video-publish/languages")return reply({items:[{value:"zh",label:"中文"}]});
    if(path==="/api/fb-video-publish/pages")return reply({items:[cfg]});
    if(path.startsWith("/api/fb-video-publish/pages/101/pool/operations/")){
      operationQueries++;return reply({message:"Fixture operation absent"},404);
    }
    if(path==="/api/fb-video-publish/pages/101/pool"&&route.request().method()==="GET"){
      const state=url.searchParams.get("state"),cursor=url.searchParams.get("cursor") || "";
      const response=new Promise(resolve=>held.push({resolve}));
      requests.push({state,cursor});
      waiters.filter(w=>requests.length>=w.n).forEach(w=>w.resolve());
      const {failure=false,version=1}=await response;
      if(failure)return reply({message:"素材池读取暂不可用"},503);
      return reply({pool:{config_instance_id:cfg.config_instance_id,version,status:"paused",times:["09:00"],processing_mode:"original"},
        items:[{id:state+"-"+(cursor || "first"),material_id:"201",material_name:state+" "+(cursor || "first"),language:"zh",status:state,created_at_utc:"2026-10-20T01:00:00Z"}],
        counts:{pending:2,published:1},next_cursor:cursor?"":"next-page"});
    }
    return reply({message:"Unexpected fixture request"},404);
  });
  await page.goto(base+"/fb-pages.html");
  await page.locator('[data-pool-page="101"]').waitFor();
  await page.evaluate(()=>{
    const button=document.querySelector('[data-pool-page="101"]');
    button.click();button.click();
  });
  await waitRequests(1);
  check(requests.length===1&&await page.locator("#pagePool").count()===1,"double open makes one modal and one pool GET");
  check(await page.locator("#poolControls").evaluate(el=>el.disabled),"first load blocks writes until fresh version");
  check(await page.locator("#poolRefresh").isDisabled(),"first load prevents duplicate refresh");
  check(await page.locator("#poolList").innerText()==="正在加载素材池…","first load shows meaningful queue status");
  await page.locator("#poolRefresh").dispatchEvent("click");
  check(requests.length===1,"rapid refresh does not enqueue another query");
  release();
  await page.locator("#poolControls:enabled").waitFor();
  check(await page.locator("#poolList").innerText().then(t=>t.includes("pending first")),"initial queue appears after one response");
  await page.locator('[data-pool-tab="pending"]').click();
  check(requests.length===1,"clicking active state does not reread the pool");

  await page.locator("#poolTimeInput").fill("18:00");await page.locator("#poolAddTime").click();
  await page.locator("#poolProcessing").selectOption("random_overlay");
  await page.locator("#poolRefresh").click();await waitRequests(2);
  check(await page.locator("#poolControls").evaluate(el=>el.disabled),"refresh freezes controls until current snapshot arrives");
  await page.locator("#poolRefresh").dispatchEvent("click");
  await page.locator('[data-pool-tab="published"]').dispatchEvent("click");
  await page.locator("#poolNext").dispatchEvent("click");
  check(requests.length===2,"refresh state and pagination gestures cannot duplicate in-flight reads");
  check(await page.locator("#poolTimes").innerText().then(t=>t.includes("18:00")),"refresh retains edited time while reading");
  release({failure:true});
  await page.getByText("素材池读取暂不可用",{exact:true}).waitFor();
  check(await page.locator("#poolControls").evaluate(el=>el.disabled),"failed refresh cannot write using a stale pool version");
  check(!await page.locator("#poolRefresh").isDisabled(),"failed refresh offers retry");
  check(await page.locator("#poolRefresh").innerText()==="重试读取，保留输入","retry explains retained input");
  check(await page.locator("#poolTimes").innerText().then(t=>t.includes("18:00"))&&await page.locator("#poolProcessing").inputValue()==="random_overlay","failed refresh preserves unsaved time and processing");
  await page.locator("#poolRefresh").click();await waitRequests(3);release({version:2});
  await page.locator("#poolControls:enabled").waitFor();
  check(await page.locator("#poolStatus").innerText().then(t=>t.includes("v2")),"retry reads fresh pool version before unlocking");
  check(await page.locator("#poolTimes").innerText().then(t=>t.includes("18:00"))&&await page.locator("#poolProcessing").inputValue()==="random_overlay","successful retry also preserves unsaved settings");
  check(await page.locator("#poolEnable").isDisabled(),"unsaved settings still require save before enabling");

  await page.locator('[data-pool-tab="published"]').click();await waitRequests(4);
  check(requests.at(-1).state==="published"&&requests.at(-1).cursor==="","state switch requests requested state at first page");
  release({version:3});await page.locator("#poolControls:enabled").waitFor();
  check(await page.locator("#poolList").innerText().then(t=>t.includes("published first")),"selected state response is rendered");
  await page.locator("#poolNext").click();await waitRequests(5);
  check(requests.at(-1).state==="published"&&requests.at(-1).cursor==="next-page","pagination keeps state and server cursor");
  release({version:3});await page.locator("#poolControls:enabled").waitFor();
  check(await page.locator("#poolList").innerText().then(t=>t.includes("published next-page")),"requested next-page response is rendered");
  await page.locator("#poolPrev").click();await waitRequests(6);release({version:3});
  await page.locator("#poolControls:enabled").waitFor();
  check(requests.at(-1).cursor===""&&await page.locator("#poolList").innerText().then(t=>t.includes("published first")),"previous cursor restores first page");

  await page.locator("#pagePool [data-close]").first().click();
  await page.locator('[data-pool-page="101"]').click();await waitRequests(7);release({failure:true});
  await page.getByText("素材池暂时无法加载",{exact:true}).waitFor();
  check(await page.locator("#poolControls").evaluate(el=>el.disabled),"initial read failure leaves writes disabled");
  check(await page.locator("#poolList").innerText().then(t=>t.includes("读取失败")),"initial read failure replaces indefinite loading text");
  await page.locator("#poolRefresh").click();await waitRequests(8);
  await page.locator("#pagePool [data-close]").first().click();
  await page.locator('[data-pool-page="101"]').click();await waitRequests(9);
  const oldResponse=page.waitForResponse(response=>new URL(response.url()).pathname==="/api/fb-video-publish/pages/101/pool");
  release({version:99}); // Old modal's delayed response must not reach the new modal.
  await (await oldResponse).finished();
  check(await page.locator("#poolControls").evaluate(el=>el.disabled),"closed modal response cannot unlock a newer modal");
  release({version:4});await page.locator("#poolControls:enabled").waitFor();
  check(await page.locator("#poolStatus").innerText().then(t=>t.includes("v4")&&!t.includes("v99")),"reopened modal only renders its own response");
  await page.locator("#pagePool [data-close]").first().click();
  const pendingKey="fb-pool-pending:fixture:fixture:101:pool-fixture";
  await page.evaluate(key=>sessionStorage.setItem(key,JSON.stringify({request:{operation_id:"closed-modal-operation"}})),pendingKey);
  await page.locator('[data-pool-page="101"]').click();await waitRequests(10);
  await page.locator("#pagePool [data-close]").first().click();
  await page.evaluate(key=>sessionStorage.removeItem(key),pendingKey);
  const closedPendingResponse=page.waitForResponse(response=>new URL(response.url()).pathname==="/api/fb-video-publish/pages/101/pool");
  release({version:4});await (await closedPendingResponse).finished();
  await page.locator('[data-pool-page="101"]').click();await waitRequests(11);
  check(operationQueries===0,"closed pending modal never starts operation recovery after its read finishes");
  release({version:5});await page.locator("#poolControls:enabled").waitFor();
  check(operationQueries===0&&await page.locator("#poolPendingPanel").isHidden(),"old pending operation cannot affect a newly opened modal");
  check(errors.length===0,"no unhandled errors during delayed failed or closed-modal reads");
  return {ok:true,checks:checks.length,pool_requests:requests.length,details:checks};
}
