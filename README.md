# USB 直通守护进程（udev + libvirt 热插拔）

把 USB 键鼠/手柄在宿主机与 Windows 虚拟机之间自动热插拔：

- **设备插入** → 若 Windows VM 正在运行，直通（attach）给 VM
- **设备拔出** → 若 Windows VM 正在运行，取消直通（detach）
- **每 30s 对账** → 自愈（VM 启动前已插着的设备、服务重启、宿主休眠唤醒、漏掉的事件）

针对你的设备做了专门处理：8BitDo 手柄只在游戏模式（`2dc8:3106`）直通，`2dc8:3109` IDLE（手柄未连接）状态一律忽略；8BitDo 的模式切换/无线设备休眠唤醒引起的重枚举不会误触发"取消直通"。

> **重要**：Razer 鼠标的蓝牙↔2.4G 开关切换**不产生任何 udev 事件**（dongle 始终保持 USB 枚举），详见"注意事项"。

## 文件

| 文件 | 说明 |
|---|---|
| `usb-passthrough-daemon.py` | 主程序（Python 3 标准库；依赖 `python3-pyudev`，唯一事件源） |
| `usb-passthrough.service` | systemd 单元 |
| `test_replay.py` | 回放验证状态机（**零文件依赖**：33 个真实捕获事件内嵌在代码里；不操作真实设备/VM） |
| `docs/DESIGN.md` | **详尽设计文档与决策记录**（每条设计决策的 why，即代码的"why 注释"载体） |

## 安装

```bash
sudo install -m 755 usb-passthrough-daemon.py /usr/local/sbin/
sudo install -m 644 usb-passthrough.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now usb-passthrough

# 看日志
journalctl -u usb-passthrough -f
```

**依赖：`python3-pyudev`（唯一事件源，无回退）**——守护进程直接用 libudev 监控内核 uevent（无子进程、无文本解析），没装会拒绝启动：

```bash
# Fedora Silverblue（不可变系统，需 rpm-ostree）
sudo rpm-ostree install python3-pyudev
sudo systemctl restart usb-passthrough   # 或重启系统后生效

# 普通 Fedora / Arch 等
sudo dnf install python3-pyudev          # Arch: sudo pacman -S python-pyudev
```

启动日志确认事件源：`event source: pyudev (libudev, kernel uevent socket)`。

> 📖 每条设计决策背后的"为什么"（为什么用 DEVPATH 不用 DEVNUM、为什么去抖、为什么先清失效条目再直通、为什么 `LC_ALL=C`、为什么临时文件传 XML……）见 **`docs/DESIGN.md`**。

VM 名不是 `windows` 的话，编辑 `/etc/systemd/system/usb-passthrough.service` 里注释掉的 `Environment=USB_PT_VM=...`。

## 工作原理

1. **pyudev（libudev 原生监控）**监听 **usb_device 层**事件（内核 uevent 直连、无子进程、无回退）；只用内核属性（`PRODUCT`/`TYPE`/`DEVTYPE`/`DEVPATH`），所以内核 socket 就够；
2. 事件只认 `ACTION=add` / `ACTION=remove`（一次插入会发 add→change→bind 三个事件，拔出发 unbind→remove，其余动作全部忽略）；
3. 设备身份用 **`PRODUCT`（vid/pid）** + **`DEVPATH`（端口路径）**，绝不用 DEVNUM（每次重枚举都会变，见 8BitDo 045→052、Razer 004→053）；
4. **add**：命中允许清单 → 等 1s（settle，等接口枚举完）→ VM running？→ attach（幂等；失败重试 3 次）；
5. **remove**：去抖 1s——同端口重新出现（= 8BitDo 模式切换/休眠唤醒/抖动等重枚举）就跳过取消；确认消失 → VM running？→ detach（设备已物理消失也容错）；
6. **每 30s 对账**：物理设备清单 vs VM 的 hostdev 清单——补 attach、清僵尸条目，外加**地址比对**（VM 条目记录的 bus/device ≠ 设备当前值 = 重枚举过 = 条目失效 → 先清再挂）；
7. **状态未知（libvirtd 挂了）时不动作**，宁可等下次对账，绝不错 detach。

### 允许清单（环境变量覆盖）

| 变量 | 默认 | 说明 |
|---|---|---|
| `USB_PT_VM` | `windows` | 目标虚拟机名 |
| `USB_PT_ALLOWED` | `05ac:024f,1532:0083,2dc8:3106` | Keychron K6、Razer Basilisk X、8BitDo（仅游戏模式） |
| `USB_PT_IDLE` | `2dc8:3109` | 永不直通的状态（8BitDo IDLE），仅记日志 |
| `USB_PT_SETTLE` | `1.0` | add 后等待秒数 |
| `USB_PT_DEBOUNCE` | `1.0` | remove 后去抖秒数 |
| `USB_PT_RECONCILE` | `30` | 对账周期秒数 |
| `USB_PT_ATTACH_RETRIES` | `3` | attach 失败重试次数 |
| `USB_PT_ATTACH_RETRY_GAP` | `1.5` | 重试间隔秒数 |

## 与 virt-manager 配合（推荐用法）

