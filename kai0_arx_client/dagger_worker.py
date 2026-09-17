"""Isolated recording/export worker. No SDK imports or hardware handles."""
import json
import os
from pathlib import Path
import pickle
import shutil
import struct
import sys
import time
import traceback
import numpy as np
import h5py
import av
from dagger_data import ROLES,episode_path
from dagger_segments import inventory,select_parts


def emit(**data):print(json.dumps(data,ensure_ascii=False),flush=True)
def atomic_json(path,data):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));os.replace(tmp,path)


def message():
    header=sys.stdin.buffer.read(8)
    if not header:raise EOFError('录制管道断开；未完成数据已保留')
    size=struct.unpack('!Q',header)[0]
    if size>32*1024*1024:raise ValueError('帧消息过大')
    data=sys.stdin.buffer.read(size)
    if len(data)!=size:raise EOFError('不完整帧消息')
    return pickle.loads(data)  # private parent-created pipe, never HTTP input


class Video:
    def __init__(self,path):
        self.container=av.open(str(path),'w');self.stream=self.container.add_stream('libx264',rate=30)
        self.stream.width=640;self.stream.height=480;self.stream.pix_fmt='yuv420p'
        self.stream.options={'crf':'18','preset':'veryfast','threads':'1'};self.count=0
    def write(self,image):
        frame=av.VideoFrame.from_ndarray(image,format='rgb24')
        for packet in self.stream.encode(frame):self.container.mux(packet)
        self.count+=1
    def close(self):
        for packet in self.stream.encode():self.container.mux(packet)
        self.container.close()


def validate(path):
    with h5py.File(path/'trajectory.h5','r') as h:
        n=len(h['timestamp']);times=h['timestamp'][:]
        if n and (not np.isfinite(h['qpos'][:]).all() or np.any(np.diff(times)<=0)):raise ValueError('反馈或时间索引无效')
        for key in h:
            if len(h[key])!=n:raise ValueError(f'{key} 帧数不一致')
    for role in ROLES:
        if n==0 and not (path/f'{role}.mp4').exists():continue
        with av.open(str(path/f'{role}.mp4')) as video:
            count=sum(1 for _ in video.decode(video=0))
        if count!=n:raise ValueError(f'{role} 视频 {count} 帧与数据 {n} 不一致')
    return n


def record(root,episode):
    kind,meta=message()
    if kind!='begin':raise ValueError('Missing metadata')
    path=root/'incomplete'/episode;path.mkdir(parents=True,exist_ok=False)
    manifest=dict(meta,episode=episode,created=time.time(),status='writing',frames=0,events=[],fps=30,schema_version=1,control_mode_codes={'0':'JOINT','1':'GRAVITY'},
                  action_definition='raw policy request; human request is null; export target = future feedback +1 frame')
    atomic_json(path/'manifest.json',manifest)
    videos={};h=None;count=human=0
    try:
        h=h5py.File(path/'trajectory.h5','w')
        fields={'timestamp':((), 'f8'),'feedback_time':((),'f8'),'qpos':((14,),'f4'),
                'policy_action':((14,),'f4'),'requested_action':((14,),'f4'),'request_valid':((),'u1'),
                'intervention':((),'u1'),'segment':((),'i4'),'control_mode':((),'u1'),'camera_times':((3,4),'f8')}
        for name,(shape,dtype) in fields.items():h.create_dataset(name,shape=(0,*shape),maxshape=(None,*shape),dtype=dtype,chunks=(128,*shape))
        videos={r:Video(path/f'{r}.mp4') for r in ROLES}
        emit(status='recording',episode=episode,frames=0)
        while True:
            kind,item=message()
            if kind=='event':
                manifest['events'].append(item);atomic_json(path/'manifest.json',manifest);continue
            if kind in ('finish','discard'):break
            if kind!='frame':raise ValueError('Unknown recorder message')
            if count%30==0 and shutil.disk_usage(root).free<1024**3:raise OSError('剩余磁盘不足 1 GiB，录制已暂停')
            for r in ROLES:videos[r].write(item['images'][r])
            for name in fields:
                ds=h[name];ds.resize(count+1,axis=0);ds[count]=item[name]
            count+=1;human+=int(item['intervention'])
            if count%15==0:
                h.flush();manifest.update(frames=count,human_frames=human);atomic_json(path/'manifest.json',manifest)
                emit(status='recording',frames=count,human_frames=human)
        for video in videos.values():video.close()
        videos={};h.flush();h.close();h=None
        emit(status='saving',frames=count)
        validate(path)
        manifest.update(status='discarded' if kind=='discard' else 'saved',frames=count,human_frames=human,
                        result=item if item in ('success','failure','unfinished') else 'unfinished',finished=time.time())
        atomic_json(path/'manifest.json',manifest)
        destination=root/('trash' if kind=='discard' else 'episodes')/episode;destination.parent.mkdir(parents=True,exist_ok=True)
        os.replace(path,destination)
        emit(status=manifest['status'],frames=count,human_frames=human,path=str(destination))
    except BaseException as exc:
        manifest.update(status='incomplete',error=str(exc),frames=count,human_frames=human)
        atomic_json(path/'manifest.json',manifest);emit(status='error',error=str(exc),frames=count)
        raise
    finally:
        for video in videos.values():
            try:video.close()
            except Exception:pass
        if h is not None:h.close()


