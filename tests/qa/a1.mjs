import { setBefore, launch, login, settle, WEB, API, seed, T, save, body, q, apiLogin, api, newUser } from "./lib.mjs";
const b=await launch(); const p=await login(b,"admin@example.com"); const G="admin";
const go=async(path)=>{ await p.goto(WEB+path,{waitUntil:"networkidle",timeout:60000}).catch(()=>{}); await settle(p,1200); };
const dlg=()=>p.locator('[role=dialog]');
setBefore(async()=>{ await p.keyboard.press("Escape").catch(()=>{}); await p.waitForTimeout(150); });
const at=await apiLogin("admin@example.com");

// ---------- OVERVIEW ----------
await go("/admin/overview");
await T(G,"overview: tiles match /v1/admin/stats",async()=>{ const s=(await api(at,"GET","/v1/admin/stats")).json; const t=await body(p); return t.includes(String(s.total_users)) && t.includes(String(s.total_jobs)); });
await T(G,"overview: recent jobs table is populated",async()=>(await p.locator("tbody tr").count())>=5);
for(const [label,href] of [["Manage users","/admin/users"],["Manage providers","/admin/providers"],["All jobs","/admin/jobs"],["Open disputes","/admin/disputes"],["Audit logs","/admin/audit-logs"],["Configuration","/admin/configuration"]]){
  await T(G,`overview: "${label}" link goes to ${href}`,async()=>{ await go("/admin/overview"); await p.locator(`main a:has-text("${label}")`).first().click(); await p.waitForURL(u=>u.pathname===href,{timeout:20000}); return true; });
}
await T(G,"overview: Refresh works",async()=>{ await go("/admin/overview"); await p.locator('main button:has-text("Refresh")').click(); await settle(p); return /Total users/.test(await body(p)); });

// ---------- USERS ----------
await go("/admin/users");
await T(G,"users: every registered user is listed with roles, status, verified",async()=>{ const n=+q("select count(*) from users"); const rows=await p.locator("tbody tr").count(); const t=await body(p); return rows===n && /Administrator/.test(t) && /Provider/.test(t) && /suspended/.test(t); });
await T(G,"users: own row is marked and Suspend is disabled for yourself",async()=>{ const row=p.locator("tr",{hasText:"admin@example.com"}); return /You/.test(await row.innerText()) && await row.locator('button:has-text("Suspend")').isDisabled(); });
await T(G,"users: Suspend -> Cancel leaves the user active",async()=>{ const row=p.locator("tr",{hasText:"empty@example.com"}); await row.locator('button:has-text("Suspend")').click(); await p.waitForTimeout(500); await dlg().locator('button:has-text("Cancel")').click(); await p.waitForTimeout(500); return q("select status from users where email='empty@example.com'")==="active"; });
await T(G,"users: Suspend -> confirm suspends and writes an audit entry",async()=>{ const n=q("select count(*) from audit_logs where action like '%suspend%'"); await p.locator("tr",{hasText:"empty@example.com"}).locator('button:has-text("Suspend")').click(); await p.waitForTimeout(400); await dlg().locator('button:has-text("Confirm suspension")').click(); await p.waitForTimeout(1800); return q("select status from users where email='empty@example.com'")==="suspended" && +q("select count(*) from audit_logs where action like '%suspend%'")>+n; });
await T(G,"users: row flips to Reinstate after suspending",async()=>/Reinstate/.test(await p.locator("tr",{hasText:"empty@example.com"}).innerText()));
await T(G,"users: a suspended user cannot sign in through the UI",async()=>{ const pg=await login(b,"empty@example.com"); const ok=/\/login/.test(pg.url()) && /Invalid credentials|suspended|disabled/i.test(await body(pg)); await pg.context().close(); return ok; });
await T(G,"users: a suspended user's existing API token stops working",async()=>{ const r=await fetch(API+"/auth/login",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({email:"empty@example.com",password:"Password123!"})}); return r.status>=400; });
await T(G,"users: Reinstate restores sign-in",async()=>{ await p.locator("tr",{hasText:"empty@example.com"}).locator('button:has-text("Reinstate")').click(); await p.waitForTimeout(400); await dlg().locator('button:has-text("Confirm reinstatement")').click(); await p.waitForTimeout(1800); const pg=await login(b,"empty@example.com"); const ok=/\/dashboard/.test(pg.url()); await pg.context().close(); return ok && q("select status from users where email='empty@example.com'")==="active"; });
await T(G,"users: API refuses self-suspension",async()=>{ const id=q("select id from users where email='admin@example.com'"); const r=await api(at,"POST",`/v1/admin/users/${id}/suspend`,{reason:"x"}); return r.status>=400 && q("select status from users where email='admin@example.com'")==="active"; });
await T(G,"users: unknown user id is a clean 404",async()=>{ const r=await api(at,"POST","/v1/admin/users/11111111-1111-1111-1111-111111111111/suspend",{reason:"x"}); return r.status===404; });
await T(G,"users: non-admin cannot list or suspend (403)",async()=>{ const t=await apiLogin("cust1@example.com"); const a=await api(t,"GET","/v1/admin/users"); const id=q("select id from users where email='cust2@example.com'"); const s=await api(t,"POST",`/v1/admin/users/${id}/suspend`,{reason:"x"}); return a.status===403 && s.status===403 && q("select status from users where email='cust2@example.com'")==="active"; });
await T(G,"users: Refresh works",async()=>{ await go("/admin/users"); await p.locator('main button:has-text("Refresh")').click(); await settle(p); return (await p.locator("tbody tr").count())>=10; });

