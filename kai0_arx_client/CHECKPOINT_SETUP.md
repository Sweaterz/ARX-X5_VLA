# ARX place_plate π0.5 checkpoint 联调

当前运行设置已按用户要求改为 `test_only=false`、`motion_enabled=true`，并已重启验证生效。客户端仍需显式传入 `--enable-motion`；关节限位、动作限幅和超时保护未修改。下文只推理标记描述的是初次联调时的设置，真机验证状态仍未由本次开关修改更新。

2026-09-11：真实 checkpoint 已完成加载与新客户端跨机推理验证。本次没有初始化机械臂控制 SDK 或发送机械臂运动命令。

## 当前地址

- Server：`zhhy@192.168.2.117`，Kai0 项目 `/home/zhhy/projects/kai0`，监听 `192.168.2.117:8000`。
- Client：`qijun@192.168.2.136:/home/qijun/Documents/zhanghaoyi/kai0_arx_client`。
- 旧 `.114` 客户端不再用于后续联调。早期 README 中的该地址仅是历史记录。

## 模型与接口

权重目录为 `/home/zhhy/projects/kai0/checkpoints/place_plate/015000/pretrained_model`。这是 LeRobot PyTorch π0.5 的第 15,000 步 checkpoint，不是 JAX `params/`。保留原 JAX/mock 启动脚本，新增独立的 `local_scripts/serve_arx_lerobot.py` 和 `local_scripts/start_arx_lerobot_server.sh`。

加载使用已有 `/home/zhhy/projects/lerobot/.venv/bin/python`：Python 3.13、LeRobot 0.6.1、Torch 2.11.0+cu128、Transformers 5.5.4。网络依赖 websockets 15.0.1 与 msgpack 1.1.1 安装在 Kai0 `.cache/arx-lerobot-deps/`，没有覆盖原 JAX 环境或 LeRobot 环境的包。

启动时严格加载 813 个权重张量；缺失、不匹配或加载异常都会使启动失败。使用 checkpoint 保存的量化分位数归一化/反归一化处理器与本地 tokenizer，关闭训练用 gradient checkpointing，不修改权重文件。服务监听前完成两次预热。

输入保留 OpenPI 消息协议：`state[14]`、`images{top_head,hand_left,hand_right}` 与 `prompt`。服务将三个视图映射到 checkpoint 的 `head/left/right`，RGB 数值转换后由模型进行缩放。输出是反归一化的绝对关节角 `[50,14]`，左六关节、左夹爪、右六关节、右夹爪。

默认任务提示逐字沿用 `place_plate` 数据集中的 `pick up the plate  on the ruck.`。数据集还包含另一条提示 `pick up the pink cube  on the blue plate.`；切换任务时应明确传入对应 prompt。本次未改变采集站自己的任务配置。

metadata 明确包含 `mode=checkpoint`、checkpoint 路径与 step、严格加载张量数、默认 prompt、运行库版本。`test_only=true`、`motion_enabled=false`、`hardware_validated=false` 保持有效，现有客户端会拒绝以该服务开启真机动作。

## 启动与只读推理

在服务器终端手动启动，保持终端打开，Ctrl+C 停止；没有创建开机自启或异常自动重启服务：

```bash
cd /home/zhhy/projects/kai0
./local_scripts/start_arx_lerobot_server.sh
```

换 checkpoint 时显式指定完整 `pretrained_model` 目录：

```bash
./local_scripts/start_arx_lerobot_server.sh \
  --checkpoint /absolute/path/to/pretrained_model --prompt 'EXACT TRAINING TASK'
```

新客户端先做环境检查，然后通过现有采集站 GET 接口做真实观测推理：

```bash
cd /home/qijun/Documents/zhanghaoyi/kai0_arx_client
./start_client.sh doctor
.venv-inference/bin/python probe_checkpoint.py --count 5
```

该探针只读取 `http://127.0.0.1:8090` 的状态与相机预览，不抢占 CAN/相机，不操作录制，不执行模型动作。每次抓取一个真实快照再重复发送；三路 JPEG 与状态不严格同步，所以此探针用于模型与网络验证，不作为实时控制输入。

迁移后的启动脚本使用 `.venv-inference`。原复制来的 `.venv` 仍保留，但它是 Ubuntu 24 上的环境副本，不应在这台 Ubuntu 22 主机直接使用。新环境用已部署的 CPython 3.12.13 创建，并通过 `.pth` 复用 `/home/qijun/ARX5_beta/.venv-control` 的 NumPy、Pinocchio 等 SDK 依赖；独立安装网络、Pillow 和 RealSense 包。SDK 路径与末端负载 URDF 已改到 `/home/qijun/...`。旧配置及启动脚本备份在客户端 `logs/migration-20260911/`。

## 实测结果与边界

- 严格权重加载完成；两次 GPU 预热约 366 ms 和 121 ms，显存约 9.9 GB。
- 从 `.136` 取得三路真实 RGB `3×480×640` 与 14 维状态，向 `.117` 发送 5 次请求，均返回有限值 `[50,14]`，不是输入状态的简单回传。
- 网络往返为 707.9、512.5、539.9、668.6、701.8 ms，平均 626.1 ms，包含约 2.8 MB 图像的无线传输及推理。
- 客户端原有 13 项测试与 doctor 通过。
- 测试时双臂无故障且处于 protect，录制 idle，当前 fold_box_v1 数据集保持 810 条。
- 部分预测越过客户端保守限位：双臂 J2/J3 出现略小于零的值，夹爪最小约 -3.336/-3.373 rad，低于目前配置的 -3.14 rad。没有放宽限位。
- 上次 0.5° 微动跟踪仍未通过；本次没有复测运动。因此模型加载/协议联调成功不代表抓取放置任务成功，也不代表可以直接下发预测动作。

完整报告与原始观测/动作保存在新客户端 `logs/checkpoint-probe-1789137750840469442/`。本地报告副本随本文保存。
