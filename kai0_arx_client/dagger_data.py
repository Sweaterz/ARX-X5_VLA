"""Dataset paths, bounded process transport and read-only episode catalog."""
import json
import os
from pathlib import Path
import pickle
import queue
import re
import struct
import subprocess
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parent
DATA_ROOT=Path(os.environ.get('ARX_DAGGER_DATA_ROOT',str(ROOT.parent/'datasets/dagger')))
DATA_PYTHON=os.environ.get('ARX_DATA_PYTHON',str(ROOT/'.venv-data/bin/python'))
ROLES=('head','left','right')


def worker_env():
    env=os.environ.copy()
    env['PYTHONPATH']=str(ROOT/'data_deps')+os.pathsep+str(ROOT)
    env.update(OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',TOKENIZERS_PARALLELISM='false',HF_HUB_OFFLINE='1')
    return env


def episode_path(episode,root=DATA_ROOT):
    if not re.fullmatch(r'ep_[0-9]+_[a-f0-9]{8}',episode):raise ValueError('无效 episode')
    for parent in (root/'episodes',root/'incomplete'):
        path=parent/episode
        if path.is_dir():return path
    raise ValueError('Episode 不存在')


def catalog(root=DATA_ROOT):
    records=[]
    for category in ('episodes','incomplete'):
        for path in (root/category).glob('ep_*/manifest.json'):
            try:
                m=json.loads(path.read_text());m['recoverable']=category=='incomplete'
                records.append({k:m.get(k) for k in ('episode','created','status','prompt','checkpoint_name','frames','human_frames','result','recoverable','error')})
            except (OSError,ValueError):pass
    return sorted(records,key=lambda x:x.get('created',0),reverse=True)[:100]


class Recorder:
    def __init__(self,metadata,root=DATA_ROOT):
        self.id=f'ep_{time.time_ns()}_{uuid.uuid4().hex[:8]}'
        self.root=Path(root);self.queue=queue.Queue(maxsize=8)
        self.state={'status':'starting','episode':self.id,'frames':0};self.lock=threading.Lock()
        self.closed=False;self.ready=threading.Event();self.done=threading.Event()
        self.process=subprocess.Popen([DATA_PYTHON,'-u',str(ROOT/'dagger_worker.py'),'record',str(self.root),self.id],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=worker_env(),start_new_session=True)
        threading.Thread(target=self._feed,daemon=True).start()
        threading.Thread(target=self._status,daemon=True).start()
        self.queue.put_nowait(('begin',metadata))

    def _status(self):
        for line in self.process.stdout:
            try:item=json.loads(line)
            except ValueError:continue
            with self.lock:self.state.update(item)
            if item.get('status')=='recording':self.ready.set()
            if item.get('status') in ('saved','error','discarded'):self.done.set()
        self.process.stdout.close()
        code=self.process.wait()
        with self.lock:
            if self.state.get('status') not in ('saved','discarded','error'):
                self.state.update(status='error',error=f'录制进程意外退出 {code}；数据保留在未完成区')
        self.done.set()

    def _feed(self):
        try:
            while True:
                try:item=self.queue.get(timeout=.2)
                except queue.Empty:
                    if self.done.is_set():break
                    continue
                data=pickle.dumps(item,protocol=5)
                self.process.stdin.write(struct.pack('!Q',len(data)));self.process.stdin.write(data);self.process.stdin.flush()
                if item[0] in ('finish','discard'):break
        except (OSError,ValueError) as exc:
            with self.lock:self.state.update(status='error',error=str(exc))
            self.done.set()
        finally:
            try:self.process.stdin.close()
            except (OSError,ValueError):pass

    def snapshot(self):
        with self.lock:return dict(self.state)

    def send(self,kind,value):
        if self.closed:raise RuntimeError('录制边界已经关闭')
        if self.snapshot()['status']=='error':raise RuntimeError(self.snapshot().get('error','录制失败'))
        try:self.queue.put_nowait((kind,value))
        except queue.Full:raise RuntimeError('录制缓冲已满，已拒绝样本；请暂停后保存')

    def finish(self,result='unfinished',discard=False):
        if self.closed:return
        self.closed=True
        if self.done.is_set():return
        # FIFO barrier is inserted by a helper after every previously accepted sample.
        def barrier():
            while not self.done.is_set():
                try:self.queue.put(('discard' if discard else 'finish',result),timeout=.2);return
                except queue.Full:continue
        with self.lock:self.state['status']='saving'
        threading.Thread(target=barrier,daemon=True).start()
