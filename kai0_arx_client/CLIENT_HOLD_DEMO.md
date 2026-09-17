# 测试 client 的 Ctrl+C 保持

## 官方对照模式 EXIT 归位更新

`--official-demo` 模式输入 EXIT 后，先调用 SDK `go_home(1, wait=True)` 依次归位左、右臂，再保护并关闭。暂停阶段仍保持当前位置；不会因 10 秒完成就自动归位。故障、终端断开或取消归位不触发额外归位。默认原地测试及 `--move-j6` 的 EXIT 行为不变。以下旧说明中“不在退出时自动回零”已由本节替代。

退出归位期间 Ctrl+C 会中止流程并保护退出，可能失去支撑。当前运行中的旧进程不会自动获得更新。

## 启动反馈检查与官方对照（2026-09-16 更新）

SDK 初始化后，在保护状态下以约 50ms 间隔读取反馈。3 秒内必须取得连续 10 次有限反馈，且左右夹爪在 `get_arm_model({'type': 2}).gripper_range` 范围内，才发送位置目标。异常样本记入 `startup_feedback_rejected`，包含左右反馈、限位与差值；SDK fault 直接退出。此检查不标定零点、不裁剪数值，也不证明底层每个 CAN 包都是新鲜数据。若错误发生在 SDK 构造函数内部，仍需要 SDK 的完整终端输出定位。

进入测试后，夹爪异常反馈或目标也会阻止命令并转保护退出。此检查只在本测试脚本中使用 SDK 的范围，不修改 client.py、GUI 或 SDK。

官方动作对照：

```bash
cd ~/Documents/zhanghaoyi/kai0_arx_client
.venv-inference/bin/python client_hold_demo.py --enable-motion --official-demo
```

此选项使用官方默认 URDF、依次 `left.go_home(1, wait=True)` 和 `right.go_home(1, wait=True)`，然后按约 100Hz 向双臂发送六关节全为 0.2 rad、夹爪 -1 rad 的目标。沿用 SDK 默认平滑参数，关节 duration=0，夹爪不覆盖 duration。与官方示例不同，保留故障/反馈检查和看门狗，10 秒或 Ctrl+C 后进入 client 保持流程，不在退出时自动回零。日志写盘可能影响上层周期，不承诺精确 100Hz。

**初始化、反馈确认和归位阶段 Ctrl+C 是取消并保护退出；归位结束进入目标运行阶段后 Ctrl+C 才是暂停并保持。** 归位任一臂失败或超时不会继续另一臂。此选项会移动所有关节及夹爪，请在现场确认完整路径；无 `--enable-motion` 只显示计划。

本次 50 项离线模拟测试通过，未执行真机动作。已运行的旧进程不会自动加载更新；支撑双臂后正常 EXIT，再运行新脚本。

本脚本复用 `SDKRobot.hold_current()` 和 `cli_hold_until_exit()`，不连接模型或相机。不修改官方 SDK。默认参数未带 `--enable-motion` 时只打印计划，不初始化硬件。

在机器人电脑上运行：

```bash
cd ~/Documents/zhanghaoyi/kai0_arx_client
.venv-inference/bin/python client_hold_demo.py --enable-motion
```

先在 GUI 断开控制、放稳双臂、清空夹爪并确保周围无人处于运动范围，再输入 START。初始化会经过保护模式。脚本向双臂及夹爪发送当前反馈作为目标；按 Ctrl+C 或等 10 秒后，重新读取当前位置，进入与 client 相同的保持流程。再次 Ctrl+C 仍保持。放稳双臂后输入 EXIT 回车，才保护并关闭 SDK。EOF、终端断开、故障或看门狗超时仍走保护；进程被强杀不能保证保持。

需要附加微动时：

```bash
.venv-inference/bin/python client_hold_demo.py --enable-motion --move-j6
```

此选项请求右臂 J6 约 0.5° 单程位移，无自动回程，使用 client 的默认 SDK 调用。运动可能很快完成；10 秒是等待按键的时间，不是运动时长，因此不能凭此证明中途取消长轨迹的效果。

保持不是 kill，也不是 Python 循环重发目标：一次位置目标后，Python 每约 50ms 检查反馈并更新看门狗，SDK 控制线程持续运行。包括夹爪，区别于旧 pause_mode_demo.py 只下发六个关节目标的 JOINT 测试。旧脚本 Ctrl+C 会保护退出，不用于验证新的 Ctrl+C 行为。

观察保持后双臂是否稳定、有无漂移、声音是否变化，第二次 Ctrl+C 后是否仍显示保持。日志为 logs/client-hold-*.jsonl，记录初始目标、可选微动目标、进入保持前反馈、暂停/完成、保持目标及明确退出事件；保持期间不逐帧落盘。44 个离线模拟测试已通过，不代表真机保持和声音已经验证。
