import unittest
from unittest.mock import Mock
import test_gui

class ServerIndependenceTests(unittest.TestCase):
    setUp = test_gui.GuiTests.setUp
    def test_start_server_while_holding_keeps_robot_connected(self):
        robot=self.m.robot;self.m.mode='holding';self.m.local_server=Mock()
        self.m.submit('server_start',{'prompt':'fold'},'owner')
        action,data=self.m.jobs.get_nowait()
        self.m.execute(action,data)
        self.m.local_server.start.assert_called_once_with(prompt='fold')
        self.assertIs(self.m.robot,robot);self.assertTrue(self.m.connected)
        self.assertEqual(self.m.mode,'holding');self.assertEqual(robot.commands,[])
    def test_connect_during_model_loading_is_allowed(self):
        self.m.robot=None;self.m.connected=False;self.m.cams=None;self.m.mode='disconnected'
        self.m.local_server=Mock();self.m.local_server.snapshot.return_value={'status':'starting'}
        self.m.submit('connect',{'confirmed':True},'owner')
        self.assertEqual(self.m.jobs.get_nowait()[0],'connect')
    def test_select_stopped_checkpoint_while_connected(self):
        self.m.local_server=Mock();self.m.local_server.snapshot.return_value={'checkpoint':'/tmp/example'};self.m.mode='holding'
        self.m.submit('server_checkpoint',{'checkpoint':'/tmp/example'},'owner')
        self.m.local_server.select_checkpoint.assert_called_once_with('/tmp/example')
        self.assertTrue(self.m.connected);self.assertEqual(self.m.mode,'holding')
    def test_running_task_still_blocks_model_management(self):
        self.m.busy=True
        for action,data in [('server_start',{}),('server_checkpoint',{'checkpoint':'/tmp/example'})]:
            with self.assertRaises(ValueError):self.m.submit(action,data,'owner')

if __name__=='__main__':unittest.main()
