#!/usr/bin/env python3
"""Replay captured USB hotplug events through the passthrough state machine.

Self-contained: the 33 events below were captured with
`udevadm monitor --property --udev --subsystem-match=usb/usb_device` on the
real hardware (Keychron K6 keyboard, Razer Basilisk X mouse, 8BitDo Ultimate
gamepad) and embedded here, so the test needs no external files.

Uses mocked virsh/sysfs, so nothing real is touched. Validates:
  * allowed devices get attached on add (settle)
  * real removals (no quick re-add) get detached (debounce)
  * re-enumerations (8BitDo IDLE<->active, sleep/wake, power cycle) are
    NOT mistaken for removals (no thrash detach)
  * 8BitDo IDLE mode (2dc8:3109) is never attached

Note: the Razer mouse's BT<->2.4G switch emits NO udev events (the dongle
stays enumerated), so the 7202/7229 remove+add below is a real
removal/insertion (unplug or power cycle), not a mode switch.

Usage: python3 test_replay.py
"""

import importlib.util
import os
import sys

# Pin configuration BEFORE loading the daemon module: the daemon reads these
# from the environment at import time, and a shell that exports e.g.
# USB_PT_ALLOWED (for the systemd unit) would otherwise change test behavior.
os.environ["USB_PT_ALLOWED"] = "05ac:024f,1532:0083,2dc8:3106"
os.environ["USB_PT_IDLE"] = "2dc8:3109"
os.environ["USB_PT_SETTLE"] = "1.0"
os.environ["USB_PT_DEBOUNCE"] = "1.0"

