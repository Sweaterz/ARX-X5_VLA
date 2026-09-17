# GUI / client 退出与保持

2026-09-16。仅修改 zhanghaoyi/kai0_arx_client，不修改 ARX5_beta。

## 当前行为

| 触发 | GUI | client.py run 真机模式 |
|---|---|---|
| 正常完成 / 暂停 | 固定当前位置，后台保持 | 固定当前位置，等待 EXIT |
| Ctrl+C / SIGTERM / SIGHUP | 请求退出，取消任务并保持，HTTP 继续服务 | 取消任务并保持，保留本机控制 socket |
| 重复退出信号 | 仍保持，不等于确认释放 | 仍保持，不等于确认释放 |
| 模型断连、无效预测、相机错误、动作过期 | 检查机械臂反馈，健康则保持；不再接收迟到动作 | 相同 |
| SDK fault、非有限反馈、看门狗已触发、SDK 命令失败 | 不恢复保持，保护并释放设备，锁定故障 | 保护清理退出 |
| 软件紧急停止 | 优先保护，不能转回保持 | 本机控制 stop 优先保护 |
| 明确断开 / 确认释放 | 操作者确认支撑后断开；等待退出状态下随后关闭 HTTP | EXIT 或 release --supported 释放 SDK |

保护不是机械锁定。正常归位不代表电机卸力后一定不下落；释放前仍需确认双臂有稳定支撑。不会在故障、相机断线或关闭页面时自动归位。

## GUI 使用

```bash
cd ~/Documents/zhanghaoyi/kai0_arx_client
./start_gui.sh --port 8092
```

本机浏览器打开 http://127.0.0.1:8092。后台收到退出信号后显示等待退出；先“接管后台保持”，按需“双臂归位”，放稳/支撑后确认“断开机械臂”。重复 Ctrl+C 不跳过确认。终止后台后 HTTP 关闭是最后一步。

独立于网页的停止入口仍可用（会保护，不是保持）：

```bash
.venv-inference/bin/python gui_stop.py --port 8092
```

## 命令行 client 终端丢失后的入口

client.py run 真机模式会创建权限 0600 的当前用户 Unix socket。终端 EOF 后保持进程，不能通过关闭终端释放双臂。另一个终端中：

```bash
cd ~/Documents/zhanghaoyi/kai0_arx_client
.venv-inference/bin/python client_control.py status
.venv-inference/bin/python client_control.py pause
```

放稳并支撑双臂后才执行：

```bash
.venv-inference/bin/python client_control.py release --supported
```

紧急时请求 SDK 保护：

```bash
.venv-inference/bin/python client_control.py stop
```

这个 socket 只用于命令行 client，不用于 GUI 或独立测试脚本。GUI 使用其 HTTP 控制入口。client_hold_demo.py 的显式 EXIT 归位仍只在 --official-demo 中启用。

## I/O 与反馈维护

相机读取/初始化与模型连接/推理由 daemon 工作线程执行；控制循环每约 20ms 检查暂停、退出、急停、SDK fault 和有限反馈并更新看门狗。SDK 电机控制线程仍独立运行。GUI 空闲保持约 40ms 检查，CLI 保持约 50ms 检查。没有放宽原来的 2 秒看门狗。

推理结果仍受 1.5 秒观测有效期检查。取消或超时后丢弃迟到结果，不重放动作；同一 I/O 通道未结束时不叠加新任务。相机错误暂停预览，需显式断开/重连；不会让预览错误直接释放机械臂。资源收尾不占用维持保持的控制循环。

## 保护接口的含义与边界

本机 SDK 模式表实际为 IDLE / PROTECT / GRAVITY / JOINT / EE_POSE。底层持续进行安全检查，与上层显式 protect_mode() 切换模式是不同概念。本次没有套用其他实现中的 ±3.14、100 倍扭矩、40ms 等参数，也没有更改增益、速度、夹爪方向或 SDK 保护配置。

同进程软件不能保证 SIGKILL、进程崩溃、主机/电机断电、CAN 故障、SDK 调用永久阻塞或进程监督器最终强杀后的支撑。反馈有效性也依赖 SDK 的故障/通信状态判定；此改动不是机械制动器，不构成不会掉臂的保证。

## 验证

71 项离线测试通过。包含延迟 I/O 超过测试看门狗期限时仍维护反馈、模型/相机故障保持、SDK 故障不强制保持、终端 EOF、本机释放确认、CLI SIGTERM/SIGHUP 丢弃迟到动作。纯演示子进程实际接收两次 SIGTERM，验证继续保持并提供 HTTP，接管/归位/明确断开后正常退出。前端构建通过。没有执行真机运动、掉臂或断电测试。

备份位于 logs/before-exit-lifecycle/。正在运行的旧进程不会热更新；请按旧版流程先放稳并明确断开，再关闭旧进程，随后启动新版。
