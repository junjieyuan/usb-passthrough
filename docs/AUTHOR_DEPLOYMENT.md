# 作者部署实录（AUTHOR DEPLOYMENT）

> 本文档是**本项目作者的实际部署与开发环境记录**（原始硬件、VM、场景、环境约定），**完整保留**，供作者与他人参阅。
> 项目主体（README / DESIGN / DEVELOPMENT）已泛化为通用 USB 直通项目，不绑定以下任何内容；通用部署请看主文档。
> 内容来源：原 README.md / docs/DESIGN.md / docs/DEVELOPMENT.md（迁移时原文保留，仅整理归并）。

---

## 1. 部署总览与动机

**做什么**：把 USB 键盘/鼠标/手柄在 Linux 宿主机与 Windows 虚拟机之间自动热插拔。

- 设备插入（或从蓝牙切回 USB 模式）→ 若 Windows VM 运行中，自动直通（attach）给 VM
- 设备拔出（或切到蓝牙模式）→ 若 Windows VM 运行中，自动取消直通（detach）
- 周期性对账，自愈事件丢失、服务重启、宿主休眠唤醒等异常

**为什么做这个（背景）**：

1. 宿主是 Linux + 核显，Windows VM 独占 4080 显卡——VM 是主要使用场景，需要键鼠手柄随开随用；
2. 宿主键鼠有蓝牙模式——"VM 用 USB、宿主用蓝牙"的手动切换是逃逸通道；
3. **直接动机**：virt-manager 的持久 USB 直通在设备物理重枚举（如键盘切蓝牙再切回）后**不会自动重新分配**。libvirt 的 hostdev 条目在设备重枚举后变成失效条目（stale entry），不会自愈。整个守护进程就是为了补上这个缺口。

**部署/开发系统**：Fedora Silverblue（不可变系统，依赖 `python3-pyudev` 需用 `rpm-ostree` 安装）实测。

---

## 2. 设备清单与 .service 配置示例

### 2.1 三台设备

| 设备 | vid:pid | 说明 |
|---|---|---|
| Keychron K6 键盘 | `05ac:024f` | 在 `USB_PT_ALLOWED`，直通 |
| Razer Basilisk X 鼠标 | `1532:0083` | 在 `USB_PT_ALLOWED`，直通 |
| 8BitDo Ultimate 手柄 | `2dc8:3106`（游戏模式） | 在 `USB_PT_ALLOWED`，直通 |
| 8BitDo Ultimate（IDLE 空壳） | `2dc8:3109` | 在 `USB_PT_IDLE`，永不直通 |

### 2.2 systemd 单元对应示例（取消注释即用）

```ini
Environment=USB_PT_VM=windows
Environment=USB_PT_ALLOWED=05ac:024f,1532:0083,2dc8:3106
Environment=USB_PT_IDLE=2dc8:3109
```

---

## 3. 手柄"空闲状态"特殊情况（核心实战知识，完整保留）

**8BitDo Ultimate 分两种 PID 处理**：

- `2dc8:3106` = 游戏模式（手柄已连接，厂商自定义协议）→ 允许清单，直通；
- `2dc8:3109` = **IDLE**（接收器挂着、手柄没连，空壳 HID）→ `USB_PT_IDLE`，记录日志但**永不直通**。直通一个空壳设备毫无意义，还会在 Windows 里留下一堆死 HID。

**模式切换 = 完整重枚举，去抖不误 detach**：无线设备（8BitDo 手柄、无线键鼠）在模式切换、休眠唤醒、配对变化时会**物理重枚举**——表现为 remove + add 紧跟着来（实测 8BitDo 在 3106↔3109 间切换，间隔最短只有 1 秒）。去抖逻辑：

- remove 到达 → 等 1 秒；
- 期间/之后同一 `DEVPATH` 重新出现 → 判定为"重枚举/模式切换"，**跳过 detach**（避免无谓的取消再直通抖动）；
- 端口持续消失 → 确认是真拔 → 执行 detach。

**virt-manager 持久配置必须写 `2dc8:3106`（游戏模式）而不是 `3109`**：3109 是 IDLE 空壳（手柄未连接），激活时匹配不到、休眠时直通一堆没用的 HID——这是部署时发现的实际配置错误；改完重启一次 VM 清掉运行态的旧条目。

**IDLE 术语定义**：IDLE = 8BitDo 接收器未连接手柄的状态（`2dc8:3109`），永不直通。

---

## 4. Razer 鼠标特殊性

