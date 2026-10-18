#!/usr/bin/env python3
"""
rigtop — a featherweight load monitor and cleaner for dev machines running
AI coding agents and Docker.

Zero dependencies (Python 3.9+ standard library only). CPU and memory come
straight from the Mach kernel APIs via ctypes, so the cheap path never forks.
`ps` and the Docker CLI only run while a dashboard tab is open and visible;
disk scans run at background priority, on demand or at most every 30 minutes
while the dashboard is open. Otherwise rigtop drops to one tiny kernel read
every few seconds to keep the terminal status line fresh.

    ./rigtop               start, print a live line, open the dashboard
    ./rigtop --no-open     don't open a browser tab
    ./rigtop --once        print a one-shot snapshot and exit
"""

import argparse
import collections
import ctypes
import ctypes.util
import json
import math
import os
import platform
import random
import re
import resource
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.realpath(os.path.expanduser("~"))
ACTIVE_WINDOW = 12      # seconds since the last dashboard poll that still counts as "watching"
IDLE_INTERVAL = 5       # kernel-only sampling cadence when nobody is watching
CACHE_SCAN_EVERY = 1800
HISTORY_SECONDS = 15 * 60
GB = 1024 ** 3
BG = ["/usr/sbin/taskpolicy", "-b"] if os.path.exists("/usr/sbin/taskpolicy") else ["nice", "-n", "19"]


# ───────────────────────────── kernel stats (ctypes) ─────────────────────────────

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
c_uint, c_int, c_u32, c_u64, c_size_t = ctypes.c_uint, ctypes.c_int, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_size_t

libc.mach_host_self.restype = c_uint
libc.host_processor_info.argtypes = [c_uint, c_int, ctypes.POINTER(c_uint),
                                     ctypes.POINTER(ctypes.POINTER(c_int)), ctypes.POINTER(c_uint)]
libc.vm_deallocate.argtypes = [c_uint, ctypes.c_void_p, c_size_t]
libc.host_statistics64.argtypes = [c_uint, c_int, ctypes.c_void_p, ctypes.POINTER(c_uint)]
libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(c_size_t), ctypes.c_void_p, c_size_t]

HOST = libc.mach_host_self()          # fetched once; each call would add a port reference
TASK_SELF = c_uint.in_dll(libc, "mach_task_self_").value
PROCESSOR_CPU_LOAD_INFO, HOST_VM_INFO64 = 2, 4


class VMStats64(ctypes.Structure):    # <mach/vm_statistics.h> vm_statistics64
    _fields_ = [
        ("free_count", c_u32), ("active_count", c_u32), ("inactive_count", c_u32), ("wire_count", c_u32),
        ("zero_fill_count", c_u64), ("reactivations", c_u64), ("pageins", c_u64), ("pageouts", c_u64),
        ("faults", c_u64), ("cow_faults", c_u64), ("lookups", c_u64), ("hits", c_u64), ("purges", c_u64),
        ("purgeable_count", c_u32), ("speculative_count", c_u32),
        ("decompressions", c_u64), ("compressions", c_u64), ("swapins", c_u64), ("swapouts", c_u64),
        ("compressor_page_count", c_u32), ("throttled_count", c_u32),
        ("external_page_count", c_u32), ("internal_page_count", c_u32),
        ("total_uncompressed_pages_in_compressor", c_u64),
    ]


class XswUsage(ctypes.Structure):     # <sys/sysctl.h> xsw_usage
    _fields_ = [("total", c_u64), ("avail", c_u64), ("used", c_u64), ("pagesize", c_u32), ("encrypted", c_int)]


def sysctl_raw(name, ctype):
    val, size = ctype(), c_size_t(ctypes.sizeof(ctype))
    if libc.sysctlbyname(name.encode(), ctypes.byref(val), ctypes.byref(size), None, 0) != 0:
        return None
    return val


def sysctl_int(name, default=0):
    v = sysctl_raw(name, ctypes.c_int64)
    return v.value if v is not None else default


def sysctl_str(name):
    size = c_size_t(0)
    if libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0) != 0:
        return ""
    buf = ctypes.create_string_buffer(size.value)
    libc.sysctlbyname(name.encode(), buf, ctypes.byref(size), None, 0)
    return buf.value.decode(errors="replace")


def cpu_ticks():
    """Per-core (user, system, idle, nice) tick counters."""
    count, info, info_count = c_uint(), ctypes.POINTER(c_int)(), c_uint()
    if libc.host_processor_info(HOST, PROCESSOR_CPU_LOAD_INFO, ctypes.byref(count),
                                ctypes.byref(info), ctypes.byref(info_count)) != 0:
        return []
    try:
        return [tuple(info[i * 4 + j] & 0xFFFFFFFF for j in range(4)) for i in range(count.value)]
    finally:
        libc.vm_deallocate(TASK_SELF, ctypes.cast(info, ctypes.c_void_p), info_count.value * ctypes.sizeof(c_int))


