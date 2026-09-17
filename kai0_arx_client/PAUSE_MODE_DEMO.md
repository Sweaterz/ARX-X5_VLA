# SDK pause comparison

Sources checked 2026-09-16:
- Official README: https://github.com/ARXroboticsX/ARX5_beta
- Official example: https://github.com/ARXroboticsX/ARX5_beta/blob/main/dual_arm_demo.py
- Installed bimanual.hardware.controller.MODE_NAMES: 0 IDLE, 1 PROTECT, 2 GRAVITY, 3 JOINT, 4 EE_POSE.

README describes joint control with underlying gravity compensation, feedback, and gravity compensation. It does not enumerate all internal modes. Five-mode enumeration above was verified by importing the installed official SDK without constructing hardware. No separate HOLD mode exists. SingleArm.hold(seconds) is a wait helper, not an actuator hold mode. Do not equate PROTECT to guaranteed physical braking.

Script: pause_mode_demo.py. Environment: client .venv-inference (reuses official .venv-control SDK dependencies). Direct SingleArm({'type':2,'can_port':...}) calls, default SDK URDF/tuning. No model, cameras, GUI, gain overrides, homing or gripper target commands. Only selected arm initialized; other arm is not controlled or supported by this test. Shared controller flock prevents compliant competing controllers.

Default prints plan only. Supervised hardware commands, ONE at a time:

```bash
cd ~/Documents/zhanghaoyi/kai0_arx_client
.venv-inference/bin/python pause_mode_demo.py --mode joint --enable-motion
.venv-inference/bin/python pause_mode_demo.py --mode gravity --enable-motion
.venv-inference/bin/python pause_mode_demo.py --mode protect --enable-motion
```

Default right arm; add --side left for left. Disconnect GUI/controllers first. Empty gripper, support both arms, verify small wrist rotation clearance. Each run requires typing START before any hardware initialization. Restore comparable starting posture manually before the next run; no automatic return.

To create an observable mid-trajectory interruption, test requests a 0.5-degree J6 excursion over 3 seconds, pauses after approximately 0.5 seconds. Duration=3 is an intentional test choice (documented official API), not an exact replay of the demo's duration=0. Actual displacement can be very small; compare recorded feedback instead of assuming movement happened.

joint: capture measured six-joint pose and set_joint_positions(..., duration=0) once. gravity: gravity_compensation(). protect: protect_mode(). No gripper command in any pause transition.

Logs logs/pause-mode-*.jsonl include raw get_status, positions, velocity, SDK current feedback, actual URDF, and five-second drift/current-RMS summary. Current values keep SDK raw units; no unsupported conversion to amps or torque. No microphone recording or automatic noise diagnosis.

After summary the chosen mode stays active. Place/support arm, then type q or Ctrl+C to request protection and close SDK. Test stops on SDK fault, nonfinite feedback, feedback-loop stall (~2 seconds), or arm-joint drift beyond 5 degrees. These are experimental guardrails, not collision detection or certified emergency stop; PROTECT can lose support, and blocked SDK/system failure can prevent stopping. Use physical emergency stop for danger.

Four mock tests pass: correct mode-specific call, fault rejection, SDK rejection, nonfinite feedback. Plan-only launch verified. No hardware run performed. GUI mode behavior is not changed by this script.

Compare sound, drift, and load together. Gravity compensation permits movement and depends on calibrated payload; it is not position locking. Test does not validate holding a grasped object because no gripper experiment is included.
