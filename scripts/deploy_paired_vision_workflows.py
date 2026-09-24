"""Create paired image-plus-numeric n8n workflows, preserving numeric controls.

Copies the user's repaired alpha chart-fetch path and the tested ProDex image
agent. Workflow exports and Chart-Img session headers stay in memory/private Pi
backups and are never printed or committed.
"""
import copy
from datetime import datetime, timezone
import json
import re
import shlex
import subprocess
import sys
import uuid

CONTAINER='n8n-docker-caddy-n8n-1'
BACKUP_DIR='/home/thetopham/.config/tradingview-bot/n8n-backups'
CONTROL={
 'alpha':('7a86d6d4684846ea','e4b40e7c2f694f3a','5m'),
 'beta':('1449a84ff77e4f11','ab0b4fdd7a36428a','5m'),
 'gamma':('096d30bc2c0a4ccd','c8907a384fd546d7','5m'),
 'delta':('92060dd327f8431e','c8a3ed72011d4070','15m'),
 'epsilon':('db4dd0b4058d47ef','80b5c1746f2d4abc','30m'),
}
CHART_NODES=['Supabase5','If','HTTP Request4','Tradingview Chart','Supabase6','HTTP Request7','set url']

def ssh(*args,input_bytes=None):
 r=subprocess.run(['wsl.exe','--exec','ssh','-o','BatchMode=yes','pi',*args],input=input_bytes,capture_output=True,timeout=120)
 if r.returncode: raise RuntimeError(f'Pi command failed: exit {r.returncode}')
 return r.stdout

def export_all():
 raw=ssh('docker','exec',CONTAINER,'n8n','export:workflow','--all').decode(errors='replace')
 return json.JSONDecoder().raw_decode(raw[raw.find('['):])[0]

def edge(name):return {'node':name,'type':'main','index':0}

def adapt_prompt(prompt,account):
 old='Use only the numeric datafeed snapshot; no chart image is supplied.'
 assert prompt.count(old)==1
 prompt=prompt.replace(old,'An attached chart image shows recent price action. Use it for visual context. The numeric datafeed is authoritative for exact prices, brackets, and position data.')
 version=re.search(r'("prompt_version"\s*:\s*")([^" ]+)(")',prompt)
 assert version, f'prompt version not found for {account}'
 old_version=version.group(2)
 new_version=(old_version.replace('-prodex-v1','-vision-prodex-v1')
              if old_version.endswith('-prodex-v1') else old_version+'-vision')
 prompt=prompt[:version.start(2)]+new_version+prompt[version.end(2):]
 return prompt

def adapt_chart_nodes(nodes,timeframe):
 by={n['name']:n for n in nodes}
 for n in nodes:n['id']=str(uuid.uuid4())
 for cond in by['Supabase5']['parameters']['filters']['conditions']:
  if cond['keyName']=='timeframe':cond['keyValue']=timeframe
 by['If']['parameters']['conditions']['conditions'][0]['rightValue']={'5m':4.8,'15m':14.8,'30m':29.8}[timeframe]
 for cond in by['Supabase6']['parameters']['filters']['conditions']:
  if cond['keyName']=='timeframe':cond['keyValue']=timeframe
 interval=[x for x in by['Tradingview Chart']['parameters']['bodyParameters']['parameters'] if x.get('name')=='interval']
 assert len(interval)==1
 interval[0]['value']=timeframe
 assert by['set url']['parameters']['options'].get('includeBinary') is True