def vm_stats():
    s, count = VMStats64(), c_uint(ctypes.sizeof(VMStats64) // 4)
    libc.host_statistics64(HOST, HOST_VM_INFO64, ctypes.byref(s), ctypes.byref(count))
    return s


DATA_VOLUME = "/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/"


def disk_usage():
    st = os.statvfs(DATA_VOLUME)
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


# ───────────────────────────── helpers ─────────────────────────────

def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
          "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def parse_size(s):
    m = re.match(r"\s*([\d.]+)\s*([a-zA-Z]*)", s or "")
    if not m:
        return 0
    return int(float(m.group(1)) * _UNITS.get(m.group(2).lower() or "b", 1))


def fmt_gb(b):
    return "%.1f GB" % (b / GB) if b >= GB / 10 else "%d MB" % (b / 1024 ** 2)


def du_k(path, depth=0, timeout=900):
    """Sizes in bytes from `du -k`, at background priority. depth 0 → {path: size}."""
    cmd = BG + ["du", "-x", "-k"] + (["-d", str(depth)] if depth else ["-s"]) + [path]
    code, out, _ = run(cmd, timeout=timeout)     # du exits 1 on unreadable folders; the totals are still useful
    sizes = {}
    for line in out.splitlines():
        k, _, p = line.partition("\t")
        if k.isdigit():
            sizes[p] = int(k) * 1024
    return sizes


def under_home(path, allow_home=False):
    real = os.path.realpath(path)
    return real if (real.startswith(HOME + os.sep) or (allow_home and real == HOME)) else None


def empty_dir(path, make_writable=False):
    """Delete a folder's contents but keep the folder. Never follows symlinks."""
    real = under_home(path)
    if not real or real.count(os.sep) - HOME.count(os.sep) < 2 or not os.path.isdir(real):
        return False, "refusing to clean %s" % path
    errors = 0
    for child in os.listdir(real):
        p = os.path.join(real, child)
        if make_writable and not os.path.islink(p):
            run(BG + ["chmod", "-R", "u+w", p], timeout=1800)
        code, _, _ = run(BG + ["rm", "-rf", p], timeout=3600)
        errors += code != 0
    return errors == 0, "some files were in use and were skipped" if errors else "cleaned"


# Developer caches that tools rebuild or re-download on their own. "safe" ones are
# pre-selected for the one-click "Clean selected" sweep.
def _p(rel):
    return os.path.join(HOME, rel)


CACHE_TARGETS = [
    {"id": "go-build", "label": "Go build cache", "path": os.environ.get("GOCACHE") or _p("Library/Caches/go-build"), "safe": True,
     "hint": "Compiled Go packages. Rebuilt automatically; the next build is slower."},
    {"id": "go-mod", "label": "Go module downloads", "path": os.environ.get("GOMODCACHE") or _p("go/pkg/mod"), "safe": False,
     "writable": True, "hint": "Downloaded Go modules. Re-downloaded on the next build (needs network)."},
    {"id": "npm", "label": "npm cache", "path": _p(".npm/_cacache"), "safe": True, "hint": "Downloaded npm packages. Re-downloaded when needed."},
    {"id": "uv", "label": "uv cache", "path": _p(".cache/uv"), "safe": True, "hint": "Python packages downloaded by uv. Existing virtualenvs keep working."},
    {"id": "pip", "label": "pip cache", "path": _p("Library/Caches/pip"), "safe": True, "hint": "Downloaded Python wheels."},
    {"id": "yarn", "label": "Yarn cache", "path": _p("Library/Caches/Yarn"), "safe": True, "hint": "Downloaded Yarn packages."},
    {"id": "bun", "label": "Bun cache", "path": _p(".bun/install/cache"), "safe": True, "hint": "Downloaded Bun packages."},
    {"id": "brew", "label": "Homebrew downloads", "path": _p("Library/Caches/Homebrew"), "safe": True, "hint": "Old bottle downloads."},
    {"id": "cargo", "label": "Cargo downloads", "path": _p(".cargo/registry/cache"), "safe": True, "hint": "Downloaded .crate files."},
    {"id": "xcode", "label": "Xcode DerivedData", "path": _p("Library/Developer/Xcode/DerivedData"), "safe": True, "hint": "Xcode build products and indexes."},
    {"id": "sim", "label": "Simulator caches", "path": _p("Library/Developer/CoreSimulator/Caches"), "safe": True, "hint": "iOS Simulator dyld caches."},
    {"id": "playwright", "label": "Playwright browsers", "path": _p("Library/Caches/ms-playwright"), "safe": False,
     "hint": "Test browsers. Reinstall with `npx playwright install`."},
    {"id": "gradle", "label": "Gradle caches", "path": _p(".gradle/caches"), "safe": False, "hint": "Re-downloaded on the next Gradle build."},
    {"id": "maven", "label": "Maven repository", "path": _p(".m2/repository"), "safe": False, "hint": "Re-downloaded on the next Maven build."},
    {"id": "hf", "label": "Hugging Face models", "path": _p(".cache/huggingface"), "safe": False, "hint": "Downloaded models; large to re-download."},
]
TARGETS_BY_ID = {t["id"]: t for t in CACHE_TARGETS}
APP_CACHES = _p("Library/Caches")
KNOWN_CACHE_PATHS = {os.path.realpath(t["path"]) for t in CACHE_TARGETS}

# Interpreters and dev servers. One of these whose parent died (ppid 1) is almost
# always a leftover from a crashed agent run or a forgotten dev server.
DEV_RUNTIME = re.compile(r"^(node|bun|deno|python[\d.]*|Python|ruby|java|go|uv|uvx|npm|npx|pnpm|yarn|tsx|ts-node|esbuild|vite|"
                         r"next-server|uvicorn|gunicorn|celery|air|nodemon|turbo|webpack|playwright|chrome-headless-shell)$")

ALIASES = {"claude": "Claude Code", "codex": "Codex CLI", "gemini": "Gemini CLI",
           "com.docker.backend": "Docker Desktop", "com.docker.build": "Docker Desktop",
           "com.docker.virtualization": "Docker Desktop", "docker": "Docker CLI"}
NOT_KILLABLE = {"rigtop (this tool)", "Docker VM", "Virtual Machine"}
# macOS's own agents run as you but launchd restarts them, so ending them frees nothing
SYSTEM_PATH = re.compile(r"^(/System/|/usr/|/bin/|/sbin/|/Library/Apple/|/private/var/)")


def group_of(path, docker_vm_name):
    base = path.rsplit("/", 1)[-1]
    if base in ALIASES:
        return ALIASES[base], False
    if base == "com.apple.Virtualization.VirtualMachine":
        return docker_vm_name, False
    m = re.search(r"/([^/]+)\.app/", path)
    if m:
        name = "Docker Desktop" if m.group(1) == "Docker" else m.group(1)
        # only real top-level apps can be asked to quit gracefully
        quittable = bool(re.match(r"^(/System)?(/Users/[^/]+)?/Applications/[^/]+\.app/", path))
        return name, quittable
    m = re.search(r"/Library/Application Support/([^/]+)/", path)
    if m:
        return m.group(1), False
    return base, False


# ───────────────────────────── background jobs ─────────────────────────────

class Jobs:
    """Long cleanups run off the request thread; the dashboard polls their state."""

    def __init__(self, on_done, measure=True):
        self.lock = threading.Lock()
        self.items = collections.OrderedDict()
        self.n = 0
        self.on_done = on_done
        self.measure = measure      # compare free disk before/after to report "freed X GB"

    def start(self, key, label, fn):
        with self.lock:
            if any(j["key"] == key and j["state"] == "running" for j in self.items.values()):
                return None
            self.n += 1
            job = {"id": self.n, "key": key, "label": label, "state": "running", "message": "",
                   "t0": time.time(), "t1": None, "freed": 0}
            self.items[self.n] = job
            while len(self.items) > 20:
                self.items.popitem(last=False)

        def runner():
            before = disk_usage()[1] if self.measure else 0
            try:
                ok, msg = fn()
            except Exception as e:
                ok, msg = False, str(e)
            freed = max(disk_usage()[1] - before, 0) if self.measure else 0
            with self.lock:
                job.update(state="done" if ok else "failed", message=msg, t1=time.time(), freed=freed)
            self.on_done(key)

        threading.Thread(target=runner, daemon=True).start()
        return job["id"]

    def running(self, prefix):
        with self.lock:
            return any(j["key"].startswith(prefix) and j["state"] == "running" for j in self.items.values())

    def list(self):
        with self.lock:
            return [dict(j) for j in self.items.values()]


# ───────────────────────────── the monitor ─────────────────────────────

class Monitor:
    demo = False
    def __init__(self, interval, docker_interval):
        self.interval = interval
        self.docker_interval = docker_interval
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.docker_wake = threading.Event()
        self.cache_wake = threading.Event()
        self.last_client = 0.0
        self.uid = os.getuid()
        self.pid = os.getpid()
        self.ncpu = os.cpu_count() or 1
        self.page = sysctl_int("hw.pagesize", 16384)
        self.host = {
            "name": socket.gethostname().split(".")[0],
            "chip": sysctl_str("machdep.cpu.brand_string") or platform.machine(),
            "cores": self.ncpu,
            "pcores": sysctl_int("hw.perflevel0.logicalcpu"),
            "ecores": sysctl_int("hw.perflevel1.logicalcpu"),
            "mem_total": sysctl_int("hw.memsize"),
            "os": "macOS " + platform.mac_ver()[0],
        }
        self.history = collections.deque(maxlen=HISTORY_SECONDS // 2)
        self.state = {}
        self.apps = []
        self.apps_t = 0.0
        self.procs = {}            # group -> [proc dict]
        self.orphans = []
        self.protected = {self.pid}
        self.self_stats = {"cpu": 0.0, "rss": 0}
        self.docker = {"available": bool(self._docker_bin()), "running": False, "containers": [],
                       "df": None, "vm": None, "t": 0, "df_t": 0, "error": None}
        self.caches = {"targets": [], "app_caches": [], "t": 0, "scanning": False}
        self.du = {}               # folder -> {"t", "total", "items": [{name, path, size}]}
        self.jobs = Jobs(self._job_done)
        self._prev_ticks = None
        self._prev_vm = None
        self._prev_t = None
        self._prev_rusage = None

    def _job_done(self, key):
        if key.startswith(("cache:", "appcache:", "clean-selected", "trash")):
            self.caches["t"] = 0
            self.cache_wake.set()
        if key.startswith(("docker", "clean-selected")):
            self.docker["df_t"] = 0
            self.docker_wake.set()
        self.wake.set()

    # watching = a dashboard tab polled recently
    def touch(self, every=None):
        was_idle = not self.watching()
        if every and math.isfinite(every):   # the dashboard's refresh picker drives the sampling cadence
            self.interval = min(max(every, 1.0), 60.0)
        self.last_client = time.time()
        if was_idle:
            self.wake.set()
            self.docker_wake.set()
            self.cache_wake.set()

    def watching(self):
        return time.time() - self.last_client < max(ACTIVE_WINDOW, self.interval * 3)

    # ── system sampling ──
    def sample(self, with_procs):
        now = time.time()
        ticks, vm = cpu_ticks(), vm_stats()
        cores, user, system = [], 0.0, 0.0
        if self._prev_ticks and len(self._prev_ticks) == len(ticks):
            tu = ts = tt = 0
            for a, b in zip(self._prev_ticks, ticks):
                du, ds, di, dn = (b[i] - a[i] for i in range(4))
                tot = max(du + ds + di + dn, 1)
                cores.append(round(100.0 * (du + ds + dn) / tot, 1))
                tu, ts, tt = tu + du + dn, ts + ds, tt + tot
            user, system = 100.0 * tu / max(tt, 1), 100.0 * ts / max(tt, 1)
        dt = (now - self._prev_t) if self._prev_t else None

        def rate(field):
            if not (dt and self._prev_vm):
                return 0
            return max(getattr(vm, field) - getattr(self._prev_vm, field), 0) / dt

        pg = self.page
        app = max(vm.internal_page_count - vm.purgeable_count, 0) * pg
        wired = vm.wire_count * pg
        compressed = vm.compressor_page_count * pg
        cached = (vm.external_page_count + vm.purgeable_count) * pg
        total = self.host["mem_total"]
        swap = sysctl_raw("vm.swapusage", XswUsage)
        disk_total, disk_free = disk_usage()
        mem = {
            "total": total, "app": app, "wired": wired, "compressed": compressed, "cached": cached,
            "used": app + wired + compressed,
            "free": max(total - app - wired - compressed - cached, 0),
            "pressure_level": sysctl_int("kern.memorystatus_vm_pressure_level", 1),
            "free_pct": sysctl_int("kern.memorystatus_level", 100),
            "swap_total": swap.total if swap else 0, "swap_used": swap.used if swap else 0,
            "swapins": rate("swapins") * pg, "swapouts": rate("swapouts") * pg,
            "pageins": rate("pageins") * pg, "compressions": rate("compressions") * pg,
        }
        self._prev_ticks, self._prev_vm, self._prev_t = ticks, vm, now

        ru = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_s = sum(r.ru_utime + r.ru_stime for r in ru)
        if self._prev_rusage:
            self.self_stats["cpu"] = round(100.0 * (cpu_s - self._prev_rusage[0]) / max(now - self._prev_rusage[1], 0.1) / self.ncpu, 2)
        self._prev_rusage = (cpu_s, now)

        if with_procs:
            self._sample_procs()

        with self.lock:
            self.state = {
                "t": now,
                "cpu": {"total": round(user + system, 1), "user": round(user, 1), "system": round(system, 1), "cores": cores},
                "load": [round(x, 2) for x in os.getloadavg()],
                "mem": mem,
                "disk": {"total": disk_total, "free": disk_free},
            }
            if cores and dt and dt >= 0.5:     # a back-to-back sample has no meaningful CPU delta
                self.history.append([round(now, 1), round(user, 1), round(system, 1),
                                     round(app / GB, 3), round(wired / GB, 3), round(compressed / GB, 3),
                                     round(cached / GB, 3), round(mem["swap_used"] / GB, 3),
                                     mem["pressure_level"], mem["free_pct"]])

    def _sample_procs(self):
        code, out, _ = run(["ps", "-axo", "pid=,ppid=,uid=,pcpu=,rss=,comm="], timeout=10)
        if code != 0:
            return
        docker_up = "com.docker.backend" in out
        vm_name = "Docker VM" if docker_up else "Virtual Machine"
        rows, parent = [], {}
        for line in out.splitlines():
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            try:
                pid, ppid, uid, pcpu, rss = int(parts[0]), int(parts[1]), int(parts[2]), float(parts[3]), int(parts[4]) * 1024
            except ValueError:
                continue
            parent[pid] = ppid
            rows.append((pid, ppid, uid, pcpu, rss, parts[5]))
        # never signal rigtop or the shell/terminal chain that launched it
        protected, p = {self.pid}, self.pid
        while p > 1 and p in parent:
            p = parent[p]
            protected.add(p)
        groups, procs, orphans = {}, {}, []
        for pid, ppid, uid, pcpu, rss, path in rows:
            if pid == self.pid:
                self.self_stats["rss"] = rss
                name, quittable = "rigtop (this tool)", False
            else:
                name, quittable = group_of(path, vm_name)
            if rss == 0 and pcpu == 0:
                continue
            own = uid == self.uid and not SYSTEM_PATH.match(path)
            g = groups.get(name)
            if g is None:
                g = groups[name] = {"name": name, "count": 0, "cpu": 0.0, "mem": 0, "quittable": quittable,
                                    "own": False, "killable": False}
            g["count"] += 1
            g["cpu"] += pcpu / self.ncpu        # % of the whole machine, so it adds up to the CPU tile
            g["mem"] += rss
            g["own"] = g["own"] or own
            g["quittable"] = g["quittable"] or quittable
            g["killable"] = g["killable"] or (own and pid not in protected)
            proc = {"pid": pid, "cpu": round(pcpu / self.ncpu, 2), "cores": round(pcpu / 100, 2),
                    "mem": rss, "path": path, "own": own and pid not in protected}
            procs.setdefault(name, []).append(proc)
            if own and ppid == 1 and pid not in protected and not path.startswith("/Applications/") and DEV_RUNTIME.match(path.rsplit("/", 1)[-1]):
                orphans.append(proc)
        apps = sorted(groups.values(), key=lambda g: g["cpu"] * 4 + g["mem"] / GB, reverse=True)
        for g in apps:
            g["cpu"] = round(g["cpu"], 2)
            g["quittable"] = g["quittable"] and g["own"]
            g["killable"] = g["killable"] and g["name"] not in NOT_KILLABLE
        procs["__orphans__"] = orphans
        with self.lock:
            self.apps, self.procs, self.apps_t = apps[:60], procs, time.time()
            self.orphans, self.protected = orphans, protected

    def loop(self):
        while True:
            active = self.watching()
            try:
                self.sample(with_procs=active)
            except Exception as e:      # never let one bad read kill the sampler
                print("\nsample error: %r" % e, file=sys.stderr)
            self.wake.wait(self.interval if active else IDLE_INTERVAL)
            self.wake.clear()

    # ── caches (disk) ──
    def scan_caches(self):
        self.caches["scanning"] = True
        try:
            targets = []
            for t in CACHE_TARGETS:
                if os.path.isdir(t["path"]):
                    size = du_k(t["path"]).get(t["path"], 0)
                    targets.append(dict({k: t[k] for k in ("id", "label", "path", "safe", "hint")}, size=size))
            app_caches = []
            if os.path.isdir(APP_CACHES):
                for p, size in du_k(APP_CACHES, depth=1).items():
                    if p == APP_CACHES or os.path.realpath(p) in KNOWN_CACHE_PATHS or size < 100 * 1024 ** 2:
                        continue
                    app_caches.append({"id": "appcache:" + os.path.basename(p), "label": os.path.basename(p), "path": p, "size": size})
                app_caches.sort(key=lambda c: c["size"], reverse=True)
            with self.lock:
                self.caches.update(targets=sorted(targets, key=lambda c: c["size"], reverse=True),
                                   app_caches=app_caches[:12], t=time.time())
        finally:
            self.caches["scanning"] = False

    def cache_loop(self):
        while True:
            if self.watching() and time.time() - self.caches["t"] > CACHE_SCAN_EVERY and not self.jobs.running("cache"):
                try:
                    self.scan_caches()
                except Exception as e:
                    print("\ncache scan error: %r" % e, file=sys.stderr)
            self.cache_wake.wait(60)
            self.cache_wake.clear()

    def scan_folder(self, path):
        sizes = du_k(path, depth=2, timeout=1800)
        tree = collections.defaultdict(list)
        for p, size in sizes.items():
            if p != path:
                tree[os.path.dirname(p)].append({"name": os.path.basename(p), "path": p, "size": size})
        now = time.time()
        with self.lock:
            for folder, items in tree.items():
                items.sort(key=lambda i: i["size"], reverse=True)
                self.du[folder] = {"t": now, "total": sizes.get(folder, 0), "items": items[:40]}
            if path not in tree:
                self.du[path] = {"t": now, "total": sizes.get(path, 0), "items": []}
        return True, "scanned %s (%s)" % (path.replace(HOME, "~"), fmt_gb(sizes.get(path, 0)))

    # ── docker ──
    @staticmethod
    def _docker_bin():
        return shutil.which("docker") or next(
            (p for p in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                         "/Applications/Docker.app/Contents/Resources/bin/docker") if os.path.exists(p)), None)

    def docker_refresh(self, include_df):
        d = self._docker_bin()
        if not d:
            return
        code, out, err = run([d, "ps", "-a", "--format", "{{json .}}"], timeout=15)
        if code != 0:
            with self.lock:
                self.docker.update(running=False, containers=[], t=time.time(),
                                   error=(err.strip().splitlines() or ["Docker is not running"])[-1][:200])
            return
        containers = []
        for line in out.splitlines():
            try:
                c = json.loads(line)
            except ValueError:
                continue
            proj = re.search(r"com\.docker\.compose\.project=([^,]+)", c.get("Labels", ""))
            containers.append({"id": c.get("ID"), "name": c.get("Names"), "image": c.get("Image"),
                               "state": c.get("State"), "status": c.get("Status"), "ports": c.get("Ports", ""),
                               "project": proj.group(1) if proj else None, "cpu": 0.0, "mem": 0, "mem_limit": 0})
        if any(c["state"] == "running" for c in containers):
            code, out, _ = run([d, "stats", "--no-stream", "--format", "{{json .}}"], timeout=20)
            stats = {}
            for line in out.splitlines() if code == 0 else []:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                used, _, limit = s.get("MemUsage", "").partition("/")
                stats[s.get("ID")] = (float(s.get("CPUPerc", "0").rstrip("%") or 0), parse_size(used), parse_size(limit))
            for c in containers:
                if c["id"] in stats:
                    c["cpu"], c["mem"], c["mem_limit"] = stats[c["id"]]
        upd = {"running": True, "error": None, "containers": containers, "t": time.time()}
        if include_df:
            code, out, _ = run([d, "system", "df", "--format", "{{json .}}"], timeout=60)
            df = {}
            for line in out.splitlines() if code == 0 else []:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                key = {"Images": "images", "Containers": "containers", "Local Volumes": "volumes",
                       "Build Cache": "build_cache"}.get(r.get("Type"))
                if key:
                    df[key] = {"size": parse_size(r.get("Size")), "reclaimable": parse_size(r.get("Reclaimable")),
                               "count": int(r.get("TotalCount") or 0), "active": int(r.get("Active") or 0)}
            code, out, _ = run([d, "info", "--format", "{{.MemTotal}} {{.NCPU}} {{.ServerVersion}}"], timeout=15)
            parts = out.split()
            upd["df"], upd["df_t"] = df or None, time.time()
            upd["vm"] = {"mem_limit": int(parts[0]), "cpus": int(parts[1]), "version": parts[2]} if code == 0 and len(parts) == 3 else None
        with self.lock:
            self.docker.update(upd)

    def docker_loop(self):
        while True:
            active = self.watching()
            df_age = time.time() - self.docker["df_t"]
            try:
                if active:
                    self.docker_refresh(include_df=df_age > 60)
                elif df_age > 300:      # keep the terminal line's cache figure roughly fresh
                    self.docker_refresh(include_df=True)
            except Exception as e:
                print("\ndocker error: %r" % e, file=sys.stderr)
            self.docker_wake.wait(self.docker_interval if active else 60)
            self.docker_wake.clear()

    # ── views ──
    def snapshot(self, since=0.0):
        with self.lock:
            s = dict(self.state)
            s["host"] = self.host
            s["apps"] = self.apps
            s["apps_t"] = self.apps_t
            s["history"] = [h for h in self.history if h[0] > since]
            s["docker"] = dict(self.docker)
            s["self"] = dict(self.self_stats)
            s["interval"] = self.interval
            s["caches"] = dict(self.caches)
            s["orphans"] = {"count": len(self.orphans), "mem": sum(p["mem"] for p in self.orphans),
                            "cpu": round(sum(p["cpu"] for p in self.orphans), 2)}
            s["home"] = HOME
        s["jobs"] = self.jobs.list()
        return s

    def group_procs(self, name):
        with self.lock:
            procs = sorted(self.procs.get(name, []), key=lambda p: (p["cpu"], p["mem"]), reverse=True)[:40]
            procs = [dict(p) for p in procs]
        if procs:   # fetch full command lines lazily, only for the group being inspected
            code, out, _ = run(["ps", "-o", "pid=,args=", "-p", ",".join(str(p["pid"]) for p in procs)], timeout=10)
            args = {}
            for line in out.splitlines():
                pid, _, a = line.strip().partition(" ")
                args[pid] = a.strip()
            for p in procs:
                p["args"] = args.get(str(p["pid"]), p["path"])[:400]
        return procs


# ───────────────────────────── demo mode ─────────────────────────────
# `--demo` serves invented data, so screenshots never show a real machine and the UI can be
# worked on safely. Nothing in this section reads processes, runs commands or changes anything.

DEMO_HOME = "/Users/demo"


def _wave(t, period, lo, hi, phase=0.0):
    return lo + (hi - lo) * (0.5 + 0.5 * math.sin(2 * math.pi * t / period + phase))


# name, processes, % of machine CPU, memory GB, quittable app, yours (not system), sample command lines
DEMO_APPS = [
    ("node", 14, 7.9, 3.4, False, True, [
        "node /Users/demo/code/shop-web/node_modules/.bin/next dev -p 3000",
        "node /Users/demo/code/shop-web/node_modules/.bin/vitest --watch",
        "node /Users/demo/code/admin/node_modules/.bin/vite --port 5173",
        "npm exec @modelcontextprotocol/server-filesystem /Users/demo/code",
        "node /Users/demo/code/shop-web/node_modules/typescript/lib/tsserver.js"]),
    ("Docker VM", 1, 4.6, 6.2, False, True, ["com.apple.Virtualization.VirtualMachine"]),
    ("Claude Code", 4, 5.8, 2.1, False, True, [
        "claude --continue", "claude", "claude -p \"fix the flaky checkout test\"", "claude --resume"]),
    ("Google Chrome", 41, 3.2, 4.6, True, True, [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "Google Chrome Helper (Renderer) --type=renderer"]),
    ("Cursor", 11, 1.9, 2.4, True, True, ["/Applications/Cursor.app/Contents/MacOS/Cursor", "Cursor Helper (Plugin)"]),
    ("chrome-headless-shell", 6, 3.1, 1.1, False, True, ["chrome-headless-shell --headless --remote-debugging-pipe"]),
    ("Codex CLI", 2, 2.4, 0.8, False, True, ["codex", "codex exec \"update the README\""]),
    ("python3.12", 3, 1.2, 0.5, False, True, ["python3.12 -m uvicorn api.main:app --reload", "python3.12 -m pytest -x"]),
    ("Docker Desktop", 9, 0.9, 0.6, True, True, ["/Applications/Docker.app/Contents/MacOS/com.docker.backend"]),
    ("Slack", 6, 0.1, 0.9, True, True, ["/Applications/Slack.app/Contents/MacOS/Slack"]),
    ("Spotify", 4, 0.1, 0.6, True, True, ["/Applications/Spotify.app/Contents/MacOS/Spotify"]),
    ("kernel_task", 1, 3.4, 0.1, False, False, ["kernel_task"]),
    ("WindowServer", 1, 2.1, 0.2, False, False, ["/System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer"]),
    ("mds_stores", 1, 0.8, 0.1, False, False, ["/System/Library/Frameworks/CoreServices.framework/mds_stores"]),
]
DEMO_ORPHANS = ["node /Users/demo/code/api/node_modules/.bin/tsx watch src/server.ts",
                "node /Users/demo/code/shop-web/node_modules/.bin/playwright test --ui"]


class DemoMonitor(Monitor):
    demo = True

    def __init__(self, interval, docker_interval):
        super().__init__(interval, docker_interval)
        self.jobs = Jobs(self._job_done, measure=False)
        self.ncpu = 10
        self.host = {"name": "demo-mac", "chip": "Apple M5", "cores": 10, "pcores": 4, "ecores": 6,
                     "mem_total": 32 * GB, "os": "macOS 26.1"}
        self.self_stats = {"cpu": 0.08, "rss": 22 * 1024 ** 2}
        self.rng = random.Random(7)
        now = time.time()
        for i in range(HISTORY_SECONDS // 3, 0, -1):     # 15 minutes of believable history
            self._fake_sample(now - i * 3)
        self._fake_world(now)

    def _fake_sample(self, t):
        r = self.rng
        user = _wave(t, 240, 22, 46) + r.uniform(-4, 4)
        system = _wave(t, 170, 6, 11, 1.3) + r.uniform(-1.5, 1.5)
        cores = [round(max(2.0, min(100.0, (user + system) * f + r.uniform(-8, 8))), 1)
                 for f in (1.7, 1.6, 1.5, 1.4, 0.9, 0.8, 0.7, 0.6, 0.5, 0.5)]
        app, compressed = _wave(t, 600, 12.6, 14.2) * GB, _wave(t, 420, 6.2, 7.8, 0.7) * GB
        wired, cached, swap, total = 4.6 * GB, 4.9 * GB, 3.1 * GB, 32 * GB
        level = 2 if compressed > 7.2 * GB else 1
        free_pct = 31 if level == 2 else 38
        mem = {"total": total, "app": app, "wired": wired, "compressed": compressed, "cached": cached,
               "used": app + wired + compressed, "free": max(total - app - wired - compressed - cached, 0),
               "pressure_level": level, "free_pct": free_pct, "swap_total": 4 * GB, "swap_used": swap,
               "swapins": 0, "swapouts": r.choice([0, 0, 0, 180 * 1024]), "pageins": 0, "compressions": r.uniform(20, 90) * 1024 ** 2}
        self.state = {"t": t, "cpu": {"total": round(user + system, 1), "user": round(user, 1), "system": round(system, 1), "cores": cores},
                      "load": [7.8, 8.4, 8.1], "mem": mem, "disk": {"total": 994 * 10 ** 9, "free": 38 * 10 ** 9}}
        self.history.append([round(t, 1), round(user, 1), round(system, 1), round(app / GB, 3), round(wired / GB, 3),
                             round(compressed / GB, 3), round(cached / GB, 3), round(swap / GB, 3), level, free_pct])

    def _fake_world(self, now):
        pid = iter(range(41000, 49000, 7))
        apps, procs = [], {}
        for name, count, cpu, mem_gb, quittable, own, cmds in DEMO_APPS:
            apps.append({"name": name, "count": count, "cpu": cpu, "mem": int(mem_gb * GB), "quittable": quittable,
                         "own": own, "killable": own and name != "Docker VM"})
            procs[name] = [{"pid": next(pid), "cpu": round(cpu / min(count, 6), 2), "cores": round(cpu / min(count, 6) / 10, 2),
                            "mem": int(mem_gb * GB / min(count, 6)), "path": cmds[i % len(cmds)].split(" ")[0],
                            "args": cmds[i % len(cmds)], "own": own} for i in range(min(count, 6))]
        apps.append({"name": "rigtop (this tool)", "count": 1, "cpu": 0.08, "mem": 22 * 1024 ** 2,
                     "quittable": False, "own": True, "killable": False})
        self.orphans = [{"pid": next(pid), "cpu": 0.4, "cores": 0.04, "mem": int(m * 1024 ** 2), "path": "node", "args": a, "own": True}
                        for a, m in zip(DEMO_ORPHANS, (310, 420))]
        procs["__orphans__"] = self.orphans
        self.apps, self.procs, self.apps_t = apps, procs, now

        def ctr(cid, name, image, project, state, status, cpu=0.0, mem_mb=0):
            return {"id": cid, "name": name, "image": image, "state": state, "status": status, "ports": "",
                    "project": project, "cpu": cpu, "mem": mem_mb * 1024 ** 2, "mem_limit": 8 * GB}
        self.docker = {"available": True, "running": True, "error": None, "t": now, "df_t": now,
                       "vm": {"mem_limit": 8 * GB, "cpus": 6, "version": "28.0.1"},
                       "containers": [
                           ctr("a1b2c3d4e5f6", "shop-api-1", "shop-api:dev", "shop", "running", "Up 2 hours", 6.8, 410),
                           ctr("b2c3d4e5f6a1", "shop-postgres-1", "postgres:16", "shop", "running", "Up 2 hours", 1.2, 180),
                           ctr("c3d4e5f6a1b2", "shop-redis-1", "redis:7", "shop", "running", "Up 2 hours", 0.4, 12),
                           ctr("d4e5f6a1b2c3", "shop-worker-1", "shop-worker:dev", "shop", "exited", "Exited (137) 40 minutes ago"),
                           ctr("e5f6a1b2c3d4", "analytics-clickhouse-1", "clickhouse/clickhouse-server:24.8", "analytics", "running", "Up 5 hours", 3.1, 1310),
                           ctr("f6a1b2c3d4e5", "mailpit", "axllent/mailpit", None, "running", "Up 3 days", 0.1, 25)],
                       "df": {"build_cache": {"size": int(14.2 * GB), "reclaimable": int(11.8 * GB), "count": 214, "active": 0},
                              "images": {"size": int(9.4 * GB), "reclaimable": int(3.1 * GB), "count": 23, "active": 6},
                              "volumes": {"size": int(4.2 * GB), "reclaimable": int(2.6 * GB), "count": 17, "active": 5},
                              "containers": {"size": 310 * 1024 ** 2, "reclaimable": 120 * 1024 ** 2, "count": 6, "active": 5}}}
        sizes = {"go-build": 41.3, "uv": 5.1, "npm": 3.2, "xcode": 7.4, "go-mod": 2.2, "hf": 3.8, "playwright": 1.1, "pip": 0.6, "brew": 0.4}
        self.caches = {"t": now, "scanning": False,
                       "targets": sorted([{"id": t["id"], "label": t["label"], "path": t["path"].replace(HOME, DEMO_HOME), "safe": t["safe"],
                                           "hint": t["hint"], "size": int(sizes[t["id"]] * GB)} for t in CACHE_TARGETS if t["id"] in sizes],
                                         key=lambda c: c["size"], reverse=True),
                       "app_caches": [{"id": "appcache:" + n, "label": n, "path": DEMO_HOME + "/Library/Caches/" + n, "size": int(g * GB)}
                                      for n, g in (("Google", 2.4), ("com.spotify.client", 1.3), ("Cursor", 0.9), ("com.tinyspeck.slackmacgap", 0.5))]}

        def folder(path, items):
            return {"t": now, "total": int(sum(g for _, g in items) * GB),
                    "items": [{"name": n, "path": path + "/" + n, "size": int(g * GB)} for n, g in items]}
        self.du = {DEMO_HOME: folder(DEMO_HOME, [("Library", 312), ("code", 188), ("Movies", 96), ("Downloads", 64), (".cache", 48),
                                                 (".ollama", 41), ("go", 22), (".docker", 9), ("Documents", 7)]),
                   DEMO_HOME + "/code": folder(DEMO_HOME + "/code", [("shop-web", 61), ("ml-experiments", 54), ("admin", 31), ("api", 24), ("old-prototypes", 18)]),
                   DEMO_HOME + "/Library": folder(DEMO_HOME + "/Library", [("Caches", 63), ("Containers", 118), ("Application Support", 71),
                                                                         ("Developer", 46), ("Group Containers", 14)])}

    def sample(self, with_procs):
        with self.lock:
            self._fake_sample(time.time())
            for a in self.apps:     # a little movement so the dashboard looks alive
                if a["cpu"] > 0.5:
                    a["cpu"] = round(max(0.3, a["cpu"] * self.rng.uniform(0.85, 1.15)), 2)

    def docker_loop(self):
        while True:
            with self.lock:
                for c in self.docker["containers"]:
                    if c["state"] == "running" and c["cpu"] > 0.2:
                        c["cpu"] = round(max(0.1, c["cpu"] * self.rng.uniform(0.8, 1.2)), 1)
                self.docker["t"] = time.time()
            time.sleep(self.docker_interval)

    def cache_loop(self):
        pass

    def _job_done(self, key):
        self.wake.set()

    def group_procs(self, name):
        with self.lock:
            return [dict(p) for p in self.procs.get(name, [])]

    def snapshot(self, since=0.0):
        s = super().snapshot(since)
        s["home"] = DEMO_HOME
        return s

    def demo_du(self, path):
        path = path if (path or "").startswith(DEMO_HOME) else DEMO_HOME
        with self.lock:
            r = self.du.get(path)
        return dict(r, path=path) if r else {"path": path, "pending": True}


def demo_action(mon, body):
    """Every button "works" in demo mode, and nothing is ever changed."""
    kind, msg = body.get("kind"), "demo mode: nothing was changed"
    key = {"docker": "docker:%s" % body.get("action"), "cache": "cache:%s" % body.get("id"), "clean-selected": "clean-selected",
           "trash": "trash", "scan": "scan:%s" % body.get("path"), "scan-caches": "cache-scan"}.get(kind)
    if body.get("kind") == "cache" and str(body.get("id", "")).startswith("appcache:"):
        key = body["id"]
    if key is None:
        return 200, {"ok": True, "message": msg}

    def fn():
        time.sleep(2)
        return True, msg
    jid = mon.jobs.start(key, "Demo: " + kind.replace("-", " "), fn)
    return 200, {"ok": True, "job": jid, "message": msg}


# ───────────────────────────── actions (strict allow-list) ─────────────────────────────

DOCKER_ACTIONS = {
    "builder-prune-old": (["builder", "prune", "-af", "--filter", "until=24h"], "Clearing Docker build cache (older than 24h)"),
    "builder-prune-all": (["builder", "prune", "-af"], "Clearing all Docker build cache"),
    "image-prune-dangling": (["image", "prune", "-f"], "Removing dangling images"),
    "image-prune-unused": (["image", "prune", "-af"], "Removing unused images"),
    "container-prune": (["container", "prune", "-f"], "Removing stopped containers"),
    "volume-prune": (["volume", "prune", "-f"], "Removing anonymous volumes"),
    "volume-prune-all": (["volume", "prune", "-af"], "Removing all unused volumes"),
    "desktop-restart": (["desktop", "restart"], "Restarting Docker Desktop"),
}
CONTAINER_VERBS = {"stop", "start", "restart", "rm"}
ID_RE = re.compile(r"^[0-9a-f]{12,64}$")


def _signal_pids(mon, pids, sig):
    sent = failed = 0
    for pid in pids:
        try:
            os.kill(pid, sig)
            sent += 1
        except OSError:
            failed += 1
    mon.wake.set()
    return sent, failed


def _docker_job(mon, action_id):
    args, label = DOCKER_ACTIONS[action_id]

    def fn():
        code, out, err = run([mon._docker_bin()] + args, timeout=900)
        tail = (out.strip().splitlines() or ["done"])[-1]
        return code == 0, tail if code == 0 else (err.strip() or out.strip())[-400:]
    return "docker:" + action_id, label, fn


def _cache_job(mon, cid):
    if cid.startswith("appcache:"):
        name = cid.split(":", 1)[1]
        path = os.path.join(APP_CACHES, name)
        if "/" in name or name in ("", ".", "..") or os.path.islink(path) or not os.path.isdir(path):
            return None
        return cid, "Cleaning %s cache" % name, lambda: empty_dir(path)
    t = TARGETS_BY_ID.get(cid)
    if not t or not os.path.isdir(t["path"]):
        return None
    return "cache:" + cid, "Cleaning " + t["label"], lambda: empty_dir(t["path"], make_writable=t.get("writable", False))


def do_action(mon, body):
    kind = body.get("kind")

    def started(jid, label):
        if jid is None:
            return 409, {"ok": False, "message": "already running: " + label}
        return 200, {"ok": True, "job": jid, "message": label + "…"}

    if kind == "docker":
        if body.get("action") not in DOCKER_ACTIONS:
            return 400, {"ok": False, "message": "unknown docker action"}
        key, label, fn = _docker_job(mon, body["action"])
        return started(mon.jobs.start(key, label, fn), label)

    if kind == "cache":
        spec = _cache_job(mon, str(body.get("id", "")))
        if not spec:
            return 400, {"ok": False, "message": "unknown cache"}
        return started(mon.jobs.start(*spec), spec[1])

    if kind == "clean-selected":
        ids = [str(i) for i in body.get("ids") or []]
        docker_ids = [i.split(":", 1)[1] for i in ids if i.startswith("docker:") and i.split(":", 1)[1] in DOCKER_ACTIONS]
        specs = [s for s in (_cache_job(mon, i) for i in ids if not i.startswith("docker:")) if s]
        if not specs and not docker_ids:
            return 400, {"ok": False, "message": "nothing selected"}

        def fn():
            done, problems = 0, []
            for a in docker_ids:
                ok, msg = _docker_job(mon, a)[2]()
                done += ok
                if not ok:
                    problems.append(msg)
            for _, label, f in specs:
                ok, msg = f()
                done += ok
                if not ok:
                    problems.append("%s: %s" % (label, msg))
            return not problems, "cleaned %d item(s)" % done + ("; " + "; ".join(problems) if problems else "")
        label = "Cleaning %d item(s)" % (len(specs) + len(docker_ids))
        return started(mon.jobs.start("clean-selected", label, fn), label)

    if kind == "scan-caches":
        def fn():
            mon.scan_caches()
            return True, "caches measured"
        return started(mon.jobs.start("cache-scan", "Measuring caches", fn), "Measuring caches")

    if kind == "trash":
        def fn():
            code, _, err = run(["osascript", "-e", 'tell application "Finder" to empty trash'], timeout=1800)
            return code == 0, "Trash emptied" if code == 0 else err.strip()[-300:]
        return started(mon.jobs.start("trash", "Emptying Trash", fn), "Emptying Trash")

    if kind == "scan":
        path = under_home(body.get("path") or HOME, allow_home=True)
        if not path or not os.path.isdir(path):
            return 400, {"ok": False, "message": "can only scan folders in your home"}
        label = "Measuring " + path.replace(HOME, "~")
        return started(mon.jobs.start("scan:" + path, label, lambda: mon.scan_folder(path)), label)

    if kind == "reveal":
        path = under_home(body.get("path") or "", allow_home=True)
        if not path or not os.path.exists(path):
            return 400, {"ok": False, "message": "not found"}
        code, _, err = run(["open", "-R", path], timeout=10)
        return 200, {"ok": code == 0, "message": "revealed in Finder" if code == 0 else err.strip()}

    if kind == "container":
        verb, ids = body.get("action"), body.get("ids") or []
        if verb not in CONTAINER_VERBS or not ids or not all(isinstance(i, str) and ID_RE.match(i) for i in ids):
            return 400, {"ok": False, "message": "bad container request"}
        code, out, err = run([mon._docker_bin(), verb] + ids, timeout=120)
        mon.docker_wake.set()
        return 200, {"ok": code == 0, "message": "%s: %d container(s)" % (verb, len(ids)) if code == 0 else err.strip()[-400:]}

    if kind == "process":
        try:
            pid = int(body.get("pid"))
        except (TypeError, ValueError):
            return 400, {"ok": False, "message": "bad pid"}
        sig = signal.SIGKILL if body.get("action") == "kill" else signal.SIGTERM
        if pid <= 1 or pid in mon.protected:
            return 400, {"ok": False, "message": "refusing to signal rigtop or the terminal running it"}
        code, out, _ = run(["ps", "-o", "uid=,comm=", "-p", str(pid)], timeout=5)
        uid, _, comm = out.strip().partition(" ")
        if code != 0 or not uid:
            return 200, {"ok": True, "message": "process %d already exited" % pid}
        if int(uid) != mon.uid:
            return 403, {"ok": False, "message": "pid %d belongs to another user (system process) — not touching it" % pid}
        if body.get("path") and comm.strip() != body["path"]:     # guard against pid reuse
            return 409, {"ok": False, "message": "pid %d is now a different program; refresh and retry" % pid}
        sent, _ = _signal_pids(mon, [pid], sig)
        return 200, {"ok": bool(sent), "message": "sent %s to %d" % (sig.name, pid)}

    if kind == "group":         # end every process of one app/group that we own
        name = body.get("name", "")
        with mon.lock:
            ok = any(g["name"] == name and g["killable"] for g in mon.apps)
            pids = [p["pid"] for p in mon.procs.get(name, []) if p["own"]]
        if not ok or not pids:
            return 400, {"ok": False, "message": "nothing to end in %s" % name}
        sent, failed = _signal_pids(mon, pids, signal.SIGTERM)
        return 200, {"ok": sent > 0, "message": "ended %d %s process(es)" % (sent, name) + (", %d failed" % failed if failed else "")}

    if kind == "orphans":
        want = {int(p) for p in body.get("pids") or [] if str(p).isdigit()}
        with mon.lock:
            pids = [p["pid"] for p in mon.orphans if not want or p["pid"] in want]
        if not pids:
            return 200, {"ok": True, "message": "no orphaned processes left"}
        sent, failed = _signal_pids(mon, pids, signal.SIGTERM)
        return 200, {"ok": sent > 0, "message": "ended %d orphaned process(es)" % sent + (", %d failed" % failed if failed else "")}

    if kind == "app":
        name = body.get("name", "")
        with mon.lock:
            ok = any(g["name"] == name and g["quittable"] for g in mon.apps)
        if not ok:
            return 400, {"ok": False, "message": "not a quittable app"}
        code, _, err = run(["osascript", "-e", "on run argv", "-e", "tell application (item 1 of argv) to quit",
                            "-e", "end run", name], timeout=30)
        mon.wake.set()
        return 200, {"ok": code == 0, "message": "asked %s to quit" % name if code == 0 else err.strip()[-300:]}

    return 400, {"ok": False, "message": "unknown action"}


# ───────────────────────────── http ─────────────────────────────

def make_handler(mon, key, port):
    allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
    allowed_origins = {"http://" + h for h in allowed_hosts}
    # Every API call must carry the per-run key in X-Rigtop-Key. The key reaches the browser only
    # in the URL fragment (#k=…), which browsers never send to any server, and the page keeps it
    # in storage scoped to this exact origin (host *and* port). No cookies: those would be sent to
    # every port on 127.0.0.1. A custom header also can't be sent cross-site without a CORS
    # preflight, which we never grant, so the key doubles as CSRF protection.

    def page():     # static and secret-free; read per load so edits show up on refresh
        with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
            return f.read()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):    # blocks DNS-rebinding: only answer to our own address
            return self.headers.get("Host") in allowed_hosts

        def _signed_in(self):
            return secrets.compare_digest(self.headers.get("X-Rigtop-Key", ""), key)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                return self._send(200, page(), "text/html; charset=utf-8")
            if not self._signed_in():
                return self._send(401, {"error": "not signed in: open the link rigtop printed in your terminal"})
            if u.path == "/api/state":
                try:
                    every = float(q.get("every", ["0"])[0])
                    since = float(q.get("since", ["0"])[0] or 0)
                except ValueError:
                    return self._send(400, {"error": "bad number"})
                mon.touch(every)
                return self._send(200, mon.snapshot(since if math.isfinite(since) else 0.0))
            if u.path == "/api/procs":
                return self._send(200, mon.group_procs(q.get("group", [""])[0]))
            if u.path == "/api/du" and mon.demo:
                return self._send(200, mon.demo_du(q.get("path", [""])[0]))
            if u.path == "/api/du":
                path = under_home(q.get("path", [HOME])[0] or HOME, allow_home=True) or HOME
                with mon.lock:
                    r = mon.du.get(path)
                return self._send(200, dict(r, path=path) if r else {"path": path, "pending": True})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self._host_ok() or not self._signed_in() or (origin and origin not in allowed_origins):
                return self._send(403, {"ok": False, "message": "forbidden"})
            if urlparse(self.path).path != "/api/action":
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 65536)) or b"{}")
            except ValueError:
                return self._send(400, {"ok": False, "message": "bad json"})
            code, resp = (demo_action if mon.demo else do_action)(mon, body)
            self._send(code, resp)

    return H


