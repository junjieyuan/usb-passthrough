# 开发指南（接手项目必读）

> 给接手辅助开发的工程师。阅读顺序建议：
> **README.md**（是什么、怎么部署）→ **docs/DESIGN.md**（设计与每条决策的 why、代码地图、领域知识）→ 本文（怎么改、怎么测、怎么提交）。
> 本项目作者的实际部署与验收记录见 [docs/AUTHOR_DEPLOYMENT.md](./AUTHOR_DEPLOYMENT.md)。

---

## 1. 环境准备

- **系统**：Linux（Fedora 系实测；systemd + libvirt 环境即可）
- **Python 3.11+**；生产依赖 `python3-pyudev`（唯一事件源）+ `python3-libvirt`（VM 动作层）。`python3-libvirt` 版本必须与 libvirtd 配对——**用发行版包安装**（`rpm-ostree install python3-libvirt` / `dnf install python3-libvirt`），不要 pip 装
- **测试不需要 pyudev/libvirt**：`test_replay.py` 把 pyudev/sysfs/libvirt 动作函数全部 mock 掉，裸 Python 就能跑
- **真机验证**需要：`python3-pyudev` + `python3-libvirt`（绑定代替 virsh）、一个目标 KVM 虚拟机（名称由 `USB_PT_VM` 指定）

```bash
git clone git@github.com:junjieyuan/usb-passthrough.git && cd usb-passthrough
python3 -m py_compile usb-passthrough-daemon.py test_replay.py   # 语法
python3 test_replay.py                                            # 回放测试
```

## 2. 快速开始

```bash
# 1) 跑测试（不需要任何依赖）
python3 test_replay.py

# 2) 真机单次对账（需要 pyudev + python3-libvirt；只读+可能 attach/detach，安全）
sudo USB_PT_VM=myvm USB_PT_ALLOWED=1234:5678 \
  /usr/local/sbin/usb-passthrough-daemon.py --reconcile-once --debug

# 3) 部署为 systemd 服务（先配好 .service 里的必需环境变量，见 README「安装」）
```

> 注意：守护进程**只从环境变量读取配置**，缺 `USB_PT_VM` 或 `USB_PT_ALLOWED` 会拒绝启动（非零退出）。手动运行请显式传这两个变量。

## 3. 代码导览（一句话版）

| 文件 | 作用 |
|---|---|
| `usb-passthrough-daemon.py` | 唯一程序文件：配置层 → libvirt 动作层 → sysfs 层 → 状态机 `Daemon`（每端口设备记录为 `DeviceState` dataclass）→ 入口 `main`（**逐函数地图见 DESIGN.md §14**） |
| `test_replay.py` | 自包含回放测试：真实捕获事件内嵌 + mock，多项断言 |
| `usb-passthrough.service` | systemd 单元（通用模板；`After=libvirtd`、`Restart=always`，需配 `Environment=`） |

## 4. 如何新增一个 USB 设备

1. 确定设备的 **vid:pid**（`lsusb`）；
2. 观察其**重枚举行为**：拔插/模式切换时跑
   `udevadm monitor --property --udev --subsystem-match=usb/usb_device`，
   确认有没有"空闲/未连接"状态（无线接收器常见——**这种状态绝不能进 `USB_PT_ALLOWED`，保持不配置、由守护进程无视即可**）；
3. 改配置：在 systemd 单元的 `Environment=USB_PT_ALLOWED=` 里加入该设备的**真实状态**（**已无代码默认值**）；
4. 验证：`python3 test_replay.py` + 真机 `--reconcile-once --debug`；
5. 加测试（见 §5.2），保证新设备的重枚举行为被断言锁定。

## 5. 测试体系

### 5.1 `test_replay.py` 结构

