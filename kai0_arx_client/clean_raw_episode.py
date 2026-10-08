"""Create a non-destructive raw-format copy with human leading still frames removed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

import av
import h5py
import numpy as np

from dagger_data import ROLES,episode_path
from dagger_segments import human_motion_trim_ranges
from dagger_worker import Video,validate


def atomic_json(path,data):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2))
    os.replace(temporary,path)


def sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def source_hashes(path):
    names=['manifest.json','trajectory.h5',*[f'{role}.mp4' for role in ROLES]]
    return {name:sha256(path/name) for name in names}


def clean(root,episode,config=None):
    source=episode_path(episode,root)
    manifest=json.loads((source/'manifest.json').read_text())
    if manifest.get('status')!='saved':raise ValueError('必须先完成原始数据保存')
    validate(source)
    before=source_hashes(source)
    with h5py.File(source/'trajectory.h5','r') as h:
        q=h['qpos'][:];t=h['timestamp'][:];labels=h['intervention'][:];segments=h['segment'][:]
    excluded,segment_report,resolved=human_motion_trim_ranges(q,t,labels,segments,config or True)
    keep=np.ones(len(t),dtype=bool)
    for first,last in excluded:keep[first:last+1]=False
    indices=np.flatnonzero(keep)
    if len(indices)<2:raise ValueError('清洗后没有足够帧')
    output_root=root/'raw_cleaned';output_root.mkdir(exist_ok=True)
    output=output_root/(episode+'_human-motion-trim_'+str(time.time_ns()))
    temporary=output.with_name(output.name+'.incomplete');temporary.mkdir(exist_ok=False)
    try:
        with h5py.File(source/'trajectory.h5','r') as src,h5py.File(temporary/'trajectory.h5','w') as dst:
            for key,value in src.attrs.items():dst.attrs[key]=value
            dst.attrs['derived_from_episode']=episode
            dst.attrs['cleaning']='human_leading_stationary_trim'
            for key in src:
                source_dataset=src[key];data=source_dataset[:][keep]
                chunks=(min(128,len(data)),*source_dataset.shape[1:]) if len(data) else None
                target=dst.create_dataset(key,data=data,dtype=source_dataset.dtype,chunks=chunks)
                for attr,value in source_dataset.attrs.items():target.attrs[attr]=value
            dst.create_dataset('source_frame_index',data=indices.astype('i8'),chunks=(min(128,len(indices)),))
        for role in ROLES:
            writer=Video(temporary/f'{role}.mp4');decoded=0
            try:
                with av.open(str(source/f'{role}.mp4')) as container:
                    for index,frame in enumerate(container.decode(video=0)):
                        decoded+=1
                        if keep[index]:writer.write(frame.to_ndarray(format='rgb24'))
            finally:writer.close()
            if decoded!=len(t):raise ValueError(f'{role} 原视频帧数 {decoded} 与 HDF5 {len(t)} 不一致')
        cleaning={'type':'human_leading_stationary_trim','config':resolved,'excluded_ranges':excluded,
                  'segments':segment_report,'source_frames':len(t),'output_frames':len(indices),
                  'removed_frames':int((~keep).sum()),'source_human_frames':int(labels.sum()),
                  'output_human_frames':int(labels[keep].sum()),'source_frame_index_dataset':'source_frame_index'}
        derived=dict(manifest,frames=len(indices),human_frames=int(labels[keep].sum()),derived=True,
                     derived_from_episode=episode,derived_created=time.time(),cleaning=cleaning)
        atomic_json(temporary/'manifest.json',derived)
        report={'source_episode':episode,'source_path':str(source),'output_path':str(output),
                'source_files_modified':False,'source_sha256':before,**cleaning}
        atomic_json(temporary/'cleaning_report.json',report)
        validate(temporary)
        after=source_hashes(source)
        if after!=before:raise RuntimeError('原始文件哈希发生变化，拒绝提交清洗副本')
        os.replace(temporary,output)
        return output,report
    except BaseException:
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path)
    parser.add_argument('episode')
    parser.add_argument('--threshold-rad',type=float,default=.008)
    parser.add_argument('--consecutive-frames',type=int,default=3)
    parser.add_argument('--baseline-frames',type=int,default=3)
    parser.add_argument('--pre-roll-frames',type=int,default=0)
    args=parser.parse_args()
    config={'displacement_threshold_rad':args.threshold_rad,'consecutive_frames':args.consecutive_frames,
            'baseline_frames':args.baseline_frames,'pre_roll_frames':args.pre_roll_frames,'drop_if_no_motion':True}
    output,report=clean(args.root.expanduser().resolve(),args.episode,config)
    print(json.dumps({'status':'complete','path':str(output),'report':report},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