# ───────────────────────────── terminal ─────────────────────────────

def ansi(on):
    def c(code, s):
        return "\033[%sm%s\033[0m" % (code, s) if on else s
    return c


def status_line(mon, url, color):
    c = ansi(color)
    s = mon.snapshot(time.time())
    if "cpu" not in s:
        return "rigtop warming up…"
    cpu, m = s["cpu"]["total"], s["mem"]
    bar_n = int(round(cpu / 12.5))
    bar = "▮" * bar_n + "▯" * (8 - bar_n)
    cpu_col = "32" if cpu < 60 else "33" if cpu < 85 else "31"
    lvl = {1: ("32", "normal"), 2: ("33", "WARN"), 4: ("31", "CRITICAL")}.get(m["pressure_level"], ("37", "?"))
    disk_free = s["disk"]["free"]
    parts = [
        c("1;36", "● rigtop") + " " + c("4", url),
        "CPU " + c(cpu_col, "%3.0f%% %s" % (cpu, bar)),
        "MEM %s/%s" % (("%.1f" % (m["used"] / GB)), "%.0f GB" % (m["total"] / GB)),
        "pressure " + c(lvl[0], lvl[1]),
        "swap " + fmt_gb(m["swap_used"]),
        "disk " + c("31" if disk_free < 30 * GB else "33" if disk_free < 80 * GB else "32", fmt_gb(disk_free) + " free"),
    ]
    d = s["docker"]
    if d.get("running"):
        up = sum(1 for x in d["containers"] if x["state"] == "running")
        bc = (d.get("df") or {}).get("build_cache")
        parts.append("docker %d up" % up + (" · cache %s" % fmt_gb(bc["size"]) if bc else ""))
    running = [j for j in s["jobs"] if j["state"] == "running"]
    if running:
        parts.append(c("36", running[-1]["label"] + "…"))
    parts.append(c("2", "dashboard " + ("open" if mon.watching() else "idle")))
    line = "  ".join(parts)
    width = shutil.get_terminal_size((160, 20)).columns
    visible = re.sub(r"\033\[[\d;]*m", "", line)
    if len(visible) > width - 1:          # degrade gracefully on narrow terminals
        line = re.sub(r"\033\[[\d;]*m", "", line)[: width - 2] + "…"
    return line