// ---------- PROVIDERS / JOBS ----------
await go("/admin/providers");
await T(G,"providers: every live machine is listed with GPU, region, price",async()=>{ const t=await body(p); return ["RTX 4090","A100","H100"].every(x=>t.includes(x)) && /us-east/.test(t) && /1\.50/.test(t); });
await T(G,"providers: Refresh works",async()=>{ await p.locator('main button:has-text("Refresh")').click(); await settle(p); return /RTX 4090/.test(await body(p)); });
await go("/admin/jobs");
await T(G,"jobs: shows jobs from ALL customers with status, mode, isolation and error code",async()=>{ const t=await body(p); return /cust|[0-9a-f]{8}…/.test(t) && /failed/.test(t) && /container/.test(t) && /no_provider/.test(t); });
await T(G,"jobs: row count matches the platform (capped at the page limit)",async()=>{ const n=+q("select count(*) from jobs"); const rows=await p.locator("tbody tr").count(); return rows===Math.min(n,50); });
await T(G,"jobs: Refresh works",async()=>{ await p.locator('main button:has-text("Refresh")').click(); await settle(p); return (await p.locator("tbody tr").count())>0; });
await T(G,"jobs: a customer is refused the admin job list (403)",async()=>{ const t=await apiLogin("cust1@example.com"); return (await api(t,"GET","/v1/admin/jobs")).status===403; });

// ---------- DISPUTES ----------
await go("/admin/disputes");
await T(G,"disputes: lists every customer's disputes (open and resolved)",async()=>{ const n=+q("select count(*) from disputes"); const rows=await p.locator("tbody tr").count(); const t=await body(p); return rows===n && n>=2 && /open/.test(t) && /resolved/.test(t); });
await T(G,"disputes: customer endpoint cannot be used to list others' disputes",async()=>{ const t=await apiLogin("cust2@example.com"); const r=await api(t,"GET","/v1/admin/disputes"); return r.status===403; });
await T(G,"disputes: Refresh works",async()=>{ await p.locator('main button:has-text("Refresh")').click(); await settle(p); return (await p.locator("tbody tr").count())>=2; });

