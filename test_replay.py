#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Replay captured USB hotplug events through the passthrough state machine.

Self-contained: the 33 events below were captured with
`udevadm monitor --property --udev --subsystem-match=usb/usb_device` on the
real hardware (Keychron K6 keyboard, Razer Basilisk X mouse, 8BitDo Ultimate
gamepad) and embedded here, so the test needs no external files.

Uses mocked libvirt/sysfs, so nothing real is touched.

Structure: replay_main_flow() replays the embedded event stream through the
state machine; a set of scenario_*() functions exercises targeted paths
(stale entries, reconcile edge cases, event filtering, attach failures,
timer semantics, env parsing). report() aggregates and exits non-zero on
any failure.

Note: the Razer mouse's BT<->2.4G switch emits NO udev events (the dongle
stays enumerated), so the 7202/7229 remove+add below is a real
removal/insertion (unplug or power cycle), not a mode switch.

Usage: python3 test_replay.py
"""

import importlib.util
import os
import subprocess
import sys

# Pin configuration BEFORE loading the daemon module: it reads these from
# the environment at import time, so any USB_PT_* exported in the shell
# (e.g. for the systemd unit) would otherwise change test behavior.
os.environ["USB_PT_VM"] = "testvm"
os.environ["USB_PT_ALLOWED"] = "05ac:024f,1532:0083,2dc8:3106"
os.environ["USB_PT_SETTLE"] = "1"
os.environ["USB_PT_DEBOUNCE"] = "1"

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

# --- base mocks (scenarios patch and restore via save/restore_mocks) ------

attached_log = []   # [(vid, pid), ...] in order
detached_log = []   # [(vid, pid), ...] in order
fake_sysfs = {}     # devpath -> present?

d.vm_snapshot = lambda name: (True, {})
d.attach_device = lambda vid, pid, name: attached_log.append((vid, pid)) or True
d.detach_device = lambda vid, pid, name: detached_log.append((vid, pid)) or True


def fake_present(devpath):
    return fake_sysfs.get(devpath, False)


d.devpath_present = fake_present


def save_mocks():
    return (d.vm_snapshot, d.vm_snapshots, d.VM_NAMES, d.attach_device,
            d.detach_device, d.scan_physical_devices, d.pyudev)


def restore_mocks(saved):
    (d.vm_snapshot, d.vm_snapshots, d.VM_NAMES, d.attach_device,
     d.detach_device, d.scan_physical_devices, d.pyudev) = saved


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

# Simulate a startup reconcile: these devices were already present AND
# attached (VM running) before the captured log began. Without this seed
# their first event would be an "untracked remove". The Razer's 7202
# remove is a REAL removal (unplug/power cycle) — its BT<->2.4G switch
# produces no udev events at all.
SEED = {
    "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.2": (0x1532, 0x0083),  # Razer mouse
    "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3": (0x2DC8, 0x3106),  # 8BitDo gamepad
}


def contains(l, vid, pid):
    return any(x == (vid, pid) for x in l)


def replay_main_flow():
    """Replay the embedded event stream; assert the main-path behaviors."""
    results = []
    daemon = d.Daemon()
    for devpath, (vid, pid) in SEED.items():
        daemon.devices[devpath] = d.DeviceState(vid, pid,
                                                present=True, home="testvm")
        fake_sysfs[devpath] = True

    for ts, action, devpath, product in EVENTS:
        fake.t = ts
        # pending timers first, with port state as of now; then update fake
        # sysfs, then the event — the order must not be swapped (settle /
        # debounce decisions depend on timers firing before the event)
        daemon.fire_timers()
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

    print(f"replayed {len(EVENTS)} embedded events (no files needed)")
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
         "8BitDo IDLE (2dc8:3109) is not allowlisted and must never be attached"),
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
    results.extend(checks)
    return results


def scenario_stale_entry_recovery():
    """The VM config still lists a hostdev whose device re-enumerated
    (libvirt never auto-recovers). On re-add the daemon must clear the stale
    entry FIRST, then attach fresh. The main replay mocks vm_snapshot
    with an empty map, so this path is exercised explicitly here."""
    STALE_DP = "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3"
    results = []
    saved = save_mocks()
    try:
        d.vm_snapshot = lambda name: (True, {(0x2DC8, 0x3106): None})  # stale entry in VM config (no recorded address)
        stale_detached = []
        stale_attached = []
        d.detach_device = lambda vid, pid, name: stale_detached.append((vid, pid)) or True
        d.attach_device = lambda vid, pid, name: stale_attached.append((vid, pid)) or True

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
        results.append(("stale-entry recovery", ok,
                        f"detach-then-attach on re-add "
                        f"(detached={stale_detached}, attached={stale_attached})"))
    finally:
        restore_mocks(saved)
    return results


def scenario_reconcile_stale_address():
    """The VM's live config records the host bus/device libvirt resolved at
    attach time. If the device re-enumerated since (DEVNUM changed), the
    recorded address no longer matches the physical device — reconcile must
    detach the stale entry and attach fresh. A matching address must NOT
    cause churn; an unreadable config must abort cleanly."""
    REC_DP = "/devices/pci0000:00/0000:00:08.1/0000:0d:00.4/usb5/5-2/5-2.1/5-2.1.3"
    results = []
    saved = save_mocks()
    try:
        d.pyudev = object()  # satisfy reconcile()'s pyudev-required guard
        d.vm_snapshot = lambda name: (True, {(0x2DC8, 0x3106): (5, 47)})  # resolved at old enumeration
        d.scan_physical_devices = lambda: {REC_DP: (0x2DC8, 0x3106, 5, 48)}  # re-enumerated
        rec_detached = []
        rec_attached = []
        d.detach_device = lambda vid, pid, name: rec_detached.append((vid, pid)) or True
        d.attach_device = lambda vid, pid, name: rec_attached.append((vid, pid)) or True

        r = d.Daemon()
        r.reconcile()
        ok = (rec_detached == [(0x2DC8, 0x3106)] and
              rec_attached == [(0x2DC8, 0x3106)])
        results.append(("reconcile stale-address", ok,
                        f"detach-then-attach on mismatch "
                        f"(detached={rec_detached}, attached={rec_attached})"))

        # healthy case: recorded address matches the current device -> no churn
        d.vm_snapshot = lambda name: (True, {(0x2DC8, 0x3106): (5, 48)})
        rec_detached.clear()
        rec_attached.clear()
        r2 = d.Daemon()
        r2.reconcile()
        results.append(("reconcile matching address",
                        not rec_detached and not rec_attached,
                        "no detach/attach churn"))

        # VM config unreadable -> reconcile aborts cleanly instead of crashing
        d.vm_snapshot = lambda name: (True, None)
        d.scan_physical_devices = lambda: {REC_DP: (0x2DC8, 0x3106, 5, 48)}
        rec_detached.clear()
        rec_attached.clear()
        r3 = d.Daemon()
        r3.reconcile()
        results.append(("reconcile unreadable VM config",
                        not rec_detached and not rec_attached,
                        "aborts cleanly"))
    finally:
        restore_mocks(saved)
    return results


def scenario_event_filtering():
    """Hub events (USB class 09) and non-add/remove actions must be dropped
    at the gate without creating records or timers."""
    results = []
    s = d.Daemon()
    s.handle_event({"action": "add", "DEVTYPE": "usb_device",
                    "devpath": "/devices/hub", "PRODUCT": "1d6b/3/0",
                    "TYPE": "9/0/0"})
    results.append(("hub event ignored", not s.devices and not s.timers,
                    "USB class 09 (hub) events must not create records or timers"))
    DP = "/devices/kbd"
    for action in ("change", "bind", "unbind"):
        s.handle_event({"action": action, "DEVTYPE": "usb_device",
                        "devpath": DP, "PRODUCT": "5ac/24f/108",
                        "TYPE": "0/0/0"})
    results.append(("change/bind/unbind ignored", not s.devices and not s.timers,
                    "only add/remove may reach the state machine"))
    s.handle_event({"action": "add", "DEVTYPE": "usb_interface",
                    "devpath": DP, "PRODUCT": "5ac/24f/108", "TYPE": "0/0/0"})
    results.append(("wrong DEVTYPE ignored", not s.devices,
                    "non-usb_device events are dropped at the gate"))
    return results


def scenario_bad_product_ignored():
    """Malformed PRODUCT values must log and skip, never crash or record."""
    results = []
    s = d.Daemon()
    DP = "/devices/bad"
    s.handle_event({"action": "add", "DEVTYPE": "usb_device", "devpath": DP,
                    "PRODUCT": "", "TYPE": "0/0/0"})
    s.handle_event({"action": "add", "DEVTYPE": "usb_device", "devpath": DP,
                    "PRODUCT": "zzzz/gggg/0", "TYPE": "0/0/0"})
    s.handle_event({"action": "add", "DEVTYPE": "usb_device", "devpath": DP,
                    "PRODUCT": "5ac", "TYPE": "0/0/0"})
    results.append(("bad PRODUCT ignored", not s.devices and not s.timers,
                    "missing/unparseable/single-part PRODUCT must not crash or record"))
    return results


def scenario_untracked_remove_listed():
    """Remove of an untracked allowed device still listed in the VM config
    must detach immediately (daemon may have started mid-life)."""
    results = []
    saved = save_mocks()
    det = []
    try:
        d.vm_snapshot = lambda name: (True, {(0x1532, 0x0083): (5, 47)})
        d.detach_device = lambda vid, pid, name: det.append((vid, pid)) or True
        s = d.Daemon()
        s.handle_event({"action": "remove", "DEVTYPE": "usb_device",
                        "devpath": "/devices/unknown", "PRODUCT": "1532/83/200"})
        results.append(("untracked remove heals config", det == [(0x1532, 0x0083)],
                        "remove of an untracked allowed device still listed "
                        "in the VM config should detach"))
        det.clear()
        d.vm_snapshot = lambda name: (False, {})
        s2 = d.Daemon()
        s2.handle_event({"action": "remove", "DEVTYPE": "usb_device",
                         "devpath": "/devices/unknown", "PRODUCT": "1532/83/200"})
        results.append(("untracked remove VM off", not det,
                        "VM not running: untracked remove must take no action"))
    finally:
        restore_mocks(saved)
    return results


def scenario_attach_vm_unknown_skip():
    """vm_snapshot() returning running=None means unknown state: settle must
    skip the attach."""
    results = []
    saved = save_mocks()
    att = []
    DP = "/devices/unk"
    try:
        d.vm_snapshot = lambda name: (None, None)
        d.attach_device = lambda vid, pid, name: att.append((vid, pid)) or True
        fake_sysfs[DP] = True
        s = d.Daemon()
        s.handle_event({"action": "add", "DEVTYPE": "usb_device", "devpath": DP,
                        "PRODUCT": "5ac/24f/108"})
        fake.t += 2.0  # past the settle window
        s.fire_timers()
        rec = s.devices[DP]
        results.append(("attach skipped when VM unknown",
                        not att and rec is not None and rec.present,
                        "running=None must skip attach and not crash"))
    finally:
        fake_sysfs.pop(DP, None)
        restore_mocks(saved)
    return results


def scenario_attach_exhausts_retries():
    """attach_device() returning False (retries exhausted) must leave the
    record unattached; reconcile retries later."""
    results = []
    saved = save_mocks()
    DP = "/devices/retry"
    try:
        d.vm_snapshot = lambda name: (True, {})
        d.attach_device = lambda vid, pid, name: False
        fake_sysfs[DP] = True
        s = d.Daemon()
        s.handle_event({"action": "add", "DEVTYPE": "usb_device", "devpath": DP,
                        "PRODUCT": "5ac/24f/108"})
        fake.t += 2.0
        s.fire_timers()
        rec = s.devices[DP]
        results.append(("attach failure leaves unattached",
                        rec is not None and rec.home is None,
                        "failed attach must not flip the record to attached"))
    finally:
        fake_sysfs.pop(DP, None)
        restore_mocks(saved)
    return results


def scenario_timer_same_key_dedupe():
    results = []
    calls = []
    s = d.Daemon()
    s.set_timer("k", 1.0, lambda: calls.append("first"))
    s.set_timer("k", 1.0, lambda: calls.append("second"))
    fake.t += 2.0
    s.fire_timers()
    results.append(("timer same-key dedupe", calls == ["second"],
                    "set_timer on an existing key must replace, not stack"))
    return results


def scenario_reconcile_vm_not_running():
    results = []
    saved = save_mocks()
    DP = "/devices/rc"
    try:
        d.pyudev = object()
        d.vm_snapshot = lambda name: (False, {})
        d.scan_physical_devices = lambda: {DP: (0x2DC8, 0x3106, 5, 48)}
        att, det = [], []
        d.attach_device = lambda vid, pid, name: att.append((vid, pid)) or True
        d.detach_device = lambda vid, pid, name: det.append((vid, pid)) or True
        s = d.Daemon()
        s.devices[DP] = d.DeviceState(0x2DC8, 0x3106, present=True, home="testvm")
        s.reconcile()
        ok = (not att and not det and s.devices[DP].home is None)
        results.append(("reconcile VM off releases state", ok,
                        "VM not running: reconcile must take no action and "
                        "mark devices unattached"))
    finally:
        restore_mocks(saved)
    return results


def scenario_nonallowed_ignored():
    """Devices outside the allowlist (e.g. an idle receiver state like
    2dc8:3109) must never trigger any action: their adds create only
    bookkeeping records, never timers or libvirt calls."""
    results = []
    saved = save_mocks()
    att = []
    DP = "/devices/nonallowed"
    try:
        d.attach_device = lambda vid, pid, name: att.append((vid, pid)) or True
        fake_sysfs[DP] = True
        s = d.Daemon()
        s.handle_event({"action": "add", "DEVTYPE": "usb_device", "devpath": DP,
                        "PRODUCT": "2dc8/3109/200"})
        fake.t += 2.0
        s.fire_timers()
        results.append(("non-allowlisted add does nothing",
                        not att and not s.timers,
                        "add of a device outside the allowlist must schedule "
                        "no timers and perform no attach"))
    finally:
        fake_sysfs.pop(DP, None)
        restore_mocks(saved)
    return results


def scenario_multi_vm_order():
    """A device must attach to the first running VM in config order."""
    results = []
    saved = save_mocks()
    DP = "/devices/mv"
    try:
        d.VM_NAMES = ["a", "b"]
        d.pyudev = object()
        d.detach_device = lambda vid, pid, name: None
        d.scan_physical_devices = lambda: {DP: (0x05AC, 0x024F, 5, 8)}

        # A running, B off -> attach to A
        d.vm_snapshots = lambda: [d.VMSnapshot("a", True, {}),
                                  d.VMSnapshot("b", False, {})]
        att_a = []
        d.attach_device = lambda vid, pid, name: att_a.append(name) or True
        s = d.Daemon()
        s.reconcile()
        got_a = att_a == ["a"]

        # A off, B running -> attach to B
        d.vm_snapshots = lambda: [d.VMSnapshot("a", False, {}),
                                  d.VMSnapshot("b", True, {})]
        att_b = []
        d.attach_device = lambda vid, pid, name: att_b.append(name) or True
        s2 = d.Daemon()
        s2.reconcile()
        got_b = att_b == ["b"]

        results.append(("multi-vm order", got_a and got_b,
                        f"first running VM wins in order (A={att_a}, B={att_b})"))
    finally:
        restore_mocks(saved)
    return results


def scenario_multi_vm_no_migration():
    """A device already healthy in a lower-priority running VM must NOT be
    migrated to a higher-priority running VM."""
    results = []
    saved = save_mocks()
    DP = "/devices/mv"
    try:
        d.VM_NAMES = ["a", "b"]
        d.pyudev = object()
        det, att = [], []
        d.attach_device = lambda vid, pid, name: att.append(name) or True
        d.detach_device = lambda vid, pid, name: det.append(name) or True
        d.vm_snapshots = lambda: [d.VMSnapshot("a", True, {}),
                                  d.VMSnapshot("b", True, {(0x05AC, 0x024F): (5, 8)})]
        d.scan_physical_devices = lambda: {DP: (0x05AC, 0x024F, 5, 8)}
        s = d.Daemon()
        s.devices[DP] = d.DeviceState(0x05AC, 0x024F, present=True, home="b")
        s.reconcile()
        results.append(("multi-vm no migration", not det and not att,
                        f"healthy device in B must stay in B (det={det}, att={att})"))
    finally:
        restore_mocks(saved)
    return results


def scenario_multi_vm_stale_reentry():
    """On re-enumeration the device's home VM holds a stale entry: it must be
    cleared and re-attached to the SAME home (not the higher-priority VM)."""
    results = []
    saved = save_mocks()
    DP = "/devices/mv"
    try:
        d.VM_NAMES = ["a", "b"]
        d.pyudev = object()
        det, att = [], []
        d.attach_device = lambda vid, pid, name: att.append(name) or True
        d.detach_device = lambda vid, pid, name: det.append(name) or True
        # B resolved the device at (5,7); the device re-enumerated to (5,8)
        d.vm_snapshots = lambda: [d.VMSnapshot("a", True, {}),
                                  d.VMSnapshot("b", True, {(0x05AC, 0x024F): (5, 7)})]
        d.scan_physical_devices = lambda: {DP: (0x05AC, 0x024F, 5, 8)}
        s = d.Daemon()
        s.devices[DP] = d.DeviceState(0x05AC, 0x024F, present=True, home="b")
        s.reconcile()
        ok = det == ["b"] and att == ["b"]
        results.append(("multi-vm stale reentry", ok,
                        f"detach stale then re-attach to home B, not A "
                        f"(det={det}, att={att})"))
    finally:
        restore_mocks(saved)
    return results


def scenario_multi_vm_unknown_defer():
    """A higher-priority VM with unknown state must defer the action even when
    a lower-priority VM is confirmed running."""
    results = []
    saved = save_mocks()
    DP = "/devices/mv"
    try:
        d.VM_NAMES = ["a", "b"]
        d.pyudev = object()
        det, att = [], []
        d.attach_device = lambda vid, pid, name: att.append(name) or True
        d.detach_device = lambda vid, pid, name: det.append(name) or True
        d.vm_snapshots = lambda: [d.VMSnapshot("a", None, None),
                                  d.VMSnapshot("b", True, {})]
        d.scan_physical_devices = lambda: {DP: (0x05AC, 0x024F, 5, 8)}
        s = d.Daemon()
        s.reconcile()
        results.append(("multi-vm unknown defer", not det and not att,
                        f"higher-priority VM unknown must defer (det={det}, att={att})"))
    finally:
        restore_mocks(saved)
    return results


def scenario_multi_vm_dead_zombie():
    """A physically absent allowed device still listed in several VMs must be
    detached from every one of them."""
    results = []
    saved = save_mocks()
    try:
        d.VM_NAMES = ["a", "b"]
        d.pyudev = object()
        det, att = [], []
        d.attach_device = lambda vid, pid, name: att.append(name) or True
        d.detach_device = lambda vid, pid, name: det.append(name) or True
        d.vm_snapshots = lambda: [d.VMSnapshot("a", True, {(0x05AC, 0x024F): (5, 8)}),
                                  d.VMSnapshot("b", True, {(0x05AC, 0x024F): (4, 3)})]
        d.scan_physical_devices = lambda: {}  # device physically gone
        s = d.Daemon()
        s.reconcile()
        ok = sorted(det) == ["a", "b"] and not att
        results.append(("multi-vm dead zombie", ok,
                        f"detach from every VM holding the absent device (det={det})"))
    finally:
        restore_mocks(saved)
    return results


def scenario_multi_vm_duplicate_sweep():
    """A duplicate entry in another running VM must be cleaned even when the
    home VM already holds the device healthy (no re-attach / churn)."""
    results = []
    saved = save_mocks()
    DP = "/devices/mv"
    try:
        d.VM_NAMES = ["a", "b"]
        d.pyudev = object()
        det, att = [], []
        d.attach_device = lambda vid, pid, name: att.append(name) or True
        d.detach_device = lambda vid, pid, name: det.append(name) or True
        # home B holds it healthy; higher-priority A also has a stale duplicate
        d.vm_snapshots = lambda: [d.VMSnapshot("a", True, {(0x05AC, 0x024F): (5, 8)}),
                                  d.VMSnapshot("b", True, {(0x05AC, 0x024F): (5, 8)})]
        d.scan_physical_devices = lambda: {DP: (0x05AC, 0x024F, 5, 8)}
        s = d.Daemon()
        s.devices[DP] = d.DeviceState(0x05AC, 0x024F, present=True, home="b")
        s.reconcile()
        ok = det == ["a"] and not att
        results.append(("multi-vm duplicate sweep", ok,
                        f"detach duplicate in A, keep healthy home B (det={det}, att={att})"))
    finally:
        restore_mocks(saved)
    return results


def scenario_multi_vm_first_running_wins():
    """Selection always starts from the first VM: with no home memory (fresh
    daemon), a device healthy in a lower-priority VM is re-assigned to the
    first RUNNING VM, then stays there."""
    results = []
    saved = save_mocks()
    DP = "/devices/mv"
    try:
        d.VM_NAMES = ["a", "b"]
        d.pyudev = object()
        det, att = [], []
        d.attach_device = lambda vid, pid, name: att.append(name) or True
        d.detach_device = lambda vid, pid, name: det.append(name) or True
        d.vm_snapshots = lambda: [d.VMSnapshot("a", True, {}),
                                  d.VMSnapshot("b", True, {(0x05AC, 0x024F): (5, 8)})]
        d.scan_physical_devices = lambda: {DP: (0x05AC, 0x024F, 5, 8)}
        s = d.Daemon()
        # home=None: fresh daemon -> re-select from the first VM
        s.reconcile()
        ok = det == ["b"] and att == ["a"]
        results.append(("multi-vm first running wins", ok,
                        f"fresh selection picks first running VM A "
                        f"(det={det}, att={att})"))
    finally:
        restore_mocks(saved)
    return results


def scenario_multi_vm_reelect_on_stop():
    """When the selected home VM stops running, the daemon re-elects: it
    scans from the first VM again and picks the first running one (sticky
    once re-elected)."""
    results = []
    saved = save_mocks()
    DP = "/devices/mv"
    try:
        d.VM_NAMES = ["a", "b"]
        d.pyudev = object()
        att, det = [], []
        d.attach_device = lambda vid, pid, name: att.append(name) or True
        d.detach_device = lambda vid, pid, name: det.append(name) or True
        d.scan_physical_devices = lambda: {DP: (0x05AC, 0x024F, 5, 8)}

        s = d.Daemon()
        # pass 1: A running, B off -> owner A
        d.vm_snapshots = lambda: [d.VMSnapshot("a", True, {}),
                                  d.VMSnapshot("b", False, {})]
        s.reconcile()
        first = att == ["a"]

        # pass 2: A stopped, B running -> re-elect to B (from first VM)
        d.vm_snapshots = lambda: [d.VMSnapshot("a", False, {}),
                                  d.VMSnapshot("b", True, {})]
        s.reconcile()
        second = att == ["a", "b"]

        results.append(("multi-vm reelect on stop", first and second,
                        f"owner moves A -> B once A stops (att={att}, det={det})"))
    finally:
        restore_mocks(saved)
    return results


def scenario_env_vm_list():
    """USB_PT_VM comma-separated ordered list parsing (backward compatible)."""
    results = []
    results.append(("vm list parse",
                    d._parse_vms(" a,b , c ") == ["a", "b", "c"],
                    "comma-separated USB_PT_VM parses to an ordered, trimmed list"))
    results.append(("vm list single",
                    d._parse_vms("solo") == ["solo"],
                    "a single VM name still parses to a one-element list"))
    results.append(("vm list empty",
                    d._parse_vms("") == [] and d._parse_vms(None) == [],
                    "empty/missing USB_PT_VM parses to an empty list"))
    return results


def scenario_env_strict():
    """Integer env parsing: well-formed/missing values resolve normally,
    but malformed values must abort startup (fail fast, non-zero exit) —
    never fall back to the default. The abort path is verified end-to-end
    in subprocesses, since an in-process check would kill the test itself."""
    results = []
    os.environ["USB_PT_TEST_INT_OK"] = "9"
    try:
        results.append(("env valid value parsed",
                        d._env_int("USB_PT_TEST_INT_OK", 1) == 9,
                        "a well-formed integer env value must be parsed"))
        results.append(("env missing value uses default",
                        d._env_int("USB_PT_TEST_INT_MISSING", 41) == 41,
                        "absent env vars must resolve to the default"))
        results.append(("_env_float removed",
                        not hasattr(d, "_env_float"),
                        "config values are integer-only; the float helper is gone"))
    finally:
        os.environ.pop("USB_PT_TEST_INT_OK", None)

    daemon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "usb-passthrough-daemon.py")
    # "1.0" is deliberately invalid now: integer-only config, so legacy
    # decimal notation must refuse to start instead of being accepted
    for var, bad in (("USB_PT_SETTLE", "abc"),
                     ("USB_PT_SETTLE", "1.0"),
                     ("USB_PT_ATTACH_RETRIES", "12x")):
        env = os.environ.copy()
        env.update({"USB_PT_VM": "testvm", "USB_PT_ALLOWED": "05ac:024f",
                    var: bad})
        p = subprocess.run([sys.executable, daemon_path, "--reconcile-once"],
                           capture_output=True, text=True, env=env)
        ok = p.returncode != 0 and var in p.stderr
        results.append((f"bad {var} aborts startup", ok,
                        f"{var}={bad!r} must exit non-zero with a clear error"))
    return results


def report(results):
    failed = False
    for name, ok, why in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {why}")
        failed = failed or not ok
    print("attached:", [f"{v:04x}:{p:04x}" for v, p in attached_log])
    print("detached:", [f"{v:04x}:{p:04x}" for v, p in detached_log])
    return failed


def main():
    # replay_main_flow must run first: it relies on the base mocks, while
    # scenario_*() patch and restore them
    results = []
    results += replay_main_flow()
    results += scenario_stale_entry_recovery()
    results += scenario_reconcile_stale_address()
    results += scenario_event_filtering()
    results += scenario_bad_product_ignored()
    results += scenario_untracked_remove_listed()
    results += scenario_attach_vm_unknown_skip()
    results += scenario_attach_exhausts_retries()
    results += scenario_timer_same_key_dedupe()
    results += scenario_reconcile_vm_not_running()
    results += scenario_nonallowed_ignored()
    results += scenario_multi_vm_order()
    results += scenario_multi_vm_no_migration()
    results += scenario_multi_vm_stale_reentry()
    results += scenario_multi_vm_unknown_defer()
    results += scenario_multi_vm_dead_zombie()
    results += scenario_multi_vm_duplicate_sweep()
    results += scenario_multi_vm_first_running_wins()
    results += scenario_multi_vm_reelect_on_stop()
    results += scenario_env_vm_list()
    results += scenario_env_strict()
    return 1 if report(results) else 0


if __name__ == "__main__":
    sys.exit(main())