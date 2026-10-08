"""Explicit device release; never changes CAN configuration or starts controllers."""
import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path

LABELS = {
    'arx-button-control.service': '8090 按钮控制程序',
    'tate-arx-ui.service': 'TATE 控制程序',
}
SAFE_MODES = {'idle', 'disconnected', 'protect', 'holding', 'fault'}


def ownership(manager, lock_path):
    if manager.demo:
        return {'label': '本页面（演示）' if manager.robot else '空闲（演示）', 'known': True}
    try:
        result = subprocess.run(['lslocks', '--json', '-o', 'PID,PATH,COMMAND'],
                                capture_output=True, text=True, timeout=2, check=True)
        holders = [row for row in json.loads(result.stdout).get('locks', [])
                   if row.get('path') == str(lock_path)]
        if not holders:
            return {'label': '空闲', 'known': True, 'pid': None}
        row = holders[0]
        pid = int(row['pid'])
        if pid == os.getpid():
            return {'label': '8092 本页面控制程序', 'known': True, 'pid': pid}
        cgroup = Path(f'/proc/{pid}/cgroup').read_text()
        unit = next((unit for unit in LABELS
                     if any(unit in line.split('/') for line in cgroup.splitlines())), None)
        return {'label': LABELS.get(unit, '其他控制程序'), 'known': unit is not None,
                'pid': pid, 'service': unit}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'label': '占用状态暂不可确认', 'known': False, 'pid': None}


def validate_request(manager, data, owner):
    if data.get('confirmed') is not True or data.get('supported') is not True:
        raise ValueError('请确认双臂已放稳或有支撑，再释放设备')
    if manager.robot and manager.owner and manager.owner != owner:
        raise ValueError('另一页面持有控制权，请先在原页面释放')
    if manager.robot and manager.detached:
        raise ValueError('请先接管后台保持，再释放设备')
    if manager.busy or manager.server_stop_request is not None:
        raise ValueError('请先暂停当前操作并等待完成')
    if manager.robot and manager.mode not in SAFE_MODES:
        raise ValueError('请先暂停并保持，或进入保护模式')
    recorder = manager.dagger.recorder
    if manager.dagger.active or (recorder and not recorder.done.is_set()):
        raise ValueError('请先结束并保存或明确丢弃当前录制，等待保存完成')
    if manager.dagger.task.get('status') in {'running', 'exporting'}:
        raise ValueError('请等待数据处理完成')


def preflight(manager, units, tate_unit, lock_path):
    states = {unit: manager._service_state(unit) for unit in units}
    if any(state not in {'active', 'inactive', 'failed'} for state in states.values()):
        raise RuntimeError('控制服务状态正在变化或不可确认，请稍后再试')
    current = ownership(manager, lock_path)
    if not current['known']:
        raise RuntimeError('控制锁由未知程序占用或无法核实，未停止任何服务')
    if states['arx-data-station.service'] == 'active' or states['arx-button-control.service'] == 'active':
        try:
            station = manager._read_local_status('http://127.0.0.1:8090/api/status')
        except Exception as exc:
            raise RuntimeError('无法核实 8090 的录制状态，未停止任何服务') from exc
        if station.get('recording', {}).get('state') != 'idle':
            raise ValueError('8090 正在录制、保存或状态未知，请先完成保存')
        if states['arx-button-control.service'] == 'active':
            arm = station.get('arm', {})
            if not arm.get('online') or not isinstance(arm.get('age_ms'), (int, float)) or arm['age_ms'] > 2000:
                raise ValueError('按钮控制程序状态过期，无法确认可释放')
            if arm.get('mode') not in SAFE_MODES:
                raise ValueError('8090 仍处于活动控制模式，请先暂停或进入保护模式')
        manager.event('手动释放前采集状态', snapshot=station)
    if states[tate_unit] == 'active':
        try:
            tate = manager._read_local_status('http://127.0.0.1:8089/api/status')
        except Exception as exc:
            raise RuntimeError('无法核实 TATE 状态，未停止任何服务') from exc
        if tate.get('mode') not in SAFE_MODES:
            raise ValueError('TATE 仍处于活动控制模式，请先暂停并保持')
        manager.event('手动释放前 TATE 状态', snapshot=tate)
    return states


def release(manager, units, tate_unit, lock_path):
    try:
        if manager.demo:
            states = {}
        elif manager.robot:
            states = manager.io.call('release_check',
                                     lambda: preflight(manager, units, tate_unit, lock_path),
                                     manager.tick, 20)
        else:
            states = preflight(manager, units, tate_unit, lock_path)
        # Every external recording/motion check completes before any shutdown.
        manager.disconnect()
        if not manager.demo:
            if states[tate_unit] == 'active':
                manager._stop_service_group((tate_unit,), force=False)
            # TATE's exit wrapper can restore collection services; check again.
            states = preflight(manager, units, tate_unit, lock_path)
            if states['arx-data-station.service'] == 'active':
                request = urllib.request.Request('http://127.0.0.1:8090/api/devices/release',
                    data=json.dumps({'confirmed': True, 'supported': True}).encode(),
                    headers={'Content-Type': 'application/json'}, method='POST')
                try:
                    with urllib.request.urlopen(request, timeout=35) as response:
                        result = json.load(response)
                except urllib.error.HTTPError as exc:
                    detail = json.loads(exc.read()).get('detail', str(exc))
                    raise RuntimeError('8090 未完成释放：' + str(detail)) from exc
                if result.get('state') != 'released':
                    raise RuntimeError('8090 未确认设备释放成功')
            elif states['arx-button-control.service'] == 'active':
                raise RuntimeError('8090 页面不可用，无法安全核实并释放按钮控制')
            if any(manager._service_state(unit) not in {'inactive', 'failed'}
                   for unit in (tate_unit, 'arx-button-control.service')):
                raise RuntimeError('仍有控制服务运行，未确认释放成功')
            deadline = time.monotonic() + 3
            while True:
                lock_free = manager._arm_lock_available()
                video_owners = manager._video_device_owners()
                if lock_free and not video_owners:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('设备仍被占用，未确认释放成功；请查看占用状态')
                time.sleep(.2)
        manager.latched = False
        manager.stop_event.clear()
        manager.pause_event.clear()
        manager.detached = False
        manager.error = ''
        manager.release_status = {'state': 'success', 'message': '设备已释放，可以关闭页面或切换控制程序'}
        manager.event('结束使用：控制程序已释放，CAN 配置保持不变')
    except Exception as exc:
        manager.release_status = {'state': 'error', 'message': str(exc)}
        # Report a refused release without the generic handler changing live hardware.
        manager.error = str(exc)
        manager.event('释放未完成：' + str(exc), level='error')
