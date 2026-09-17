# Kai0 ARX X5-2025 client

部署位置：`arx@192.168.2.114:/home/arx/Documents/zhanghaoyi/kai0_arx_client`。
这是直连当前 ARX SDK 的命令行客户端。默认模拟机器人和图像，不初始化机械臂。

## 使用

在远端运行：

```bash
cd /home/arx/Documents/zhanghaoyi/kai0_arx_client
./start_client.sh doctor
.venv/bin/python -m unittest -v test_client
```

本机模拟联调，两个终端分别运行：

```bash
.venv/bin/python mock_server.py --port 18000
```

```bash
./start_client.sh run --server ws://127.0.0.1:18000 --prompt 'fold the box' --steps 30
```

确定 GPU 服务后，用实际 IP 替换下面的 `GPU_IP`。配置已设为计划中的 `192.168.2.117:8000`，不代表该主机已经部署模型。

```bash
./start_client.sh run --server ws://GPU_IP:8000 --prompt 'YOUR TRAINED TASK' --steps 30
```

此命令只测试真实服务与模拟观测的协议兼容性，不能证明模型适合真机。使用已有状态广播源时可选 `--robot udp`，绑定本机 `8765`；该端口不能被采集站同时占用，不会自动启动广播源。广播时间戳必须来自这台电脑；过期或故障状态会被拒绝。

相机采图检查：

```bash
./start_client.sh camera-check
```

相机检查只采集图像，不初始化 SDK 机械臂。若采集站占用相机，先正常关闭其相机采集再执行。图像保存到 `logs/cameras-*/`。

模型已用匹配的 X5-2025 数据训练、硬件故障已排除且原采集控制器退出后，真机入口为：

```bash
./start_client.sh run --server ws://GPU_IP:8000 --prompt 'YOUR TRAINED TASK' \
  --robot sdk --cameras realsense --enable-motion --steps 30
```

`--steps 0` 持续运行。Ctrl+C、SIGTERM、异常退出会请求 SDK `protect_mode()` 并关闭资源；不回零。SDK 初始化本身可能激活控制线程，所以真机后端必须显式启用运动。真实机械臂动作和 SDK 初始化尚未验证。

## 数据约定

- 机型：X5-2025，SDK `type=2`；左臂 `can1`，右臂 `can3`。
- `state`: float32 `[14]`，顺序为左六关节、左夹爪、右六关节、右夹爪。
- 状态与动作均为 SDK 原始弧度，夹爪也是角度；不是 Piper 的米，也不是旧款 ARX 的 `0..5` 夹爪范围。
- 当前采集器直接保存 SDK 七维反馈，动作是下一帧状态。客户端因此接收反归一化后的绝对关节目标。训练和服务端必须保持这一约定。
- `images`: `top_head`、`hand_left`、`hand_right`，RGB uint8 CHW，各 `3×224×224`；按官方 image_tools 等比例缩放补黑边。
- 相机序列号来自已有采集站配置：head `409122273248`，left `260322272716`，right `409122274457`。请在首次真机使用前目视核对实际安装位置。
- 返回值：`actions` 为 `[T,14]`。不接受批次维、NaN、Inf、错误维度或越界目标。
- 网络使用官方 msgpack NumPy 编码、WebSocket 初始 metadata 握手。默认端口 `8000`，不经系统 HTTP 代理。

现有 Piper GUI 的 `active_state` 协议不是本客户端的默认协议。GPU 应运行 Kai0 的 ARX policy transform，不要直接使用 Piper checkpoint。

## 执行与限制

首版使用同步 chunk 推理，每次最多执行 8 步，步频 30 Hz；推理等待会使整体平均步频低于 30 Hz。没有实现 RTC、异步预取或 temporal ensembling。

关节每步变化上限默认 `0.01 rad`，夹爪 `0.02 rad`，逐步相对实时反馈限制；配置限位来自已安装 SDK 文档，夹爪采用文档中较保守的 `[-3.14, 0.1]`。这些属于客户端限制，不保证无碰撞，也不代替 SDK 保护。限位配置在 `config.json`。

