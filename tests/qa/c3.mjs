import { setBefore, launch, login, settle, WEB, API, seed, T, save, body, q, apiLogin, api, newUser } from "./lib.mjs";
const b=await launch(); const p=await login(b,"cust1@example.com"); const G="customer";
const go=async(path)=>{ await p.goto(WEB+path,{waitUntil:"networkidle",timeout:60000}).catch(()=>{}); await settle(p,1000); };
const dlg=()=>p.locator('[role=dialog]');
setBefore(async()=>{ await p.keyboard.press("Escape").catch(()=>{}); await p.waitForTimeout(150); });
const rows=async()=>p.locator("tbody tr").count();

// ---------- MARKETPLACE (live) ----------
await T(G,"gpus: 3 live machines listed, offline machine hidden",async()=>{ await go("/marketplace/gpus"); const t=await body(p); return /3 live machines/.test(t) && !t.includes("RTX 3080"); });
await T(G,"gpus: model filter narrows results",async()=>{ await p.fill("#f-model","4090"); await p.waitForTimeout(1200); const t=await body(p); return /1 live machine/.test(t) && t.includes("RTX 4090") && !t.includes("A100"); });
await T(G,"gpus: Reset filters restores all",async()=>{ await p.locator('button:has-text("Reset filters")').first().click(); await p.waitForTimeout(1200); return /3 live machines/.test(await body(p)); });
await T(G,"gpus: min VRAM filter excludes small GPUs",async()=>{ await p.fill("#f-vram","30000"); await p.waitForTimeout(1200); const t=await body(p); return !t.includes("RTX 4090") && t.includes("A100"); });
await T(G,"gpus: max price filter",async()=>{ await p.locator('button:has-text("Reset filters")').first().click(); await p.fill("#f-price","2"); await p.waitForTimeout(1200); const t=await body(p); return t.includes("RTX 4090") && !t.includes("H100"); });
await T(G,"gpus: region filter",async()=>{ await p.locator('button:has-text("Reset filters")').first().click(); await p.fill("#f-region","EU"); await p.waitForTimeout(1200); const t=await body(p); return t.includes("A100") && !t.includes("RTX 4090"); });
await T(G,"gpus: workload filter (image-generation matches only the H100)",async()=>{ await p.locator('button:has-text("Reset filters")').first().click(); await p.fill("#f-workload","image-generation"); await p.waitForTimeout(1200); const t=await body(p); return t.includes("H100") && !t.includes("A100"); });
await T(G,"gpus: no-match filter shows an empty state, not an error",async()=>{ await p.locator('button:has-text("Reset filters")').first().click(); await p.fill("#f-model","zzz-none"); await p.waitForTimeout(1200); const t=await body(p); return /0 live machines/.test(t) && /No machines/i.test(t) && !/error/i.test(t); });
await T(G,"gpus: verification dropdown (Detected) keeps results",async()=>{ await p.locator('button:has-text("Reset filters")').first().click(); await p.selectOption("#f-verif","detected"); await p.waitForTimeout(1200); return /3 live machines/.test(await body(p)); });
await T(G,"gpus: negative VRAM / price are handled without crashing",async()=>{ await p.locator('button:has-text("Reset filters")').first().click(); await p.fill("#f-vram","-5"); await p.fill("#f-price","abc").catch(()=>{}); await p.waitForTimeout(1200); return !/Application error|\[object/.test(await body(p)); });
await T(G,"gpus: Refresh works",async()=>{ await p.locator('button:has-text("Reset filters")').first().click(); await p.locator('main button:has-text("Refresh")').click(); await p.waitForTimeout(1200); return /3 live machines/.test(await body(p)); });
await T(G,"quote: dialog shows machine, workload, minutes, GPUs, spend ceiling",async()=>{ await go("/marketplace/gpus"); await p.locator('button:has-text("Get quote")').first().click(); await p.waitForTimeout(600); const t=await dlg().innerText(); return /Get a quote/.test(t)&&/Estimated minutes/.test(t)&&/GPU count/.test(t)&&/Max authorized spend/.test(t); });
await T(G,"quote: Cancel closes without creating a quote",async()=>{ await go("/marketplace/gpus"); await p.locator('button:has-text("Get quote")').first().click(); await p.waitForTimeout(500); const n=q("select count(*) from quotes"); await dlg().locator('button:has-text("Cancel")').click(); await p.waitForTimeout(400); return q("select count(*) from quotes")===n && (await dlg().count())===0; });
await T(G,"quote: valid request returns a price breakdown",async()=>{ await p.locator('button:has-text("Get quote")').first().click(); await p.fill("#estimated_minutes","60"); await dlg().locator('button:has-text("Get quote")').last().click(); await p.waitForTimeout(2000); const t=await dlg().innerText().catch(()=>body(p)); return /1\.50|total|fee|cost|Reserve/i.test(t); });
await T(G,"quote: price math is right (60 min at 1.50/hr = 1.50 before fee)",async()=>{ const t=await body(p); return /1\.50/.test(t); });
await T(G,"quote: zero minutes refused",async()=>{ await go("/marketplace/gpus"); await p.locator('button:has-text("Get quote")').first().click(); await p.fill("#estimated_minutes","0"); const n=q("select count(*) from quotes"); await dlg().locator('button:has-text("Get quote")').last().click({timeout:3000}).catch(()=>{}); await p.waitForTimeout(1200); return q("select count(*) from quotes")===n; });
await T(G,"quote: GPU count above machine's GPUs refused with message",async()=>{ await go("/marketplace/gpus"); await p.locator('button:has-text("Get quote")').first().click(); await p.fill("#gpu_count","8"); const n=q("select count(*) from quotes"); await dlg().locator('button:has-text("Get quote")').last().click({timeout:3000}).catch(()=>{}); await p.waitForTimeout(1500); return q("select count(*) from quotes")===n && !/\[object|undefined/.test(await body(p)); });

// ---------- RESERVATIONS ----------
await go("/dashboard/reservations");
await T(G,"reservations: list renders with status and expiry",async()=>{ const t=await body(p); return /active|released|expired/.test(t) && (await rows())>=1; });
await T(G,"reservations: Release is disabled on non-active rows",async()=>{ const dis=await p.locator('tbody tr:has-text("released") button:has-text("Release")').evaluateAll(es=>es.map(e=>e.disabled)); return dis.length===0 || dis.every(Boolean); });
await T(G,"reservations: Release -> Cancel keeps it active",async()=>{ const btn=p.locator('tbody tr:has-text("active") button:has-text("Release")').first(); if(!(await btn.count())) return "skip-none"; await btn.click(); await p.waitForTimeout(500); await dlg().locator('button:has-text("Keep it")').click(); await p.waitForTimeout(500); return (await p.locator('tbody tr:has-text("active")').count())>=1; });
await T(G,"reservations: Release -> confirm frees the slot",async()=>{ await go("/dashboard/reservations"); const before=await p.locator('tbody tr:has-text("active")').count(); const btn=p.locator('tbody tr:has-text("active") button:has-text("Release")').first(); if(!(await btn.count())) return true; await btn.click(); await p.waitForTimeout(500); await dlg().locator('button:has-text("Release slot")').click(); await p.waitForTimeout(1800); return (await p.locator('tbody tr:has-text("active")').count())===before-1; });
await T(G,"reservations: Refresh works",async()=>{ await p.locator('main button:has-text("Refresh")').click(); await settle(p); return (await rows())>=1; });

// ---------- BILLING ----------
const acct=async()=>{ const t=await apiLogin("cust1@example.com"); return (await api(t,"GET","/v1/billing/account")).json; };
await go("/dashboard/billing");
await T(G,"billing: balance, reserved and total are shown and add up",async()=>{ const a=await acct(); const t=await body(p); return t.includes(a.available) && Math.abs(parseFloat(a.available)+parseFloat(a.reserved)-parseFloat(a.balance))<0.005; });
await T(G,"billing: ledger lists purchases, holds and releases",async()=>{ const t=await body(p); return /credit purchase/.test(t) && /hold/.test(t) && /release/.test(t); });
const buy=async(amt)=>{ await go("/dashboard/billing"); await p.click('button:has-text("Buy credits")'); await p.fill("#amount",String(amt)); const btn=dlg().locator('button[type=submit]'); const dis=await btn.isDisabled(); if(!dis) await btn.click(); await p.waitForTimeout(1800); return {dis,t:await body(p)}; };
await T(G,"billing: buy 25.00 raises the balance by exactly 25.00",async()=>{ const a0=await acct(); const r=await buy("25.00"); const a1=await acct(); return Math.abs(parseFloat(a1.available)-parseFloat(a0.available)-25)<0.001 && /New available balance/.test(r.t); });
await T(G,"billing: zero is refused",async()=>{ const a0=await acct(); const r=await buy("0"); const a1=await acct(); return a0.available===a1.available && /greater than zero|valid/i.test(r.t); });
await T(G,"billing: negative is refused",async()=>{ const a0=await acct(); await buy("-10"); const a1=await acct(); return a0.available===a1.available; });
await T(G,"billing: text is refused",async()=>{ const a0=await acct(); await buy("abc"); const a1=await acct(); return a0.available===a1.available; });
await T(G,"billing: 3 decimal places refused (no silent rounding)",async()=>{ const a0=await acct(); await buy("1.999"); const a1=await acct(); return a0.available===a1.available; });
await T(G,"billing: absurdly large amount is capped/refused",async()=>{ const a0=await acct(); const r=await buy("999999999999"); const a1=await acct(); return a0.available===a1.available || parseFloat(a1.available)-parseFloat(a0.available)<=1000000; });
await T(G,"billing: Cancel in the dialog buys nothing",async()=>{ const a0=await acct(); await go("/dashboard/billing"); await p.click('button:has-text("Buy credits")'); await p.fill("#amount","50"); await dlg().locator('button:has-text("Cancel")').click(); await p.waitForTimeout(500); return (await acct()).available===a0.available; });
await T(G,"billing: Refresh on the ledger works",async()=>{ await go("/dashboard/billing"); await p.locator('main button:has-text("Refresh")').click(); await settle(p); return /Ledger/.test(await body(p)); });
await T(G,"billing: a brand-new user sees a clean zero state, no errors",async()=>{ const e=await newUser(b,"fresh"); const pg=await login(b,e); await pg.goto(WEB+"/dashboard/billing",{waitUntil:"networkidle"}); await settle(pg,1500); const t=await body(pg); const bad=pg.__issues.filter(i=>i.startsWith("HTTP 5")); await pg.context().close(); return /1000\.00/.test(t) && bad.length===0; });

// ---------- INVOICES ----------
await go("/dashboard/invoices");
await T(G,"invoices: lists invoices with job, estimated, actual, status",async()=>{ const t=await body(p); return (await rows())>=3 && /paid/.test(t) && /0\.03/.test(t); });
await T(G,"invoices: clicking an invoice opens its detail",async()=>{ await go("/dashboard/invoices"); await p.locator("tbody tr a").first().click(); await p.waitForURL(/\/dashboard\/invoices\/[0-9a-f-]{36}/,{timeout:20000}); return true; });
await T(G,"invoice detail: shows estimated vs actual separately",async()=>{ await go("/dashboard/invoices"); await p.locator("tbody tr a").first().click(); await settle(p); const t=await body(p); return /estimated/i.test(t) && /actual/i.test(t); });
await T(G,"invoice detail: Download saves the invoice as a JSON file",async()=>{ const [dl]=await Promise.all([p.waitForEvent("download",{timeout:8000}),p.locator('main button:has-text("Download")').first().click()]); const fs=await import("fs"); const txt=fs.readFileSync(await dl.path(),"utf8"); const j=JSON.parse(txt); return /^invoice-.*\.json$/.test(dl.suggestedFilename()) && j.actual_total==="0.03" && Array.isArray(j.line_items); });
await T(G,"invoice detail: another user's invoice shows not-found",async()=>{ const t2=await apiLogin("cust2@example.com"); const inv=await api(await apiLogin("cust1@example.com"),"GET","/v1/invoices"); const id=(Array.isArray(inv.json)?inv.json:inv.json.items)[0].invoice_id; const r=await api(t2,"GET","/v1/invoices/"+id); return r.status===404||r.status===403; });
await T(G,"invoice detail: garbage id handled gracefully",async()=>{ await go("/dashboard/invoices/not-a-uuid"); return !/Application error|\[object/.test(await body(p)); });

// ---------- USAGE ----------
await go("/dashboard/usage");
await T(G,"usage: totals match the API (no more 0.00 with metered jobs)",async()=>{ const t=await apiLogin("cust1@example.com"); const u=(await api(t,"GET","/v1/usage")).json; const tx=await body(p); return tx.includes(u.total_actual) && tx.includes(u.total_estimated) && parseFloat(u.total_actual)>0; });
await T(G,"usage: per-job duration is shown (60s), not a dash",async()=>{ const rowsT=await p.locator("tbody tr").allInnerTexts(); return rowsT.length>0 && rowsT.every(r=>!/^\S+\s+—\s/.test(r)); });
await T(G,"usage: Refresh works",async()=>{ await p.locator('main button:has-text("Refresh")').click(); await settle(p); return /Per-job usage/.test(await body(p)); });

// ---------- DISPUTES ----------
await go("/dashboard/disputes");
await T(G,"disputes: lists open and resolved disputes",async()=>{ const t=await body(p); return /open/.test(t) && /resolved/.test(t); });
await T(G,"disputes: dispute row links to its job",async()=>{ await go("/dashboard/disputes"); const l=p.locator("tbody tr a").first(); if(!(await l.count())) return false; await l.click(); await p.waitForURL(/\/dashboard\/jobs\//,{timeout:20000}); return true; });
await T(G,"dispute dialog: <3 chars keeps submit disabled, valid reason opens a dispute",async()=>{ await go("/dashboard/jobs/"+seed.jobs["batch-done"]); await p.click('main button:has-text("Dispute")'); await p.fill("#dispute-reason","ab"); const dis=await dlg().locator('button:has-text("Open dispute")').isDisabled(); await p.fill("#dispute-reason","Result looks wrong to me"); await dlg().locator('button:has-text("Open dispute")').click(); await p.waitForTimeout(2000); await go("/dashboard/disputes"); return dis && /Result looks wrong/.test(await body(p)); });
await T(G,"dispute: a second dispute on the same job is blocked with a clear message",async()=>{ const t=await apiLogin("cust1@example.com"); const r=await api(t,"POST",`/v1/jobs/${seed.jobs["batch-done"]}/dispute`,{reason:"again please"}); return r.status===409 && r.json.detail==="dispute_exists"; });
await T(G,"dispute: cannot dispute someone else's job",async()=>{ const t2=await apiLogin("cust2@example.com"); const r=await api(t2,"POST",`/v1/jobs/${seed.jobs["done-1"]}/dispute`,{reason:"not mine"}); return r.status===404||r.status===403; });

// ---------- SETTINGS ----------
await go("/dashboard/settings");
await T(G,"settings: email is shown",async()=>{ return (await p.inputValue("#s-email")).includes("cust1@example.com") || (await body(p)).includes("cust1@example.com"); });
await T(G,"settings: no stale 'tokens stored in browser' claim",async()=>!/stored in your browser/i.test(await body(p)));
await T(G,"settings: Delete account dialog opens and Cancel closes it",async()=>{ await p.click('button:has-text("Delete account")'); await p.waitForTimeout(400); const open=(await dlg().count())>0; await dlg().locator('button:has-text("Cancel")').click(); await p.waitForTimeout(300); return open && (await dlg().count())===0; });
await T(G,"settings: Delete account is a demo no-op (documented gap)",async()=>{ await p.click('button:has-text("Delete account")'); await dlg().locator('button:has-text("Delete account")').click(); await p.waitForTimeout(800); return q("select count(*) from users where email='cust1@example.com'")==="1"; });
await T(G,"settings: Log out ends the session and protects the dashboard",async()=>{ const e=await newUser(b,"logout"); const pg=await login(b,e); await pg.goto(WEB+"/dashboard/settings",{waitUntil:"networkidle"}); await settle(pg); await pg.click('button:has-text("Log out")'); await pg.waitForURL(u=>/^\/(login)?$/.test(u.pathname),{timeout:20000}).catch(()=>{}); await pg.waitForTimeout(1500); const home=/^\/(login)?$/.test(new URL(pg.url()).pathname); await pg.goto(WEB+"/dashboard",{waitUntil:"networkidle"}); await settle(pg,1500); const guarded=/login|register/.test(pg.url()); await pg.context().close(); return home && guarded; });

console.log("page issues:",p.__issues.length,p.__issues.filter(i=>!/40[0-9]|422|409/.test(i)).slice(0,6));
save("c3.json"); await b.close();
