# SDK-managed motion — 2026-09-13

The original software limit table in config.json is retained for diagnostics only. It does NOT reject or clip motion. client.SDKRobot.command validates finite [14] input and SDK feedback faults, then submits the original six joint angles and gripper angle to each SingleArm. The returned/logged sdk_request is the raw request, NOT the SDK's internal target or measured position.

No client position tolerance check, position clamp, or per-step clamp remains in the main client path. The old joint_step_rad and gripper_step_rad fields were removed. The compatibility function bounded_target validates finite shape only. Diagnostic reference violations remain in CLI raw_prediction JSONL records and GUI result/event records, without blocking diagnostic inference. Full raw action chunks are logged, including actions outside the reference ranges.

SDK set_motion_smoothness(accel_time=0.5, jerk_time=0.5) is called at arm initialization. get_motion_limits() and smoothness return values are logged. Both set_joint_positions and set_gripper_pos receive duration=0.2 seconds. SDK chooses actual trajectory timing and applies its built-in protection; requested duration is not guaranteed actual arrival time. These initial settings have NOT been physically validated or proven to reduce noise. The SDK's J2/J3 range may differ from the reference table; we do not override its internal model or guard.

Fault checking, SDK bool return checking, ownership flock, finite action/state validation, 2-second software watchdog, 1.5-second observation expiry, GUI stop latch/heartbeat and cleanup remain. Hardware emergency stop is still necessary. No vendor SDK or external URDF files were edited.

25 offline tests passed after migration, including raw request forwarding, diagnostic-only reporting, SDK smoothing and joint/gripper duration arguments, invalid input blocked before dispatch, SDK rejection cleanup, watchdog and GUI no-motion/stop paths. No actual hardware or model inference run was performed for this migration.

The standalone sdk_sound_test.py and micro_motion_test.py remain bounded diagnostic experiments; their deliberately small test excursion checks are not the VLA client's motion limiter.

Backup on client host: logs/before-sdk-managed-limits/

Restart required: disconnect devices in the old GUI, stop that GUI server, then ./start_gui.sh. Do not run both old and new controllers simultaneously.
