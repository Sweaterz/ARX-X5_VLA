import json, threading, time, unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import gui_server as gui
import client

class Arm:
    def __init__(self):self.protected=0
    def protect_mode(self):self.protected+=1;return True
class Robot:
    def __init__(self):self.arms=[Arm(),Arm()];self.lock=threading.RLock();self.commands=[];self.q=client.MockRobot().q
    def read(self):return self.q.copy()
    def command(self,a):self.commands.append(a)
    def hold_current(self):
        self.command(self.read()); return self.read()
    def maintain_hold(self):return self.read()
    def close(self):pass
class Policy:
    metadata={'test_only':False,'motion_enabled':True,'action_dim':14,'mode':'checkpoint'}
    def __init__(self,*a):pass
    def infer(self,obs):return np.tile(obs['state'],(50,1))
    def close(self):pass

class GuiTests(unittest.TestCase):
    def setUp(self):
        with patch('threading.Thread.start'):self.m=gui.Manager()
        self.m.robot=Robot();self.m.connected=True;self.m.mode='protect';self.m.owner='owner'
        self.m.cams=client.Cameras(self.m.config,False)
        self.data=dict(server=self.m.config['server']['url'],prompt='test',steps=2,confirmed=True)
    def test_start_server_does_not_construct_sdk(self):
        with patch('threading.Thread.start'),patch.object(client,'SDKRobot') as sdk:
            m=gui.Manager();sdk.assert_not_called();self.assertFalse(m.connected)
    def test_no_motion_uses_real_inference_path_without_command(self):
        with patch.object(client,'Policy',Policy):self.m.inference(self.data,False)
        self.assertEqual(self.m.robot.commands,[])
        self.assertEqual(self.m.result['shape'],[50,14])
    def test_stop_during_inference_prevents_commands(self):
        m=self.m
        robot=m.robot
        class StopPolicy(Policy):
            def infer(self,obs):m.submit('stop',{},'owner');return super().infer(obs)
        with patch.object(client,'Policy',StopPolicy),self.assertRaises(InterruptedError):m.inference(self.data,True)
        self.assertEqual(robot.commands,[])
        self.assertTrue(all(a.protected for a in robot.arms))
        self.assertIsNone(m.robot)
    def test_lost_heartbeat_prevents_motion(self):
        self.m.heartbeat=time.monotonic()-3
        with self.assertRaises(gui.PauseRequested):self.m.check_stop()
        self.m.pause_hold('offline')
        self.assertEqual(self.m.mode,'holding')
        self.assertIsNone(self.m.owner)
        self.assertFalse(self.m.latched)
    def test_test_only_blocks_motion(self):
        class TestPolicy(Policy):metadata=dict(Policy.metadata,test_only=True)
        with patch.object(client,'Policy',TestPolicy),self.assertRaises(ValueError):self.m.inference(self.data,True)
        self.assertEqual(len(self.m.robot.commands),1)  # hold only; no model action
        np.testing.assert_equal(self.m.robot.commands[0],self.m.robot.read())
        self.assertEqual(self.m.mode,'holding')
    def test_owner_and_confirmation(self):
        with self.assertRaises(ValueError):self.m.submit('home',self.data,'other')
        with self.assertRaises(ValueError):self.m.submit('gravity',{},'owner')
        self.assertTrue(self.m.jobs.empty())

    def test_repair_bypasses_only_a_stale_disconnected_owner(self):
        self.m.robot=None;self.m.connected=False;self.m.cams=None;self.m.owner='stale-page'
        with self.assertRaisesRegex(ValueError,'确认'):
            self.m.submit('repair',{},'new-page')
        self.m.submit('repair',{'confirmed':True},'new-page')
        self.assertTrue(self.m.busy)
        self.assertEqual(self.m.owner,'new-page')
        self.assertEqual(self.m.jobs.get_nowait()[0],'repair')

    def test_repair_never_takes_over_a_connected_controller(self):
        self.m.cams=None
        for action in ('repair','release'):
            for mode in ('gravity','protect','holding'):
                with self.subTest(action=action,mode=mode):
                    self.m.mode=mode
                    with self.assertRaisesRegex(ValueError,'先断开'):
                        self.m.submit(action,{'confirmed':True},'other-page')
                    self.assertEqual(self.m.owner,'owner')
                    self.assertFalse(self.m.busy)
                    self.assertTrue(self.m.jobs.empty())

    def test_repair_requires_both_devices_and_controller_handle_disconnected(self):
        self.m.connected=False
        with self.assertRaisesRegex(ValueError,'先断开'):
            self.m.submit('repair',{'confirmed':True},'other-page')
        self.m.robot=None
        with self.assertRaisesRegex(ValueError,'先断开'):
            self.m.submit('repair',{'confirmed':True},'other-page')
        self.assertEqual(self.m.owner,'owner')
        self.assertTrue(self.m.jobs.empty())

    def test_repair_stops_known_services_and_clears_stale_owner(self):
        self.m.robot=None;self.m.connected=False;self.m.cams=None;self.m.owner='stale-page'
        completed=SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch('urllib.request.urlopen',side_effect=OSError('offline')),\
             patch.object(gui.subprocess,'run',return_value=completed) as run,\
             patch.object(self.m,'_service_state',return_value='inactive'),\
             patch.object(self.m,'_arm_lock_available',return_value=True),\
             patch.object(self.m,'_video_device_owners',return_value=[]):
            self.m.repair_conflicts()
        commands=[call.args[0] for call in run.call_args_list]
        self.assertTrue(any(command[:4]==['systemctl','--user','--no-block','stop'] for command in commands))
        self.assertEqual(self.m.repair_status['state'],'success')
        self.assertIsNone(self.m.owner)

    def test_repair_execution_guard_prevents_blocking_live_feedback(self):
        with patch.object(gui.subprocess,'run') as run,\
             patch.object(self.m,'_read_local_status') as read,\
             self.assertRaisesRegex(ValueError,'先断开'):
            self.m.repair_conflicts()
        run.assert_not_called();read.assert_not_called()
        self.assertEqual(self.m.owner,'owner')
        self.assertEqual(self.m.mode,'protect')

    def test_repair_unknown_recording_state_does_not_stop_any_controller(self):
        self.m.robot=None;self.m.connected=False;self.m.cams=None
        def state(unit):return 'active' if unit in gui.COLLECTION_SERVICES else 'inactive'
        with patch.object(self.m,'_service_state',side_effect=state),\
             patch.object(self.m,'_read_local_status',side_effect=OSError('offline')),\
             patch.object(self.m,'_stop_service_group') as stop,\
             self.assertRaisesRegex(RuntimeError,'录制状态不可核实'):
            self.m.repair_conflicts()
        stop.assert_not_called()
        self.assertEqual(self.m.repair_status['state'],'error')

    def test_repair_recording_or_unknown_state_does_not_stop_any_controller(self):
        self.m.robot=None;self.m.connected=False;self.m.cams=None
        for recording in ('recording','saving',None):
            with self.subTest(recording=recording),\
                 patch.object(self.m,'_service_state',return_value='active'),\
                 patch.object(self.m,'_read_local_status',return_value={'recording':{'state':recording},'arm':{'mode':'protect'}}),\
                 patch.object(self.m,'_stop_service_group') as stop,\
                 self.assertRaisesRegex(ValueError,'录制'):
                self.m.repair_conflicts()
            stop.assert_not_called()

    def test_repair_unknown_collection_service_does_not_stop_any_controller(self):
        self.m.robot=None;self.m.connected=False;self.m.cams=None
        with patch.object(self.m,'_service_state',return_value='unknown'),\
             patch.object(self.m,'_stop_service_group') as stop,\
             self.assertRaisesRegex(RuntimeError,'不可确认'):
            self.m.repair_conflicts()
        stop.assert_not_called()

    def test_repair_refuses_to_stop_a_moving_tate_controller(self):
        self.m.robot=None;self.m.connected=False;self.m.cams=None
        def state(unit):return 'active' if unit==gui.TATE_SERVICE else 'inactive'
        def status(url):
            if ':8090/' in url:raise OSError('offline')
            return {'running':True,'mode':'testing'}
        with patch.object(self.m,'_service_state',side_effect=state),\
             patch.object(self.m,'_read_local_status',side_effect=status),\
             self.assertRaisesRegex(ValueError,'TATE.*testing'):
            self.m.repair_conflicts()
        self.assertEqual(self.m.repair_status['state'],'error')

    def test_repair_stops_held_tate_before_collection_services(self):
        self.m.robot=None;self.m.connected=False;self.m.cams=None
        tate_checks=0;groups=[]
        def state(unit):
            nonlocal tate_checks
            if unit==gui.TATE_SERVICE:
                tate_checks+=1
                return 'active' if tate_checks==1 else 'inactive'
            return 'inactive'
        def status(url):
            if ':8090/' in url:raise OSError('offline')
            return {'running':True,'mode':'holding'}
        completed=SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(self.m,'_service_state',side_effect=state),\
             patch.object(self.m,'_read_local_status',side_effect=status),\
             patch.object(self.m,'_stop_service_group',side_effect=lambda units,force:groups.append((tuple(units),force)) or {}),\
             patch.object(self.m,'_arm_lock_available',return_value=True),\
             patch.object(self.m,'_video_device_owners',return_value=[]),\
             patch.object(gui.subprocess,'run',return_value=completed):
            self.m.repair_conflicts()
        self.assertEqual(groups[0],((gui.TATE_SERVICE,),False))
        self.assertEqual(groups[1],(gui.COLLECTION_SERVICES,False))
        self.assertEqual(self.m.repair_status['state'],'success')

    def test_repair_rechecks_capture_restored_by_tate(self):
        self.m.robot=None;self.m.connected=False;self.m.cams=None
        restored=False;groups=[]
        def state(unit):
            if unit==gui.TATE_SERVICE:return 'inactive' if restored else 'active'
            return 'active' if restored else 'inactive'
        def status(url):
            if ':8090/' in url:raise OSError('recording state unavailable')
            return {'running':True,'mode':'holding'}
        def stop(units,force):
            nonlocal restored
            groups.append((tuple(units),force));restored=True
        with patch.object(self.m,'_service_state',side_effect=state),\
             patch.object(self.m,'_read_local_status',side_effect=status),\
             patch.object(self.m,'_stop_service_group',side_effect=stop),\
             self.assertRaisesRegex(RuntimeError,'录制状态不可核实'):
            self.m.repair_conflicts()
        self.assertEqual(groups,[((gui.TATE_SERVICE,),False)])

    def test_nonforcing_service_stop_never_kills_a_stuck_controller(self):
        completed=SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(gui.subprocess,'run',return_value=completed) as run,\
             patch.object(self.m,'_service_state',return_value='active'),\
             self.assertRaisesRegex(RuntimeError,'未强制结束'):
            self.m._stop_service_group(gui.COLLECTION_SERVICES,grace_s=0,force=False)
        self.assertEqual(len(run.call_args_list),1)
        self.assertNotIn('kill',run.call_args.args[0])

    def test_failed_disconnected_operation_releases_page_owner(self):
        self.m.robot=None;self.m.connected=False;self.m.cams=None;self.m.owner='failed-page'
        self.m.recover_error(RuntimeError('connect failed'))
        self.assertIsNone(self.m.owner)
    def test_stop_works_while_busy_and_latches(self):
        self.m.busy=True;self.m.submit('stop',{},'other')
        self.assertTrue(self.m.stop_event.is_set());self.assertTrue(self.m.latched)
        self.m.busy=False
        with self.assertRaises(ValueError):self.m.submit('run',self.data,'owner')
    def test_protect_failure_not_reported_as_success(self):
        self.m.robot.arms[0].protect_mode=lambda:False
        self.m.protect();self.assertEqual(self.m.mode,'fault');self.assertTrue(self.m.latched)
    def test_test_logs_full_chunk_violations_without_dispatch(self):
        class BadPolicy(Policy):
            def infer(self,obs):
                a=super().infer(obs);a[:,0]=2;return a
        with patch.object(client,'Policy',BadPolicy):self.m.inference(self.data,False)
        self.assertEqual(len(self.m.result['violations']),50)
        self.assertEqual(self.m.robot.commands,[])
        self.assertEqual(self.m.result['violations'][0]['dimension'],'left_joint_1')
    def test_successful_motion_holds_without_protect(self):
        with patch.object(client,'Policy',Policy): self.m.inference(self.data,True)
        self.assertEqual(self.m.mode,'holding')
        self.assertEqual(len(self.m.robot.commands),3) # two actions + captured hold pose
        self.assertTrue(all(a.protected==0 for a in self.m.robot.arms))
        np.testing.assert_equal(self.m.robot.commands[-1],self.m.robot.read())

    def test_diagnostic_test_preserves_existing_hold(self):
        self.m.mode='holding'
        with patch.object(client,'Policy',Policy): self.m.inference(self.data,False)
        self.assertEqual(self.m.mode,'holding')
        self.assertEqual(self.m.robot.commands,[])
        self.assertTrue(all(a.protected==0 for a in self.m.robot.arms))

    def test_pause_during_prediction_only_sends_hold(self):
        m=self.m
        class PausePolicy(Policy):
            def infer(self,obs):m.submit('pause',{},'owner');return super().infer(obs)
        with patch.object(client,'Policy',PausePolicy),self.assertRaises(gui.PauseRequested):
            m.inference(self.data,True)
        self.assertEqual(m.robot.commands,[])
        m.pause_hold('paused')
        self.assertEqual(len(m.robot.commands),1)
        self.assertTrue(all(a.protected==0 for a in m.robot.arms))

    def test_emergency_overrides_pause(self):
        self.m.pause_event.set();self.m.stop_event.set()
        with self.assertRaises(InterruptedError):self.m.check_stop()
        with self.assertRaises(InterruptedError):self.m.pause_hold('pause')
        self.assertEqual(self.m.robot.commands,[])

    def test_disconnect_needs_confirmation(self):
        with self.assertRaises(ValueError):self.m.submit('disconnect',{},'owner')

    def test_claim_does_not_resume_motion(self):
        self.m.owner=None;self.m.detached=True;self.m.mode='holding'
        self.m.submit('claim',{'confirmed':True},'new')
        self.assertEqual(self.m.owner,'new')
        self.assertEqual(self.m.robot.commands,[])
        self.assertTrue(self.m.jobs.empty())

    def test_home_allowed_from_holding_with_confirmation(self):
        self.m.mode='holding'
        self.m.submit('home',self.data,'owner')
        self.assertEqual(self.m.jobs.get_nowait()[0],'home')
        self.assertTrue(all(a.protected==0 for a in self.m.robot.arms))

    def test_home_no_longer_requires_second_confirmation(self):
        self.m.submit('home',{},'owner')
        self.assertEqual(self.m.jobs.get_nowait()[0],'home')

    def test_home_preserves_sdk_targets_without_extra_commands(self):
        calls=[]
        for i, arm in enumerate(self.m.robot.arms):
            arm.go_home=lambda *args, _i=i, **kw: calls.append((_i,args,kw))
        self.m.execute('home',{})
        self.assertEqual(calls,[(0,(1,),{'wait':True}),(1,(1,),{'wait':True})])
        self.assertEqual(self.m.robot.commands,[])
        self.assertEqual(self.m.mode,'holding')
        self.assertEqual(self.m.snapshot()['hold_source'],'sdk_home')
        self.assertIsNone(self.m.snapshot()['hold_target_rad'])
        self.m.robot.q[0]+=.02
        self.m.pause_hold('pause after home')
        self.m.detached=True
        self.m.pause_hold('page detached after home')
        self.m.tick()
        self.assertEqual(self.m.robot.commands,[])
        self.assertIsNone(self.m.hold_target)

    def test_repeated_pause_keeps_original_target_despite_feedback_drift(self):
        self.m.pause_hold('pause')
        original=self.m.hold_target.copy()
        self.m.robot.q[0] += .01
        self.m.pause_hold('pause again')
        self.m.tick()
        self.assertEqual(len(self.m.robot.commands),1)
        self.assertEqual(self.m.hold_target,original)
        self.assertNotEqual(self.m.state[0],original[0])

    def test_page_disconnect_during_hold_does_not_resend_target(self):
        self.m.pause_hold('pause')
        self.m.heartbeat=time.monotonic()-3
        with self.assertRaises(gui.PauseRequested):self.m.check_stop()
        self.m.pause_hold('offline')
        self.assertEqual(len(self.m.robot.commands),1)
        self.assertIsNone(self.m.owner)
        self.assertTrue(self.m.detached)

    def test_hold_feedback_fault_is_not_suppressed(self):
        self.m.pause_hold('pause')
        with patch.object(self.m.robot,'maintain_hold',side_effect=RuntimeError('fault')):
            with self.assertRaisesRegex(RuntimeError,'fault'):self.m.tick()

    def test_protection_clears_hold_status(self):
        self.m.pause_hold('pause')
        self.m.protect()
        self.assertIsNone(self.m.snapshot()['hold_target_rad'])
        self.assertIsNone(self.m.hold_target)

    def test_home_rejection_does_not_show_hold_or_move_second_arm(self):
        from unittest.mock import Mock
        self.m.robot.arms[0].go_home=Mock(return_value=False)
        self.m.robot.arms[1].go_home=Mock()
        with self.assertRaisesRegex(RuntimeError,'拒绝归位'):self.m.execute('home',{})
        self.assertNotEqual(self.m.mode,'holding')
        self.m.robot.arms[1].go_home.assert_not_called()

    def test_home_requires_protect(self):
        self.m.mode='gravity'
        with self.assertRaises(ValueError):self.m.submit('home',self.data,'owner')
if __name__=='__main__':unittest.main()
