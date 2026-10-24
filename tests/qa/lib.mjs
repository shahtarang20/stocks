import { chromium } from "playwright-core";
import { readFileSync } from "fs";
import { dirname } from "path";
import { fileURLToPath } from "url";
// Output + seed files live in QA_DIR (default: ./out next to these scripts).
export const S=process.env.QA_DIR||dirname(fileURLToPath(import.meta.url))+"/out";
export const PGDSN=process.env.QA_PG||"postgresql://cc@localhost:55432/computecoin";
export const WEB=process.env.QA_WEB||"http://localhost:3000", API=process.env.QA_API||"http://localhost:8010";
export const seed=JSON.parse(readFileSync(S+"/seed.json","utf8"));
export async function launch(){ return chromium.launch({executablePath:process.env.QA_CHROME||"/usr/bin/google-chrome",args:["--no-sandbox"]}); }
export async function settle(p,ms=900){
  await p.waitForLoadState("networkidle",{timeout:60000}).catch(()=>{});
  await p.waitForFunction(()=>!/Checking your session/.test(document.body.innerText),null,{timeout:30000}).catch(()=>{});
  await p.waitForFunction(()=>{ const el=document.querySelector("button"); return !el || Object.keys(el).some(k=>k.startsWith("__reactProps")||k.startsWith("__reactFiber")); },null,{timeout:8000}).catch(()=>{});
  await p.waitForTimeout(ms);
}
export async function login(b,email,pw="Password123!",viewport){
  const ctx=await b.newContext(viewport?{viewport}:{}); const p=await ctx.newPage(); p.setDefaultTimeout(8000);
  const issues=[]; p.__issues=issues;
  p.on("pageerror",e=>issues.push("pageerror: "+String(e).slice(0,160)));
  p.on("console",m=>{ if(m.type()==="error" && !/Failed to load resource.*(401|403|404)/.test(m.text())) issues.push("console: "+m.text().slice(0,160)); });
  p.on("response",r=>{ if(r.status()>=500) issues.push(`HTTP ${r.status()} ${r.request().method()} ${r.url().replace(API,"")}`); });
  await p.goto(WEB+"/login",{waitUntil:"networkidle",timeout:60000}); await hydrated(p);
  await p.fill("#email",email); await p.fill("#password",pw); await p.click("button[type=submit]");
  await p.waitForURL(u=>!u.pathname.startsWith("/login"),{timeout:20000}).catch(()=>{});
  await settle(p);
  return p;
}
// A fixed sleep after page load raced React hydration on slow runs: a form submitted
// before hydration is a native POST and nothing happens. Wait for React to attach.
export async function hydrated(p){
  await p.waitForFunction(()=>{ const el=document.querySelector("button[type=submit], main button, button"); return !!el && Object.keys(el).some(k=>k.startsWith("__reactProps")||k.startsWith("__reactFiber")); },null,{timeout:30000}).catch(()=>{});
  await p.waitForTimeout(300);
}
export const body=async p=>(await p.locator("body").innerText()).replace(/\s+/g," ");

import { writeFileSync, existsSync } from "fs";
import { execSync } from "child_process";
export const results=[];
export let beforeEach=null; export const setBefore=f=>{beforeEach=f};
export async function T(group,name,fn){
  try{ if(beforeEach) await beforeEach(); const r=await fn(); const ok=r===undefined?true:!!r; results.push({group,name,ok,detail:""}); console.log(ok?"[PASS]":"[FAIL]",group,"|",name); }
  catch(e){ results.push({group,name,ok:false,detail:String(e).slice(0,200)}); console.log("[FAIL]",group,"|",name,"--",String(e).slice(0,160).replace(/\n/g," ")); }
}
export function save(file){ writeFileSync(S+"/"+file,JSON.stringify(results,null,1)); const f=results.filter(r=>!r.ok).length; console.log(`\n${results.length-f}/${results.length} passed, ${f} failed`); }
export const q=(sqlText)=>execSync(`psql ${PGDSN} -tAc "${sqlText}"`).toString().trim();
const _tok={};
export async function apiLogin(email,pw="Password123!"){ if(_tok[email]) return _tok[email]; const r=await fetch(API+"/auth/login",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({email,password:pw})}); const t=(await r.json()).access_token; if(t) _tok[email]=t; return t; }
export async function api(token,method,path,bodyObj){ const r=await fetch(API+path,{method,headers:{Authorization:"Bearer "+token,"content-type":"application/json"},body:bodyObj?JSON.stringify(bodyObj):undefined}); let j=null; try{j=await r.json()}catch{}; return {status:r.status,json:j}; }
export async function newUser(b,prefix){ const email=`${prefix}-${Math.random().toString(36).slice(2,7)}@example.com`;
  const r=await fetch(API+"/auth/register",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({email,password:"Password123!"})}); const j=await r.json();
  await fetch(API+"/auth/verify-email",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({token:j.email_verification_token})}); return email; }
