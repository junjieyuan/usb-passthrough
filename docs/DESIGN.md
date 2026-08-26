# USB 直通守护进程 — 设计文档与决策记录

> 本文档是代码的"why 注释"载体：每一节先描述"做了什么"，再解释"为什么这么做"。
> 代码本身保持精简，非显然的设计决策全部沉淀在这里。
> 适用版本：配合 `usb-passthrough-daemon.py` 当前实现阅读。

---

## 1. 项目目标与背景

**做什么**：把 USB 键盘/鼠标/手柄在 Linux 宿主机与 Windows 虚拟机之间自动热插拔。

- 设备插入（或从蓝牙切回 USB 模式）→ 若 Windows VM 运行中，自动直通（attach）给 VM
- 设备拔出（或切到蓝牙模式）→ 若 Windows VM 运行中，自动取消直通（detach）
- 周期性对账，自愈事件丢失、服务重启、宿主休眠唤醒等异常

**为什么做这个（背景）**：

1. 宿主是 Linux + 核显，Windows VM 独占 4080 显卡——VM 是主要使用场景，需要键鼠手柄随开随用；
2. 宿主键鼠有蓝牙模式——"VM 用 USB、宿主用蓝牙"的手动切换是逃逸通道；
3. **直接动机**：virt-manager 的持久 USB 直通在设备物理重枚举（如键盘切蓝牙再切回）后**不会自动重新分配**。libvirt 的 hostdev 条目在设备重枚举后变成失效条目（stale entry），不会自愈。整个守护进程就是为了补上这个缺口。

---

## 2. 架构总览

```
                        ┌──────────────────────────────────────────┐
  内核 uevent socket    │              usb-passthrough 守护进程       │
  (libudev / pyudev) ──▶│  pyudev monitor ─▶ handle_event() 状态机    │
                        │       │                │                   │
                        │       ▼                ▼                   │
                        │  定时器(settle/去抖)  对账 reconcile(30s)    │
                        │       │                │                   │
                        │       ▼                ▼                   │
                        │   attach_device() / detach_device()         │
                        └───────────────┬──────────────────────────┘
                                        ▼
                              virsh attach/detach-device
                                        │
                                        ▼
                              libvirt (qemu:///system) → QEMU → Windows
```

**组件职责**：

| 组件 | 职责 |
|---|---|
| pyudev 事件源 | 监听内核 USB 设备事件，产出标准事件 dict |
| `Daemon` 状态机 | 事件 → 决策（是否/何时 attach/detach），持有设备状态表 + 定时器 |
| 定时器 | settle（attach 前等待）、去抖（remove 后确认） |
| 对账 reconcile | 周期性地把"物理设备清单"与"VM 配置清单"对齐，自愈一切漏网之鱼 |
| libvirt 动作层 | 通过 `virsh` 执行 attach/detach，全部幂等、容错 |

**为什么是"事件驱动 + 对账兜底"双保险（最重要的设计原则）**：

- 纯事件驱动会漏事件（守护进程崩溃重启、宿主休眠唤醒时的事件风暴、netlink 缓冲区溢出）；
- 纯轮询又太迟钝（attach 延迟不可接受）；
- 事件驱动保证响应速度（秒级），对账保证最终一致性（≤30s 收敛）。二者互补，缺一不可。

---

## 3. 核心设计原则

1. **状态未知时绝不动作**：`virsh domstate` 失败（libvirt 未运行）返回 `None`，调用方必须把 `None` 当作"不知道"而不是"没运行"——宁可跳过动作等下次对账，也绝不错 detach 设备。
2. **一切动作幂等**：attach 前先查 VM 当前配置，已挂就跳过（或先清失效条目再挂）；detach 容忍"设备已不存在"。
3. **对账是最终仲裁者**：任何事件路径的失败/遗漏，最终由 reconcile 收敛。
4. **守护进程只做运行期恢复，不碰持久配置**：开机直通归 virt-manager 持久配置，运行时恢复归守护进程（见第 9 节）。

---

## 4. 事件源：pyudev

