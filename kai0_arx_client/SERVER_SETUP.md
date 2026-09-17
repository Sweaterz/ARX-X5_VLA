# ARX 局域网 server（2026-09-10）

当前按用户选择部署 **无 checkpoint 通信测试服务**，不是 VLA 推理。

- Server：`192.168.2.117:8000`，项目 `/home/zhhy/projects/kai0`。
- Client：`arx@192.168.2.114:/home/arx/Documents/zhanghaoyi/kai0_arx_client`。
- WebSocket：`ws://192.168.2.117:8000`，官方 msgpack NumPy 编码与 metadata 握手。
- 输入：`state` float32 `[14]`，`images` 中三路 `top_head / hand_left / hand_right` RGB uint8 `[3,224,224]`，以及 `prompt`。也兼容旧 LeRobot dotted keys。
- 输出：`actions` float32 `[50,14]`，每一行等于请求中的当前状态。图像在协议中传输并校验，但没有模型处理图像或生成任务动作。
- `test_only=true`，现有 client 会在相机/SDK 初始化之前拒绝 `--enable-motion`。

## 按需手动启动（192.168.2.117，zhhy 用户）

已取消开机启动和异常自动重启，移除本次安装的 systemd 服务，并恢复用户 linger 为关闭。当前服务已停止。需要通信测试时，在推理机器的终端执行：

```bash
cd /home/zhhy/projects/kai0
./local_scripts/start_arx_server.sh --mock
```

保持此终端打开，日志直接显示在终端；使用完按 `Ctrl+C` 停止，不会自动重启。脚本直接使用项目虚拟环境，无需手动激活。
默认监听 `192.168.2.117:8000`，供当前局域网使用。另一个终端可执行 `curl --noproxy '*' http://192.168.2.117:8000/healthz` 检查服务。
未配置公网映射、TLS 或认证；当前不涉及跨公网部署。
旧 `local_scripts/serve_task_a.py` 进程已停止，脚本和已有 Task_A 权重保留。mock 服务不占用 GPU。

## Client 复测（192.168.2.114）

先按上面的命令启动 server，再在 client 的终端执行：

```bash
cd /home/arx/Documents/zhanghaoyi/kai0_arx_client
# 现有 client 的完整模拟流程，无硬件初始化
./start_client.sh run --prompt 'communication test only' --steps 30

# 只读获取现有 8090 采集站的真实相机预览与状态，发往 server
.venv/bin/python probe_arx_server.py \
  --station http://127.0.0.1:8090 --count 10 \
  --output logs/server-station-latest.json
```

第二个命令要求原采集站正在运行。它仅 GET 预览与状态，不占用 CAN/RealSense、不改变采集状态。
一次抓取的三路 JPEG 与状态并非严格同步；默认将这个快照重复发送 10 次以检查通信，不是实时闭环控制。
不带 `--station` 则使用模拟数据，可在没有连接硬件时运行。

验证结果：

- Server 输入校验与兼容测试：7 项通过。
- 本机 10 次请求通过；client 跨机 10 次模拟请求通过；原 client 30 步模拟流程通过。
- 真实三路相机预览与 14 维机械臂反馈跨机传输成功，10 次请求返回 `[50,14]`，均与输入状态一致。
- 真实快照请求平均往返约 45.8 ms（含序列化、网络与 mock 处理；不是 VLA 推理速度）。
- Client 报告：`logs/server-synthetic-20260910.json`、`logs/server-station-20260910.json`。
- Server 报告副本：`.cache/arx-server/`。
- 本次未初始化机械臂 SDK、未发送运动指令；采集站继续由原程序运行。

## 之后接入自己的 checkpoint

已准备 `local_scripts/serve_arx.py` 的 JAX checkpoint 加载入口，并修复 `LerobotARXDataConfig.create()` 传递不存在的 `episodes` 字段导致的异常。当前只验证了 ARX 配置构建；按本次选择，没有下载或加载 ARX 权重。

准备与训练一致的 ARX config、完整 `params/` 和 checkpoint 根目录 `norm_stats.json` 后，先用 `Ctrl+C` 停止 mock 服务，再在推理机器上手动启动模型服务（替换以下路径、配置名和任务提示）：

```bash
cd /home/zhhy/projects/kai0
./local_scripts/start_arx_server.sh \
  --checkpoint /absolute/path/to/checkpoint \
  --config YOUR_ARX_TRAIN_CONFIG --prompt 'YOUR TRAINED TASK'
```

server 在监听前完成两次预热，避免首次 JAX 编译超过 client 的接收时限。此入口仍固定 `test_only=true`。
加载自己的模型时必须使用对应的训练配置与归一化统计，确认 X5-2025 双臂关节顺序、绝对目标和 SDK 夹爪弧度约定；当前有权重的 Task_A 属于 Piper，不能据此认为已适配 ARX。
未来真机部署需另外完成模型和动作约定验证，然后再启用运动；通信成功不代表模型具备该任务能力。

官方参考：[Kai0](https://github.com/OpenDriveLab/kai0)、[ARX 部署说明](https://github.com/OpenDriveLab/kai0/blob/main/train_deploy_alignment/inference/arx/README.md)。
