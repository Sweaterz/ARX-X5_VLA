import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
import numpy as np
from dagger_data import Recorder,DATA_PYTHON,ROOT,worker_env,episode_path
from dagger_runtime import Dagger
import test_gui
import gui_server as gui


def metadata():
    return {'prompt':'pick up the plate','checkpoint':str(ROOT.parent/'kai0_arx_server/checkpoints/place_plate/015000/pretrained_model'),
            'checkpoint_name':'place_plate / 015000','action_names':[f'{s}_{j}' for s in ('left','right') for j in ('joint_1','joint_2','joint_3','joint_4','joint_5','joint_6','gripper')], 'demo':True}


def frame(i,human=1,segment=1):
    q=np.linspace(0,.1,14,dtype='f4')+i*.01;q[[6,13]]=-1-i*.01
    image=np.zeros((480,640,3),dtype='u1');image[:,:,0]=i*3%255;image[:,:,1]=50
    return {'timestamp':10+i/30,'feedback_time':10+i/30,'qpos':q,'policy_action':np.full(14,np.nan,dtype='f4'),
            'requested_action':np.full(14,np.nan,dtype='f4'),'request_valid':0,'control_mode':human,'intervention':human,'segment':segment,
            'images':{r:image.copy() for r in ('head','left','right')},'camera_times':np.tile([10+i/30,10+i/30,i*33,i],(3,1))}


class SessionTests(unittest.TestCase):
    def setup_manager(self):
        fixture=test_gui.GuiTests();fixture.setUp();m=fixture.m
        m.dagger.recorder=Mock(closed=False);m.dagger.recorder.snapshot.return_value={'status':'recording','frames':0};m.dagger.phase='policy'
        return fixture,m

    def test_late_policy_response_does_not_send_any_action(self):
        fixture,m=self.setup_manager();m.busy=True
        class Policy(test_gui.Policy):
            def infer(self,obs):
                m.submit('dagger_takeover',{},'owner')
                return super().infer(obs)
        with patch.object(m.dagger,'sample'),patch.object(test_gui.client,'Policy',Policy):
            with self.assertRaises(gui.PauseRequested):m.inference(fixture.data,True)
        self.assertEqual(m.robot.commands,[])
        m.pause_hold('takeover');self.assertEqual(len(m.robot.commands),1)
        for arm in m.robot.arms:arm.gravity_compensation=Mock(return_value=True)
        m.handle_dagger_pending();self.assertEqual(m.dagger.phase,'human');self.assertEqual(m.mode,'gravity')
        self.assertEqual(len(m.robot.commands),1)
        m.submit('dagger_end_correction',{},'owner');m.pause_hold('end correction');m.handle_dagger_pending()
        self.assertEqual(m.dagger.phase,'paused');self.assertEqual(m.mode,'holding')
        self.assertEqual(len(m.robot.commands),2)

    def test_partial_gravity_failure_protects_both_arms(self):
        _,m=self.setup_manager();m.mode='holding';m.dagger.pending=('dagger_takeover',{})
        robot=m.robot
        robot.arms[0].gravity_compensation=Mock(return_value=True)
        robot.arms[1].gravity_compensation=Mock(return_value=False)
        with self.assertRaises(gui.HardwareControlError) as e:m.handle_dagger_pending()
        m.recover_error(e.exception)
        self.assertFalse(m.connected);self.assertTrue(m.latched)
        self.assertTrue(all(a.protected>0 for a in robot.arms))

    def test_gravity_exception_is_a_hardware_fault(self):
        _,m=self.setup_manager();m.mode='holding';m.dagger.pending=('dagger_takeover',{})
        robot=m.robot
        robot.arms[0].gravity_compensation=Mock(return_value=True)
        robot.arms[1].gravity_compensation=Mock(side_effect=RuntimeError('SDK exception'))
        with self.assertRaises(gui.HardwareControlError) as e:m.handle_dagger_pending()
        m.recover_error(e.exception)
        self.assertTrue(m.latched);self.assertFalse(m.connected)
        self.assertTrue(all(a.protected>0 for a in robot.arms))

    def test_detach_cancels_pending_takeover(self):
        _,m=self.setup_manager();m.dagger.pending=('dagger_takeover',{});m.detached=True
        m.pause_hold('page lost');self.assertIsNone(m.dagger.pending);self.assertEqual(m.dagger.phase,'paused')

    def test_human_camera_failure_holds_instead_of_staying_gravity(self):
        _,m=self.setup_manager();m.dagger.phase='human';m.mode='gravity'
        with patch.object(m.dagger,'sample'):m.recover_error(RuntimeError('camera old'))
        self.assertEqual(m.mode,'holding');self.assertEqual(m.dagger.phase,'paused')

    def test_expired_samples_never_enter_recorder(self):
        _,m=self.setup_manager();m.state=np.zeros(14).tolist();m.observed_at=time.monotonic()
        m.cams=Mock();now=time.monotonic()
        m.cams.latest.return_value=({}, {r:dict(captured=now-.2,received=now,device_ms=0,sequence=1) for r in ('head','left','right')})
        with self.assertRaisesRegex(RuntimeError,'过期'):m.dagger.sample(m)
        m.dagger.recorder.send.assert_not_called()

    def test_page_cannot_launch_competing_task_or_export_connected(self):
        fixture,m=self.setup_manager();m.mode='holding'
        with self.assertRaises(ValueError):m.submit('run',fixture.data,'owner')
        with self.assertRaises(ValueError):m.submit('dagger_export',{'episode':'bad'},'owner')
        with self.assertRaises(ValueError):m.submit('dagger_takeover',{},'other')

    def test_server_stop_while_human_requests_hold(self):
        _,m=self.setup_manager();m.mode='gravity';m.dagger.phase='human'
        m.submit('server_stop',{},'owner');self.assertTrue(m.pause_event.is_set())
        with patch.object(m.dagger,'sample'),patch.object(m.local_server,'stop') as stop:
            m.pause_hold('stop server');m.finish_server_stop();stop.assert_called_once()
        self.assertEqual(m.mode,'holding')


class DataTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def record(self,n=12):
        r=Recorder(metadata(),self.root)
        self.assertTrue(r.ready.wait(15),r.snapshot())
        for i in range(n):
            while True:
                try:r.send('frame',frame(i,segment=1 if i<6 else 2));break
                except RuntimeError as exc:
                    if '缓冲' not in str(exc):raise
                    time.sleep(.05)
        return r

    def test_save_barrier_and_duplicate_finish(self):
        import h5py
        r=self.record();r.finish('success');r.finish('failure')
        self.assertTrue(r.done.wait(30),r.snapshot());self.assertEqual(r.snapshot()['status'],'saved',r.snapshot())
        path=episode_path(r.id,self.root)
        with h5py.File(path/'trajectory.h5') as h:
            self.assertEqual(len(h['timestamp']),12);self.assertTrue(np.isnan(h['requested_action'][:]).all())
            self.assertTrue((h['qpos'][:,6]<0).all())
        self.assertEqual(json.loads((path/'manifest.json').read_text())['result'],'success')

    def test_export_actual_lerobot_and_pi05_preprocessing(self):
        r=self.record(18);r.finish();self.assertTrue(r.done.wait(30));self.assertEqual(r.snapshot()['status'],'saved')
        p=subprocess.run([DATA_PYTHON,str(ROOT/'dagger_worker.py'),'export',str(self.root),r.id],env=worker_env(),capture_output=True,text=True,timeout=180)
        self.assertEqual(p.returncode,0,p.stdout+'\n'+p.stderr)
        reports=list((self.root/'exports').glob('*/dagger_export.json'));self.assertEqual(len(reports),1)
        report=json.loads(reports[0].read_text());self.assertEqual(report['segments'],2);self.assertTrue(report['processor_validated'])
        self.assertEqual(report['action_shift_frames'],1)
        # Verify non-static future targets and signed grippers in the actual Parquet.
        import pyarrow.parquet as pq
        tables=[pq.read_table(p) for p in reports[0].parent.glob('data/**/*.parquet')]
        rows=[r for t in tables for r in t.to_pylist()]
        self.assertTrue(all(r['action'][0]>r['observation.state'][0] for r in rows))
        self.assertTrue(all(r['action'][6]<0 for r in rows))

    def test_writer_pipe_failure_preserves_incomplete_data(self):
        r=self.record(3)
        time.sleep(.3);r.queue.put(('event',{'timestamp':time.monotonic(),'phase':'paused'}))
        time.sleep(.1);r.process.terminate();self.assertTrue(r.done.wait(10));self.assertEqual(r.snapshot()['status'],'error')
        self.assertTrue((self.root/'incomplete'/r.id/'manifest.json').exists())
        r.finish();self.assertTrue(r.closed);self.assertEqual(r.snapshot()['status'],'error')

    def test_discard_moves_only_own_episode(self):
        r=self.record(3);r.finish(discard=True);self.assertTrue(r.done.wait(30));self.assertEqual(r.snapshot()['status'],'discarded')
        self.assertTrue((self.root/'trash'/r.id).exists());self.assertFalse((self.root/'episodes'/r.id).exists())

    def test_empty_episode_can_finish(self):
        r=self.record(0);r.finish();self.assertTrue(r.done.wait(20));self.assertEqual(r.snapshot()['status'],'saved',r.snapshot())

    def test_bounded_queue_rejects_without_silent_loss(self):
        r=Recorder.__new__(Recorder);r.closed=False;r.state={'status':'recording'};r.lock=threading.Lock();r.queue=queue.Queue(maxsize=1)
        r.send('frame',1)
        with self.assertRaisesRegex(RuntimeError,'缓冲'):r.send('frame',2)
        self.assertEqual(r.queue.get(),('frame',1))

    def test_disk_full_and_encoder_error_keep_incomplete(self):
        import dagger_worker as worker
        for failure in ('disk','encoder'):
            episode='ep_'+str(time.time_ns())+'_deadbeef'
            disk=Mock(free=0 if failure=='disk' else 10**12)
            video=Mock()
            if failure=='encoder':video.write.side_effect=OSError('encoder failed')
            with patch.object(worker,'message',side_effect=[('begin',metadata()),('frame',frame(0))]),patch.object(worker,'Video',return_value=video),patch.object(worker.shutil,'disk_usage',return_value=disk),patch.object(worker,'emit'):
                with self.assertRaises(OSError):worker.record(self.root,episode)
            path=self.root/'incomplete'/episode
            self.assertEqual(json.loads((path/'manifest.json').read_text())['status'],'incomplete')
            self.assertFalse((self.root/'episodes'/episode).exists())

    def test_recover_common_prefix_preserves_originals(self):
        import h5py
        import dagger_worker as worker
        r=self.record(6);r.finish();self.assertTrue(r.done.wait(20))
        saved=episode_path(r.id,self.root);broken=self.root/'incomplete'/r.id;saved.rename(broken)
        with h5py.File(broken/'trajectory.h5','r+') as h:
            for key in h:h[key].resize(7,axis=0)
        with patch.object(worker,'emit'):worker.recover(self.root,r.id)
        path=episode_path(r.id,self.root);self.assertEqual(worker.validate(path),6)
        self.assertTrue(list(path.glob('recovery_original_*/trajectory.h5')))

    def test_paths_cannot_escape_dataset(self):
        for value in ['../../config.json','ep_1_deadbeef/..','/etc/passwd']:
            with self.assertRaises(ValueError):episode_path(value,self.root)


if __name__=='__main__':unittest.main()
