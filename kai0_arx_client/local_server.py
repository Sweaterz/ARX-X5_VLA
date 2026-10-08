"""Manage the verified local checkpoint server, never robot hardware."""
import json
import fcntl
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.request

ROOT=Path(__file__).resolve().parent
SERVER_ROOT=ROOT.parent/'kai0_arx_server'
FOLD_CHECKPOINT_ROOT=Path('/home/qijun/lyt/fold/checkpoints')
URL='ws://127.0.0.1:8001'
HEALTH='http://127.0.0.1:8001/healthz'
CHECKPOINT=SERVER_ROOT/'checkpoints/place_plate/015000/pretrained_model'
RECORD=SERVER_ROOT/'logs/managed-server.json'
SELECTION=ROOT/'logs/checkpoint-selection.json'
SCRIPT=SERVER_ROOT/'local_scripts/serve_arx_lerobot.py'
ACTION_NAMES=[*(f'left_joint_{i}' for i in range(1,7)),'left_gripper',
              *(f'right_joint_{i}' for i in range(1,7)),'right_gripper']
INPUT_FEATURES={'observation.state','observation.images.head','observation.images.left','observation.images.right'}


def checkpoint_name(path):
    path=Path(path).resolve()
    try:
        relative=path.relative_to(FOLD_CHECKPOINT_ROOT.resolve())
        if relative.parts==('pretrained_model',):step='current'
        elif relative.parts[-1]=='pretrained_model' and len(relative.parts)>=2:step=relative.parts[-2]
        else:step=relative.parts[-1]
        run=relative.parts[-3] if relative.parts[-1]=='pretrained_model' and len(relative.parts)>=3 else ''
        mix=re.search(r'dagger\d+_orig\d+',run)
        return f'fold / {mix.group()} / {step}' if mix else f'fold / {step}'
    except ValueError:pass
    try:
        relative=path.relative_to((SERVER_ROOT/'checkpoints').resolve())
        if len(relative.parts)>=3 and relative.parts[-1]=='pretrained_model':
            return f'{relative.parts[-3]} / {relative.parts[-2]}'
    except ValueError:pass
    return f'{path.parent.parent.name} / {path.parent.name}' if path.name=='pretrained_model' else path.name


def checkpoint_prompt(path):
    """Explicit sidecar first; known deployment task names are not guessed from folders."""
    path=Path(path).resolve()
    for filename in ('inference_prompt.json',):
        try:
            data=json.loads((path/filename).read_text())
            prompt=data.get('prompt') if isinstance(data,dict) else data
            if isinstance(prompt,str) and prompt.strip():return prompt.strip(),filename
        except (OSError,ValueError):pass
    try:
        training=json.loads((path/'train_config.json').read_text())
        for key in ('default_prompt','prompt','task'):
            value=training.get(key)
            if isinstance(value,str) and value.strip():return value.strip(),'train_config.json:'+key
        if training.get('dataset',{}).get('repo_id')=='local/fold_box_v1_pi05':
            return 'fold the paper boxes.','fold_box_policy_robot.py --task (local/fold_box_v1_pi05)'
    except (OSError,ValueError,AttributeError):pass
    if path==CHECKPOINT.resolve():return 'pick up the plate  on the ruck.','place_plate deployment default'
    return '', 'unknown'


def checkpoint_verified(path):
    path=Path(path).resolve()
    markers=(SERVER_ROOT/'CHECKPOINT_VERIFIED.json',path/'CHECKPOINT_VERIFIED.json',path.parent/'CHECKPOINT_VERIFIED.json')
    for marker in markers:
        try:
            if Path(json.loads(marker.read_text())['checkpoint']).resolve()==path:return True
        except (OSError,ValueError,KeyError,TypeError):pass
    return False


