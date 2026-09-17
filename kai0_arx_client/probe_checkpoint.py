"""Evaluate real station observations against a named checkpoint without robot control."""
import argparse
import io
import json
from pathlib import Path
import time
import urllib.request

import numpy as np
from PIL import Image
from client import ROOT, Policy


def get_status(base):
    with urllib.request.urlopen(base + '/api/status', timeout=3) as r:
        return json.load(r)


def capture(base):
    names = {'head': 'top_head', 'left': 'hand_left', 'right': 'hand_right'}
    before = get_status(base)
    if not before['arm']['online'] or before['arm']['age_ms'] > 250 or any(before['arm']['faults'].values()):
        raise RuntimeError('No fresh fault-free arm feedback')
    images = {}
    for role, name in names.items():
        if not before['cameras'][role]['online']:
            raise RuntimeError(f'{role} camera is offline')
        with urllib.request.urlopen(base + '/stream/' + role, timeout=3) as r:
            data = bytearray(); deadline = time.monotonic() + 5
            while len(data) < 2_000_000 and time.monotonic() < deadline:
                block = r.read(4096)
                if not block: break
                data.extend(block)
                start = data.find(b'\xff\xd8')
                end = data.find(b'\xff\xd9', start + 2) if start >= 0 else -1
                if end >= 0:
                    rgb = np.asarray(Image.open(io.BytesIO(data[start:end + 2])).convert('RGB'))
                    images[name] = np.ascontiguousarray(rgb.transpose(2, 0, 1)); break
            if name not in images: raise RuntimeError(f'{role}: no JPEG')
    after = get_status(base)
    arm = after['arm']
    if not arm['online'] or arm['age_ms'] > 250 or any(arm['faults'].values()):
        raise RuntimeError('Arm feedback became unavailable')
    for role in names:
        cam = after['cameras'][role]
        if not cam['online'] or cam['age_ms'] > 250 or cam['frame_number'] <= before['cameras'][role]['frame_number']:
            raise RuntimeError(f'{role}: stale camera stream')
    state = np.asarray(arm['positions'], np.float32)
    if state.shape != (14,) or not np.isfinite(state).all(): raise ValueError('Invalid state')
    return {'state': state, 'images': images}, after


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--server', default='ws://192.168.2.117:8000')
    p.add_argument('--station', default='http://127.0.0.1:8090')
    p.add_argument('--prompt')
    p.add_argument('--count', type=int, default=5)
    args = p.parse_args()
    if not 1 <= args.count <= 100: p.error('--count must be 1..100')
    policy = Policy(args.server, 30)
    directory = ROOT / 'logs' / f'checkpoint-probe-{time.time_ns()}'; directory.mkdir()
    try:
        metadata = policy.metadata
        if metadata.get('mode') != 'checkpoint' or not metadata.get('weights_strictly_loaded'):
            raise RuntimeError('Expected a verified real-checkpoint service')
        obs, status = capture(args.station)
        obs['prompt'] = args.prompt or metadata['default_prompt']
        config = json.loads((ROOT / 'config.json').read_text())
        lo, hi = np.array(config['safety']['lower']), np.array(config['safety']['upper'])
        rounds = []; all_actions = []
        for i in range(args.count):
            started = time.monotonic(); actions = policy.infer(obs); elapsed = time.monotonic() - started
            if actions.shape != (metadata['action_horizon'], 14): raise ValueError('Wrong action horizon')
            rounds.append(elapsed * 1000); all_actions.append(actions)
            print(f'Inference {i+1}: {actions.shape}, {elapsed*1000:.1f} ms; no motion', flush=True)
        actions = np.stack(all_actions)
        violations = np.any((actions < lo) | (actions > hi), axis=(0, 1))
        report = {'passed': True, 'motion_executed': False, 'server': args.server, 'metadata': metadata,
                  'prompt': obs['prompt'], 'source': 'One real station snapshot, replayed for timing; not synchronized control input',
                  'requests': args.count, 'action_shape': list(actions.shape[1:]), 'all_actions_finite': bool(np.isfinite(actions).all()),
                  'not_state_echo': not bool(np.allclose(actions, obs['state'])),
                  'round_trip_ms': rounds, 'mean_round_trip_ms': float(np.mean(rounds)),
                  'images': {k: list(v.shape) for k,v in obs['images'].items()},
                  'arm_mode': status['arm']['mode'], 'arm_faults': status['arm']['faults'],
                  'recording_state': status['recording']['state'], 'dataset_episodes': status['recording']['episodes'],
                  'outside_client_limits': [metadata['action_names'][i] for i in np.flatnonzero(violations)],
                  'action_min': actions.min(axis=(0,1)).tolist(), 'action_max': actions.max(axis=(0,1)).tolist()}
        np.savez_compressed(directory/'observations_and_actions.npz', state=obs['state'], actions=actions, **obs['images'])
        (directory/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2)); print('REPORT', directory/'report.json')
    finally:
        policy.close()


if __name__ == '__main__':
    main()
