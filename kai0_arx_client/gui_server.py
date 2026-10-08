#!/usr/bin/env python3
"""Local ARX console. Starting HTTP never initializes hardware."""
import argparse, collections, fcntl, io, json, os, queue, secrets, signal, subprocess, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import numpy as np
import client
import device_release
from control_runtime import IOLanes, background_close
from local_server import LocalServer
from camera_process import CameraProcess
from dagger_runtime import Dagger
from dagger_data import episode_path
from folder_browser import browse_folders
from replay_runtime import Replay

ROOT = Path(__file__).resolve().parent
STATIC_ROOT=Path(os.environ.get("ARX_GUI_STATIC_ROOT",str(ROOT/"gui/dist"))).resolve()
TAKEOVER_LAST_TARGET_HOLD_S=1.0
TAKEOVER_SETTLE_STABLE_S=.2
TAKEOVER_SETTLE_TIMEOUT_S=.75
ARM_JOINT_INDICES=np.array([*range(6),*range(7,13)])
GRIPPER_DIAGNOSTIC_INTERVAL_S=.1
COLLECTION_SERVICES=('arx-data-station.service','arx-button-control.service')
TATE_SERVICE='tate-arx-ui.service'
CONFLICT_SERVICES=(*COLLECTION_SERVICES,TATE_SERVICE)
ARM_CONTROL_LOCK=Path(os.environ.get('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}'))/'arx-arm-control.lock'

class PauseRequested(Exception):
    pass

class HardwareControlError(RuntimeError):
    pass

