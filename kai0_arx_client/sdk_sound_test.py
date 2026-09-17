#!/usr/bin/env python3
"""One SDK trajectory per leg. No policy, camera, home, or gripper commands."""
import argparse, contextlib, json, math, signal, time
from pathlib import Path
import numpy as np
import client

def plan(q, c, side, degrees):
    q = client.vector(q)
    offset = 0 if side == 'left' else 7
    lo=np.array(c['safety']['lower']); hi=np.array(c['safety']['upper'])
    # Require all six commanded joints strictly within the software envelope.
    if np.any(q[offset:offset+6]<lo[offset:offset+6]) or np.any(q[offset:offset+6]>hi[offset:offset+6]):
        raise ValueError('所选臂当前关节超出软件范围；拒绝自动调整其他关节')
    target=q.copy(); i=offset+5; delta=math.radians(degrees)
    target[i]+=delta if q[i]+delta <= hi[i] else -delta
    if not lo[i] <= target[i] <= hi[i]: raise ValueError('J6 没有足够限位余量')
    return target

def leg(robot, c, side, target, origin, duration, log, phase):
    offset=0 if side=='left' else 7
    arm=robot.arms[offset//7]
    with robot.lock:
        robot.read()
        if time.monotonic()>robot.deadline: raise TimeoutError('Watchdog expired')
        if arm.set_joint_positions(positions=target[offset:offset+6].tolist(),duration=duration) is not True:
            raise RuntimeError('SDK 拒绝轨迹')
    started=time.monotonic(); samples=[]
    while time.monotonic()-started < duration+2:
        with robot.lock:
            q=robot.read()
            if time.monotonic()>robot.deadline: raise TimeoutError('Watchdog expired')
            # Planned trajectory is active: keepalive only following successful feedback.
            robot.deadline=time.monotonic()+c['safety']['watchdog_s']
            v=np.asarray(arm.get_joint_velocities(),dtype=float)
            current=np.asarray(arm.get_joint_currents(),dtype=float)
            if not np.isfinite(v).all() or not np.isfinite(current).all():raise ValueError('非有限 SDK 反馈')
            displacement=np.abs(q-origin)
            allowed=np.full(14,math.radians(1));allowed[[6,13]]=.05
            if np.any(displacement>allowed):raise RuntimeError('反馈偏离起始姿态过大，停止测试')
            row=dict(phase=phase,t=round(time.monotonic()-started,4),position_rad=q.tolist(),
                     sdk_velocity=v.tolist(),sdk_current=current.tolist())
            log.write(json.dumps(row)+'\n');log.flush();samples.append(q)
        time.sleep(.05)
    error=float(np.max(np.abs(samples[-1][offset:offset+6]-target[offset:offset+6])))
    if error>math.radians(.2):raise RuntimeError(f'目标跟踪未通过，最大误差 {math.degrees(error):.3f}°；不自动继续')
    return samples

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--side',choices=['left','right'],default='right')
    p.add_argument('--degrees',type=float,default=.5)
    p.add_argument('--duration',type=float,default=3)
    p.add_argument('--enable-motion',action='store_true')
    a=p.parse_args()
    if not math.isfinite(a.degrees) or not 0<a.degrees<=.5: p.error('degrees must be 0..0.5')
    if not math.isfinite(a.duration) or not 3<=a.duration<=10:p.error('duration must be 3..10 seconds')
    c=json.loads((client.ROOT/'config.json').read_text());client.validate_config(c)
    print(f'{a.side} J6 往返 {a.degrees}°，每程请求 {a.duration}s；无模型、无归位、无夹爪命令。')
    if not a.enable_motion:
        print('仅显示计划；未初始化 SDK。实际运行需 --enable-motion。');return
    print('请先在 GUI 断开机械臂；双臂须有支撑，确认 J6 小幅转动空间并准备硬件急停。')
    def stop(*_):raise KeyboardInterrupt()
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,stop)
    path=client.ROOT/'logs'/f'sdk-sound-{time.time_ns()}.jsonl';path.parent.mkdir(exist_ok=True)
    print('LOG',path,flush=True)
    with path.open('w') as log,contextlib.closing(client.SDKRobot(c)) as robot:
        origin=robot.read();target=plan(origin,c,a.side,a.degrees)
        log.write(json.dumps(dict(event='plan',side=a.side,origin=origin.tolist(),target=target.tolist(),duration=a.duration))+'\n');log.flush()
        try:
            outbound=leg(robot,c,a.side,target,origin,a.duration,log,'outbound')
            i=(0 if a.side=='left' else 7)+5
            observed=max(abs(float(q[i]-origin[i])) for q in outbound)
            if observed<math.radians(a.degrees)*.5:raise RuntimeError('实际位移不足请求的一半，不能认定运动有效')
            leg(robot,c,a.side,origin,origin,a.duration,log,'return')
            print(f'往返完成，J6 实测最大位移 {math.degrees(observed):.3f}°。声音需现场对比，脚本不判断正常噪声。')
            log.write(json.dumps(dict(event='completed',observed_degrees=math.degrees(observed)))+'\n')
        except BaseException as e:
            log.write(json.dumps(dict(event='aborted',error=type(e).__name__+': '+str(e)))+'\n');log.flush();raise
if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:print('已请求保护；不会自动回程。')
    except Exception as e:print('ERROR:',e);raise SystemExit(1)
