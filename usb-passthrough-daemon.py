#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""usb-passthrough-daemon.py — hot-plug USB input devices into a Windows VM.

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

Design decisions (rationale in README.md):
    * identity  = PRODUCT=vid/pid/rev (present on both add AND remove events)
                  + DEVPATH (stable port path; DEVNUM changes on every
                  re-enumeration, e.g. 8BitDo 3106<->3109, Razer 004->053)
    * only ACTION=add / ACTION=remove are acted upon
      (insertion fires add/change/bind, removal fires unbind/remove)
    * settle delay before attach (device still enumerating interfaces)
    * debounce before detach (some wireless devices re-enumerate on
      sleep/wake or mode switches — e.g. 8BitDo IDLE<->active; a reappearing
      port means no real removal). The Razer mouse's BT<->2.4G switch emits
      NO udev events, so a switching mouse never reaches this path.
    * 8BitDo receivers in "IDLE" mode (2dc8:3109) are deliberately ignored
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

# pyudev is the ONLY event source (no fallback). The import is guarded only
# so the module can be imported for testing on machines without pyudev; the
# daemon itself refuses to start when pyudev is missing.
try:
    import pyudev  # noqa: F401
except ImportError:
    pyudev = None

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment)
# ---------------------------------------------------------------------------

VM_NAME = os.environ.get("USB_PT_VM", "windows")


def _parse_pairs(s):
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v, p = part.split(":", 1)
            out.append((int(v, 16), int(p, 16)))
        except ValueError:
            logging.getLogger("usb-pt").warning("ignoring bad pair: %r", part)
    return out


# Devices to pass through: comma-separated "vid:pid" hex pairs.
ALLOWED = _parse_pairs(os.environ.get(
    "USB_PT_ALLOWED", "05ac:024f,1532:0083,2dc8:3106"))

# "Idle" states of the same hardware that must never be attached (log only).
KNOWN_IDLE = _parse_pairs(os.environ.get("USB_PT_IDLE", "2dc8:3109"))

SETTLE_SEC = float(os.environ.get("USB_PT_SETTLE", "1.0"))
REMOVE_DEBOUNCE_SEC = float(os.environ.get("USB_PT_DEBOUNCE", "1.0"))
RECONCILE_SEC = float(os.environ.get("USB_PT_RECONCILE", "30"))
ATTACH_RETRIES = int(os.environ.get("USB_PT_ATTACH_RETRIES", "3"))
ATTACH_RETRY_GAP = float(os.environ.get("USB_PT_ATTACH_RETRY_GAP", "1.5"))

VIRSH = ["virsh"]

log = logging.getLogger("usb-pt")


def is_allowed(vid, pid):
    return (vid, pid) in ALLOWED


def is_idle(vid, pid):
    return (vid, pid) in KNOWN_IDLE


# ---------------------------------------------------------------------------
# libvirt helpers
# ---------------------------------------------------------------------------

def _sh(cmd, timeout=15, **kw):
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


def vm_running():
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


