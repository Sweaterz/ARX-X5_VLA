"""Recorded-feedback replay. All motion remains on the existing Manager control thread."""
import json,subprocess,time,hashlib
from pathlib import Path
import numpy as np
from dagger_data import ROOT,DATA_PYTHON,worker_env
from dagger_segments import inventory

class Replay:
    def __init__(self):
        self.phase='empty';self.episode=None;self.q=None;self.times=None;self.parts=[];self.index=0;self.part=0;self.speed=.5;self.error='';self.root=None;self.meta={}
    def snapshot(self):
        return {'phase':self.phase,'episode':self.episode,'parts':self.parts,'part':self.part,'frame':self.index,
                'total':len(self.q) if self.q is not None else 0,'speed':self.speed,'error':self.error,
                'target':self.q[min(self.index,len(self.q)-1)].tolist() if self.q is not None else None,
                'prompt':self.meta.get('prompt'),'source_root':str(self.root) if self.root else None}
    def load(self,m,episode):
        self.phase='loading';self.error='';self.q=None;self.episode=None;self.parts=[];self.times=None;self.meta={};self.index=0
        root=m.dagger.root
        def read():
            p=subprocess.run([DATA_PYTHON,str(ROOT/'dagger_worker.py'),'replay',str(root),episode],env=worker_env(),capture_output=True,text=True,timeout=18)
            result=json.loads(p.stdout.strip().splitlines()[-1])
            if p.returncode:raise ValueError(result.get('error','轨迹读取失败'))
            return result
        data=m.io.call('replay_load',read,m.tick,20)
        self.install(m,data,root,episode)
    def install(self,m,data,root,episode):
        q=np.asarray(data['qpos'],dtype=float);t=np.asarray(data['timestamp'],dtype=float);meta=data['metadata']
        if q.ndim!=2 or q.shape[1]!=14 or len(q)<2 or t.ndim!=1 or len(t)!=len(q) or not np.isfinite(q).all() or not np.isfinite(t).all() or np.any(np.diff(t)<=0):raise ValueError('回放轨迹维度、反馈或时间索引无效')
        if not m.demo:
            if meta.get('demo'):raise ValueError('演示轨迹不能用于真机回放')
            expected=[f'{s}_{j}' for s in ('left','right') for j in ('joint_1','joint_2','joint_3','joint_4','joint_5','joint_6','gripper')]
            if meta.get('action_names')!=expected:raise ValueError('记录关节顺序不匹配')
            for k in ('model','units','left_can','right_can'):
                if meta.get('robot_config',{}).get(k)!=m.config['robot'].get(k):raise ValueError('记录的机械臂配置不匹配：'+k)
            urdf=Path(m.config['robot']['urdf_path'])
            if not urdf.is_file() or meta.get('urdf_sha256')!=hashlib.sha256(urdf.read_bytes()).hexdigest():raise ValueError('记录 URDF 与当前配置不匹配')
        labels=np.asarray(data['intervention']);segments=np.asarray(data['segment'])
        if labels.shape!=t.shape or segments.shape!=t.shape or not np.isin(labels,[0,1]).all() or not np.isfinite(segments).all():raise ValueError('回放片段标签无效')
        self.parts=inventory(t,labels,segments)
        if not self.parts:raise ValueError('没有可回放片段')
        self.q=q;self.times=t;self.meta=meta;self.root=Path(root);self.episode=episode;self.part=0;self.index=0;self.phase='ready';self.error=''
    def select(self,index):
        if type(index) is not int or not 0<=index<len(self.parts):raise ValueError('无效回放片段')
        self.part=index;self.index=self.parts[index]['start_frame'];self.phase='ready';self.error=''
    def close_enough(self,current,target=None):
        target=self.q[min(self.index,len(self.q)-1)] if target is None else target
        delta=np.abs(np.asarray(current)-target)
        joints=[0,1,2,3,4,5,7,8,9,10,11,12]
        return bool(np.max(delta[joints])<=.05 and np.max(delta[[6,13]])<=.15)
    def send(self,m,target):
        with m.lock:
            m.check_stop()
            if m.demo:m.robot.q=target.copy()
            else:m.robot.command(target)
    def align(self,m):
        self.index=min(self.index,self.parts[self.part]['end_frame'])
        self.phase='aligning';self.error='';m.mode='running';m.hold_target=None;m.hold_started_at=None
        m.event('Replay 前往起点',episode=self.episode,frame=self.index,target=self.q[self.index].tolist())
        self.send(m,self.q[self.index]);self.finish_target(m,self.q[self.index],20)
        self.phase='ready'
    def finish_target(self,m,target,timeout=5):
        # Keep the SDK's original endpoint, never chase feedback after completion.
        deadline=time.monotonic()+timeout
        while True:
            m.tick()
            if self.close_enough(m.state,target):break
            if time.monotonic()>deadline:raise TimeoutError('Replay 目标未到达，停止并保持；请检查障碍或夹爪状态')
            time.sleep(.01)
        with m.lock:
            m.check_stop()
            m.hold_target=target.tolist();m.hold_started_at=time.monotonic();m.hold_source='replay_target';m.mode='holding'
            m.event('Replay 到达目标，保留 SDK 目标并保持',target_rad=m.hold_target)
    def run(self,m,speed):
        if type(speed) not in (int,float) or speed not in (.25,.5,1):raise ValueError('回放速度仅支持 0.25、0.5、1 倍')
        end=self.parts[self.part]['end_frame']
        self.index=min(self.index,end)
        m.tick()
        if not self.close_enough(m.state):raise ValueError('当前位置与回放起点不一致，请先前往起点')
        self.speed=speed;self.phase='playing';self.error='';m.mode='running';m.hold_target=None;m.hold_started_at=None
        start=self.index;end=self.parts[self.part]['end_frame'];began=time.monotonic();last_preview=began
        m.event('Replay 开始',episode=self.episode,part=self.part,speed=speed,frame=start)
        while self.index<=end:
            deadline=began+(self.times[self.index]-self.times[start])/speed
            while time.monotonic()<deadline:
                m.tick();time.sleep(min(.005,max(0,deadline-time.monotonic())))
            m.tick()
            if time.monotonic()-deadline>.2:raise TimeoutError('Replay 调度延迟超过 200ms，暂停保持，禁止补发动作')
            self.send(m,self.q[self.index]);self.index+=1
            if m.cams and time.monotonic()-last_preview>.25:m.read_images();last_preview=time.monotonic()
        self.finish_target(m,self.q[end])
        if self.part+1<len(self.parts):
            self.part+=1;self.index=self.parts[self.part]['start_frame'];self.phase='boundary'
        else:self.index=end;self.phase='complete'
        m.event('Replay 已停止并保持',phase=self.phase,frame=self.index)
