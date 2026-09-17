#!/usr/bin/env python3
"""Exercise the client's Ctrl+C hold path without a policy or cameras."""
import argparse
import contextlib
import json
import signal
import sys
import threading
import time

import client
from sdk_sound_test import plan


def record(log, **row):
    log.write(json.dumps(row, ensure_ascii=False) + '\n')
    log.flush()


def check_grippers(q, limits):
    q = client.vector(q)
    for side, index in (('left', 6), ('right', 13)):
        lo, hi = limits
        if not lo <= q[index] <= hi:
            difference = lo - q[index] if q[index] < lo else q[index] - hi
            raise ValueError(f'{side} gripper 反馈/目标 {q[index]:.6f} rad 超出 SDK 行程 '
                             f'[{lo}, {hi}]，超出 {difference:.6f} rad；不下发目标')
    return q


def wait_feedback(robot, limits, log, pause_event, timeout=3, required=10):
    """Bounded startup sampling; does not claim packet freshness unavailable in this API."""
    deadline = time.monotonic() + timeout
    consecutive = 0
    while time.monotonic() < deadline:
        if pause_event.is_set():
            raise RuntimeError('反馈确认阶段取消，尚未发送运动目标')
        q = robot.maintain_hold()  # read + watchdog only; no position command
        try:
            check_grippers(q, limits)
        except ValueError as exc:
            consecutive = 0
            record(log, event='startup_feedback_rejected', error=str(exc), position_rad=q.tolist())
        else:
            consecutive += 1
            record(log, event='startup_feedback', consecutive=consecutive, position_rad=q.tolist())
            if consecutive >= required:
                return
        pause_event.wait(.05)
    raise TimeoutError('启动反馈未通过：3 秒内未取得连续 10 次有效夹爪反馈；不归位、不发送保持目标')


class CheckedRobot:
    """Demo-only gate using the SDK model range, never clamps or changes SDK settings."""
    def __init__(self, robot, limits, log):
        self.robot, self.limits, self.log = robot, limits, log
        self.arms = robot.arms

    def maintain_hold(self):
        return check_grippers(self.robot.maintain_hold(), self.limits)

    def command(self, target):
        self.maintain_hold()
        check_grippers(target, self.limits)
        record(self.log, event='command_request', position_rad=target.tolist())
        return self.robot.command(target)

    def hold_current(self):
        return self.command(self.maintain_hold())


def official_home(robot, log, pause_event):
    for side, arm in zip(('left', 'right'), robot.arms):
        if pause_event.is_set():
            raise RuntimeError('归位阶段取消，转保护退出')
        robot.maintain_hold()
        errors = []
        def home():
            try:
                result = arm.go_home(1, wait=True)
                if result is False:
                    raise RuntimeError('SDK 拒绝归位')
            except BaseException as exc:
                errors.append(exc)
        record(log, event='home_start', side=side)
        worker = threading.Thread(target=home, daemon=True)
        worker.start()
        deadline = time.monotonic() + 20
        while worker.is_alive():
            if pause_event.is_set():
                raise RuntimeError('归位阶段取消，转保护退出；不继续另一臂')
            robot.maintain_hold()
            if time.monotonic() > deadline:
                raise TimeoutError('SDK 归位等待超过 20 秒')
            worker.join(.05)
        if errors:
            raise errors[0]
        if pause_event.is_set():
            raise RuntimeError('归位阶段取消，转保护退出')
        record(log, event='home_complete', side=side)