观测到执行超过 1.5 秒即拒绝剩余 chunk；独立 2 秒 watchdog 请求保护并锁定会话，不能自动恢复。软件 watchdog 依赖 Python 调度和 SDK API 返回，无法覆盖 SDK 原生代码永久阻塞或操作系统故障。SDK 未公开逐关节反馈时间戳，客户端检查 SDK fault 和反馈数值，不能独立证明每一条 CAN 反馈的新鲜度。

与原控制器共用 `arx-arm-control.lock`，锁冲突时拒绝初始化，不终止其他进程。关节或夹爪命令失败会请求双臂保护。日志保存在 `logs/run-*.jsonl`，包含反馈、原始动作、限幅目标和时延。

## 安装环境

本机 SDK 是 CPython 3.12 的二进制扩展，不能直接套用官方文档里的 Python 3.10 环境。部署使用 Python 3.12 venv，复用系统已安装的 NumPy、Pillow 和 Pinocchio，只在新目录内安装 WebSocket、msgpack 和 RealSense Python 包：

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
```

需预先存在 `/home/arx/ARX5_beta` 和正常可导入的系统 SDK 依赖。未运行 SDK setup.sh，未修改系统 Python、CAN 配置、原采集项目或训练数据。

## GPU 服务参考

按官方仓库选择你实际训练的 ARX config 与 checkpoint，在 GPU 主机的 Kai0 项目运行：

```bash
uv run scripts/serve_policy.py --port=8000 policy:checkpoint \
  --policy.config=YOUR_ARX_TRAIN_CONFIG --policy.dir=/path/to/checkpoint
```

配置名称、权重路径和地址尚未确定，客户端未虚构这些参数，也没有部署 GPU 服务。

参考：[Kai0 仓库](https://github.com/OpenDriveLab/kai0)、[ARX 部署说明](https://github.com/OpenDriveLab/kai0/blob/main/train_deploy_alignment/inference/arx/README.md)、[ARX 输入输出转换](https://github.com/OpenDriveLab/kai0/blob/main/src/openpi/policies/arx_policy.py)。
`vendor/image_tools.py` 与 `vendor/msgpack_numpy.py` 保留官方实现及许可证，来源见 `vendor/SOURCE.json`。未复制官方 ROS2 客户端里旧夹爪二值化和自动回初始姿态的动作逻辑。

## 2026-09-08 部署验证

- 13 项测试通过：官方 NumPy 消息编码、三路相机命名与补边、完整 WebSocket CLI 联调、关节/夹爪限幅、异常动作拒绝、超时、故障停止、控制锁互斥、watchdog、部分初始化与资源清理。
- doctor 通过：Python 3.12 SDK 导入、两个 CAN 接口存在、三台 D405 序列号匹配。
- `.venv/bin/python check_station.py` 通过原采集站的 GET 接口取得 14 维状态和三路 640×480 预览，并验证转换后为官方三路 3×224×224 图像。仅做观测格式验证，没有请求推理或运动。
- 原采集站在 `8090` 运行，并持有机械臂锁、UDP 8765 和相机。直接 RealSense 采集尝试返回 `VIDIOC_S_FMT` I/O 错误；独占采集模式尚未验证，未终止原进程。
- 原站报告双臂 joint1/2/3 故障码 `0x4`（电压过低），mode=`fault`。这是读取到的原控制器状态，不是本客户端触发的故障。
- 未初始化真实 SDK 机械臂、未发送 CAN 控制命令、未做真机动作测试。真实 Kai0 checkpoint 联调仍待服务器地址与模型确定。

最新运行记录、采集站状态及预览在远端 `logs/`，测试结果在 `test-results.txt`。本地源码副本位于 `/Users/haoyi/Documents/zhanghaoyi/kai0_arx_client`。

## 末端负载配置

客户端已对齐当前重力补偿服务：双臂使用 LifEgo 标定的 `X5-2025-gripper-handle-0p65kg.urdf`，路径保存在 `config.json` 的 `robot.urdf_path`。文件不存在时拒绝初始化机械臂。

计划的 GPU 服务地址已按用户指定设为 `ws://192.168.2.117:8000`，端口沿用 Kai0 官方默认值。当前尚无真实模型推理服务，checkpoint 与 ARX 训练配置待部署时确定。
