# rigtop

**`top` for dev machines that run AI coding agents and Docker all day.**
A live terminal status line and a local web dashboard showing what's straining your machine, with one-tap fixes to free memory and disk.

![license: MIT](https://img.shields.io/badge/license-MIT-blue) ![python: 3.9+](https://img.shields.io/badge/python-3.9%2B-blue) ![platform: macOS](https://img.shields.io/badge/platform-macOS-lightgrey) ![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)

```bash
git clone https://github.com/alonstech/rigtop && cd rigtop && ./rigtop
```

No install, no `pip`, no Homebrew. One Python file and one HTML file.

<!-- TODO: add docs/screenshot.png (taken in demo mode so it shows no personal data) -->

---

## Why this exists

A typical day now looks like this: three or four coding agents running in parallel (Claude Code, Codex, Gemini, Cursor…), each starting its own dev servers, test runners and headless browsers, while a Docker stack runs alongside. On a 32 GB laptop that adds up quickly:

- **Memory quietly runs out.** macOS compresses RAM, then swaps to disk, and everything gets sluggish long before Activity Monitor looks alarming.
- **Docker's VM keeps memory it no longer needs.** Containers use 300 MB while the VM holds 7 GB of Linux page cache.
- **Agents leave processes behind.** A crashed or cancelled run leaves `node`, `python` and `vite` processes owned by no one, still holding memory and ports.
- **Caches grow without anyone noticing.** The machine rigtop was built on had **103 GB of Go build cache**, 10 GB of Docker build cache and 4.5 GB of uv cache, and only 27 GB of free disk.

The usual tools each cover part of this. Activity Monitor, `btop` and Glances *show* the load. CleanMyMac, Mole and kondo *clean*, but from a separate command or app. None of them knows that `node` with a dead parent is probably a leftover agent, or that the Docker VM is the real memory hog.

rigtop puts the picture and the fix on one screen. You see what's wrong and fix it with two taps, without leaving the dashboard and without adding load to a machine that's already struggling.

## Quick start

```bash
git clone https://github.com/alonstech/rigtop
cd rigtop
./rigtop
```

Your browser opens at `http://127.0.0.1:7777`, and the terminal shows a live line:

```
● rigtop http://127.0.0.1:7777  CPU  47% ▮▮▮▮▯▯▯▯  MEM 26.1/32 GB  pressure normal  swap 2.9 GB  disk 41.0 GB free  docker 3 up · cache 9.8 GB  dashboard open
```

**Requirements:** macOS with Python 3.9+. The Python that ships with Xcode Command Line Tools (`/usr/bin/python3`) is enough; if you don't have it, run `xcode-select --install`. The launcher always uses that system Python, so asdf, pyenv or an active virtualenv can't get in the way. Docker is optional.

To run it from anywhere:

```bash
ln -s "$(pwd)/rigtop" ~/.local/bin/rigtop
```

| Option | Default | What it does |
|---|---|---|
| `--no-open` | off | Don't open a browser tab |
| `--once` | off | Print a text snapshot and exit. Handy inside an agent session or a script |
| `--port N` | `7777` | Port to listen on (moves up automatically if it's taken) |
| `--interval S` | `3` | Seconds between samples while the dashboard is open |
| `--docker-interval S` | `10` | Seconds between `docker stats` calls while the dashboard is open |

## What you get

**Four tiles, each with an action.** Every tile ends in a short list of the things worth acting on:

| Tile | Shows | One-tap actions |
|---|---|---|
| **CPU** | Whole-machine %, per-core bars, load | The 3 busiest apps → Quit / End |
| **Memory pressure** | The kernel's own Normal / Elevated / Critical level, swap, swap rate | The 3 biggest memory users → Quit / End |
| **Memory** | App / Wired / Compressed / Cached / Free (Activity Monitor's math) | Orphaned dev processes → End all · Docker VM cache → Restart Docker · Idle apps holding RAM → Quit |
| **Disk** | Free space and % used | The 3 biggest cleanable items → Clean · **Clean all safe caches** |

**Free up disk space.** One list of everything that's safe to throw away, measured at background priority:

- **Developer caches:** Go build and modules, npm, uv, pip, Yarn, Bun, Homebrew, Cargo, Xcode DerivedData, iOS Simulator, Playwright, Gradle, Maven, Hugging Face.
- **Docker:** build cache, unused images, stopped containers.
- **App caches:** the biggest folders in `~/Library/Caches`.
- **Trash.**

Tick what you want and press **Clean selected**. Caches that tools rebuild on their own come pre-ticked.

**Where's the rest of your disk?** A read-only folder explorer for your home folder: measure, drill down, then Reveal in Finder. rigtop never deletes anything you browse here.

**What's using your Mac.** Processes grouped by app, so 37 Chrome helpers show as one "Google Chrome" and Claude Code, Codex and the Docker VM each get a line of their own. Expand any row to see full command lines, each with Quit and Force quit.

**Docker.** Disk usage with prune buttons, containers grouped by compose project with Stop / Restart / Start / Remove, and how much of your Mac's RAM the Docker VM really holds compared with what the containers report.

**Suggestions.** Plain-language notes such as *"Only 41 GB disk free. Biggest win: Go build cache holds 104 GB."* or *"Docker's VM holds 7.3 GB but containers use 344 MB."*, each with a button that goes straight to the fix.

**History.** 15 minutes of CPU, memory, swap and pressure, with hover tooltips.

## Built to be light

A monitor for an overloaded machine must not add to the load.

- **No dependencies.** Python standard library only.
- **No polling subprocesses for the basics.** CPU, memory, pressure and swap come straight from the Mach kernel through `ctypes`. Nothing runs `top` or `vm_stat`.
- **Heavier work only while you're looking.** `ps` and the Docker CLI run only while a dashboard tab is open *and visible*. A hidden tab stops polling entirely.
- **Background priority for disk work.** Cache measurement (`du`) and cleanups run under `taskpolicy -b`, macOS's lowest I/O and CPU tier, off the web thread.
- **Nothing written to disk.** History lives in memory.

Measured on an M5 MacBook Pro:

| | rigtop's CPU | rigtop's memory |
|---|---|---|
| Dashboard open (`ps` + `docker stats` included) | ≈ 0.1% of the machine | ~24 MB |
| Dashboard closed (terminal line only) | ≈ 0% | ~22 MB |

## Safety

rigtop can delete files and end processes, so it's deliberately conservative.

- **Two taps for quick actions.** The first tap arms the button (it turns red), the second within 3.5 s runs it. Anything that can lose data (unused Docker volumes, all unused images, emptying the Trash, restarting Docker) opens a dialog showing the exact command instead.
- **Fixed allow-lists.** Docker commands and cache folders come from fixed lists in the code. Cache cleaning only empties known folders inside your home folder and never follows symlinks.
- **Careful with processes.** rigtop only signals processes you own. It never touches macOS system agents (`/System`, `/usr`…), itself, or the terminal that launched it, and it checks a pid still belongs to the same program before signalling it. Coding agents and terminals are never suggested as "idle", because waiting for input looks idle.
- **Local only.** The server listens on `127.0.0.1` only and rejects requests with any other `Host` header (DNS-rebinding protection). Every action needs a per-run token embedded in the page, so no other website can trigger a cleanup.
- **No root.** No `sudo`, no privileged helpers.

## Similar tools

rigtop is small and focused. If these fit you better, use them:

- [**Mole**](https://github.com/tw93/Mole): a thorough macOS cleaner, uninstaller and monitor CLI.
- [**Glances**](https://github.com/nicolargo/glances): a cross-platform monitor with a web UI and Docker support.
- [**btop**](https://github.com/aristocratos/btop): a beautiful terminal resource monitor.
- [**kondo**](https://github.com/tbillington/kondo): cleans project build artifacts such as `node_modules` and `target/`.

rigtop's angle is the combination of a live view plus one-tap fixes, tuned for agent-heavy and Docker-heavy development machines.

## Platform support

| Platform | Status |
|---|---|
| macOS (Apple Silicon) | ✅ Supported, and where it's developed |
| macOS (Intel) | Should work (same kernel APIs); reports welcome |
| Linux | 🚧 Not yet. A great first contribution: `/proc/stat`, `/proc/meminfo` and `/proc/pressure/memory` cover everything needed |
| Windows | 💭 Not yet. Needs a Win32 backend; Docker Desktop runs in WSL2 there |

## Roadmap ideas

- Linux backend, then Windows
- Group processes by **agent session or worktree** ("this Claude session and its dev servers use 4 GB")
- `--demo` mode with synthetic data, for screenshots and UI work on any OS
- Find stale `node_modules` / `.venv` / `target/` folders in old projects
- Alerts: a macOS notification when pressure turns critical or disk drops below a threshold
- Per-agent token or cost overlays, for agents that expose them

## Contributing

Contributions are very welcome: bug reports, new cache locations, platform backends, UI polish and docs. Start with [CONTRIBUTING.md](CONTRIBUTING.md). It explains how the code is laid out, how to add a cleanup target, and the few rules that keep rigtop light and safe.

If rigtop freed some disk or rescued a laggy afternoon for you, a ⭐ helps other people find it.

## License

[MIT](LICENSE)
