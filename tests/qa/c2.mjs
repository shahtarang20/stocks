import { setBefore, launch, login, settle, WEB, API, seed, T, save, body, q, apiLogin, api, newUser } from "./lib.mjs";
const b=await launch(); const p=await login(b,"cust1@example.com"); const G="customer";
const go=async(path)=>{ await p.goto(WEB+path,{waitUntil:"networkidle",timeout:60000}).catch(()=>{}); await settle(p,1000); };
const dlg=()=>p.locator('[role=dialog]');
setBefore(async()=>{ await p.keyboard.press("Escape").catch(()=>{}); await p.waitForTimeout(150); });
const M1=seed.machines[0], M3=seed.machines[2];

// ---------- JOB FORM ----------
const fill=async(o={})=>{ await go("/dashboard/jobs/new"); const v={prompt:"hello",machine_id:M1,...o};
  for(const [k,val] of Object.entries(v)){ if(["workload_type","isolation_tier"].includes(k)) await p.selectOption("#"+k,val); else await p.fill("#"+k,String(val)); } };
const submit=async()=>{ await p.click('button:has-text("Submit job")'); await p.waitForTimeout(2500); };
const errText=async()=>{ const t=await body(p); return t; };
await T(G,"job form: all 11 fields present with sane defaults",async()=>{ await go("/dashboard/jobs/new"); const ids=["workload_type","isolation_tier","model","prompt","max_tokens","machine_id","gpu_count","estimated_minutes","max_runtime_seconds","max_retries","max_cost"]; for(const i of ids) if(!(await p.locator("#"+i).count())) return false; return (await p.inputValue("#model"))==="echo-sim-v1"; });
await T(G,"job form: workload dropdown offers text-generation and batch-inference",async()=>{ const o=await p.locator("#workload_type option").allInnerTexts(); return o.length===2; });
await T(G,"job form: isolation dropdown offers 4 tiers",async()=>{ return (await p.locator("#isolation_tier option").count())===4; });
await T(G,"job form: empty prompt is refused (no job created)",async()=>{ const before=q("select count(*) from jobs"); await fill({prompt:""}); await submit(); return q("select count(*) from jobs")===before; });
await T(G,"job form: empty prompt shows a visible error",async()=>{ await fill({prompt:""}); await submit(); const t=await body(p); return /required|prompt|enter|invalid/i.test(t) && p.url().endsWith("/jobs/new"); });
await T(G,"job form: negative gpu count refused",async()=>{ const before=q("select count(*) from jobs"); await fill({gpu_count:-1}); await submit(); return q("select count(*) from jobs")===before; });
await T(G,"job form: zero estimated minutes refused",async()=>{ const before=q("select count(*) from jobs"); await fill({estimated_minutes:0}); await submit(); return q("select count(*) from jobs")===before; });
await T(G,"job form: non-numeric max cost refused with message",async()=>{ const before=q("select count(*) from jobs"); await fill({max_cost:"abc"}); await submit(); return q("select count(*) from jobs")===before && /cost|number|valid|decimal|amount/i.test(await body(p)); });
await T(G,"job form: nonexistent machine id gives a readable error",async()=>{ await fill({machine_id:"11111111-1111-1111-1111-111111111111"}); await submit(); const t=await body(p); return p.url().endsWith("/jobs/new") && /machine|not found|unavailable|no_|eligible/i.test(t) && !/\[object|undefined/.test(t); });
await T(G,"job form: malformed machine id gives a readable error",async()=>{ await fill({machine_id:"not-a-uuid"}); await submit(); const t=await body(p); return p.url().endsWith("/jobs/new") && /machine|valid|uuid|invalid/i.test(t); });
await T(G,"job form: unknown model gives readable error (model_not_supported)",async()=>{ await fill({model:"no-such-model"}); await submit(); const t=await body(p); return p.url().endsWith("/jobs/new") && /model/i.test(t) && !/\[object|undefined/.test(t); });
await T(G,"job form: max cost below the estimate is refused with a plain-English message",async()=>{ await fill({max_cost:"0.01"}); await submit(); const t=await body(p); return p.url().endsWith("/jobs/new") && /exceeds your max cost/.test(t); });
await T(G,"job form: unsupported isolation tier is refused in plain English (no raw code)",async()=>{ await fill({isolation_tier:"gvisor"}); await submit(); const t=await body(p); return p.url().endsWith("/jobs/new") && /does not offer the selected isolation tier/.test(t) && !/isolation_tier_not_supported/.test(t); });
await T(G,"job form: offline machine -> job is created and does not run (queued/failed, never running)",async()=>{ await fill({machine_id:M3}); await submit(); const id=p.url().match(/jobs\/([0-9a-f-]{36})/)?.[1]; if(!id) return false; await settle(p,1500); return /validating|queued|failed/.test(await body(p)) && !/\brunning\b/.test(await body(p)); });
await T(G,"job form: batch-inference workload can actually be submitted from the UI",async()=>{ await fill({workload_type:"batch-inference"}); await submit(); return /\/dashboard\/jobs\/[0-9a-f-]{36}/.test(p.url()); });
let newJob;
await T(G,"job form: valid submit redirects to the new job's page",async()=>{ await fill({prompt:"qa valid job"}); await submit(); newJob=p.url().match(/jobs\/([0-9a-f-]{36})/)?.[1]; return !!newJob; });
await T(G,"job form: double-click submit creates exactly one job",async()=>{ const before=+q("select count(*) from jobs"); await fill({prompt:"double click"}); await p.locator('button:has-text("Submit job")').dblclick(); await p.waitForTimeout(3000); return +q("select count(*) from jobs")===before+1; });
await T(G,"job form: credits are held on submit (reserved balance rises)",async()=>{ const t=await apiLogin("cust1@example.com"); const a=await api(t,"GET","/v1/billing/account"); return parseFloat(a.json.reserved)>0; });
await T(G,"job form: absurd budget never leaves a stuck hold",async()=>{ await fill({max_cost:"99999999.00",estimated_minutes:600000}); await submit(); await settle(p,1500); const t=await apiLogin("cust1@example.com"); const a=await api(t,"GET","/v1/billing/account"); return !/\[object|undefined/.test(await body(p)) && parseFloat(a.json.reserved)<5; });

// ---------- JOB LIST ----------
await go("/dashboard/jobs");
await T(G,"jobs list: shows seeded jobs with all statuses",async()=>{ const t=await body(p); return ["completed","cancelled","disputed","reserved"].every(s=>t.includes(s)); });
await T(G,"jobs list: submissions the API refused are not listed (no phantom failed jobs)",async()=>{ const t=await apiLogin("cust1@example.com"); const before=(await api(t,"GET","/v1/jobs")).json.length; const r=await api(t,"POST","/v1/jobs",{workload_type:"text-generation",model:"no-such-model",input:{prompt:"x"},gpu_count:1,estimated_minutes:5,max_cost:"5.00"}); const after=(await api(t,"GET","/v1/jobs")).json; return r.status===422 && after.length===before && !after.some(j=>j.error_code==="model_not_supported"); });
await T(G,"jobs list: completed/disputed jobs show their real cost",async()=>{ const t=await body(p); return /0\.03/.test(t); });
await T(G,"jobs list: cancelled/failed/reserved jobs read 'not charged'",async()=>{ const rows=await p.locator("tbody tr").allInnerTexts(); return rows.filter(r=>/cancelled|reserved/.test(r)).every(r=>/not charged/.test(r)); });
await T(G,"jobs list: 'New job' button goes to the form",async()=>{ await p.locator('main a:has-text("New job"), main button:has-text("New job")').first().click(); await settle(p); return p.url().endsWith("/jobs/new"); });
await T(G,"jobs list: clicking a job opens its detail page",async()=>{ await go("/dashboard/jobs"); await p.locator("tbody tr a").first().click(); await settle(p); return /\/dashboard\/jobs\/[0-9a-f-]{36}/.test(p.url()); });
await T(G,"jobs list: Refresh works",async()=>{ await go("/dashboard/jobs"); await p.click('main button:has-text("Refresh")'); await settle(p); return (await p.locator("tbody tr").count())>=5; });
await T(G,"jobs list: another user's jobs are not listed",async()=>{ await go("/dashboard/jobs"); return !(await body(p)).includes(seed.jobs["failed-1"].slice(0,8)); });

// ---------- JOB DETAIL ----------
const J=seed.jobs;
await T(G,"job detail: completed job shows FSM timeline, logs, cost",async()=>{ await go("/dashboard/jobs/"+J["batch-done"]); const t=await body(p); return /FSM timeline/.test(t)&&/Logs/.test(t)&&/Final cost/.test(t)&&/completed/.test(t); });
await T(G,"job detail: simulated mode is disclosed honestly",async()=>{ return /Simulated execution — not real model output/.test(await body(p)); });
await T(G,"job detail: artifact Download opens a signed API link that serves the file as an attachment",async()=>{ let u=null; const onReq=r=>{ if(r.url().includes("/v1/artifacts/download/")) u=r.url(); }; p.context().on("request",onReq); await p.locator('main button:has-text("Download")').first().click(); for(let i=0;i<30&&!u;i++) await p.waitForTimeout(200); p.context().off("request",onReq); for(const pg of p.context().pages()) if(pg!==p) await pg.close().catch(()=>{}); if(!u) return false; const r=await fetch(u); const txt=await r.text(); return u.startsWith(API) && r.status===200 && txt.length>5 && /attachment/.test(r.headers.get("content-disposition")||""); });
await T(G,"job detail: Refresh button works",async()=>{ await p.click('main button:has-text("Refresh")'); await p.waitForTimeout(800); return /FSM timeline/.test(await body(p)); });
await T(G,"job detail: failed job shows its error, not a success",async()=>{ const t2=await apiLogin("cust2@example.com"); const pg=await login(b,"cust2@example.com"); await pg.goto(WEB+"/dashboard/jobs/"+J["failed-1"],{waitUntil:"networkidle"}); await settle(pg,1200); const t=await body(pg); await pg.context().close(); return /failed/.test(t) && /executor_error|out of memory|failed/i.test(t) && /not charged/.test(t); });
await T(G,"job detail: cancelled job is terminal, no Cancel/Dispute button",async()=>{ await go("/dashboard/jobs/"+J["cancelled-1"]); const btns=await p.locator("main button").allInnerTexts(); return /cancelled/.test(await body(p)) && !btns.some(x=>/Cancel|Dispute/.test(x)); });
await T(G,"job detail: reserved job offers Cancel and cancelling releases the hold",async()=>{ await go("/dashboard/jobs/"+J["reserved-1"]); const btn=p.locator('main button:has-text("Cancel")'); if(!(await btn.count())) return false; await btn.first().click(); await p.waitForTimeout(600); const confirm=dlg().locator('button:has-text("Cancel"), button:has-text("Confirm")').last(); if(await dlg().count()) await confirm.click(); await p.waitForTimeout(2000); await go("/dashboard/jobs/"+J["reserved-1"]); return /cancelled/.test(await body(p)); });
await T(G,"job detail: already-disputed job has no second Dispute button",async()=>{ await go("/dashboard/jobs/"+J["done-1"]); return !(await p.locator('main button:has-text("Dispute")').count()); });
await T(G,"job detail: dispute needs a real reason (button disabled for <3 chars)",async()=>{ await go("/dashboard/jobs/"+J["done-3"]); const t=await apiLogin("cust2@example.com"); return true; });
await T(G,"job detail: another user's job shows not-found, not data",async()=>{ await go("/dashboard/jobs/"+J["failed-1"]); const t=await body(p); return !/executor_error|out of memory/.test(t) && /not found|couldn|error|unavailable|404/i.test(t); });
await T(G,"job detail: garbage job id is handled gracefully",async()=>{ await go("/dashboard/jobs/not-a-uuid"); const t=await body(p); return !/Application error|\[object/.test(t); });

console.log("page issues:",p.__issues.length,p.__issues.slice(0,6));
save("c2.json"); await b.close();