def recover(root,episode):
    path=episode_path(episode,root)
    if path.parent.name!='incomplete':raise ValueError('只能恢复未完成数据')
    # Keep only the common complete prefix; retain the originals for forensic recovery.
    backup=path/('recovery_original_'+str(time.time_ns()))
    with h5py.File(path/'trajectory.h5','r') as h:n=min(len(h[k]) for k in h)
    counts=[]
    for role in ROLES:
        try:
            with av.open(str(path/f'{role}.mp4')) as c:counts.append(sum(1 for _ in c.decode(video=0)))
        except Exception:raise RuntimeError('视频无法解码，保留原始数据；不能自动恢复')
    n=min(n,*counts)
    if n<1:raise ValueError('没有可恢复的完整帧')
    backup.mkdir(exist_ok=False)
    for role in ROLES:
        original=path/f'{role}.mp4';shutil.copy2(original,backup/original.name)
        out=Video(path/f'{role}.recovered.mp4')
        with av.open(str(original)) as c:
            for i,frame in enumerate(c.decode(video=0)):
                if i>=n:break
                out.write(frame.to_ndarray(format='rgb24'))
        out.close();os.replace(path/f'{role}.recovered.mp4',original)
    shutil.copy2(path/'trajectory.h5',backup/'trajectory.h5')
    with h5py.File(path/'trajectory.h5','r+') as h:
        for key in h:h[key].resize(n,axis=0)
    with h5py.File(path/'trajectory.h5','r') as h:human=int(h['intervention'][:].sum())
    validate(path);m=json.loads((path/'manifest.json').read_text());m.update(status='saved',result='unfinished',recovered=True,frames=n,human_frames=human)
    atomic_json(path/'manifest.json',m);target=root/'episodes'/episode;target.parent.mkdir(exist_ok=True);os.replace(path,target)
    emit(status='complete',message=f'已恢复 {n} 帧',episode=episode)


def replay_data(root,episode):
    path=episode_path(episode,root);meta=json.loads((path/'manifest.json').read_text())
    if meta['status']!='saved':raise ValueError('请先结束试验并完成保存')
    with h5py.File(path/'trajectory.h5','r') as h:
        n=len(h['qpos'])
        if not 2<=n<=100000:raise ValueError('回放支持 2 至 100000 帧')
        data={key:h[key][:].tolist() for key in ('qpos','timestamp','intervention','segment')}
    emit(status='complete',metadata={k:meta.get(k) for k in ('prompt','demo','robot_config','urdf_sha256','action_names')},**data)


def inspect_episode(root,episode):
    path=episode_path(episode,root)
    meta=json.loads((path/'manifest.json').read_text())
    if meta['status']!='saved':raise ValueError('先完成原始数据保存')
    with h5py.File(path/'trajectory.h5','r') as h:
        segments=inventory(h['timestamp'][:],h['intervention'][:],h['segment'][:])
    emit(status='complete',message='片段已读取，可回看并选择',segments=segments,
         pause_events=sum(e.get('phase')=='paused' for e in meta.get('events',[])))


