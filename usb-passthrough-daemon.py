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
subprocess, no fallback. Only kernel-level properties are used
(PRODUCT / TYPE / DEVTYPE / DEVPATH), so the kernel socket is sufficient.
Requires python3-pyudev; the daemon refuses to start without it.

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
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

# pyudev is the ONLY event source (no fallback). Guarded import: tests can
# load this module without pyudev; the daemon refuses to start without it.
try:
    import pyudev  # noqa: F401
except ImportError:
    pyudev = None

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

# Required: the target VM name and the allowlist come exclusively from the
# environment. The daemon refuses to start without them (see main()).
VM_NAME = os.environ.get("USB_PT_VM")


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

VIRSH = ["virsh"]


def is_allowed(vid: int, pid: int) -> bool:
    return (vid, pid) in ALLOWED


# ---------------------------------------------------------------------------
# libvirt helpers
# ---------------------------------------------------------------------------

def _sh(cmd: list[str], timeout: float = 15,
        **kw: object) -> subprocess.CompletedProcess | None:
    # C locale so virsh state names / error strings are stable English
    kw.setdefault("env", {**os.environ, "LC_ALL": "C"})
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, **kw)
    except FileNotFoundError:
        log.error("command not found: %s", cmd[0])
        return None
    except subprocess.TimeoutExpired:
        log.error("command timed out: %s", " ".join(cmd))
        return None


def vm_running() -> bool | None:
    """Return True/False, or None when the state cannot be determined.

    Callers must never treat None as "not running": on an unknown state we
    skip the action and let the periodic reconcile retry later.
    """
    r = _sh(VIRSH + ["domstate", VM_NAME])
    if r is None or r.returncode != 0:
        return None
    state = r.stdout.strip()
    if state != "running":
        log.info("VM %s state is %r (not running)", VM_NAME, state)
    return state == "running"


