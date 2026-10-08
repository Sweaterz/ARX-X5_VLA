"""Non-destructive batch export of motion-trimmed human DAgger segments."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from dagger_segments import DEFAULT_HUMAN_MOTION_TRIM,human_motion_trim_ranges


HERE=Path(__file__).resolve().parent
WORKER=HERE/'dagger_worker.py'
OPTIONS={'mode':'human','human_motion_trim':True}


def atomic_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2))
    os.replace(tmp,path)


def default_human_report(report,episode):
    """Only reuse the same validated export that this batch would generate."""
    if not isinstance(report,dict):return False
    selection=report.get('selection')
    if not isinstance(selection,dict) or selection.get('mode')!='human':return False
    allowed={'mode','human_motion_trim','selected_segments','excluded_segments','excluded_ranges'}
    if set(selection)-allowed:return False
    if any(selection.get(key,[])!=[] for key in ('selected_segments','excluded_segments','excluded_ranges')):return False
    requested_trim=selection.get('human_motion_trim')
    if requested_trim is not True:
        if not isinstance(requested_trim,dict) or set(requested_trim)-set(DEFAULT_HUMAN_MOTION_TRIM):return False
        if {**DEFAULT_HUMAN_MOTION_TRIM,**requested_trim}!=DEFAULT_HUMAN_MOTION_TRIM:return False
    trim=report.get('human_motion_trim')
    return (report.get('source')==episode and report.get('source_files_modified') is False
            and report.get('human_only') is True and isinstance(trim,dict)
            and trim.get('config')==DEFAULT_HUMAN_MOTION_TRIM
            and report.get('processor_validated') is True and report.get('segment_padding_validated') is True
            and report.get('action_chunk')==[50,14] and report.get('action_shift_frames')==1 and report.get('fps')==30
            and type(report.get('frames')) is int and report['frames']>0
            and type(report.get('segments')) is int and report['segments']>0)


def completed_export(root,episode):
    for report_path in sorted((root/'exports').glob(episode+'_*/dagger_export.json'),reverse=True):
        export=report_path.parent
        # The worker writes the report before atomically renaming .incomplete.
        if not export.name[len(episode)+1:].isdigit():continue
        try:
            report=json.loads(report_path.read_text())
            info=json.loads((export/'meta'/'info.json').read_text())
        except (OSError,ValueError):continue
        if not default_human_report(report,episode) or not isinstance(info,dict):continue
        if (info.get('codebase_version')!='v3.0' or info.get('fps')!=30
                or info.get('total_frames')!=report['frames'] or info.get('total_episodes')!=report['segments']):continue
        if not any((export/'data').rglob('*.parquet')):continue
        if any(not any((export/'videos'/f'observation.images.{role}').rglob('*.mp4')) for role in ('head','left','right')):continue
        return str(export)
    return None


def tree_manifest(path):
    result={}
    for file in sorted(x for x in path.rglob('*') if x.is_file()):
        digest=hashlib.sha256()
        with file.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        result[str(file.relative_to(path))]=[file.stat().st_size,digest.hexdigest()]
    return result


class CloudUpload:
    def __init__(self,host,port,key,root):
        self.host=host;self.port=port;self.key=key.expanduser().resolve();self.root=root.rstrip('/')
        if not self.key.is_file():raise ValueError(f'上传密钥不存在：{self.key}')
        control=f'/tmp/dagger-cloud-{os.getpid()}'
        self.ssh=['ssh','-p',str(port),'-i',str(self.key),'-o','BatchMode=yes','-o','IdentitiesOnly=yes',
                  '-o','StrictHostKeyChecking=yes','-o','ControlMaster=auto','-o',f'ControlPath={control}','-o','ControlPersist=600',host]

    def run(self,command,input_bytes=None):
        result=subprocess.run([*self.ssh,command],input=input_bytes,capture_output=True)
        if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace')[-4000:] or f'云端命令退出 {result.returncode}')
        return result.stdout.decode()

    def remote_manifest(self,path):
        code="""import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]);out={}
