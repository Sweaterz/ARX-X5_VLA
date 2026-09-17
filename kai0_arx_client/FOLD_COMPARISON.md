# fold VLA 执行与 Kai0 GUI 对比

2026-09-16，在 qijun@192.168.2.136 只读检查。实际目录 `/home/qijun/lyt/fold`；用户提及的 `/home/lyt/fold` 未找到。未启动任何 fold 控制脚本，未修改 fold 或官方 SDK。

## 执行链

`scripts/run_fold_box_control_ui.sh` → `src/deployment/fold_box_control_ui.py`（FastAPI UI）→ 独立子进程 `scripts/run_fold_box_policy.sh` → `src/deployment/fold_box_policy_robot.py`。

执行脚本先本地加载 LeRobot ACT / PI0.5 和 checkpoint pre/postprocessor，再获取控制锁，启动三台 RealSense 并等待就绪，然后初始化 can1/can3 的 SingleArm(type=2)。使用与 Kai0 GUI 相同的 0.65kg URDF。用户确认 RUN 后开始执行。模型 select_action 使用动作队列，队列耗尽才重新生成一个 chunk。

## 关键差异

| 项目 | fold | 当前 Kai0 |
|---|---|---|
| SDK 型号、CAN、URDF | type=2，can1/can3，同一 URDF | 相同 |
| 进程 | Web UI 单独进程；SDK、相机、模型在执行子进程 | Web UI 后端、SDK、相机同进程；模型在 .117 |
| 相机启动 | 先相机就绪，再初始化机械臂 | 按钮可在机械臂控制已启动后连接相机 |
| 采集 | RealSenseRig 每相机持久线程持续 30fps，控制循环取最新缓存帧 | 每次 read 顺序 wait_for_frames；GUI 在临时 I/O 线程执行该调用 |
| 帧有效性 | 每帧接收年龄≤250ms，三帧接收时间差≤100ms | 观测整体年龄≤1.5s；没有逐帧年龄/三帧时间差检查 |
| 预览 | OpenCV JPEG，5Hz 写 /tmp，UI 进程读文件 | PIL JPEG，约4Hz，由同一后端 HTTP 返回 |
| 模型 | 本机 CUDA 的 ACT/PI0.5，LeRobot select_action 队列 | 远程 websocket PI0.5，每次返回50步 |
| 执行动作数 | 脚本默认50；UI从checkpoint读配置。PI0.5=50，两个ACT=10，可改 | 每次只执行8步，再推理 |
| 上层频率 | 最大30Hz，按上次发送完成时间补足周期 | 目标30Hz；GUI每步处理后额外sleep(1/30)，实际间隔含处理耗时 |
| 关节目标 | duration=0 | duration=0 |
| 夹爪目标 | 显式 duration=0 | 省略 duration，SDK签名默认None；未验证None内部是否等价0 |
| 保持 | 捕获一次当前位置；暂停期间约30Hz重发同一目标，包含夹爪 | 捕获并发送一次目标，此后只读反馈维护看门狗；SDK线程继续运行 |
| 上层限幅 | 数据集绝对范围 + 相对当前反馈的逐步限幅 | 按用户要求取消重复手写限幅，交给SDK，参考表只诊断 |
| 归位 | 独立 reset_arx_home.py，左右依次8秒，含夹爪，结束保护关闭 | 左右依次1秒，结束 JOINT 保持 |
| 退出 | stop标志后finally保护、关闭SDK；没有先归位 | 正常退出请求先保持，等待明确释放；硬件故障/急停仍保护 |

## fold 的限幅细节

`fold_box_policy_robot.py:217` 的 limit_action：先按 fold_box_v1 训练数据 ACTION_MIN/MAX clip，再把 target-state 限制为关节 ±0.06599528 rad（约3.78°）、夹爪 ±0.44523156 rad（约25.51°），最后再次按绝对范围clip。这里 state 是当前反馈，不是上一次命令，所以不能把它等同于严格的相邻命令速度限制。

这确实会改变发给 SDK 的动作，与 Kai0 原样传递模型输出不同。它可能减少 VLA 的目标跳变，但不是抖动已解决的证据，也不能把折盒子数据集边界直接用于 Kai0 的放盘子任务。本次未重新加入这些限幅。

## 与本次抖动的关系

1. 相机持续采集/缓存与按需顺序取帧是实际差异，优先参考缓存方式减少观测读取阻塞。fold 也在 SDK 所在进程中运行相机，故“同进程一定导致抖动”的判断不成立。
2. 保持时30Hz重发固定目标与一次下发不同。是否改变实际电机行为必须对照，不能仅从代码确定重发一定更稳定。
3. 夹爪 duration=0 与默认None值得单独对照；默认None的实际内部时长尚未证实。
4. 50步队列 vs 8步重新规划、逐步限幅、发令周期计算，会影响 VLA 目标连续性。增加连续执行步数也会降低重新观察/纠偏频率，不能直接认定50步对当前任务更好。
5. 同SDK/同URDF不能推出控制行为相同。现有代码比较能解释差异，但没有真机A/B控制周期与声音数据，仍不能确认唯一根因。

## 反复占用资源的线索

`scripts/run_fold_box_policy.sh:7` 定义 restore_services，EXIT/INT/TERM trap 会自动 start arx-data-station.service 和 arx-button-control.service。运行或退出这套测试可以重新占用三台相机和机械臂锁，甚至脚本参数检查失败也可能触发 EXIT trap。不能把这些启动脚本当作无副作用的帮助命令运行。本次只读取文件。

## 主要源码位置

- fold_box_policy_robot.py:179 相机缓存与观测；217 限幅；227 下发；358 初始化；428 执行循环；440 暂停；526 退出保护。
- /home/qijun/ARX5_beta/arx_data_station/cameras.py 的 RealSenseRig：每台相机持久采集线程及缓存。
- fold_box_control_ui.py:150 子进程启动；discover_checkpoints 读取实际 n_action_steps。
- reset_arx_home.py:63 左右依次8秒归位。
- 已安装 LeRobot 的 pi05/modeling_pi05.py:1024 select_action：队列为空时生成chunk，然后逐项popleft。