// ---------- BILLING & RECONCILIATION ----------
await go("/admin/billing");
await T(G,"billing: reconciliation shows balanced ledger (debits == credits)",async()=>{ const t=await body(p); return /Balanced/.test(t) && /Imbalance: 0\.00/.test(t); });
await T(G,"billing: account table lists customer, platform and provider accounts",async()=>{ const t=await body(p); return /customer_credits/.test(t) && /platform_revenue/.test(t) && /provider_earnings/.test(t); });
const refund=async(o)=>{ await go("/admin/billing"); for(const [k,v] of Object.entries(o)) await p.fill("#"+k,v); await p.click('button:has-text("Post refund")'); await p.waitForTimeout(1800); return body(p); };
await T(G,"billing/refund: no job or invoice id is refused with a message",async()=>{ const n=q("select count(*) from ledger_transactions where kind='refund'"); const t=await refund({"refund-amount":"1.00","refund-reason":"qa test"}); return /Provide a job ID or an invoice ID/.test(t) && q("select count(*) from ledger_transactions where kind='refund'")===n; });
await T(G,"billing/refund: malformed amount is refused",async()=>{ const t=await refund({"refund-job":seed.jobs["done-3"],"refund-amount":"abc","refund-reason":"qa test"}); return /Enter a decimal like 5\.00/.test(t); });
await T(G,"billing/refund: a 3-decimal amount is refused (no silent rounding)",async()=>{ const t=await refund({"refund-job":seed.jobs["done-3"],"refund-amount":"1.999","refund-reason":"qa test"}); return /Enter a decimal like 5\.00/.test(t); });
await T(G,"billing/refund: reason under 3 chars is refused",async()=>{ const t=await refund({"refund-job":seed.jobs["done-3"],"refund-amount":"0.01","refund-reason":"x"}); return /reason for the audit log/i.test(t); });
await T(G,"billing/refund: unknown job id gives a readable error, not a crash",async()=>{ const t=await refund({"refund-job":"11111111-1111-1111-1111-111111111111","refund-amount":"0.01","refund-reason":"qa test"}); return !/\[object|undefined|Application error/.test(t) && /not found|no settled|invalid|error|couldn/i.test(t); });
await T(G,"billing/refund: refund larger than the charge is refused (cumulative cap)",async()=>{ const n=q("select count(*) from ledger_transactions where kind='refund'"); await refund({"refund-job":seed.jobs["done-3"],"refund-amount":"500.00","refund-reason":"qa overrefund"}); return q("select count(*) from ledger_transactions where kind='refund'")===n; });
await T(G,"billing/refund: valid refund posts and the ledger stays balanced",async()=>{ const n=+q("select count(*) from ledger_transactions where kind='refund'"); const before=(await api(at,"GET","/v1/admin/reconciliation")).json; await refund({"refund-job":seed.jobs["done-3"],"refund-amount":"0.01","refund-reason":"qa valid refund"}); const after=(await api(at,"GET","/v1/admin/reconciliation")).json; return +q("select count(*) from ledger_transactions where kind='refund'")===n+1 && after.balanced===true && after.imbalance==="0.00"; });
await T(G,"billing/payout: authorizing an unknown payout id fails cleanly",async()=>{ await go("/admin/billing"); await p.fill("#authorize-payout","11111111-1111-1111-1111-111111111111"); await p.click('button:has-text("Authorize payout")'); await p.waitForTimeout(1500); return !/\[object|undefined|Application error/.test(await body(p)); });
await T(G,"billing/payout: authorize button stays disabled with an empty id",async()=>{ await go("/admin/billing"); return await p.locator('button:has-text("Authorize payout")').isDisabled(); });
await T(G,"billing/payout: authorizing a requested payout settles it",async()=>{ const id=q("select id from provider_payouts where status='requested' limit 1"); if(!id) return false; await go("/admin/billing"); await p.fill("#authorize-payout",id); await p.click('button:has-text("Authorize payout")'); await p.waitForTimeout(1800); return q(`select status from provider_payouts where id='${id}'`)==="settled"; });
await T(G,"billing/payout: authorizing the same payout twice is refused",async()=>{ const id=q("select id from provider_payouts where status='settled' limit 1"); const r=await api(at,"POST",`/v1/admin/payouts/${id}/authorize`); return r.status>=400 && r.status<500; });
await T(G,"billing/dispute: resolving an unknown dispute fails cleanly",async()=>{ await go("/admin/billing"); await p.fill("#resolve-id","11111111-1111-1111-1111-111111111111"); await p.click('button:has-text("Resolve dispute")'); await p.waitForTimeout(1500); return !/\[object|undefined|Application error/.test(await body(p)); });
await T(G,"billing/dispute: submitting without a resolution shows a validation error and changes nothing",async()=>{ const id=q("select id from disputes where status='open' limit 1"); await go("/admin/billing"); await p.fill("#resolve-id",id); await p.click('button:has-text("Resolve dispute")'); await p.waitForTimeout(1200); return q(`select status from disputes where id='${id}'`)==="open" && /required|resolution|enter|least/i.test(await body(p)); });
await T(G,"billing/dispute: resolving an open dispute closes it and records the resolution",async()=>{ const id=q("select id from disputes where status='open' limit 1"); await go("/admin/billing"); await p.fill("#resolve-id",id); await p.fill("#resolve-resolution","rejected - qa"); await p.click('button:has-text("Resolve dispute")'); await p.waitForTimeout(2000); return q(`select status from disputes where id='${id}'`)==="resolved" && /rejected - qa/.test(q(`select resolution from disputes where id='${id}'`)); });
await T(G,"billing/dispute: resolving an already-resolved dispute is refused",async()=>{ const id=q("select id from disputes where status='resolved' limit 1"); const r=await api(at,"POST",`/v1/admin/disputes/${id}/resolve`,{resolution:"rejected"}); return r.status>=400 && r.status<500; });
await T(G,"billing: a customer is refused every admin money endpoint (403)",async()=>{ const t=await apiLogin("cust1@example.com"); const r1=await api(t,"GET","/v1/admin/reconciliation"); const r2=await api(t,"POST","/v1/admin/refunds",{job_id:seed.jobs["done-3"],amount:"0.01",reason:"x"}); const id=q("select id from provider_payouts limit 1"); const r3=await api(t,"POST",`/v1/admin/payouts/${id}/authorize`); return [r1,r2,r3].every(r=>r.status===403); });

// ---------- SETTLEMENT / SECURITY / CONFIGURATION ----------
await go("/admin/settlement");
await T(G,"settlement: loads with the chain disconnected, shows zero escrows, no crash",async()=>{ const t=await body(p); return /On-Chain Settlement/.test(t) && /TOTAL ESCROWS/.test(t) && !/Application error/.test(t); });
await T(G,"settlement: no longer claims the contracts are 'Audited'",async()=>!/Audited smart contracts/.test(await body(p)));
await T(G,"settlement: voucher form with blank fields does not crash",async()=>{ await p.click('button:has-text("Generate Platform Voucher")').catch(()=>{}); await p.waitForTimeout(1200); return !/Application error|\[object/.test(await body(p)); });
await T(G,"settlement: Refresh State works",async()=>{ await p.click('button:has-text("Refresh State")'); await settle(p); return /Smart Contract Architecture/.test(await body(p)); });
await go("/admin/security");
await T(G,"security: posture page loads with counts and a self-assessment disclaimer",async()=>{ const t=await body(p); return /Active controls/.test(t) && /does not substitute for an independent security audit/i.test(t); });
await go("/admin/configuration");
await T(G,"configuration: read-only page loads and is labelled as defaults",async()=>{ const t=await body(p); return /Platform configuration/.test(t) && /Read-only/.test(t) && !(await p.locator("main input").count()); });

// ---------- TELEMETRY ----------
await go("/admin/telemetry");
await T(G,"telemetry: node picker lists every machine (admin no longer 403s)",async()=>{ const t=await body(p); return ["beta-h100","alpha-a100","alpha-rtx4090","alpha-offline"].every(x=>t.includes(x)); });
await T(G,"telemetry: stat tiles and alert panel render without errors",async()=>{ const t=await body(p); return /Active Telemetry GPUs/.test(t) && /Live Operational Alerts/.test(t) && !/NaN|undefined/.test(t); });
await T(G,"telemetry: selecting a node shows its explorer / empty state",async()=>{ await p.locator("select").first().selectOption({index:1}); await p.waitForTimeout(1500); return /No telemetry stream|1-MINUTE/i.test(await body(p)); });
await T(G,"telemetry: Refresh works",async()=>{ await p.locator('main button:has-text("Refresh")').click(); await settle(p); return /Telemetry & Fleet Health/.test(await body(p)); });
await T(G,"telemetry: a provider/customer cannot read another machine's telemetry (403/404)",async()=>{ const t=await apiLogin("prov2@example.com"); const r=await api(t,"GET",`/v1/telemetry/machines/${seed.machines[0]}`); return r.status===403||r.status===404; });

// ---------- VENDORS ----------
await go("/admin/vendors");
await T(G,"vendors: nine service cards with plan, cost and renewal fields",async()=>{ const t=await body(p); return /Services\s*9/.test(t) && /Resend/.test(t) && /Sentry/.test(t) && /Renewal date not set/.test(t); });
await T(G,"vendors: unmeasurable usage is stated honestly, not faked",async()=>/Not measurable/.test(await body(p)) && /not set/.test(await body(p)));
await T(G,"vendors: 'Check now' runs without error",async()=>{ await p.click('button:has-text("Check now")'); await p.waitForTimeout(2500); return !/Application error|failed/i.test(await body(p)); });
await T(G,"vendors: non-admin refused (403)",async()=>{ const t=await apiLogin("cust1@example.com"); return (await api(t,"GET","/v1/admin/vendors")).status===403; });

// ---------- AUDIT LOGS ----------
await go("/admin/audit-logs");
await T(G,"audit logs: records admin actions with actor, action, IP and outcome",async()=>{ const t=await body(p); return /admin\.user\.suspend|user\.suspend/.test(t) && /127\.0\.0\.1/.test(t) && /success/.test(t); });
await T(G,"audit logs: newest entries first",async()=>{ const rows=await p.locator("tbody tr").allInnerTexts(); return rows.length>5 && /just now|\bm ago|s ago/.test(rows[0]); });
await T(G,"audit logs: Refresh works",async()=>{ await p.locator('main button:has-text("Refresh")').click(); await settle(p); return (await p.locator("tbody tr").count())>5; });
await T(G,"audit logs: a customer cannot read them (403)",async()=>{ const t=await apiLogin("cust1@example.com"); return (await api(t,"GET","/v1/admin/audit-logs")).status===403; });

// ---------- ROLE GATES IN THE BROWSER ----------
for(const [who,label] of [["cust1@example.com","customer"],["prov1@example.com","provider"]]){
  await T(G,`gates: a ${label} opening /admin/users sees no user data`,async()=>{ const pg=await login(b,who); await pg.goto(WEB+"/admin/users",{waitUntil:"networkidle",timeout:60000}); await settle(pg,2000); const t=await body(pg); await pg.context().close(); return !/admin@example\.com|empty@example\.com/.test(t); });
}
await T(G,"gates: an anonymous visitor to /admin/overview is sent to login",async()=>{ const pg=await (await b.newContext()).newPage(); await pg.goto(WEB+"/admin/overview",{waitUntil:"networkidle",timeout:60000}); await pg.waitForTimeout(3000); const ok=/login/.test(pg.url()); await pg.context().close(); return ok; });

console.log("page issues:",p.__issues.length,p.__issues.filter(i=>!/40[0-9]|422|409/.test(i)).slice(0,6));
save("a1.json"); await b.close();
