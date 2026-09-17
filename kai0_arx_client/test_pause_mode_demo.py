import unittest,numpy as np
import pause_mode_demo as d
class Arm:
    fault=None
    def __init__(self):self.calls=[]
    def get_joint_positions(self):return np.zeros(7)
    def get_joint_velocities(self):return np.zeros(7)
    def get_joint_currents(self):return np.zeros(7)
    def set_joint_positions(self,**kw):self.calls.append(('joint',kw));return True
    def gravity_compensation(self):self.calls.append(('gravity',));return True
    def protect_mode(self):self.calls.append(('protect',));return True
class Tests(unittest.TestCase):
    def test_modes(self):
        for mode in ('joint','gravity','protect'):
            a=Arm();d.pause(a,mode);self.assertEqual(a.calls[0][0],mode);self.assertEqual(len(a.calls),1)
            if mode=='joint':self.assertEqual(a.calls[0][1],dict(positions=[0.]*6,duration=0))
    def test_fault_blocks_transition(self):
        a=Arm();a.fault='fault'
        with self.assertRaises(RuntimeError):d.pause(a,'gravity')
        self.assertEqual(a.calls,[])
    def test_rejection_not_success(self):
        a=Arm();a.gravity_compensation=lambda:False
        with self.assertRaises(RuntimeError):d.pause(a,'gravity')
    def test_nonfinite_blocks(self):
        a=Arm();a.get_joint_positions=lambda:np.full(7,np.nan)
        with self.assertRaises(ValueError):d.pause(a,'joint')
if __name__=='__main__':unittest.main()
