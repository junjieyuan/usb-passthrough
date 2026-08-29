#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""usb-passthrough-daemon.py — hot-plug USB input devices into a VM (guest).

Watches USB hotplug events (usb_device level) for allowed keyboards / mice /
gamepads. While the target VM is running:
    * an allowed device appears    -> live-attach it to the VM (hostdev)
    * an allowed device disappears -> live-detach it from the VM (tolerant)
A periodic reconciliation heals missed events (service restarts, host
suspend/resume, devices already present when the VM started).

Event source: pyudev (libudev) — a native kernel-uevent monitor, no
fallback. Only kernel-level properties are used (PRODUCT / TYPE /
DEVTYPE / DEVPATH), so the kernel socket is sufficient. VM actions
(attach/detach/state queries) go through the libvirt python bindings —
no virsh subprocess.
Requires python3-pyudev and python3-libvirt; the daemon refuses to start
without either.

Design decisions (rationale in docs/DESIGN.md):
    * identity  = PRODUCT=vid/pid/rev (present on both add AND remove events)
                  + DEVPATH (stable port path; DEVNUM changes on every
                  re-enumeration)
    * only ACTION=add / ACTION=remove are acted upon
      (insertion fires add/change/bind, removal fires unbind/remove)
    * settle delay before attach (device still enumerating interfaces)
    * debounce before detach (wireless devices re-enumerate on sleep/wake
      or mode switches; a reappearing port means no real removal)
    * only allowlisted devices are ever acted upon; anything else is
      ignored
    * never act when the VM state is unknown (e.g. libvirtd down):
      attach/detach decisions are skipped, reconcile retries later
"""

import logging
import os
import re
import select
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

# pyudev is the ONLY event source (no fallback). Guarded import: tests can
# load this module without pyudev; the daemon refuses to start without it.
try:
    import pyudev  # noqa: F401
except ImportError:
    pyudev = None

# python3-libvirt drives the VM action layer (state queries, attach/detach).
# Guarded import like pyudev: tests can load this module without it; the
# daemon refuses to start without it. Version must pair with the running
# libvirtd (install via the distro package, not pip).
try:
    import libvirt  # noqa: F401
except ImportError:
    libvirt = None

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# (vendor id, product id)
VidPid = tuple[int, int]
# host bus/device address libvirt resolved at attach time
BusDev = tuple[int, int]
# vid:pid -> address recorded in the VM's live XML (None when the XML entry
# carries no <address>)
AttachedMap = dict[VidPid, BusDev | None]

log = logging.getLogger("usb-pt")

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment)
# ---------------------------------------------------------------------------

# Required: the ordered target VM name list and the allowlist come
# exclusively from the environment. The daemon refuses to start without
# them (see main()). USB_PT_VM is comma-separated; its order is the
# passthrough priority (a device goes to the first running VM). A single
# name still works (list of length one), so existing deployments are intact.
def _parse_vms(raw: str | None) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


VM_NAMES = _parse_vms(os.environ.get("USB_PT_VM"))


def _env_int(name: str, default: int) -> int:
    """Env var as an integer; missing uses the default, malformed aborts
    startup (deliberately fail-fast: running with a misconfigured value is
    worse than not running at all, so a bad value refuses to start instead
    of silently falling back to the default)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"error: invalid {name}={raw!r} (not an integer); refusing to "
                 f"start — fix it or unset it to use the default {default}")


def _parse_pairs(s: str) -> list[VidPid]:
    out: list[VidPid] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v, p = part.split(":", 1)
            out.append((int(v, 16), int(p, 16)))
        except ValueError:
            log.warning("ignoring bad pair: %r", part)
    return out


# Devices to pass through: comma-separated "vid:pid" hex pairs (required).
ALLOWED = _parse_pairs(os.environ.get("USB_PT_ALLOWED", ""))

SETTLE_SEC = _env_int("USB_PT_SETTLE", 1)
REMOVE_DEBOUNCE_SEC = _env_int("USB_PT_DEBOUNCE", 1)
RECONCILE_SEC = _env_int("USB_PT_RECONCILE", 30)
ATTACH_RETRIES = _env_int("USB_PT_ATTACH_RETRIES", 3)
ATTACH_RETRY_GAP = _env_int("USB_PT_ATTACH_RETRY_GAP", 2)


