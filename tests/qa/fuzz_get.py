import httpx, re, uuid, collections, json
import os
BASE=os.environ.get("QA_API","http://localhost:8010"); spec=httpx.get(BASE+"/openapi.json").json()
def login(e): return httpx.post(BASE+"/auth/login",json={"email":e,"password":"Password123!"}).json()["access_token"]
TOK={"admin":login("admin@example.com"),"cust":login("cust1@example.com")}
PV=["not-a-uuid","../../etc/passwd","%00","0","-1","99999999999999999999","' OR 1=1--","é中","a"*3000,str(uuid.uuid4())]
QV=["", "abc", "-1", "0", "99999999999999999999", "NaN", "1e999", "%00", "' OR 1=1--", "a"*3000, "true", "[]", "2026-13-45", "é"]
fails=collections.defaultdict(list); n=0
for path,ops in spec["paths"].items():
    if "get" not in ops or path.startswith(("/openapi","/docs","/redoc","/metrics","/ws")): continue
    params=ops["get"].get("parameters",[])
    q=[p["name"] for p in params if p["in"]=="query"]; hasp="{" in path
    combos=[]
    for pv in (PV if hasp else [None]):
        u=re.sub(r"\{[^}]+\}",lambda m:pv if pv else "x",path) if hasp else path
        combos.append((u,{}))
        for name in q:
            for v in QV: combos.append((u,{name:v}))
    for who,t in TOK.items():
        for u,qs in combos:
            n+=1
            try: r=httpx.get(BASE+u,params=qs,headers={"Authorization":"Bearer "+t},timeout=30)
            except Exception as e: fails[path].append((who,"EXC "+str(e)[:50])); continue
            if r.status_code>=500: fails[path].append((who,u[:50],qs,r.status_code))
print("requests:",n,"| endpoints with 5xx:",len(fails))
for k,v in sorted(fails.items()): print(" ",k,"x%d"%len(v),v[0])