spec = importlib.util.spec_from_file_location(
    "usb_pt", "usb-passthrough-daemon.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

# --- fake clock -----------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

fake = FakeClock()
d.time.monotonic = fake.monotonic
d.time.sleep = lambda s: None  # never really sleep

# --- mocks ----------------------------------------------------------------

attached_log = []   # [(vid, pid), ...] in order
detached_log = []   # [(vid, pid), ...] in order
fake_sysfs = {}     # devpath -> present?

d.vm_running = lambda: True
d.vm_attached_devices = lambda: set()
d.attach_device = lambda vid, pid: attached_log.append((vid, pid)) or True
d.detach_device = lambda vid, pid: detached_log.append((vid, pid)) or True


def fake_present(devpath):
    return fake_sysfs.get(devpath, False)


d.devpath_present = fake_present

# --- embedded real events ------------------------------------------------
# (timestamp, action, devpath, PRODUCT=vid/pid/rev)
# key 5-2.1.2 = Razer mouse, 5-2.1.3 = 8BitDo gamepad, 5-2.1.4 = K6 keyboard

EVENTS = [
    (5782.240824, "add", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.4", "5ac/24f/108"),
    (5782.243166, "change", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.4", "5ac/24f/108"),
    (5782.309818, "bind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.4", "5ac/24f/108"),
    (5788.579430, "unbind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.4", "5ac/24f/108"),
    (5788.581494, "remove", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.4", "5ac/24f/108"),
    (5808.300588, "add", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.4", "5ac/24f/108"),
    (5808.303268, "change", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.4", "5ac/24f/108"),
    (5808.362577, "bind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.4", "5ac/24f/108"),
    (6055.202740, "unbind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3106/114"),
    (6055.203968, "remove", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3106/114"),
    (6055.694356, "add", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3109/200"),
    (6055.715780, "bind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3109/200"),
    (6085.409720, "unbind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3109/200"),
    (6085.410932, "remove", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3109/200"),
    (6085.829776, "add", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3106/114"),
    (6085.881377, "bind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3106/114"),
    (6097.441480, "unbind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3106/114"),
    (6097.442829, "remove", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3106/114"),
    (6098.349731, "add", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3109/200"),
    (6098.370519, "bind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3109/200"),
    (6129.185493, "unbind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3109/200"),
    (6129.186760, "remove", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3109/200"),
    (6129.829191, "add", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3106/114"),
    (6129.875350, "bind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3106/114"),
    (6139.425302, "unbind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3106/114"),
    (6139.426567, "remove", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3106/114"),
    (6140.333062, "add", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3109/200"),
    (6140.353342, "bind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3", "2dc8/3109/200"),
    (7202.588124, "unbind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.2", "1532/83/200"),
    (7202.589344, "remove", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.2", "1532/83/200"),
    (7229.999828, "add", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.2", "1532/83/200"),
    (7230.002811, "change", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.2", "1532/83/200"),
    (7230.106220, "bind", "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.2", "1532/83/200"),
]

# --- replay ---------------------------------------------------------------

daemon = d.Daemon()
daemon.reconcile_due = float("inf")  # reconcile is exercised separately

# Simulate a startup reconcile: these devices were already present AND
# attached (VM running) before the captured log began. Without this seed
# their first event would be an "untracked remove". The Razer's 7202
# remove is a REAL removal (unplug/power cycle) — its BT<->2.4G switch
# produces no udev events at all.
SEED = {
    "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.2": (0x1532, 0x0083),  # Razer mouse
    "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3": (0x2DC8, 0x3106),  # 8BitDo gamepad
}
for devpath, (vid, pid) in SEED.items():
    daemon.devices[devpath] = {"vid": vid, "pid": pid,
                               "present": True, "attached": True}
    fake_sysfs[devpath] = True

for ts, action, devpath, product in EVENTS:
    fake.t = ts
    daemon.fire_timers()  # pending timers first, with port state as of now
    if action == "add":
        fake_sysfs[devpath] = True
    elif action == "remove":
        fake_sysfs[devpath] = False
    v = product.split("/")
    vid, pid = int(v[0], 16), int(v[1], 16)
    daemon.handle_event({"action": action, "DEVTYPE": "usb_device",
                         "devpath": devpath, "PRODUCT": product})

fake.t = EVENTS[-1][0] + 5.0  # flush remaining timers
daemon.fire_timers()

# --- assertions -----------------------------------------------------------

def contains(l, vid, pid):
    return any(x == (vid, pid) for x in l)


checks = [
    ("keyboard attached",
     contains(attached_log, 0x05AC, 0x024F),
     "Keychron K6 (05ac:024f) should be attached on add"),
    ("mouse attached",
     contains(attached_log, 0x1532, 0x0083),
     "Razer Basilisk X (1532:0083) should be attached on re-add"),
    ("gamepad attached",
     contains(attached_log, 0x2DC8, 0x3106),
     "8BitDo active mode (2dc8:3106) should be attached"),
    ("IDLE never attached",
     not contains(attached_log, 0x2DC8, 0x3109),
     "8BitDo IDLE (2dc8:3109) must never be attached"),
    ("keyboard detached on real unplug",
     contains(detached_log, 0x05AC, 0x024F),
     "keyboard removal with no quick re-add should detach"),
    ("mouse detached on real removal",
     contains(detached_log, 0x1532, 0x0083),
     "Razer removal (unplug/power-cycle, no quick re-add) should detach"),
    ("gamepad not thrash-detached",
     not contains(detached_log, 0x2DC8, 0x3106),
     "8BitDo mode switches must NOT cause detach"),
]

failed = False
print(f"replayed {len(EVENTS)} embedded events (no files needed)")
for name, ok, why in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {why}")
    failed = failed or not ok

# --- stale-entry recovery check (the core virt-manager gap) ---------------
# The VM's live config can still list a hostdev whose device physically
# re-enumerated (libvirt never auto-recovers). On re-add the daemon must
# clear the stale entry FIRST, then attach fresh. The main replay mocks
# vm_attached_devices as empty, so this path is exercised explicitly here.
STALE_DP = "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3"
saved_mocks = (d.vm_attached_devices, d.attach_device, d.detach_device)
try:
    d.vm_attached_devices = lambda: {(0x2DC8, 0x3106)}  # stale entry in VM config
    stale_detached = []
    stale_attached = []
    d.detach_device = lambda vid, pid: stale_detached.append((vid, pid)) or True
    d.attach_device = lambda vid, pid: stale_attached.append((vid, pid)) or True

    s = d.Daemon()
    fake_sysfs.clear()
    fake_sysfs[STALE_DP] = True
    fake.t = 20000.0
    s.handle_event({"action": "add", "DEVTYPE": "usb_device",
                    "devpath": STALE_DP, "PRODUCT": "2dc8/3106/114"})
    fake.t += 2.0  # past the settle window
    s.fire_timers()
    ok = (stale_detached == [(0x2DC8, 0x3106)] and
          stale_attached == [(0x2DC8, 0x3106)])
    print(f"{'PASS' if ok else 'FAIL'}  stale-entry recovery: "
          f"detach-then-attach on re-add "
          f"(detached={stale_detached}, attached={stale_attached})")
    failed = failed or not ok
finally:
    d.vm_attached_devices, d.attach_device, d.detach_device = saved_mocks

# --- reconcile stale-address recovery check -------------------------------
# The VM's live config records the host bus/device libvirt resolved at attach
# time. If the device re-enumerated since (DEVNUM changed), that recorded
# address no longer matches the physical device — reconcile must detach the
# stale entry and attach fresh. This covers daemon late start and events
# missed while the daemon was down (the event path normally handles it via
# add). A matching address must NOT cause churn.
REC_DP = "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3"
saved_rec = (d.pyudev, d.vm_running, d.vm_attached_devices,
             d.scan_physical_devices, d.attach_device, d.detach_device)
try:
    d.pyudev = object()  # satisfy reconcile()'s pyudev-required guard
    d.vm_running = lambda: True
    d.vm_attached_devices = lambda: {(0x2DC8, 0x3106): (5, 47)}  # resolved at old enumeration
    d.scan_physical_devices = lambda: {REC_DP: (0x2DC8, 0x3106, 5, 48)}  # re-enumerated
    rec_detached = []
    rec_attached = []
    d.detach_device = lambda vid, pid: rec_detached.append((vid, pid)) or True
    d.attach_device = lambda vid, pid: rec_attached.append((vid, pid)) or True

    r = d.Daemon()
    r.reconcile()
    ok = (rec_detached == [(0x2DC8, 0x3106)] and
          rec_attached == [(0x2DC8, 0x3106)])
    print(f"{'PASS' if ok else 'FAIL'}  reconcile stale-address: detach-then-attach "
          f"on mismatch (detached={rec_detached}, attached={rec_attached})")
    failed = failed or not ok

    # healthy case: recorded address matches the current device -> no churn
    d.vm_attached_devices = lambda: {(0x2DC8, 0x3106): (5, 48)}
    rec_detached.clear()
    rec_attached.clear()
    r2 = d.Daemon()
    r2.reconcile()
    ok = not rec_detached and not rec_attached
    print(f"{'PASS' if ok else 'FAIL'}  reconcile matching address: no detach/attach churn")
    failed = failed or not ok

    # dumpxml failure -> reconcile aborts cleanly instead of crashing
    d.vm_attached_devices = lambda: None
    d.scan_physical_devices = lambda: {REC_DP: (0x2DC8, 0x3106, 5, 48)}
    rec_detached.clear()
    rec_attached.clear()
    r3 = d.Daemon()
    r3.reconcile()
    ok = not rec_detached and not rec_attached
    print(f"{'PASS' if ok else 'FAIL'}  reconcile unreadable VM config: aborts cleanly")
    failed = failed or not ok
finally:
    (d.pyudev, d.vm_running, d.vm_attached_devices, d.scan_physical_devices,
     d.attach_device, d.detach_device) = saved_rec

print("attached:", [f"{v:04x}:{p:04x}" for v, p in attached_log])
print("detached:", [f"{v:04x}:{p:04x}" for v, p in detached_log])
sys.exit(1 if failed else 0)
