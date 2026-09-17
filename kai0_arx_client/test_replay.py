"""No hardware: recorded trajectory validation and interruption semantics."""
import hashlib,time,threading,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import gui_server as gui
from test_gui import Robot

def payload():
    q=np.zeros((6,14));q[:,0]=np.arange(6)*.005;q[:,6]=-.5;q[:,13]=-.8
    return dict(qpos=q.tolist(),timestamp=(np.arange(6)*.002).tolist(),intervention=[0,0,0,1,1,1],segment=[1,1,1,2,2,2],metadata={'demo':True,'prompt':'synthetic'})

class ReplayTests(unittest.TestCase):
    def setUp(self):
        with patch('threading.Thread.start'):self.m=gui.Manager(demo=True)
        m=self.m;m.robot=Robot();m.connected=True;m.mode='holding';m.owner='owner';m.cams=None
        m.replay.install(m,payload(),m.dagger.root,'ep_1_aaaaaaaa');m.robot.q=m.replay.q[0].copy()
    def test_boundary_requires_explicit_continue(self):
        m=self.m;m.replay.run(m,1)
        self.assertEqual(m.mode,'holding');self.assertEqual(m.replay.phase,'boundary');self.assertEqual(m.replay.index,3)
        np.testing.assert_array_equal(m.robot.q,m.replay.q[2])
        m.replay.align(m);self.assertEqual(m.replay.phase,'ready');np.testing.assert_array_equal(m.robot.q,m.replay.q[3])
        m.replay.run(m,.5);self.assertEqual(m.replay.phase,'complete');self.assertEqual(m.mode,'holding')
        self.assertLess(m.robot.q[6],0)
    def test_wrong_start_no_command(self):
        m=self.m;m.robot.q[0]=1
        with self.assertRaisesRegex(ValueError,'起点'):m.replay.run(m,1)
        self.assertEqual(m.robot.q[0],1);self.assertEqual(m.mode,'holding')
    def test_pause_prevents_next_command_and_resumes(self):
        m=self.m;original=m.replay.send;sent=[]
        def send(manager,q):
            original(manager,q);sent.append(q.copy());manager.pause_event.set()
        with patch.object(m.replay,'send',side_effect=send),self.assertRaises(gui.PauseRequested):m.replay.run(m,1)
        self.assertEqual(len(sent),1);m.pause_hold('test');self.assertEqual(m.replay.phase,'paused')
        m.replay.run(m,1);self.assertEqual(m.replay.phase,'boundary')
    def test_emergency_prevents_any_command(self):
        m=self.m;m.stop_event.set()
        with patch.object(m.replay,'send') as send,self.assertRaises(InterruptedError):m.replay.run(m,1)
        send.assert_not_called()
    def test_lost_page_holds(self):
        m=self.m;m.heartbeat=time.monotonic()-3;m.replay.phase='playing'
        with self.assertRaises(gui.PauseRequested):m.replay.run(m,1)
        m.pause_hold('lost');self.assertTrue(m.detached);self.assertEqual(m.mode,'holding');self.assertEqual(m.replay.phase,'paused')
    def test_feedback_failure_protects(self):
        m=self.m;m.demo=False;m.replay.phase='playing';robot=m.robot;robot.maintain_hold=lambda:(_ for _ in ()).throw(RuntimeError('feedback failed'))
        m.recover_error(RuntimeError('feedback failed'))
        self.assertTrue(m.latched);self.assertFalse(m.connected);self.assertTrue(all(a.protected for a in robot.arms))
    def test_bad_data(self):
        for key,value in [('qpos',[[float('nan')]*14]*6),('timestamp',[0]*6),('intervention',[0]),('segment',[0]*5)]:
            data=payload();data[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.m.replay.install(self.m,data,self.m.dagger.root,'test')
    def test_real_rejects_demo_or_wrong_config(self):
        m=self.m;m.demo=False
        with self.assertRaisesRegex(ValueError,'演示'):m.replay.install(m,payload(),m.dagger.root,'test')
        data=payload();data['metadata']['demo']=False
        with self.assertRaisesRegex(ValueError,'关节顺序'):m.replay.install(m,data,m.dagger.root,'test')
    def test_sdk_dispatch_preserves_raw_negative_gripper(self):
        m=self.m;m.demo=False;m.replay.send(m,m.replay.q[1]);np.testing.assert_array_equal(m.robot.commands[0],m.replay.q[1])
    def test_ownership_busy_confirmation_and_mode(self):
        m=self.m
        for owner,data in [('other',{'confirmed':True}),('owner',{})]:
            with self.assertRaises(ValueError):m.submit('replay_run',data,owner)
        m.mode='gravity'
        with self.assertRaises(ValueError):m.submit('replay_run',{'confirmed':True},'owner')
        m.mode='holding';m.busy=True
        with self.assertRaises(ValueError):m.submit('replay_select',{'part':1},'owner')
    def test_invalid_speed_and_selection(self):
        for speed in (0,2,float('nan'),True):
            with self.assertRaises(ValueError):self.m.replay.run(self.m,speed)
        for part in (-1,8,True):
            with self.assertRaises(ValueError):self.m.replay.select(part)
    def test_path_change_requires_reload(self):
        self.m.dagger.root=Path('/different')
        with self.assertRaisesRegex(ValueError,'目录'):self.m.submit('replay_run',{'confirmed':True},'owner')
    def test_pause_after_final_send_has_valid_resume_index(self):
        m=self.m;m.replay.select(1);m.replay.index=6;m.robot.q=m.replay.q[-1].copy()
        m.replay.run(m,1);self.assertEqual(m.replay.phase,'complete')
    def test_late_schedule_does_not_burst_send(self):
        m=self.m;m.state=m.robot.q.tolist();clock=[0.];calls=[0]
        def tick():
            calls[0]+=1
            if calls[0]>1:clock[0]=.3
        with patch.object(m,'tick',side_effect=tick),patch('replay_runtime.time.monotonic',side_effect=lambda:clock[0]),patch.object(m.replay,'send') as send:
            with self.assertRaisesRegex(TimeoutError,'200ms'):m.replay.run(m,1)
            send.assert_not_called()
    def test_completed_target_retained_without_hold_rewrite(self):
        m=self.m;m.demo=False;m.robot.q=m.replay.q[0].copy()
        def command(q):m.robot.commands.append(q.copy());m.robot.q=q.copy()
        m.robot.command=command
        m.replay.run(m,1)
        self.assertEqual(len(m.robot.commands),3);self.assertEqual(m.hold_source,'replay_target')
        np.testing.assert_equal(m.hold_target,m.replay.q[2])
    def test_real_urdf_mismatch(self):
        m=self.m;m.demo=False;data=payload();meta=data['metadata'];meta['demo']=False;meta['robot_config']=m.config['robot'].copy()
        meta['action_names']=[f'{s}_{j}' for s in ('left','right') for j in ('joint_1','joint_2','joint_3','joint_4','joint_5','joint_6','gripper')]
        meta['urdf_sha256']='wrong'
        with self.assertRaisesRegex(ValueError,'URDF'):m.replay.install(m,data,m.dagger.root,'test')
