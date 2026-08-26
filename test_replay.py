#!/usr/bin/env python3
"""Replay a captured udev log through the passthrough state machine.

Uses mocked virsh/sysfs, so nothing real is touched. Validates:
  * allowed devices get attached on add (settle)
  * real removals (no quick re-add) get detached (debounce)
  * re-enumerations (8BitDo IDLE<->active, sleep/wake, power cycle) are
    NOT mistaken for removals (no thrash detach)
  * 8BitDo IDLE mode (2dc8:3109) is never attached

Note: the Razer mouse's BT<->2.4G switch emits NO udev events (the dongle
stays enumerated), so the 7202/7229 remove+add in the fixture is a real
removal/insertion (unplug or power cycle), not a mode switch.

Usage: python3 test_replay.py /path/to/udev.log
"""

import importlib.util
import re
import sys

LOG_PATH = sys.argv[1] if len(sys.argv) > 1 else "sample-events.log"

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

# --- parse udev.log into (ts, action, devpath, PRODUCT) -------------------

first_re = re.compile(
    r"^UDEV\s+\[([0-9.]+)\]\s+(\S+)\s+(\S+)\s+\((\S+)\)")
events = []
current = None
with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
    for raw in f:
        line = raw.rstrip("\n").rstrip("\r")
        if line == "":
            if current is not None:
                product = current.get("PRODUCT")
                if product:
                    events.append((float(current["_ts"]), current["_action"],
                                   current["DEVPATH"], product))
                current = None
            continue
        m = first_re.match(line)
        if m:
            current = {"_ts": m.group(1), "_action": m.group(2),
                       "DEVPATH": m.group(3)}
            continue
        if current is not None and "=" in line:
            k, v = line.split("=", 1)
            current[k] = v

events.sort(key=lambda e: e[0])
print(f"parsed {len(events)} usb_device events from {LOG_PATH}")
if not events:
    sys.exit("no events found")

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

for ts, action, devpath, product in events:
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

fake.t = events[-1][0] + 5.0  # flush remaining timers
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
for name, ok, why in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {why}")
    failed = failed or not ok

print("attached:", [f"{v:04x}:{p:04x}" for v, p in attached_log])
print("detached:", [f"{v:04x}:{p:04x}" for v, p in detached_log])
sys.exit(1 if failed else 0)
