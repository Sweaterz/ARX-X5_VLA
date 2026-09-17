# Kai0 DAgger 检查与 ARX GUI 适配评估

检查日期：2026-09-17。仅源码检查与隔离的纯 Python 探针，没有启动机器人、相机、ROS 节点、采集或训练。

## 结论

官方 Kai0 已有 DAgger-style 人工接管采集，支持 ARX 和 Agilex。当前 kai0_arx_client 没有 DAgger 采集器、人工示范 action 来源、episode 保存/删除/校验或数据集训练管理；已有 JSONL 诊断日志不等于带图像和监督动作的数据集。

可适配，但不是直接运行官方 ARX 脚本即可接入当前 GUI。建议复用其数据字段、接管分段思路和转换/训练工具，保留现有 SDK 单一控制入口、相机独立进程与保持/故障处理。官方采集器和转换器存在需修复的具体问题，不能直接用于可靠的训练数据闭环。

## 仓库来源

官方：https://github.com/OpenDriveLab/kai0
已克隆：/home/qijun/Documents/zhanghaoyi/kai0
命令：git clone --depth 1 https://github.com/OpenDriveLab/kai0.git kai0
提交：9d93078c757840f50e75248c5c5a94ab7b41e13a（浅克隆，完整当前主仓库文件，不含历史完整记录）。工作区保持干净，未安装源码环境、编译 ROS 或运行硬件脚本。

192.168.2.117:/home/zhhy/projects/kai0 的 origin 是 Sweaterz/kai0，基准提交与上述官方提交一致，但其训练配置/依赖/本地脚本有修改。本次未覆盖该目录。

## 已实现的内容

- train_deploy_alignment/dagger/arx/arx_openpi_dagger_collect.py：WebSocket 推理、动作缓冲、主臂对齐、人工接管/恢复、自动和人工帧采集。
- SimpleDAggerCollector：后台队列、episode 编号、HDF5 关节数据、逐帧 intervention=0/1、JSON 标签、可选三路视频。
- train_deploy_alignment/data_augment：HDF5/视频转换、数据集合并和增强。
- src/openpi/training 与 scripts：训练工具；未发现把当前 ARX GUI 接管、保存、聚合、训练、评估、发布串联的一键迭代流程。可人工组织迭代，不等于整套闭环已集成。

## 关键缺口与证据

1. **控制平台不同。** 官方 ARX README 要求 ROS2、两只主臂和两只从臂，脚本通过 RobotStatus 话题与 SetParameters 切换主臂模式。当前 GUI 使用 /home/qijun/ARX5_beta SingleArm 和 can1/can3 控制双臂；当前配置未提供独立主臂控制接口。不能让两套控制程序同时接管同一 CAN。需确定采用主臂遥操作还是经验证的拖动示教；拖动时不能仍保持锁定 JOINT 目标。
2. **夹爪不兼容。** 官方脚本 1690 附近直接调用 apply_gripper_binary(act)，函数 1751 附近默认把夹爪转成 0 或 5，未传入命令行 gripper 参数。当前 AC one 软件参考范围是 -3.4 到 0.1。隔离探针输入 -3.3/-0.5 均得到 0，证明原动作语义被改变。不能沿用该处理。
3. **保存边界缺少队列屏障。** _writer_loop(416)、_do_save(434)、save_current_episode(544)：每取一帧就可能处理保存标志，而不是处理完保存请求之前的所有帧。探针排入 3 帧再请求保存，首个 episode 只交给保存函数 1 帧，其余 2 帧仍在队列。它们可能进入下一 episode；这不是立即全部丢失，但分段边界不可靠。探针用保存替身，不创建真实 HDF5。
4. **退出不能保证写完。** shutdown(555) 直接置 writer_running=False、join 最多 5 秒，没有队列 drain/最终保存事务；main 最终 os._exit(0)。未保存帧或未完成的视频写出有丢失/截断风险。保存直接以 w 打开目标 HDF5，缺少整个 HDF5+视频+JSON 的临时目录提交和完整性标记。
5. **训练转换改变 action 并丢掉接管标签。** convert_h5_lerobot.py 导入 mini_lerobot/interface.py 的 lazy_load_hdf5_dataset_noimg。后者约 60–77 行将 action 设置为 observations/qpos，不读取 HDF5 /action，也不保留 intervention；features.json 同样没有 intervention。探针用不同的 state/action 数组确认这一行为。对当前绝对目标角监督，需明确保留哪一种 expert action 及时间对齐，不能不加检查地复制。
6. **数据新鲜度和接管可靠性需补齐。** get_frame(983) 取最新左右反馈而没有统一时间戳同步；get_camera_images(1037) 可无限回退到旧图。进入 DAgger 时的 set_master_mode 调用未逐一核验返回成功；推理线程在返回结果后未再次核验接管 generation，因此仅 clear buffer 不足以证明旧请求结果不会晚到。应加入反馈/图像时间戳、过期拒收、控制权确认及推理代次失效处理。
7. **默认启动行为不适合直接复用。** auto_homing 使用 store_true 且 default=True，没有关闭选项；源码启动流程还涉及主从臂动作。本次没有运行 --help 或导入整个入口模块，仅 AST 抽取纯函数/采集类做验证。
8. **运行依赖未接入现有客户端环境。** 当前 .venv-inference 中未找到 rclpy、h5py、dm_env、av、openpi_client；/opt/ros 未找到。只说明当前所查环境，不断言整机所有环境都没有。现有 WebSocket 协议可复用 client.Policy/vendor，没必要为复用整个入口而引入另一套机器人控制栈。

## 建议的适配顺序

1. 继续使用当前 GUI+SingleArm，先定义人工 action 的输入方式和控制权状态机：策略运行→停止接受旧动作→保持确认→人工接管→结束接管→新观测重新推理。相机仍独立进程，保存/编码另用工作进程，不能重新把视频写出压回 SDK 控制进程。
2. 增加统一采样与 episode writer：同一帧包含单调时间戳、相机时间戳、SDK 反馈、原始策略 action、实际请求 action、expert action、intervention、task、checkpoint 标识及控制模式。区分目标与实测姿态，不能把 state 静默冒充 action。
3. 实现开始/停止录制、保存、丢弃本段、完成确认；停止边界必须有队列屏障，保存完成才能确认成功。用临时文件和原子提交，退出等待写完，磁盘不足/编码失败可恢复。
4. 修正并验证转换器：保留指定监督 action 和 intervention，统一 RGB、head/left/right 命名、14 维顺序、弧度及 AC one 夹爪语义。当前 LeRobot 0.6.1/checkpoint 预处理器兼容性需实际小数据集转换和加载测试；不能只看形状相同。
5. 用无硬件小样本测试保存/重启恢复/取消/磁盘错误/视频帧数/训练加载，再做现场接管；最后增加旧数据+修正数据的采样配比、微调与固定评估集验证。当前仅确认 4090 推理能力，没有验证微调显存和训练配置。

## 验证记录

client/audits/check_official_dagger.py：只 AST 抽取 SimpleDAggerCollector、apply_gripper_binary、lazy_load_hdf5_dataset_noimg，使用合成数组、模拟 HDF5/保存函数，无机器人/ROS/相机导入执行。
结果：client/audits/official_dagger_probe.txt。确认队列保存边界、夹爪二值化、转换器 action 来源和 intervention 丢失。

本次没有修改官方源码、现有 GUI 控制逻辑或 server；没有安装新依赖，没有采集真实数据，也没有运行训练。
