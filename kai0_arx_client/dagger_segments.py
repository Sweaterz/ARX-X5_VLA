"""Pure selection logic. Frame ranges are inclusive, indexed in source videos/HDF5."""
import numpy as np

def parts(t,labels,segments):
    if not len(t):return []
    cuts=np.flatnonzero((np.diff(segments)!=0)|(np.diff(labels)!=0)|(np.diff(t)>.1)|(np.diff(t)<=0))+1
    return np.split(np.arange(len(t)),cuts)

def inventory(t,labels,segments):
    return [{'id':f'{int(segments[p[0]])}:{p[0]}:{p[-1]}','segment':int(segments[p[0]]),
             'intervention':int(labels[p[0]]),'start_frame':int(p[0]),'end_frame':int(p[-1]),
             'frames':len(p),'duration_s':round(float(t[p[-1]]-t[p[0]]),3),
             'video_start_s':int(p[0])/30,'video_end_s':int(p[-1])/30} for p in parts(t,labels,segments)]

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
