import copy
import json
import subprocess
import sys
import threading
import tempfile
import os
import time
import unittest
from unittest.mock import patch
from pathlib import Path
import numpy as np
from websockets.sync.server import serve
import client
from vendor import msgpack_numpy

C = json.loads((client.ROOT / 'config.json').read_text())
# FakeArm does not parse URDF; make its path check independent of deployment files.
_FAKE_PAYLOAD_DIR=tempfile.TemporaryDirectory(prefix='arx-sdk-test-')
_FAKE_PAYLOAD=Path(_FAKE_PAYLOAD_DIR.name)/'fake_payload.urdf'
_FAKE_PAYLOAD.write_text('<robot name="mock"/>')
C['robot']['urdf_path']=str(_FAKE_PAYLOAD)


class FakeArm:
    fault = None

    def __init__(self, config):
        self.config = config
        self.q = np.array([0., .5, .8, 0, 0, 0, -.5])
        self.protected = 0
        self.closed = False
        self.targets = []
        self.fail = False

    def protect_mode(self):
        self.protected += 1
        return True

    def close(self):
        self.closed = True

    def get_joint_positions(self):
        return self.q.copy()

    def get_gripper_pos(self):
        return float(self.q[6])

    def get_gripper_vel(self):
        return -.12

    def get_gripper_current(self):
        return .34

    def set_motion_smoothness(self, **kw):
        self.smoothness = kw; return kw

    def get_motion_limits(self):
        return {'owner':'sdk'}

    def set_joint_positions(self, positions, duration):
        self.duration = duration
        self.targets.append(positions)
        return not self.fail

    def set_gripper_pos(self, pos, duration=None):
        self.gripper_request = (pos, duration)
        return True


class ClientTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)

    def test_wire_and_camera_order(self):
        obs = client.make_observation(client.MockRobot().q, client.Cameras(C).read(), 'fold the box')
        decoded = msgpack_numpy.unpackb(msgpack_numpy.packb(obs))
        np.testing.assert_equal(decoded['state'], obs['state'])
        for name, value in [('top_head', 35), ('hand_left', 90), ('hand_right', 145)]:
            self.assertEqual(decoded['images'][name].shape, (3, 224, 224))
            self.assertTrue(np.all(decoded['images'][name][:, 112, 112] == value))
            self.assertTrue(np.all(decoded['images'][name][:, 0, 0] == 0))

    def test_raw_predictions_and_diagnostic_only_limits(self):
        q = client.MockRobot().q
        a = q.copy(); a[0] = 1.58; a[6] = -3.41
        np.testing.assert_equal(client.bounded_target(a, q, C), a)
        details = client.diagnostic_violations(a, C)
        self.assertEqual([d['dimension'] for d in details], ['left_joint_1', 'left_gripper'])
        self.assertAlmostEqual(details[0]['excess_rad'], .01)
        for bad in [np.full(14, np.nan), np.zeros(7), np.full(14, np.inf)]:
            with self.assertRaises(ValueError): client.bounded_target(bad, q, C)

    def test_sdk_receives_unmodified_request_and_trajectory_settings(self):
        r = client.SDKRobot(C, FakeArm)
        try:
            a = r.read(); a[0] = 1.58; a[6] = -3.41
            np.testing.assert_equal(r.command(a), a)
            self.assertEqual(r.arms[0].targets[-1][0], 1.58)
            self.assertEqual(r.arms[0].gripper_request, (-3.41, None))
            self.assertEqual(r.arms[0].duration, 0)
            self.assertFalse(hasattr(r.arms[0], 'smoothness'))
        finally: r.close()

    def test_gripper_feedback_is_read_only(self):
        r = client.SDKRobot(C, FakeArm)
        try:
            feedback=r.read_gripper_feedback()
            self.assertEqual([x['role'] for x in feedback],['left','right'])
            self.assertEqual([x['position'] for x in feedback],[-.5,-.5])
            self.assertEqual([x['velocity'] for x in feedback],[-.12,-.12])
            self.assertEqual([x['current'] for x in feedback],[.34,.34])
            self.assertTrue(all(not a.targets and not hasattr(a,'gripper_request') for a in r.arms))
        finally:r.close()

    def test_invalid_request_never_reaches_sdk(self):
        r = client.SDKRobot(C, FakeArm)
        try:
            with self.assertRaises(ValueError): r.command(np.full(14, np.nan))
            self.assertEqual(r.arms[0].targets, [])
            self.assertTrue(r.tripped.is_set())
        finally: r.close()

    def test_sdk_order_and_cleanup(self):
        made = []
        def factory(config):
            arm = FakeArm(config); made.append(arm); return arm
        r = client.SDKRobot(C, factory)
        try:
            self.assertEqual([a.config['can_port'] for a in made], ['can1', 'can3'])
            self.assertTrue(all(a.config['urdf_path'] == C['robot']['urdf_path'] for a in made))
            target = r.read(); target[0] += .1; target[7] -= .1
            r.command(target)
            self.assertAlmostEqual(made[0].targets[-1][0], .1)
            self.assertAlmostEqual(made[1].targets[-1][0], -.1)
            made[1].fail = True
            with self.assertRaises(RuntimeError):
                r.command(target)
            self.assertTrue(r.tripped.is_set())
            self.assertTrue(all(a.protected >= 2 for a in made))
        finally:
            r.close()
        self.assertTrue(all(a.closed for a in made))

    def test_hold_captures_feedback_and_preserves_sdk_connection(self):
        r = client.SDKRobot(C, FakeArm)
        try:
            q = r.read(); held = r.hold_current()
            np.testing.assert_equal(held, q)
            r.maintain_hold()
            self.assertTrue(all(a.protected == 1 and not a.closed for a in r.arms))
            r.arms[0].fault = 'hold fault'
            with self.assertRaisesRegex(RuntimeError, 'hold fault'): r.maintain_hold()
        finally: r.close()

    def test_ctrlc_after_inference_discards_actions_then_holds(self):
        import argparse, signal
        from unittest.mock import Mock
        args=argparse.Namespace(enable_motion=True,robot='sdk',cameras='realsense',steps=30,
                                prompt='test',server='ws://unused')
        robot=Mock(); robot.read.return_value=client.MockRobot().q; robot.sdk_motion_limits=[]
        policy=Mock(); policy.metadata={'test_only':False}
        def infer(_):
            signal.raise_signal(signal.SIGINT)
            return np.ones((8,14))
        policy.infer.side_effect=infer
        cameras=Mock(); cameras.read.return_value=client.Cameras(C).read()
        with patch.object(client,'Policy',return_value=policy), patch.object(client,'Cameras',return_value=cameras), patch.object(client,'SDKRobot',return_value=robot), patch.object(client,'cli_hold_until_exit') as hold:
            client.run(args,C)
            robot.command.assert_not_called();hold.assert_called_once()
            robot.close.assert_called_once()

    def test_hold_waits_for_explicit_exit_and_logs_release(self):
        import io
        from unittest.mock import Mock
        robot=Mock();robot.hold_current.return_value=client.MockRobot().q
        log=io.StringIO();readfd,writefd=os.pipe()
        os.write(writefd,b'EXIT\n');os.close(writefd)
        with os.fdopen(readfd,'rb') as stream:
            client.cli_hold_until_exit(robot,log,threading.Event(),stream)
        robot.hold_current.assert_called_once();robot.maintain_hold.assert_called()
        robot.close.assert_not_called()
        self.assertIn('operator_exit',log.getvalue())

    def test_hold_does_not_swallow_sdk_fault(self):
        import io
        from unittest.mock import Mock
        robot=Mock();robot.hold_current.return_value=client.MockRobot().q
        robot.maintain_hold.side_effect=RuntimeError('feedback fault')
        with self.assertRaisesRegex(RuntimeError,'feedback fault'):
            client.cli_hold_until_exit(robot,io.StringIO(),threading.Event())

    def test_lock_refuses_second_controller(self):
        first = client.SDKRobot(C, FakeArm)
        try:
            with patch.object(client, 'MockRobot') as unrelated:
                with self.assertRaisesRegex(RuntimeError, 'Another ARX'):
                    client.SDKRobot(C, lambda c: self.fail('Must not construct hardware'))
                unrelated.assert_not_called()
        finally:
            first.close()

    def test_fault_prevents_command(self):
        r = client.SDKRobot(C, FakeArm)
        try:
            r.arms[0].fault = 'undervoltage'
            with self.assertRaisesRegex(RuntimeError, 'undervoltage'):
                r.command(client.MockRobot().q)
            self.assertTrue(r.tripped.is_set())
            self.assertTrue(all(not arm.targets for arm in r.arms))
        finally:
            r.close()

    def test_partial_init_cleanup(self):
        made = []
        def factory(config):
            if made:
                raise RuntimeError('right init failure')
            arm = FakeArm(config); made.append(arm); return arm
        with self.assertRaises(RuntimeError):
            client.SDKRobot(C, factory)
        self.assertTrue(made[0].closed)
        self.assertGreaterEqual(made[0].protected, 2)

    def test_watchdog(self):
        c = copy.deepcopy(C); c['safety']['watchdog_s'] = .05
        r = client.SDKRobot(c, FakeArm)
        try:
            self.assertTrue(r.tripped.wait(1))
            with self.assertRaises(TimeoutError):
                r.command(client.MockRobot().q)
        finally:
            r.close()

    def test_mode_guard_before_hardware_or_network(self):
        import argparse
        a = argparse.Namespace(enable_motion=False, robot='sdk', cameras='mock')
        with patch.object(client, 'Policy') as policy, patch.object(client, 'SDKRobot') as sdk:
            with self.assertRaises(ValueError):
                client.run(a, C)
            policy.assert_not_called(); sdk.assert_not_called()

    def roundtrip(self, handler, fn):
        with serve(handler, '127.0.0.1', 0, compression=None) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                fn(f'ws://127.0.0.1:{server.socket.getsockname()[1]}')
            finally:
                server.shutdown(); thread.join(2)

    def test_e2e_cli(self):
        from mock_server import handle
        def check(url):
            p = subprocess.run([sys.executable, str(client.ROOT / 'client.py'), 'run', '--server', url,
                                '--prompt', 'fold the box', '--steps', '10'], capture_output=True, text=True, timeout=10)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn('steps=10', p.stdout)
            self.assertIn('motion=False', p.stdout)
        self.roundtrip(handle, check)

    def test_server_error(self):
        def handler(ws):
            ws.send(msgpack_numpy.packb({})); ws.recv(); ws.send('model failure')
        def check(url):
            p = client.Policy(url, 1)
            try:
                with self.assertRaisesRegex(RuntimeError, 'model failure'):
                    p.infer({})
            finally:
                p.close()
        self.roundtrip(handler, check)

    def test_invalid_chunk(self):
        for actions in (np.zeros((8, 7)), np.full((8, 14), np.inf), np.zeros((0, 14))):
            def handler(ws):
                ws.send(msgpack_numpy.packb({})); ws.recv(); ws.send(msgpack_numpy.packb({'actions': actions}))
            def check(url):
                p = client.Policy(url, 1)
                try:
                    with self.assertRaises(ValueError):
                        p.infer({})
                finally:
                    p.close()
            self.roundtrip(handler, check)

    def test_timeout(self):
        def handler(ws):
            ws.send(msgpack_numpy.packb({})); ws.recv(); time.sleep(.2)
        def check(url):
            p = client.Policy(url, .05)
            try:
                with self.assertRaises(TimeoutError):
                    p.infer({})
            finally:
                p.close()
        self.roundtrip(handler, check)

    def test_camera_partial_cleanup(self):
        import types
        pipelines = []
        class Pipeline:
            def __init__(self):
                self.stopped = False; pipelines.append(self)
            def start(self, config):
                if len(pipelines) == 2:
                    raise RuntimeError('camera busy')
            def stop(self): self.stopped = True
        fake = types.SimpleNamespace(pipeline=Pipeline, config=lambda: unittest.mock.Mock(),
                                     stream=types.SimpleNamespace(color=1), format=types.SimpleNamespace(rgb8=1))
        with patch.dict(sys.modules, {'pyrealsense2': fake}):
            with self.assertRaisesRegex(RuntimeError, 'camera busy'):
                client.Cameras(C, True)
        self.assertTrue(pipelines[0].stopped)


if __name__ == '__main__':
    unittest.main()
