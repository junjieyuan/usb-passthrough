# USB 直通守护进程 — 设计文档与决策记录

> 本文档是代码的"why 注释"载体：每一节先描述"做了什么"，再解释"为什么这么做"。
> 代码本身保持精简，非显然的设计决策全部沉淀在这里。
> 适用版本：配合 `usb-passthrough-daemon.py` 当前实现阅读。
> 本项目作者的实际部署与设备行为案例见 [AUTHOR_DEPLOYMENT.md](./AUTHOR_DEPLOYMENT.md)。

---

## 1. 项目目标与背景

**做什么**：把 USB 设备（如键鼠/手柄）在 Linux 宿主机与虚拟机（VM）之间自动热插拔。

- 设备插入（或从无线模式切回 USB 模式）→ 若 VM 运行中，自动直通（attach）给 VM
- 设备拔出（或切到无线模式）→ 若 VM 运行中，自动取消直通（detach）
- 周期性对账，自愈事件丢失、服务重启、宿主休眠唤醒等异常

**为什么做这个（背景）**：

1. 主要计算场景在 VM 内（如显卡直通、专用输入设备随开随用），需要 USB 输入设备在宿主与 VM 之间无缝切换；
2. 部分设备支持无线/蓝牙模式，可作为"宿主免插拔"的逃逸通道；
3. **直接动机**：virt-manager / libvirt 的持久 USB 直通在设备物理重枚举后**不会自动重新分配**。libvirt 的 hostdev 条目在设备重枚举后变成失效条目（stale entry），不会自愈。整个守护进程就是为了补上这个缺口。

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
                        python3-libvirt 绑定 (attachDeviceFlags /
                        detachDeviceFlags / state / XMLDesc)
                                        │
                                        ▼
                              libvirt (qemu:///system) → QEMU → VM (guest)
```

**组件职责**：

| 组件 | 职责 |
|---|---|
| pyudev 事件源 | 监听内核 USB 设备事件，产出标准事件 dict |
| `Daemon` 状态机 | 事件 → 决策（是否/何时 attach/detach），持有设备状态表 + 定时器 |
| 定时器 | settle（attach 前等待）、去抖（remove 后确认） |
| 对账 reconcile | 周期性地把"物理设备清单"与"VM 配置清单"对齐，自愈一切漏网之鱼 |
| libvirt 动作层 | 通过 **python3-libvirt 绑定**执行 attach/detach/查询，全部幂等、容错 |

**为什么是"事件驱动 + 对账兜底"双保险（最重要的设计原则）**：

- 纯事件驱动会漏事件（守护进程崩溃重启、宿主休眠唤醒时的事件风暴、netlink 缓冲区溢出）；
- 纯轮询又太迟钝（attach 延迟不可接受）；
- 事件驱动保证响应速度（秒级），对账保证最终一致性（≤30s 收敛）。二者互补，缺一不可。

---

## 3. 核心设计原则

1. **状态未知时绝不动作**：读 VM 状态失败（libvirt 不可用/未运行）返回 `None`，调用方必须把 `None` 当作"不知道"而不是"没运行"——宁可跳过动作等下次对账，也绝不错 detach 设备。
2. **一切动作幂等**：attach 前先查 VM 当前配置，已挂就跳过（或先清失效条目再挂）；detach 容忍"设备已不存在"。
3. **对账是最终仲裁者**：任何事件路径的失败/遗漏，最终由 reconcile 收敛。
4. **守护进程只做运行期恢复，不碰持久配置**：传统上"开机直通归持久配置（如 virt-manager）、运行时恢复归守护进程"（见第 9 节）；但作者真机实测持久配置开机直通在部分设备上不可靠（usbfs claim 竞态，键鼠先后中招），部署上已改为纯守护进程接管（见第 9 节）。

---

## 4. 事件源：pyudev

**做什么**：`pyudev.Monitor.from_netlink()` 创建 libudev 内核监控，`filter_by(subsystem="usb", device_type="usb_device")` 只接收 USB 设备层事件，`select` + `poll(timeout=0)` 排空事件。

**为什么是 pyudev 而不是其他方案**：

| 方案 | 放弃的原因 |
|---|---|
| `udevadm monitor` 子进程 | 多一层管道和文本解析；子进程崩溃有事件丢失窗口 |
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

**为什么用 `PRODUCT` 而不是 `ID_VENDOR_ID`/`ID_MODEL_ID`**：`PRODUCT` 是内核 uevent 属性，**remove 事件也携带**；而 `ID_*` 是 udev 规则后处理属性，remove 时可能缺失。识别设备必须靠"add 和 remove 都有"的字段。

**为什么用 `DEVPATH`（如 `/devices/.../usb5/5-2/5-2.1`）而不是 `DEVNUM`/`/dev/bus/usb` 路径**：

- `DEVNUM` 每次重枚举都会变；
- `DEVPATH` 是端口拓扑路径，重枚举保持不变；
- 用 `DEVNUM` 做匹配或 hostdev 地址，一次重枚举就全部失效。

**为什么允许清单按 vid:pid 且不强制 serial**：部分设备**没有** `ID_SERIAL_SHORT`，只能按 vid:pid 匹配；有的设备虽有稳定 serial（且不同状态模式 serial 相同）。所以 serial 只能作为可选的精确化手段，不能是强制条件。

**关于"空闲/未连接"状态**：某些无线接收器设备在"已连接"与"未连接/空壳"两种状态使用不同 PID——只有真实设备状态进允许清单；空壳状态**不配任何清单**，守护进程对它的 add/remove 只做记录与去抖取消（见 §6.3），绝不直通。直通一个空壳设备毫无意义，还会在客户机里留下一堆死 HID（具体设备案例见 AUTHOR_DEPLOYMENT §3）。

**为什么 hub 过滤**：监控会收到 hub（USB class 09，含 root hub）的事件，用 `TYPE` 首字段判类过滤，避免无谓记录。

---

## 6. 事件处理状态机

### 6.1 只认 `add` / `remove`

一次物理插入会发一串事件：`add` → `change` → `bind`；拔出发 `unbind` → `remove`。状态机**只处理 `add` 和 `remove`**，其余动作（change/bind/unbind）一律忽略——否则一次插拔会被重复处理 3 次。

### 6.2 settle 延迟（attach 前等 1 秒）

**为什么**：`add` 事件发生在设备刚枚举时，接口驱动还没绑定完。实测证据：日志中 add 的 SEQNUM=7939、change=7940，而 bind 是 7965——中间 25 个序号全是接口层（`:1.0`/`:1.1`）的事件。此刻立刻 attach 可能失败或让客户机看到接口不完整的设备。等 1 秒让设备稳定。

### 6.3 去抖（remove 后等 1 秒再决定 detach）

**为什么**：无线设备（无线手柄、无线键鼠）在模式切换、休眠唤醒、配对变化时会**物理重枚举**——表现为 remove + add 紧跟着来（间隔最短可只有 1 秒）。去抖逻辑：

- remove 到达 → 等 1 秒；
- 期间/之后同一 `DEVPATH` 重新出现 → 判定为"重枚举/模式切换"，**跳过 detach**（避免无谓的取消再直通抖动）；
- 端口持续消失 → 确认是真拔 → 执行 detach。

**为什么 detach 对"设备已消失"容错**：物理 remove 时 QEMU 早已自己把设备摘了（usbfs 句柄失效），detach 调用可能失败或空操作——这是正常情况，记日志继续，不能崩、不能重试卡死。

**注意**：某些设备（如鼠标的蓝牙↔2.4G 开关）切换**不产生任何 udev 事件**（dongle 始终保持枚举）。这类设备切换时守护进程什么都做不了也不需要做——这是物理行为，不是守护进程的职责。

### 6.4 stale 条目：先清再挂（核心修复）

**场景**：设备直通中发生物理重枚举，VM 的 live XML 里残留失效 hostdev 条目。libvirt 不会自动恢复——**这正是 virt-manager 不重新分配设备的根因**。

**处理**：`add` 事件 → settle → 发现 `(vid,pid)` 已在 VM 配置里 → 判断这是**失效条目**（设备重枚举过）→ 先 `detach`（清掉失效条目）→ 再 `attach`（重新直通，客户机重新识别）。

**为什么"在配置里"不等于"可用"**：libvirt 的 hostdev 条目在设备物理消失后仍然留在 live XML 里，但 QEMU 侧的设备已经死了。只看 XML 会误判"已直通"而跳过——所以对 add 事件必须走"先清再挂"。

### 6.5 幂等

attach 前查 VM live 配置（`vm_snapshot()` 的 attached map，已在配置 → 清失效 → 重挂）；detach 前也查（不在配置 → 跳过）。保证重复事件、对账与事件并发时都不会重复动作。

### 6.6 定时器实现

字符串 key 的 `(due, key, func)` 列表，`set_timer` 同 key 自动去重、`fire_timers` 到期执行且异常隔离。定时器有 settle、去抖、对账三类，统一走同一机制；对账是**自续期周期定时器**（`_reconcile_tick` 每次执行后自行调度下一轮）。用 `heapq` 属于过度设计。

---

## 7. 对账（reconcile）

**做什么**：每 30 秒 + 启动时，把 `scan_physical_devices()`（当前物理允许设备，含 bus/device）与 `vm_snapshot()` 的 attached map（VM live XML 里的 hostdev，含 libvirt 记录解析地址）求差集：

- 物理在、VM 没有 → attach；
- VM 有、物理不在（且允许清单内）→ detach（清僵尸条目）；
- **物理在、VM 也有，但条目记录地址 ≠ 设备当前 bus/device** → 判定重枚举过、条目失效 → **先 detach 再 attach**（见下）；
- VM 未运行 → 全部标记未直通、不做任何事（libvirt 在 VM 停止时已自动释放 hostdev）。

**地址比对（stale 条目恢复）**：libvirt 在 attach 时会把解析到的宿主 bus/device 写进 live XML 的 `<source>`（如 `<address bus='5' device='47'/>`）。设备重枚举后 DEVNUM 变化，条目记录地址就陈旧了。对账发现"XML 记录地址 ≠ 物理设备当前地址"即判定失效（guest 早已丢了它），执行 detach + attach 恢复。这补上了"守护进程晚启动 / 守护进程停机期间事件丢失"时事件路径够不到的盲区——设备在位却一直没进 VM 的场景，现在对账也能自愈。地址缺失（`None`）时保守跳过（视为健康），避免误判。

**为什么必须有对账**（事件驱动必然漏事件的三类场景）：

1. **设备在 VM 启动前就插着**——udev 不会发 add，事件路径永远不会直通它；
2. **守护进程自身重启/崩溃/升级**——期间事件全丢；
3. **宿主休眠唤醒**——唤醒瞬间所有 USB 设备批量重枚举，事件风暴且时序混乱。

**为什么对账也是最终仲裁者**：任何事件路径的失败（attach 失败、detach 容错跳过、事件丢失），最终 30 秒内会被对账收敛到正确状态。

**为什么"VM 状态未知时对账直接中止"**：读 VM 状态失败（libvirt 不可用）= 无法确认 VM 状态，此时 attach/detach 都可能产生错误副作用，宁可中止等下次。

**手动触发即时对账**：收到 `SIGHUP` 信号后，守护进程用一个零延时、同 key 的对账定时器替换既定计划（`set_timer` 同 key 去重），在下一个事件循环迭代（≤0.5s）内立即执行一次对账（`sudo systemctl kill -s HUP usb-passthrough`），不需要等 30s 周期——用于部署验证或紧急恢复。

**内存卫生**：对账会删除"非白名单且已不在总线上"的设备记录（避免长期运行后记录无限增长）；白名单记录保留，因为可能有未触发的去抖定时器要查它。

---

## 8. libvirt 集成

### 8.1 hostdev XML 只按 vendor/product 匹配

```xml
<hostdev mode='subsystem' type='usb' managed='yes'>
  <source>
    <vendor id='0x1234'/>
    <product id='0x5678'/>
  </source>
</hostdev>
```

**为什么不加 `<address>`**：宿主侧地址（`<source><address bus=.. device=../>`）绑定 DEVNUM，重枚举必失效；且 libvirt 按 XML 记账，vendor/product 匹配时**物理设备已消失也能正常 detach**（匹配的是配置条目而不是设备）。

### 8.2 用 python3-libvirt 绑定，而非 virsh 子进程

**为什么弃用 virsh**（历史实现是 `subprocess.run(["virsh", ...])`）：

| virsh 子进程的问题 | 绑定的解法 |
|---|---|
| 输出文本解析（`domstate`、stderr）脆弱，旧实现靠 `LC_ALL=C` 兜底本地化坑 | 结构化 API：`dom.state()[0]` 直接是枚举值，`libvirtError` 带错误码 |
| XML 必须写临时文件传（此前实测该 virsh 版本把 `-` 当文件名 open） | `attachDeviceFlags(xml, ...)` / `detachDeviceFlags(xml, ...)` 直接收 XML **字符串** |
| 每次动作一个子进程 + 依赖 `virsh` 二进制在 PATH | 进程内 C 绑定调用 |

**API 对应关系**（行为完全等价；状态机决策逻辑与 virsh 时代零差异）：

- `virsh domstate` → `dom.state()[0] == libvirt.VIR_DOMAIN_RUNNING`（`vm_snapshot()` 的 running 部分）
- `virsh dumpxml` → `dom.XMLDesc(0)` + 同款 regex 解析 hostdev（`_parse_hostdev_map`）：运行中域给 live XML，解析到的 `<source><address>` 与 virsh 完全一致——§7 地址比对的数据来源；唯一变化是 XML 字符串由绑定直接返回，不再经过 virsh 子进程
- `virsh attach-device --live` → `dom.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_LIVE)`
- `virsh detach-device --live` → `dom.detachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_LIVE)`

**为什么不用 `virDomainListHostdevs` 的类型化包装（实测决策记录）**：C API 自 5.7 起提供 `virDomainListHostdevs(sane=1)`——本意正是取代"dumpxml + 自己解析地址"。但 **python 绑定并不暴露它**：作者真机实测（libvirtd 12.0.0 + 发行版 python3-libvirt）报 `'virDomain' object has no attribute 'listHostdevs'`。因此 regex 解析 live XML 是绑定可见的唯一路径，保留（解析器隔离在 `_parse_hostdev_map()` 一处，若未来绑定补齐该 API 可换回）。

**为什么模块顶部 `import libvirt` 是 try/except**：与 pyudev 同理由（§4）——仅保证没装 libvirt 的机器（CI 回放测试）仍能加载模块；`run()` 里检查、缺失拒绝启动。这不是回退。

**版本配对**：python3-libvirt 版本必须与 libvirtd 配对，**必须用发行版包安装**（Fedora 系 `rpm-ostree install python3-libvirt`），严禁 pip 单装（版本错配会 ABI 报错）。

### 8.3 `managed='yes'`

libvirt 自动处理"从宿主驱动解绑 / 归还时回绑"，直通动作不会把宿主留在半绑定状态。

### 8.4 只用 `--live`（非持久）

**为什么**：持久配置属于 virt-manager（开机直通靠它）；守护进程只动运行态，VM 重启后持久配置照常生效，两者互不干扰、不会打架。绑定里对应 `attachDeviceFlags`/`detachDeviceFlags` 的 `VIR_DOMAIN_AFFECT_LIVE` 标志——与 `virsh --live` 是同一个标志位。

### 8.5 短生命期连接（每次调用新建，不缓存）

**为什么**：libvirtd 重启后旧连接会失效，缓存连接就得自己写"断线检测 + 重连"逻辑。每次动作现开现关（`_open()`：查询用 `openReadOnly`，attach/detach 用读写连接），libvirtd 重启天然在**下一次调用**自愈——正好落在"状态未知 → 跳过 → 对账重试"的既有框架里，无需任何重连代码。

**为什么不用 keepalive**（`setKeepAlive`，也不注册 `virEventRegisterDefaultImpl`）：keepalive 依赖一个**持续运转**的事件循环（`virEventRunDefaultImpl()` 要在循环里跑），但本守护进程是单线程、只在短促的同步 RPC 里阻塞，从不泵事件循环。注册了默认 poll 实现后，每条连接的 keepalive 定时器只有在该循环运行时才会被回收；开开/关关却不跑循环，会导致**每次连接关闭都漏掉文件描述符**，进程最终耗尽 fd 表，报 `Too many open files`（EMFILE，errno 24）——libvirt 连 `open()` 读 `/etc/libvirt/libvirt.conf` 都失败、pyudev `poll()` 也一起失败，正是本项目真机踩过的坑。加上这些连接只存活毫秒级（keepalive 5s 一探，根本来不及触发），而 libvirtd 宕机/重启在 `open()` 阶段就快速失败 → "状态未知 → 跳过"，keepalive 在这里既无效又只带来 fd 泄漏，故不启用。

**为什么显式释放 `dom`**：Python 侧 `virDomain` 对象持有对所属 `virConnect` 的引用；若它在连接 `close()` 之后才被 GC 回收，连接无法干净关闭、libvirt 会报 `One or more references were leaked after disconnect from the hypervisor`（并推迟 socket 释放）。因此 `vm_snapshot()` 在读完 `dom.state()`/`dom.XMLDesc(0)` 后、`with` 退出前显式 `del dom`，保证 `_conn` 的 `close()` 干净关闭。attach/detach 里的 `conn.lookupByName(name).xxx(...)` 是临时对象，表达式结束即释放，无此问题。

### 8.6 状态与配置一次读完（`vm_snapshot()`）

`vm_running()` + `vm_attached_devices()` 合并为 `vm_snapshot() -> (running, attached)`：**单只读连接**一次取 `dom.state()` + `dom.XMLDesc(0)`（仅 VM 运行中才读配置——VM 关闭时配置与决策无关，省一次 RPC）。两个用途（决策运行态、比对配置）的调用点几乎所有场景都同时需要两者，合并后从 2 次连接握手降为 1 次。

失败语义保持**两维独立**（都可单独返回 `None`）：

- `running=None`（状态未知）绝不是 `False`——第 3 节原则 1；
- `attached=None`（配置读不到）单独让调用方安全中止。

**为什么对账共享同一份 attached map**：对账一次 pass 内配置不会自变（变的只有本进程自己的动作），开头读一次传给 `_heal_attach` 复用，消灭"每设备重读一遍配置"的 N+1 冗余；事件路径（`bus=None`）仍以快照为准、语义不变。

---

## 9. 与持久配置（virt-manager）的分工

| 时机 | 谁负责 | 机制 |
|---|---|---|
| VM 开机（持久配置模式） | 持久配置 | libvirt 按 vendor/product 自动直通（设备在 USB 模式时） |
| VM 开机（纯 daemon 模式，作者当前部署） | 守护进程 | ≤30s 对账补直通（或 qemu hook 在 `started` 时 `--reconcile-once` 立即直通） |
| 设备重枚举/重插（运行中） | 守护进程 | add → settle → 清失效条目 → 重新直通 |
| 设备切无线/拔出（运行中） | 守护进程 | remove → 去抖 → detach |
| VM 关闭 | libvirt 自动 | 释放 hostdev、设备归还宿主 |
| 一切漏网之鱼 | 守护进程对账 | ≤30s 收敛 |

**持久配置 vs 纯守护进程（2026-08-29 修订）**：原设计倾向"保留持久配置做开机直通 + 守护进程做运行期恢复"，理由是开机直通"即插即用、零延迟、libvirt 原生最可靠"。**作者真机日志证伪了这个前提**：开机按持久配置直通键盘（`05ac:024f`）与鼠标（`1532:0083`）时，QEMU 反复报 `usbfs: ... did not claim interface 0/1(/2) before use`，设备被 reset + 宿主 HID 驱动重新绑定后**实际留在宿主机**，VM 里"看得到但用不了"；且该失败在 libvirt 层面表现为"成功"（live XML 留下地址一致的条目），对账地址比对判"健康"跳过、无法自愈（盲区详见 §12 第 6 条，证据见 AUTHOR_DEPLOYMENT.md §6/§11.2）。因此**作者部署改为：删除全部持久 USB hostdev，纯靠守护进程**——守护进程的 `--live` 直通路径（事件/对账）实测干净；开机立即直通用 qemu hook 补齐。设计上两套机制仍互不冲突（守护进程只动运行态），"保留持久配置"作为可选路径保留，但默认推荐纯守护进程。

---

## 10. 真机踩坑记录（通用）

本仓库踩过的全部真坑（含 virsh 时代的 locale 误判、临时文件传 XML，以及新版 libvirt 绑定的版本配对等）与正确做法已合并为**单一权威表**，见 [DEVELOPMENT.md §6](./DEVELOPMENT.md)——这里不再重复维护一份。

---

## 11. 验证方法论

**为什么需要自动化验证**：守护进程的行为（去抖、settle、重枚举判断）用真机验证成本高且不可重复，必须用真实数据锁定行为。

1. **回放测试 `test_replay.py`**：把**内嵌在代码里的真实捕获事件**（部署时用 `udevadm monitor` 在真实硬件上采集，**零文件依赖**）按时间戳回放进状态机，mock 掉 libvirt 和 sysfs，断言多项行为（attach/detach/不抖动/非允许清单设备无动作/stale 条目先清再挂/对账地址失配恢复/VM 配置不可读安全中止等）。零副作用、可重复、CI 友好。具体断言清单见 `docs/DEVELOPMENT.md`。
2. **集成冒烟**：只读扫描真实 sysfs（容器共享宿主 /sys），验证 `scan_physical_devices()`/`devpath_present()`/事件解析与真实设备数据吻合。
3. **真机验收**：VM 运行中逐项验证无线切换、开关机、VM 关机释放（本项目作者的真实验收记录见 `docs/AUTHOR_DEPLOYMENT.md`）。

**为什么 mock 而不是真连**：回放测试要锁定"状态机逻辑"，不依赖 libvirt 环境；真机行为用上述第 3 步单独验证。

---

## 12. 已知限制与权衡

1. **同 VID:PID 的多台设备无法区分**（如两个同型号设备），直通匹配第一台。需要精确到端口时可扩展 hostdev XML 加地址，但 DEVNUM 变化会使其失效，需配合对账刷新——权衡后保持 vendor/product。
2. **对账的覆盖边界**：30s 对账清理"已直通但物理已不存在"的僵尸条目，并通过地址比对恢复"重枚举后残留失效条目"（设备在位但 guest 没有）——守护进程晚启动、停机期间丢事件都能自愈。盲区之一：设备在位、VM 条目地址恰好缺失（`None`）且从未有过 add 事件——保守跳过，靠下次重插或 VM 重启收敛；另一个盲区（"libvirt 以为挂上、实际没挂上"）见第 6 条。
3. **30s 对账延迟**：VM 刚启动、设备已插着的场景最迟 30s 内补直通。可加 libvirt qemu hook（`started` 时执行 `--reconcile-once`）做到即时，作为可选增强。
4. **客户机侧重枚举掉线**：设备每次物理重枚举（休眠唤醒、模式切换）客户机都会掉线重连一次，这是 USB 物理行为，无法避免；守护进程保证不放大抖动、不误取消。
5. **某些无线设备开关切换不可见**（见 6.3 末尾）：VM 运行期间其接收器归 VM，宿主使用请走无线/蓝牙模式。
6. **开机"持久配置直通"的 usbfs claim 竞态（实测键盘、鼠标先后中招）**：libvirt 开机按持久 hostdev 直通时，QEMU 的 usbfs claim 与 reset 后宿主 HID 驱动重新绑定竞态，`did not claim interface 0/1(/2)` 报错、设备留在宿主机；且失败在 libvirt 层面表现为"成功"（live XML 地址一致），对账识别不了——这是对账地址比对盲区的第二个来源（见第 9 节）。作者部署已删除全部持久 USB hostdev、纯靠守护进程（+ qemu hook 实现开机即直通）。

---

## 13. 配置参数参考

| 环境变量 | 默认 | 为什么是这个默认 |
|---|---|---|
| `USB_PT_VM` | **无（必填）** | 目标虚拟机名，逗号分隔、**有序（顺序即直通优先级）**；设备按顺序直通给第一台运行中的 VM（一台设备同时只在一台 VM）；单台只写一个名字；缺失拒绝启动 |
| `USB_PT_ALLOWED` | **无（必填）** | 允许直通清单 `vid:pid,...`，至少一个；缺失拒绝启动。设备的空壳/空闲状态绝不能进此清单 |
| `USB_PT_SETTLE` | `1` | add 后等 1 秒让接口枚举完（见 6.2） |
| `USB_PT_DEBOUNCE` | `1` | remove 后等 1 秒确认不是重枚举（见 6.3） |
| `USB_PT_RECONCILE` | `30` | 对账周期：30 秒内收敛所有异常，兼顾响应与开销 |
| `USB_PT_ATTACH_RETRIES` | `3` | attach 失败重试 3 次（VM 刚启动 QEMU 未就绪时常见） |
| `USB_PT_ATTACH_RETRY_GAP` | `2` | 重试间隔 2 秒 |

> 数值型配置（SETTLE/DEBOUNCE/RECONCILE/ATTACH_RETRIES/ATTACH_RETRY_GAP）**全部是整数秒/次**；**写错（非整数）会直接拒绝启动**（fast fail：非零退出 + 明确报错），绝不带着错误配置或静默兜底运行；缺失才用默认值。历史上 RETRY_GAP 默认是 1.5（float），整数化后取 2。
> 作者部署的示例值见 `docs/AUTHOR_DEPLOYMENT.md`。

---

## 14. 代码地图（实现导览）

> 供接手开发者快速定位。函数名与当前实现一一对应；详细 why 见对应章节。

### 14.1 配置层

| 符号 | 作用 |
|---|---|
| `VidPid` / `BusDev` / `AttachedMap` | 类型别名：`(vid,pid)` / `(bus,device)` / `vid:pid → 记录地址` 的映射（地址缺失为 `None`） |
| `VM_NAMES` | 目标 VM 名**有序列表**（env `USB_PT_VM`，逗号分隔，顺序即优先级，必填，无内置默认） |
| `_parse_vms(s)` | 解析 `USB_PT_VM` 逗号分隔 → 有序名字列表（去空白、丢空段，`None`→`[]`） |
| `_env_int(name, default)` | 环境变量整数解析（所有数值配置共用）：缺失用默认值；**坏值（非整数）直接拒绝启动（fast fail：非零退出 + 明确报错），绝不兜底** |
| `_parse_pairs(s)` | 解析 `vid:pid` 逗号列表 → 元组列表；坏项记警告跳过（不崩溃） |
| `ALLOWED` | 允许直通清单（env `USB_PT_ALLOWED`，必填；空壳/空闲状态不得列入） |
| `SETTLE_SEC` / `REMOVE_DEBOUNCE_SEC` / `RECONCILE_SEC` / `ATTACH_RETRIES` / `ATTACH_RETRY_GAP` | 时序与重试参数（§13） |

### 14.2 libvirt 动作层

| 函数 | 职责 / 关键点 |
|---|---|
| `_open(readonly)` | **短生命期连接**（§8.5）：现开现关，libvirtd 重启在下一次调用自愈；查询走 `openReadOnly`，attach/detach 走读写连接；**不用 keepalive / 不注册事件循环**（避免 fd 泄漏，见 §8.5）；bindings 缺失/libvirtd 不可达 → `None`（= 状态未知） |
| `_conn(readonly)` | contextmanager 包装 `_open()`：with 块结束担保 `close()`；连接开失败时 yield `None` |
| `vm_snapshot(name)` | **单只读连接**返回单台 VM 的 `(running, attached)`（§8.6）：`dom.state()[0]==VIR_DOMAIN_RUNNING` + `dom.XMLDesc(0)`（仅运行中读配置）；两维失败独立返回 `None`；非 running 打印 `VM ... not running (state=...)`；**`VIR_ERR_NO_DOMAIN`（名字不存在）视为「确定未运行」返回 `(False,{})`**，不算未知 |
| `VMSnapshot` / `vm_snapshots()` | `VMSnapshot` = `(name, running, attached)` dataclass；`vm_snapshots()` 按 `VM_NAMES` 顺序逐台 `vm_snapshot(name)` 合成全量快照（每台一个短只读连接，§8.6 单机不变量不变） |
| `_parse_hostdev_map(xml)` | 正则解析 live XML 的 hostdev → **`{(vid,pid): (bus,device) 或 None}`**；地址从 `<source>` 里提取，是对账地址比对（§7）的数据来源；为什么不用 `virDomainListHostdevs` 类型化包装见 §8.2（python 绑定未暴露，实测决策记录） |
| `hostdev_xml(vid,pid)` | 生成 vendor/product-only 的 hostdev XML（§8.1） |
| `attach_device(vid,pid,name)` | 对 `name` 执行 `attachDeviceFlags(xml, VIR_DOMAIN_AFFECT_LIVE)`（§8.2）；重试 `ATTACH_RETRIES` 次、间隔 `ATTACH_RETRY_GAP`；成功返回 `True` |
| `detach_device(vid,pid,name)` | 对 `name` 执行 `detachDeviceFlags(xml, VIR_DOMAIN_AFFECT_LIVE)`；单次、**容错**（设备已消失/条目已不在视为正常，记日志返回 `False`） |

### 14.3 sysfs 层

| 函数 | 职责 |
|---|---|
| `devpath_present(devpath)` | `/sys + devpath` 目录是否存在——settle/去抖的**物理存在性判据**（比状态表更可靠） |
| `scan_physical_devices()` | pyudev 枚举 `usb_device` → **`{devpath: (vid, pid, bus, device)}`**；bus/device 读不到时为 `None` |

### 14.4 状态机 `Daemon`

| 成员 | 职责 |
|---|---|
| `DeviceState` | dataclass 记录 `{vid, pid, present, home}`——每端口设备状态（事件/对账共享）；`home` = 设备当前归属的 VM 名（`None` 未直通），承载「静态归属」记忆 |
| `self.devices` | `devpath → DeviceState`（事件/对账共享的状态表） |
| `self.timers` | `(due, key, func)` 列表；`set_timer` 同 key 去重、`fire_timers` 到期执行且异常隔离；settle/去抖/对账三类统一（对账为自续期周期定时器，§6.6） |
| `_reconcile_tick` | 一次对账（异常隔离）+ 自续期调度下一轮；也是 SIGHUP 手动触发的入口 |
| `handle_event(ev)` | 入口：校验 DEVTYPE / 解析 PRODUCT / hub 过滤 → 只分派 `add`/`remove`（§6.1） |
| `on_add` | 置 present → 非白名单短路 → 调度 settle 定时器（§6.2） |
| `attach_if_needed` | settle 到期执行：present + 物理存在 + `vm_snapshots()` 选属主 → 调 `_heal_attach`（§8.6） |
| `_entry_stale` | 失效判定：事件路径（bus=None）有条目即失效；对账路径按地址比对，记录地址缺失时保守视为健康 |
| `_pick_target` | 属主选择：任一 VM `running=None` → `unknown`（保守推迟）；无运行 VM → `none`；`rec.home` 仍运行则驻留不做迁移；否则取第一台运行 VM（顺序即优先级，停止后重新选举，§18） |
| `_heal_attach` | 事件/对账共享的「跨 VM 先清再挂 → 清 settle 定时器」：清除所有运行 VM 里的 stale/duplicate 条目，再 attach 到属主；目标已健康则幂等跳过（§6.4/§7/§18）；快照由调用方传入复用 |
| `_detach_if_listed` | 「所有运行 VM 里还列着该设备就 detach」的助手（`on_remove` 对未跟踪设备的即时清理用；接收调用方的全量快照） |
| `on_remove` | 未跟踪设备即时清理（`vm_snapshots()` 查配置）→ 已知设备置 present=False 并调度去抖（§6.3） |
| `maybe_detach` | 去抖到期执行：端口重现=重枚举跳过；真拔 → 从 `home` + 所有列出它的运行 VM detach |
| `reconcile` | 三动作：补 attach（经 `_heal_attach`）/ 清僵尸（跨所有运行 VM）；附内存卫生（§7/§18） |
| `run` | pyudev + libvirt 检查（缺失拒绝启动）→ monitor 初始化 → **启动即对账**（`_reconcile_tick`）→ select 循环（排空事件 / 定时器） |

### 14.5 入口 `main`

| 能力 | 说明 |
|---|---|
| `--reconcile-once` | 执行一次对账后退出（部署验证用） |
| `--debug` | DEBUG 级别日志 |
| 环境变量校验 | 缺 `USB_PT_VM` / `USB_PT_ALLOWED` 时打印缺失项并以非零码退出（守护循环与 `--reconcile-once` 都覆盖） |
| `SIGHUP` | 以同 key 零延时定时器替换对账计划 → 下一个循环迭代（≤0.5s）内立即对账（`systemctl kill -s HUP usb-passthrough`） |

---

## 15. 真实事件走查与设备案例

状态机的真实事件走查（含具体设备行为对照）与作者部署的设备案例见 [AUTHOR_DEPLOYMENT.md](./AUTHOR_DEPLOYMENT.md)——它们以作者的实际硬件为基础，是理解去抖/重枚举/stale 条目行为的最好实例。

---

## 16. 领域知识（接手必读）

### 16.1 USB 枚举与重枚举

- **插入 = 枚举**：分配 DEVNUM（单调递增、不立刻复用）→ 创建设备节点 → 绑定接口驱动；
- **重枚举 = 同一物理端口的一对 `remove`+`add`**：无线设备模式切换、休眠唤醒、拔插都会触发；
- 重枚举后 **DEVNUM 变化但 DEVPATH（端口拓扑）不变**——所以识别设备必须用 DEVPATH。

### 16.2 uevent 属性

- **内核 uevent 自带**：`ACTION`/`DEVPATH`/`SUBSYSTEM`/`DEVTYPE`/`PRODUCT`/`TYPE`/`BUSNUM`/`DEVNUM`——add 和 remove 事件都有；
- **udev 规则后处理才有**：`ID_VENDOR_ID`/`ID_MODEL_ID`/`ID_SERIAL` 等——remove 时可能缺失；
- 守护进程只用内核属性，所以**内核 socket（pyudev `from_netlink`）就够**，不需要等 udev 规则。

### 16.3 libvirt hostdev 语义（本项目最关键的外部知识）

- 按 vendor/product 匹配的 hostdev，libvirt 在 attach 时解析设备并把**解析地址写进 live XML 的 `<source>`**——这是对账地址比对（§7）能工作的前提；
- 设备物理消失后，live XML 条目**不会自动移除** → 变成失效条目（stale entry）；QEMU 侧设备已死、guest 已丢设备；
- libvirt **不会**在设备重枚举后自动恢复——这正是本项目存在的理由（virt-manager 持久直通也依赖同一机制，同样不会恢复）；
- `managed='yes'`：libvirt 自动处理宿主驱动解绑/回绑；
- `--live` 只动运行态（绑定里即 `VIR_DOMAIN_AFFECT_LIVE` 标志位）；持久配置（virt-manager）在 VM 启动时生效——两者分工见 §9。

---

## 17. 术语表

| 术语 | 含义 |
|---|---|
| uevent | 内核设备事件（经 netlink 发出），udev 及用户态监听的原生事件 |
| DEVNUM | 设备在总线上的编号（对应 `/dev/bus/usb/BUS/DEV`），重枚举会变 |
| DEVPATH | 设备 sysfs 路径（端口拓扑，如 `/devices/.../usb5/5-2.1.3`），重枚举不变 |
| PRODUCT | uevent 属性 `vid/pid/rev`，add/remove 都携带 |
| hostdev | libvirt 的设备直通条目（XML 中的 `<hostdev>`） |
| stale entry（失效条目） | VM 配置里设备已重枚举/消失但条目仍在的 hostdev |
| settle | add 后、attach 前的等待期（等接口枚举完） |
| 去抖（debounce） | remove 后、detach 前的等待期（区分真拔与重枚举） |
| reconcile（对账） | 周期性"物理设备清单 vs VM 配置清单"对齐 |
| IDLE / 空闲状态 | 无线接收器未连接设备时的空壳状态，不在允许清单，守护进程不做任何动作 |
| `managed='yes'` | libvirt 自动管理宿主驱动解绑/回绑 |
| 地址比对 | 对账用"XML 记录地址 vs 设备当前 bus/device"判断条目是否失效 |
| 属主/归属（home） | 设备当前直通到的 VM；静态归属——一旦直通后驻留该 VM，直至其关闭（§18） |

---

## 18. 多 VM 支持（属主选择）

**做什么**：`USB_PT_VM` 支持逗号分隔的**有序 VM 列表**（顺序即直通优先级）。设备按顺序直通给**第一台运行中**的 VM；一台设备同时只直通给一台 VM。

**为什么不迁移（静态归属）**：设备一旦正常直通到某台运行中的 VM，就固定留在那台（`DeviceState.home` 记忆归属），重枚举也回到它——即便更高优先级的 VM 之后也运行起来。这与本项目核心的「不放大抖动」原则一致：迁移意味着无谓的 detach+attach、客户机掉线重连。只有当归属 VM 关闭后，设备才按顺序转投下一台运行中的 VM。

**为什么状态未知就整体推迟**：因为「一台设备只在一台 VM」，若某台更高优先级 VM 的运行态读不到（`running=None`），无法排除设备已被其占用，此时直通给更低优先级 VM 有**重复直通**风险。故 `_pick_target` 对任一 `running=None` 直接返回 `unknown`，attach 与对账都推迟到下一轮（沿用 §3 原则 1）。注意：VM **名不存在**不算「未知」——`vm_snapshot` 把 `VIR_ERR_NO_DOMAIN` 视为「确定未运行」（返回 `(False,{})`），这样写错/漏建一个 VM 名只是让那台不参与，不会拖垮其它配置正确的 VM。

**选举规则**：`_pick_target` 从第一个 VM 开始顺序扫描，选定**第一台运行中**的 VM 作为属主；一旦选定就写入 `rec.home` 保持不变——重枚举、对账、更高优先级 VM 后来运行都不会改变归属。只有当该 VM **停止运行**后，`rec.home` 不再命中运行列表，才**重新从头选举**（取新的第一台运行 VM）。因此守护进程重启后（`home` 是内存态、随进程丢失）同样按「第一台运行 VM」重新归属。

**跨 VM 的「先清再挂」**：设备重枚举或配置调整后，stale/duplicate 条目可能同时残留在多台 VM。`_heal_attach` 在 attach 前遍历**所有运行 VM**，凡列出该 `(vid,pid)` 且判定失效（或属重复条目）的 VM 都先 `detach`，再向属主 `attach`，保证「一台设备至多出现在一台运行 VM」。

**对账不变量的推广**：`reconcile` 从「单 VM 求差集」推广为「对所有 VM 求差集」——补 attach（经 `_heal_attach`）、清僵尸（每个运行 VM 里、物理已消失且白名单内的条目都 detach）。单 VM（列表长度为 1）时路径退化回改动前的行为，完全向后兼容。
