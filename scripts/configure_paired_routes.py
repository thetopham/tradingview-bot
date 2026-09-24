"""Configure five private n8n routes for new simulated vision accounts."""
import subprocess

REMOTE=r'''
from pathlib import Path
import os, shutil
from datetime import datetime,timezone

path=Path('/home/thetopham/.config/tradingview-bot-sim.env')
lines=path.read_text().splitlines()
values={}
for line in lines:
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1)
        values[key]=value.strip().strip('"').strip("'")
routes={
 'alpha':('ALPHA','simple5m-alpha-sim-prodex','simple5m-alpha-vision-sim-prodex'),
 'beta':('BETA','simple5m-beta-sim-prodex','simple5m-beta-vision-sim-prodex'),
 'gamma':('GAMMA','simple5m-gamma-sim-prodex','simple5m-gamma-vision-sim-prodex'),
 'delta':('DELTA','simple15m-delta-sim-prodex','simple15m-delta-vision-sim-prodex'),
 'epsilon':('EPSILON','simple30m-sim-prodex','simple30m-epsilon-vision-sim-prodex'),
}
additions=[]
for account,(source,old,new) in routes.items():
    url=values['N8N_OVERSEER_URL_'+source]
    assert url.rstrip('/').endswith('/'+old),(account,'unexpected source route')
    target=url.rstrip('/')[:-len(old)]+new
    name='N8N_OVERSEER_URL_'+account.upper()+'_VISION'
    existing=values.get(name)
    if existing and existing!=target:raise RuntimeError('conflicting existing vision route '+name)
    if not existing:additions.append(name+'='+target)
assert values['SIM_DECISION_SOURCE']=='scheduler'
if additions:
    backup=path.with_name(path.name+'.before-vision-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    shutil.copy2(path,backup)
    temporary=path.with_name(path.name+'.vision-tmp')
    temporary.write_text(path.read_text().rstrip()+'\n'+'\n'.join(additions)+'\n')
    os.chmod(temporary,0o600)
    os.replace(temporary,path)
print('configured paired routes:',len(routes),'new:',len(additions),'source:scheduler')
'''
r=subprocess.run(['wsl.exe','--exec','ssh','-o','BatchMode=yes','pi','python3','-'],input=REMOTE.encode(),capture_output=True,timeout=30)
if r.returncode:
    safe=' '.join(line for line in r.stderr.decode(errors='replace').splitlines()
                  if line.startswith(('AssertionError:','KeyError:','RuntimeError:','FileNotFoundError:')))
    raise SystemExit('Route configuration failed: '+(safe or f'exit {r.returncode}'))
print(r.stdout.decode().strip())