def is_allowed(vid: int, pid: int) -> bool:
    return (vid, pid) in ALLOWED


# ---------------------------------------------------------------------------
# libvirt helpers
# ---------------------------------------------------------------------------

def _open(readonly: bool = False):
    """Open a short-lived connection to the default hypervisor
    (same URI resolution as virsh, i.e. qemu:///system as root).

    A fresh connection per call, never cached: libvirtd restarts are then
    healed naturally by the next call instead of needing reconnect logic.
    Queries use read-only connections; attach/detach need read-write.
    Returns None when the bindings are missing or libvirtd is unreachable —
    callers treat that as unknown state and skip (reconcile retries later).
    """
    if libvirt is None:
        log.error("python3-libvirt unavailable")
        return None
    try:
        opener = libvirt.openReadOnly if readonly else libvirt.open
        # Deliberately NO keepalive (setKeepAlive) and NO libvirt event-loop
        # registration (virEventRegisterDefaultImpl). Keepalive requires an
        # event loop that is pumped continuously, but this single-threaded
        # daemon only ever blocks in short, synchronous RPCs and never runs
        # virEventRunDefaultImpl(). With the default impl registered, each
        # connection's keepalive timer is only reaped when that loop runs;
        # opening/closing connections without running it leaks fds until the
        # process exhausts its fd table (EMFILE "Too many open files"). A
        # dead/restarting libvirtd already fails fast at open() -> "state
        # unknown" -> skip, and these connections live for milliseconds, so
        # keepalive (which pings on a 5s interval) never gets a chance to
        # fire here anyway.
        return opener(None)
    except libvirt.libvirtError as e:
        log.error("libvirt connection failed: %s", e)
        return None


@contextmanager
def _conn(readonly: bool = False):
    """With-block wrapper around _open(): guarantees close() on the way
    out. Yields None when the connection could not be opened."""
    c = _open(readonly)
    try:
        yield c
    finally:
        if c is not None:
            c.close()


def vm_snapshot(name: str) -> tuple[bool | None, AttachedMap | None]:
    """One read-only connection for one VM: (running?, USB hostdevs in config).

    running is True/False/None — None means the state could not be
    determined and must NEVER be treated as "not running": skip the action,
    reconcile retries later. attached is {(vid,pid): (host bus, host device)
    or None} for USB hostdevs, or None when the config could not be read
    (only fetched while the VM runs; unused by callers otherwise). The two
    failure modes stay distinct so callers can abort on either.

    The address in the map is libvirt's resolution of the device at attach
    time. Comparing it with the device's current bus/device numbers reveals
    re-enumerations: the VM entry is stale and the guest already lost
    the device.
    """
    with _conn(readonly=True) as conn:
        if conn is None:
            return None, None
        try:
            dom = conn.lookupByName(name)
            state = dom.state()[0]
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                # a mistyped / not-yet-defined VM name is definitively "not
                # running", NOT "unknown": treat it as off so it merely
                # doesn't participate instead of blocking every other VM
                log.info("VM %s not defined (not running)", name)
                return False, {}
            log.error("cannot read state of VM %s: %s", name, e)
            return None, None
        running = state == libvirt.VIR_DOMAIN_RUNNING
        if not running:
            log.info("VM %s not running (state=%s)", name, state)
            # Drop `dom` before `_conn` closes the connection: a virDomain
            # holds a reference to its virConnect, so leaving it alive makes
            # close() report "references leaked" and postpone socket teardown.
            del dom
            # config stays unread: it is irrelevant while the VM is off and
            # no caller consumes the map in that case
            return running, {}
        try:
            # flags=0 gives the live XML for a running domain, exactly like
            # `virsh dumpxml` (the resolved <address> lands in <source>)
            xml = dom.XMLDesc(0)
        except libvirt.libvirtError as e:
            log.error("cannot read config of VM %s: %s", name, e)
            del dom  # release before conn.close() (see not-running branch)
            return running, None
        del dom  # release before conn.close() (see not-running branch)
        attached = _parse_hostdev_map(xml)
    log.debug("vm snapshot %s: running=%r, attached=%r", name, running, attached)
    return running, attached


@dataclass
class VMSnapshot:
    """One VM's state snapshot: name + running + attached hostdev map."""
    name: str
    running: bool | None
    attached: AttachedMap | None


