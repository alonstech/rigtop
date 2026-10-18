"""Tests for rigtop's pure helpers. They use fixtures, not the live machine, so they run on macOS and Linux alike.

    python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rigtop  # noqa: E402

KB = 1024

PROC_STAT = """cpu  400 20 100 3000 50 5 5 0 0 0
cpu0 100 10 50 1500 25 3 2 0 0 0
cpu1 300 10 50 1500 25 2 3 1 0 0
intr 12345
ctxt 6789
"""

MEMINFO = """MemTotal:       16000000 kB
MemFree:         2000000 kB
MemAvailable:    6000000 kB
Buffers:          100000 kB
Cached:          4000000 kB
SwapTotal:       4000000 kB
SwapFree:        3000000 kB
Shmem:            500000 kB
SReclaimable:     400000 kB
SUnreclaim:       200000 kB
KernelStack:       20000 kB
PageTables:        80000 kB
Zswap:            300000 kB
"""

PSI = """some avg10=12.50 avg60=3.00 avg300=1.00 total=123456
full avg10=0.50 avg60=0.10 avg300=0.00 total=2345
"""


class LinuxParsers(unittest.TestCase):
    def test_cpu_ticks_per_core(self):
        # (user, system + irq + softirq + steal, idle + iowait, nice); the aggregate "cpu" line is skipped
        self.assertEqual(rigtop.parse_proc_stat_cpus(PROC_STAT), [(100, 55, 1525, 10), (300, 56, 1525, 10)])

    def test_cpu_ticks_short_lines(self):    # very old kernels report fewer columns
        self.assertEqual(rigtop.parse_proc_stat_cpus("cpu0 1 2 3 4\n"), [(1, 3, 4, 2)])

    def test_parse_kv(self):
        mi = rigtop.parse_kv(MEMINFO, KB)
        self.assertEqual(mi["MemTotal"], 16000000 * KB)
        self.assertEqual(rigtop.parse_kv("pswpin 12\npswpout 34\n"), {"pswpin": 12, "pswpout": 34})
        self.assertEqual(rigtop.parse_kv("Active(anon):  5 kB\n", KB), {"Active(anon)": 5 * KB})

    def test_parse_psi(self):
        self.assertEqual(rigtop.parse_psi(PSI), {"some": 12.5, "full": 0.5})
        self.assertEqual(rigtop.parse_psi(""), {})

    def test_memory_buckets_add_up(self):
        m = rigtop.linux_memory(rigtop.parse_kv(MEMINFO, KB), {}, zram=100000 * KB)
        self.assertEqual(m["cached"], (100000 + 4000000 - 500000 + 400000) * KB)
        self.assertEqual(m["wired"], (200000 + 20000 + 80000) * KB)
        self.assertEqual(m["compressed"], (300000 + 100000) * KB)
        self.assertEqual(m["app"] + m["wired"] + m["compressed"] + m["cached"] + m["free"], m["total"])
        self.assertEqual(m["used"], m["app"] + m["wired"] + m["compressed"])
        self.assertEqual(m["swap_used"], 1000000 * KB)
        self.assertEqual(m["free_pct"], 38)          # MemAvailable / MemTotal

    def test_pressure_levels(self):
        mi = rigtop.parse_kv(MEMINFO, KB)
        self.assertEqual(rigtop.linux_memory(mi, {"some": 0.0, "full": 0.0})["pressure_level"], 1)
        self.assertEqual(rigtop.linux_memory(mi, rigtop.parse_psi(PSI))["pressure_level"], 2)
        self.assertEqual(rigtop.linux_memory(mi, {"some": 80.0, "full": 20.0})["pressure_level"], 4)
        low = dict(mi, MemAvailable=500000 * KB)      # ~3% available is critical even without PSI
        self.assertEqual(rigtop.linux_memory(low, {})["pressure_level"], 4)


APP = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-firefox-1234.scope\n"
SESSION = "0::/user.slice/user-1000.slice/user@1000.service/session.slice/org.gnome.Shell@wayland.service\n"
INIT = "0::/user.slice/user-1000.slice/user@1000.service/init.scope\n"
CONTAINER = "0::/system.slice/docker-82c7b4859ae5c5533919eab2bcbe73f13fdf558b417219f45c5bcaf7b1339070.scope\n"
FLATPAK = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-com.slack.Slack-5678.scope\n"


class LinuxGrouping(unittest.TestCase):
    def group(self, path, cgroup=APP, vm="Virtual Machine"):
        return rigtop.group_of_linux(path, cgroup, vm)[0]

    def test_aliases_and_plain_names(self):
        self.assertEqual(self.group("/opt/google/chrome/chrome"), "Google Chrome")
        self.assertEqual(self.group("/snap/firefox/5000/usr/lib/firefox/firefox"), "Firefox")
        self.assertEqual(self.group("/home/u/.local/bin/claude"), "Claude Code")
        self.assertEqual(self.group("/usr/bin/node"), "node")

    def test_cgroup_based_groups(self):
        self.assertEqual(self.group("/usr/lib/postgresql/16/bin/postgres", CONTAINER), "Docker containers")
        self.assertEqual(self.group("/app/lib/slack/slack", FLATPAK), "Slack")
        self.assertEqual(self.group("/usr/bin/bwrap", FLATPAK), "Slack")
        self.assertEqual(self.group("[kworker/0:1]", ""), "Linux kernel")

    def test_docker_desktop_vm(self):
        self.assertEqual(self.group("/opt/docker-desktop/bin/qemu-system-x86_64", APP, "Docker VM"), "Docker VM")
        self.assertEqual(self.group("/usr/bin/qemu-system-x86_64", APP), "qemu-system-x86_64")

    def test_system_processes(self):
        self.assertTrue(rigtop.linux_is_system("/usr/bin/gnome-shell", SESSION))
        self.assertTrue(rigtop.linux_is_system("/usr/lib/systemd/systemd", INIT))
        self.assertTrue(rigtop.linux_is_system("/usr/libexec/gsd-color", APP))
        self.assertTrue(rigtop.linux_is_system("/usr/bin/pipewire", ""))        # by name, without systemd
        self.assertTrue(rigtop.linux_is_system("[kthreadd]", ""))
        self.assertFalse(rigtop.linux_is_system("/usr/bin/node", APP))
        self.assertFalse(rigtop.linux_is_system("/usr/bin/python3.12", "0::/user.slice/user-1000.slice/session-3.scope\n"))


class MacGrouping(unittest.TestCase):
    def test_app_bundles(self):
        g = rigtop.group_of
        self.assertEqual(g("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Docker VM"), ("Google Chrome", True))
        self.assertEqual(g("/Applications/Docker.app/Contents/MacOS/com.docker.backend", "Docker VM"), ("Docker Desktop", False))
        self.assertEqual(g("/System/Library/Frameworks/Virtualization.framework/com.apple.Virtualization.VirtualMachine",
                           "Docker VM"), ("Docker VM", False))
        self.assertEqual(g("/opt/homebrew/bin/node", "Docker VM"), ("node", False))


class Helpers(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(rigtop.parse_size("1.5GiB"), int(1.5 * 1024 ** 3))
        self.assertEqual(rigtop.parse_size("300MB"), 300 * 10 ** 6)
        self.assertEqual(rigtop.parse_size("12B"), 12)
        self.assertEqual(rigtop.parse_size(""), 0)

    def test_under_home(self):
        self.assertIsNone(rigtop.under_home("/etc"))
        self.assertIsNone(rigtop.under_home(rigtop.HOME))
        self.assertEqual(rigtop.under_home(rigtop.HOME, allow_home=True), rigtop.HOME)
        self.assertIsNone(rigtop.under_home(os.path.join(rigtop.HOME, "..", "..", "etc")))


if __name__ == "__main__":
    unittest.main()
