import { launch, login, settle, WEB, seed, T, save } from "./lib.mjs";
const b=await launch(); const G="mobile";
const jobId=seed.jobs["done-1"];
const SETS={ "":["/","/pricing","/developers","/providers","/security","/status","/whitepaper","/about","/legal/terms","/marketplace","/login","/register","/forgot-password"],
 "cust1@example.com":["/dashboard","/dashboard/projects","/dashboard/api-keys","/dashboard/jobs","/dashboard/jobs/new","/dashboard/jobs/"+jobId,"/dashboard/reservations","/dashboard/billing","/dashboard/wallet","/dashboard/invoices","/dashboard/usage","/dashboard/disputes","/dashboard/settings","/marketplace/gpus"],
 "prov1@example.com":["/provider/onboarding","/provider/dashboard","/provider/hardware","/provider/earnings","/provider/payouts"],
 "admin@example.com":["/admin/overview","/admin/users","/admin/providers","/admin/jobs","/admin/billing","/admin/disputes","/admin/settlement","/admin/security","/admin/telemetry","/admin/vendors","/admin/token-deposits","/admin/audit-logs","/admin/configuration"] };
for(const [email,paths] of Object.entries(SETS)){
  const vp={width:390,height:844}; let p;
  if(email) p=await login(b,email,"Password123!",vp); else { p=await (await b.newContext({viewport:vp})).newPage(); p.setDefaultTimeout(8000); }
  for(const path of paths){
    await T(G,`${email?email.split("@")[0]:"public"} ${path.length>40?path.slice(0,40)+"…":path}: no horizontal page scroll at 390px`,async()=>{
      await p.goto(WEB+path,{waitUntil:"networkidle",timeout:60000}).catch(()=>{}); await settle(p,1200);
      const m=await p.evaluate(()=>({sw:document.documentElement.scrollWidth,iw:window.innerWidth,over:[...document.querySelectorAll("body *")].filter(e=>{const r=e.getBoundingClientRect(); return r.right>window.innerWidth+2 && r.width>0 && getComputedStyle(e).position!=="fixed" && !e.closest("table,[class*=overflow-x],[class*=overflow-auto]")}).slice(0,2).map(e=>e.tagName+"."+(e.className||"").toString().slice(0,40))}));
      if(m.sw>m.iw+1) console.log("   overflow:",path,m.sw,">",m.iw,JSON.stringify(m.over));
      return m.sw<=m.iw+1; });
  }
  await p.context().close();
}
save("mobile.json"); await b.close();
