import io
import os
import tempfile
import threading
import time
import copy
import argparse
import json
import signal
import socket
import subprocess
import sys
import urllib.request
import unittest
from unittest.mock import Mock, patch

import client
import gui_server as gui
from client_control import Control
from control_runtime import IOLanes
import test_gui as fixtures
from test_gui import Policy
from test_client import FakeArm, C


class RuntimeTests(unittest.TestCase):
    def test_cli_term_and_hup_cancel_blocked_inference_then_hold(self):
        for sig in (signal.SIGTERM,signal.SIGHUP):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'XDG_RUNTIME_DIR':root}):
                robot=Mock();robot.read.return_value=client.MockRobot().q;robot.sdk_motion_limits=[]
                policy=Mock();policy.metadata={'test_only':False}
                release=threading.Event()
                def infer(_):
                    os.kill(os.getpid(),sig);release.wait(1)
                    return __import__('numpy').zeros((1,14))
                policy.infer.side_effect=infer
                cams=Mock();cams.read.return_value=client.Cameras(C).read()
                args=argparse.Namespace(enable_motion=True,robot='sdk',cameras='realsense',steps=30,
                                        prompt='test',server='ws://unused')
                def held(*a,**kw):
                    robot.close.assert_not_called();release.set()
                try:
                    with patch.object(client,'Policy',return_value=policy),patch.object(client,'Cameras',return_value=cams), \
                         patch.object(client,'SDKRobot',return_value=robot),patch.object(client,'cli_hold_until_exit',side_effect=held) as hold:
                        client.run(args,C)
                    hold.assert_called_once();robot.command.assert_not_called()
                finally:release.set()

    def test_cli_inference_error_enters_hold_before_cleanup(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'XDG_RUNTIME_DIR':root}):
            robot=Mock();robot.read.return_value=client.MockRobot().q;robot.sdk_motion_limits=[]
            policy=Mock();policy.metadata={'test_only':False};policy.infer.side_effect=ConnectionError('server lost')
            cams=Mock();cams.read.return_value=client.Cameras(C).read()
            args=argparse.Namespace(enable_motion=True,robot='sdk',cameras='realsense',steps=30,
                                    prompt='test',server='ws://unused')
            def held(*a,**kw):
                robot.close.assert_not_called()
                self.assertIs(a[0],robot)
            with patch.object(client,'Policy',return_value=policy),patch.object(client,'Cameras',return_value=cams), \
                 patch.object(client,'SDKRobot',return_value=robot),patch.object(client,'cli_hold_until_exit',side_effect=held) as hold:
                client.run(args,C)
            hold.assert_called_once();robot.command.assert_not_called()

    def test_feedback_maintenance_survives_io_longer_than_watchdog(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'XDG_RUNTIME_DIR':root}):
            config=copy.deepcopy(C);config['safety']['watchdog_s']=.15
            robot=client.SDKRobot(config,FakeArm)
            try:
                IOLanes().call('slow',lambda:time.sleep(.35),robot.maintain_hold,1)
                self.assertFalse(robot.tripped.is_set())
                self.assertTrue(all(a.protected==1 for a in robot.arms))
                self.assertTrue(all(not a.targets for a in robot.arms))
            finally:robot.close()

    def test_motor_fault_interrupts_blocked_io_and_cannot_hold(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'XDG_RUNTIME_DIR':root}):
            robot=client.SDKRobot(C,FakeArm);release=threading.Event()
            def work():
                robot.arms[0].fault='CAN feedback timeout'
                release.wait(1)
            try:
                with self.assertRaisesRegex(RuntimeError,'CAN feedback timeout'):
                    IOLanes().call('policy',work,robot.maintain_hold,.5)
                with self.assertRaises(RuntimeError):robot.hold_current()
                self.assertTrue(all(not a.targets for a in robot.arms))
            finally:
                release.set();robot.close()

    def test_blocked_io_still_ticks_and_discards_late_result(self):
        release=threading.Event(); closed=threading.Event(); ticks=[]
        lanes=IOLanes()
        try:
            with self.assertRaises(TimeoutError):
                lanes.call('policy',lambda:release.wait(1),lambda:ticks.append(time.monotonic()),
                           .08,dispose=lambda _:closed.set())
            self.assertGreaterEqual(len(ticks),3)
            with self.assertRaisesRegex(RuntimeError,'尚未结束'):
                lanes.call('policy',lambda:None,lambda:None,1)
        finally: release.set()
        self.assertTrue(closed.wait(1))

    def test_cli_eof_keeps_hold_until_local_release(self):
        r=Mock(); r.hold_current.return_value=client.MockRobot().q
        control=Mock(); control.stop=threading.Event(); control.release=threading.Event()
        calls=[]
        def maintain():
            calls.append(1)
            if len(calls)==3: control.release.set()
        r.maintain_hold.side_effect=maintain
        readfd,writefd=os.pipe();os.close(writefd)
        log=io.StringIO()
        with os.fdopen(readfd,'rb') as stream:
            client.cli_hold_until_exit(r,log,threading.Event(),stream,control)
        r.close.assert_not_called()
        self.assertIn('terminal_closed_holding',log.getvalue())
        self.assertEqual(len(calls),3)

    def test_control_requires_support_confirmation_and_holding(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'XDG_RUNTIME_DIR':root}):
            ctl=Control(threading.Event())
            try:
                with self.assertRaises(ValueError):ctl.handle({'action':'release','supported':True})
                ctl.holding=True
                with self.assertRaises(ValueError):ctl.handle({'action':'release'})
                ctl.handle({'action':'release','supported':True})
                self.assertTrue(ctl.release.is_set())
            finally:ctl.close()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.GuiTests();fixture.setUp()
        self.m=fixture.m;self.data=fixture.data

    def test_model_connection_error_preserves_existing_hold(self):
        m=self.m; m.pause_hold('start')
        robot=m.robot
        with patch.object(client,'Policy',side_effect=ConnectionError('server down')):
            with self.assertRaises(ConnectionError):m.inference(self.data,True)
        self.assertEqual(m.mode,'holding')
        self.assertTrue(all(a.protected==0 for a in robot.arms))

    def test_camera_failure_stops_task_and_holds(self):
        m=self.m
        m.pause_hold('already holding')
        with patch.object(m.cams,'read',side_effect=RuntimeError('camera unplugged')):
            try:m.read_images()
            except Exception as exc:m.recover_error(exc)
        self.assertTrue(m.camera_failed)
        self.assertEqual(m.mode,'holding')
        self.assertTrue(all(a.protected==0 for a in m.robot.arms))

    def test_camera_failure_does_not_activate_joint_from_protect_or_gravity(self):
        for mode in ('protect','gravity'):
            self.m.mode=mode
            self.m.recover_error(RuntimeError('camera busy'))
            self.assertEqual(self.m.mode,mode)
            self.assertEqual(self.m.robot.commands,[])

    def test_exit_signal_during_blocked_prediction_keeps_process_and_holds(self):
        m=self.m; release=threading.Event(); closed=threading.Event()
        class BlockedPolicy(Policy):
            def infer(self,obs):
                m.exit_signal.set();release.wait(1)
                return super().infer(obs)
            def close(self):closed.set()
        try:
            with patch.object(client,'Policy',BlockedPolicy):
                with self.assertRaises(gui.PauseRequested):m.inference(self.data,True)
            m.pause_hold('exit requested')
            self.assertTrue(m.exit_pending)
            self.assertFalse(m.shutdown.is_set())
            self.assertEqual(m.mode,'holding')
            self.assertTrue(m.detached)
            self.assertEqual(len(m.robot.commands),1)
        finally:release.set()
        self.assertTrue(closed.wait(1))

    def test_repeated_exit_signal_does_not_release(self):
        m=self.m
        for _ in range(2):
            m.exit_signal.set()
            with self.assertRaises(gui.PauseRequested):m.check_stop()
            m.pause_hold('exit requested')
        self.assertFalse(m.shutdown.is_set())
        self.assertEqual(len(m.robot.commands),1)
        m.disconnect()
        self.assertTrue(m.shutdown.is_set())

    def test_hardware_failure_never_forces_hold(self):
        m=self.m;robot=m.robot
        with patch.object(robot,'maintain_hold',side_effect=RuntimeError('motor fault')), \
             patch.object(robot,'hold_current',side_effect=RuntimeError('motor fault')):
            m.recover_error(RuntimeError('feedback lost'))
        self.assertTrue(m.latched)
        self.assertIsNone(m.robot)
        self.assertTrue(all(a.protected for a in robot.arms))
        self.assertEqual(robot.commands,[])

    def test_stopped_client_cannot_recover_to_hold(self):
        m=self.m;robot=m.robot;m.stop_event.set()
        m.recover_error(ConnectionError('server lost'))
        self.assertEqual(robot.commands,[])
        self.assertTrue(all(a.protected for a in robot.arms))