**做什么**：`pyudev.Monitor.from_netlink()` 创建 libudev 内核监控，`filter_by(subsystem="usb", device_type="usb_device")` 只接收 USB 设备层事件，`select` + `poll(timeout=0)` 排空事件。

**为什么是 pyudev 而不是其他方案**：

| 方案 | 放弃的原因 |
|---|---|
| `udevadm monitor` 子进程 | 多一层管道和文本解析；子进程崩溃有事件丢失窗口；曾经实现过，后来删除 |
| udev 规则 `RUN=` | 适合无状态的一次性动作；本需求有去抖/settle/对账等**状态逻辑**，放规则里很别扭，还需 systemd timer + 状态文件 |
| 裸 `AF_NETLINK` socket | 那是 udev 内部做的事，要自己解析 sysfs、与 udev 规则处理竞态，纯属重复造轮子 |
| pyudev（采用） | 直接调 libudev，无子进程、无文本解析、源头过滤；Fedora 一个包搞定 |

**为什么用内核 socket（`from_netlink`）就够**：守护进程只用内核 uevent 自带的属性——`PRODUCT`/`TYPE`/`DEVTYPE`/`DEVPATH`，这些在 add 和 remove 事件里都有。不需要 udev 规则后处理才有的 `ID_*` 属性。

**为什么没有回退路径**：宁可启动即报错（systemd 日志明确提示缺 pyudev），也不静默降级到次优路径——回退代码是长期维护负担，且掩盖环境问题。

**为什么模块顶部的 `import pyudev` 是 try/except**：仅为了让模块在没装 pyudev 的机器上仍可被导入（回放测试需要）；守护进程本体在 `run()` 里检查，缺了就拒绝启动。这不是回退，是"可测试性"。

**踩坑（已修）**：pyudev 0.24.1 起 `Device.get()` 已弃用、1.0 会移除——必须用 `dev.properties.get()`（实测 `-W error::DeprecationWarning` 验证）。

---

## 5. 设备识别

**身份 = `PRODUCT`（vid/pid/rev）+ `DEVPATH`（端口路径）**。

**为什么用 `PRODUCT` 而不是 `ID_VENDOR_ID`/`ID_MODEL_ID`**：`PRODUCT` 是内核 uevent 属性，**remove 事件也携带**（日志中 remove 事件同样有 `PRODUCT=1532/83/200`）；而 `ID_*` 是 udev 规则后处理属性，remove 时可能缺失。识别设备必须靠"add 和 remove 都有"的字段。

**为什么用 `DEVPATH`（如 `/devices/.../usb5/5-2/5-2.1/5-2.1.3`）而不是 `DEVNUM`/`/dev/bus/usb` 路径**：

- `DEVNUM` 每次重枚举都会变——实测 8BitDo 在日志里从 045 一路涨到 052，Razer 从 004 变 053；
- `DEVPATH` 是端口拓扑路径，重枚举保持不变；
- 用 `DEVNUM` 做匹配或 hostdev 地址，一次重枚举就全部失效。

**为什么 8BitDo 分两种 PID 处理**：

- `2dc8:3106` = 游戏模式（手柄已连接，厂商自定义协议）→ 允许清单，直通；
- `2dc8:3109` = **IDLE**（接收器挂着、手柄没连，空壳 HID）→ `KNOWN_IDLE`，记录日志但**永不直通**。直通一个空壳设备毫无意义，还会在 Windows 里留下一堆死 HID。

**为什么 hub 过滤**：监控会收到 hub（USB class 09，含 root hub）的事件，用 `TYPE` 首字段判类过滤，避免无谓记录。

**为什么允许清单按 vid:pid 且不强制 serial**：Keychron K6（`05ac:024f`）**没有** `ID_SERIAL_SHORT`，只能按 vid:pid 匹配；8BitDo 有稳定 serial（`E417D8FD31F9`，且 IDLE/Active 两种模式相同）。所以 serial 只能作为可选的精确化手段，不能是强制条件。

---

## 6. 事件处理状态机

### 6.1 只认 `add` / `remove`