**蓝牙↔2.4G 开关切换不产生任何 udev 事件**（dongle 始终保持 USB 枚举）。切到蓝牙后，dongle **不会**自动从 VM 取消直通，而是留在 VM 里变成"空闲"设备，宿主通过蓝牙获得鼠标；切回 2.4G 时鼠标直接重连 VM 里的 dongle，无掉线。**VM 运行期间 dongle 归 VM 所有**：想在宿主用 2.4G 模式，只能停 VM 或手动 `virsh detach-device`（但 30s 对账会把它重新直通回去）——**宿主请用蓝牙**。

守护进程对鼠标的开关切换**什么都做不了也不需要做**——这是物理行为，不是守护进程的职责。

---

## 5. Keychron K6 特殊性

**切蓝牙需要拔 USB 线** → 真实 remove 事件 → 自动取消直通；插回 → add → 自动重新直通（若 VM 在运行）。**无 `ID_SERIAL_SHORT`**，只能按 vid:pid 匹配。

---

## 6. 与 virt-manager 配合

实际用法是"virt-manager 持久直通 + 宿主蓝牙逃生"，这正是本守护进程设计的使用方式：

- **保留 virt-manager 里的持久 hostdev 配置**（不要删）——VM 开机时 libvirt 自动直通，这就是"虚拟机开机就能用"；
- **守护进程负责恢复**：键盘切蓝牙断开 USB、再切回 USB 后，virt-manager 不会重新直通（libvirt 的 hostdev 条目在设备物理重枚举后变成失效条目，不会自动恢复）——守护进程在 `add` 事件后自动"先清掉失效条目再重新直通"；
- 守护进程与持久配置**不冲突**：attach/detach 都只加 `--live`（不动持久配置），动作前先查 VM 当前配置，幂等，不会重复直通；
- **检查 virt-manager 生成的 hostdev XML**，区分两种 `<address>`：
  - `<source>` **里面**的 `<address type='usb' bus='..' device='..'/>` 是**宿主侧**地址（对应 /dev/bus/usb 的 DEVNUM），重枚举后必然失效——**必须删掉**，只按 `<vendor>`/`<product>` 匹配；
  - `<hostdev>` 外层（`<source>` 的兄弟）的 `<address type='usb' bus='0' port='N'/>` 是**客户机侧**端口（设备出现在 Windows 模拟 USB 总线的哪个口），不会失效，可留可删；多个设备端口别重复，想省心就删掉让 QEMU 自动分配。

**K6 事件流走查（VM 运行中）**：切蓝牙 → 拔线 → remove → 去抖 → `detach-device --live`（K6 回宿主蓝牙）；切回 USB → 插线 → add → settle → 清失效条目 → `attach-device --live` → Windows 重新识别 K6。

---

## 7. 真实事件走查（状态机行为对照）

> 用测试内嵌的真实捕获事件（`test_replay.py` 的 `EVENTS`，在真实硬件上用 `udevadm monitor --property --udev --subsystem-match=usb/usb_device` 采集）讲解状态机在每个关键时刻的行为。时间戳为捕获日志的相对秒。

### 7.1 K6 键盘插拔（端口 5-2.1.4）

| 时间 | 事件 | 状态机动作 |
|---|---|---|
| 5782.24 | `add 05ac:024f` | present=True，调度 settle（5783.24 到期） |
| 5782.24 / 5782.31 | `change` / `bind` | 忽略（只认 add/remove） |
| 5788.58 | `unbind` → `remove` | present=False，调度去抖（5789.58 到期） |
| 5789.58 | 去抖到期 | 端口仍无设备 → detach（若 VM 运行）→ 键盘回宿主 |
| 5808.30 | `add 05ac:024f` | 重新调度 settle → attach → Windows 重新识别 |

### 7.2 8BitDo 手柄模式循环（端口 5-2.1.3，核心场景）

| 时间 | 事件 | 状态机动作 |
|---|---|---|
| 6055.20 | `remove 2dc8:3106` | present=False，调度去抖 |
| 6055.69 | `add 2dc8:3109`（IDLE） | `on_add` **清掉去抖定时器**；IDLE → ignored，不调度任何事 |
| 6085.41 | `remove 3109` | 非白名单 → 直接返回（无去抖） |
| 6085.83 | `add 2dc8:3106` | 调度 settle → attach（手柄回到 Windows） |
| 6097.44 | `remove 3106` | 去抖调度 |
| 6098.35 | `add 3109`（IDLE） | 清去抖；ignored |
| …… | 循环 | **每次 3106 出现都重新直通，IDLE 永不直通，全程零 detach 抖动** |

### 7.3 Razer 鼠标重枚举（端口 5-2.1.2）

| 时间 | 事件 | 状态机动作 |
|---|---|---|
| 7202.59 | `unbind` → `remove 1532:0083` | 去抖调度（这是**真实拔除/断电**——Razer 开关切换不产生事件，见第 4 节） |
| 7203.59 | 去抖到期 | 端口仍无设备 → detach（鼠标离开 VM） |
| 7229.99 | `add 1532:0083` | settle → attach（鼠标回到 VM） |

