"""Manage the verified local checkpoint server, never robot hardware."""
import json
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import urllib.request

ROOT=Path(__file__).resolve().parent
SERVER_ROOT=ROOT.parent/'kai0_arx_server'
URL='ws://127.0.0.1:8001'
HEALTH='http://127.0.0.1:8001/healthz'
CHECKPOINT=SERVER_ROOT/'checkpoints/place_plate/015000/pretrained_model'
RECORD=SERVER_ROOT/'logs/managed-server.json'
SCRIPT=SERVER_ROOT/'local_scripts/serve_arx_lerobot.py'


def process_identity(pid):
    path=Path('/proc')/str(pid)
    if path.stat().st_uid!=os.getuid():raise RuntimeError('Server owner mismatch')
    args=(path/'cmdline').read_bytes().split(b'\0')
    if str(SCRIPT).encode() not in args:raise RuntimeError('Server command mismatch')
    return (path/'stat').read_text().rsplit(')',1)[1].split()[19]


class LocalServer:
    def __init__(self):
        self.lock=threading.Lock();self.operation=threading.Lock()
        self.process=None;self.worker=None;self.cancel=threading.Event()
        self.state={'status':'checking','url':URL,'message':'检查本机服务',
                    'checkpoint_name':'place_plate / 015000','checkpoint':str(CHECKPOINT),
                    'managed':False,'log':None}

    def monitor(self):
        threading.Thread(target=self._monitor,daemon=True).start()

    def _monitor(self):
        while True:
            if self.operation.acquire(blocking=False):
                try:self.refresh()
                finally:self.operation.release()
            time.sleep(2)

    def snapshot(self):
        with self.lock:return dict(self.state)

    def update(self,**values):
        with self.lock:self.state.update(values)

    def health(self):
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(HEALTH,timeout=.5) as response:meta=json.load(response)
        if (meta.get('service')!='kai0-arx-lerobot-pi05' or meta.get('mode')!='checkpoint'
                or meta.get('checkpoint')!=str(CHECKPOINT) or not meta.get('weights_strictly_loaded')):
            raise RuntimeError('8001 服务不是预期的本机 checkpoint server')
        return meta

    def managed_pid(self):
        try:
            record=json.loads(RECORD.read_text());pid=int(record['pid'])
            if process_identity(pid)!=record['start_ticks']:return None
            return pid
        except (OSError,ValueError,KeyError,RuntimeError):return None

    def refresh(self):
        pid=self.managed_pid()
        try:meta=self.health()
        except Exception as exc:
            previous=self.snapshot()
            if previous['status']=='error':return
            self.update(status='unavailable' if pid else 'stopped', managed=bool(pid),pid=pid,
                        message=f'进程存在，健康检查失败：{exc}' if pid else 'Server 未启动或已退出')
        else:
            self.update(status='ready',message='模型已加载，健康检查正常',
                        managed=bool(pid),pid=pid,checkpoint=meta['checkpoint'],weights=meta['weights_strictly_loaded'])

    def start(self):
        with self.lock:
            if self.worker and self.worker.is_alive():return
            self.cancel.clear()
            self.state.update(status='starting',message='正在加载模型',managed=True)
            self.worker=threading.Thread(target=self._run,daemon=True);self.worker.start()

    def stop(self):
        with self.lock:
            self.cancel.set()
            if self.worker and self.worker.is_alive():
                self.state.update(status='stopping',message='正在取消加载并关闭 Server');return
            self.state.update(status='stopping',message='正在关闭 Server')
            self.worker=threading.Thread(target=self._stop_run,daemon=True);self.worker.start()

    def _terminate(self):
        pid=self.managed_pid()
        if pid is None:
            raise RuntimeError('没有可验证的受管 Server 进程；未停止其他服务')
        # pidfd binds this signal to one process even if the numeric PID is recycled.
        expected=process_identity(pid)
        # The packaged client Python omits pidfd APIs; Ubuntu's system Python
        # provides them. Revalidate identity after opening the stable pidfd.
        helper="""import os,signal,sys,pathlib
pid=int(sys.argv[1]);path=pathlib.Path('/proc')/str(pid)
fd=os.pidfd_open(pid)
try:
 assert path.stat().st_uid==os.getuid(), 'owner mismatch'
 assert sys.argv[2].encode() in (path/'cmdline').read_bytes().split(b'\\0'), 'command mismatch'
 assert (path/'stat').read_text().rsplit(')',1)[1].split()[19]==sys.argv[3], 'start identity mismatch'
 signal.pidfd_send_signal(fd,signal.SIGTERM)
finally:os.close(fd)
"""
        subprocess.run(['/usr/bin/python3','-c',helper,str(pid),str(SCRIPT),expected],
                       check=True,capture_output=True,text=True,timeout=3)
        for _ in range(100):
            if self.process is not None:self.process.poll()
            if self.managed_pid()!=pid:
                # Process identity may disappear just before inherited lock cleanup.
                with (SERVER_ROOT/'logs/server.lock').open('a') as lockfile:
                    try:fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:pass
                    else:
                        fcntl.flock(lockfile,fcntl.LOCK_UN)
                        break
            time.sleep(.1)
        else:raise RuntimeError('Server 未响应正常退出；未强制结束')
        RECORD.unlink(missing_ok=True)
        self.update(status='stopped',message='Server 已关闭',managed=False,pid=None)

    def _stop_run(self):
        with self.operation:
            try:self._terminate()
            except Exception as exc:self.update(status='error',message=str(exc),managed=bool(self.managed_pid()))

    def _run(self):
        with self.operation:
            try:
                try:meta=self.health()
                except Exception:meta=None
                if meta is None:
                    if not (SERVER_ROOT/'CHECKPOINT_VERIFIED.json').is_file():raise RuntimeError('checkpoint 尚未完成同步校验')
                    if self.managed_pid():raise RuntimeError('受管进程仍存在，请先关闭后重试')
                    with socket.socket() as sock:
                        sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                        try:sock.bind(('127.0.0.1',8001))
                        except OSError as exc:raise RuntimeError('8001 被其他服务占用；未停止它') from exc
                    path=ROOT/'logs'/f'local-server-{time.time_ns()}.log';path.parent.mkdir(exist_ok=True)
                    script=SERVER_ROOT/'local_scripts/start_arx_lerobot_server.sh'
                    with path.open('ab') as log:
                        self.process=subprocess.Popen(['bash',str(script)],cwd=SERVER_ROOT,
                                                      stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    # Wait for bash to exec the exact Python script before recording identity.
                    for _ in range(100):
                        if self.process.poll() is not None:raise RuntimeError(f'Server 启动失败；日志：{path}')
                        try:identity=process_identity(self.process.pid);break
                        except (OSError,RuntimeError):time.sleep(.02)
                    else:raise RuntimeError('无法验证启动进程身份')
                    RECORD.parent.mkdir(exist_ok=True)
                    temporary=RECORD.with_suffix('.tmp')
                    temporary.write_text(json.dumps({'pid':self.process.pid,'start_ticks':identity}))
                    temporary.replace(RECORD)
                    self.update(log=str(path),pid=self.process.pid,managed=True)
                    deadline=time.monotonic()+600
                    while time.monotonic()<deadline:
                        if self.cancel.is_set():self._terminate();return
                        if self.process.poll() is not None:raise RuntimeError(f'Server 已退出；日志：{path}')
                        try:meta=self.health();break
                        except Exception:time.sleep(.5)
                    else:
                        self._terminate();raise RuntimeError('加载超过10分钟，已关闭 Server')
                if self.cancel.is_set():self._terminate();return
                self.update(status='ready',message='模型已加载，健康检查正常',
                            managed=bool(self.managed_pid()),checkpoint=meta['checkpoint'],weights=meta['weights_strictly_loaded'])
            except Exception as exc:self.update(status='error',message=str(exc),managed=bool(self.managed_pid()))
