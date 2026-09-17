"""Operator-authorized 0.5 degree right joint6 round-trip, independent of any policy."""
import argparse
import json
import math
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request

import numpy as np
from client import ROOT, SDKRobot, validate_config

SERVICE = 'arx-button-control.service'
OFFSET = math.radians(0.5)


def station():
    with urllib.request.urlopen('http://127.0.0.1:8090/api/status', timeout=3) as r:
        return json.load(r)


def preflight():
    samples = []
    for _ in range(6):
        r = station()
        arm = r['arm']
        if r['recording']['state'] != 'idle':
            raise RuntimeError('Refusing motion while recording is active')
        if not arm.get('online') or arm.get('age_ms', 1000) > 250 or arm['mode'] != 'gravity' or any(arm['faults'].values()):
            raise RuntimeError('Fresh, fault-free gravity mode is required')
        samples.append(arm['positions'])
        time.sleep(0.1)
    if np.max(np.ptp(np.asarray(samples), axis=0)) > 0.01:
        raise RuntimeError('Arms are moving; stabilize before this test')
    return r


def fraction(t):
    if t < 0.5:
        return 0.0
    if t < 1.5:
        return t - 0.5
    if t < 2.0:
        return 1.0
    if t < 3.0:
        return 3.0 - t
    return 0.0


def motion(robot, config, log, now=time.monotonic, sleep=time.sleep):
    initial = robot.read()
    baseline = np.clip(initial, config['safety']['lower'], config['safety']['upper'])
    if np.max(np.abs(baseline - initial)) > 0.003:
        raise RuntimeError('Initial pose is outside configured limits')
    if baseline[12] + OFFSET >= config['safety']['upper'][12]:
        raise RuntimeError('Insufficient right joint6 range')
    records = []
    started = now()
    previous = started
    while True:
        tick = now()
        elapsed = tick - started
        if tick - previous > 0.15:
            raise RuntimeError('Control scheduling stalled')
        previous = tick
        current = robot.read()
        if np.max(np.abs(current - initial)) > math.radians(2):
            raise RuntimeError('Unexpected joint displacement above 2 degrees')
        desired = baseline.copy()
        desired[12] += OFFSET * fraction(elapsed)
        if np.max(np.abs(current - desired)) > math.radians(2):
            raise RuntimeError('Excessive position tracking error')
        target = robot.command(desired)
        record = {'t_s': elapsed, 'state': current.tolist(), 'command': target.tolist()}
        records.append(record)
        log.write(json.dumps(record) + '\n'); log.flush()
        if elapsed >= 3.8:
            break
        sleep(max(0, 1 / 30 - (now() - tick)))
    observed = np.array([r['state'] for r in records])
    times = np.array([r['t_s'] for r in records])
    final = robot.read()
    excursion = float(np.median(observed[(times >= 1.65) & (times < 2.0), 12]) - initial[12])
    returned = float(final[12] - initial[12])
    passed = 0.5 * OFFSET <= excursion <= 1.5 * OFFSET and abs(returned) <= math.radians(0.2)
    result = {'passed': passed, 'commanded_offset_deg': 0.5,
              'measured_excursion_deg': math.degrees(excursion), 'return_error_deg': math.degrees(returned),
              'max_each_joint_displacement_deg': np.degrees(np.max(np.abs(observed - initial), axis=0)).tolist(),
              'samples': len(records), 'initial': initial.tolist(), 'final': final.tolist()}
    if not passed:
        raise RuntimeError('Motion tracking validation failed: ' + json.dumps(result))
    return result


def restore_gravity():
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        arm = station()['arm']
        if arm.get('online') and arm['mode'] == 'idle':
            break
        if arm.get('online') and arm['mode'] == 'gravity' and not any(arm['faults'].values()):
            return
        time.sleep(0.2)
    else:
        raise RuntimeError('Original controller did not become idle')
    frame = struct.Struct('=IB3x8s')
    with socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as bus:
        bus.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, struct.pack('=II', 0x721, 0x7FF))
        bus.settimeout(0.5); bus.bind(('can6',))
        started = last = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                cid, length, data = frame.unpack(bus.recv(frame.size))
            except TimeoutError:
                started = last = None; continue
            now = time.monotonic()
            if cid != 0x721 or length < 4 or data[:4] != bytes(4):
                started = last = None; continue
            if last is None or now - last > 0.8:
                started = now
            last = now
            if now - started >= 0.6:
                break
        else:
            raise RuntimeError('Physical button release heartbeat missing')
        arm = station()['arm']
        if arm['mode'] == 'idle' and arm.get('online'):
            bus.send(frame.pack(0x721, 4, bytes([1, 0, 0, 0, 0, 0, 0, 0])))
        elif arm['mode'] != 'gravity':
            raise RuntimeError('Controller mode changed before restoring gravity')
    stable = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        arm = station()['arm']
        if arm.get('mode') == 'fault' or any(arm.get('faults', {}).values()):
            raise RuntimeError('Fault during gravity restoration')
        if arm.get('online') and arm['mode'] == 'gravity':
            stable = time.monotonic() if stable is None else stable
            if time.monotonic() - stable >= 2:
                return
        else:
            stable = None
        time.sleep(0.2)
    raise RuntimeError('Gravity restoration was not confirmed')


def self_test(config):
    import io
    class Sim:
        def __init__(self):
            self.q = np.array([0., .5, .8, 0, 0, 0, -.05] * 2)
        def read(self): return self.q.copy()
        def command(self, action): self.q = action.copy(); return self.q.copy()
    clock = [0.]
    result = motion(Sim(), config, io.StringIO(), now=lambda: clock[0], sleep=lambda dt: clock.__setitem__(0, clock[0] + dt))
    assert result['passed'] and abs(result['measured_excursion_deg'] - .5) < 1e-6
    class Stuck(Sim):
        def command(self, action): return self.q.copy()
    clock[0] = 0.
    try:
        motion(Stuck(), config, io.StringIO(), now=lambda: clock[0], sleep=lambda dt: clock.__setitem__(0, clock[0] + dt))
    except RuntimeError:
        pass
    else:
        raise AssertionError('Stuck feedback must fail')
    print('Simulation: round trip passes, stuck feedback rejected; no hardware accessed')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute', action='store_true')
    args = p.parse_args()
    config = json.loads((ROOT / 'config.json').read_text()); validate_config(config)
    if not args.execute:
        self_test(config); return
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    before = preflight()
    directory = ROOT / 'logs' / f'micro-motion-{time.time_ns()}'; directory.mkdir()
    (directory / 'station-before.json').write_text(json.dumps(before, ensure_ascii=False, indent=2))
    print('Preflight passed. Recording idle; fixed 0.5 degree right joint6 test.', flush=True)
    report = {'motion_passed': False, 'gravity_restored': False}
    stop_attempted = False
    try:
        stop_attempted = True
        subprocess.run(['systemctl', '--user', 'stop', SERVICE], check=True, timeout=15)
        robot = SDKRobot(config)
        try:
            with (directory / 'trajectory.jsonl').open('w') as log:
                report['motion'] = motion(robot, config, log)
            report['motion_passed'] = True
        finally:
            robot.close()
    except BaseException as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        if stop_attempted:
            try:
                subprocess.run(['systemctl', '--user', 'start', SERVICE], check=True, timeout=15)
                if report['motion_passed']:
                    restore_gravity()
                    report['gravity_restored'] = True
            except BaseException as exc:
                report['restoration_error'] = type(exc).__name__ + ': ' + str(exc)
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        print('REPORT', directory / 'report.json', flush=True)
    if not report['motion_passed'] or not report['gravity_restored']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