---

## 8. 真机验收记录

### 8.1 验收记录（本机实测通过）

- **locale 误判**：中文 locale 下 `virsh domstate windows` 返回 `运行` → `LC_ALL=C` 修复后，对账正确识别 VM 运行并开始 attach；
- **virsh `-` 标准输入**：报 `打开文件 '-' 失败` → 改临时文件后 detach 成功（运行态僵尸条目 `2dc8:3106`/`05ac:024f` 被正确清除）；
- **全链路行为**：K6 插回 → `add → attached`（对账路径，settle 定时器正确取消）；K6 切蓝牙 → `remove → 去抖 → detached`（宿主蓝牙可用）；8BitDo 开机 → `add 2dc8:3106 → attached`（事件路径）；8BitDo 关机 → `idle-mode 2dc8:3109 ignored`（不抖动）；Windows 内键鼠/手柄均正常；VM 关闭 → 键鼠即时归还宿主。

### 8.2 改完后的真机验收步骤（作者部署版）

1. 部署 + 重启（见 README「运维」节）；
2. VM 运行中逐项验证（对照第 11 节日志速查）：
   - K6 切蓝牙 → `remove → detached`；切回 → `add → attached`
   - 8BitDo 开机 → `attached 2dc8:3106`；关机 → `idle-mode ... ignored`（无抖动）
   - **守护进程晚启动恢复**（本项目的核心场景）：停掉服务 → 拔插一次 K6 → 启动服务 → 对账应见 `reconcile: hostdev ... resolved at ... but device now at ... — stale entry, re-attaching`
   - VM 关闭 → 设备即时归还宿主

---

## 9. 踩坑记录

| # | 现象 | 根因 | 修复 | 为什么这样修 |
|---|---|---|---|---|
| 1 | 守护进程一直报 "VM not running"，但 VM 明明在跑 | 中文 locale 下 `virsh domstate` 输出 `运行`，与 `"running"` 比较失败 | 所有 virsh 调用强制 `LC_ALL=C` | 状态名/错误信息稳定为英文，且新增状态日志（非 running 时打印实际状态）便于诊断 |
| 2 | detach 报 `打开文件 '-' 失败` | 该 virsh 版本不认 `-` 标准输入，把 `-` 当文件名 open | 改用临时文件传 XML | 兼容所有 virsh 版本，用完即删无残留 |
| 3 | （代码审查发现）对账先 attach 后，残留 settle 定时器再触发一次 detach+attach 抖动 | 对账与事件路径竞态 | 对账 attach 成功后取消该端口残留的 settle 定时器 | 避免无谓的 Windows 设备掉线重连 |
| 4 | （代码审查发现）对账读 VM 配置瞬时失败（dumpxml 出错返回 None）时未防护，`(vid,pid) in attached` 直接 TypeError | `vm_attached_devices()` 返回 None 未检查 | reconcile 增加 `attached is None → 安全中止，下周期重试` | 与"状态未知不动作"同原则：读不到配置宁可中止，绝不基于错误数据动作（其他调用点本就有防护，唯独 reconcile 漏了） |
| 5 | （代码审查发现）对账失配重挂后，残留 settle 定时器再次触发 detach+attach | 失配分支漏了 `clear_timer`（普通 attach 分支有） | 失配重挂后同步取消该端口 settle 定时器，并置 `rec["attached"]=False` 再按结果更新 | 与对账普通 attach 分支对称，避免无谓抖动；状态字段与实际一致 |

---

## 10. 设备行为速查表（从捕获日志总结）

| 设备 | 行为 |
|---|---|
| Keychron K6（`05ac:024f`） | 切蓝牙 = **拔 USB 线**（真实 remove）；切回 = 插线（add）。无 serial，只能按 vid:pid 匹配 |
| Razer Basilisk X（`1532:0083`） | 蓝牙↔2.4G 开关**不产生任何 uevent**（dongle 保持枚举）——切蓝牙后 dongle 留在 VM 里空闲，宿主走蓝牙 |
| 8BitDo Ultimate | 激活 = `3106`（游戏模式，厂商自定义）；关机/未连 = `3109`（IDLE 空壳 HID）；两者切换 = 完整重枚举、DEVNUM 持续增长（实测 045→052→…）；serial 恒为 `E417D8FD31F9` |

> 补充：DEVPATH（如 `/devices/.../usb5/5-2/5-2.1/5-2.1.3`）是端口拓扑路径，重枚举保持不变；DEVNUM 每次重枚举都会变（实测 8BitDo 从 045 一路涨到 052，Razer 从 004 变 053）。

