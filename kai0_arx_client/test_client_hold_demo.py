import io
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np
import client_hold_demo as demo


class Tests(unittest.TestCase):
    def test_explicit_exit_homes_after_hold_returns(self):
        robot = Mock(); robot.maintain_hold.return_value = np.zeros(14)
        order = []
        with patch.object(demo, 'official_home', side_effect=lambda *a: order.append('home')), \
             patch.object(demo.client, 'cli_hold_until_exit', side_effect=lambda *a: order.append('exit')):
            demo.exercise(robot, {}, io.StringIO(), threading.Event(), seconds=0, official=True)
        self.assertEqual(order, ['home', 'exit', 'home'])

    def test_hold_fault_does_not_home_on_exit(self):
        robot = Mock(); robot.maintain_hold.return_value = np.zeros(14)
        with patch.object(demo, 'official_home') as home, \
             patch.object(demo.client, 'cli_hold_until_exit', side_effect=RuntimeError('feedback fault')):
            with self.assertRaisesRegex(RuntimeError, 'feedback fault'):
                demo.exercise(robot, {}, io.StringIO(), threading.Event(), seconds=0, official=True)
            self.assertEqual(home.call_count, 1)  # startup only

    def test_transient_bad_startup_sample_never_sent(self):
        robot = Mock()
        bad = np.zeros(14); bad[6] = -12.48
        robot.maintain_hold.side_effect = [bad, np.zeros(14), np.zeros(14)]
        event = Mock(); event.is_set.return_value = False
        log = io.StringIO()
        demo.wait_feedback(robot, (-3.4, .1), log, event, required=2)
        self.assertIn('startup_feedback_rejected', log.getvalue())
        robot.command.assert_not_called()
        robot.hold_current.assert_not_called()

    def test_startup_timeout_does_not_command(self):
        robot = Mock()
        with self.assertRaises(TimeoutError):
            demo.wait_feedback(robot, (-3.4, .1), io.StringIO(), threading.Event(), timeout=0)
        robot.command.assert_not_called()

    def test_bad_gripper_blocks_hold(self):
        robot = Mock()
        bad = np.zeros(14); bad[13] = -12.48
        robot.maintain_hold.return_value = bad
        checked = demo.CheckedRobot(robot, (-3.4, .1), io.StringIO())
        with self.assertRaisesRegex(ValueError, 'right.*9.080000'):
            checked.hold_current()
        robot.command.assert_not_called()

    def test_official_home_order_and_defaults(self):
        robot = Mock(); left = Mock(); right = Mock()
        robot.arms = [left, right]
        order = []
        left.go_home.side_effect = lambda *a, **kw: order.append(('left', a, kw))
        right.go_home.side_effect = lambda *a, **kw: order.append(('right', a, kw))
        demo.official_home(robot, io.StringIO(), threading.Event())
        self.assertEqual(order, [('left', (1,), {'wait': True}), ('right', (1,), {'wait': True})])

    def test_failed_left_home_never_starts_right(self):
        robot = Mock(); robot.arms = [Mock(), Mock()]
        robot.arms[0].go_home.return_value = False
        with self.assertRaises(RuntimeError):
            demo.official_home(robot, io.StringIO(), threading.Event())
        robot.arms[1].go_home.assert_not_called()

    def test_official_targets_then_same_hold(self):
        robot = Mock(); robot.maintain_hold.return_value = np.zeros(14)
        event = threading.Event()
        robot.command.side_effect = lambda target: event.set()
        with patch.object(demo, 'official_home'), patch.object(demo.client, 'cli_hold_until_exit') as hold:
            demo.exercise(robot, {}, io.StringIO(), event, official=True)
            hold.assert_called_once()
        np.testing.assert_array_equal(robot.command.call_args.args[0], [.2]*6+[-1]+[.2]*6+[-1])
        robot.hold_current.assert_not_called()

    def test_pause_skips_motion_and_uses_client_hold(self):
        robot = Mock()
        robot.hold_current.return_value = np.zeros(14)
        event = threading.Event()
        event.set()
        log = io.StringIO()
        with patch.object(demo.client, 'cli_hold_until_exit') as hold:
            demo.exercise(robot, {}, log, event, move_j6=True)
            hold.assert_called_once_with(robot, log, event)
        robot.command.assert_not_called()
        robot.close.assert_not_called()

    def test_completion_enters_same_hold(self):
        robot = Mock()
        robot.hold_current.return_value = np.zeros(14)
        with patch.object(demo.client, 'cli_hold_until_exit') as hold:
            demo.exercise(robot, {}, io.StringIO(), threading.Event(), seconds=0)
            hold.assert_called_once()
        robot.command.assert_not_called()

    def test_feedback_fault_does_not_force_hold(self):
        robot = Mock()
        robot.hold_current.return_value = np.zeros(14)
        robot.maintain_hold.side_effect = RuntimeError('fault')
        with patch.object(demo.client, 'cli_hold_until_exit') as hold:
            with self.assertRaisesRegex(RuntimeError, 'fault'):
                demo.exercise(robot, {}, io.StringIO(), threading.Event())
            hold.assert_not_called()


if __name__ == '__main__':
    unittest.main()