for f in sorted(x for x in p.rglob('*') if x.is_file()):
 h=hashlib.sha256()
 with f.open('rb') as s:
  for c in iter(lambda:s.read(1024*1024),b''):h.update(c)
 out[str(f.relative_to(p))]=[f.stat().st_size,h.hexdigest()]
print(json.dumps(out,sort_keys=True))"""
        output=self.run('python3 -c '+shlex.quote(code)+' '+shlex.quote(path))
        return json.loads(output)

    def export(self,local_path,collection):
        destination=f'{self.root}/datasets/{collection}/{local_path.name}'
        local_manifest=tree_manifest(local_path)
        exists=self.run('test -d '+shlex.quote(destination)+' && echo yes || true').strip()=='yes'
        if exists:
            if self.remote_manifest(destination)!=local_manifest:raise RuntimeError(f'云端同名目录内容不一致，未覆盖：{destination}')
            return destination
        temporary=destination+f'.uploading-{os.getpid()}-{time.time_ns()}'
        self.run('install -d -m 0775 '+shlex.quote(temporary))
        tar_process=subprocess.Popen(['tar','-C',str(local_path),'-cf','-','.'],stdout=subprocess.PIPE)
        ssh_process=subprocess.Popen([*self.ssh,'tar -C '+shlex.quote(temporary)+' -xf -'],stdin=tar_process.stdout,
                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        tar_process.stdout.close();_,ssh_error=ssh_process.communicate();tar_code=tar_process.wait()
        if tar_code or ssh_process.returncode:
            raise RuntimeError(ssh_error.decode(errors='replace')[-4000:] or '上传 tar 流失败；临时目录已保留')
        if self.remote_manifest(temporary)!=local_manifest:raise RuntimeError(f'云端逐文件 SHA-256 校验失败：{temporary}')
        self.run('test ! -e '+shlex.quote(destination)+' && mv '+shlex.quote(temporary)+' '+shlex.quote(destination))
        return destination

    def report(self,local_path):
        destination=f'{self.root}/reports/{local_path.name}'
        temporary=destination+f'.tmp-{os.getpid()}'
        command=('install -d -m 0775 '+shlex.quote(f'{self.root}/reports')+' && cat > '
                 +shlex.quote(temporary)+' && mv '+shlex.quote(temporary)+' '+shlex.quote(destination))
        self.run(command,local_path.read_bytes())
        return destination


def preflight(path):
    import h5py
    with h5py.File(path/'trajectory.h5','r') as h:
        q=h['qpos'][:];t=h['timestamp'][:];labels=h['intervention'][:];segments=h['segment'][:]
    human_frames=int(labels.sum())
    ranges,report,config=human_motion_trim_ranges(q,t,labels,segments,True)
    retained=sum(item['retained_frames'] for item in report)
    return {'source_frames':len(t),'human_frames':human_frames,'retained_human_frames':retained,
            'excluded_human_frames':sum(b-a+1 for a,b in ranges),'config':config,'segments':report}


def run_export(root,episode):
    command=[sys.executable,'-u',str(WORKER),'export',str(root),episode,json.dumps(OPTIONS)]
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
    final=None;tail=[]
    for line in process.stdout:
        line=line.rstrip();tail=(tail+[line])[-30:]
        try:item=json.loads(line)
        except ValueError:continue
        if not isinstance(item,dict):continue
        if item.get('status') in {'exporting','complete','error'}:
            print(json.dumps({'episode':episode,**{k:item.get(k) for k in ('status','progress','message','error','path')}},ensure_ascii=False),flush=True)
        if item.get('status') in {'complete','error'}:final=item
    code=process.wait()
    if code or not final or final.get('status')!='complete':
        raise RuntimeError((final or {}).get('error') or '\n'.join(tail)[-4000:] or f'导出进程退出 {code}')
    if not isinstance(final.get('path'),str) or not default_human_report(final.get('report'),episode):
        raise RuntimeError('导出完成消息缺少匹配的人工段选项或加载验证报告')
    return final


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roots',nargs='+',type=Path)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--force',action='store_true',help='即使已有成功清洗导出也重新生成')
    parser.add_argument('--limit',type=int,default=0,help='最多实际导出的 episode 数；0 表示不限')
    parser.add_argument('--upload-existing-only',action='store_true',help='只上传已有清洗结果，不生成新结果')
    parser.add_argument('--episode',action='append',help='只处理指定 episode；可重复传入')
    parser.add_argument('--upload-host')
    parser.add_argument('--upload-port',type=int,default=22)
    parser.add_argument('--upload-key',type=Path)
    parser.add_argument('--upload-root')
    args=parser.parse_args()
    roots=[root.expanduser().resolve() for root in args.roots]
    for root in roots:
        if not (root/'episodes').is_dir():raise ValueError(f'缺少 episodes 目录：{root}')
    report_path=(args.report.expanduser().resolve() if args.report else
                 roots[0]/'exports'/f'human_motion_trim_batch_{time.time_ns()}.json')
    upload_values=(args.upload_host,args.upload_key,args.upload_root)
    if any(upload_values) and not all(upload_values):raise ValueError('云端上传必须同时提供 host、key 和 root')
    uploader=CloudUpload(args.upload_host,args.upload_port,args.upload_key,args.upload_root) if all(upload_values) else None
    batch={'status':'running','created':time.time(),'dry_run':args.dry_run,'roots':[str(x) for x in roots],
           'options':OPTIONS,'source_files_modified':False,
           'cloud':{'host':args.upload_host,'port':args.upload_port,'root':args.upload_root} if uploader else None,'records':[]}
    atomic_json(report_path,batch)
    if uploader:uploader.report(report_path)
    print(json.dumps({'status':'batch_started','report':str(report_path)},ensure_ascii=False),flush=True)
    exported=uploaded=failed=0
    try:
        for root in roots:
            for path in sorted((root/'episodes').glob('ep_*')):
                if not path.is_dir():continue
                episode=path.name;record={'root':str(root),'episode':episode}
                if args.episode and episode not in args.episode:continue
                try:
                    existing=None if args.force else completed_export(root,episode)
                    if existing:
                        record.update(status='already_exported',output=existing)
                    else:
                        record['preflight']=preflight(path)
                        if not record['preflight']['human_frames']:
                            record['status']='skipped_no_human'
                        elif not record['preflight']['retained_human_frames']:
                            record['status']='skipped_no_motion'
                        elif args.upload_existing_only:
                            record['status']='deferred_missing_local_export'
                        elif args.dry_run:
                            record['status']='ready'
                        elif args.limit and exported>=args.limit:
                            record['status']='deferred_by_limit'
                        else:
                            final=run_export(root,episode);exported+=1
                            record.update(status='complete',output=final['path'],report=final['report'])
                    if uploader and not args.dry_run and record.get('output') and record.get('status') in {'already_exported','complete'}:
                        collection='base' if root.name=='dagger' else root.name
                        record['cloud_output']=uploader.export(Path(record['output']),collection);uploaded+=1
                except Exception as exc:
                    failed+=1;record.update(status='error',error=str(exc))
                batch['records'].append(record)
                batch.update(updated=time.time(),exported=exported,uploaded=uploaded,failed=failed)
                atomic_json(report_path,batch)
                if uploader and (record.get('cloud_output') or record.get('status')=='error' or len(batch['records'])%10==0):uploader.report(report_path)
                print(json.dumps({'status':'batch_progress','episode':episode,'result':record['status'],
                                  'completed_records':len(batch['records']),'exported':exported,'uploaded':uploaded,'failed':failed},ensure_ascii=False),flush=True)
        batch.update(status='complete' if failed==0 else 'complete_with_errors',finished=time.time())
    except BaseException as exc:
        batch.update(status='interrupted',error=str(exc),finished=time.time())
        raise
    finally:
        atomic_json(report_path,batch)
        if uploader:uploader.report(report_path)
    print(json.dumps({'status':batch['status'],'report':str(report_path),'exported':exported,'uploaded':uploaded,'failed':failed},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
