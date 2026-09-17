"""Read-only snapshot of the existing local data station. Does not bind cameras/CAN."""
import io
import json
from pathlib import Path
import time
import urllib.request
from PIL import Image
import numpy as np
from client import make_observation

root = Path(__file__).resolve().parent / 'logs' / f'station-{time.time_ns()}'
root.mkdir(parents=True)
base = 'http://127.0.0.1:8090'
with urllib.request.urlopen(base + '/api/status', timeout=3) as response:
    status = json.load(response)
(root / 'status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2))
images = {}
for role in ('head', 'left', 'right'):
    with urllib.request.urlopen(base + '/stream/' + role, timeout=3) as response:
        data = bytearray()
        deadline = time.monotonic() + 5
        while len(data) < 2_000_000 and time.monotonic() < deadline:
            block = response.read(4096)
            if not block:
                break
            data.extend(block)
            begin = data.find(b'\xff\xd8')
            end = data.find(b'\xff\xd9', begin + 2) if begin >= 0 else -1
            if end >= 0:
                jpeg = bytes(data[begin:end + 2])
                rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert('RGB'))
                (root / f'{role}.jpg').write_bytes(jpeg)
                images[role] = rgb
                print(role, rgb.shape, 'station preview OK')
                break
        if role not in images:
            raise RuntimeError(f'No JPEG received for {role}')
obs = make_observation(status['arm']['positions'], images, 'observation format check only')
print('Observed state shape:', obs['state'].shape)
print('Model images:', {k: v.shape for k, v in obs['images'].items()})
print('Station arm mode:', status['arm']['mode'])
print('Station reported faults:', status['arm'].get('faults'))
print('Saved:', root)
print('No inference or motion requested. JPEG stream preview is diagnostic, not synchronized control input.')
