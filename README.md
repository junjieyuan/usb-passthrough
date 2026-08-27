# USB 直通守护进程（udev + libvirt 热插拔）

把 USB 设备在宿主机与虚拟机（VM）之间自动热插拔：

- **设备插入** → 若 VM 正在运行，直通（attach）给 VM
- **设备拔出** → 若 VM 正在运行，取消直通（detach）
- **每 30s 对账** → 自愈（VM 启动前已插着的设备、服务重启、宿主休眠唤醒、漏掉的事件）

> 特定设备的行为差异与特殊处理（如某些无线接收器的"空闲状态"）见 [docs/AUTHOR_DEPLOYMENT.md](docs/AUTHOR_DEPLOYMENT.md)。

## 文件

| 文件 | 说明 |
|---|---|
| `usb-passthrough-daemon.py` | 主程序（Python 3 标准库；依赖 `python3-pyudev`（唯一事件源）+ `python3-libvirt`（VM 动作层）） |
| `usb-passthrough.service` | systemd 单元（通用模板，需配置必需环境变量） |
| `test_replay.py` | 回放验证状态机（**零文件依赖**：真实捕获事件内嵌在代码里；不操作真实设备/VM） |
| `docs/DESIGN.md` | 设计文档与决策记录（每条设计决策的 why；含代码地图、领域知识、术语表） |
| `docs/DEVELOPMENT.md` | 开发指南（接手开发必读：环境、测试体系、加设备流程、踩坑清单、提交约定） |
| `docs/AUTHOR_DEPLOYMENT.md` | **作者部署实录**（本项目作者的实际部署与开发环境记录，供参阅） |
| `LICENSE` | MIT License |
| `.github/workflows/test.yml` | CI：push/PR 自动跑语法检查 + 回放测试（零依赖） |

> 👷 要参与开发？先读 `docs/DEVELOPMENT.md`，再配合 `docs/DESIGN.md`。

## 安装

```bash
sudo install -m 755 usb-passthrough-daemon.py /usr/local/sbin/
sudo install -m 644 usb-passthrough.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now usb-passthrough

# 看日志
journalctl -u usb-passthrough -f
```

**配置（必需）**：守护进程只从环境变量读取配置，**不设置必需变量会拒绝启动**（非零退出）。编辑 `/etc/systemd/system/usb-passthrough.service`，取消 `USB_PT_VM` 与 `USB_PT_ALLOWED` 两行的注释并改成自己的值：

```ini
Environment=USB_PT_VM=myvm
Environment=USB_PT_ALLOWED=1234:5678
```

然后 `sudo systemctl daemon-reload && sudo systemctl restart usb-passthrough`。

**依赖：`python3-pyudev`（唯一事件源，无回退）+ `python3-libvirt`（VM 动作层，无回退）**——守护进程直接用 libudev 监控内核 uevent，用 libvirt python 绑定执行 attach/detach/状态查询（无 virsh 子进程、无文本解析），缺一即拒绝启动：

```bash
# Fedora Silverblue（不可变系统，需 rpm-ostree）
sudo rpm-ostree install python3-pyudev python3-libvirt
sudo systemctl restart usb-passthrough   # 或重启系统后生效

# 普通 Fedora / Arch 等
sudo dnf install python3-pyudev python3-libvirt   # Arch: sudo pacman -S python-pyudev libvirt-python
```

> 注意：`python3-libvirt` 的版本必须与运行中的 libvirtd 配对——**用发行版包安装**（如上），不要用 pip 装（版本不配对会导致 ABI 报错）。

启动日志确认事件源：`event source: pyudev (libudev, kernel uevent socket)`。

> 📖 每条设计决策背后的"为什么"（为什么用 DEVPATH 不用 DEVNUM、为什么去抖、为什么先清失效条目再直通、为什么用 libvirt python 绑定而不是 virsh 子进程……）见 **`docs/DESIGN.md`**。

## 工作原理

1. **pyudev（libudev 原生监控）**监听 **usb_device 层**事件（内核 uevent 直连、无子进程、无回退）；只用内核属性（`PRODUCT`/`TYPE`/`DEVTYPE`/`DEVPATH`），所以内核 socket 就够；
2. 事件只认 `ACTION=add` / `ACTION=remove`（一次插入会发 add→change→bind 三个事件，拔出发 unbind→remove，其余动作全部忽略）；
3. 设备身份用 **`PRODUCT`（vid/pid）** + **`DEVPATH`（端口路径）**，绝不用 DEVNUM（每次重枚举都会变）；
4. **add**：命中允许清单 → 等 1s（settle，等接口枚举完）→ VM running？→ attach（幂等；失败重试 3 次）；
5. **remove**：去抖 1s——同端口重新出现（= 无线设备模式切换/休眠唤醒/抖动等重枚举）就跳过取消；确认消失 → VM running？→ detach（设备已物理消失也容错）；
6. **每 30s 对账**：物理设备清单 vs VM 的 hostdev 清单——补 attach、清僵尸条目，外加**地址比对**（VM 条目记录的 bus/device ≠ 设备当前值 = 重枚举过 = 条目失效 → 先清再挂）；
7. **状态未知（libvirtd 挂了）时不动作**，宁可等下次对账，绝不错 detach。

