"""Serve an actual LeRobot PI05 checkpoint over the Kai0/OpenPI wire protocol."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import logging
from pathlib import Path
import threading
import time

import numpy as np
from vendor import msgpack_numpy
from websockets.sync.server import serve

CAMERAS = {'top_head': 'head', 'hand_left': 'left', 'hand_right': 'right'}
ACTION_NAMES = [*(f'left_joint_{i}' for i in range(1, 7)), 'left_gripper',
                *(f'right_joint_{i}' for i in range(1, 7)), 'right_gripper']
ROOT = Path(__file__).resolve().parents[1]


def execution_profile(path,config):
    # Original fold deployment: --n-action-steps=50; short replans repeat its hold prefix.
    try:training=json.loads((path/'train_config.json').read_text())
    except (OSError,ValueError):training={}
    if training.get('dataset',{}).get('repo_id')=='local/fold_box_v1_pi05':
        return min(50,config.chunk_size),'fold original deployment (50 ordered actions)'
    return min(8,config.chunk_size),'existing client default (8 ordered actions)'


def checkpoint_info(path):
    config = json.loads((path / 'config.json').read_text())
    if config['type'] != 'pi05' or config['use_relative_actions']:
        raise ValueError('Requires PI05 absolute-action checkpoint')
    if config['input_features']['observation.state']['shape'] != [14] or config['output_features']['action']['shape'] != [14]:
        raise ValueError('Requires 14D state and actions')
    if config.get('action_feature_names') != ACTION_NAMES:
        raise ValueError('Unexpected joint ordering')
    required = {'observation.state', *(f'observation.images.{r}' for r in CAMERAS.values())}
    if set(config['input_features']) != required:
        raise ValueError('Unexpected checkpoint cameras')
    for filename in ('model.safetensors', 'tokenizer/tokenizer.json', 'tokenizer/tokenizer_config.json'):
        if not (path / filename).is_file():
            raise FileNotFoundError(path / filename)
    for name in ('policy_preprocessor.json', 'policy_postprocessor.json'):
        for step in json.loads((path / name).read_text())['steps']:
            if step.get('state_file') and not (path / step['state_file']).is_file():
                raise FileNotFoundError(path / step['state_file'])
    return config


def observation(obs, default_prompt):
    state = np.asarray(obs.get('state', obs.get('observation.state')), dtype=np.float32)
    if state.shape != (14,) or not np.isfinite(state).all():
        raise ValueError('state must be finite [14]')
    prompt = obs.get('prompt', default_prompt)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('prompt must be nonempty text')
    images = obs.get('images')
    if images is None:
        images = {name: obs[f'observation.images.{name}'] for name in CAMERAS}
    prepared = {}
    for name, role in CAMERAS.items():
        image = np.asarray(images[name])
        if image.ndim != 3:
            raise ValueError(f'{name}: expected CHW or HWC RGB image')
        if image.shape[-1] == 3:
            image = image.transpose(2, 0, 1)
        if image.shape[0] != 3 or not all(1 <= n <= 2048 for n in image.shape[1:]):
            raise ValueError(f'{name}: invalid image shape')
        if image.dtype == np.uint8:
            image = image.astype(np.float32) / 255
        elif not np.issubdtype(image.dtype, np.floating) or not np.isfinite(image).all() or image.min() < 0 or image.max() > 1:
            raise ValueError(f'{name}: expected uint8 or float RGB in [0,1]')
        prepared[role] = np.ascontiguousarray(image, dtype=np.float32)
    return state.copy(), prepared, prompt


class CheckpointPolicy:
    def __init__(self, path, prompt):
        import torch
        from lerobot.configs.policies import PreTrainedConfig
        from lerobot.policies.pi05.modeling_pi05 import PI05Policy
        from lerobot.policies.factory import make_pre_post_processors
        from safetensors.torch import load_file
        self.path = path
        self.raw_config = checkpoint_info(path)
        self.default_prompt = prompt
        self.lock = threading.Lock()
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA is unavailable')
        config = PreTrainedConfig.from_pretrained(path, local_files_only=True)
        config.device = 'cuda'
        config.compile_model = False
        config.gradient_checkpointing = False
        config.rtc_config = None
        self.config = config
        self.pre, self.post = make_pre_post_processors(
            config, pretrained_path=str(path),
            preprocessor_overrides={'device_processor': {'device': 'cuda'},
                                    'tokenizer_processor': {'tokenizer_name': str(path / 'tokenizer')}},
            postprocessor_overrides={'device_processor': {'device': 'cpu'}},
        )
        self.policy = PI05Policy(config)
        # The installed from_pretrained catches load failures and can return random weights.
        # Load explicitly with strict validation so readiness can never mask that failure.
        weights = load_file(path / 'model.safetensors', device='cpu')
        model_keys = set(self.policy.state_dict())
        if set(weights) != model_keys:
            fixed = self.policy._fix_pytorch_state_dict_keys(weights, config)
            weights = {k if k.startswith('model.') else 'model.' + k: v for k, v in fixed.items()}
        loaded = self.policy.load_state_dict(weights, strict=True)
        if loaded.missing_keys or loaded.unexpected_keys:
            raise RuntimeError('Incomplete checkpoint weights')
        self.weight_count = len(weights)
        del weights
        self.policy.eval()
        torch.cuda.synchronize()
        execution_steps,execution_source=execution_profile(path,config)
        self.metadata = {
            'service': 'kai0-arx-lerobot-pi05', 'mode': 'checkpoint', 'test_only': False,
            'motion_enabled': True, 'hardware_validated': False,
            'checkpoint': str(path), 'checkpoint_step': path.parent.name,
            'checkpoint_config_sha256': hashlib.sha256((path / 'config.json').read_bytes()).hexdigest(),
            'policy_type': 'pi05', 'action_dim': 14, 'action_horizon': config.chunk_size,
            'recommended_actions_per_chunk':execution_steps,'execution_profile':execution_source,
            'action_names': ACTION_NAMES, 'action_semantics': 'absolute SDK joint angles; left then right',
            'default_prompt': prompt, 'input_profile': 'state[14], images{top_head,hand_left,hand_right}, prompt',
            'checkpoint_cameras': list(CAMERAS.values()), 'weights_strictly_loaded': self.weight_count,
            'runtime': {k: importlib.metadata.version(k) for k in ('lerobot','torch','transformers')},
            'notice': 'Real checkpoint inference; physical motion has not been validated.',
        }

    def infer(self, obs):
        import torch
        state, images, prompt = observation(obs, self.default_prompt)
        with self.lock, torch.inference_mode():
            started = time.perf_counter()
            batch = {'observation.state': torch.from_numpy(state), 'task': prompt}
            batch.update({f'observation.images.{role}': torch.from_numpy(img) for role, img in images.items()})
            batch = self.pre(batch)
            normalized = self.policy.predict_action_chunk(batch)
            actions = self.post(normalized)
            if isinstance(actions, dict):
                actions = actions['action']
            actions = actions.detach().to(device='cpu', dtype=torch.float32).numpy()
            expected = (1, self.config.chunk_size, 14)
            if actions.shape != expected or not np.isfinite(actions).all():
                raise ValueError(f'Invalid model actions: {actions.shape}')
            return {'actions': actions[0], 'policy_timing': {'infer_ms': (time.perf_counter() - started) * 1000},
                    'checkpoint_step': self.path.parent.name}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--checkpoint', type=Path, default=ROOT / 'checkpoints/place_plate/015000/pretrained_model')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8001)
    p.add_argument('--prompt', default='pick up the plate  on the ruck.')
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, force=True)
    policy = CheckpointPolicy(args.checkpoint.resolve(), args.prompt)
    warmup = {'state': np.array([0, .5, .8, 0, 0, 0, -.5] * 2, np.float32),
              'images': {name: np.zeros((3, 480, 640), np.uint8) for name in CAMERAS}, 'prompt': args.prompt}
    for i in range(2):
        result = policy.infer(warmup)
        logging.info('Warmup %s: actions=%s %.1f ms', i + 1, result['actions'].shape, result['policy_timing']['infer_ms'])
    def handler(ws):
        ws.send(msgpack_numpy.packb(policy.metadata))
        for raw in ws:
            try:
                if isinstance(raw, str):
                    raise ValueError('Expected binary msgpack input')
                obs = msgpack_numpy.unpackb(raw)
                if not isinstance(obs, dict):
                    raise ValueError('Expected an observation map')
                result = policy.infer(obs)
            except Exception as exc:
                logging.exception('Inference rejected')
                ws.send(f'{type(exc).__name__}: {exc}')
                continue
            ws.send(msgpack_numpy.packb(result))
    def health(connection, request):
        if request.path == '/healthz':
            return connection.respond(200, json.dumps(policy.metadata))
    with serve(handler, args.host, args.port, compression=None, max_size=32 * 1024 * 1024, process_request=health) as server:
        logging.info('READY ws://%s:%s checkpoint=%s', args.host, args.port, args.checkpoint)
        server.serve_forever()


if __name__ == '__main__':
    main()
