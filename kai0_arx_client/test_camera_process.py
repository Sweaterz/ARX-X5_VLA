import os
import unittest
from unittest.mock import patch
from camera_process import CameraProcess
import test_gui


class Tests(unittest.TestCase):
    def test_images_and_preview_come_from_separate_process(self):
        camera = CameraProcess({'cameras': {'serials': {}}}, real=False)
        try:
            self.assertNotEqual(camera.pid, os.getpid())
            images, frames, timing = camera.read_preview()
            self.assertEqual(set(images), {'head', 'left', 'right'})
            for role, image in images.items():
                self.assertEqual(image.shape, (480, 640, 3))
                self.assertTrue(frames[role].startswith(b'\xff\xd8'))
            self.assertGreaterEqual(timing, 0)
        finally:
            camera.close()
        self.assertFalse(camera.process.is_alive())
        camera.close()
        with self.assertRaises(RuntimeError):camera.read_preview()

    def test_child_failure_does_not_release_healthy_robot(self):
        camera = CameraProcess({'cameras': {'serials': {}}}, real=False)
        fixture=test_gui.GuiTests();fixture.setUp();m=fixture.m
        m.pause_hold('initial hold');robot=m.robot;before=len(robot.commands)
        m.cams=camera
        camera.process.terminate();camera.process.join(2)
        try:
            with self.assertRaises((RuntimeError, OSError)) as error:m.read_images()
            m.recover_error(error.exception)
            self.assertTrue(m.connected)
            self.assertEqual(m.mode,'holding')
            self.assertEqual(len(robot.commands),before)
            self.assertTrue(all(a.protected==0 for a in robot.arms))
            self.assertTrue(m.camera_failed)
        finally:camera.close()


if __name__=='__main__':unittest.main()
