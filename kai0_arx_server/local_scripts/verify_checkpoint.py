"""Verify rsync output against the hashes read directly on .117. No hardware or GPU."""
from pathlib import Path
import hashlib
import json
import time

root=Path(__file__).resolve().parents[1]
checkpoint=root/'checkpoints/place_plate/015000/pretrained_model'
manifest=json.loads((root/'SOURCE_MANIFEST.json').read_text())
for name,expected in manifest.items():
    path=checkpoint/name
    if path.stat().st_size!=expected['size']:raise RuntimeError(f'Size mismatch: {name}')
    sha=hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(8*1024*1024),b''):sha.update(data)
    if sha.hexdigest()!=expected['sha256']:raise RuntimeError(f'SHA256 mismatch: {name}')
    print('VERIFIED',name,flush=True)
temporary=root/'.CHECKPOINT_VERIFIED.tmp'
temporary.write_text(json.dumps({'checkpoint':str(checkpoint),'source':'zhhy@192.168.2.117:/home/zhhy/projects/kai0/checkpoints/place_plate/015000/pretrained_model',
                                 'verified_at':time.time(),'files':manifest},indent=2))
temporary.replace(root/'CHECKPOINT_VERIFIED.json')
print('ALL CHECKPOINT FILES VERIFIED')