def _xml_tempfile(xml):
    """Write hostdev XML to a temp file (virsh needs a real path, not '-')."""
    fd, path = tempfile.mkstemp(prefix="usb-pt-", suffix=".xml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(xml)
    except Exception:
        os.unlink(path)
        raise
    return path


def hostdev_xml(vid, pid):
    return (
        "<hostdev mode='subsystem' type='usb' managed='yes'>\n"
        "  <source>\n"
        f"    <vendor id='0x{vid:04x}'/>\n"
        f"    <product id='0x{pid:04x}'/>\n"
        "  </source>\n"
        "</hostdev>\n"
    )


def vm_attached_devices():
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
    found = {}
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


def attach_device(vid, pid):
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


def detach_device(vid, pid):
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

def devpath_present(devpath):
    """Whether the device port currently exists on the bus."""
    return os.path.isdir("/sys" + devpath)


def scan_physical_devices():
    """Return {devpath: (vid, pid, bus, device)} for allowlisted devices on
    the bus now. bus/device are the current host USB numbers (None if
    unreadable); they are compared against the VM entry's recorded address
    to detect stale hostdevs after re-enumeration."""
    out = {}
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
            devn = int(dev.attributes.get("devnum").decode())
        except (AttributeError, ValueError):
            bus = devn = None
        out[dev.device_path] = (vid, pid, bus, devn)
    return out


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------

class Daemon:
    def __init__(self):
        self.devices = {}          # devpath -> record {vid,pid,present,attached}
        self.timers = []           # (due, key, callable)
        self.reconcile_due = float("inf")

    # ---- timers ---------------------------------------------------------

    def set_timer(self, key, delay, func):
        self.clear_timer(key)
        self.timers.append((time.monotonic() + delay, key, func))

    def clear_timer(self, key):
        self.timers = [t for t in self.timers if t[1] != key]

    def fire_timers(self):
        now = time.monotonic()
        due = [t for t in self.timers if t[0] <= now]
        self.timers = [t for t in self.timers if t[0] > now]
        for _, _, func in due:
            try:
                func()
            except Exception:
                log.exception("timer callback failed")

    # ---- udev events ----------------------------------------------------

    def handle_event(self, ev):
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
        # skip hubs (USB class 09)
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

    def on_add(self, devpath, vid, pid):
        rec = self.devices.setdefault(devpath, {
            "vid": vid, "pid": pid, "present": False, "attached": False})
        rec.update(vid=vid, pid=pid, present=True)
        self.clear_timer("debounce-remove:" + devpath)
        if is_idle(vid, pid):
            log.info("idle-mode device %04x:%04x on %s ignored", vid, pid, devpath)
            return
        if not is_allowed(vid, pid):
            return
        log.info("add %04x:%04x on %s (settle %.1fs)", vid, pid, devpath,
                 SETTLE_SEC)
        self.set_timer("attach:" + devpath, SETTLE_SEC,
                       lambda: self.attach_if_needed(devpath))

    def attach_if_needed(self, devpath):
        rec = self.devices.get(devpath)
        if not rec or not rec.get("present"):
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
        vid, pid = rec["vid"], rec["pid"]
        attached = vm_attached_devices()
        if attached is None:
            log.warning("cannot read VM config, skipping attach of %s", devpath)
            return
        if (vid, pid) in attached:
            # stale hostdev entry from an earlier enumeration: clear it, then
            # attach fresh (the guest already lost the device on the physical
            # remove, and the XML entry alone won't bring it back)
            log.info("stale hostdev %04x:%04x, detaching first", vid, pid)
            detach_device(vid, pid)
        if attach_device(vid, pid):
            rec["attached"] = True

    def on_remove(self, devpath, vid, pid):
        rec = self.devices.get(devpath)
        if rec is None:
            # daemon may have started mid-life; heal immediately if the VM
            # config still holds this device (reconcile covers the rest)
            if is_allowed(vid, pid):
                log.info("remove of untracked allowed device %04x:%04x on %s",
                         vid, pid, devpath)
                running = vm_running()
                if running:
                    attached = vm_attached_devices()
                    if attached and (vid, pid) in attached:
                        detach_device(vid, pid)
            return
        rec["present"] = False
        if not is_allowed(rec["vid"], rec["pid"]):
            return
        log.info("remove %04x:%04x on %s (debounce %.1fs)",
                 rec["vid"], rec["pid"], devpath, REMOVE_DEBOUNCE_SEC)
        self.set_timer("debounce-remove:" + devpath, REMOVE_DEBOUNCE_SEC,
                       lambda: self.maybe_detach(devpath))

    def maybe_detach(self, devpath):
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
            rec["present"] = False
            return
        if running:
            if rec.get("attached"):
                detach_device(rec["vid"], rec["pid"])
                rec["attached"] = False
            else:
                attached = vm_attached_devices()
                if attached and (rec["vid"], rec["pid"]) in attached:
                    detach_device(rec["vid"], rec["pid"])
        rec["present"] = False

    # ---- reconciliation -------------------------------------------------

    def reconcile(self):
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
            rec = self.devices.setdefault(devpath, {
                "vid": vid, "pid": pid, "present": False, "attached": False})
            rec.update(vid=vid, pid=pid, present=True)
            self.clear_timer("debounce-remove:" + devpath)
        for devpath, rec in list(self.devices.items()):
            if devpath not in physical:
                rec["present"] = False
                # drop records for non-allowlisted devices (they never hold
                # timers or attach state); allowlisted records are kept so a
                # pending debounce timer can still find them
                if not is_allowed(rec["vid"], rec["pid"]):
                    del self.devices[devpath]

        if not running:
            for rec in self.devices.values():
                rec["attached"] = False
            log.info("VM not running, nothing to do")
            return

        # attach physically-present allowed devices that aren't in the VM
        for devpath, (vid, pid, bus, dev) in physical.items():
            if is_idle(vid, pid):
                continue
            rec = self.devices[devpath]
            if (vid, pid) in attached:
                rec["attached"] = True
                xml_addr = attached[(vid, pid)]
                if (xml_addr is not None and bus is not None
                        and xml_addr != (bus, dev)):
                    # the VM entry was resolved at an earlier enumeration and
                    # the guest already lost the device — clear the stale
                    # entry, then attach fresh (covers daemon late start and
                    # events missed during daemon downtime)
                    log.info("reconcile: hostdev %04x:%04x resolved at %s but "
                             "device now at %s — stale entry, re-attaching",
                             vid, pid, xml_addr, (bus, dev))
                    detach_device(vid, pid)
                    rec["attached"] = False
                    if attach_device(vid, pid):
                        rec["attached"] = True
                    # a settle timer may still be pending for this
                    # enumeration; it would otherwise re-run attach_if_needed
                    # and cause a needless detach+attach churn
                    self.clear_timer("attach:" + devpath)
                continue
            log.info("reconcile: attaching %04x:%04x (%s)", vid, pid, devpath)
            if attach_device(vid, pid):
                rec["attached"] = True
                # a settle timer may still be pending for this enumeration; it
                # would only re-run attach_if_needed and cause a needless
                # detach+attach churn — cancel it
                self.clear_timer("attach:" + devpath)

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

    def run(self):
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
        self.reconcile()
        self.reconcile_due = time.monotonic() + RECONCILE_SEC
        fd = monitor.fileno()
        while True:
            try:
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
            now = time.monotonic()
            if now >= self.reconcile_due:
                try:
                    self.reconcile()
                except Exception:
                    log.exception("reconcile failed")
                self.reconcile_due = now + RECONCILE_SEC
            self.fire_timers()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if "--debug" in sys.argv:
        logging.getLogger().setLevel(logging.DEBUG)
    daemon = Daemon()
    if "--reconcile-once" in sys.argv:
        daemon.reconcile()
        return 0
    signal.signal(signal.SIGHUP,
                  lambda s, f: setattr(daemon, "reconcile_due", time.monotonic()))
    try:
        daemon.run()
    except KeyboardInterrupt:
        log.info("interrupted, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