---

## 11. 日志速查与注意事项

### 11.1 日志速查（作者部署版，含真实示例值）

| 日志行 | 含义 |
|---|---|
| `event source: pyudev (libudev, kernel uevent socket)` | 事件源正常 |
| `VM windows state is 'shut off' (not running)` | 诊断行：VM 状态不是 running（VM 关着时正常出现） |
| `add 05ac:024f ... (settle 1.0s)` | 设备插入，等 1s 后直通 |
| `attached 05ac:024f to windows` | 直通成功 |
| `remove ... (debounce 1.0s)` → `detached ...` | 真拔除，已取消直通 |
| `... re-enumerated, skipping detach` | 去抖判定为重枚举（模式切换/休眠唤醒），不取消 |
| `idle-mode device 2dc8:3109 ... ignored` | 8BitDo IDLE，永不直通 |
| `reconcile: hostdev 2dc8:3106 resolved at (5, 47) but device now at (5, 48) — stale entry, re-attaching` | 对账发现失效条目，先清再挂 |
| `attach ... failed:` | attach 失败（自动重试 3 次，仍失败交给下次对账） |
| `python3-pyudev is required` | 缺依赖，装 pyudev 后重启服务 |

### 11.2 注意事项 / 已知限制（作者部署版）

- **键盘/鼠标直通给 VM 后宿主机没有 USB 输入**——这是设计内行为，宿主用蓝牙模式操作。注意两台设备的切换行为不同：
  - **Razer 鼠标**：蓝牙↔2.4G 开关切换**不产生任何 udev 事件**（dongle 始终保持 USB 枚举）。切到蓝牙后，dongle **不会**自动从 VM 取消直通，而是留在 VM 里变成"空闲"设备，宿主通过蓝牙获得鼠标；切回 2.4G 时鼠标直接重连 VM 里的 dongle，无掉线。**VM 运行期间 dongle 归 VM 所有**：想在宿主用 2.4G 模式，只能停 VM 或手动 `virsh detach-device`（但 30s 对账会把它重新直通回去）——**宿主请用蓝牙**。
  - **Keychron K6**：切蓝牙需要拔 USB 线 → 真实 remove 事件 → 自动取消直通；插回 → add → 自动重新直通（若 VM 在运行）。
- **同一 VID:PID 的多台设备无法区分**（如两个同型号手柄），直通会匹配第一台。需要精确到端口时，把 `hostdev_xml()` 生成的 XML 加上 `<address type='usb' bus='..' device='..'/>`（但 DEVNUM 变化会使其失效，需配合对账刷新）。
- **Windows 侧**：设备每次物理重枚举（8BitDo 模式切换、休眠唤醒、拔插）都会掉线重连一次，这是 USB 物理行为，无法避免；本守护进程保证不放大抖动、不误取消。Razer 鼠标切开关不重枚举，Windows 里无掉线。
- **对账的覆盖边界**：30s 对账清理"已直通但物理已不存在"的僵尸条目；并通过**地址比对**恢复"重枚举后残留失效条目"（VM 条目记录的 bus/device ≠ 设备当前值 → 判定失效 → 先清再挂）。所以即使守护进程晚启动、或停机期间漏了 add 事件，设备在位也能被对账重新直通。仅剩的盲区：条目地址缺失（无 `<address>`）时保守跳过，靠下次重插/VM 重启收敛。
- **可选增强**：想"VM 一启动就立即直通"而不是等 ≤30s 的对账，可加一个 libvirt qemu hook（`/etc/libvirt/hooks/qemu`）在 `started` 时执行 `--reconcile-once`。

---

## 12. 开发环境约定（提交与推送，作者环境专用）

- 工作区规则：**所有 SSH 传输必须用 `~/.ssh/id_ed25519_agent` 这把密钥**（`-o IdentitiesOnly=yes`、`-F /dev/null`），不尝试其他密钥；密钥不可读时停止并报告，不要静默回退；
- **推送前必做**：`env -u SSH_AUTH_SOCK` 绕开 ssh-agent——本环境 `SSH_AUTH_SOCK` 指向 Bitwarden 桌面版的 agent socket，ssh 连它会**挂起**（实测踩坑）；
- 推送示例：

```bash
env -u SSH_AUTH_SOCK \
  GIT_SSH_COMMAND="ssh -i ~/.ssh/id_ed25519_agent -o IdentitiesOnly=yes -F /dev/null -o StrictHostKeyChecking=accept-new" \
  git push origin main
```

---

> 主文档：通用说明见 [README.md](../README.md)；设计与决策见 [DESIGN.md](./DESIGN.md)；开发指南见 [DEVELOPMENT.md](./DEVELOPMENT.md)。
