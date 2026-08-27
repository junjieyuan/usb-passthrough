# AGENTS.md — USB 直通守护进程（Agent 开发指南）

## 项目概览

**usb-passthrough-daemon**：基于 udev + libvirt 的 Linux USB 热插拔直通守护进程，在宿主机与 KVM 虚拟机之间自动切换 USB 输入设备（键鼠、手柄等）。

**核心功能**：
- 设备插入 → VM 运行中自动 attach 直通
- 设备拔出 → VM 运行中自动 detach 取消直通
- 每 30s 周期性对账 → 自愈漏事件、服务重启、休眠唤醒等异常

**核心设计理念**：
- 事件驱动（秒级响应）+ 对账兜底（最终一致性，≤30s 收敛）
- **状态未知时绝不动作**：`vm_running()` 返回 `None` 时宁可跳过等下次对账，绝不误 attach/detach
- 一切动作幂等、容错，靠对账收敛
- 只动运行态（`--live`），不碰持久配置，与 virt-manager 分工

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `usb-passthrough-daemon.py` | 主程序（单文件；Python 3 标准库 + `python3-pyudev`，唯一事件源） |
| `test_replay.py` | 自包含回放测试（内嵌真实捕获事件，全 mock，**零外部依赖**） |
| `usb-passthrough.service` | systemd 单元模板（`After=libvirtd`、`Restart=always`，需配 `Environment=`） |
| `docs/DESIGN.md` | 设计文档：每条设计决策的 why、代码地图、领域知识、术语表 |
| `docs/DEVELOPMENT.md` | 开发指南：环境、测试体系、加设备流程、踩坑清单 |
| `docs/AUTHOR_DEPLOYMENT.md` | 作者实际部署实录（设备行为案例、SSH 提交约定） |
| `.github/workflows/test.yml` | CI：push/PR 自动跑语法检查 + 回放测试 |

---

## 核心设计约束（改代码前必读）

1. **设备识别**：身份 = `PRODUCT`(vid/pid) + `DEVPATH`（端口路径）。**绝不用 DEVNUM**（每次重枚举都变）。

2. **事件处理**：只认 `ACTION=add` / `ACTION=remove`，忽略 `change`/`bind`/`unbind`（一次插入会连发多个事件）。

3. **时序控制**：
   - `add` → 等 `USB_PT_SETTLE`（默认 1s）等接口枚举完 → 再 attach
   - `remove` → 等 `USB_PT_DEBOUNCE`（默认 1s）→ 端口未重现才 detach（过滤无线设备重枚举、休眠唤醒）

4. **stale 条目**：设备重枚举后 libvirt 的 hostdev 条目不会自愈，必须 **先 detach 再 attach** 才能恢复。

5. **对账（reconcile）**：
   - 启动时立即执行一次；每 `USB_PT_RECONCILE`（默认 30s）周期执行；`SIGHUP` 立即触发
   - 做三件事：补 attach（物理在、VM 没有）／地址比对恢复（VM 条目记录地址 ≠ 设备当前 bus/device = 失效 → 先清再挂）／清僵尸（VM 有、物理不在且属允许清单）
   - VM 状态未知时对账直接中止，等下次周期
   - 对账成功后需取消该设备残留的 settle 定时器，避免 detach+attach 抖动

6. **libvirt 调用规范**：
   - 所有 `virsh` 调用强制 `LC_ALL=C`（否则中文 locale 输出 `运行`，`running` 比较失败）
   - XML 必须写临时文件再传给 virsh（实测该版本不认 `-` 标准输入）
   - hostdev XML **只按 vendor/product 匹配，不加 `<address>`**（宿主侧地址绑定 DEVNUM，重枚举必失效）
   - 只用 `--live`（不碰持久配置，与 virt-manager 各管一摊：开机直通归持久配置，运行期恢复归守护进程）

7. **配置**：**全部来自环境变量，代码零硬编码设备/VM 名**。`USB_PT_VM` 与 `USB_PT_ALLOWED` 必填，缺失拒绝启动（非零退出）。

8. **代码风格**：
   - 保持单文件主程序，按「配置层 → libvirt 动作层 → sysfs 层 → Daemon 状态机 → main」分层组织
   - 关键决策的 why 写在 `docs/DESIGN.md`，写清楚后才算完成改动

---

## 测试要求（改动后必跑）

```bash
python3 -m py_compile usb-passthrough-daemon.py test_replay.py
python3 test_replay.py
```

