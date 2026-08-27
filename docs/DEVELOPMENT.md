# 开发指南（接手项目必读）

> 给接手辅助开发的工程师。阅读顺序建议：
> **README.md**（是什么、怎么部署）→ **docs/DESIGN.md**（设计与每条决策的 why、代码地图、领域知识）→ 本文（怎么改、怎么测、怎么提交）。
> 本项目作者的实际部署与验收记录见 [docs/AUTHOR_DEPLOYMENT.md](./AUTHOR_DEPLOYMENT.md)。

---

## 1. 环境准备

- **系统**：Linux（Fedora 系实测；systemd + libvirt 环境即可）
- **Python 3.11+**；生产依赖 `python3-pyudev`（唯一事件源）
- **测试不需要 pyudev**：`test_replay.py` 把 pyudev/sysfs/virsh 全部 mock 掉，裸 Python 就能跑
- **真机验证**需要：`virsh`（libvirt-client）、一个目标 KVM 虚拟机（名称由 `USB_PT_VM` 指定）、`python3-pyudev`

```bash
git clone git@github.com:junjieyuan/usb-passthrough.git && cd usb-passthrough
python3 -m py_compile usb-passthrough-daemon.py test_replay.py   # 语法
python3 test_replay.py                                            # 回放测试
```

## 2. 快速开始

```bash
# 1) 跑测试（不需要任何依赖）
python3 test_replay.py

# 2) 真机单次对账（需要 pyudev + virsh；只读+可能 attach/detach，安全）
sudo USB_PT_VM=myvm USB_PT_ALLOWED=1234:5678 \
  /usr/local/sbin/usb-passthrough-daemon.py --reconcile-once --debug

# 3) 部署为 systemd 服务（先配好 .service 里的必需环境变量，见 README「安装」）
```

> 注意：守护进程**只从环境变量读取配置**，缺 `USB_PT_VM` 或 `USB_PT_ALLOWED` 会拒绝启动（非零退出）。手动运行请显式传这两个变量。

## 3. 代码导览（一句话版）

| 文件 | 作用 |
|---|---|
| `usb-passthrough-daemon.py` | 唯一程序文件：配置层 → libvirt 动作层 → sysfs 层 → 状态机 `Daemon` → 入口 `main`（**逐函数地图见 DESIGN.md §14**） |
| `test_replay.py` | 自包含回放测试：真实捕获事件内嵌 + mock，多项断言 |
| `usb-passthrough.service` | systemd 单元（通用模板；`After=libvirtd`、`Restart=always`，需配 `Environment=`） |

## 4. 如何新增一个 USB 设备

1. 确定设备的 **vid:pid**（`lsusb`）；
2. 观察其**重枚举行为**：拔插/模式切换时跑
   `udevadm monitor --property --udev --subsystem-match=usb/usb_device`，
   确认有没有"空闲/未连接"状态（无线接收器常见——**这种状态要进 `USB_PT_IDLE`，绝不能进 `USB_PT_ALLOWED`**）；
3. 改配置：在 systemd 单元的 `Environment=USB_PT_ALLOWED=`（及需要时 `USB_PT_IDLE=`）里加入该设备（**已无代码默认值**）；
4. 验证：`python3 test_replay.py` + 真机 `--reconcile-once --debug`；
5. 加测试（见 §5.2），保证新设备的重枚举行为被断言锁定。

## 5. 测试体系

### 5.1 `test_replay.py` 结构

| 部分 | 作用 |
|---|---|
| `EVENTS` | 内嵌真实事件 `(时间戳, action, devpath, PRODUCT)`（含无线设备全部模式切换；来源为作者真机捕获，详见 AUTHOR_DEPLOYMENT.md） |
| `SEED` | 模拟"守护进程启动前设备已在位且已直通"（否则首个事件会被当作 untracked） |
| mocks | `vm_running`/`vm_attached_devices`/`scan_physical_devices`/`attach_device`/`detach_device`/`devpath_present`/`time` 全部替换为测试替身 |
| 回放循环 | 每个事件：先 `fire_timers()`（触发到期定时器）→ 更新 fake sysfs → `handle_event()`。**顺序不能反**：去抖/settle 的判定依赖"定时器先于事件生效" |
| `checks` | 断言列表，全部 PASS 才 exit 0 |

> 环境变量在 import 前固定（含 `USB_PT_VM`），保证测试环境与外部 shell 无关。

### 5.2 如何加断言场景

**方式 A（事件流）**：往 `EVENTS` 追加 `(时间戳, action, devpath, product)`——时间戳决定 settle/去抖的时序判定，必须与真实行为一致；再在 `checks` 加断言。

**方式 B（定向场景）**：不依赖 `EVENTS`，单独构造 `Daemon` + 定向 mock。参考现有专项检查块：
- "stale-entry recovery"（事件路径：add → 配置里有失效条目 → 先清再挂）
- "reconcile stale-address"（对账路径：XML 记录地址 ≠ 设备当前地址 → 恢复）
- "reconcile unreadable VM config"（dumpxml 失败 → 安全中止）

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
| 认为 `domstate` 输出是英文 | 所有 virsh 调用强制 **`LC_ALL=C`**（非英文 locale 输出 `运行`，字符串比较失败） |
| 用 `-` 把 XML 传给 virsh | 写**临时文件**（实测该 virsh 版本把 `-` 当文件名 open，报"打开文件 '-' 失败"） |
| 把 `vm_running()` 的 `None` 当"没运行" | **`None` = 状态未知，绝不动作**（attach/detach 都跳过，等对账） |
| 用 `dev.get()`（pyudev） | 用 **`dev.properties.get()`**（0.24.1 起弃用，1.0 移除） |
| 对账只看"设备在不在 VM 配置里" | 还要**地址比对**（配置里有条目 ≠ 可用——设备重枚举后条目是死的） |
| 以为 VM 重启设备会自动回来 | libvirt **不会**自动恢复重枚举——这是本项目的存在理由 |
| 在 udev 规则里做状态逻辑 | 用守护进程 + 对账（有 settle/去抖/对账等状态，规则做不了） |
| 给守护进程写死默认设备/VM 名 | **只从环境变量读取**（`USB_PT_VM`/`USB_PT_ALLOWED` 必填，缺失拒绝启动） |

## 7. 提交与推送

提交与推送的 SSH 环境约定（密钥、`SSH_AUTH_SOCK` 绕开等）见 [docs/AUTHOR_DEPLOYMENT.md](./AUTHOR_DEPLOYMENT.md)。

## 8. 改完后的真机验收

1. 配置 .service 的必需环境变量并部署 + 重启（见 README「安装」「运维」节）；
2. VM 运行中逐项验证（对照 README「日志速查」）：
   - 设备切无线/拔出 → `remove → detached`；切回 → `add → attached`
   - 空闲状态设备 → `idle-mode ... ignored`（无抖动）
   - **守护进程晚启动恢复**（本项目的核心场景）：停掉服务 → 拔插一次设备 → 启动服务 → 对账应见 `reconcile: hostdev ... resolved at ... but device now at ... — stale entry, re-attaching`
   - VM 关闭 → 设备即时归还宿主
3. 本项目作者的具体设备验收记录见 `docs/AUTHOR_DEPLOYMENT.md`。