def checkpoint_compatible(path):
    path=Path(path).resolve()
    try:
        config=json.loads((path/'config.json').read_text())
        if config.get('type')!='pi05' or config.get('use_relative_actions') is not False:return False
        if config.get('input_features',{}).get('observation.state',{}).get('shape')!=[14]:return False
        if config.get('output_features',{}).get('action',{}).get('shape')!=[14]:return False
        if set(config.get('input_features',{}))!=INPUT_FEATURES:return False
        if config.get('action_feature_names')!=ACTION_NAMES:return False
        required=('model.safetensors','policy_preprocessor.json','policy_postprocessor.json',
                  'tokenizer/tokenizer.json','tokenizer/tokenizer_config.json')
        if any(not (path/name).is_file() for name in required):return False
        for name in ('policy_preprocessor.json','policy_postprocessor.json'):
            for step in json.loads((path/name).read_text()).get('steps',[]):
                if step.get('state_file') and not (path/step['state_file']).is_file():return False
        return True
    except (OSError,ValueError,KeyError,TypeError):return False


def saved_selection():
    try:return json.loads(SELECTION.read_text())
    except (OSError,ValueError):return {}


def discover_checkpoints():
    found=[];seen=set()
    configs=[*((SERVER_ROOT/'checkpoints').glob('*/*/pretrained_model/config.json')),
             *FOLD_CHECKPOINT_ROOT.rglob('config.json')]
    for config in sorted(configs):
        path=config.parent.resolve()
        if path in seen or not checkpoint_compatible(path):continue
        seen.add(path);verified=checkpoint_verified(path)
        source='fold' if path.is_relative_to(FOLD_CHECKPOINT_ROOT.resolve()) else 'bundled'
        found.append({'name':checkpoint_name(path),'path':str(path),'verified':verified,
                      'source':source,'selectable':verified or source=='fold'})
    custom=saved_selection().get('custom')
    if isinstance(custom,str):
        path=Path(custom).resolve()
        if path in seen and checkpoint_compatible(path):
            for item in found:
                if item['path']==str(path):item.update(source='custom',selectable=True)
        elif checkpoint_compatible(path):
            found.append({'name':checkpoint_name(path),'path':str(path),'verified':checkpoint_verified(path),'source':'custom','selectable':True})
    return found