def build_variant(control,alpha_vision,account,variant_id,timeframe):
 w=copy.deepcopy(control)
 w['id']=variant_id
 w['name']=f'MES {timeframe} {account} ProDex image and numeric simulator'
 w['active']=False
 w['activeVersionId']=None
 w['versionId']=str(uuid.uuid4())
 for k in ('createdAt','updatedAt','shared','sourceWorkflowId','versionMetadata'):w.pop(k,None)
 nodes={n['name']:n for n in w['nodes']}
 assert 'Basic LLM Chain' in nodes and 'ProDex Chat Model' in nodes
 prompt=adapt_prompt(nodes['Basic LLM Chain']['parameters']['text'],account)
 w['nodes']=[n for n in w['nodes'] if n['name'] not in ('Basic LLM Chain','ProDex Chat Model')]
 w['connections'].pop('Basic LLM Chain',None)
 w['connections'].pop('ProDex Chat Model',None)
 source={n['name']:n for n in alpha_vision['nodes']}
 chart=[copy.deepcopy(source[name]) for name in CHART_NODES]
 adapt_chart_nodes(chart,timeframe)
 w['nodes'].extend(chart)
 agent=copy.deepcopy(source['ProDex Vision'])
 agent['id']=str(uuid.uuid4())
 agent['parameters']['prompt']=prompt
 agent['parameters']['reasoningEffort']='medium'
 agent['parameters']['workingDirectory']='/home/node/.n8n/prodex-trading-workspace'
 w['nodes'].append(agent)
 webhook=nodes['Webhook']
 webhook['parameters']['path']=f'simple{timeframe}-{account}-vision-sim-prodex'
 webhook['webhookId']=str(uuid.uuid4())
 code=nodes['Code']['parameters']['jsCode']
 old='let raw = $input.first().json.text;'
 assert code.count(old)==1
 nodes['Code']['parameters']['jsCode']=code.replace(old,'let raw = $input.first().json.output ?? $input.first().json.text;',1)
 assert w['connections']['Continuity Context']['main'][0]==[edge('Basic LLM Chain')]
 w['connections']['Continuity Context']={'main':[[edge('Supabase5')]]}
 w['connections']['Supabase5']={'main':[[edge('If')]]}
 w['connections']['If']={'main':[[edge('HTTP Request4')],[edge('Tradingview Chart')]]}
 w['connections']['HTTP Request4']={'main':[[edge('set url')]]}
 w['connections']['Tradingview Chart']={'main':[[edge('Supabase6')]]}
 w['connections']['Supabase6']={'main':[[edge('HTTP Request7')]]}
 w['connections']['HTTP Request7']={'main':[[edge('set url')]]}
 w['connections']['set url']={'main':[[edge('ProDex Vision')]]}
 w['connections']['ProDex Vision']={'main':[[edge('Code')]]}
 assert len({n['name'] for n in w['nodes']})==len(w['nodes'])
 return w

live={w['id']:w for w in export_all()}
alpha_vision=copy.deepcopy(live[CONTROL['alpha'][1]])
assert any(n['name']=='ProDex Vision' for n in alpha_vision['nodes'])

# Correct the alpha chart interval copied from 30m in the user's repaired file.
alpha_nodes={n['name']:n for n in alpha_vision['nodes']}
alpha_nodes['ProDex Vision']['parameters']['reasoningEffort']='medium'
interval=[x for x in alpha_nodes['Tradingview Chart']['parameters']['bodyParameters']['parameters'] if x.get('name')=='interval']
assert len(interval)==1
interval[0]['value']='5m'
alpha_vision['versionId']=str(uuid.uuid4())

variants=[alpha_vision]
for account,(control_id,vision_id,timeframe) in CONTROL.items():
 if account=='alpha':continue
 assert vision_id not in live
 variants.append(build_variant(live[control_id],alpha_vision,account,vision_id,timeframe))

if '--dry-run' in sys.argv:
 print(json.dumps({'planned':[(a,CONTROL[a][1],len(w['nodes'])) for a,w in zip(CONTROL,variants)]}))
 raise SystemExit(0)

# A direct n8n edit should not be lost during preparation.
fresh={w['id']:w for w in export_all()}
for wid in [CONTROL['alpha'][1]]+[CONTROL[a][0] for a in CONTROL if a!='alpha']:
 if (fresh[wid].get('updatedAt'),fresh[wid].get('versionId'))!=(live[wid].get('updatedAt'),live[wid].get('versionId')):
  raise RuntimeError('Workflow changed during preparation; retry')

stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backup=f'{BACKUP_DIR}/before-vision-pairs-{stamp}.json'
ssh('sh','-c',shlex.quote(f'umask 077; cat > {backup}'),
    input_bytes=json.dumps([live[CONTROL['alpha'][1]]],ensure_ascii=False).encode())
stage=f'/tmp/vision-pairs-{uuid.uuid4().hex[:8]}.json'
try:
 ssh('docker','exec','-i',CONTAINER,'sh','-c',shlex.quote(f'umask 077; cat > {stage}'),
     input_bytes=json.dumps(variants,ensure_ascii=False).encode())
 ssh('docker','exec',CONTAINER,'n8n','import:workflow',f'--input={stage}')
 for w in variants:ssh('docker','exec',CONTAINER,'n8n','publish:workflow',f"--id={w['id']}")
finally:
 ssh('docker','exec',CONTAINER,'rm','-f',stage)
print(json.dumps({'deployed':[(a,CONTROL[a][1],len(w['nodes'])) for a,w in zip(CONTROL,variants)],'backup':backup}))