| 部分 | 作用 |
|---|---|
| `EVENTS` | 内嵌真实事件 `(时间戳, action, devpath, PRODUCT)`（含无线设备全部模式切换；来源为作者真机捕获，详见 AUTHOR_DEPLOYMENT.md） |
| `SEED` | 模拟"守护进程启动前设备已在位且已直通"（否则首个事件会被当作 untracked） |
| mocks | `vm_snapshot`/`vm_snapshots`/`VM_NAMES`/`scan_physical_devices`/`attach_device`/`detach_device`/`devpath_present`/`time` 全部替换为测试替身；`vm_snapshot` mock 带 `name` 形参 `lambda name: (running, attached)`（两维独立控制）；单 VM 场景改 `vm_snapshot`（`vm_snapshots()` 会自动组合），多 VM 场景改 `vm_snapshots` + `VM_NAMES`；场景函数经 `save_mocks()`/`restore_mocks()` 打补丁并复位 |
| `replay_main_flow()` | 回放 `EVENTS` 并断言主路径行为。回放循环每个事件：先 `fire_timers()`（触发到期定时器）→ 更新 fake sysfs → `handle_event()`。**顺序不能反**：去抖/settle 的判定依赖"定时器先于事件生效" |
| `scenario_*()` | 定向场景函数（清单见 §5.2），各自返回 `[(断言名, 通过, 说明)]` |
| `report()` | 汇总打印全部 `PASS/FAIL` 并决定退出码（有任一 FAIL 则 exit 1） |

> 环境变量在 import 前固定（含 `USB_PT_VM`），保证测试环境与外部 shell 无关。

### 5.2 如何加断言场景

**方式 A（事件流）**：往 `EVENTS` 追加 `(时间戳, action, devpath, product)`——时间戳决定 settle/去抖的时序判定，必须与真实行为一致；再在 `checks` 加断言。

**方式 B（定向场景）**：不依赖 `EVENTS`，新增或修改 `scenario_*()` 函数：单独构造 `Daemon` + 定向 mock（打完补丁必须 `restore_mocks()` 复位，否则污染后续场景）。参考现有场景：
- `scenario_stale_entry_recovery`（事件路径：add → 配置里有失效条目 → 先清再挂）
- `scenario_reconcile_stale_address`（对账路径：XML 记录地址 ≠ 设备当前地址 → 恢复；含地址匹配 no-churn 与 VM 配置读取失败安全中止）
- `scenario_event_filtering`（hub / change / bind / unbind / 错误 DEVTYPE 全部忽略）
- `scenario_bad_product_ignored`（PRODUCT 缺失/不可解析/单段不崩）
- `scenario_untracked_remove_listed`（未跟踪设备的 remove 即时清理；VM 未运行时不动）
- `scenario_attach_vm_unknown_skip` / `scenario_attach_exhausts_retries`（attach 的跳过与失败路径）
- `scenario_timer_same_key_dedupe`（同 key 定时器替换不叠加）
- `scenario_reconcile_vm_not_running`（对账边界：VM 未运行时无动作）
- `scenario_nonallowed_ignored`（非允许清单设备——如接收器的空壳状态——的 add 不产生任何定时器或动作）
- `scenario_multi_vm_order` / `_first_running_wins` / `_reelect_on_stop`（多 VM 属主选举：第一台运行 VM 胜出；无 home 记忆时重选第一台运行；属主 VM 停止后重新选举）
- `scenario_multi_vm_no_migration` / `_stale_reentry` / `_duplicate_sweep`（静态归属与跨 VM 清理：不迁移到更高优先级、重枚举仍回原 VM、清掉另一台 VM 的重复条目）
- `scenario_multi_vm_unknown_defer`（更高优先级 VM 状态未知 → 整体推迟）
- `scenario_multi_vm_dead_zombie`（物理已消失但多台 VM 均列有该设备 → 从所有 VM detach）
- `scenario_env_vm_list`（`_parse_vms`：逗号分隔/去空白/单值向后兼容/空值 → 空列表）
- `scenario_env_strict`（`_env_int`：合法/缺失值正常解析；坏值——含历史小数写法 `1.0`——经子进程验证**非零退出拒绝启动**，fast fail 不兜底）

**注意**：对账类测试要先把 `d.pyudev` mock 成非 None（`reconcile()` 开头有 pyudev 守卫，测试环境没装 pyudev 会直接中止）。

### 5.3 改动后必跑

```bash
python3 -m py_compile usb-passthrough-daemon.py test_replay.py
python3 test_replay.py
```

**CI**：仓库带 `.github/workflows/test.yml`——每次 push / PR 自动跑上述两步（测试零依赖，runner 不需要 pyudev/libvirt）。PR 前保证本地同样通过即可。