class ProcessTests(unittest.TestCase):
    def test_gui_sigterm_holds_until_explicit_disconnect_demo_only(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        proc=subprocess.Popen([sys.executable,str(client.ROOT/'gui_server.py'),'--demo','--port',str(port)],
                              stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        base=f'http://127.0.0.1:{port}'
        def get(path):
            with urllib.request.urlopen(base+path,timeout=1) as response:return json.load(response)
        def eventually(fn,timeout=4):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                try:
                    value=fn()
                    if value:return value
                except (OSError,ValueError):pass
                time.sleep(.03)
            self.fail('Demo GUI did not reach expected state')
        try:
            token=eventually(lambda:get('/api/session'))['token']
            def post(action):
                req=urllib.request.Request(base+'/api/action',data=json.dumps({'action':action,'confirmed':True}).encode(),
                    headers={'Content-Type':'application/json','X-Control-Token':token,'X-Control-Owner':'test-owner'})
                with urllib.request.urlopen(req,timeout=1) as response:return json.load(response)
            post('connect');eventually(lambda:get('/api/state')['connected'])
            proc.send_signal(signal.SIGTERM)
            def held():
                s=get('/api/state')
                return s if s['exit_pending'] and s['mode']=='holding' and not s['busy'] else None
            eventually(held)
            self.assertIsNone(proc.poll())
            proc.send_signal(signal.SIGTERM)
            eventually(lambda:sum('请求退出后台' in e['message'] for e in get('/api/state')['events'])>=2)
            eventually(held)
            post('claim');post('home')
            eventually(lambda:get('/api/state')['mode']=='holding' and not get('/api/state')['busy'])
            post('disconnect')
            self.assertEqual(proc.wait(timeout=4),0)
        finally:
            if proc.poll() is None:proc.kill();proc.wait(timeout=2)
            proc.stderr.close()


if __name__=='__main__':unittest.main()
