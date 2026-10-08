# Checkpoint 文件夹与持续运行

- 控制台 Checkpoint 下新增“选择 Checkpoint 文件夹”。选择服务器电脑上的 `pretrained_model` 目录；此操作不上传本地浏览器电脑的文件。
- 模型必须为 PI05、14 维绝对关节动作、head/left/right 图像。读取配置检查文件完整性，实际启动仍须严格加载权重。文件完整性检查不代表模型适合当前任务。
- Server 停止且没有活动任务或试验时允许更换；不要求机械臂先断开。模型选择本身不向机械臂发送动作。选择保存到 `logs/checkpoint-selection.json`，下次启动加载；已有受管 Server 正在运行时，GUI 重启会优先识别该进程记录的 checkpoint。
- VLA/DAgger 默认 steps=0，持续执行到人工接管、暂停、结束或故障。正整数指定步数，不再限制 300；负数、小数和布尔值拒绝。无运动测试始终只执行一次推理。
- 完整试验录制仍保留策略和人工段；每个动作块写入请求日志，避免持续模式把全部动作请求堆积在内存中。
- 暂停、页面失联、旧预测失效、反馈故障与急停逻辑不变。
- “每块执行步数”默认为 0，使用 Server 的 `recommended_actions_per_chunk`；明确填写正整数时覆盖，但不能超过模型动作块长度。新版 Server 对已核实的 fold 数据集使用最多 50 帧，对 place_plate 和其他模型使用最多 8 帧。旧 Server 没有该字段时回退到客户端配置，因此 fold 应配套升级 Server。

当前运行中的 GUI 不会热更新。更新源码并完成前端构建后，先结束保存/丢弃 DAgger，放稳双臂，断开机械臂及相机，再正常重启后台：

```bash
cd ~/Documents/zhanghaoyi/kai0_arx_client
./start_gui.sh --port 8092
```

如旧后台仍在运行，先在设备全部断开后向对应后台发送 SIGTERM，等待退出再启动。一次性部署更新器未包含在仓库中。模型 Server 单独管理，重启 GUI 不会自动重启模型。成功后刷新浏览器。

## Checkpoint 与 Prompt 联动

切换模型后，控制台与 DAgger 共用的任务描述同步更新。同一 checkpoint 的轮询不会覆盖手动修改。活动试验继续使用其开始时锁定的描述。

来源优先级：模型目录的 `inference_prompt.json`（`{"prompt":"任务描述"}` 或 JSON 字符串）→ `train_config.json` 的明确 prompt/task 字段 → 已核实的部署任务映射。`local/fold_box_v1_pi05` 对应 `fold the paper boxes.`；默认 place_plate 对应 `pick up the plate  on the ruck.`。未知模型清空描述，须手动填写。此更新仅读取模型目录，不向模型目录写入文件。

新启动的 Server 使用 GUI 当前 prompt 作为默认任务；GUI 推理请求本身也明确携带 prompt。旧 Server 无需因页面描述变化而重启。

## 人工接管等待

接管会先作废在途推理和旧动作块。保留 SDK 最后目标 1 秒后，读取实测关节与夹爪位置建立 JOINT 保持，再检测至少 0.2 秒连续稳定反馈；最长检测 0.75 秒。稳定后依次进入重力补偿，两臂切换成功才显示可拖动。暂停、页面失联、故障或急停会取消过渡，不自动恢复 VLA。

这段流程不同于普通暂停：普通暂停捕获当前位置并持续保持，不会自动切换重力补偿。
