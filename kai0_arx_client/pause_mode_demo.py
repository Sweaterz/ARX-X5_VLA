#!/usr/bin/env python3
"""Official SingleArm API pause comparison. Default: no hardware initialization."""
import argparse,fcntl,json,math,os,select,signal,sys,threading,time
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parent

def feedback(arm):
    if arm.fault is not None:raise RuntimeError(f'SDK fault: {arm.fault}')
    q=np.asarray(arm.get_joint_positions(),dtype=float)
    v=np.asarray(arm.get_joint_velocities(),dtype=float)
    current=np.asarray(arm.get_joint_currents(),dtype=float)
    if q.shape!=(7,) or not all(np.isfinite(x).all() for x in (q,v,current)):raise ValueError('Invalid SDK feedback')
    return q,v,current

def pause(arm,mode):
    q,_,_=feedback(arm)
    if mode=='joint':ok=arm.set_joint_positions(positions=q[:6].tolist(),duration=0)
    elif mode=='gravity':ok=arm.gravity_compensation()
    elif mode=='protect':ok=arm.protect_mode()
    else:raise ValueError('Unknown pause mode')
    if ok is not True:raise RuntimeError(f'SDK rejected {mode}')
    return q

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['joint','gravity','protect'],default='joint')
    p.add_argument('--side',choices=['left','right'],default='right')
    p.add_argument('--enable-motion',action='store_true')
    a=p.parse_args()
    print(f'{a.side}: J6 +/−0.5°, duration=3s; pause at 0.5s -> {a.mode}. No gripper or home command.')
    if not a.enable_motion:
        print('PLAN ONLY. No SDK/hardware initialized. Add --enable-motion for supervised test.');return
    print('先断开 GUI/其他控制器。测试所选单臂；空夹爪，支撑双臂，准备硬件急停。')
    if input('确认后输入 START：').strip()!='START':return
    c=json.loads((ROOT/'config.json').read_text());sys.path.insert(0,c['robot']['sdk_path'])
    from bimanual import SingleArm
    from bimanual.core.model import get_arm_model
    from bimanual.hardware.controller import MODE_NAMES
    runtime=Path(os.environ.get('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}'))
    fd=os.open(runtime/'arx-arm-control.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    lease=os.fdopen(fd,'w');fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
    cfg={'can_port':c['robot'][a.side+'_can'],'type':2} # official model and default tuning
    path=ROOT/'logs'/f'pause-mode-{a.mode}-{time.time_ns()}.jsonl';path.parent.mkdir(exist_ok=True)
    arm=None;finished=threading.Event();tripped=threading.Event();last=[time.monotonic()];watcher=None
    def protect():
        if arm is not None:
            try:
                if arm.protect_mode() is not True:print('SDK protection FAILED; use hardware stop',flush=True)
            except Exception as e:print('SDK protection failed:',e,flush=True)
    def watch():
        while not finished.wait(.05):
            if time.monotonic()-last[0]>2:
                tripped.set();protect();return
    def interrupt(*_):raise KeyboardInterrupt()
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,interrupt)
    with path.open('w') as log:
        def record(**row):log.write(json.dumps(row,default=lambda x:x.tolist() if isinstance(x,np.ndarray) else str(x))+'\n');log.flush()
        try:
            arm=SingleArm(cfg)
            if arm.protect_mode() is not True:raise RuntimeError('Initial protection failed')
            q,_,_=feedback(arm);origin=q.copy();target=q[:6].copy()
            m=get_arm_model(cfg);lo,hi=np.radians(np.array(m.doc_limits_deg)).T
            if np.any(q[:6]<lo-.002) or np.any(q[:6]>hi+.002):raise ValueError('Initial joints outside documented range')
            delta=math.radians(.5);target[5]+=delta if target[5]+delta<=hi[5] else -delta
            if not lo[5]<=target[5]<=hi[5]:raise ValueError('No J6 range available')
            record(event='start',mode=a.mode,side=a.side,config=cfg,urdf=m.urdf_path,modes=MODE_NAMES,origin=origin,target=target)
            last[0]=time.monotonic();watcher=threading.Thread(target=watch,daemon=True);watcher.start()
            if arm.set_joint_positions(positions=target.tolist(),duration=3) is not True:raise RuntimeError('Move rejected')
            start=time.monotonic();paused_at=None;anchor=None;summary_done=False;rows=[]
            print('LOG',path,flush=True)
            while True:
                if tripped.is_set():raise TimeoutError('Feedback watchdog expired')
                q,v,current=feedback(arm);last[0]=time.monotonic();elapsed=last[0]-start
                if np.max(np.abs(q[:6]-origin[:6]))>math.radians(5):raise RuntimeError('Drift exceeded 5 degrees; requesting protection')
                if paused_at is None and elapsed>=.5:
                    anchor=pause(arm,a.mode);paused_at=time.monotonic()
                    record(event='pause',mode=a.mode,position=anchor)
                    print('PAUSED:',a.mode,'观察声音和漂移；不会自动回程。',flush=True)
                # SDK status may include internal mode: preserve it verbatim, don't fabricate labels.
                status=arm.get_status()
                record(t=elapsed,phase='paused' if paused_at else 'moving',position_rad=q,velocity_raw=v,current_raw=current,status=status)
                if paused_at is not None:
                    rows.append((q.copy(),current.copy()))
                    if not summary_done and time.monotonic()-paused_at>=5:
                        drift=np.max(np.abs(np.array([r[0] for r in rows])-anchor),axis=0)
                        rms=np.sqrt(np.mean(np.array([r[1] for r in rows])**2,axis=0))
                        record(event='summary',max_drift_rad=drift,current_rms_raw=rms)
                        print('5s drift rad:',drift.round(5),'SDK current RMS (raw):',rms.round(5),flush=True)
                        print('仍维持所选模式。放稳机械臂后输入 q 退出；Ctrl+C 请求保护。',flush=True)
                        summary_done=True;rows.clear()
                    elif summary_done:rows.clear()
                if select.select([sys.stdin],[],[],0)[0]:
                    line=sys.stdin.readline()
                    if not line or line.strip().lower()=='q':break
                time.sleep(.05)
        except BaseException as e:
            record(event='exit',reason=type(e).__name__+': '+str(e))
            raise
        finally:
            finished.set();protect()
            if watcher:watcher.join(.3)
            if arm:arm.close()
            lease.close()
if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:print('已请求保护并关闭 SDK，不回零。')
    except Exception as e:print('ERROR:',e);raise SystemExit(1)