def print_once(mon):
    mon.sample(with_procs=False)
    time.sleep(1.0)
    mon.sample(with_procs=True)
    mon.docker_refresh(include_df=True)
    s = mon.snapshot()
    m, h = s["mem"], s["host"]
    print("%s · %s · %d cores · %s" % (h["name"], h["chip"], h["cores"], fmt_gb(h["mem_total"])))
    print("CPU %.0f%% (user %.0f%%, system %.0f%%)  load %s" % (s["cpu"]["total"], s["cpu"]["user"], s["cpu"]["system"],
                                                              " ".join(map(str, s["load"]))))
    print("Memory %s used of %s  (app %s, wired %s, compressed %s)  pressure %s  swap %s" % (
        fmt_gb(m["used"]), fmt_gb(m["total"]), fmt_gb(m["app"]), fmt_gb(m["wired"]), fmt_gb(m["compressed"]),
        {1: "normal", 2: "WARN", 4: "CRITICAL"}.get(m["pressure_level"], "?"), fmt_gb(m["swap_used"])))
    print("Disk %s free of %s" % (fmt_gb(s["disk"]["free"]), fmt_gb(s["disk"]["total"])))
    print("\nTop apps                          CPU      Memory")
    for g in s["apps"][:12]:
        print("  %-30s %6.1f%%  %9s   (%d proc)" % (g["name"][:30], g["cpu"], fmt_gb(g["mem"]), g["count"]))
    if s["orphans"]["count"]:
        print("\nOrphaned dev processes: %d using %s (end them from the dashboard)" % (s["orphans"]["count"], fmt_gb(s["orphans"]["mem"])))
    d = s["docker"]
    if d.get("running") and d.get("df"):
        print("\nDocker disk          size   reclaimable")
        for k, label in (("build_cache", "Build cache"), ("images", "Images"), ("volumes", "Volumes"), ("containers", "Containers")):
            r = d["df"].get(k)
            if r:
                print("  %-14s %9s   %9s" % (label, fmt_gb(r["size"]), fmt_gb(r["reclaimable"])))
    elif d.get("available"):
        print("\nDocker: " + (d.get("error") or "not running"))
    print("\nDeveloper caches (safe to clean)")
    for t in CACHE_TARGETS:
        if os.path.isdir(t["path"]):
            print("  %-22s %9s   %s" % (t["label"], fmt_gb(du_k(t["path"]).get(t["path"], 0)), t["path"].replace(HOME, "~")))


