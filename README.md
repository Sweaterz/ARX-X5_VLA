# ARX X5 · Kai0 VLA Console

ARX X5-2025 双臂 VLA 客户端、GUI 和 LeRobot PI05 推理服务。此仓库是 `.136` 部署源码快照，不是 Kai0 官方完整仓库，也不包含官方 ARX SDK 或模型权重。

## 功能

- GUI：设备连接、双臂归位、VLA 执行、暂停保持、软件紧急停止、Server 管理。
- 独立相机进程；运动限位与平滑使用官方 SDK，故障保护与反馈检查保留。
- DAgger：当前双臂重力补偿拖动纠正，完整 episode 录制、人工标签、视频回看，三种 LeRobot v3.0 导出方式。
- Replay：按记录的双臂/夹爪反馈回放，支持分段、速度选择和暂停保持。

## 2026-10-08 更新说明

本次基于 `qijun@192.168.2.136:~/Documents/zhanghaoyi` 的部署源码更新，相对首次上传的 `4651c90`，同时更新 Client 与 Server。未拷贝或改动正在运行的官方 SDK，不包含 checkpoint 权重、采集数据和机器私有配置。

### 从部署源码同步的变化

| 部分 | 本次行为 |
|---|---|
| Checkpoint | GUI 可浏览服务所在电脑的文件夹，选择完整 `pretrained_model`；检查 PI05、14 维绝对动作、三相机、关节顺序以及权重/预处理/tokenizer 文件，持久化选择。 |
| Prompt | 切换 checkpoint 后自动更新任务描述；同一模型下的手动修改不会被轮询覆盖，活动 DAgger 试验继续使用开始时锁定的描述。未知模型清空旧描述，等待输入。 |
| 运动步数 | GUI 与 DAgger 默认总步数 `0` 表示持续运行；正整数仍可限定总步数，不再设 300 步上限。CLI 可显式使用 `--steps 0`，其命令行默认仍为 30 步。 |
| 动作块 | 新增“每块执行步数”，`0` 使用 Server 推荐值；已核实的 `local/fold_box_v1_pi05` 按原部署执行最多 50 个有序动作，place_plate 保持最多 8 个。正整数覆盖值不能超过模型动作块长度。 |
| 下发时序 | 按控制频率顺序下发；晚于计划时间超过 250 ms 时停止，不追赶补发。持续运行的动作请求逐块写入日志。 |
| 人工接管 | 停止新策略动作并作废旧预测，保留 SDK 最后下发的关节与夹爪目标 1 秒；再用实测位置重建 JOINT 保持，等待至少 0.2 秒反馈稳定后依次切换双臂重力补偿。稳定等待超过 0.75 秒则取消接管并保持。 |
| 夹爪诊断 | GUI 增加左右夹爪位置、速度、电流反馈及诊断错误；不会将 AC one 的负反馈二值化。 |
| Server 与设备 | 本机模型管理不再要求先断开机械臂；启动/检查在独立线程中进行。运行中关闭 Server 先暂停并确认保持。新增占用状态、“结束使用并释放设备”和已知服务的连接冲突处理。 |
| 数据工具 | 人工段起始静止帧裁剪、另存原始清洗副本、批量人工导出及可选 SSH 上传工具；人工/完整/指定片段三种导出方式继续保留。 |

fold 的 50 步建议由新版 Server 的 `recommended_actions_per_chunk` 提供，Client 的自动值会读取该字段。只更新 Client、仍连接旧版 Server，自动值可能回退到客户端配置（示例为 8 步）；应配套更新 Server 或明确设置每块执行步数。完整 50 帧中每一帧仍可被暂停、接管、急停或故障中断。

人工接管的“1 秒”从停止新动作后计时，不是等待最后目标已经到达；后续反馈稳定检测也不构成碰撞或受力检测。切换期间继续维护反馈和故障检查，页面失联、暂停与紧急停止会取消切换。

### 发布前审查修复

