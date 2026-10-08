"""Pure selection logic. Frame ranges are inclusive, indexed in source videos/HDF5."""
import numpy as np

ARM_JOINT_INDICES=np.array([0,1,2,3,4,5,7,8,9,10,11,12])
DEFAULT_HUMAN_MOTION_TRIM={'displacement_threshold_rad':.008,'consecutive_frames':3,
                           'baseline_frames':3,'pre_roll_frames':0,'drop_if_no_motion':True}

def parts(t,labels,segments):
    if not len(t):return []
    cuts=np.flatnonzero((np.diff(segments)!=0)|(np.diff(labels)!=0)|(np.diff(t)>.1)|(np.diff(t)<=0))+1
    return np.split(np.arange(len(t)),cuts)

def inventory(t,labels,segments):
    return [{'id':f'{int(segments[p[0]])}:{p[0]}:{p[-1]}','segment':int(segments[p[0]]),
             'intervention':int(labels[p[0]]),'start_frame':int(p[0]),'end_frame':int(p[-1]),
             'frames':len(p),'duration_s':round(float(t[p[-1]]-t[p[0]]),3),
             'video_start_s':int(p[0])/30,'video_end_s':int(p[-1])/30} for p in parts(t,labels,segments)]

def human_motion_trim_ranges(q,t,labels,segments,config=None):
    """Find leading stationary ranges in human segments without changing source data."""
    if config is False:return [],[],None
    if config is True or config is None:config={}
    if not isinstance(config,dict):raise ValueError('human_motion_trim 必须为 true 或参数对象')
    unknown=set(config)-set(DEFAULT_HUMAN_MOTION_TRIM)
    if unknown:raise ValueError('未知 human_motion_trim 参数：'+','.join(sorted(unknown)))
    resolved={**DEFAULT_HUMAN_MOTION_TRIM,**config}
    threshold=resolved['displacement_threshold_rad'];consecutive=resolved['consecutive_frames']
    baseline_frames=resolved['baseline_frames'];pre_roll=resolved['pre_roll_frames'];drop=resolved['drop_if_no_motion']
    if isinstance(threshold,bool) or not isinstance(threshold,(int,float)) or not 0<threshold<=1:
        raise ValueError('位移阈值须为 (0, 1] rad')
    for name,value in [('consecutive_frames',consecutive),('baseline_frames',baseline_frames)]:
        if type(value) is not int or value<1 or value>300:raise ValueError(f'{name} 须为 1 至 300 的整数')
    if type(pre_roll) is not int or not 0<=pre_roll<=300:raise ValueError('pre_roll_frames 须为 0 至 300 的整数')
    if type(drop) is not bool:raise ValueError('drop_if_no_motion 须为布尔值')
    q=np.asarray(q);t=np.asarray(t);labels=np.asarray(labels);segments=np.asarray(segments)
    if q.ndim!=2 or q.shape[0]!=len(t) or q.shape[1]<14 or len(labels)!=len(t) or len(segments)!=len(t):
        raise ValueError('运动检测输入维度不一致')
    if len(t) and not np.isfinite(q[:,ARM_JOINT_INDICES]).all():raise ValueError('机械臂关节数据包含非有限值')
    excluded=[];report=[]
    for p in parts(t,labels,segments):
        if not len(p) or int(labels[p[0]])!=1:continue
        arm=q[p][:,ARM_JOINT_INDICES]
        baseline=np.median(arm[:min(baseline_frames,len(arm))],axis=0)
        displaced=np.max(np.abs(arm-baseline),axis=1)>=threshold
        sustained=np.flatnonzero(np.convolve(displaced.astype(np.int16),np.ones(consecutive,dtype=np.int16),'valid')==consecutive) if len(p)>=consecutive else np.array([],dtype=int)
        if len(sustained):
            detected_local=int(sustained[0]);keep_local=max(0,detected_local-pre_roll)
            if keep_local:excluded.append([int(p[0]),int(p[keep_local-1])])
            report.append({'source_segment':int(segments[p[0]]),'source_start_frame':int(p[0]),'source_end_frame':int(p[-1]),
                           'motion_start_frame':int(p[detected_local]),'retained_start_frame':int(p[keep_local]),
                           'excluded_frames':keep_local,'retained_frames':len(p)-keep_local,'status':'motion_detected'})
        else:
            if drop:excluded.append([int(p[0]),int(p[-1])])
            report.append({'source_segment':int(segments[p[0]]),'source_start_frame':int(p[0]),'source_end_frame':int(p[-1]),
                           'motion_start_frame':None,'retained_start_frame':None if drop else int(p[0]),
                           'excluded_frames':len(p) if drop else 0,'retained_frames':0 if drop else len(p),
                           'status':'no_motion_dropped' if drop else 'no_motion_kept'})
    return excluded,report,resolved

def select_parts(t,labels,segments,options=None):
    options=options or {};mode=options.get('mode','human')
    if mode not in ('human','full','selected'):raise ValueError('未知导出方式')
    catalog=inventory(t,labels,segments);known={s['id'] for s in catalog}
    selected=options.get('selected_segments',[]);excluded=options.get('excluded_segments',[])
    for values in (selected,excluded):
        if not isinstance(values,list) or any(not isinstance(x,str) or x not in known for x in values):raise ValueError('片段选择已失效或无效，请重新读取')
    if mode=='selected' and not selected:raise ValueError('请至少选择一个片段')
    ranges=options.get('excluded_ranges',[])
    if not isinstance(ranges,list) or len(ranges)>200:raise ValueError('排除范围无效或过多')
    keep=np.ones(len(t),dtype=bool)
    for span in ranges:
        if not isinstance(span,list) or len(span)!=2 or any(type(x) is not int for x in span):raise ValueError('帧范围必须为整数起止帧')
        a,b=span
        if not 0<=a<=b<len(t):raise ValueError('帧范围超出记录边界')
        keep[a:b+1]=False
    output=[]
    for info,p in zip(catalog,parts(t,labels,segments)):
        if info['id'] in excluded or (mode=='human' and info['intervention']!=1) or (mode=='selected' and info['id'] not in selected):continue
        p=p[keep[p]]
        if not len(p):continue
        for contiguous in np.split(p,np.flatnonzero(np.diff(p)!=1)+1):
            if len(contiguous)>=3:output.append(contiguous)
    return output
