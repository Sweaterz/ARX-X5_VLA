import time
import unittest
from unittest.mock import Mock,patch
import numpy as np
import gui_server as gui
from test_dagger import SessionTests

class TakeoverDelay(unittest.TestCase):
    def setup(self):
        _,m=SessionTests().setup_manager();m.mode='running';m.dagger.pending=('dagger_takeover',{});m.dagger.last_action=(m.robot.q+.1).tolist()
        for arm in m.robot.arms:arm.gravity_compensation=Mock(return_value=True)
        return m
    def test_preserves_target_and_waits_one_second(self):
        m=self.setup();target=m.dagger.last_action.copy();m.pause_hold('takeover')
        self.assertEqual(m.robot.commands,[]);np.testing.assert_equal(m.hold_target,target)
        started=time.monotonic();calls=[]
        for arm in m.robot.arms:arm.gravity_compensation.side_effect=lambda:(calls.append(time.monotonic()-started) or True)
        with patch.object(m.dagger,'sample'):m.handle_dagger_pending()
        self.assertTrue(all(t>=1 for t in calls));self.assertEqual(len(calls),2);self.assertEqual(len(m.robot.commands),1);self.assertEqual(m.mode,'gravity')
        np.testing.assert_equal(m.robot.commands[0],m.robot.read())
    def test_gravity_mode_allows_manual_arm_motion(self):
        m=self.setup();m.pause_hold('takeover')
        with patch.object(m.dagger,'sample'),patch.object(gui,'TAKEOVER_LAST_TARGET_HOLD_S',.01),\
             patch.object(gui,'TAKEOVER_SETTLE_STABLE_S',.01):
            m.handle_dagger_pending()
        self.assertEqual(m.mode,'gravity');self.assertEqual(m.dagger.phase,'human')
        commands=len(m.robot.commands);m.robot.q[10]+=.1
        with patch.object(m.dagger,'sample'):m.tick()
        self.assertEqual(m.mode,'gravity');self.assertEqual(m.dagger.phase,'human')
        self.assertEqual(len(m.robot.commands),commands)
    def test_pause_cancels_gravity_during_wait(self):
        m=self.setup();m.pause_hold('takeover');original=m.tick;calls=[0]
        def tick():
            calls[0]+=1
            if calls[0]==4:m.submit('pause',{},'owner')
            original()
        with patch.object(m,'tick',side_effect=tick),patch.object(m.dagger,'sample'),self.assertRaises(gui.PauseRequested):m.handle_dagger_pending()
        for arm in m.robot.arms:arm.gravity_compensation.assert_not_called()
        m.pause_hold('cancel');self.assertEqual(m.mode,'holding');self.assertFalse(m.busy)
    def test_stop_or_bad_feedback_cancels_gravity(self):
        for failure in (InterruptedError('stop'),RuntimeError('feedback fault')):
            m=self.setup();m.pause_hold('takeover')
            with patch.object(m,'tick',side_effect=failure),self.assertRaises(type(failure)):m.handle_dagger_pending()
            for arm in m.robot.arms:arm.gravity_compensation.assert_not_called()
            self.assertFalse(m.busy)
    def test_stop_preempts_last_target_hold(self):
        m=self.setup();m.stop_event.set()
        with self.assertRaises(InterruptedError):m.pause_hold('takeover')
        self.assertEqual(m.robot.commands,[])
