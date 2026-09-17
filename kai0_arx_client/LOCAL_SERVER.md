# 本机 Kai0 Server

文件均位于 `/home/qijun/Documents/zhanghaoyi`。模型服务使用现有 `/home/qijun/ARX5_beta/.venv/bin/python`，不修改官方 SDK 或 fold 项目。缺少的 msgpack 1.1.2 单独安装在 server/vendor_deps，启动脚本通过 PYTHONPATH 加载。

## 启动

```bash
cd ~/Documents/zhanghaoyi/kai0_arx_client
./start_gui.sh
```

浏览器打开 http://127.0.0.1:8092 。先点击“启动本机 Server”，状态显示真实 checkpoint 已加载并完成预热后，再连接机械臂和相机。加载阶段要求机械臂断开；按钮本身不初始化机械臂或相机。

本机模型地址 `ws://127.0.0.1:8001`。8000 已被 CAN 管理页面占用，保留原服务。模型服务仅监听回环地址。

独立启动：

```bash
cd ~/Documents/zhanghaoyi/kai0_arx_server
bash local_scripts/start_arx_lerobot_server.sh
```

首次部署先运行 `local_scripts/verify_checkpoint.py`，逐文件核对 `SOURCE_MANIFEST.json` 中 SHA-256；全部通过才写入 `CHECKPOINT_VERIFIED.json`。启动脚本拒绝加载未完成校验的同步目录。此标记不是每次启动重新计算 9.4 GB 权重的校验；更换 checkpoint 时应重新验证。

GUI 关闭不终止独立模型进程；重新打开后点击启动按钮会识别已有健康服务。启动脚本有进程锁，避免重复占用 GPU。错误和加载日志位置显示在服务状态中，文件位于 client/logs/local-server-*.log。

## 验证范围

```bash
cd ~/Documents/zhanghaoyi/kai0_arx_client
.venv-inference/bin/python validate_local_server.py
```

脚本通过真实 websocket 协议执行三次 checkpoint 推理，输入合成图像与模拟关节状态，不初始化 SDK、相机或下发运动。输出保存到 `logs/local-server-validation.json`。模型加载和推理成功不代表真机动作已经验证。

## 本次部署结果

9 个 checkpoint 文件 SHA-256 均与 192.168.2.117 源文件一致。RTX 4090 24 GB 上严格加载 813 项权重，两次预热约 266/126 ms；三次合成观测真实推理往返约 138/126/122 ms，均返回有限数值的 (50,14) 数组。没有初始化机械臂或相机。GUI API 已验证识别现有 READY 服务且保持设备断开。

## Server 状态与关闭

GUI 后台每两秒自动检查一次本机服务：绿点表示健康检查成功、真实权重已加载；黄点表示检查/加载/关闭；红点表示未启动、服务无响应或故障。显示 checkpoint 名称 `place_plate / 015000`。

“关闭 Server”只停止有管理记录且 PID、进程启动时间、用户和脚本路径均匹配的本机服务。外部服务仅监测，不按端口强杀。服务身份记录位于 server/logs/managed-server.json，GUI 重启后可以重新识别其启动的服务。

VLA 运行中点击关闭，先请求暂停并保持，主控制循环完成保持并再次检查机械臂反馈后才停止推理进程。保持失败/控制故障会取消关闭请求，按原有机械臂保护路径处理。停止 Server 不退出 GUI，也不释放机械臂。加载过程中可取消加载；重新启动模型仍要求机械臂断开。