def _xml_tempfile(xml: str) -> str:
    """Write hostdev XML to a temp file (virsh needs a real path, not '-')."""
    fd, path = tempfile.mkstemp(prefix="usb-pt-", suffix=".xml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(xml)
    except Exception:
        os.unlink(path)
        raise
    return path


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


def vm_attached_devices() -> AttachedMap | None:
    """Return {(vid,pid): (host bus, host device) or None} for USB hostdevs
    in the VM's live config; None on error.

    The address is libvirt's resolution of the device at attach time.
    Comparing it with the device's current bus/device numbers reveals
    re-enumerations: the VM entry is stale and the guest already lost
    the device.
    """
    r = _sh(VIRSH + ["dumpxml", VM_NAME])
    if r is None or r.returncode != 0:
        return None
    found: AttachedMap = {}
    for block in re.findall(r"<hostdev\b.*?</hostdev>", r.stdout, re.S):
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


def attach_device(vid: int, pid: int) -> bool:
    """Live-attach; retries internally. Returns True on success."""
    xml = hostdev_xml(vid, pid)
    path = _xml_tempfile(xml)
    try:
        for attempt in range(1, ATTACH_RETRIES + 1):
            r = _sh(VIRSH + ["attach-device", VM_NAME, path, "--live"])
            if r is not None and r.returncode == 0:
                log.info("attached %04x:%04x to %s", vid, pid, VM_NAME)
                return True
            log.warning("attach %04x:%04x attempt %d/%d failed: %s",
                        vid, pid, attempt, ATTACH_RETRIES,
                        (r.stderr.strip() if r else "virsh unavailable"))
            if attempt < ATTACH_RETRIES:
                time.sleep(ATTACH_RETRY_GAP)
        return False
    finally:
        os.unlink(path)


def detach_device(vid: int, pid: int) -> bool:
    """Live-detach; tolerant when the device / entry is already gone."""
    xml = hostdev_xml(vid, pid)
    path = _xml_tempfile(xml)
    try:
        r = _sh(VIRSH + ["detach-device", VM_NAME, path, "--live"])
    finally:
        os.unlink(path)
    if r is None:
        return False
    if r.returncode == 0:
        log.info("detached %04x:%04x from %s", vid, pid, VM_NAME)
        return True
    log.info("detach %04x:%04x tolerated: %s", vid, pid,
             r.stderr.strip() or "rc=%d" % r.returncode)
    return False


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
    attached: bool = False


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
        running = vm_running()
        if running is None:
            log.warning("VM state unknown, skipping attach of %s", devpath)
            return
        if not running:
            log.info("VM %s not running, not attaching %s", VM_NAME, devpath)
            return
        self._heal_attach(rec, devpath)

    def _heal_attach(self, rec: DeviceState, devpath: str,
                     bus: int | None = None, dev: int | None = None) -> None:
        """Attach a physically present device, clearing a stale hostdev first.

        Shared by the event path (bus/dev unknown: any entry already in the
        VM config counts as stale — the add event itself proves a fresh
        enumeration) and the reconcile path (bus/dev known: stale only when
        the entry's recorded address no longer matches the device).
        """
        attached = vm_attached_devices()
        if attached is None:
            log.warning("cannot read VM config, skipping attach of %s", devpath)
            return
        key = (rec.vid, rec.pid)
        if key in attached:
            xml_addr = attached[key]
            if bus is None:
                stale = True
            elif xml_addr is None:
                # recorded address absent: conservatively treat as healthy
                stale = False
            else:
                stale = xml_addr != (bus, dev)
            if not stale:
                rec.attached = True
                return
            if bus is None:
                log.info("stale hostdev %04x:%04x, detaching first",
                         rec.vid, rec.pid)
            else:
                # the VM entry was resolved at an earlier enumeration and
                # the guest already lost the device — clear the stale
                # entry, then attach fresh
                log.info("reconcile: hostdev %04x:%04x resolved at %s but "
                         "device now at %s — stale entry, re-attaching",
                         rec.vid, rec.pid, xml_addr, (bus, dev))
            detach_device(rec.vid, rec.pid)
            cleared_stale = True
        else:
            if bus is not None:
                log.info("reconcile: attaching %04x:%04x (%s)",
                         rec.vid, rec.pid, devpath)
            cleared_stale = False
        rec.attached = False
        if attach_device(rec.vid, rec.pid):
            rec.attached = True
            # cancel a settle timer that may still be pending for this
            # enumeration; it would otherwise re-run attach_if_needed and
            # cause a needless detach+attach churn
            self.clear_timer(TK_ATTACH + devpath)
        elif cleared_stale:
            # same churn avoidance as above, also when the re-attach failed
            self.clear_timer(TK_ATTACH + devpath)

    def _detach_if_listed(self, vid: int, pid: int) -> None:
        """Detach a hostdev the VM config still lists (idempotent, tolerant)."""
        attached = vm_attached_devices()
        if attached and (vid, pid) in attached:
            detach_device(vid, pid)

    def on_remove(self, devpath: str, vid: int, pid: int) -> None:
        rec = self.devices.get(devpath)
        if rec is None:
            # daemon may have started mid-life; heal immediately if the VM
            # config still holds this device (reconcile covers the rest)
            if is_allowed(vid, pid):
                log.info("remove of untracked allowed device %04x:%04x on %s",
                         vid, pid, devpath)
                running = vm_running()
                if running:
                    self._detach_if_listed(vid, pid)
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
        running = vm_running()
        if running is None:
            log.info("VM state unknown, leaving %s for reconcile", devpath)
            rec.present = False
            return
        if running:
            if rec.attached:
                detach_device(rec.vid, rec.pid)
                rec.attached = False
            else:
                self._detach_if_listed(rec.vid, rec.pid)
        rec.present = False

    # ---- reconciliation -------------------------------------------------

    def reconcile(self) -> None:
        """Diff physical devices vs VM config; heal attach/detach state.

        Covers: devices already present when the VM started, service
        restarts, host suspend/resume, missed events, zombie hostdevs.
        """
        log.info("reconcile start")
        if pyudev is None:
            log.error("pyudev unavailable, reconcile aborted")
            return
        running = vm_running()
        if running is None:
            log.warning("cannot determine VM state, reconcile aborted")
            return
        physical = scan_physical_devices()
        attached = vm_attached_devices() if running else {}
        if attached is None:
            log.warning("cannot read VM config, reconcile aborted")
            return

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

        if not running:
            for rec in self.devices.values():
                rec.attached = False
            log.info("VM not running, nothing to do")
            return

        # attach physically-present allowed devices that aren't in the VM
        # (scan_physical_devices() already filters to the allowlist)
        for devpath, (vid, pid, bus, dev) in physical.items():
            rec = self.devices[devpath]
            self._heal_attach(rec, devpath, bus, dev)

        # clean zombie entries: allowed hostdev in the VM config whose
        # physical device is not present anywhere
        present_ids = {(vid, pid) for (vid, pid, _b, _d) in physical.values()}
        for (vid, pid) in attached:
            if is_allowed(vid, pid) and (vid, pid) not in present_ids:
                log.info("reconcile: stale hostdev %04x:%04x, detaching",
                         vid, pid)
                detach_device(vid, pid)
        log.info("reconcile done")

    # ---- main loop ------------------------------------------------------

    def run(self) -> None:
        log.info("starting USB passthrough daemon (VM=%s, allowed=%s)",
                 VM_NAME, ", ".join("%04x:%04x" % p for p in ALLOWED))
        if pyudev is None:
            log.error("python3-pyudev is required; install it "
                      "(e.g. sudo rpm-ostree install python3-pyudev) and "
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
    if not VM_NAME:
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