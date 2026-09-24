"""Report recent paired workflow results without prompts, images, or credentials."""
import subprocess

REMOTE=r'''
import json,sqlite3
c=sqlite3.connect('file:/var/lib/docker/volumes/n8n_data/_data/database.sqlite?mode=ro',uri=True)
ids={'alpha':'7a86d6d4684846ea','alpha_vision':'e4b40e7c2f694f3a',
'beta':'1449a84ff77e4f11','beta_vision':'ab0b4fdd7a36428a',
'gamma':'096d30bc2c0a4ccd','gamma_vision':'c8907a384fd546d7',
'delta':'92060dd327f8431e','delta_vision':'c8a3ed72011d4070',
'epsilon':'db4dd0b4058d47ef','epsilon_vision':'80b5c1746f2d4abc'}
def inspect(raw):
 d=json.loads(raw)
 def ref(v):return d[int(v)] if isinstance(v,str) and v.isdigit() else v
 root=d[0] if isinstance(d,list) else d
 result=ref(root.get('resultData')) or {}
 run=ref(result.get('runData')) or {}
 err=ref(result.get('error')) or {}
 info={'nodes':list(run),'error':str(ref(err.get('message')) or '')[:150] if isinstance(err,dict) else ''}
 for name in ['ProDex Vision','Supabase','Respond to Webhook']:
  arr=ref(run.get(name)) or []
  if not arr:continue
  execution=ref(arr[-1]); data=ref(execution.get('data')) or {}
  main=ref(data.get('main')) or []
  items=ref(main[0]) if main else []
  first=ref(items[0]) if items else {}
  payload=ref(first.get('json')) if isinstance(first,dict) else {}
  if isinstance(payload,dict):
   if name=='ProDex Vision':info['image_included']=ref(payload.get('imageIncluded'))
   if name=='Supabase':info['decision_id']=ref(payload.get('ai_decision_id'))
   if name=='Respond to Webhook':info['responded']=True
 return info
for account,wid in ids.items():
 rows=c.execute('select id,status,startedAt,stoppedAt from execution_entity where workflowId=? order by id desc limit 2',(wid,)).fetchall()
 out=[]
 for id,status,start,stop in rows:
  row={'id':id,'status':status,'startedAt':start}
  data=c.execute('select data from execution_data where executionId=?',(id,)).fetchone()
  if data:row.update(inspect(data[0]))
  out.append(row)
 print(json.dumps({'account':account,'runs':out}))
'''
r=subprocess.run(['wsl.exe','--exec','ssh','-o','BatchMode=yes','pi','sudo','-n','python3','-'],input=REMOTE.encode(),capture_output=True,timeout=30)
if r.returncode:raise SystemExit('Unable to read n8n execution metadata')
print(r.stdout.decode())