def main():
    ap = argparse.ArgumentParser(description="Featherweight load monitor and cleaner for macOS.")
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between samples while the dashboard is open")
    ap.add_argument("--docker-interval", type=float, default=10.0, help="seconds between docker stats while open")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    ap.add_argument("--once", action="store_true", help="print a snapshot and exit")
    ap.add_argument("--demo", action="store_true", help="serve made-up data (for screenshots and UI work); every button is a no-op")
    a = ap.parse_args()
    if sys.platform != "darwin":
        sys.exit("rigtop reads macOS kernel stats and only runs on macOS.")

    if a.demo and a.once:
        sys.exit("--demo only applies to the dashboard, not --once.")
    mon = (DemoMonitor if a.demo else Monitor)(max(a.interval, 1.0), max(a.docker_interval, 3.0))
    if a.once:
        return print_once(mon)

    key = secrets.token_urlsafe(24)      # per-run secret, only ever in the link we open/print
    server = None
    for port in range(a.port, a.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(mon, key, port))
            break
        except OSError:
            continue
    if server is None:
        sys.exit("no free port near %d" % a.port)
    server.daemon_threads = True
    url = "http://127.0.0.1:%d" % port
    link = "%s/#k=%s" % (url, key)

    mon.sample(with_procs=False)
    for target in (mon.loop, mon.docker_loop, mon.cache_loop, server.serve_forever):
        threading.Thread(target=target, daemon=True).start()

    tty = sys.stdout.isatty()
    print(ansi(tty)("1", "rigtop") + (" demo (made-up data, buttons do nothing) → " if a.demo else " is watching this Mac → ") + link)
    print(ansi(tty)("2", "Sampling is cheap kernel reads only, until the dashboard is open. Ctrl+C to quit.\n"), flush=True)
    if not a.no_open:
        webbrowser.open(link)
    try:
        while True:
            if tty:
                sys.stdout.write("\r\033[K" + status_line(mon, url, True))
                sys.stdout.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