一次物理插入会发一串事件：`add` → `change` → `bind`；拔出发 `unbind` → `remove`。状态机**只处理 `add` 和 `remove`**，其余动作（change/bind/unbind）一律忽略——否则一次插拔会被重复处理 3 次。

### 6.2 settle 延迟（attach 前等 1 秒）

**为什么**：`add` 事件发生在设备刚枚举时，接口驱动还没绑定完。实测证据：日志中 add 的 SEQNUM=7939、change=7940，而 bind 是 7965——中间 25 个序号全是接口层（`:1.0`/`:1.1`）的事件。此刻立刻 attach 可能失败或让 Windows 看到接口不完整的设备。等 1 秒让设备稳定。

### 6.3 去抖（remove 后等 1 秒再决定 detach）

**为什么**：无线设备（8BitDo 手柄、无线键鼠）在模式切换、休眠唤醒、配对变化时会**物理重枚举**——表现为 remove + add 紧跟着来（日志里 8BitDo 在 3106↔3109 间切换，间隔最短只有 1 秒）。去抖逻辑：

- remove 到达 → 等 1 秒；
- 期间/之后同一 `DEVPATH` 重新出现 → 判定为"重枚举/模式切换"，**跳过 detach**（避免无谓的取消再直通抖动）；
- 端口持续消失 → 确认是真拔 → 执行 detach。

**为什么 detach 对"设备已消失"容错**：物理 remove 时 QEMU 早已自己把设备摘了（usbfs 句柄失效），`virsh detach-device` 可能失败或空操作——这是正常情况，记日志继续，不能崩、不能重试卡死。

**注意（Razer 鼠标的特殊性）**：Razer 的蓝牙↔2.4G 开关切换**不产生任何 udev 事件**（dongle 始终保持枚举）。所以鼠标切蓝牙时，dongle 不会自动从 VM 取消直通，而是留在 VM 里变"空闲"，宿主通过蓝牙获得鼠标；切回 2.4G 直接重连 VM 里的 dongle。守护进程对鼠标的开关切换**什么都做不了也不需要做**——这是物理行为，不是守护进程的职责。

### 6.4 stale 条目：先清再挂（核心修复）

**场景**：设备直通中发生物理重枚举（如 K6 切蓝牙再切回），VM 的 live XML 里残留失效 hostdev 条目。libvirt 不会自动恢复——**这正是 virt-manager 不重新分配设备的根因**。

**处理**：`add` 事件 → settle → 发现 `(vid,pid)` 已在 VM 配置里 → 判断这是**失效条目**（设备重枚举过）→ 先 `detach`（清掉失效条目）→ 再 `attach`（重新直通，Windows 重新识别）。

**为什么"在配置里"不等于"可用"**：libvirt 的 hostdev 条目在设备物理消失后仍然留在 live XML 里，但 QEMU 侧的设备已经死了。只看 XML 会误判"已直通"而跳过——所以对 add 事件必须走"先清再挂"。

### 6.5 幂等

attach 前查 `dumpxml`（已在配置 → 清失效 → 重挂）；detach 前也查（不在配置 → 跳过）。保证重复事件、对账与事件并发时都不会重复动作。

### 6.6 定时器实现

字符串 key 的 `(due, key, func)` 列表，`set_timer` 同 key 自动去重、`fire_timers` 到期执行且异常隔离。就两种定时器（settle、去抖），用 `heapq` 属于过度设计。

---

## 7. 对账（reconcile）

**做什么**：每 30 秒 + 启动时，把 `scan_physical_devices()`（当前物理允许设备）与 `vm_attached_devices()`（VM live XML 里的 hostdev）求差集：

- 物理在、VM 没有 → attach；
- VM 有、物理不在（且允许清单内）→ detach（清僵尸条目）；
- VM 未运行 → 全部标记未直通、不做任何事（libvirt 在 VM 停止时已自动释放 hostdev）。

**为什么必须有对账**（事件驱动必然漏事件的三类场景）：

1. **设备在 VM 启动前就插着**——udev 不会发 add，事件路径永远不会直通它；
2. **守护进程自身重启/崩溃/升级**——期间事件全丢；
3. **宿主休眠唤醒**——唤醒瞬间所有 USB 设备批量重枚举，事件风暴且时序混乱。

