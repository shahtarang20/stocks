import json, glob, re, collections, datetime
import os
S=os.environ.get("QA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
inv=json.load(open(S+"/inventory.json"))
files=["c1","c2","c3","p1","a1","pub","mobile","w1"]
results=[]
for f in files:
    try: results+= [dict(r,suite=f) for r in json.load(open(f"{S}/{f}.json"))]
    except Exception as e: print("missing",f)
skip={"E","C","P","A","","Toggle color theme"}
out=[]
out.append("# ComputeCoin QA checklist and results\n")
out.append(f"Generated {datetime.date.today()} from a live crawl of the running web app plus the browser test suites in `tests/qa/`.\n")
tot=len(results); ok=sum(r["ok"] for r in results)
out.append(f"**{ok} of {tot} control-level checks pass.** Each check drives a real browser (or the API directly for permission and race checks) against a stack with seeded customers, providers, machines, jobs in every status, disputes, payouts and refunds.\n")
out.append("## 1. Control inventory (what exists on each page)\n")
out.append("Counts are visible interactive elements found by crawling the live DOM as the right role. Dialog-only fields appear in section 2.\n")
out.append("| Role | Page | Buttons | Fields | Links | Controls |\n|---|---|---|---|---|---|")
tb=tf=tl=0
for k,v in inv.items():
    who,path=k.split("|"); who=who.split("@")[0] or "public"
    names=[b["t"] for b in v["buttons"] if b["t"] not in skip]
    nb=len(names); nf=len(v["fields"]); nl=len({l["h"] for l in v["links"]})
    tb+=nb; tf+=nf; tl+=nl
    ctrl=", ".join(dict.fromkeys(names))[:90]
    if v["fields"]: ctrl+=(" · " if ctrl else "")+", ".join(dict.fromkeys((f["id"] or f["ph"] or f["type"])[:18] for f in v["fields"]))[:80]
    out.append(f"| {who} | `{path[:46]}` | {nb} | {nf} | {nl} | {ctrl or '—'} |")
out.append(f"\nTotals across {len(inv)} pages: {tb} buttons, {tf} form fields, {tl} link slots.\n")
out.append("## 2. Test results by area\n")
by=collections.OrderedDict()
for r in results: by.setdefault((r["group"],r["suite"]),[]).append(r)
names={"c1":"Customer: dashboard, navigation, projects, API keys","c2":"Customer: job form, job list, job detail","c3":"Customer: marketplace, quotes, reservations, billing, invoices, usage, disputes, settings","p1":"Provider: onboarding, dashboard, hardware, earnings, payouts","a1":"Admin: every admin page","pub":"Public pages and authentication","mobile":"Mobile layout (390px) on every page","w1":"Wallet: link, balance, refusal and ownership checks against a real local chain"}
out.append("| Area | Checks | Pass | Fail |\n|---|---|---|---|")
for (g,s),rs in by.items():
    out.append(f"| {names.get(s,s)} | {len(rs)} | {sum(x['ok'] for x in rs)} | {sum(not x['ok'] for x in rs)} |")
fails=[r for r in results if not r["ok"]]
out.append("\n### Failing checks\n")
if fails:
    for r in fails: out.append(f"- **{r['suite']}** {r['name']} {('— '+r['detail']) if r['detail'] else ''}")
else: out.append("None.")
out.append("\n### Every check\n")
cur=None
for r in results:
    if r["suite"]!=cur: cur=r["suite"]; out.append(f"\n**{names.get(cur,cur)}**\n")
    out.append(f"- [{'x' if r['ok'] else ' '}] {r['name']}")
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "QA_CHECKLIST.md"),"w").write("\n".join(out)+"\n")
print("wrote docs/QA_CHECKLIST.md:",ok,"/",tot)