def vm_snapshots() -> list[VMSnapshot]:
    """Ordered snapshot of every configured VM (one short connection each).

    Order equals USB_PT_VM order = passthrough priority. Running/attached
    retain the same None semantics as vm_snapshot(): running=None is
    "unknown" (never "off"), attached=None is "config unreadable".
    """
    return [VMSnapshot(n, *vm_snapshot(n)) for n in VM_NAMES]


def _parse_hostdev_map(xml: str) -> AttachedMap:
    """{(vid,pid): (bus,device) or None} for USB hostdevs in the live XML.

    NOTE: libvirt's C API virDomainListHostdevs (>= 5.7) has a typed
    equivalent, but the python bindings do NOT expose it (measured on
    libvirtd 12.0.0: 'virDomain' object has no attribute 'listHostdevs').
    XMLDesc(0) + parse is the only binding-visible path, so it stays.
    """
    found: AttachedMap = {}
    for block in re.findall(r"<hostdev\b.*?</hostdev>", xml, re.S):
        mv = re.search(r"<vendor\s+id='(0x[0-9a-fA-F]+)'\s*/>", block)
        mp = re.search(r"<product\s+id='(0x[0-9a-fA-F]+)'\s*/>", block)
        if not (mv and mp):
            continue
        key = (int(mv.group(1), 16), int(mp.group(1), 16))
        addr = None
        ms = re.search(r"<source>.*?</source>", block, re.S)
        if ms:
            tag = re.search(r"<address\b[^>]*/>", ms.group(0))
            if tag:
                mb = re.search(r"\bbus='(\d+)'", tag.group(0))
                md = re.search(r"\bdevice='(\d+)'", tag.group(0))
                if mb and md:
                    addr = (int(mb.group(1)), int(md.group(1)))
        found[key] = addr
    return found


def hostdev_xml(vid: int, pid: int) -> str:
    # match by vendor/product only — deliberately NO <address>: a host-side
    # address binds DEVNUM, which changes on every re-enumeration
    return (
        "<hostdev mode='subsystem' type='usb' managed='yes'>\n"
        "  <source>\n"
        f"    <vendor id='0x{vid:04x}'/>\n"
        f"    <product id='0x{pid:04x}'/>\n"
        "  </source>\n"
        "</hostdev>\n"
    )


def attach_device(vid: int, pid: int, name: str) -> bool:
    """Live-attach to `name`; retries internally. Returns True on success.

    attachDeviceFlags(xml, VIR_DOMAIN_AFFECT_LIVE) is the exact API that
    `virsh attach-device --live` drives; the XML string goes in directly
    (no temp file), and libvirt resolves the host address at attach time.
    """
    xml = hostdev_xml(vid, pid)
    for attempt in range(1, ATTACH_RETRIES + 1):
        with _conn() as conn:
            if conn is None:
                reason = "libvirt unavailable"
            else:
                try:
                    conn.lookupByName(name).attachDeviceFlags(
                        xml, libvirt.VIR_DOMAIN_AFFECT_LIVE)
                    log.info("attached %04x:%04x to %s", vid, pid, name)
                    return True
                except libvirt.libvirtError as e:
                    reason = str(e)
        log.warning("attach %04x:%04x attempt %d/%d failed: %s",
                    vid, pid, attempt, ATTACH_RETRIES, reason)
        if attempt < ATTACH_RETRIES:
            time.sleep(ATTACH_RETRY_GAP)
    return False


def detach_device(vid: int, pid: int, name: str) -> bool:
    """Live-detach from `name`; tolerant when the device/entry is gone."""
    with _conn() as conn:
        if conn is None:
            return False
        try:
            conn.lookupByName(name).detachDeviceFlags(
                hostdev_xml(vid, pid), libvirt.VIR_DOMAIN_AFFECT_LIVE)
        except libvirt.libvirtError as e:
            log.info("detach %04x:%04x tolerated: %s", vid, pid, e)
            return False
    log.info("detached %04x:%04x from %s", vid, pid, name)
    return True


# ---------------------------------------------------------------------------
# sysfs helpers
# ---------------------------------------------------------------------------

def devpath_present(devpath: str) -> bool:
    return os.path.isdir("/sys" + devpath)