class Manager:
    def __init__(self, demo=False):
        self.demo = demo
        self.config = json.loads((ROOT / 'config.json').read_text())
        client.validate_config(self.config)
        self.robot = self.cams = None
        self.images = {}; self.frames = {}; self.state = None
        self.mode = 'disconnected'; self.busy = False; self.connected = False
        self.latched = False; self.error = ''; self.result = None
        self.events = collections.deque(maxlen=150)
        self.jobs = queue.Queue(maxsize=1); self.stop_event = threading.Event()
        self.pause_event = threading.Event(); self.detached = False
        self.hold_target = None; self.hold_started_at = None; self.hold_source = None
        self.gripper_feedback=None;self.gripper_feedback_error='';self.last_gripper_feedback_at=0.
        self.io = IOLanes(); self.camera_failed = False
        self.repair_status={'state':'idle','message':'未执行'}
        self.release_status={'state':'idle','message':'未执行'}
        self.local_server = LocalServer()
        self.server_stop_request = None
        self.dagger = Dagger()
        self.replay = Replay()
        self.local_server.monitor()
        self.exit_pending = False; self.exit_signal = threading.Event()
        self.last_tick_at=None; self.tick_gap_max_s=0.; self.camera_processing_ms=None
        self.shutdown = threading.Event(); self.lock = threading.RLock()
        self.heartbeat = time.monotonic(); self.owner = None; self.observed_at = 0
        self.logpath = ROOT / 'logs' / f'gui-{time.time_ns()}.jsonl'
        self.logpath.parent.mkdir(exist_ok=True)
        self.event('页面服务已启动；机械臂尚未连接')
        self.thread = threading.Thread(target=self.loop, daemon=True); self.thread.start()

    def event(self, message, **extra):
        record = dict(time=time.strftime('%H:%M:%S'), message=message, **extra)
        with self.lock:
            self.events.append(record)
            try:
                with self.logpath.open('a') as f: f.write(json.dumps(record, ensure_ascii=False) + '\n')
            except OSError as exc:
                self.error = f'日志写入失败：{exc}'

    def update_gripper_diagnostics(self):
        active=self.dagger.active and self.dagger.phase=='human' and self.mode=='gravity'
        if not active:
            self.gripper_feedback=None
            self.gripper_feedback_error=''
            return
        now=self.observed_at
        if now-self.last_gripper_feedback_at<GRIPPER_DIAGNOSTIC_INTERVAL_S:return
        reader=getattr(self.robot,'read_gripper_feedback',None)
        if not callable(reader):return
        self.last_gripper_feedback_at=now
        try:
            feedback=reader()
        except Exception as exc:
            message=str(exc)
            if message!=self.gripper_feedback_error:
                self.gripper_feedback_error=message
                self.event('夹爪反馈诊断不可用：'+message,level='warning')
            return
        self.gripper_feedback_error=''
        self.gripper_feedback={'observed_at':now,'arms':feedback}
        self.event('夹爪反馈诊断',grippers=feedback)

    def beat(self, owner):
        with self.lock:
            if self.owner == owner: self.heartbeat = time.monotonic()

    def submit(self, action, data, owner):
        with self.lock:
            if action == 'stop':
                self.dagger.invalidate(); self.dagger.pending=None
                self.stop_event.set(); self.latched = True
                self.event('软件紧急停止已请求；等待 SDK 保护响应')
                return
            if action == 'claim':
                if self.owner or self.busy or self.latched: raise ValueError('当前不可接管')
                if data.get('confirmed') is not True: raise ValueError('需要操作者确认')
                self.owner=owner; self.detached=False; self.heartbeat=time.monotonic()
                return
            if action == 'end_use':
                device_release.validate_request(self, data, owner)
                self.release_status={'state':'running','message':'正在检查并释放设备…'}
                self.busy=True; self.jobs.put_nowait(('end_use',data)); return
            if action in {'repair','release'}:
                if data.get('confirmed') is not True: raise ValueError('需要操作者确认')
                if self.connected or self.robot or self.cams:
                    raise ValueError('请先断开 Kai0 机械臂和相机，再修复连接冲突')
                if self.busy or self.latched or self.dagger.active: raise ValueError('当前有任务或停止锁定，不能自动修复')
                if self.server_stop_request is not None: raise ValueError('Server 正在关闭')
                if self.dagger.task.get('status') in {'running','exporting'}: raise ValueError('数据处理期间不能自动修复')
                # Repair is deliberately allowed to replace a stale page owner while
                # no Kai0 device is connected. It never takes over a live controller.
                self.owner=owner; self.detached=False;self.heartbeat=time.monotonic()
                self.repair_status={'state':'running','message':'正在停止冲突服务并释放设备'}
                self.busy=True;self.jobs.put_nowait(('repair',data));return
            if action.startswith('replay_'):
                return self.submit_replay(action,data,owner)
            if action.startswith('dagger_'):
                return self.submit_dagger(action,data,owner)
            if action == 'pause':
                self.dagger.invalidate(); self.dagger.pending=None
                if not self.connected or self.latched: raise ValueError('当前不可保持')
                if self.owner and self.owner != owner: raise ValueError('另一页面持有控制权')
                self.pause_event.set(); self.event('暂停并保持已请求')
                return
            if action == 'server_stop':
                if self.owner and self.owner != owner: raise ValueError('另一页面持有控制权')
                if self.detached or self.latched: raise ValueError('请先处理控制权或停止锁定')
                if self.busy and self.mode not in {'running','testing'}: raise ValueError('请等待当前操作完成')
                if self.demo: raise ValueError('演示模式不关闭真实 Server')
                self.server_stop_request={'must_hold':self.mode in {'running','testing'} or (self.dagger.active and self.dagger.phase=='human')}
                if self.server_stop_request['must_hold']:
                    self.dagger.invalidate(); self.dagger.pending=None; self.pause_event.set()
                self.event('关闭 Server 已请求；活动任务先暂停并确认保持')
                return
            if action == 'server_checkpoint':
                if self.owner and self.owner != owner: raise ValueError('另一页面持有控制权')
                if self.busy or self.dagger.active: raise ValueError('请先结束当前任务')
                if self.server_stop_request is not None: raise ValueError('正在处理 Server 关闭请求')
                self.local_server.select_checkpoint(data.get('checkpoint'),folder=True) if data.get('folder') is True else self.local_server.select_checkpoint(data.get('checkpoint'))
                self.event('已选择 checkpoint',checkpoint=self.local_server.snapshot()['checkpoint'])
                return
            if self.server_stop_request is not None: raise ValueError('正在处理 Server 关闭请求')
            if action not in {'connect','disconnect','protect','gravity','home','cameras','test','run','server_start'}:
                raise ValueError('未知操作')
            if self.detached and self.connected: raise ValueError('请先接管后台保持；急停始终可用')
            if self.dagger.active and action not in {'disconnect'}: raise ValueError('请先结束或丢弃当前 DAgger 试验')
            if self.dagger.active and action=='disconnect' and self.dagger.phase not in {'paused','error'}:raise ValueError('先暂停并保存 DAgger 试验')
            if self.dagger.task.get('status') in {'running','exporting'} and action in {'connect','run'}:raise ValueError('数据处理期间不能启动机械臂控制')
            if self.busy: raise ValueError('正在执行操作，请先停止')
            if self.owner and self.owner != owner: raise ValueError('另一页面持有控制权')
            if self.latched and action not in {'disconnect'}: raise ValueError('停止已锁定，请先断开，再重新连接')
            if action in {'connect','disconnect','gravity','run'} and data.get('confirmed') is not True:
                raise ValueError('需要操作者确认')
            if action in {'gravity','home','test','run','protect'} and not self.connected:
                raise ValueError('请先连接机械臂')
            if ((action == 'home' and self.mode not in {'protect','holding'}) or
                    (action in {'test','run'} and self.mode not in {'protect','holding'})):
                raise ValueError('归位及推理要求保护或位置保持模式')
            if action in {'test','run'}:
                if self.local_server.snapshot()['status'] in {'starting','stopping'}: raise ValueError('Server 正在切换状态')
                if not self.cams: raise ValueError('请先连接相机')
                steps = data.get('steps',30)
                if type(steps) is not int or steps < 0: raise ValueError('步数须为非负整数；0 表示持续运行')
                chunk_steps=data.get('chunk_steps',0)
                if type(chunk_steps) is not int or chunk_steps<0:raise ValueError('每块执行步数须为非负整数；0 表示使用 checkpoint 默认值')
                if data.get('server') != self.config['server']['url']: raise ValueError('服务地址须与 config.json 一致')
                if not isinstance(data.get('prompt'),str) or not data['prompt'].strip(): raise ValueError('任务描述不能为空')
            if action == 'connect':
                if self.exit_pending: raise ValueError('后台等待退出；请退出后重新启动')
                self.stop_event.clear(); self.pause_event.clear(); self.detached=False; self.owner = owner; self.heartbeat = time.monotonic()
            self.busy = True
            self.jobs.put_nowait((action,data))

    def submit_replay(self,action,data,owner):
        if action not in {'replay_load','replay_select','replay_align','replay_run'}:raise ValueError('未知 Replay 操作')
        if self.owner and self.owner!=owner:raise ValueError('另一页面持有控制权')
        if self.busy or self.latched or self.detached or self.exit_pending:raise ValueError('请先处理当前任务或停止状态')
        if self.server_stop_request or self.dagger.active or self.dagger.task.get('status') in {'running','exporting'}:raise ValueError('先结束采集或数据任务')
        if self.connected and self.mode!='holding':raise ValueError('Replay 要求机械臂处于位置保持')
        if action in {'replay_align','replay_run'}:
            if not self.connected or self.replay.q is None:raise ValueError('请先加载轨迹并连接双臂进入保持')
            if self.replay.root!=self.dagger.root:raise ValueError('保存目录已切换，请重新加载轨迹')
            if self.replay.phase=='complete':raise ValueError('已完成回放，请重新选择片段')
            if data.get('confirmed') is not True:raise ValueError('真机回放需要确认运动路径')
        if action=='replay_select':
            if self.replay.q is None:raise ValueError('请先加载轨迹')
            self.replay.select(data.get('part'));return
        self.busy=True;self.jobs.put_nowait((action,data))

    def submit_dagger(self,action,data,owner):
        d=self.dagger
        if self.owner and self.owner!=owner:raise ValueError('另一页面持有控制权')
        if self.detached and self.connected:raise ValueError('请先接管后台保持')
        if action=='dagger_storage':
            if self.connected or self.busy:raise ValueError('请先断开机械臂，再修改保存路径')
            d.configure_storage(data.get('data_root'));return
        if action in {'dagger_export','dagger_recover','dagger_trash','dagger_inspect'}:
            if self.connected or self.busy or d.active:raise ValueError('请先结束试验并断开机械臂，再处理数据')
            if action=='dagger_trash' and data.get('confirmed') is not True:raise ValueError('回收数据需要确认')
            options=data.get('selection')
            if options is not None and not isinstance(options,dict):raise ValueError('导出选择格式无效')
            d.background_job(action.removeprefix('dagger_'),data.get('episode',''),options);return
        if action in {'dagger_finish','dagger_discard'} and d.active and not self.connected:
            if action=='dagger_discard' and data.get('confirmed') is not True:raise ValueError('丢弃需要确认')
            d.transition('saving','结束已断开设备的试验');d.recorder.finish(data.get('result','unfinished'),action=='dagger_discard');return
        if self.latched:raise ValueError('停止已锁定，请先处理故障')
        if d.pending is not None:raise ValueError('DAgger 正在切换状态')
        if self.server_stop_request is not None:raise ValueError('Server 正在关闭')
        if action in {'dagger_start','dagger_resume'}:
            if self.busy or not self.connected or self.mode!='holding':raise ValueError('开始/恢复要求双臂位置保持')
            if not self.cams or self.camera_failed:raise ValueError('需要三路正常相机')
            if not self.demo and self.local_server.snapshot()['status']!='ready':raise ValueError('Server 尚未就绪')
            if d.task.get('status') in {'running','exporting'}:raise ValueError('数据处理尚未完成')
            if action=='dagger_start':
                if d.active or (d.recorder and not d.recorder.done.is_set()):raise ValueError('先完成当前试验保存')
                if not isinstance(data.get('prompt'),str) or not data['prompt'].strip():raise ValueError('任务描述不能为空')
                if type(data.get('steps')) is not int or data['steps']<0:raise ValueError('步数须为非负整数；0 表示持续运行')
                if type(data.get('chunk_steps',0)) is not int or data.get('chunk_steps',0)<0:raise ValueError('每块执行步数须为非负整数；0 表示使用 checkpoint 默认值')
            elif not d.active or d.phase!='paused':raise ValueError('没有可恢复的试验')
            self.busy=True;self.jobs.put_nowait((action,data));return
        if action not in {'dagger_takeover','dagger_end_correction','dagger_finish','dagger_discard','dagger_pause'}:raise ValueError('未知 DAgger 操作')
        if not d.active:raise ValueError('没有进行中的试验')
        if action=='dagger_takeover' and d.phase not in {'policy','paused'}:raise ValueError('当前不可人工接管')
        if action=='dagger_end_correction' and d.phase!='human':raise ValueError('当前不是人工纠正')
        if action=='dagger_discard' and data.get('confirmed') is not True:raise ValueError('丢弃需要确认')
        if data.get('result','unfinished') not in {'success','failure','unfinished'}:raise ValueError('无效试验结果')
        d.invalidate();d.pending=(action,data);self.pause_event.set()

    def execute_dagger(self,action,data):
        d=self.dagger
        if action=='dagger_start':
            recorder=d.begin(self,data)
            deadline=time.monotonic()+15
            while not recorder.ready.is_set():
                self.tick()
                if recorder.done.is_set():raise RuntimeError(recorder.snapshot().get('error','录制无法启动'))
                if time.monotonic()>deadline:raise RuntimeError('录制进程启动超时')
                time.sleep(.01)
        d.error='';d.invalidate();d.transition('policy','开始策略执行')
        config=dict(server=self.config['server']['url'],prompt=d.metadata['prompt'],steps=d.metadata['steps'],
                    chunk_steps=d.metadata.get('chunk_steps',0))
        self.inference(config,True)

    def handle_dagger_pending(self):
        d=self.dagger
        if d.pending is None:return
        action,data=d.pending;d.pending=None
        self.check_stop()
        if not self.connected or self.mode!='holding':raise RuntimeError('未确认保持，取消 DAgger 切换')
        if action=='dagger_takeover':
            self.busy=True
            try:
                d.transition('transition','保留 SDK 最后目标 1 秒，再进入重力补偿')
                started=time.monotonic()
                self.event('人工接管：保持最后目标 1 秒',hold_target_rad=self.hold_target)
                while time.monotonic()-started<TAKEOVER_LAST_TARGET_HOLD_S:
                    self.tick()
                    time.sleep(min(.01,max(0.,TAKEOVER_LAST_TARGET_HOLD_S-(time.monotonic()-started))))
                self.tick()
                d.transition('transition','锚定实测位置并确认双臂静止')
                if not self.demo:
                    held=self.robot.hold_current()
                    self.state=held.tolist();self.observed_at=time.monotonic()
                    self.hold_target=self.state.copy();self.hold_source='takeover_feedback_rebase';self.hold_started_at=time.monotonic()
                    self.event('人工接管：切换前锚定实测位置',hold_target_rad=self.hold_target)
                    previous=np.asarray(held);stable_since=None;deadline=time.monotonic()+TAKEOVER_SETTLE_TIMEOUT_S
                    while True:
                        self.tick();now=time.monotonic();current=np.asarray(self.state)
                        if np.max(np.abs(current[ARM_JOINT_INDICES]-previous[ARM_JOINT_INDICES]))<=.003:
                            stable_since=stable_since or now
                            if now-stable_since>=TAKEOVER_SETTLE_STABLE_S:break
                        else:stable_since=None
                        if now>=deadline:raise RuntimeError('切换前双臂未稳定，人工接管已取消')
                        previous=current.copy();time.sleep(.01)
                    try:
                        with self.robot.lock:
                            for arm in self.robot.arms:
                                self.check_stop()
                                if arm.gravity_compensation() is not True:raise RuntimeError('SDK 未确认切换成功')
                    except Exception as exc:
                        raise HardwareControlError('双臂重力补偿切换未全部确认：'+str(exc)) from exc
                    self.mode='gravity';self.hold_target=None;self.hold_started_at=None
                self.mode='gravity';self.hold_target=None;self.hold_started_at=None
                d.transition('human','人工拖动纠正，夹爪手动拨动')
            finally:self.busy=False
        elif action in {'dagger_finish','dagger_discard'}:
            d.transition('saving','关闭 episode 写入边界')
            d.recorder.finish(data.get('result','unfinished'),action=='dagger_discard')
        else:d.transition('paused','纠正结束，待手动恢复' if action=='dagger_end_correction' else '暂停试验')
        self.event('DAgger 状态切换',action=action,phase=d.phase)

    def check_stop(self):
        if self.stop_event.is_set(): raise InterruptedError('软件停止已触发')
        if self.exit_signal.is_set():
            self.exit_signal.clear(); self.exit_pending=True
            if self.robot:
                self.detached=True
                raise PauseRequested('请求退出后台：保持双臂，等待接管后归位或确认支撑并断开')
            self.finish_dagger_before_exit()
            raise InterruptedError('无机械臂连接，完成录制保存后退出后台')
        if self.owner and time.monotonic()-self.heartbeat > 2:
            self.detached=True
            raise PauseRequested('页面已离线，停止任务并转后台保持')
        if self.pause_event.is_set(): raise PauseRequested('手动暂停，保持当前位置')

    def enter_hold(self, reason):
        if self.stop_event.is_set(): raise InterruptedError('软件停止优先于保持')
        if not self.robot: raise RuntimeError('没有可保持的设备')
        # Repeated pause / page detach must not make the target follow drift.
        already_holding = self.mode == 'holding'
        if already_holding:
            held=self.robot.read() if self.demo else self.robot.maintain_hold()
        else:
            held=self.robot.read() if self.demo else self.robot.hold_current()
            self.hold_target=held.tolist(); self.hold_started_at=time.monotonic(); self.hold_source="captured_feedback"
        self.state=held.tolist(); self.observed_at=time.monotonic(); self.mode='holding'
        self.event(reason, position_rad=self.state, hold_target_rad=self.hold_target,
                   controller='JOINT', target_update='unchanged' if already_holding else 'once',
                   hold_source=self.hold_source, gripper_included=True)

    def pause_hold(self, reason):
        if self.stop_event.is_set():raise InterruptedError('软件停止优先于保持')
        if self.replay.phase in {'playing','aligning'}:self.replay.phase='paused'
        elif self.replay.phase=='loading':self.replay.phase='empty'
        d=self.dagger
        takeover=d.active and d.pending and d.pending[0]=='dagger_takeover' and not self.detached
        if takeover and self.mode in {'running','holding'}:
            # Keep the last SDK joint/gripper target, do not replace it with feedback.
            held=self.robot.read() if self.demo else self.robot.maintain_hold()
            if self.mode!='holding':
                self.hold_target=list(d.last_action) if d.last_action is not None else None
                self.hold_started_at=time.monotonic();self.hold_source='sdk_last_action'
            self.state=held.tolist();self.observed_at=time.monotonic();self.mode='holding'
            self.event('人工接管：停止新策略动作，保留 SDK 最后目标',hold_target_rad=self.hold_target)
        else:self.enter_hold(reason)
        if d.active:d.transition('paused',reason)
        self.pause_event.clear()
        if self.detached:self.owner=None;self.dagger.pending=None
        while True:
            try:self.jobs.get_nowait()
            except queue.Empty:break
        self.busy=False


    def finish_server_stop(self):
        request=self.server_stop_request
        if request is None:return
        # Clear first: failures must not retry automatically after a fault/reset.
        self.server_stop_request=None
        try:
            self.check_stop()
            if request['must_hold']:
                if not self.connected or self.mode!='holding':
                    raise RuntimeError('未确认机械臂保持，取消关闭 Server')
                self.tick()  # a fresh successful feedback/fault check is required
            self.local_server.stop()
            self.event('已请求关闭 Server', hold_confirmed=bool(request['must_hold']))
        except Exception as exc:
            self.event('Server 未关闭：'+str(exc),level='error')
            raise

    def protect(self):
        self.hold_target=None; self.hold_started_at=None
        if self.robot:
            if not self.demo:
                with self.robot.lock:
                    ok = True
                    for arm in self.robot.arms:
                        try:
                            if arm.protect_mode() is not True: ok = False
                        except Exception: ok = False
                    if not ok:
                        self.mode = 'fault'; self.latched = True; self.stop_event.set()
                        self.event('SDK 未确认双臂保护成功，请使用硬件急停', level='error')
                        return
            self.mode = 'protect'

    def disconnect(self):
        self.gripper_feedback=None;self.gripper_feedback_error='';self.last_gripper_feedback_at=0.
        if self.dagger.active:self.dagger.transition('paused','设备断开，录制暂停')
        self.hold_target=None; self.hold_started_at=None
        if self.cams:
            background_close(self.cams); self.cams = None
        self.frames = {}; self.images = {}
        if self.robot: self.robot.close(); self.robot = None
        self.connected = False; self.state = None; self.mode = 'disconnected'; self.owner = None
        self.last_tick_at=None
        if self.exit_pending:self.finish_dagger_before_exit()

    def finish_dagger_before_exit(self):
        recorder=self.dagger.recorder
        if recorder and not recorder.done.is_set():
            if not recorder.closed:
                self.dagger.transition('saving','退出后台，等待保存完成');recorder.finish('unfinished')
            def wait():
                recorder.done.wait()
                if recorder.snapshot()['status'] in ('saved','discarded'):self.shutdown.set()
                else:self.error='保存失败，后台保留；请恢复或回收未完成数据后退出'
            threading.Thread(target=wait,daemon=True).start()
        else:self.shutdown.set()

    def tick(self):
        self.check_stop()
        if self.robot:
            if self.demo and self.dagger.active and self.dagger.phase=='human':
                self.robot.q[0]=.15*np.sin(time.monotonic()-self.dagger.started)
                self.robot.q[7]=-.12*np.sin(time.monotonic()-self.dagger.started)
            q = self.robot.maintain_hold() if not self.demo else self.robot.read()
            self.state = q.tolist(); self.observed_at = time.monotonic()
            if self.last_tick_at is not None:
                self.tick_gap_max_s=max(self.tick_gap_max_s,self.observed_at-self.last_tick_at)
            self.last_tick_at=self.observed_at
            self.update_gripper_diagnostics()
            self.dagger.sample(self)

    def read_images(self):
        if not self.cams: return
        from PIL import Image
        if self.camera_failed: raise RuntimeError('相机已暂停读取，请断开相机后重新连接')
        cams = self.cams
        try:
            if isinstance(cams, CameraProcess):
                self.images, self.frames, self.camera_processing_ms = self.io.call(
                    'camera', cams.read_preview, self.tick, 1.5)
                return
            self.images = self.io.call('camera', cams.read, self.tick, 1.5)
        except PauseRequested:
            raise
        except Exception:
            self.camera_failed=True
            self.frames={}; self.images={}
            raise
        frames = {}
        started=time.monotonic()
        for name, rgb in self.images.items():
            b = io.BytesIO(); Image.fromarray(rgb).save(b,format='JPEG',quality=70); frames[name]=b.getvalue()
        self.frames = frames
        self.camera_processing_ms=round((time.monotonic()-started)*1000,3)

    def recover_error(self, exc):
        if self.replay.phase in {'playing','aligning','loading'}:
            self.replay.phase='paused' if self.replay.q is not None else 'error';self.replay.error=str(exc)
        was_human=self.dagger.active and self.dagger.phase=='human'
        if self.dagger.active:
            self.dagger.invalidate();self.dagger.pending=None;self.dagger.error=str(exc);self.dagger.transition('paused',str(exc))
            if was_human:self.mode='human'  # healthy upper-layer error must return to JOINT hold
        if self.server_stop_request is not None:
            self.event("控制异常，取消 Server 关闭请求",level="error")
            self.server_stop_request=None
        self.error=str(exc)
        if not self.robot:
            # A constructor failure happens before SDKRobot is assigned. Do not
            # leave that failed page as the permanent owner of a disconnected UI.
            self.owner=None;self.detached=False
        if self.robot and not self.stop_event.is_set() and not isinstance(exc, HardwareControlError):
            try:
                if self.mode in {'protect','gravity'}:
                    # Camera/preview failure must not activate JOINT control from a passive mode.
                    self.state=(self.robot.read() if self.demo else self.robot.maintain_hold()).tolist()
                    self.observed_at=time.monotonic()
                    self.event('上层操作失败，保留当前机械臂模式：'+str(exc), mode=self.mode, level='error')
                    while True:
                        try:self.jobs.get_nowait()
                        except queue.Empty:break
                    self.busy=False
                else:
                    self.pause_hold('上层任务已停止，反馈正常，保持当前位置：'+str(exc))
                return
            except Exception as failed:
                self.error=f'{exc}; 无法保持：{failed}'
        self.event(self.error, level='error')
        if self.robot:
            self.latched=True; self.stop_event.set()
            self.protect(); self.disconnect()
        while True:
            try:self.jobs.get_nowait()
            except queue.Empty:break
        self.busy=False

    def _service_state(self, unit):
        try:
            result=subprocess.run(['systemctl','--user','is-active',unit],capture_output=True,
                                  text=True,timeout=2)
        except (OSError,subprocess.TimeoutExpired):
            return 'unknown'
        return result.stdout.strip() or 'unknown'

    def _arm_lock_available(self):
        ARM_CONTROL_LOCK.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(ARM_CONTROL_LOCK,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(fd,fcntl.LOCK_UN)
            return True
        finally:
            os.close(fd)

    def _video_device_owners(self):
        devices=sorted(Path('/dev').glob('video*'))
        if not devices:return []
        try:
            result=subprocess.run(['lsof','-t',*[str(path) for path in devices]],
                                  capture_output=True,text=True,timeout=3)
        except (OSError,subprocess.TimeoutExpired):
            return ['unknown']
        return sorted(set(result.stdout.split()))

    def _read_local_status(self, url):
        import urllib.request
        with urllib.request.urlopen(url,timeout=1.5) as response:
            return json.load(response)

    def _stop_service_group(self, units, grace_s=6, force=True):
        stopped=subprocess.run(['systemctl','--user','--no-block','stop',*units],
                               capture_output=True,text=True,timeout=4)
        if stopped.returncode:
            raise RuntimeError('无法请求停止服务：'+(stopped.stderr.strip() or '未知错误'))
        deadline=time.monotonic()+grace_s
        while time.monotonic()<deadline:
            states={unit:self._service_state(unit) for unit in units}
            if all(state in {'inactive','failed'} for state in states.values()):return states
            time.sleep(.25)
        states={unit:self._service_state(unit) for unit in units}
        stuck=[unit for unit,state in states.items() if state not in {'inactive','failed'}]
        if stuck and not force:
            raise RuntimeError('控制服务尚未正常退出，未强制结束，请稍后检查')
        for unit in stuck:
            subprocess.run(['systemctl','--user','kill','--signal=SIGKILL',unit],
                           capture_output=True,text=True,timeout=3)
        if stuck:
            subprocess.run(['systemctl','--user','--no-block','stop',*stuck],
                           capture_output=True,text=True,timeout=3)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                states={unit:self._service_state(unit) for unit in units}
                if all(state in {'inactive','failed'} for state in states.values()):return states
                time.sleep(.2)
        states={unit:self._service_state(unit) for unit in units}
        if not all(state in {'inactive','failed'} for state in states.values()):
            raise RuntimeError('服务未全部停止：'+json.dumps(states,ensure_ascii=False))
        return states

    def repair_conflicts(self):
        # External service probes and shutdowns may block for seconds. Never run
        # them while this manager owns a controller that needs feedback ticks.
        with self.lock:
            if self.connected or self.robot or self.cams:
                raise ValueError('请先断开 Kai0 机械臂和相机，再修复连接冲突')
        if self.demo:
            self.repair_status={'state':'success','message':'演示模式无需释放设备'}
            self.owner=None
            return
        self.event('一键修复开始：检查并停止已知冲突控制器')
        try:
            def check_collection():
                states={unit:self._service_state(unit) for unit in COLLECTION_SERVICES}
                if any(state not in {'active','inactive','failed'} for state in states.values()):
                    raise RuntimeError('采集控制服务状态正在变化或不可确认，已拒绝自动停止')
                if not any(state=='active' for state in states.values()):return
                try:
                    station=self._read_local_status('http://127.0.0.1:8090/api/status')
                except Exception as exc:
                    raise RuntimeError('采集服务正在运行但录制状态不可核实，已拒绝自动停止') from exc
                recording=station.get('recording',{}).get('state')
                arm_mode=station.get('arm',{}).get('mode')
                if recording!='idle':raise ValueError('采集服务正在录制，已拒绝自动停止')
                if arm_mode not in {'protect','disconnected','idle'}:
                    raise ValueError(f'采集机械臂模式为 {arm_mode or "未知"}，请先放稳并转保护模式')
                self.event('保存一键修复前采集状态',snapshot=station)

            # Verify every active recorder before stopping any known controller.
            check_collection()

            tate_state=self._service_state(TATE_SERVICE)
            if tate_state not in {'inactive','failed'}:
                try:tate=self._read_local_status('http://127.0.0.1:8089/api/status')
                except Exception as exc:
                    raise RuntimeError('TATE 控制器正在运行但状态不可核实，已拒绝自动停止') from exc
                if tate.get('running') and tate.get('mode')!='holding':
                    raise ValueError(f'TATE 正处于 {tate.get("mode") or "未知"} 模式，仅允许释放已保持的机械臂')
                self.event('检测到 TATE 控制器，已确认无运动或处于位置保持',snapshot=tate)
                # Stop TATE first. Its wrapper may restore the collection units
                # while exiting, so those units are stopped again afterwards.
                self._stop_service_group((TATE_SERVICE,),force=False)

            # TATE may restart collection units on exit. Their new recording
            # state must be checked rather than relying on the earlier snapshot.
            check_collection()
            self._stop_service_group(COLLECTION_SERVICES,force=False)

            subprocess.run(['systemctl','--user','reset-failed',*CONFLICT_SERVICES],
                           capture_output=True,text=True,timeout=3)
            states={unit:self._service_state(unit) for unit in CONFLICT_SERVICES}
            if not all(state=='inactive' for state in states.values()):
                raise RuntimeError('冲突控制服务未全部停止：'+json.dumps(states,ensure_ascii=False))
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                lock_ready=self._arm_lock_available()
                if lock_ready and not self._video_device_owners():break
                time.sleep(.2)
            if not self._arm_lock_available():
                raise RuntimeError('已知服务已停止，但机械臂锁仍被未知控制器占用')
            video_owners=self._video_device_owners()
            if video_owners:
                raise RuntimeError('已知服务已停止，但相机仍被其他进程占用：'+','.join(video_owners))
            self.error=''
            self.repair_status={'state':'success','message':'冲突服务已停止，机械臂锁和相机已释放'}
            self.event('一键修复完成：可重新连接机械臂和相机',services=states)
        except Exception as exc:
            self.repair_status={'state':'error','message':str(exc)}
            raise
        finally:
            # The disconnected-only contract makes clearing stale ownership safe.
            with self.lock:
                self.owner=None
                self.detached=False

    def loop(self):
        last_camera = 0
        last_timing = time.monotonic()
        while not self.shutdown.is_set():
            try:
                if not self.robot and self.exit_signal.is_set():
                    self.exit_signal.clear();self.exit_pending=True;self.finish_dagger_before_exit()
                    if self.shutdown.is_set():break
                if self.robot: self.tick()
                self.handle_dagger_pending()
                self.finish_server_stop()
                if self.robot and time.monotonic()-last_timing >= 5:
                    self.event('控制循环时间诊断（不是 SDK 电机线程周期）',
                               tick_gap_max_ms=round(self.tick_gap_max_s*1000,3),
                               camera_processing_ms=self.camera_processing_ms, mode=self.mode,
                               cameras=bool(self.cams))
                    self.tick_gap_max_s=0.; last_timing=time.monotonic()
                if self.cams and not self.camera_failed and not self.io.pending('camera') and time.monotonic()-last_camera > .25:
                    self.read_images(); last_camera=time.monotonic()
                try: action,data=self.jobs.get(timeout=.01 if self.dagger.active else .04)
                except queue.Empty: continue
                try:
                    if action not in {'disconnect','end_use'}: self.check_stop()
                    self.execute(action,data)
                    self.event('操作完成：'+action)
                finally:
                    with self.lock: self.busy=False
            except PauseRequested as e:
                try: self.pause_hold(str(e))
                except Exception as failed:
                    self.server_stop_request=None;self.dagger.pending=None
                    self.error=str(failed); self.latched=True; self.stop_event.set()
                    self.event('无法保持，转 SDK 保护：'+str(failed), level='error')
                    self.protect(); self.disconnect(); self.busy=False
            except Exception as e:
                self.recover_error(e)
                time.sleep(.05)
        self.disconnect()

    def execute(self, action, data):
        self.error=''
        if action == 'end_use':
            return device_release.release(self, CONFLICT_SERVICES, TATE_SERVICE, ARM_CONTROL_LOCK)
        if action.startswith('replay_'):
            if action=='replay_load':return self.replay.load(self,data.get('episode',''))
            if action=='replay_align':return self.replay.align(self)
            if action=='replay_run':return self.replay.run(self,data.get('speed',.5))
        if action in {'dagger_start','dagger_resume'}:
            return self.execute_dagger(action,data)
        if action=='server_start':
            if self.demo: raise ValueError('演示模式不启动真实 server')
            self.local_server.start(prompt=data.get('prompt'))
            self.event('已请求启动本机推理 server；不操作机械臂')
        elif action=='connect':
            if self.robot: raise ValueError('已经连接')
            self.robot = client.MockRobot() if self.demo else client.SDKRobot(self.config)
            self.connected=True; self.mode='protect'; self.tick()
            if not self.demo: self.event('SDK 限位与平滑配置', configuration=json.loads(json.dumps(self.robot.sdk_motion_limits, default=str)))
        elif action=='disconnect':
            self.disconnect(); self.latched=False; self.stop_event.clear()
        elif action=='protect': self.protect()
        elif action=='gravity':
            self.hold_target=None; self.hold_started_at=None
            if not self.demo:
                for arm in self.robot.arms:
                    self.check_stop()
                    if arm.gravity_compensation() is not True: raise HardwareControlError('SDK 拒绝重力补偿')
            self.mode='gravity'
        elif action=='home':
            # Exact SDK demo call, left then right; worker keeps UI stop handling responsive.
            self.mode='homing'
            self.hold_target=None; self.hold_started_at=None
            for index in range(2):
                self.check_stop()
                if self.demo:
                    self.robot.q[index*7:index*7+6]=0
                    continue
                errors=[]
                def home_one(arm=self.robot.arms[index]):
                    try:
                        if arm.go_home(1, wait=True) is False: raise HardwareControlError('SDK 拒绝归位')
                    except BaseException as exc: errors.append(exc)
                worker=threading.Thread(target=home_one,daemon=True)
                worker.start(); deadline=time.monotonic()+20
                while worker.is_alive():
                    self.tick()
                    if time.monotonic()>deadline: raise HardwareControlError('SDK 归位等待超时')
                    worker.join(.05)
                self.check_stop()
                if errors: raise HardwareControlError(str(errors[0])) from errors[0]
            # go_home already owns the position/gripper targets. Read feedback only;
            # do not turn residual tracking error into a new position command.
            self.tick()
            self.hold_target=None  # SDK target is not exposed here; do not label feedback as target.
            self.hold_source='sdk_home'; self.hold_started_at=time.monotonic()
            self.mode='holding'
            self.event('双臂归位完成，保留 SDK 归位目标', position_rad=self.state,
                       hold_target_rad=None, hold_source='sdk_home', controller='JOINT',
                       target_update='unchanged', gripper_included=True)
        elif action=='cameras':
            if self.cams:
                if self.io.pending('camera'): raise RuntimeError('相机读取尚未结束，等待后重试断开')
                self.io.call('camera', self.cams.close, self.tick, 5)
                self.cams=None; self.frames={}; self.images={}; self.camera_failed=False
            else:
                self.cams=self.io.call('camera', lambda: CameraProcess(self.config,real=not self.demo),
                                      self.tick, 10, dispose=lambda c: c.close() if c else None)
                self.camera_failed=False
                self.event('相机已连接', camera_backend='synthetic' if self.demo else 'separate_process',
                           camera_pid=getattr(self.cams,'pid',None))
                self.read_images()
        elif action=='repair':self.repair_conflicts()
        elif action in ('test','run'): self.inference(data,action=='run')

    def inference(self,data,motion):
        previous_mode=self.mode
        generation=self.dagger.generation
        if motion: self.hold_target=None; self.hold_started_at=None
        self.mode='running' if motion else 'testing'; done=0; all_violations=[]
        request_trace=[]
        policy=None;execution_steps=client.policy_chunk_count({},self.config,data.get('chunk_steps',0))
        try:
            if not self.demo:
                policy=self.io.call('policy', lambda: client.Policy(data['server'],self.config['server']['timeout_s']),
                                    self.tick, self.config['server']['timeout_s'],
                                    dispose=lambda p: p.close() if p else None)
                meta=policy.metadata
                if motion and (meta.get('test_only') is not False or meta.get('motion_enabled') is not True
                               or meta.get('action_dim') != 14 or meta.get('mode')!='checkpoint'):
                    raise ValueError('服务元数据不允许真机执行')
                execution_steps=client.policy_chunk_count(meta,self.config,data.get('chunk_steps',0))
                self.event('推理服务已连接',metadata=meta,actions_per_chunk=execution_steps)
            while data['steps']==0 or done<data['steps']:
                self.check_stop(); observed=time.monotonic()
                current=self.robot.read(); self.read_images()
                obs=client.make_observation(current,self.images,data['prompt'])
                actions=np.tile(current,(8,1)) if self.demo else self.io.call(
                    'policy', lambda: policy.infer(obs), self.tick,
                    max(.001,self.config['safety']['max_observation_age_s']-(time.monotonic()-observed)),
                    dispose=lambda _: policy.close())
                if generation!=self.dagger.generation:raise PauseRequested('推理代次已失效')
                self.event('模型预测',motion=motion,actions=actions.tolist(),state=current.tolist())
                if self.dagger.active:self.dagger.recorder.send('event',{'timestamp':time.monotonic(),'phase':'prediction','actions':actions.tolist()})
                self.check_stop()
                age=time.monotonic()-observed
                if age>self.config['safety']['max_observation_age_s']:raise TimeoutError('推理观测超过 1.5 秒')
                count=min(len(actions),execution_steps) if motion else 1
                if motion and data['steps']>0:count=min(count,data['steps']-done)
                # No-motion test inspects the entire predicted chunk, never calls command().
                if not motion:
                    for index,a in enumerate(actions):
                        all_violations.extend(dict(horizon=index, **x) for x in client.diagnostic_violations(a, self.config))
                    done=1
                else:
                    chunk_started=time.monotonic()
                    for index,a in enumerate(actions[:count]):
                        client.wait_policy_frame(index,chunk_started,self.config,self.tick)
                        self.check_stop()
                        with self.lock:
                            self.check_stop()
                            if generation!=self.dagger.generation:raise PauseRequested('旧动作块已失效')
                            if time.monotonic()-(chunk_started+index/self.config['control']['fps'])>.25:raise TimeoutError('动作下发延迟超过250ms，停止且不补发')
                            command_at=time.monotonic()
                            if not self.demo:self.robot.command(a)
                            else:self.robot.q=a.copy()
                        request_trace.append(dict(step=done,monotonic_s=command_at,
                                                  sdk_call_ms=round((time.monotonic()-command_at)*1000,3),target_rad=a.tolist()))
                        self.dagger.last_action=a.tolist();self.dagger.policy_action=a.tolist()
                        done+=1; self.state=self.robot.read().tolist(); self.observed_at=time.monotonic()
                    client.wait_policy_frame(count,chunk_started,self.config,self.tick)
                self.result=dict(motion=motion,steps=done,actions_per_chunk=count,shape=list(actions.shape),round_trip_ms=round(age*1000,1),violations=all_violations)
                self.event('动作块完成' if motion else '无运动推理完成', result=self.result, violations=all_violations)
                if request_trace:
                    self.event('SDK 动作请求记录',requests=request_trace)
                    if self.dagger.active:self.dagger.recorder.send('event',{'timestamp':time.monotonic(),'phase':'command_requests','requests':request_trace})
                    request_trace=[]
                if not motion:break
            self.check_stop()
            if motion:
                if self.dagger.active:self.dagger.transition('paused','VLA 步数完成')
                self.enter_hold('VLA 已完成，JOINT 位置保持；后台服务须继续运行')
            else:
                self.mode=previous_mode
        except PauseRequested:
            raise
        except Exception as exc:
            self.recover_error(exc)
            raise
        finally:
            if policy and not self.io.pending('policy'): background_close(policy)
            if request_trace:
                self.event('SDK 动作请求记录',requests=request_trace)
                if self.dagger.active:
                    self.dagger.recorder.send('event',{'timestamp':time.monotonic(),'phase':'command_requests','requests':request_trace})

    def snapshot(self):
        with self.lock:
            return dict(demo=self.demo,connected=self.connected,cameras=bool(self.cams),mode=self.mode,
                        hold_target_rad=self.hold_target if self.mode=='holding' else None,
                        hold_source=self.hold_source if self.mode=='holding' else None,
                        hold_elapsed_s=round(time.monotonic()-self.hold_started_at,1) if self.mode=='holding' and self.hold_started_at is not None else None,
                        busy=self.busy,detached=self.detached,latched=self.latched,error=self.error,state=self.state,
                        exit_pending=self.exit_pending,camera_failed=self.camera_failed,repair=self.repair_status,release=self.release_status,
                        gripper_feedback=self.gripper_feedback,
                        dagger=self.dagger.snapshot(),replay=self.replay.snapshot(),
                        local_server=self.local_server.snapshot(), server_stop_pending=self.server_stop_request is not None,
                        tick_gap_max_ms=round(self.tick_gap_max_s*1000,3),camera_processing_ms=self.camera_processing_ms,
                        age_s=round(time.monotonic()-self.observed_at,2) if self.state else None,
                        limits=self.config['safety'],server=self.config['server']['url'],prompt=self.local_server.snapshot().get('default_prompt',''),
                        events=list(self.events)[-40:],result=self.result,log=str(self.logpath))

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def send(self,body,code=200,ctype='application/json'):
        if ctype=='application/json':body=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(code);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body)
    def valid_host(self):
        return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}', f'192.168.2.136:{self.server.server_port}'}
    def do_GET(self):
        if not self.valid_host(): return self.send({'error':'Host denied'},403)
        path=urlparse(self.path).path
        if path=='/api/device-ownership':
            return self.send(device_release.ownership(self.server.manager, ARM_CONTROL_LOCK))
        if path=='/api/state':return self.send(self.server.manager.snapshot())
        if path=='/api/session':return self.send({'token':self.server.token})
        if path=='/api/dagger/folders':
            if self.headers.get('X-Control-Token')!=self.server.token:return self.send({'error':'Token required'},403)
            try:return self.send(browse_folders(parse_qs(urlparse(self.path).query).get('path',[None])[0],self.server.manager.dagger.root))
            except (ValueError,OSError) as exc:return self.send({'error':str(exc)},400)
        if path.startswith('/api/dagger/episode/'):
            try:
                parts=path.removeprefix('/api/dagger/episode/').split('/')
                directory=episode_path(parts[0],self.server.manager.dagger.root)
                if len(parts)==1:return self.send(json.loads((directory/'manifest.json').read_text()))
                if len(parts)!=2 or parts[1] not in {'head.mp4','left.mp4','right.mp4'}:raise ValueError('无效视频')
                video=directory/parts[1];size=video.stat().st_size;start=0;end=size-1
                requested=self.headers.get('Range')
                if requested:
                    import re
                    match=re.fullmatch(r'bytes=(\d+)-(\d*)',requested)
                    if not match:return self.send({'error':'Range invalid'},416)
                    start=int(match[1]);end=min(int(match[2]) if match[2] else end,end)
                    if start>end:return self.send({'error':'Range invalid'},416)
                self.send_response(206 if requested else 200)
                self.send_header('Content-Type','video/mp4');self.send_header('Accept-Ranges','bytes')
                if requested:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.send_header('Content-Length',str(end-start+1));self.end_headers()
                with video.open('rb') as f:
                    f.seek(start);remaining=end-start+1
                    while remaining:
                        block=f.read(min(remaining,256*1024))
                        if not block:break
                        self.wfile.write(block);remaining-=len(block)
                return
            except (ValueError,OSError) as exc:return self.send({'error':str(exc)},404)
        if path.startswith('/api/camera/'):
            b=self.server.manager.frames.get(path.rsplit('/',1)[-1]);return self.send(b or b'',200 if b else 404,'image/jpeg')
        p=(STATIC_ROOT/('index.html' if path=='/' else path.lstrip('/'))).resolve()
        if not p.is_relative_to(STATIC_ROOT) or not p.is_file():return self.send({'error':'Not found'},404)
        import mimetypes
        self.send(p.read_bytes(),ctype=mimetypes.guess_type(p)[0] or 'application/octet-stream')
    def do_POST(self):
        if not self.valid_host(): return self.send({'error':'Host denied'},403)
        try:
            origin=self.headers.get('Origin'); host=self.headers.get('Host')
            if origin and origin!=f'http://{host}':return self.send({'error':'Origin denied'},403)
            if self.headers.get('X-Control-Token')!=self.server.token:return self.send({'error':'Token required'},403)
            size=int(self.headers.get('Content-Length',0))
            if size>8192:raise ValueError('Request too large')
            data=json.loads(self.rfile.read(size));owner=self.headers.get('X-Control-Owner','')
            if not owner:raise ValueError('Missing owner')
            if self.path=='/api/heartbeat':self.server.manager.beat(owner)
            elif self.path=='/api/action':self.server.manager.submit(data.pop('action'),data,owner)
            else:return self.send({'error':'Not found'},404)
            self.send({'ok':True})
        except Exception as e:self.send({'error':str(e)},409)

def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8092);p.add_argument('--demo',action='store_true');p.add_argument('--host',default='0.0.0.0');a=p.parse_args()
    manager=Manager(a.demo);server=ThreadingHTTPServer((a.host,a.port),Handler)
    server.manager=manager;server.token=secrets.token_urlsafe(32)
    def terminate(*_):
        manager.exit_signal.set()
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,terminate)
    def wait_shutdown():
        manager.shutdown.wait(); server.shutdown()
    threading.Thread(target=wait_shutdown,daemon=True).start()
    print(f'GUI READY http://{a.host}:{a.port} demo={a.demo}',flush=True)
    try:server.serve_forever()
    finally:manager.shutdown.set();manager.stop_event.set();manager.thread.join(8);server.server_close()
if __name__=='__main__':main()
