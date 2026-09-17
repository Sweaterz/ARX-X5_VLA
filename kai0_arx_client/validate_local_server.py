"""Real local checkpoint inference with synthetic observations, without robot/cameras."""
import json
import time
import numpy as np
import client
from local_server import URL

policy=client.Policy(URL,30)
try:
    metadata=policy.metadata
    if metadata.get('mode')!='checkpoint' or not metadata.get('weights_strictly_loaded'):
        raise RuntimeError('Not a strictly loaded checkpoint server')
    c=json.loads((client.ROOT/'config.json').read_text())
    obs=client.make_observation(client.MockRobot().read(),client.Cameras(c,False).read(),metadata['default_prompt'])
    results=[]
    for _ in range(3):
        started=time.monotonic();actions=policy.infer(obs)
        if actions.shape!=(50,14) or not np.isfinite(actions).all():raise RuntimeError('Invalid actions')
        results.append({'round_trip_ms':round((time.monotonic()-started)*1000,2),
                        'shape':list(actions.shape),'min':float(actions.min()),'max':float(actions.max())})
    report={'metadata':metadata,'inferences':results,'synthetic_images':True,'robot_initialized':False,'time':time.time()}
    path=client.ROOT/'logs/local-server-validation.json';path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))
finally:policy.close()