def scan_physical_devices() -> dict[str, tuple[int, int, int | None, int | None]]:
    """Return {devpath: (vid, pid, bus, device)} for allowlisted devices on
    the bus now. bus/device are the current host USB numbers (None if
    unreadable); they are compared against the VM entry's recorded address
    to detect stale hostdevs after re-enumeration."""
    out: dict[str, tuple[int, int, int | None, int | None]] = {}
    ctx = pyudev.Context()
    for dev in ctx.list_devices(subsystem="usb", DEVTYPE="usb_device"):
        try:
            vid = int(dev.attributes.get("idVendor").decode(), 16)
            pid = int(dev.attributes.get("idProduct").decode(), 16)
        except (AttributeError, ValueError):
            continue
        if not is_allowed(vid, pid):
            continue
        try:
            bus = int(dev.attributes.get("busnum").decode())
            devnum = int(dev.attributes.get("devnum").decode())
        except (AttributeError, ValueError):
            bus = devnum = None
        out[dev.device_path] = (vid, pid, bus, devnum)
    return out


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------

@dataclass
class DeviceState:
    """Per-port device record shared by the event and reconcile paths."""
    vid: int
    pid: int
    present: bool = False
    # VM this device is currently attached to (None = not attached). Seeded
    # with the VM once attached; on re-enumeration we prefer to re-attach to
    # this same home (static assignment) instead of migrating to a
    # higher-priority VM.
    home: str | None = None


# timer keys (settle/debounce keys carry the devpath as suffix)
TK_ATTACH = "attach:"
TK_DEBOUNCE = "debounce-remove:"
TK_RECONCILE = "reconcile"


