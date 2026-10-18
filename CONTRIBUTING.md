# Contributing to rigtop

Thanks for wanting to help! rigtop is deliberately small: one Python file, one HTML file and no dependencies. That keeps it easy to read and easy to change. This guide covers how it fits together, and the few rules that keep it light and safe.

## Ways to help

- **Report a bug or an odd number.** Open an issue with your OS version, chip, the output of `./rigtop --once`, and what you expected to see.
- **Add a cache location** a tool on your machine fills up. This is usually a 5-line change; see below.
- **Port it to Windows**, or report how the Linux backend does on your distro and desktop; see [Adding a platform](#adding-a-platform).
- **Improve the dashboard:** layout, accessibility, clearer wording.
- **Improve the docs**, or fix something that confused you.

If you're planning something bigger, open an issue first so we can agree on the approach before you spend time on it.

## Development setup

```bash
git clone https://github.com/alonstech/rigtop && cd rigtop
./rigtop --no-open        # then open the http://127.0.0.1:7777/#k=… link it prints
```

- **Dashboard changes** (`dashboard.html`): the server re-reads the file on every page load, so just refresh the browser.
- **Server changes** (`rigtop.py`): stop with Ctrl+C and start again.
- `./rigtop --once` prints a text snapshot. It's the quickest way to check the collectors.
- `./rigtop --demo` serves made-up data, and every button is a no-op. Use it for UI work you don't want touching your real processes, and **for any screenshot you post** (real ones show your hostname, command lines and container names).
- Run `python3 -m py_compile rigtop.py` and `python3 -m unittest discover -s tests` before sending a PR. The code must run on **Python 3.9** (the version macOS ships), so avoid `match`, `X | Y` type unions and other newer syntax.

## How it fits together

```
rigtop            sh launcher: picks the system /usr/bin/python3, falls back to python3
rigtop.py         collectors, cleanup jobs, HTTP server and terminal line (one file, in sections)
dashboard.html    the whole UI: vanilla JS + inline SVG charts, no build step, no CDN
tests/            unittest suite for the pure helpers (fixtures only, runs on any OS)
```

`rigtop.py`, top to bottom:

| Section | What it does |
|---|---|
| **kernel stats: macOS (ctypes)** | `cpu_ticks()` (per-core ticks), `vm_stats()` (VM page counts), `sysctl_*` |
| **kernel stats: Linux (/proc)** | `parse_proc_stat_cpus()`, `parse_kv()`, `parse_psi()`, `linux_memory()`, `linux_proc_path()`: pure parsers fed from `/proc` |
| **helpers** | `run()` for subprocesses, `du_k()` for sizes at background priority, `empty_dir()` the guarded cache cleaner |
| **`CACHE_TARGETS`** | The allow-list of cache folders rigtop may empty |
| **`group_of()` / `ALIASES`** | Turn a process path into an app name ("Google Chrome Helper (Renderer)" → "Google Chrome"). Linux: `group_of_linux()`, `LINUX_ALIASES` and `linux_is_system()`, which also use the process's cgroup |
| **`Jobs`** | Runs long cleanups and scans off the request thread; the UI polls their state |
| **`Monitor`** | Sampling loops: `loop()` (CPU/memory/processes), `docker_loop()`, `cache_loop()`. Per-platform readers: `_mem_mac()` / `_mem_linux()`, `_ps_mac()` / `_ps_linux()` |
| **actions** | `do_action()`: every state-changing operation, each strictly validated |
| **http** | A tiny `http.server` handler with token and Host checks |
| **terminal** | The live status line and `--once` |

### HTTP API

| Method | Path | Returns |
|---|---|---|
| `GET` | `/` | The dashboard: a static page with no secrets in it. Opened as `/#k=<key>`; the page reads the key from the fragment and stores it for this origin only |
| `GET` | `/api/state?since=<t>&every=<s>` | Everything the UI renders. `since` returns only newer history points; `every` sets the sampling interval. Polling this endpoint is what marks the dashboard as "watching" |
| `GET` | `/api/procs?group=<name>` | Processes in one app group, with full command lines (`__orphans__` returns orphaned dev processes) |
| `GET` | `/api/du?path=<dir>` | Folder-explorer sizes for a directory inside your home folder |
| `POST` | `/api/action` | Every state-changing operation. Also needs a same-origin `Origin` (or none). Body: `{"kind": …}` |

Every `/api/*` endpoint needs the per-run key in an `X-Rigtop-Key` header and returns `401` (GET) or `403` (POST) without it. Never add an endpoint that skips this check, since other user accounts on the same machine can reach `127.0.0.1` too. Don't move the key into a cookie or a query string either: browsers send cookies to every port on `127.0.0.1`, and query strings end up in logs and history.

## The rules

These keep rigtop worth running on a machine that's already struggling. PRs that break them will be asked to change.

1. **No dependencies.** Standard library only, so it stays `git clone && ./rigtop`.
2. **Do nothing expensive while nobody is watching.** Anything heavier than a kernel read must be gated on `self.watching()`. Disk scans and deletes run through `BG` (`taskpolicy -b`), and anything slow runs as a `Jobs` job, never on the request thread.
3. **Every destructive action is allow-listed and confirmed.**
   - Server side: no paths, commands or shell strings from the browser. The browser sends an *id*, and the server looks it up in a fixed table (`DOCKER_ACTIONS`, `CACHE_TARGETS`…). Never use `shell=True`.
   - UI side: quick actions use the two-tap `quickBtn`. Anything that can lose user data (rather than a cache) must use a `dialog` that shows the exact command.
4. **Only touch what's the user's.** Process actions are limited to the user's own non-system processes, and file actions to their home folder. Re-check on the server even when the UI already filters.
5. **Nothing leaves the machine.** No telemetry, no update checks, no external requests, and the page loads no CDN assets.
6. **Measure it.** If your change runs on every sample, note rigtop's own CPU and memory (shown in the dashboard footer) before and after in the PR.

## Common changes

### Add a cache location

Add an entry to `CACHE_TARGETS` in `rigtop.py`:

```python
{"id": "pnpm", "label": "pnpm store", "path": _p("Library/pnpm/store"), "safe": False,
 "hint": "Packages shared by all pnpm projects. Prefer `pnpm store prune`."},
```

- `id`: a short unique key, which the browser sends back.
- `path`: must be inside the home folder. `_p()` builds it from `$HOME`.
- `safe`: `True` only if the owning tool rebuilds or re-downloads it automatically with no user-visible loss. Safe entries are pre-ticked in "Clean all safe caches".
- `writable: True`: add this if the tool stores read-only files (Go's module cache does).

It shows up in the dashboard automatically once the folder exists and holds more than 50 MB.

### Recognise a new app or agent

Add its executable name to `ALIASES` (for example `"aider": "Aider"`), or to `LINUX_ALIASES` for a Linux-only name such as `"chrome"`. If it's an interpreter or dev server that agents tend to leave behind, add it to `DEV_RUNTIME` so orphaned copies are detected. If it's an agent or terminal that sits idle while waiting for input, add it to the `busyLooking` pattern in `dashboard.html` so it's never suggested as an idle app to quit. On Linux, if it's a desktop service the session can't live without, add it to `LINUX_SYSTEM_NAMES`.

### Adding a platform

rigtop runs on macOS and Linux. Platform code is selected with `IS_MAC` / `IS_LINUX`, and kept to a few places:

- kernel stats: the macOS ctypes section and the Linux `/proc` section, used by `Monitor.sample()` through `_mem_mac()` / `_mem_linux()`
- the process list: `_ps_mac()` (`ps`) and `_ps_linux()` (`/proc`), both returning `(pid, ppid, uid, %cpu, rss, path, cgroup)` rows, plus `proc_owner()` for the pid-reuse guard
- grouping and "is this a system process": `group_of()` / `SYSTEM_PATH` and `group_of_linux()` / `linux_is_system()`
- `DATA_VOLUME`, `BG`, `APP_CACHES` / `_c()` (cache paths), `TRASH`
- the actions that call OS tools: Trash (`osascript` / `gio`), Reveal (`open -R` / `reveal_linux()`) and Quit (`osascript`, macOS only)
- dashboard wording: `OS()`, `MACHINE()` and `WIRED()` in `dashboard.html`, driven by `host.platform`

Any new platform must produce the same `mem` dict (`total, app, wired, compressed, cached, used, free, pressure_level, free_pct, swap_*`) and the same process rows, so the dashboard needs nothing beyond wording. Windows is the open one: Docker Desktop runs in WSL2 there. Please open an issue before starting, so we can agree the approach first.

## Testing your change

`tests/` holds a small `unittest` suite for the pure helpers (the Linux parsers, `group_of` / `group_of_linux`, `parse_size`, `under_home`). It uses fixtures, so it runs on either OS. Tests for the action validation in `do_action` would be a welcome next PR. Then check by hand:

- [ ] `python3 -m py_compile rigtop.py` and `python3 -m unittest discover -s tests` pass on Python 3.9 (`uv run --no-project --python 3.9 …` is a quick way to get one).
- [ ] `./rigtop --once` prints sensible numbers.
- [ ] The dashboard loads with no errors in the browser console, in light and dark mode, and at a narrow (phone) width.
- [ ] For a new endpoint or action: rejected without the key (`curl localhost:7777/api/state` returns 401; `curl -X POST localhost:7777/api/action -d '{}'` returns 403) and rejected with bad input.
- [ ] Never test a cleanup against data you care about. Point a temporary `CACHE_TARGETS` entry at a scratch folder instead.

## Pull requests

- Keep PRs focused: one feature or fix each.
- Match the style around your change: plain functions, short comments that explain *why*, no new abstractions without a second use.
- Include a screenshot for UI changes, taken with `./rigtop --demo`.
- By contributing, you agree your work is released under the [MIT License](LICENSE).

Be kind in issues and reviews. Everyone here is trying to make their laptop a little less miserable.