- CI（`.github/workflows/test.yml`）在 push/PR 时自动执行上述两步，PR 必须全绿。
- `test_replay.py` 不需要 pyudev/libvirt：import 前固顶环境变量、mock 掉 `vm_running`/`vm_attached_devices`/`scan_physical_devices`/`attach_device`/`detach_device`/`devpath_present`/`time`，回放内嵌的真实事件流并断言状态机行为。
- 回放循环顺序固定：**先 `fire_timers()` 再更新 fake sysfs 再 `handle_event()`**，不能反（settle/去抖判定依赖定时器先于事件生效）。
- 新增断言场景：要么往 `EVENTS` 加事件流（时间戳决定 settle/去抖时序，须与真实行为一致），要么做定向场景测试（参考既有 "stale-entry recovery"/"reconcile stale-address"/"reconcile unreadable VM config" 专项块）。
- 对账类测试须先把 `d.pyudev` mock 成非 None（`reconcile()` 开头有 pyudev 守卫）。

---

## 真机验证（可选，需 libvirt 环境）

```bash
sudo USB_PT_VM=myvm USB_PT_ALLOWED=1234:5678 \
  /usr/local/sbin/usb-passthrough-daemon.py --reconcile-once --debug
```

验证重点（对照 README「日志速查」）：
- 无线切换 → `remove → detached`；切回 → `add → attached`
- 空闲状态设备 → `idle-mode ... ignored`（无抖动）
- **守护进程晚启动恢复**是本项目核心场景，应见 `reconcile: hostdev ... resolved at ... but device now at ... — stale entry, re-attaching`

---

## 常见陷阱（全是本仓库踩过的真坑，别再踩）

| 陷阱 | 正确做法 |
|---|---|
| 用 DEVNUM 匹配设备 | DEVPATH + vid/pid |
| 处理 `change`/`bind` 当插入 | 只处理 add/remove |
| 依赖非英文 locale 的 virsh 输出 | 强制 `LC_ALL=C` |
| 用 `-` 把 XML 传给 virsh | 写临时文件 |
| 把 `vm_running()` 的 `None` 当"没运行" | `None` = 状态未知，绝不动作 |
| 对账只看"设备在不在 VM 配置" | 必须做地址比对，检测失效条目 |
| 写死默认 VM 名/设备 | 只从环境变量读，必填缺失拒绝启动 |
| 用 pyudev 的 `dev.get()` | 用 `dev.properties.get()`（0.24.1 起弃用） |

---

## 配置参数速查

| 环境变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `USB_PT_VM` | ✅ | — | 目标虚拟机名称 |
| `USB_PT_ALLOWED` | ✅ | — | 允许直通 `vid:pid`，逗号分隔（至少一个） |
| `USB_PT_IDLE` | — | 空 | 空闲状态 `vid:pid`，永不直通，仅记日志 |
| `USB_PT_SETTLE` | — | `1.0` | add 后等待秒数 |
| `USB_PT_DEBOUNCE` | — | `1.0` | remove 后去抖秒数 |
| `USB_PT_RECONCILE` | — | `30` | 对账周期秒数 |
| `USB_PT_ATTACH_RETRIES` | — | `3` | attach 失败重试次数 |
| `USB_PT_ATTACH_RETRY_GAP` | — | `1.5` | 重试间隔秒数 |

---

## 领域术语速查

| 术语 | 含义 |
|---|---|
| DEVNUM | USB 总线设备编号，重枚举会变 |
| DEVPATH | sysfs 端口拓扑路径，重枚举不变 |
| PRODUCT | uevent 属性 `vid/pid/rev`，add/remove 都携带 |
| hostdev | libvirt 的 USB 直通条目（XML `<hostdev>`） |
| stale entry（失效条目） | 设备重枚举/消失后 VM 配置里残留的 hostdev |
| settle | add 后等待设备接口枚举完成 |
| 去抖（debounce） | remove 后等待，区分"真拔"与"重枚举/模式切换" |
| reconcile（对账） | 周期性对齐"物理设备清单 vs VM 配置清单" |
| IDLE / 空闲状态 | 无线接收器未连接时的空壳状态，永不直通 |
| 地址比对 | 对账用"XML 记录地址 vs 设备当前 bus/device"判断条目失效 |

---

## 进一步阅读

- 设计 why：`docs/DESIGN.md`（代码地图见 §14，领域知识见 §16，术语表见 §17）
- 开发流程/加设备/踩坑：`docs/DEVELOPMENT.md`
- 作者实机部署与设备案例：`docs/AUTHOR_DEPLOYMENT.md`