class Daemon:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceState] = {}  # devpath -> record
        self.timers: list[tuple[float, str, Callable[[], None]]] = []

    # ---- timers ---------------------------------------------------------

    def set_timer(self, key: str, delay: float, func: Callable[[], None]) -> None:
        self.clear_timer(key)
        self.timers.append((time.monotonic() + delay, key, func))

    def clear_timer(self, key: str) -> None:
        self.timers = [t for t in self.timers if t[1] != key]

    def fire_timers(self) -> None:
        now = time.monotonic()
        due = [t for t in self.timers if t[0] <= now]
        self.timers = [t for t in self.timers if t[0] > now]
        for _, _, func in due:
            try:
                func()
            except Exception:
                log.exception("timer callback failed")

    def _reconcile_tick(self) -> None:
        """One reconcile pass, then self-reschedule for the next period.

        Also the SIGHUP path: set_timer(TK_RECONCILE, 0.0, ...) replaces the
        scheduled pass (same key), so the manual trigger fires immediately
        and the cycle continues from there.
        """
        try:
            self.reconcile()
        except Exception:
            log.exception("reconcile failed")
        self.set_timer(TK_RECONCILE, RECONCILE_SEC, self._reconcile_tick)

    # ---- udev events ----------------------------------------------------

    def handle_event(self, ev: dict) -> None:
        if ev.get("DEVTYPE") != "usb_device":
            return
        devpath = ev.get("devpath")
        action = ev.get("action")
        if not devpath:
            return
        product = ev.get("PRODUCT", "")
        parts = product.split("/")
        if len(parts) < 2:
            log.warning("event without usable PRODUCT: %s", action)
            return
        try:
            vid, pid = int(parts[0], 16), int(parts[1], 16)
        except ValueError:
            log.warning("bad PRODUCT %r", product)
            return
        # skip hubs (USB class 09, incl. root hub): uevent socket reports
        # them too, but they're never attach candidates (noise records only)
        try:
            usb_class = int(ev.get("TYPE", "0/0/0").split("/", 1)[0], 16)
        except ValueError:
            usb_class = 0
        if usb_class == 9:
            return
        if action == "add":
            self.on_add(devpath, vid, pid)
        elif action == "remove":
            self.on_remove(devpath, vid, pid)
        # change / bind / unbind are deliberately ignored

    def on_add(self, devpath: str, vid: int, pid: int) -> None:
        rec = self.devices.setdefault(devpath, DeviceState(vid, pid))
        rec.vid = vid
        rec.pid = pid
        rec.present = True
        self.clear_timer(TK_DEBOUNCE + devpath)
        if not is_allowed(vid, pid):
            # a non-allowlisted device still cancels a pending debounce
            # above (same port = re-enumeration, not a real removal), but
            # is otherwise ignored: no settle timer, no attach
            return
        log.info("add %04x:%04x on %s (settle %.1fs)", vid, pid, devpath,
                 SETTLE_SEC)
        self.set_timer(TK_ATTACH + devpath, SETTLE_SEC,
                       lambda: self.attach_if_needed(devpath))

    def attach_if_needed(self, devpath: str) -> None:
        rec = self.devices.get(devpath)
        if not rec or not rec.present:
            return
        if not devpath_present(devpath):
            log.info("%s gone during settle, skipping attach", devpath)
            return
        snaps = vm_snapshots()
        decision, _ = self._pick_target(snaps, rec)
        if decision == "unknown":
            log.warning("VM state unknown, skipping attach of %s", devpath)
            return
        if decision == "none":
            log.info("no VM running, not attaching %s", devpath)
            return
        self._heal_attach(rec, devpath, snaps=snaps)

    def _entry_stale(self, xml_addr: BusDev | None, bus: int | None,
                     dev: int | None) -> bool:
        """Is a recorded hostdev address stale for the device's enumeration?

        Event path (bus=None): any existing entry is stale — the add event
        itself proves a fresh enumeration. Reconcile path (bus/dev known):
        stale only when the recorded address differs; a missing recorded
        address is conservatively treated as healthy.
        """
        if bus is None:
            return True
        if xml_addr is None:
            return False
        return xml_addr != (bus, dev)

    def _pick_target(self, snaps: list[VMSnapshot],
                     rec: DeviceState) -> tuple[str, str | None]:
        """(decision, target): which VM should own this device next.

        decision: "unknown" (some VM state unreadable → defer, never risk a
        double passthrough), "none" (no VM running), "attach" (target chosen).
        Selection rule: scan VMs in config order, the first running VM wins;
        once a VM is chosen it is sticky (rec.home) and never migrated while
        it keeps running. The order of USB_PT_VM is the passthrough priority.
        """
        if any(vm.running is None for vm in snaps):
            return "unknown", None
        running = [vm for vm in snaps if vm.running]
        if not running:
            return "none", None
        if rec.home and any(vm.name == rec.home for vm in running):
            return "attach", rec.home
        return "attach", running[0].name

    def _heal_attach(self, rec: DeviceState, devpath: str,
                     bus: int | None = None, dev: int | None = None,
                     snaps: list[VMSnapshot] | None = None) -> None:
        """Attach a physically present device to its owner VM, clearing any
        stale/duplicate hostdev entries first.

        Shared by the event path and the reconcile path (see _entry_stale
        for the two staleness rules). `snaps` is the caller's fresh
        vm_snapshots() pass-wide read — shared so the config is not
        re-fetched per device.

        Multi-VM invariant: one device lives in at most one VM. Stale or
        duplicate entries are swept from every running VM first; then the
        device is attached to its owner unless the owner already holds it
        healthy (static: home if still running, else first running VM).
        """
        if snaps is None:
            snaps = vm_snapshots()
        decision, target = self._pick_target(snaps, rec)
        if decision != "attach":
            log.info("not attaching %04x:%04x on %s (decision=%s)",
                     rec.vid, rec.pid, devpath, decision)
            return
        key = (rec.vid, rec.pid)
        target_snap = next(vm for vm in snaps if vm.name == target)
        if target_snap.attached is None:
            # can't compare against a config we can't read: skip, reconcile retries
            log.warning("cannot read config of VM %s, skipping attach of %s",
                        target, devpath)
            return

        # sweep the owner's stale entry + any duplicate entry in other
        # running VMs, so the device ends up in at most one VM
        cleared_stale = False
        for vm in snaps:
            if not vm.running:
                continue
            vm_map = vm.attached or {}
            if key not in vm_map:
                continue
            if vm.name == target:
                if not self._entry_stale(vm_map[key], bus, dev):
                    continue  # healthy home entry: keep it
                if bus is None:
                    log.info("stale hostdev %04x:%04x, detaching first",
                             rec.vid, rec.pid)
                else:
                    # entry resolved at an earlier enumeration; the guest
                    # already lost it — clear then re-attach fresh
                    log.info("reconcile: hostdev %04x:%04x resolved at %s but "
                             "device now at %s — stale entry, re-attaching",
                             rec.vid, rec.pid, vm_map[key], (bus, dev))
            else:
                log.info("duplicate hostdev %04x:%04x in %s, detaching",
                         rec.vid, rec.pid, vm.name)
            detach_device(rec.vid, rec.pid, vm.name)
            cleared_stale = True

        # owner already holds it healthy (nothing swept for it) -> no attach
        if key in target_snap.attached and not self._entry_stale(
                target_snap.attached[key], bus, dev):
            rec.home = target
            return

        rec.home = None
        if attach_device(rec.vid, rec.pid, target):
            rec.home = target
            # cancel a settle timer that may still be pending for this
            # enumeration; it would otherwise re-run attach_if_needed and
            # cause a needless detach+attach churn
            self.clear_timer(TK_ATTACH + devpath)
        elif cleared_stale:
            # same churn avoidance as above, also when the re-attach failed
            self.clear_timer(TK_ATTACH + devpath)

    def _detach_if_listed(self, vid: int, pid: int,
                          snaps: list[VMSnapshot]) -> None:
        """Detach a hostdev every running VM still lists (idempotent,
        tolerant). `snaps` is the caller's vm_snapshots() read."""
        for vm in snaps:
            if vm.running and vm.attached and (vid, pid) in vm.attached:
                detach_device(vid, pid, vm.name)

    def on_remove(self, devpath: str, vid: int, pid: int) -> None:
        rec = self.devices.get(devpath)
        if rec is None:
            # daemon may have started mid-life; heal immediately if the VM
            # config still holds this device (reconcile covers the rest)
            if is_allowed(vid, pid):
                log.info("remove of untracked allowed device %04x:%04x on %s",
                         vid, pid, devpath)
                self._detach_if_listed(vid, pid, vm_snapshots())
            return
        rec.present = False
        if not is_allowed(rec.vid, rec.pid):
            return
        log.info("remove %04x:%04x on %s (debounce %.1fs)",
                 rec.vid, rec.pid, devpath, REMOVE_DEBOUNCE_SEC)
        self.set_timer(TK_DEBOUNCE + devpath, REMOVE_DEBOUNCE_SEC,
                       lambda: self.maybe_detach(devpath))

    def maybe_detach(self, devpath: str) -> None:
        if devpath_present(devpath):
            # same port re-enumerated (BT<->2.4G switch, sleep/wake, flap):
            # no real removal, keep whatever state we have
            log.info("%s re-enumerated, skipping detach", devpath)
            return
        rec = self.devices.get(devpath)
        if not rec:
            return
        # detach from the known home (if any) plus every running VM that
        # still lists this device (catches stale/duplicate entries). An
        # unknown/unreadable VM's entries are left for the next reconcile.
        homes = set()
        if rec.home:
            homes.add(rec.home)
        for vm in vm_snapshots():
            if vm.running and vm.attached and (rec.vid, rec.pid) in vm.attached:
                homes.add(vm.name)
        for name in homes:
            detach_device(rec.vid, rec.pid, name)
        rec.home = None
        rec.present = False

    # ---- reconciliation -------------------------------------------------

    def reconcile(self) -> None:
        """Diff physical devices vs all VMs' config; heal attach/detach state.

        Covers: devices already present when a VM started, service restarts,
        host suspend/resume, missed events, zombie hostdevs. One device is
        kept in at most one running VM, owned by the first running VM in
        config order (static: an existing healthy attachment is not moved).
        """
        log.info("reconcile start")
        if pyudev is None:
            log.error("pyudev unavailable, reconcile aborted")
            return
        snaps = vm_snapshots()
        if any(vm.running is None for vm in snaps):
            log.warning("cannot determine state of all VMs, reconcile aborted")
            return
        running_vms = [vm for vm in snaps if vm.running]
        for vm in running_vms:
            if vm.attached is None:
                log.warning("cannot read config of VM %s, reconcile aborted",
                            vm.name)
                return
        physical = scan_physical_devices()

        for devpath, (vid, pid, _bus, _dev) in physical.items():
            rec = self.devices.setdefault(devpath, DeviceState(vid, pid))
            rec.vid = vid
            rec.pid = pid
            rec.present = True
            self.clear_timer(TK_DEBOUNCE + devpath)
        for devpath, rec in list(self.devices.items()):
            if devpath not in physical:
                rec.present = False
                # drop records for non-allowlisted devices (they never hold
                # timers or attach state); allowlisted records are kept so a
                # pending debounce timer can still find them
                if not is_allowed(rec.vid, rec.pid):
                    del self.devices[devpath]

        if not running_vms:
            for rec in self.devices.values():
                rec.home = None
            log.info("no VM running, nothing to do")
            return

        # attach physically-present allowed devices that aren't already
        # healthy in their owner (scan_physical_devices() already filters to
        # the allowlist); the pass-wide read is shared, no per-device refetch
        for devpath, (vid, pid, bus, dev) in physical.items():
            rec = self.devices[devpath]
            self._heal_attach(rec, devpath, bus, dev, snaps)

        # clean zombie entries across every running VM: allowed hostdev whose
        # physical device is not present anywhere
        present_ids = {(vid, pid) for (vid, pid, _b, _d) in physical.values()}
        for vm in running_vms:
            for (vid, pid) in vm.attached:
                if is_allowed(vid, pid) and (vid, pid) not in present_ids:
                    log.info("reconcile: stale hostdev %04x:%04x in %s, "
                             "detaching", vid, pid, vm.name)
                    detach_device(vid, pid, vm.name)
        log.info("reconcile done")

    # ---- main loop ------------------------------------------------------

    def run(self) -> None:
        log.info("starting USB passthrough daemon (VM=%s, allowed=%s)",
                 ",".join(VM_NAMES), ", ".join("%04x:%04x" % p for p in ALLOWED))
        if pyudev is None:
            log.error("python3-pyudev is required; install it "
                      "(e.g. sudo rpm-ostree install python3-pyudev) and "
                      "restart — exiting")
            return
        if libvirt is None:
            log.error("python3-libvirt is required; install it "
                      "(e.g. sudo rpm-ostree install python3-libvirt) and "
                      "restart — exiting")
            return
        try:
            monitor = pyudev.Monitor.from_netlink(pyudev.Context())
            monitor.filter_by(subsystem="usb", device_type="usb_device")
        except Exception as e:
            log.error("pyudev monitor init failed: %s", e)
            return
        log.info("event source: pyudev (libudev, kernel uevent socket)")
        # startup reconcile (immediately, as before) + schedule the periodic
        # pass; _reconcile_tick self-reschedules from then on
        self._reconcile_tick()
        fd = monitor.fileno()
        while True:
            try:
                # 0.5s timeout wakes the loop for timers even when no
                # uevent arrives
                ready, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if fd in ready:
                while True:
                    try:
                        dev = monitor.poll(timeout=0)
                    except Exception as e:
                        log.error("pyudev poll failed: %s", e)
                        dev = None
                    if dev is None:
                        break
                    props = dev.properties  # non-deprecated accessor
                    try:
                        self.handle_event({
                            "action": dev.action,
                            "DEVTYPE": props.get("DEVTYPE", ""),
                            "devpath": dev.device_path,
                            "PRODUCT": props.get("PRODUCT", ""),
                            "TYPE": props.get("TYPE", ""),
                        })
                    except Exception:
                        log.exception("event handling failed")
            self.fire_timers()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if "--debug" in sys.argv:
        logging.getLogger().setLevel(logging.DEBUG)
    missing = []
    if not VM_NAMES:
        missing.append("USB_PT_VM")
    if not ALLOWED:
        missing.append("USB_PT_ALLOWED")
    if missing:
        # covers both the daemon loop and --reconcile-once
        log.error("refusing to start: required environment variables not set: %s "
                  "(set them in the systemd unit's Environment= or the shell)",
                  ", ".join(missing))
        return 1
    daemon = Daemon()
    if "--reconcile-once" in sys.argv:
        daemon.reconcile()
        return 0
    # SIGHUP: push a zero-delay reconcile timer — set_timer's same-key
    # dedupe makes it replace the scheduled pass, so the reconcile runs on
    # the next loop iteration and the cycle continues from there
    signal.signal(signal.SIGHUP,
                  lambda s, f: daemon.set_timer(TK_RECONCILE, 0.0,
                                                daemon._reconcile_tick))
    try:
        daemon.run()
    except KeyboardInterrupt:
        log.info("interrupted, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())