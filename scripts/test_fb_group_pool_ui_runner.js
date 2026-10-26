/* Run: node scripts/test_fb_group_pool_ui_runner.js --playwright-path <package> [--browser-path <chrome.exe>] */
"use strict";
const fs=require("node:fs"),path=require("node:path"),http=require("node:http");
const args=process.argv.slice(2),option=name=>args[args.indexOf(name)+1];
const repo=path.resolve(__dirname,".."),root=path.join(repo,"static");
const playwright=args.includes("--playwright-path")?require(path.resolve(option("--playwright-path"))):require("playwright");
const types={".html":"text/html; charset=utf-8",".js":"text/javascript; charset=utf-8",".css":"text/css; charset=utf-8"};
(async()=>{
  const server=http.createServer((req,res)=>{const file=path.resolve(root,"."+new URL(req.url,"http://localhost").pathname);if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}res.setHeader("Content-Type",types[path.extname(file)]||"application/octet-stream");fs.createReadStream(file).pipe(res);});
  await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));let browser;
  try{fs.mkdirSync(path.join(repo,"output","playwright"),{recursive:true});process.chdir(repo);browser=await playwright.chromium.launch({headless:true,...(args.includes("--browser-path")?{executablePath:option("--browser-path")}: {})});const page=await browser.newPage({baseURL:`http://127.0.0.1:${server.address().port}`});page.setDefaultTimeout(15000);const fixture=new Function("return ("+fs.readFileSync(path.join(repo,"scripts","test_fb_group_pool_ui.js"),"utf8")+")")();console.log(JSON.stringify(await fixture(page),null,2));}
  finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