def exercise(robot, config, log, pause_event, move_j6=False, seconds=10, official=False):
    if official:
        official_home(robot, log, pause_event)
    origin = robot.maintain_hold() if official else robot.hold_current()
    log.write(json.dumps({'event': 'home_feedback' if official else 'initial_hold', 'position_rad': origin.tolist()}) + '\n')
    if move_j6 and not pause_event.is_set():
        target = plan(origin, config, 'right', .5)
        robot.command(target)
        log.write(json.dumps({'event': 'micro_target', 'position_rad': target.tolist()}) + '\n')
    log.flush()
    print('SDK 已接管双臂。按 Ctrl+C 测试暂停；10 秒后也会自动进入相同保持流程。', flush=True)
    end = time.monotonic() + seconds
    while not pause_event.is_set() and time.monotonic() < end:
        if official:
            # Exactly the dual_arm_demo.py targets and public command defaults.
            target = client.vector([.2] * 6 + [-1.] + [.2] * 6 + [-1.])
            robot.command(target)
        q = robot.maintain_hold()
        log.write(json.dumps({'event': 'feedback', 'time': time.time(), 'position_rad': q.tolist()}) + '\n')
        log.flush()
        pause_event.wait(.01 if official else .05)
    log.write(json.dumps({'event': 'operator_pause' if pause_event.is_set() else 'completed'}) + '\n')
    # Exactly the same hold / repeated Ctrl+C / explicit EXIT path as live CLI.
    if official:
        print('官方对照模式：EXIT 会先依次归位双臂，再释放控制；请确认归位路径畅通。', flush=True)
    client.cli_hold_until_exit(robot, log, pause_event)
    if official:
        # Only a normal, explicit EXIT reaches this point. Fault/EOF never homes.
        print('EXIT 已确认：开始左臂、右臂依次归位，完成后保护退出。', flush=True)
        record(log, event='exit_home_start')
        official_home(robot, log, pause_event)
        record(log, event='exit_home_complete')
        print('双臂归位流程完成，即将保护并释放控制。', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-motion', action='store_true')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--move-j6', action='store_true', help='请求右臂 J6 约 0.5° 单程微动，不自动回程')
    modes.add_argument('--official-demo', action='store_true', help='依次归位，再发送官方双臂 0.2 rad / 夹爪 -1 rad 目标')
    args = parser.parse_args()
    print('双臂和夹爪使用 client 原有 JOINT 保持；不连接模型或相机。')
    print('运动计划：' + ('官方对照：双臂依次归位，然后全部关节 0.2 rad、夹爪 -1 rad，约 100Hz 重发。'
                        if args.official_demo else '右臂 J6 约 0.5°，SDK 默认轨迹参数。'
                        if args.move_j6 else '只以当前反馈为目标，不添加位移。'))
    if not args.enable_motion:
        print('仅显示计划，未初始化 SDK。实际测试需要 --enable-motion。')
        return
    print('先在 GUI 断开控制并放稳双臂，清空夹爪和运动区域。初始化和最终退出会调用保护。')
    print('Ctrl+C 保持进程；' + ('EXIT 回车先依次归位双臂，再保护退出。' if args.official_demo
                               else 'EXIT 回车释放控制，届时双臂可能失去支撑。'))
    print('初始化/反馈确认/归位期间 Ctrl+C 取消并保护退出；进入目标运行阶段后 Ctrl+C 才是保持。')
    if input('现场准备好后输入 START：').strip() != 'START':
        return
    config = json.loads((client.ROOT / 'config.json').read_text())
    client.validate_config(config)
    sys.path.insert(0, config['robot']['sdk_path'])
    from bimanual.core.model import get_arm_model
    model = get_arm_model({'type': 2})
    if args.official_demo:
        config['robot']['urdf_path'] = model.urdf_path
    pause_event = threading.Event()
    path = client.ROOT / 'logs' / f'client-hold-{time.time_ns()}.jsonl'
    path.parent.mkdir(exist_ok=True)
    print('LOG', path, flush=True)
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    with contextlib.ExitStack() as stack:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old = signal.signal(sig, terminate)
            stack.callback(signal.signal, sig, old)
        log = stack.enter_context(path.open('w'))
        robot = client.SDKRobot(config)
        stack.callback(robot.close)
        old = signal.signal(signal.SIGINT, lambda *_: pause_event.set())
        stack.callback(signal.signal, signal.SIGINT, old)
        try:
            record(log, event='sdk_model', gripper_range=list(model.gripper_range),
                   urdf=config['robot'].get('urdf_path'), official=args.official_demo)
            wait_feedback(robot, model.gripper_range, log, pause_event)
            checked = CheckedRobot(robot, model.gripper_range, log)
            exercise(checked, config, log, pause_event, args.move_j6, official=args.official_demo)
        except BaseException as exc:
            log.write(json.dumps({'event': 'aborted', 'error': repr(exc)}) + '\n')
            log.flush()
            raise


if __name__ == '__main__':
    main()
