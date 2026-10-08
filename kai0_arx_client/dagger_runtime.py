"""DAgger session state; robot ownership stays exclusively with GUI Manager."""
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import tempfile
import numpy as np
from dagger_data import DATA_ROOT,DATA_PYTHON,ROOT,Recorder,ROLES,catalog,episode_path,worker_env


class Dagger:
    def __init__(self,root=None):
        self.settings_file=ROOT/'dagger_settings.json' if root is None and not os.environ.get('ARX_DAGGER_DATA_ROOT') else None
        chosen=root or DATA_ROOT
        if self.settings_file and self.settings_file.is_file():
            chosen=json.loads(self.settings_file.read_text())['data_root']
        self.root=Path(chosen).expanduser().resolve();self.recorder=None;self.phase='idle';self.error='';self.segment=0
        self.generation=0;self.pending=None;self.last_sample=0.;self.next_sample=0.;self.started=0.;self.ended=0.;self.accepted=0;self.human=0
        self.last_action=None;self.policy_action=None;self.metadata={};self.events=[];self.episodes=[]
        self.task={'status':'idle'};self.last_quality={};self.lock=threading.RLock()
        threading.Thread(target=self._catalog,daemon=True).start()

    def _catalog(self):
        while True:
            try:
                root=self.root;records=catalog(root)
                if root==self.root:self.episodes=records
            except Exception:pass
            time.sleep(2)

    def configure_storage(self,value):
        if self.active or (self.recorder and self.recorder.process.poll() is None):
            raise ValueError('录制或保存尚未结束，不能修改保存路径')
        if self.task.get('status') in ('running','exporting'):raise ValueError('数据任务执行中，不能修改保存路径')
        if not isinstance(value,str) or not value.strip() or len(value)>4096:raise ValueError('请输入有效保存路径')
        path=Path(value.strip()).expanduser()
        if not path.is_absolute() or path==Path('/'):raise ValueError('请输入 qijun 电脑上的绝对目录路径')
        path=path.resolve();path.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryFile(dir=path) as probe:probe.write(b'write-check');probe.flush()
        if self.settings_file:
            temporary=self.settings_file.with_suffix('.tmp')
            temporary.write_text(json.dumps({'data_root':str(path)},ensure_ascii=False,indent=2))
            os.replace(temporary,self.settings_file)
        self.root=path;self.episodes=catalog(path);self.task={'status':'idle'}

    @property
    def active(self):return self.recorder is not None and not self.recorder.closed

    def invalidate(self):self.generation+=1

    def transition(self,phase,reason=''):
        self.phase=phase;self.last_sample=0;self.next_sample=0
        if phase=='saving':self.ended=time.monotonic()
        if phase in ('human','policy'):self.segment+=1
        if phase!='policy':self.last_action=None;self.policy_action=None
        event={'timestamp':time.monotonic(),'phase':phase,'segment':self.segment,'reason':reason}
        self.events.append(event)
        if self.active:
            try:self.recorder.send('event',event)
            except Exception as exc:self.error=str(exc)

    def begin(self,manager,data):
        metadata=manager.local_server.snapshot()
        checkpoint=metadata.get('checkpoint',str(ROOT.parent/'kai0_arx_server/checkpoints/place_plate/015000/pretrained_model'))
        config=manager.config
        urdf=Path(config['robot']['urdf_path'])
        self.metadata={'prompt':data['prompt'],'checkpoint':checkpoint,'checkpoint_name':metadata.get('checkpoint_name'),
            'checkpoint_sha256':metadata.get('checkpoint_config_sha256'),'robot_config':config['robot'],
            'feedback_time_basis':'host SDK read completion; no per-motor receive timestamp exposed',
            'camera_config':config['cameras'],'sdk_motion_configuration':json.loads(json.dumps(getattr(manager.robot,'sdk_motion_limits',None),default=lambda x:x.tolist() if hasattr(x,'tolist') else str(x))),'action_names':[f'{side}_{joint}' for side in ('left','right') for joint in ('joint_1','joint_2','joint_3','joint_4','joint_5','joint_6','gripper')],
            'urdf_sha256':hashlib.sha256(urdf.read_bytes()).hexdigest() if urdf.is_file() else None,
            'demo':manager.demo,'steps':data['steps'],'chunk_steps':data.get('chunk_steps',0),
            'server':config['server']['url']}
        cp=Path(checkpoint)/'config.json'
        if cp.is_file():self.metadata['checkpoint_sha256']=hashlib.sha256(cp.read_bytes()).hexdigest()
        self.recorder=Recorder(self.metadata,self.root);self.started=time.monotonic();self.ended=0.;self.accepted=self.human=0
        self.events=[];self.segment=0;self.error='';self.transition('transition','等待录制进程就绪')
        return self.recorder

    def sample(self,manager):
        if not self.active or self.phase not in ('policy','human'):return
        status=self.recorder.snapshot()
        if status['status']=='error':raise RuntimeError(status.get('error','录制进程失败'))
        now=time.monotonic()
        if now<self.next_sample:return
        images,meta=manager.cams.latest()
        camera=np.array([[meta[r][k] for k in ('captured','received','device_ms','sequence')] for r in ROLES],dtype='f8')
        age=now-camera[:,0];feedback_age=now-manager.observed_at;skew=np.ptp(camera[:,0])
        self.last_quality={'image_age_ms':round(float(max(age))*1000,1),'feedback_age_ms':round(feedback_age*1000,1),'camera_skew_ms':round(float(skew)*1000,1)}
        if feedback_age>.05 or np.max(age)>.1 or np.min(age)<-.02 or skew>.05:
            raise RuntimeError('录制数据过期或相机不同步：'+str(self.last_quality))
        q=np.asarray(manager.state,dtype='f4')
        if q.shape!=(14,) or not np.isfinite(q).all():raise RuntimeError('录制反馈无效')
        human=self.phase=='human';requested=None if human else self.last_action
        frame={'timestamp':now,'feedback_time':manager.observed_at,'qpos':q.copy(),
               'policy_action':np.full(14,np.nan,dtype='f4') if self.policy_action is None or human else np.asarray(self.policy_action,dtype='f4'),
               'requested_action':np.full(14,np.nan,dtype='f4') if requested is None else np.asarray(requested,dtype='f4'),
               'request_valid':int(requested is not None),'control_mode':int(human),'intervention':int(human),'segment':self.segment,
               'images':images,'camera_times':camera}
        self.recorder.send('frame',frame);self.last_sample=now
        self.next_sample=(self.next_sample or now)+1/30
        if self.next_sample<now:self.next_sample=now+1/30
        self.accepted+=1;self.human+=int(human)

    def snapshot(self):
        writer=self.recorder.snapshot() if self.recorder else {'status':'idle','frames':0}
        phase=self.phase
        if writer['status'] in ('saved','discarded'):phase=writer['status']
        if writer['status']=='error':phase='error'
        return {'data_root':str(self.root),'phase':phase,'active':self.active,'episode':self.recorder.id if self.recorder else None,
                'elapsed':round((self.ended or time.monotonic())-self.started,1) if self.started else 0,'samples':self.accepted,
                'human_frames':self.human,'human_ratio':round(self.human/max(1,self.accepted)*100,1),
                'interventions':sum(e['phase']=='human' for e in self.events),'events':self.events[-100:],
                'writer':writer,'error':self.error or writer.get('error',''),'metadata':self.metadata,
                'quality':self.last_quality,'episodes':self.episodes,'task':dict(self.task)}

    def background_job(self,operation,episode,options=None):
        if self.task.get('status') in ('running','exporting'):raise ValueError('已有数据任务正在执行')
        path=episode_path(episode,self.root)
        if self.recorder and self.recorder.id==episode and self.recorder.process.poll() is None:
            raise ValueError('录制进程仍在退出，请稍后处理数据')
        if self.recorder and self.recorder.id==episode and self.recorder.snapshot()['status'] not in ('saved','discarded','error'):
            raise ValueError('当前 episode 尚未完成')
        self.task={'status':'running','episode':episode,'operation':operation,'progress':0}
        def work():
            try:
                if operation=='trash':
                    target=self.root/'trash'/episode;target.parent.mkdir(parents=True,exist_ok=True)
                    os.replace(path,target);self.task.update(status='complete',message='已移入回收区');return
                args=[DATA_PYTHON,'-u',str(ROOT/'dagger_worker.py'),operation,str(self.root),episode]
                if operation=='export':args.append(json.dumps(options or {'mode':'human'}))
                p=subprocess.Popen(args,
                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=worker_env(),start_new_session=True)
                errors=[]
                for line in p.stdout:
                    try:self.task.update(json.loads(line))
                    except ValueError:errors.append(line.decode(errors='replace'))
                if p.wait()!=0:self.task.update(status='error',error=self.task.get('error') or ''.join(errors)[-2000:])
            except Exception as exc:self.task.update(status='error',error=str(exc))
            finally:self.episodes=catalog(self.root)
        threading.Thread(target=work,daemon=True).start()
