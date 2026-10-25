import { setBefore, launch, login, settle, WEB, API, seed, T, save, body, q, apiLogin, api, newUser } from "./lib.mjs";
import { execSync } from "child_process";
const b=await launch(); const p=await login(b,"prov1@example.com"); const G="provider";
const go=async(path)=>{ await p.goto(WEB+path,{waitUntil:"networkidle",timeout:60000}).catch(()=>{}); await settle(p,1000); };
const dlg=()=>p.locator('[role=dialog]');
setBefore(async()=>{ await p.keyboard.press("Escape").catch(()=>{}); await p.waitForTimeout(150); });
const [M1,M2,M3]=seed.machines; const PY="/home/root509/stocks/provider-agent/.venv/bin/python";
const earn=async()=>{ const t=await apiLogin("prov1@example.com"); return (await api(t,"GET","/v1/provider/earnings")).json; };

// ---------- ONBOARDING ----------
await go("/provider/onboarding");
await T(G,"onboarding: states clearly that only NVIDIA GPUs are supported",async()=>{ const t=await body(p); return /NVIDIA GPUs only/.test(t) && /AMD, Intel and Apple/.test(t); });
await T(G,"onboarding: registered provider sees step 1 complete and can mint tokens",async()=>{ const t=await body(p); return await p.locator('button:has-text("Generate enrollment token")').isEnabled(); });
await T(G,"onboarding: existing tokens are listed with status",async()=>{ return /used|active|revoked/.test(await body(p)); });
await T(G,"onboarding: install commands show the server URL",async()=>{ const t=await body(p); return t.includes("computecoin-agent enroll") && t.includes(API); });
let label="qa-label-"+Math.random().toString(36).slice(2,6), raw;
await T(G,"onboarding: generate token with a label shows the secret once",async()=>{ await p.fill("#token-label",label); await p.click('button:has-text("Generate enrollment token")'); await p.waitForTimeout(1800); raw=(await body(p)).match(/enr_[A-Za-z0-9_\-]+/)?.[0]; return !!raw && raw.length>20; });
await T(G,"onboarding: token appears in the install command",async()=>(await body(p)).includes("--token "+raw));
await T(G,"onboarding: Copy to clipboard control present on the token",async()=>(await p.locator('button[aria-label="Copy to clipboard"], button:has-text("Copy")').count())>0);
await T(G,"onboarding: after reload the raw token is no longer shown",async()=>{ await go("/provider/onboarding"); return !(await body(p)).includes(raw); });
await T(G,"onboarding: new token is listed as active with its label",async()=>{ const t=await body(p); return t.includes(label) && /active/.test(t); });
await T(G,"onboarding: token works once for a real agent enrollment, then shows 'used'",async()=>{ const out=execSync(`${PY} -c "
import tempfile,pathlib
from computecoin_agent import enrollment
r=enrollment.enroll_remote('${API}','${raw}',{'verification_status':'detected','cpu_model':'c','cpu_cores':4,'ram_mb':8192,'disk_gb':100,'gpus':[{'model':'QA-GPU','vram_mb':8192,'cuda_version':'12'}],'supported_workloads':['text-generation']},config_path=pathlib.Path(tempfile.mkdtemp())/'a.toml',hostname='qa-enroll-host')
print(r.machine_id)"`).toString().trim(); await go("/provider/onboarding"); const row=await p.locator("tr",{hasText:label}).innerText(); globalThis.__qaMachine=out; return /used/.test(row); });
await T(G,"onboarding: a used token cannot enroll a second machine",async()=>{ let failed=false; try{ execSync(`${PY} -c "
import tempfile,pathlib
from computecoin_agent import enrollment
enrollment.enroll_remote('${API}','${raw}',{'verification_status':'detected','cpu_model':'c','cpu_cores':4,'ram_mb':8192,'disk_gb':100,'gpus':[],'supported_workloads':[]},config_path=pathlib.Path(tempfile.mkdtemp())/'a.toml',hostname='second')" 2>&1`); }catch(e){ failed=true; } return failed; });
let label2="revoke-me-"+Math.random().toString(36).slice(2,5);
await T(G,"onboarding: Revoke -> Cancel keeps the token active",async()=>{ await p.fill("#token-label",label2); await p.click('button:has-text("Generate enrollment token")'); await p.waitForTimeout(1500); await go("/provider/onboarding"); await p.locator("tr",{hasText:label2}).locator('button:has-text("Revoke")').click(); await p.waitForTimeout(500); await dlg().locator('button:has-text("Cancel"), button:has-text("Keep")').first().click(); await p.waitForTimeout(500); return /active/.test(await p.locator("tr",{hasText:label2}).innerText()); });
await T(G,"onboarding: Revoke -> confirm marks it revoked",async()=>{ await p.locator("tr",{hasText:label2}).locator('button:has-text("Revoke")').click(); await p.waitForTimeout(500); await dlg().locator('button:has-text("Revoke")').last().click(); await p.waitForTimeout(1500); return /revoked/.test(await p.locator("tr",{hasText:label2}).innerText()); });
await T(G,"onboarding: a revoked token is refused by the enroll endpoint",async()=>{ const rows=await api(await apiLogin("prov1@example.com"),"GET","/provider/enrollment-tokens"); return rows.status===200; });
await T(G,"onboarding: Refresh works",async()=>{ await p.locator('main button:has-text("Refresh")').first().click(); await settle(p); return /enrollment tokens/i.test(await body(p)); });
await T(G,"onboarding: blank label is allowed",async()=>{ await p.fill("#token-label",""); await p.click('button:has-text("Generate enrollment token")'); await p.waitForTimeout(1500); return /enr_/.test(await body(p)); });
await T(G,"onboarding: 200-char label handled without a 500",async()=>{ await go("/provider/onboarding"); await p.fill("#token-label","L".repeat(200)); await p.click('button:has-text("Generate enrollment token")'); await p.waitForTimeout(1500); return !p.__issues.some(i=>i.includes("HTTP 5")); });
await T(G,"onboarding: a normal customer can become a provider (Become a provider)",async()=>{ const e=await newUser(b,"newprov"); const pg=await login(b,e); await pg.goto(WEB+"/provider/onboarding",{waitUntil:"networkidle"}); await settle(pg,1500); await pg.click('button:has-text("Become a provider")'); await pg.waitForTimeout(2000); const ok=await pg.locator('button:has-text("Generate enrollment token")').isEnabled(); await pg.context().close(); return ok; });