## 6. 开发时的坑（全部是本项目踩过的真坑）

| 坑 | 正确做法 |
|---|---|
| 用 DEVNUM 匹配设备 | 用 **DEVPATH（端口路径）+ PRODUCT（vid/pid）**——DEVNUM 每次重枚举都变 |
| 把 `change`/`bind` 当插入事件 | 只认 **`add`/`remove`**（一次插入发 add→change→bind 三连） |
| pip 装 `python3-libvirt` | 用**发行版包**装（版本必须与 libvirtd 配对，pip 版本错配会 ABI 报错；见 DESIGN.md §8.2） |
| 缓存 libvirt 连接跨 libvirtd 重启复用 | 每次调用开**短连接**现开现关——libvirtd 重启在下一次调用天然自愈，无需重连逻辑（DESIGN.md §8.5） |
| 以为 python 绑定暴露 `listHostdevs()`（C API 5.7 起有 `virDomainListHostdevs`） | 实测 `'virDomain' object has no attribute 'listHostdevs'`（libvirtd 12.0.0）——绑定不暴露，用 **`XMLDesc(0)` + `_parse_hostdev_map` 解析**（DESIGN.md §8.2 决策记录） |
| 直接 `setKeepAlive` 不注册事件循环 | 模块导入时先 `libvirt.virEventRegisterDefaultImpl()`，否则每次连接 stderr 刷 "caller doesn't support keepalive protocol" 且 keepalive 失效 |
| 把 `vm_snapshot()` 的 `running=None` 当"没运行" | **`None` = 状态未知，绝不动作**（attach/detach 都跳过，等对账） |
| 用 `dev.get()`（pyudev） | 用 **`dev.properties.get()`**（0.24.1 起弃用，1.0 移除） |
| 对账只看"设备在不在 VM 配置里" | 还要**地址比对**（配置里有条目 ≠ 可用——设备重枚举后条目是死的） |
| 以为 VM 重启设备会自动回来 | libvirt **不会**自动恢复重枚举——这是本项目的存在理由 |
| 在 udev 规则里做状态逻辑 | 用守护进程 + 对账（有 settle/去抖/对账等状态，规则做不了） |
| 给守护进程写死默认设备/VM 名 | **只从环境变量读取**（`USB_PT_VM`/`USB_PT_ALLOWED` 必填，缺失拒绝启动） |
| 对账先 attach 后，残留 settle 定时器再触发一次 detach+attach 抖动 | 对账 attach 成功后**取消该端口残留的 settle 定时器**（避免客户机设备无谓掉线重连） |
| 对账读 VM 配置瞬时失败（`vm_snapshot()` 的 attached 返回 `None`）未防护，`(vid,pid) in attached` 直接 TypeError | 检查 `attached is None`→**安全中止等下周期**（与"状态未知不动作"同原则） |
| 对账地址失配重挂后，残留 settle 定时器再次触发 detach+attach | 失配重挂后同步取消该端口 settle 定时器（`_heal_attach` 的失配分支成功与失败都取消），`attached` 状态按 attach 结果更新 |

> 本表是本仓库踩坑的**单一权威来源**（DESIGN.md §10 已改为指向此处）；AGENTS.md 保留一份精简速查版。

## 7. 提交与推送

提交与推送的 SSH 环境约定（密钥、`SSH_AUTH_SOCK` 绕开等）见 [docs/AUTHOR_DEPLOYMENT.md](./AUTHOR_DEPLOYMENT.md)。

## 8. 改完后的真机验收

1. 配置 .service 的必需环境变量并部署 + 重启（见 README「安装」「运维」节）；
2. VM 运行中逐项验证（对照 README「日志速查」）：
   - 设备切无线/拔出 → `remove → detached`；切回 → `add → attached`
   - 接收器待机空壳（不在允许清单）→ 无动作、零抖动
   - **守护进程晚启动恢复**（本项目的核心场景）：停掉服务 → 拔插一次设备 → 启动服务 → 对账应见 `reconcile: hostdev ... resolved at ... but device now at ... — stale entry, re-attaching`
   - VM 关闭 → 设备即时归还宿主
3. 本项目作者的具体设备验收记录见 `docs/AUTHOR_DEPLOYMENT.md`。
