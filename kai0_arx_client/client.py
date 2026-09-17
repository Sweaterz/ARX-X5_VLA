#!/usr/bin/env python3
"""Kai0/OpenPI client for the installed ARX X5-2025 SDK. No motion by default."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib
import json
import math
import os
from pathlib import Path
import signal
import select
import socket
import sys
import threading
import time

import numpy as np
from control_runtime import IOLanes, background_close
from vendor import image_tools, msgpack_numpy

ROOT = Path(__file__).resolve().parent
ROLES = ('head', 'left', 'right')
KEYS = {'head': 'top_head', 'left': 'hand_left', 'right': 'hand_right'}


def vector(value, name='state'):
    out = np.asarray(value, dtype=np.float64)
    if out.shape != (14,) or not np.isfinite(out).all():
        raise ValueError(f'{name} must be finite [14], got {out.shape}')
    return out


def validate_config(c):
    if c['robot']['model'] != 'arx5_2025' or c['robot']['units'] != 'sdk_radians':
        raise ValueError('This adapter requires ARX X5-2025 raw SDK radians, including grippers')
    if c['robot']['left_can'] == c['robot']['right_can']:
        raise ValueError('Left and right CAN ports must differ')
    lo, hi = vector(c['safety']['lower']), vector(c['safety']['upper'])
    if np.any(lo >= hi):
        raise ValueError('Invalid position limits')
    for key in ('watchdog_s', 'max_observation_age_s'):
        x = c['safety'][key]
        if not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0:
            raise ValueError(f'Invalid safety.{key}')
    if c['safety']['max_observation_age_s'] > c['safety']['watchdog_s']:
        raise ValueError('Observation age limit must not exceed watchdog')
    if not 0 < c['control']['fps'] <= 30 or c['control']['actions_per_chunk'] < 1:
        raise ValueError('Invalid control rate/chunk length')
    if len(set(c['cameras']['serials'].values())) != 3 or set(c['cameras']['serials']) != set(ROLES):
        raise ValueError('Three distinct head/left/right serials are required')
    if c['server']['timeout_s'] <= 0:
        raise ValueError('Server timeout must be positive')


class LimitViolation(ValueError):
    def __init__(self, kind, values, lo, hi):
        names = [f'{side}_{joint}' for side in ('left', 'right')
                 for joint in ('joint_1', 'joint_2', 'joint_3', 'joint_4', 'joint_5', 'joint_6', 'gripper')]
        self.details = [dict(dimension=names[i], index=int(i), value_rad=float(values[i]),
                             lower_rad=float(lo[i]), upper_rad=float(hi[i]),
                             excess_rad=float(max(lo[i]-values[i], values[i]-hi[i])))
                        for i in np.flatnonzero((values < lo) | (values > hi))]
        super().__init__(kind + ': ' + json.dumps(self.details, ensure_ascii=False))


def diagnostic_violations(action, config):
    """Compare raw predictions with the reference table, without rejecting or clipping."""
    action = vector(action, 'action')
    return LimitViolation('Reference limits', action, np.asarray(config['safety']['lower']),
                          np.asarray(config['safety']['upper'])).details


def bounded_target(action, state, config):
    """Compatibility for diagnostic scripts: validate shape/finiteness only.

    Returned value is a RAW SDK request, not the SDK-clipped or achieved target.
    """
    vector(state)
    return vector(action, 'action').copy()


def make_observation(state, images, prompt):
    if not prompt.strip():
        raise ValueError('A non-empty task prompt is required')
    result = {}
    for role in ROLES:
        rgb = np.asarray(images[role])
        if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
            raise ValueError(f'{role} must be HWC uint8 RGB')
        result[KEYS[role]] = np.ascontiguousarray(image_tools.resize_with_pad(rgb, 224, 224).transpose(2, 0, 1))
    return {'state': vector(state).astype(np.float32), 'images': result, 'prompt': prompt}


class Policy:
    def __init__(self, url, timeout):
        from websockets.sync.client import connect
        self.timeout = timeout
        self.ws = connect(url, compression=None, max_size=32 * 1024 * 1024,
                          proxy=None, open_timeout=timeout, close_timeout=1)
        try:
            self.metadata = self.receive()
        except BaseException:
            self.close()
            raise

    def receive(self):
        raw = self.ws.recv(timeout=self.timeout)
        if isinstance(raw, str):
            raise RuntimeError(f'Policy server error: {raw[:2000]}')
        data = msgpack_numpy.unpackb(raw)
        if not isinstance(data, dict):
            raise ValueError('Server response must be a map')
        return data

    def infer(self, observation):
        self.ws.send(msgpack_numpy.packb(observation))
        result = self.receive()
        actions = np.asarray(result.get('actions'), dtype=np.float64)
        if actions.ndim != 2 or actions.shape[1] != 14 or not 1 <= len(actions) <= 1000 or not np.isfinite(actions).all():
            raise ValueError(f'Expected finite actions [T,14], got {actions.shape}')
        return actions

    def close(self):
        self.ws.close()


class MockRobot:
    def __init__(self):
        self.q = np.array([0, 0.5, 0.8, 0, 0, 0, -0.5] * 2, dtype=float)

    def read(self):
        return self.q.copy()

    def close(self):
        pass


class UDPRobot:
    """Read an already running arx_button_control broadcaster, never start it."""
    def __init__(self, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(('127.0.0.1', port))
            self.sock.settimeout(1)
        except BaseException:
            self.close()
            raise

    def read(self):
        data = json.loads(self.sock.recv(65535))
        # Producer and consumer are on this host, sharing CLOCK_MONOTONIC.
        age = (time.monotonic_ns() - data['timestamp_ns']) / 1e9
        if not 0 <= age < 0.25 or any(v is not None for v in data.get('faults', {}).values()):
            raise RuntimeError('Stale or faulted UDP arm state')
        return vector(data['positions'])

    def close(self):
        self.sock.close()


class SDKRobot:
    """Construction activates vendor hardware threads; only used with --enable-motion."""
    def __init__(self, config, factory=None):
        self.config = config
        self.arms = []
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.tripped = threading.Event()
        self.watchdog = None
        self.sdk_motion_limits = []
        self.lease = None
        self.deadline = time.monotonic() + config['safety']['watchdog_s']
        try:
            runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
            if not runtime.is_dir():
                runtime = Path('/tmp')
            fd = os.open(runtime / 'arx-arm-control.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self.lease = os.fdopen(fd, 'w')
            try:
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another ARX arm controller owns arx-arm-control.lock") from exc
            if factory is None:
                sys.path.insert(0, config['robot']['sdk_path'])
                from bimanual import SingleArm
                factory = SingleArm
            payload = config['robot'].get('urdf_path')
            if payload and not Path(payload).is_file():
                raise FileNotFoundError(f'Calibrated payload URDF missing: {payload}')
            for role in ('left', 'right'):
                arm_config = {'can_port': config['robot'][f'{role}_can'], 'type': 2}
                if payload:
                    arm_config['urdf_path'] = payload
                arm = factory(arm_config)
                self.arms.append(arm)
                if arm.protect_mode() is not True:
                    raise RuntimeError(f'{role} protect_mode failed')
                limits = arm.get_motion_limits()
                if not isinstance(limits, dict):
                    raise RuntimeError('SDK trajectory configuration/query failed')
                self.sdk_motion_limits.append({'side': role, 'smoothness': 'SDK defaults (not overridden)', 'limits': limits})
            self.read()
            self.deadline = time.monotonic() + config['safety']['watchdog_s']
            self.watchdog = threading.Thread(target=self._watch, daemon=True)
            self.watchdog.start()
        except BaseException:
            self.close()
            raise

    def _watch(self):
        while not self.stop.wait(0.02):
            if time.monotonic() > self.deadline:
                self.tripped.set()
                with self.lock:
                    self._protect()
                return

    def _protect(self):
        for arm in reversed(self.arms):
            try:
                if arm.protect_mode() is not True:
                    print('ERROR: SDK protect_mode returned failure', file=sys.stderr, flush=True)
            except Exception as exc:
                print(f'ERROR: SDK protection failed: {exc}', file=sys.stderr, flush=True)

    def read(self):
        with self.lock:
            if self.tripped.is_set():
                raise TimeoutError('Motion watchdog expired; restart explicitly')
            values = []
            for arm in self.arms:
                if arm.fault is not None:
                    raise RuntimeError(f'ARX SDK fault: {arm.fault}')
                q = np.asarray(arm.get_joint_positions())
                if q.shape != (7,):
                    raise ValueError(f'Expected SDK feedback [7], got {q.shape}')
                values.extend(q)
            return vector(values)

    def command(self, action):
        with self.lock:
            if self.tripped.is_set() or time.monotonic() > self.deadline:
                raise TimeoutError('Motion watchdog expired')
            try:
                self.read()  # fault and finite-feedback checks remain mandatory
                target = vector(action, 'action').copy()
                for i, arm in enumerate(self.arms):
                    block = target[i * 7:(i + 1) * 7]
                    if arm.set_joint_positions(positions=block[:6].tolist(), duration=0) is not True:
                        raise RuntimeError('SDK rejected joint command')
                    if arm.set_gripper_pos(float(block[6])) is not True:
                        raise RuntimeError('SDK rejected gripper command')
                self.deadline = time.monotonic() + self.config['safety']['watchdog_s']
                return target
            except BaseException:
                self.tripped.set()
                self._protect()
                raise

    def hold_current(self):
        """Replace the last policy target with measured pose; keep SDK position mode alive."""
        with self.lock:
            return self.command(self.read())

    def maintain_hold(self):
        with self.lock:
            if self.tripped.is_set() or time.monotonic() > self.deadline:
                raise TimeoutError('Position hold watchdog expired')
            q = self.read()
            self.deadline = time.monotonic() + self.config['safety']['watchdog_s']
            return q

    def close(self):
        self.stop.set()
        if self.watchdog:
            self.watchdog.join(timeout=1)
        with self.lock:
            self._protect()
            for arm in reversed(self.arms):
                try:
                    arm.close()
                except Exception as exc:
                    print(f'ERROR: SDK close failed: {exc}', file=sys.stderr)
            self.arms.clear()
        if self.lease:
            self.lease.close()
            self.lease = None


class Cameras:
    def __init__(self, config, real=False):
        self.lock = threading.RLock()
        self.real = real
        self.pipelines = {}
        self.frame_meta = {}; self.clock_offsets = {}
        if not real:
            return
        import pyrealsense2 as rs
        try:
            for role, serial in config['cameras']['serials'].items():
                pipe, cfg = rs.pipeline(), rs.config()
                cfg.enable_device(serial)
                cfg.enable_stream(rs.stream.color, 640, 480, rs.format.rgb8, 30)
                try:
                    pipe.start(cfg)
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        pipe.stop()
                    raise RuntimeError(f"Camera {role} ({serial}) cannot start; check device ownership: {exc}") from exc
                self.pipelines[role] = pipe
            for _ in range(5):
                self.read()
        except BaseException:
            self.close()
            raise

    def read(self):
        with self.lock:
            return self._read()

    def _read(self):
        if not self.real:
            return {r: np.full((480, 640, 3), 35 + i * 55, dtype=np.uint8) for i, r in enumerate(ROLES)}
        images = {}
        for role, pipe in self.pipelines.items():
            frame = pipe.wait_for_frames(1000).get_color_frame()
            if not frame:
                raise RuntimeError(f'Missing {role} RGB frame')
            received=time.monotonic(); device_s=frame.get_timestamp()/1000
            offset=received-device_s
            self.clock_offsets[role]=min(offset,self.clock_offsets.get(role,offset))
            self.frame_meta[role]={'received':received,'captured':device_s+self.clock_offsets[role],
                                   'device_ms':device_s*1000,'sequence':frame.get_frame_number()}
            images[role] = np.asanyarray(frame.get_data()).copy()
        return images

    def close(self):
        with self.lock:
            for pipe in self.pipelines.values():
                try:
                    pipe.stop()
                except Exception as exc:
                    print(f'Camera stop failed: {exc}', file=sys.stderr)
            self.pipelines.clear()


def doctor(c):
    for name in ('numpy', 'PIL', 'websockets', 'msgpack', 'pyrealsense2', 'pinocchio'):
        module = importlib.import_module(name)
        print(name, getattr(module, '__version__', 'OK'))
    sys.path.insert(0, c['robot']['sdk_path'])
    from bimanual import SingleArm  # Import only. Never construct here.
    print('SDK import OK; class', SingleArm.__name__)
    payload = c['robot'].get('urdf_path')
    if payload:
        if not Path(payload).is_file():
            raise FileNotFoundError(f'Calibrated payload URDF missing: {payload}')
        print('Calibrated payload URDF:', payload)
    for role in ('left', 'right'):
        port = c['robot'][f'{role}_can']
        if not Path('/sys/class/net', port).exists():
            raise RuntimeError(f'Missing CAN interface: {port}')
        print(role, port, 'present')
    import pyrealsense2 as rs
    serials = {d.get_info(rs.camera_info.serial_number) for d in rs.context().query_devices()}
    for role, serial in c['cameras']['serials'].items():
        if serial not in serials:
            raise RuntimeError(f'Missing camera: {role} ({serial})')
        print(role, serial, 'present')
    print('Doctor OK: no arm initialized, no CAN frames sent by doctor')


class CLIPauseRequested(Exception):
    pass


def check_cli_pause(event):
    if event.is_set():
        raise CLIPauseRequested()


def safe_print(message):
    try: print(message, flush=True)
    except OSError: pass


def write_event(log, **event):
    try:
        log.write(json.dumps(event, ensure_ascii=False)+'\n'); log.flush()
    except OSError:
        pass


def cli_hold_until_exit(robot, log, pause_event, stream=None, control=None):
    """Keep the SDK alive; no inference, no accumulated policy targets, no auto resume."""
    stream = sys.stdin if stream is None else stream
    held = robot.hold_current()
    write_event(log, event='holding', position_rad=held.tolist())
    if control: control.holding = True
    pause_event.clear()
    safe_print('JOINT 位置保持中。放稳/支撑双臂后输入 EXIT 并回车，才释放控制。Ctrl+C 不退出保持。')
    pending = b''
    while True:
        if control and control.stop.is_set(): raise InterruptedError('软件紧急停止')
        robot.maintain_hold()  # faults/watchdog still propagate to SDK cleanup
        if control and control.release.is_set():
            write_event(log, event='operator_exit', release_control=True, via='local_socket')
            return
        if pause_event.is_set():
            pause_event.clear()
            safe_print('仍在位置保持；放稳双臂后输入 EXIT 退出。')
        if stream is None:
            pause_event.wait(.05)
            continue
        try:
            readable=select.select([stream], [], [], .05)[0]
            data=os.read(stream.fileno(),256) if readable else None
        except (OSError, ValueError):
            if not control: raise
            stream=None; continue
        if data is not None:
            if not data:
                if not control: raise RuntimeError('Terminal input closed; requesting SDK protection')
                stream=None
                write_event(log,event='terminal_closed_holding',control_socket=str(control.path))
                continue
            pending += data
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                if line.strip() == b'EXIT':
                    write_event(log,event='operator_exit',release_control=True)
                    return
                safe_print('输入 EXIT 才释放控制。')
            if len(pending) > 4096:
                pending = b''


def run(args, c):
    if args.enable_motion and (args.robot != 'sdk' or args.cameras != 'realsense'):
        raise ValueError('--enable-motion requires --robot sdk --cameras realsense')
    if args.robot == 'sdk' and not args.enable_motion:
        raise ValueError('SDK initialization can activate hardware; use mock or udp for dry-run')
    if args.steps < 0:
        raise ValueError('--steps must be non-negative; 0 means continuous')
    prompt = args.prompt or c['control']['prompt']
    if not prompt.strip():
        raise ValueError('Set --prompt to the task used for your model')
    url = args.server or c['server']['url']
    logdir = ROOT / 'logs'
    logdir.mkdir(exist_ok=True)
    path = logdir / f'run-{time.time_ns()}.jsonl'
    with contextlib.ExitStack() as stack:
        policy = Policy(url, c['server']['timeout_s'])
        stack.callback(policy.close)
        print('Server metadata:', policy.metadata, flush=True)
        if args.enable_motion and policy.metadata.get('test_only'):
            raise ValueError('Test policies cannot be used with physical motion')
        cams = Cameras(c, args.cameras == 'realsense')
        stack.callback(background_close, cams)
        pause_event = threading.Event()
        if args.enable_motion:
            for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
                old = signal.signal(sig, lambda *_: pause_event.set())
                stack.callback(signal.signal, sig, old)
        robot = SDKRobot(c) if args.robot == 'sdk' else UDPRobot(args.udp_port) if args.robot == 'udp' else MockRobot()
        stack.callback(robot.close)
        log = stack.enter_context(path.open('w'))
        if args.robot == 'sdk':
            log.write(json.dumps({'event': 'sdk_configuration', 'data': robot.sdk_motion_limits}, default=str) + '\n')
            log.flush()
        print(f'Client ready: motion={args.enable_motion}, robot={args.robot}, cameras={args.cameras}, log={path}', flush=True)
        control = None
        io = IOLanes()
        if args.enable_motion:
            from client_control import Control
            control = Control(pause_event)
            stack.callback(control.close)
            safe_print(f'本机控制入口：{control.path}；关闭终端后可用 client_control.py 管理保持。')
        def tick():
            if control and control.stop.is_set(): raise InterruptedError('软件紧急停止')
            check_cli_pause(pause_event)
            if args.enable_motion: robot.maintain_hold()
        try:
            step = 0
            while args.steps == 0 or step < args.steps:
                tick()
                observed = time.monotonic()
                images=io.call('camera',cams.read,tick,c['safety']['max_observation_age_s'])
                obs = make_observation(robot.read(), images, prompt)
                actions = io.call('policy',lambda:policy.infer(obs),tick,
                                  max(.001,c['safety']['max_observation_age_s']-(time.monotonic()-observed)))
                tick()
                log.write(json.dumps({'event': 'raw_prediction', 'state': obs['state'].tolist(),
                                      'actions': actions.tolist(), 'motion': args.enable_motion,
                                      'reference_violations': [dict(horizon=i, **v) for i, a in enumerate(actions)
                                                               for v in diagnostic_violations(a, c)]}) + '\n')
                log.flush()
                elapsed = time.monotonic() - observed
                if elapsed > c['safety']['max_observation_age_s']:
                    raise TimeoutError(f'Observation/action round trip too old: {elapsed:.3f}s')
                # Plain synchronous action chunking. No RTC fields or stale asynchronous queue.
                count = min(len(actions), c['control']['actions_per_chunk'])
                if args.steps:
                    count = min(count, args.steps - step)
                for action in actions[:count]:
                    started = time.monotonic()
                    if started - observed > c['safety']['max_observation_age_s']:
                        raise TimeoutError('Remaining action chunk expired')
                    tick()
                    current = robot.read()
                    tick()
                    try:
                        target = robot.command(action) if args.enable_motion else bounded_target(action, current, c)
                    except Exception as exc:
                        log.write(json.dumps({'event': 'command_rejected', 'step': step,
                                              'state': current.tolist(), 'action': action.tolist(),
                                              'error': str(exc), 'violations': getattr(exc, 'details', [])}) + '\n')
                        log.flush()
                        raise
                    log.write(json.dumps({'step': step, 'motion': args.enable_motion, 'robot': args.robot,
                                          'state': current.tolist(), 'action': action.tolist(), 'sdk_request': target.tolist(),
                                          'reference_violations': diagnostic_violations(action, c),
                                          'observation_age_s': started - observed, 'round_trip_s': elapsed}) + '\n')
                    log.flush()
                    if isinstance(robot, MockRobot):
                        robot.q = target.copy()
                    step += 1
                    time.sleep(max(0, 1 / c['control']['fps'] - (time.monotonic() - started)))
                print(f'steps={step} chunk={count} round_trip={elapsed:.3f}s motion={args.enable_motion}', flush=True)
        except CLIPauseRequested:
            write_event(log,event='operator_pause')
        except Exception as exc:
            if not args.enable_motion or (control and control.stop.is_set()): raise
            robot.maintain_hold()
            write_event(log,event='upper_layer_error_holding',error=str(exc))
            safe_print(f'任务停止，机械臂反馈正常，转位置保持：{exc}')
        if args.enable_motion:
            cli_hold_until_exit(robot, log, pause_event, control=control)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=ROOT / 'config.json')
    p.add_argument('command', choices=('doctor', 'camera-check', 'run'))
    p.add_argument('--server', help='Full ws://GPU_IP:8000 URL')
    p.add_argument('--prompt')
    p.add_argument('--robot', choices=('mock', 'udp', 'sdk'), default='mock')
    p.add_argument('--cameras', choices=('mock', 'realsense'), default='mock')
    p.add_argument('--enable-motion', action='store_true')
    p.add_argument('--udp-port', type=int, default=8765)
    p.add_argument('--steps', type=int, default=30)
    args = p.parse_args()
    c = json.loads(args.config.read_text())
    validate_config(c)
    def stop(_sig, _frame):
        if _sig == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + _sig)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    if args.command == 'doctor':
        doctor(c)
    elif args.command == 'camera-check':
        from PIL import Image
        with contextlib.closing(Cameras(c, True)) as cams:
            directory = ROOT / 'logs' / f'cameras-{time.time_ns()}'
            directory.mkdir(parents=True)
            for role, rgb in cams.read().items():
                Image.fromarray(rgb).save(directory / f'{role}.jpg')
                print(role, rgb.shape, 'RGB', directory / f'{role}.jpg')
    else:
        run(args, c)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped; hardware backend cleanup requested protection.', flush=True)
        sys.exit(130)
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