### 允许清单（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `USB_PT_VM` | **无（必填）** | 目标虚拟机名 |
| `USB_PT_ALLOWED` | **无（必填）** | 允许直通的设备 `vid:pid,...`（至少一个）；设备的空壳/空闲状态（如某些无线接收器未连接时）绝不能进此清单 |
| `USB_PT_SETTLE` | `1` | add 后等待秒数（整数） |
| `USB_PT_DEBOUNCE` | `1` | remove 后去抖秒数（整数） |
| `USB_PT_RECONCILE` | `30` | 对账周期秒数 |
| `USB_PT_ATTACH_RETRIES` | `3` | attach 失败重试次数 |
| `USB_PT_ATTACH_RETRY_GAP` | `2` | 重试间隔秒数 |

> `USB_PT_VM` 与 `USB_PT_ALLOWED` 必须显式设置，缺失时守护进程拒绝启动并打印缺失项。数值型配置（SETTLE/DEBOUNCE/RECONCILE/ATTACH_RETRIES/ATTACH_RETRY_GAP）**只接受整数**，写错（如 `1.0`、`abc`）会直接拒绝启动（不兜底），未设置才使用默认值。作者部署的示例值见 `docs/AUTHOR_DEPLOYMENT.md`。

## 与 virt-manager 配合（推荐用法）

- 可保留 virt-manager 里的持久 hostdev 配置（VM 开机时 libvirt 自动直通）——**不冲突**：守护进程的 attach/detach 都只用 `--live`（绑定对应 `VIR_DOMAIN_AFFECT_LIVE`，不动持久配置），动作前先查 VM 当前配置，幂等；
- 守护进程负责**运行期恢复**：设备物理重枚举后，virt-manager 不会自动重新直通（libvirt 的 hostdev 条目在设备重枚举后变成失效条目，不会自愈）——守护进程在 `add` 事件后自动"先清掉失效条目再重新直通"；
- 检查持久 hostdev XML 时区分两种 `<address>`：
  - `<source>` **里面**的 `<address type='usb' bus='..' device='..'/>` 是**宿主侧**地址（对应 /dev/bus/usb 的 DEVNUM），重枚举后必然失效——**必须删掉**，只按 `<vendor>`/`<product>` 匹配；
  - `<hostdev>` 外层（`<source>` 的兄弟）的 `<address type='usb' bus='0' port='N'/>` 是**客户机侧**端口（设备出现在客户机模拟 USB 总线的哪个口），不会失效，可留可删；多个设备端口别重复，想省心就删掉让 QEMU 自动分配。
- 特定设备的完整恢复流程与事件流走查见 `docs/AUTHOR_DEPLOYMENT.md`。

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
| `VM <name> not running (state=5)` | 诊断行：VM 状态不是 running（5=shutoff；VM 关着时正常出现） |
| `refusing to start: required environment variables not set: ...` | 缺少必需环境变量（检查 .service 的 Environment=） |
| `add <vid:pid> ... (settle 1.0s)` | 设备插入，等 1s 后直通 |
| `attached <vid:pid> to <vm>` | 直通成功 |
| `remove ... (debounce 1.0s)` → `detached ...` | 真拔除，已取消直通 |
| `... re-enumerated, skipping detach` | 去抖判定为重枚举（模式切换/休眠唤醒），不取消 |
| `reconcile: hostdev <vid:pid> resolved at (5, 47) but device now at (5, 48) — stale entry, re-attaching` | 对账发现失效条目，先清再挂 |
| `attach ... failed:` | attach 失败（自动重试 3 次，仍失败交给下次对账） |
| `libvirt connection failed: ...` | libvirtd 不可用/未启动；状态未知，跳过动作等下次对账 |
| `python3-pyudev is required` / `python3-libvirt is required` | 缺依赖，装对应包后重启服务 |
| `python3-libvirt unavailable` | 仅 `--reconcile-once` 手动执行且未装 libvirt 包时出现；装发行版包即可 |

## 注意事项 / 已知限制

- **设备直通给 VM 后宿主机没有该 USB 输入**——这是设计内行为，宿主用其它方式（如蓝牙/无线）操作。具体设备的切换行为差异见 `docs/AUTHOR_DEPLOYMENT.md`。
- **同一 VID:PID 的多台设备无法区分**（如两个同型号设备），直通会匹配第一台。需要精确到端口时，可在 `hostdev_xml()` 生成的 XML 加上 `<address type='usb' bus='..' device='..'/>`（但 DEVNUM 变化会使其失效，需配合对账刷新）。
- **客户机侧**：设备每次物理重枚举（模式切换、休眠唤醒、拔插）都会掉线重连一次，这是 USB 物理行为，无法避免；本守护进程保证不放大抖动、不误取消。
- **对账的覆盖边界**：30s 对账清理"已直通但物理已不存在"的僵尸条目；并通过**地址比对**恢复"重枚举后残留失效条目"（VM 条目记录的 bus/device ≠ 设备当前值 → 判定失效 → 先清再挂）。所以即使守护进程晚启动、或停机期间漏了 add 事件，设备在位也能被对账重新直通。仅剩的盲区：条目地址缺失（无 `<address>`）时保守跳过，靠下次重插/VM 重启收敛。
- **可选增强**：想"VM 一启动就立即直通"而不是等 ≤30s 的对账，可加一个 libvirt qemu hook（`/etc/libvirt/hooks/qemu`）在 `started` 时执行 `--reconcile-once`。