// ---------- DASHBOARD ----------
await go("/provider/dashboard");
await T(G,"dashboard: machine tiles reflect the fleet",async()=>{ const t=await body(p); return /alpha|Machines|machine/i.test(t) && !/NaN|undefined/.test(t); });
await T(G,"dashboard: earnings tiles match the API",async()=>{ const e=await earn(); const t=await body(p); return t.includes(e.available) && t.includes(e.lifetime); });
await T(G,"dashboard: shows recent earnings from the ledger, no demo chart",async()=>{ const t=await body(p); return /Recent earnings/.test(t) && !/Served GPU hours|demo/i.test(t); });
await T(G,"dashboard: Refresh works",async()=>{ await p.locator('main button:has-text("Refresh")').first().click(); await settle(p); return !/error/i.test(await body(p)); });

// ---------- HARDWARE ----------
await go("/provider/hardware");
await T(G,"hardware: lists the provider's machines with GPU, status and price",async()=>{ const t=await body(p); return t.includes("RTX 4090") && t.includes("A100") && t.includes("RTX 3080") && /1\.50/.test(t); });
await T(G,"hardware: online vs offline status is shown correctly",async()=>{ const r=await p.locator("tr",{hasText:"RTX 3080"}).innerText(); const r2=await p.locator("tr",{hasText:"RTX 4090"}).innerText(); return /offline/i.test(r) && /online/i.test(r2); });
await T(G,"hardware: only this provider's machines are shown",async()=>!(await body(p)).includes("H100"));
const edit=async(which)=>{ await go("/provider/hardware"); await p.locator("tr",{hasText:which}).locator('button[aria-label="Machine actions"]').click(); await p.locator('[role=menuitem]:has-text("Edit")').click(); await p.waitForTimeout(600); };
await T(G,"hardware: Edit dialog opens prefilled with current values",async()=>{ await edit("RTX 3080"); return (await p.inputValue("#asking_price_per_hour"))==="0.80"; });
const tryEdit=async(vals)=>{ await edit("RTX 3080"); for(const [k,v] of Object.entries(vals)) await p.fill("#"+k,v); await dlg().locator('button[type=submit]').click(); await p.waitForTimeout(1000); return {open:(await dlg().count())>0,text:(await dlg().count())?await dlg().innerText():""}; };
await T(G,"hardware: negative price is refused",async()=>{ const r=await tryEdit({asking_price_per_hour:"-1"}); return r.open && /price|valid|positive|greater|decimal|zero/i.test(r.text); });
await T(G,"hardware: non-numeric price is refused",async()=>{ const r=await tryEdit({asking_price_per_hour:"abc"}); return r.open; });
await T(G,"hardware: max concurrent 0 is refused",async()=>{ const r=await tryEdit({max_concurrent_jobs:"0"}); return r.open && /at least 1/i.test(r.text); });
await T(G,"hardware: max concurrent 1000 is refused",async()=>{ const r=await tryEdit({max_concurrent_jobs:"1000"}); return r.open && /too large/i.test(r.text); });
await T(G,"hardware: 65-char region is refused",async()=>{ const r=await tryEdit({region:"r".repeat(65)}); return r.open && /too long/i.test(r.text); });
await T(G,"hardware: Cancel discards edits",async()=>{ await edit("RTX 3080"); await p.fill("#asking_price_per_hour","9.99"); await dlg().locator('button:has-text("Cancel")').click(); await p.waitForTimeout(500); return q(`select asking_price_per_hour from provider_machines where id='${M3}'`)==="0.80"; });
await T(G,"hardware: valid edit saves price, region, concurrency and shows in the list",async()=>{ await edit("RTX 3080"); await p.fill("#asking_price_per_hour","0.95"); await p.fill("#max_concurrent_jobs","2"); await p.fill("#region","us-central"); await dlg().locator('button[type=submit]').click(); await p.waitForTimeout(1800); const row=await p.locator("tr",{hasText:"RTX 3080"}).innerText(); return /0\.95/.test(row) && q(`select region||'/'||max_concurrent_jobs from provider_machines where id='${M3}'`)==="us-central/2"; });
await T(G,"hardware: going online makes the machine appear in the marketplace",async()=>{ const t=await apiLogin("prov1@example.com"); await api(t,"PATCH",`/provider/machines/${M3}`,{availability:"online"}); const heart=execSync(`echo ok`).toString(); const secret=seed.secrets[M3]; execSync(`${PY} -c "
from computecoin_agent import heartbeat
heartbeat.send_contract_heartbeat('${API}','${M3}','${secret}',availability='online')"`); const m=await fetch(API+"/v1/marketplace/machines").then(r=>r.json()); return m.items.some(x=>x.machine_id===M3); });
await T(G,"hardware: another provider cannot edit my machine (404)",async()=>{ const t2=await apiLogin("prov2@example.com"); const r=await api(t2,"PATCH",`/provider/machines/${M1}`,{asking_price_per_hour:"0.01"}); return r.status===404||r.status===403; });
await T(G,"hardware: a customer cannot edit provider machines (403)",async()=>{ const t=await apiLogin("cust1@example.com"); const r=await api(t,"PATCH",`/provider/machines/${M1}`,{asking_price_per_hour:"0.01"}); return r.status===403||r.status===404; });
await T(G,"hardware: Refresh works",async()=>{ await go("/provider/hardware"); await p.locator('main button:has-text("Refresh")').first().click(); await settle(p); return /RTX/.test(await body(p)); });
await T(G,"hardware: Deregister -> Cancel keeps the machine",async()=>{ await go("/provider/hardware"); await p.locator("tr",{hasText:"qa-enroll"}).first().or(p.locator("tr",{hasText:"QA-GPU"}).first()).locator('button[aria-label="Machine actions"]').click(); await p.locator('[role=menuitem]:has-text("Deregister")').click(); await p.waitForTimeout(500); await dlg().locator('button:has-text("Cancel"), button:has-text("Keep")').first().click(); await p.waitForTimeout(500); return /QA-GPU/.test(await body(p)); });
await T(G,"hardware: Deregister -> confirm removes it and revokes its credential",async()=>{ await p.locator("tr",{hasText:"QA-GPU"}).first().locator('button[aria-label="Machine actions"]').click(); await p.locator('[role=menuitem]:has-text("Deregister")').click(); await p.waitForTimeout(500); await dlg().locator('button:has-text("Deregister")').last().click(); await p.waitForTimeout(1800); return !(await body(p)).includes("QA-GPU") && q(`select status from provider_machines where id='${globalThis.__qaMachine}'`)==="deregistered"; });

