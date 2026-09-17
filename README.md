# ARX X5 · Kai0 VLA Console

ARX X5-2025 双臂 VLA 客户端、GUI 和 LeRobot PI05 推理服务。此仓库是 `.136` 部署源码快照，不是 Kai0 官方完整仓库，也不包含官方 ARX SDK 或模型权重。

## 功能

- GUI：设备连接、双臂归位、VLA 执行、暂停保持、软件紧急停止、Server 管理。
- 独立相机进程；运动限位与平滑使用官方 SDK，故障保护与反馈检查保留。
- DAgger：当前双臂重力补偿拖动纠正，完整 episode 录制、人工标签、视频回看，三种 LeRobot v3.0 导出方式。
- Replay：按记录的双臂/夹爪反馈回放，支持分段、速度选择和暂停保持。

## 目录

```text
kai0_arx_client/   客户端、React GUI、DAgger、Replay、测试及说明
kai0_arx_server/   PI05 WebSocket 服务、checkpoint 校验及启动脚本
```

保留目录名称和相邻关系：GUI 使用 `../kai0_arx_server` 找到本地服务。权重、数据、虚拟环境、日志、真实配置和验证完成标记未上传。`SOURCE_MANIFEST.json` 仅包含当前 place_plate/015000 权重的校验信息，不含权重本身。

## 客户端配置与 GUI

Linux 运行环境使用 Python 3.12；真机需要另外安装与硬件匹配的 ARX5_beta SDK、CAN 配置及对应 URDF。

```bash
cd kai0_arx_client
cp config.example.json config.json
# 修改 SDK/URDF 绝对路径、CAN 设备与三路相机序列号。
python3.12 -m venv --system-site-packages .venv-inference
.venv-inference/bin/python -m pip install -r requirements.txt numpy Pillow
cd gui
npm ci
npm run build
cd ..
./start_gui.sh --demo --host 127.0.0.1 --port 8092
```

先以演示模式检查页面。真机运行移除 `--demo`；SDK Python 绑定及其依赖必须对 `.venv-inference` 可见。上述通用安装命令不会安装 ARX SDK。不要将未知 ABI 的绑定混入环境。

正式部署 GUI 默认监听 `0.0.0.0:8092`，Host 白名单当前为 localhost、127.0.0.1、192.168.2.136。其他机器部署时需调整 `gui_server.py` 的 `valid_host()`；页面控制令牌不是用户登录认证，应仅在可信局域网使用。

## Server

已运行环境的关键版本见 `kai0_arx_server/RUNTIME_VERSIONS.txt`。它是版本记录，不承诺任意机器直接安装即可复现；PyTorch CUDA 构建须匹配驱动。服务需要 LeRobot PI05、Transformers、tokenizer 和完整预处理/后处理文件。

将获得授权的模型文件自行放到：

```text
kai0_arx_server/checkpoints/place_plate/015000/pretrained_model/
```

```bash
cd kai0_arx_server
# 当前清单仅适用于本次 place_plate/015000，不适用于任意 checkpoint。
python3 local_scripts/verify_checkpoint.py
ARX_SERVER_PYTHON=/absolute/path/to/server-env/bin/python \
  ./local_scripts/start_arx_lerobot_server.sh --port 8001
```

启动脚本默认使用原部署机的 `/home/qijun/ARX5_beta/.venv/bin/python`；其他环境请设置 `ARX_SERVER_PYTHON`，GUI 后台也应继承该变量。默认仅本机访问推理端口；跨机器服务需显式增加 `--host 0.0.0.0`。GUI 本地 Server 管理固定使用 8001 和上述 checkpoint 位置。

## DAgger 数据环境

数据操作使用独立环境 `kai0_arx_client/.venv-data`（可用 `ARX_DATA_PYTHON` 指定其他解释器）。需安装 h5py、NumPy、Pillow、PyAV；导出及 PI05 加载验证还需要 LeRobot 0.6.1 及其模型依赖。原部署使用的 `data_deps`、`vendor_deps` 没有上传，不能假设新环境已有这些依赖。不要向 SDK 环境直接安装数据处理依赖。

参阅 [DAgger](kai0_arx_client/DAGGER.md)、[Replay](kai0_arx_client/REPLAY.md)、[GUI](kai0_arx_client/GUI_README.md)、[控制生命周期](kai0_arx_client/LIFECYCLE_SAFETY.md)。子目录内历史调试说明包含原机器路径和旧配置，以本 README 和当前源码为准。

## 验证与边界

```bash
cd kai0_arx_client
# config.json 和依赖准备好后；单元测试使用 mock，不下发真机命令。
.venv-data/bin/python -m unittest discover
```

GUI 的 `qa-*.mjs` 是原部署环境的回归脚本，含端口、样本 ID 和 Chrome 路径，需要按测试环境调整；涉及连接操作的流程只用于 demo 后端。

位置保持需要后台及 SDK 持续运行。软件紧急停止不能替代硬件急停。源码上传不代表新机器的真机运动、轨迹或训练数据已经验证。

## 上游来源

客户端内少量协议/图像工具来自 OpenDriveLab/Kai0；来源、提交和 Apache-2.0 许可保留在各 `vendor/` 目录。其余代码未在本次上传中额外指定开源许可证。官方 SDK 不包含在此仓库，也未修改。