- **连接冲突修复仅允许本 GUI 的机械臂与相机全部断开。** 防止外部服务检查阻塞控制维护或抢走另一页面的实时控制权；设备仍连接时先使用明确的释放流程。
- **GUI 的 human 起始静止裁剪改为默认关闭、按需启用。** 启用后只按 12 个机械臂关节检测拖动起点，夹爪不参与判断，因此夹爪单独示教应保持此选项关闭。原始 HDF5 和视频不会被导出裁剪覆盖。
- **前端依赖锁更新。** 升级传递依赖 `source-map-js`，本次 `npm audit` 检查无已知漏洞。
- **批量导出严格匹配已完成的导出记录与本次选项。** 不将完整/指定片段或不同清洗设置的旧导出当作人工段结果复用；可选上传会先创建报告目录，再写入报告。

这些修复只应用于本次 GitHub 发布副本，没有覆盖 qijun 电脑正在使用的部署目录，也没有操作机械臂。更新部署时仍需按设备状态正常停止后台。安装与无硬件验证方法见下文，新增测试覆盖动作块、接管等待、Server 独立管理、设备释放和数据导出。

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

启动脚本默认使用原部署机的 `/home/qijun/ARX5_beta/.venv/bin/python`；其他环境请设置 `ARX_SERVER_PYTHON`，GUI 后台也应继承该变量。默认仅本机访问推理端口；跨机器服务需显式增加 `--host 0.0.0.0`。GUI 本地 Server 管理使用 8001，可选择相邻 Server 的 checkpoint、部署机 fold 目录或兼容的自定义模型文件夹。直接启动其他模型时传入 `--checkpoint /absolute/path/to/pretrained_model --prompt '对应任务描述'`。

## DAgger 数据环境

数据操作使用独立环境 `kai0_arx_client/.venv-data`（可用 `ARX_DATA_PYTHON` 指定其他解释器）。需安装 h5py、NumPy、Pillow、PyAV；导出及 PI05 加载验证还需要 LeRobot 0.6.1 及其模型依赖。原部署使用的 `data_deps`、`vendor_deps` 没有上传，不能假设新环境已有这些依赖。不要向 SDK 环境直接安装数据处理依赖。

```bash
cd kai0_arx_client
python3.12 -m venv .venv-data
.venv-data/bin/python -m pip install 'lerobot==0.6.1' 'h5py==3.14.0' 'av==15.1.0' Pillow
# PI05 预处理验证须准备兼容的模型依赖及本机可读取的 checkpoint。
```

以上是分环境安装入口，不是全平台依赖锁定；服务器观察版本见 `RUNTIME_VERSIONS.txt`，部署时仍需核对 PyTorch/CUDA 和 PI05 依赖。批量清洗工具的参数先查看 `python batch_clean_human.py --help`、`python clean_raw_episode.py --help`；两者是显式清洗工具，会启用机械臂关节运动起点检测，不适合直接用于只动夹爪的示教。SSH 上传是可选步骤，不需要在仓库中保存密码或 Token。

参阅 [DAgger](kai0_arx_client/DAGGER.md)、[Replay](kai0_arx_client/REPLAY.md)、[GUI](kai0_arx_client/GUI_README.md)、[控制生命周期](kai0_arx_client/LIFECYCLE_SAFETY.md)。子目录内历史调试说明包含原机器路径和旧配置，以本 README 和当前源码为准。

## 验证与边界

本次发布检查：在 qijun 的独立源码副本中复用既有依赖和只读 checkpoint，**260 项后端测试通过**（包含数据录制/导出加载及新增修复回归）；GUI 生产构建通过，`npm audit` 为 0 漏洞。页面交互检查针对 `--demo` 后端，验证持续运行默认值、Prompt 联动、修复按钮禁用和裁剪开关。未进行真机运动验证。

```bash
cd kai0_arx_client
# config.json 和依赖准备好后；单元测试使用 mock，不下发真机命令。
.venv-data/bin/python -m unittest discover
```

GUI 的 `qa-*.mjs` 是原部署环境的回归脚本，含端口、样本 ID 和 Chrome 路径，需要按测试环境调整；涉及连接操作的流程只用于 demo 后端。

位置保持需要后台及 SDK 持续运行。软件紧急停止不能替代硬件急停。源码上传不代表新机器的真机运动、轨迹或训练数据已经验证。

## 上游来源

客户端内少量协议/图像工具来自 OpenDriveLab/Kai0；来源、提交和 Apache-2.0 许可保留在各 `vendor/` 目录。其余代码未在本次上传中额外指定开源许可证。官方 SDK 不包含在此仓库，也未修改。
