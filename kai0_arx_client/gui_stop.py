#!/usr/bin/env python3
"""Request SDK protection independently of the browser; does not request hold."""
import argparse,json,urllib.request,uuid
p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8092);a=p.parse_args()
url=f'http://127.0.0.1:{a.port}'
try:
    token=json.load(urllib.request.urlopen(url+'/api/session',timeout=2))['token']
    r=urllib.request.Request(url+'/api/action',data=json.dumps({'action':'stop'}).encode(),
        headers={'Content-Type':'application/json','X-Control-Token':token,'X-Control-Owner':str(uuid.uuid4())})
    with urllib.request.urlopen(r,timeout=2) as f:print(f.read().decode())
    print('已请求软件急停/SDK保护，不是位置保持；不代表机械臂已经停止。')
except Exception as e:
    print('停止请求失败：',e,'；有危险请立即使用硬件急停。');raise SystemExit(1)