def checkpoint_allowed(path):
    path=Path(path).resolve()
    return any(Path(item['path'])==path and item['selectable'] for item in discover_checkpoints())


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
        options=discover_checkpoints()
        selected=next((Path(x['path']) for x in options if x['verified']),
                      next((Path(x['path']) for x in options if x['selectable']),CHECKPOINT.resolve()))
        saved=saved_selection().get('checkpoint')
        try:
            if self.managed_pid():saved=json.loads(RECORD.read_text()).get('checkpoint',saved)
        except (OSError,ValueError):pass
        if isinstance(saved,str) and any(x['path']==str(Path(saved).resolve()) and x['selectable'] for x in options):selected=Path(saved).resolve()
        self.checkpoint=selected
        self.state={'status':'checking','url':URL,'message':'检查本机服务',
                    'checkpoint_name':checkpoint_name(selected),'checkpoint':str(selected),
                    'checkpoint_options':options,'managed':False,'log':None,
                    'default_prompt':checkpoint_prompt(selected)[0],'prompt_source':checkpoint_prompt(selected)[1]}
        self.launch_prompt=self.state['default_prompt']

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

    def update_options(self):
        options=discover_checkpoints()
        self.update(checkpoint_options=options,checkpoint=str(self.checkpoint),
                    checkpoint_name=checkpoint_name(self.checkpoint),default_prompt=checkpoint_prompt(self.checkpoint)[0],prompt_source=checkpoint_prompt(self.checkpoint)[1])
        return options

    def select_checkpoint(self,value,folder=False):
        if not isinstance(value,str) or not value.strip():raise ValueError('请选择 checkpoint')
        if not self.operation.acquire(blocking=False):raise ValueError('Server 正在检查或切换状态，请稍后重试')
        try:
            with self.lock:
                if self.worker and self.worker.is_alive():raise ValueError('Server 正在切换状态')
                if self.state['status'] in {'ready','starting','stopping'}:raise ValueError('请先关闭 Server 再切换 checkpoint')
            if self.managed_pid():raise ValueError('Server 进程仍存在，请先关闭 Server')
            path=Path(value).expanduser().resolve()
            options=discover_checkpoints()
            option=next((item for item in options if Path(item['path'])==path),None)
            if folder:
                if not checkpoint_compatible(path):raise ValueError('文件夹不兼容：请选择包含完整 PI05 权重、预处理、tokenizer 的 pretrained_model 目录；要求 14 维绝对动作和 head/left/right 相机')
                option={'name':checkpoint_name(path),'path':str(path),'verified':checkpoint_verified(path),'source':'custom','selectable':True}
            if option is None:raise ValueError('checkpoint 不在可用目录中')
            if not option['selectable']:raise ValueError('checkpoint 未通过兼容性检查')
            settings=saved_selection();settings['checkpoint']=str(path)
            if folder:settings['custom']=str(path)
            SELECTION.parent.mkdir(parents=True,exist_ok=True)
            tmp=SELECTION.with_suffix('.tmp');tmp.write_text(json.dumps(settings));tmp.replace(SELECTION)
            self.checkpoint=path
            self.update(status='stopped',message=f'已选择 {option["name"]}，下次启动加载',checkpoint=str(path),
                        checkpoint_name=option['name'],checkpoint_options=discover_checkpoints(),managed=False,pid=None,
                        default_prompt=checkpoint_prompt(path)[0],prompt_source=checkpoint_prompt(path)[1])
        finally:self.operation.release()

    def health(self):
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(HEALTH,timeout=.5) as response:meta=json.load(response)
        if (meta.get('service')!='kai0-arx-lerobot-pi05' or meta.get('mode')!='checkpoint'
                or meta.get('checkpoint')!=str(self.checkpoint) or not meta.get('weights_strictly_loaded')):
            raise RuntimeError('8001 服务不是预期的本机 checkpoint server')
        return meta

    def managed_pid(self):
        try:
            record=json.loads(RECORD.read_text());pid=int(record['pid'])
            if process_identity(pid)!=record['start_ticks']:return None
            return pid
        except (OSError,ValueError,KeyError,RuntimeError):return None

    def refresh(self):
        self.update_options()
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

    def start(self,prompt=None):
        with self.lock:
            if self.worker and self.worker.is_alive():return
            selected_prompt=prompt if prompt is not None else self.state.get('default_prompt','')
            if not isinstance(selected_prompt,str) or not selected_prompt.strip():raise ValueError('此 checkpoint 未找到任务描述，请先填写 Prompt')
            self.launch_prompt=selected_prompt.strip()
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
                    if not checkpoint_allowed(self.checkpoint):raise RuntimeError('checkpoint 未通过校验或兼容性检查')
                    if self.managed_pid():raise RuntimeError('受管进程仍存在，请先关闭后重试')
                    with socket.socket() as sock:
                        sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                        try:sock.bind(('127.0.0.1',8001))
                        except OSError as exc:raise RuntimeError('8001 被其他服务占用；未停止它') from exc
                    path=ROOT/'logs'/f'local-server-{time.time_ns()}.log';path.parent.mkdir(exist_ok=True)
                    script=SERVER_ROOT/'local_scripts/start_arx_lerobot_server.sh'
                    with path.open('ab') as log:
                        self.process=subprocess.Popen(['bash',str(script),'--checkpoint',str(self.checkpoint),'--prompt',self.launch_prompt],cwd=SERVER_ROOT,
                                                      stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    # Wait for bash to exec the exact Python script before recording identity.
                    for _ in range(100):
                        if self.process.poll() is not None:raise RuntimeError(f'Server 启动失败；日志：{path}')
                        try:identity=process_identity(self.process.pid);break
                        except (OSError,RuntimeError):time.sleep(.02)
                    else:raise RuntimeError('无法验证启动进程身份')
                    RECORD.parent.mkdir(exist_ok=True)
                    temporary=RECORD.with_suffix('.tmp')
                    temporary.write_text(json.dumps({'pid':self.process.pid,'start_ticks':identity,
                                                     'checkpoint':str(self.checkpoint)}))
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