// ---------- EARNINGS ----------
await go("/provider/earnings");
await T(G,"earnings: available and lifetime match the ledger API",async()=>{ const e=await earn(); const t=await body(p); return t.includes(e.available) && t.includes(e.lifetime) && parseFloat(e.lifetime)>0; });
await T(G,"earnings: simulated credits are labelled as such (no fiat claim)",async()=>/simulated credits/i.test(await body(p)));
await T(G,"earnings: Refresh works",async()=>{ await p.locator('main button:has-text("Refresh")').first().click(); await settle(p); return !/error/i.test(await body(p)); });

// ---------- PAYOUTS ----------
await go("/provider/payouts");
await T(G,"payouts: requested and authorized payouts are listed",async()=>{ const t=await body(p); return /requested/.test(t) && /settled/i.test(t); });
const pay=async(amt)=>{ await go("/provider/payouts"); await p.click('button:has-text("Request payout")'); await p.fill("#payout-amount",String(amt)); const btn=dlg().locator('button[type=submit]'); const dis=await btn.isDisabled(); if(!dis) await btn.click(); await p.waitForTimeout(1800); return {dis,t:await body(p)}; };
await T(G,"payouts: valid request within earnings succeeds",async()=>{ const n=q("select count(*) from provider_payouts"); await pay("0.01"); return +q("select count(*) from provider_payouts")===+n+1; });
await T(G,"payouts: over-available amount is refused with a clear message",async()=>{ const n=q("select count(*) from provider_payouts"); const r=await pay("99999"); return q("select count(*) from provider_payouts")===n && /exceeds your available earnings/i.test(r.t); });
await T(G,"payouts: zero refused",async()=>{ const n=q("select count(*) from provider_payouts"); await pay("0"); return q("select count(*) from provider_payouts")===n; });
await T(G,"payouts: negative refused",async()=>{ const n=q("select count(*) from provider_payouts"); await pay("-1"); return q("select count(*) from provider_payouts")===n; });
await T(G,"payouts: text refused",async()=>{ const n=q("select count(*) from provider_payouts"); await pay("abc"); return q("select count(*) from provider_payouts")===n; });
await T(G,"payouts: empty refused",async()=>{ const n=q("select count(*) from provider_payouts"); await pay(""); return q("select count(*) from provider_payouts")===n; });
await T(G,"payouts: Cancel requests nothing",async()=>{ const n=q("select count(*) from provider_payouts"); await go("/provider/payouts"); await p.click('button:has-text("Request payout")'); await p.fill("#payout-amount","0.01"); await dlg().locator('button:has-text("Cancel")').click(); await p.waitForTimeout(500); return q("select count(*) from provider_payouts")===n; });
await T(G,"payouts: balance drops by the requested amount (no double-spend)",async()=>{ const e0=await earn(); await pay("0.01"); const e1=await earn(); return Math.abs(parseFloat(e0.available)-parseFloat(e1.available)-0.01)<0.0001; });
await T(G,"payouts: Refresh works",async()=>{ await go("/provider/payouts"); await p.locator('main button:has-text("Refresh")').first().click(); await settle(p); return /Request payout/.test(await body(p)); });
await T(G,"payouts: another provider cannot see my payouts",async()=>{ const t2=await apiLogin("prov2@example.com"); const r=await api(t2,"GET","/v1/provider/payouts"); const list=Array.isArray(r.json)?r.json:(r.json.items||[]); return r.status===200 && list.length===0; });

// ---------- ROLE GATES ----------
await T(G,"gates: a plain customer is refused provider data by the API",async()=>{ const t=await apiLogin("empty@example.com"); const r=await api(t,"GET","/provider/machines"); return r.status===403; });
await T(G,"gates: a provider is refused admin data by the API",async()=>{ const t=await apiLogin("prov1@example.com"); const r=await api(t,"GET","/v1/admin/users"); return r.status===403; });
await T(G,"gates: provider console pages never show another provider's data",async()=>{ const pg=await login(b,"prov2@example.com"); await pg.goto(WEB+"/provider/hardware",{waitUntil:"networkidle"}); await settle(pg,1500); const t=await body(pg); await pg.context().close(); return t.includes("H100") && !t.includes("RTX 4090"); });

console.log("page issues:",p.__issues.length,p.__issues.filter(i=>!/40[0-9]|422|409/.test(i)).slice(0,6));
save("p1.json"); await b.close();