**为什么对账也是最终仲裁者**：任何事件路径的失败（attach 失败、detach 容错跳过、事件丢失），最终 30 秒内会被对账收敛到正确状态。

**为什么"VM 状态未知时对账直接中止"**：`domstate` 失败 = 无法确认 VM 状态，此时 attach/detach 都可能产生错误副作用，宁可中止等下次。

**内存卫生**：对账会删除"非白名单且已不在总线上"的设备记录（避免长期运行后记录无限增长）；白名单记录保留，因为可能有未触发的去抖定时器要查它。

---

## 8. libvirt 集成

### 8.1 hostdev XML 只按 vendor/product 匹配

```xml
<hostdev mode='subsystem' type='usb' managed='yes'>
  <source>
    <vendor id='0x05ac'/>
    <product id='0x024f'/>
  </source>
</hostdev>
```

**为什么不加 `<address>`**：宿主侧地址（`<source><address bus=.. device=../>`）绑定 DEVNUM，重枚举必失效；且 libvirt 按 XML 记账，vendor/product 匹配时**物理设备已消失也能正常 detach**（匹配的是配置条目而不是设备）。

### 8.2 临时文件传 XML（踩坑修复）

**为什么不用 `-` 标准输入**：实测该 virsh 版本**不认 `-`**，会把 `-` 当文件名去 open（报 `打开文件 '-' 失败: 没有那个文件或目录`）。所以 attach/detach 都把 XML 写进 `/tmp` 临时文件传给 virsh，用完即删。

### 8.3 `managed='yes'`

libvirt 自动处理"从宿主驱动解绑 / 归还时回绑"，直通动作不会把宿主留在半绑定状态。

### 8.4 只用 `--live`（非持久）

**为什么**：持久配置属于 virt-manager（开机直通靠它）；守护进程只动运行态，VM 重启后持久配置照常生效，两者互不干扰、不会打架。

### 8.5 `LC_ALL=C`（踩坑修复）

**为什么**：virsh 会本地化输出——实测中文 locale 下 `virsh domstate windows` 返回 `运行` 而不是 `running`，字符串比较失败导致误判"VM not running"。所有 `_sh()` 调用统一强制 `LC_ALL=C`，状态名和错误信息都稳定为英文，也方便对日志。

### 8.6 状态语义

`vm_running()` 返回三值：`True`/`False`/`None`（无法确定）。`None` 绝不是 `False`——第 3 节原则 1。

---

## 9. 与 virt-manager 持久配置的分工

| 时机 | 谁负责 | 机制 |
|---|---|---|
| VM 开机 | virt-manager 持久配置 | libvirt 按 vendor/product 自动直通（设备在 USB 模式时） |
| 设备重枚举/重插（运行中） | 守护进程 | add → settle → 清失效条目 → 重新直通 |
| 设备切蓝牙/拔出（运行中） | 守护进程 | remove → 去抖 → detach |
| VM 关闭 | libvirt 自动 | 释放 hostdev、设备归还宿主 |
| 一切漏网之鱼 | 守护进程对账 | ≤30s 收敛 |

**为什么保留持久配置而不是让守护进程全包**：开机直通要"即插即用、零延迟"，libvirt 原生支持且最可靠；守护进程补的正是 libvirt 不会做的"运行期恢复"。两套机制互相补位，职责清晰。

**为什么 virt-manager 配置里手柄必须写 `2dc8:3106` 而不是 `3109`**：3109 是 IDLE 空壳，激活时匹配不到、休眠时直通一堆没用的 HID——这是部署时发现的实际配置错误。

---

## 10. 真机踩坑记录