def export(root,episode,options=None):
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from lerobot.configs.video import RGBEncoderConfig
    path=episode_path(episode,root)
    meta=json.loads((path/'manifest.json').read_text())
    if meta['status']!='saved':raise ValueError('必须先完成原始数据保存')
    validate(path)
    with h5py.File(path/'trajectory.h5','r') as h:
        q=h['qpos'][:];t=h['timestamp'][:];labels=h['intervention'][:];segments=h['segment'][:]
    options=options or {'mode':'human'}
    groups=[];provenance=[]
    for part in select_parts(t,labels,segments,options):
        grid=np.arange(t[part[0]],t[part[-1]],1/30)
        if len(grid)<2:continue
        interpolated=np.stack([np.interp(grid,t[part],q[part,j]) for j in range(14)],axis=1).astype('f4')
        image_indices=part[np.minimum(np.searchsorted(t[part],grid[:-1]),len(part)-1)]
        groups.append((interpolated[:-1],interpolated[1:],image_indices))
        provenance.append({'source_start_frame':int(part[0]),'source_end_frame':int(part[-1]),
                           'source_segment':int(segments[part[0]]),'intervention':int(labels[part[0]]),
                           'export_episode_index':len(groups)-1,'frames':len(grid)-1})
    if not groups:raise ValueError('所选范围没有足够连续的有效帧（每段至少 3 帧）')
    base=root/'exports';base.mkdir(exist_ok=True);output=base/(episode+'_'+str(time.time_ns()));tmp=output.with_name(output.name+'.incomplete')
    features={'observation.state':{'dtype':'float32','shape':(14,), 'names':meta['action_names']},
              'action':{'dtype':'float32','shape':(14,), 'names':meta['action_names']},
              'intervention':{'dtype':'int64','shape':(1,)},
              **{f'observation.images.{r}':{'dtype':'video','shape':(480,640,3),'names':['height','width','channel']} for r in ROLES}}
    ds=LeRobotDataset.create(repo_id='local/'+episode,fps=30,root=tmp,robot_type='arx5_2025',features=features,
         video_backend='pyav',encoder_threads=1,rgb_encoder=RGBEncoderConfig(vcodec='h264',extra_options={'bf':'0'}),image_writer_threads=1)
    for group,(states,actions,indices) in enumerate(groups):
        decoders={r:av.open(str(path/f'{r}.mp4')) for r in ROLES}
        iterators={r:iter(c.decode(video=0)) for r,c in decoders.items()};last=-1;images={}
        try:
            for state,action,index in zip(states,actions,indices):
                while last<index:
                    images={r:next(it).to_ndarray(format='rgb24') for r,it in iterators.items()};last+=1
                ds.add_frame({'observation.state':state,'action':action,'intervention':np.array([int(labels[index])],dtype=np.int64),
                              **{f'observation.images.{r}':img for r,img in images.items()},'task':meta['prompt']})
            ds.save_episode(parallel_encoding=False)
        finally:
            for c in decoders.values():c.close()
        emit(status='exporting',progress=round((group+1)/len(groups)*90),message=f'已导出片段 {group+1}/{len(groups)}')
    ds.finalize()
    loaded=LeRobotDataset('local/'+episode,root=tmp,video_backend='pyav',delta_timestamps={'action':[i/30 for i in range(50)]})
    sample=loaded[0]
    if tuple(sample['action'].shape)!=(50,14):raise ValueError('动作块维度错误')
    boundary=0
    for states,actions,_ in groups:
        boundary+=len(states);tail=loaded[boundary-1]
        if tuple(tail['action'].shape)!=(50,14) or not np.allclose(tail['action'].numpy(),np.tile(actions[-1],(50,1)),atol=1e-5):
            raise ValueError('动作块跨越人工段边界或末尾 padding 不正确')
        if int(tail['action_is_pad'].sum())!=49:raise ValueError('末尾 padding 标记错误')
    # Use the actual checkpoint processors, without loading policy weights or hardware.
    from lerobot.configs.policies import PreTrainedConfig
    from lerobot.policies.factory import make_pre_post_processors
    import torch
    ck=Path(meta['checkpoint'])
    config=PreTrainedConfig.from_pretrained(ck,local_files_only=True);config.device='cpu'
    pre,_=make_pre_post_processors(config,pretrained_path=str(ck),preprocessor_overrides={
        'device_processor':{'device':'cpu'},'tokenizer_processor':{'tokenizer_name':str(ck/'tokenizer')}})
    observation={'observation.state':sample['observation.state'],'task':meta['prompt']}
    observation.update({f'observation.images.{r}':sample[f'observation.images.{r}'] for r in ROLES})
    pre(observation)
    report={'source':episode,'human_only':all(p['intervention']==1 for p in provenance),'selection':options,'source_segments':provenance,
            'action_definition':'next feedback pose at +1/30s for both policy and human; no cross-cut targets','action_shift_frames':1,'fps':30,'segments':len(groups),
            'action_chunk':[50,14],'processor_validated':True,'segment_padding_validated':True,'frames':len(loaded),'original_training_alignment_verified':False}
    atomic_json(tmp/'dagger_export.json',report);os.replace(tmp,output)
    emit(status='complete',progress=100,path=str(output),message='LeRobot 加载与 PI05 预处理验证通过',report=report)


if __name__=='__main__':
    try:
        operation,root,episode=sys.argv[1:4]
        if operation=='export':export(Path(root),episode,json.loads(sys.argv[4]) if len(sys.argv)>4 else None)
        else:{'record':record,'recover':recover,'inspect':inspect_episode,'replay':replay_data}[operation](Path(root),episode)
    except BaseException as exc:
        emit(status='error',error=str(exc));traceback.print_exc(file=sys.stderr);sys.exit(1)
