import io,json,time,urllib.request
from pathlib import Path
import numpy as np
from PIL import Image
from client import Policy,make_observation,bounded_target,ROOT
from vendor import msgpack_numpy

def status():
 with urllib.request.urlopen('http://127.0.0.1:8090/api/status',timeout=3) as response:
  return json.load(response)

c=json.loads((ROOT/'config.json').read_text())
r=status()
assert r['recording']['state']=='idle', 'Recording must be idle'
assert r['arm']['online'] and not any(r['arm']['faults'].values()),'Arm feedback fault'
images={}
for role in ('head','left','right'):
 with urllib.request.urlopen('http://127.0.0.1:8090/stream/'+role,timeout=3) as response:
  data=bytearray();deadline=time.monotonic()+5
  while len(data)<2_000_000 and time.monotonic()<deadline:
   data.extend(response.read(4096));start=data.find(b'\xff\xd8');end=data.find(b'\xff\xd9',start+2) if start>=0 else -1
   if end>=0:
    images[role]=np.asarray(Image.open(io.BytesIO(bytes(data[start:end+2]))).convert('RGB'));break
  if role not in images:raise RuntimeError('Missing image: '+role)
r=status()
assert r['arm']['online'] and not any(r['arm']['faults'].values())
prompt=r['record_config']['task']
obs=make_observation(r['arm']['positions'],images,prompt)
# This server's RepackTransform explicitly requests LeRobot-style dotted keys.
payload=dict(obs)
payload['observation.state']=obs['state']
for name,img in obs['images'].items():
 payload['observation.images.'+name]=img
p=Policy(c['server']['url'],30)
audit=ROOT/'logs'/f'policy-probe-{time.time_ns()}';audit.mkdir()
report={'server':c['server']['url'],'metadata':p.metadata,'prompt':prompt,'motion_executed':False,
        'input_profile':'kai0 plus LeRobot dotted ARX keys','input_state_shape':list(obs['state'].shape),'input_images':{k:list(v.shape) for k,v in obs['images'].items()},
        'image_source':'Existing station JPEG previews; not a synchronized control sample'}
try:
 started=time.monotonic()
 p.ws.send(msgpack_numpy.packb(payload))
 result=p.receive()
 report['round_trip_s']=time.monotonic()-started
 report['response_keys']=list(result)
 actions=np.asarray(result.get('actions'),dtype=np.float64)
 report['action_shape']=list(actions.shape)
 report['actions_finite']=bool(np.isfinite(actions).all())
 np.savez_compressed(audit/'observations_and_actions.npz',state=obs['state'],actions=actions,**obs['images'])
 report['valid_action_layout']=bool(actions.ndim==2 and actions.shape[1]==14 and len(actions)>0 and np.isfinite(actions).all())
 if report['valid_action_layout']:
  report['first_action']=actions[0].tolist()
  report['max_action_delta_rad']=np.max(np.abs(actions-obs['state']),axis=0).tolist()
  try:
   for action in actions:bounded_target(action,obs['state'],c)
   report['within_configured_limits']=True
  except ValueError as exc:
   report['within_configured_limits']=False;report['limit_error']=str(exc)
 else:report['within_configured_limits']=False
except Exception as exc:
 report['error']=type(exc).__name__+': '+str(exc)
finally:
 p.close()
 (audit/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps(report,ensure_ascii=False,indent=2));print('REPORT',audit/'report.json')