| # | 现象 | 根因 | 修复 | 为什么这样修 |
|---|---|---|---|---|
| 1 | 守护进程一直报 "VM not running"，但 VM 明明在跑 | 中文 locale 下 `virsh domstate` 输出 `运行`，与 `"running"` 比较失败 | 所有 virsh 调用强制 `LC_ALL=C` | 状态名/错误信息稳定为英文，且新增状态日志（非 running 时打印实际状态）便于诊断 |
| 2 | detach 报 `打开文件 '-' 失败` | 该 virsh 版本不认 `-` 标准输入，把 `-` 当文件名 open | 改用临时文件传 XML | 兼容所有 virsh 版本，用完即删无残留 |
| 3 | （代码审查发现）对账先 attach 后，残留 settle 定时器再触发一次 detach+attach 抖动 | 对账与事件路径竞态 | 对账 attach 成功后取消该端口残留的 settle 定时器 | 避免无谓的 Windows 设备掉线重连 |

---

## 11. 验证方法论

**为什么需要自动化验证**：守护进程的行为（去抖、settle、重枚举判断）用真机验证成本高且不可重复，必须用真实数据锁定行为。

1. **回放测试 `test_replay.py`**：把真实捕获的 udev 事件（`sample-events.log`，含 Keychron/Razer/8BitDo 全部模式切换）按时间戳回放进状态机，mock 掉 virsh 和 sysfs，断言 7 项行为（attach/detach/不抖动/IDLE 忽略等）。零副作用、可重复、CI 友好。
2. **集成冒烟**：只读扫描真实 sysfs（容器共享宿主 /sys），验证 `scan_physical_devices()`/`devpath_present()`/事件解析与真实设备数据吻合。
3. **真机验收**：VM 运行中逐项验证 K6 蓝牙切换、8BitDo 开关机、VM 关机释放。

**为什么 mock 而不是真连**：回放测试要锁定"状态机逻辑"，不依赖 libvirt 环境；真机行为用上述第 3 步单独验证。

---

## 12. 已知限制与权衡

1. **同 VID:PID 的多台设备无法区分**（如两个同型号手柄），直通匹配第一台。需要精确到端口时可扩展 hostdev XML 加地址，但 DEVNUM 变化会使其失效，需配合对账刷新——权衡后保持 vendor/product。
2. **对账的覆盖边界**：30s 对账清理"已直通但物理已不存在"的僵尸条目；"重枚举后残留失效条目"由 add 事件路径恢复（正常插拔/切换都会产生事件）。若守护进程恰好在重插瞬间重启而丢事件，下次重插或 VM 重启即自愈。
3. **30s 对账延迟**：VM 刚启动、设备已插着的场景最迟 30s 内补直通。可加 libvirt qemu hook（`started` 时执行 `--reconcile-once`）做到即时，作为可选增强。
4. **Windows 侧重枚举掉线**：设备每次物理重枚举（休眠唤醒、模式切换）Windows 都会掉线重连一次，这是 USB 物理行为，无法避免；守护进程保证不放大抖动、不误取消。
5. **Razer 鼠标开关切换不可见**（见 6.3 末尾）：VM 运行期间 dongle 归 VM，宿主用鼠标请走蓝牙。

---

## 13. 配置参数参考（含默认值由来）

| 环境变量 | 默认 | 为什么是这个默认 |
|---|---|---|
| `USB_PT_VM` | `windows` | 目标虚拟机名，启动日志会打印确认 |
| `USB_PT_ALLOWED` | `05ac:024f,1532:0083,2dc8:3106` | Keychron K6、Razer Basilisk X、8BitDo 游戏模式 |
| `USB_PT_IDLE` | `2dc8:3109` | 8BitDo IDLE 空壳，永不直通，仅记日志 |
| `USB_PT_SETTLE` | `1.0` | add 后等 1 秒让接口枚举完（见 6.2） |
| `USB_PT_DEBOUNCE` | `1.0` | remove 后等 1 秒确认不是重枚举（见 6.3） |
| `USB_PT_RECONCILE` | `30` | 对账周期：30 秒内收敛所有异常，兼顾响应与开销 |
| `USB_PT_ATTACH_RETRIES` | `3` | attach 失败重试 3 次（VM 刚启动 QEMU 未就绪时常见） |
| `USB_PT_ATTACH_RETRY_GAP` | `1.5` | 重试间隔 1.5 秒 |