你的实际用法是"virt-manager 持久直通 + 宿主蓝牙逃生"，这正是本守护进程设计的使用方式：

- **保留 virt-manager 里的持久 hostdev 配置**（不要删）——VM 开机时 libvirt 自动直通，这就是"虚拟机开机就能用"；
- **守护进程负责恢复**：键盘切蓝牙断开 USB、再切回 USB 后，virt-manager 不会重新直通（libvirt 的 hostdev 条目在设备物理重枚举后变成失效条目，不会自动恢复）——守护进程在 `add` 事件后自动"先清掉失效条目再重新直通"；
- 守护进程与持久配置**不冲突**：attach/detach 都只加 `--live`（不动持久配置），动作前先查 VM 当前配置，幂等，不会重复直通；
- **检查 virt-manager 生成的 hostdev XML**，区分两种 `<address>`：
  - `<source>` **里面**的 `<address type='usb' bus='..' device='..'/>` 是**宿主侧**地址（对应 /dev/bus/usb 的 DEVNUM），重枚举后必然失效——**必须删掉**，只按 `<vendor>`/`<product>` 匹配；
  - `<hostdev>` 外层（`<source>` 的兄弟）的 `<address type='usb' bus='0' port='N'/>` 是**客户机侧**端口（设备出现在 Windows 模拟 USB 总线的哪个口），不会失效，可留可删；多个设备端口别重复，想省心就删掉让 QEMU 自动分配。
- **手柄的持久配置必须写 `2dc8:3106`**（游戏模式）。写成 `2dc8:3109` 是常见错误——3109 是 IDLE 空壳（手柄未连接），激活时匹配不到、休眠时直通一堆没用的 HID；改完重启一次 VM 清掉运行态的旧条目。

事件流走查（K6，VM 运行中）：切蓝牙 → 拔线 → remove → 去抖 → `detach-device --live`（K6 回宿主蓝牙）；切回 USB → 插线 → add → settle → 清失效条目 → `attach-device --live` → Windows 重新识别 K6。

鼠标注意：Razer 切开关不产生 udev 事件，dongle 始终留在 VM 里（空闲），宿主走蓝牙，不经过上述恢复流程。

## 测试

```bash
# 回放内置的真实捕获事件，验证状态机行为（零文件依赖、不碰真实设备/VM）
python3 test_replay.py

# 单次对账（安全，可随时跑）
sudo /usr/local/sbin/usb-passthrough-daemon.py --reconcile-once --debug
```

## 运维

- **手动触发即时对账**（不等 30s 周期）：`sudo systemctl kill -s HUP usb-passthrough`
- **升级/重新部署**（改了代码后）：
  ```bash
  sudo install -m 755 usb-passthrough-daemon.py /usr/local/sbin/
  sudo systemctl restart usb-passthrough
  ```

## 日志速查（故障排查）

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

## 注意事项 / 已知限制

- **键盘/鼠标直通给 VM 后宿主机没有 USB 输入**——这是设计内行为，宿主用蓝牙模式操作。注意两台设备的切换行为不同：
  - **Razer 鼠标**：蓝牙↔2.4G 开关切换**不产生任何 udev 事件**（dongle 始终保持 USB 枚举）。切到蓝牙后，dongle **不会**自动从 VM 取消直通，而是留在 VM 里变成"空闲"设备，宿主通过蓝牙获得鼠标；切回 2.4G 时鼠标直接重连 VM 里的 dongle，无掉线。**VM 运行期间 dongle 归 VM 所有**：想在宿主用 2.4G 模式，只能停 VM 或手动 `virsh detach-device`（但 30s 对账会把它重新直通回去）——**宿主请用蓝牙**。
  - **Keychron K6**：切蓝牙需要拔 USB 线 → 真实 remove 事件 → 自动取消直通；插回 → add → 自动重新直通（若 VM 在运行）。
- **同一 VID:PID 的多台设备无法区分**（如两个同型号手柄），直通会匹配第一台。需要精确到端口时，把 `hostdev_xml()` 生成的 XML 加上 `<address type='usb' bus='..' device='..'/>`（但 DEVNUM 变化会使其失效，需配合对账刷新）。
- **Windows 侧**：设备每次物理重枚举（8BitDo 模式切换、休眠唤醒、拔插）都会掉线重连一次，这是 USB 物理行为，无法避免；本守护进程保证不放大抖动、不误取消。Razer 鼠标切开关不重枚举，Windows 里无掉线。
- **对账的覆盖边界**：30s 对账清理"已直通但物理已不存在"的僵尸条目；并通过**地址比对**恢复"重枚举后残留失效条目"（VM 条目记录的 bus/device ≠ 设备当前值 → 判定失效 → 先清再挂）。所以即使守护进程晚启动、或停机期间漏了 add 事件，设备在位也能被对账重新直通。仅剩的盲区：条目地址缺失（无 `<address>`）时保守跳过，靠下次重插/VM 重启收敛。
- **可选增强**：想"VM 一启动就立即直通"而不是等 ≤30s 的对账，可加一个 libvirt qemu hook（`/etc/libvirt/hooks/qemu`）在 `started` 时执行 `--reconcile-once`。
