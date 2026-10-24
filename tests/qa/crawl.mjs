import { launch, login, settle, WEB, API, seed, S } from "./lib.mjs";
import { writeFileSync } from "fs";
const jobId=seed.jobs["done-1"];
const inv=await fetch(`${API}/auth/login`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({email:"cust1@example.com",password:"Password123!"})}).then(r=>r.json());
const invoices=await fetch(`${API}/v1/invoices`,{headers:{Authorization:"Bearer "+inv.access_token}}).then(r=>r.json());
const invoiceId=(Array.isArray(invoices)?invoices:invoices.items)[0].invoice_id;
const PAGES={
 "cust1@example.com":["/dashboard","/dashboard/projects","/dashboard/api-keys","/dashboard/jobs","/dashboard/jobs/new","/dashboard/jobs/"+jobId,"/dashboard/reservations","/dashboard/billing","/dashboard/invoices","/dashboard/invoices/"+invoiceId,"/dashboard/usage","/dashboard/disputes","/dashboard/settings","/marketplace","/marketplace/gpus"],
 "prov1@example.com":["/provider/onboarding","/provider/dashboard","/provider/hardware","/provider/earnings","/provider/payouts"],
 "admin@example.com":["/admin/overview","/admin/users","/admin/providers","/admin/jobs","/admin/billing","/admin/disputes","/admin/settlement","/admin/security","/admin/telemetry","/admin/vendors","/admin/audit-logs","/admin/configuration"],
 "":["/","/pricing","/developers","/providers","/security","/status","/whitepaper","/about","/legal/terms","/legal/privacy","/login","/register","/forgot-password"],
};
const b=await launch(); const inventory={};
const collect=()=>({
  buttons:[...document.querySelectorAll("button,[role=button],[role=tab],[role=menuitem]")].filter(e=>e.offsetParent!==null).map(e=>({t:(e.innerText||e.getAttribute("aria-label")||e.title||"").trim().replace(/\s+/g," ").slice(0,50),disabled:!!e.disabled,role:e.getAttribute("role")||"button"})).filter(x=>x.t||x.role!=="button"),
  links:[...document.querySelectorAll("main a[href], header a[href], footer a[href], nav a[href], a[href]")].filter(e=>e.offsetParent!==null).map(e=>({t:(e.innerText||e.getAttribute("aria-label")||"").trim().replace(/\s+/g," ").slice(0,40),h:e.getAttribute("href")})),
  fields:[...document.querySelectorAll("input,textarea,select")].filter(e=>e.offsetParent!==null||e.type==="hidden").map(e=>({tag:e.tagName.toLowerCase(),type:e.type||"",id:e.id||e.name||"",ph:e.placeholder||"",req:e.required})),
});
for(const [email,paths] of Object.entries(PAGES)){
  let p;
  if(email){ p=await login(b,email); } else { p=await (await b.newContext()).newPage(); p.__issues=[]; p.on("pageerror",e=>p.__issues.push(String(e))); }
  for(const path of paths){
    const t0=p.__issues.length;
    await p.goto(WEB+path,{waitUntil:"networkidle",timeout:60000}).catch(()=>{}); await settle(p,1200);
    const d=await p.evaluate(collect);
    inventory[`${email||"public"}|${path}`]={...d,landed:new URL(p.url()).pathname,issues:p.__issues.slice(t0)};
    console.log(`${(email||"public").padEnd(20)} ${path.slice(0,46).padEnd(46)} btn=${String(d.buttons.length).padEnd(3)} fld=${String(d.fields.length).padEnd(3)} lnk=${String(d.links.length).padEnd(3)} ${new URL(p.url()).pathname!==path.split("?")[0]?"-> "+new URL(p.url()).pathname:""} ${p.__issues.length>t0?"ISSUES":""}`);
  }
}
writeFileSync(S+"/inventory.json",JSON.stringify(inventory,null,1)); await b.close();
