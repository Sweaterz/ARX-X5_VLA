"""Local protocol test policy. Echoes current state; this is not a trained VLA."""
import argparse
import numpy as np
from websockets.sync.server import serve
from vendor import msgpack_numpy


def handle(ws):
    ws.send(msgpack_numpy.packb({'test_only': True, 'action_dim': 14, 'protocol': 'kai0-arx'}))
    for raw in ws:
        obs = msgpack_numpy.unpackb(raw)
        assert set(obs['images']) == {'top_head', 'hand_left', 'hand_right'}
        assert all(x.shape == (3, 224, 224) and x.dtype == np.uint8 for x in obs['images'].values())
        assert obs['state'].shape == (14,) and obs['prompt'].strip()
        ws.send(msgpack_numpy.packb({'actions': np.tile(obs['state'], (8, 1))}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=18000)
    args = p.parse_args()
    with serve(handle, '127.0.0.1', args.port, compression=None) as server:
        print(f'TEST policy ws://127.0.0.1:{args.port}', flush=True)
        server.serve_forever()
