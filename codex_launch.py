#!/usr/bin/env python3
"""Open the official ChatGPT.app (Codex) with Transcript width and prompt suggestions.

The app bundle stays untouched and OpenAI-signed. The launcher starts it with
`--remote-debugging-pipe`, so only this process can drive DevTools, then stays alive as the
app's parent:
  - Fetch interception serves the patched settings chunk and panel layout, plus the files in
    assets/, which a new-document script adds to each app page's <head>;
  - the `__lpsSend` binding forwards the prompt-suggestion requests to the model provider
    (codex_tw.suggestion_provider), which the renderer CSP would block.
ChatGPT.app opened directly (or relaunched by Sparkle) runs without these additions, unless
the `install-agent` watcher reopens it through the launcher. `install-app` creates a Codex.app
that runs the launcher.
"""

import argparse
import base64
import concurrent.futures
import ctypes
import datetime
import fcntl
import hashlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import codex_tw
from codex_tw import APP, BUNDLE_ID, DATA, Abort, Asar

STATE = DATA / "launcher.json"
LOG = DATA / "logs" / "launcher.log"
CACHE = DATA / "runtime-cache"
ROUTES_VERSION = "1"  # bump when build_routes changes what it serves
LOG_LIMIT = 2 * 1024 * 1024
APP_ORIGIN = "app://-/"
BINDING = "__lpsSend"
HTTP_TIMEOUT = 60
TITLE = "Codex 启动器"
WRAPPER = Path.home() / "Applications/Codex.app"
WRAPPER_ID = "local.codex-plus.launcher"
AGENT_LABEL = "local.codex-plus.watch"
AGENT_PLIST = Path.home() / f"Library/LaunchAgents/{AGENT_LABEL}.plist"
WATCH_LOG = DATA / "logs" / "watch.log"


def log(message):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


def notify(subtitle, message):
    script = (f"display notification {json.dumps(message)} with title {json.dumps(TITLE)} "
              f"subtitle {json.dumps(subtitle)}")
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    log(f"notify: {subtitle} - {message}")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def read_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def write_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE)


def codex_pids():
    """Main processes of any running Codex bundle (com.openai.codex), wherever it lives."""
    listing = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True).stdout
    pids = []
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        command = command.strip()
        if not command.endswith(".app/Contents/MacOS/ChatGPT"):
            continue
        try:
            info = plistlib.loads(Path(command).parents[1].joinpath("Info.plist").read_bytes())
        except OSError:
            continue
        if info.get("CFBundleIdentifier") == BUNDLE_ID:
            pids.append(int(pid))
    return pids


def launcher_owns(pids):
    state = read_state()
    return bool(pids) and pid_alive(state.get("daemon_pid")) and set(pids) == {state.get("app_pid")}


# ---------------------------------------------------------------- patches

def build_routes(app):
    """URL -> (stage, payload) for every webview file the launcher replaces or adds.

    Locating takes a few seconds, so the result is cached per app.asar header and patch id.
    """
    asar = Asar(app / "Contents/Resources/app.asar")
    key = hashlib.sha256(
        asar.raw + f"{codex_tw.patch_id()}:{ROUTES_VERSION}".encode()).hexdigest()[:16]
    cache = CACHE / f"{key}.json"
    try:
        saved = json.loads(cache.read_text())
        routes = {url: (stage, base64.b64decode(data))
                  for url, (stage, data) in saved["routes"].items()}
        return routes, saved["located"]
    except (OSError, ValueError, KeyError):
        pass
    changes, located = codex_tw.locate_changes(asar, runtime=True)
    existing = {path for path, _ in asar.files()}
    routes = {}
    for path, payload in changes.items():
        if not path.startswith("webview/"):
            raise Abort(f"Runtime patch outside the webview: {path}")
        if path.endswith(".html"):
            continue  # navigations bypass Fetch; BOOTSTRAP adds the head tags instead
        url = APP_ORIGIN + path[len("webview/"):]
        # Existing files keep the app's own response headers; new files have no original.
        routes[url] = ("Response" if path in existing else "Request", payload)
    CACHE.mkdir(parents=True, exist_ok=True)
    for old in CACHE.glob("*.json"):
        old.unlink()
    cache.write_text(json.dumps({
        "routes": {url: (stage, base64.b64encode(payload).decode())
                   for url, (stage, payload) in routes.items()},
        "located": located,
    }))
    return routes, located


# Electron's app:// navigations never reach Fetch interception, so the <head> tags that a
# re-signed build writes into the HTML are added by a new-document script instead.
BOOTSTRAP = """(() => {
  if (!location.href.startsWith(%(origin)s) || globalThis.__localPatchBoot) return;
  globalThis.__localPatchBoot = true;
  const add = () => {
    for (const name of %(assets)s) {
      const url = %(origin)s + "assets/" + name;
      const node = document.createElement(name.endsWith(".css") ? "link" : "script");
      if (name.endsWith(".css")) Object.assign(node, { rel: "stylesheet", href: url });
      else Object.assign(node, { type: "module", src: url });
      document.head.append(node);
    }
  };
  // After the static <head>, as in the HTML patch, so these rules follow the app's own CSS.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", add, { once: true });
  } else add();
})();""" % {"origin": json.dumps(APP_ORIGIN), "assets": json.dumps(codex_tw.INJECTED_ASSETS)}


def content_type(url):
    for suffix, kind in ((".js", "text/javascript"), (".css", "text/css"),
                         (".html", "text/html")):
        if url.endswith(suffix):
            return kind + "; charset=utf-8"
    return "application/octet-stream"


# ---------------------------------------------------------------- suggestion requests

# Local providers must not go through a system or environment proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log_outcome(entry):
    """One line per prompt-suggestion outcome, as recorded in the page's __lpsStats."""
    parts = [f"suggestion {str(entry.get('outcome'))[:40]}",
             f"thread {str(entry.get('thread') or '-')[:60]}"]
    if entry.get("chars") is not None:
        parts.append(f"context {entry.get('chars')}")
    for field in ("text", "error"):
        if entry.get(field):
            parts.append(f"{field} {json.dumps(str(entry[field])[:200], ensure_ascii=False)}")
    log(", ".join(parts))


def provider_request(request):
    """Only GET /models and POST /responses (non-streaming, not stored) are forwarded."""
    base, token = codex_tw.suggestion_provider()
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    op = request.get("op")
    if op == "models":
        http = urllib.request.Request(base + "/models", headers=headers)
        label = "GET /models"
    elif op == "suggest":
        given = request.get("body") or {}
        body = {
            "model": str(given.get("model") or ""),
            "instructions": str(given.get("instructions") or ""),
            "input": given.get("input"),
            "reasoning": given.get("reasoning"),
            "max_output_tokens": given.get("max_output_tokens"),
            "stream": False,
            "store": False,
        }
        body = {key: value for key, value in body.items() if value is not None}
        http = urllib.request.Request(base + "/responses", data=json.dumps(body).encode(),
                                      headers=headers, method="POST")
        label = "POST /responses"
    else:
        raise RuntimeError("unknown request")
    try:
        with _opener.open(http, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read()[:300].decode("utf-8", "replace")
        raise RuntimeError(f"{label} HTTP {error.code} {detail}") from None


# ---------------------------------------------------------------- process

_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
POSIX_SPAWN_SETSIGDEF = 0x04
POSIX_SPAWN_SETSIGMASK = 0x08
POSIX_SPAWN_CLOEXEC_DEFAULT = 0x4000


def spawn_app(executable, args, fds):
    """posix_spawn with only stdio and `fds` (child fd -> our fd) inherited.

    The child disclaims responsibility, so macOS privacy prompts and grants belong to
    ChatGPT itself, as when it is opened from Finder, not to Raycast or the shell.
    """
    attr = ctypes.c_void_p()
    actions = ctypes.c_void_p()
    _libc.posix_spawnattr_init(ctypes.byref(attr))
    _libc.posix_spawn_file_actions_init(ctypes.byref(actions))
    try:
        flags = POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_CLOEXEC_DEFAULT
        _libc.posix_spawnattr_setflags(ctypes.byref(attr), ctypes.c_short(flags))
        everything, nothing = ctypes.c_uint32(0xFFFFFFFF), ctypes.c_uint32(0)
        _libc.posix_spawnattr_setsigdefault(ctypes.byref(attr), ctypes.byref(everything))
        _libc.posix_spawnattr_setsigmask(ctypes.byref(attr), ctypes.byref(nothing))
        disclaim = getattr(_libc, "responsibility_spawnattrs_setdisclaim", None)
        if disclaim is not None:
            disclaim(ctypes.byref(attr), 1)
        null = os.open(os.devnull, os.O_RDWR)
        for target in (0, 1, 2):
            _libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), null, target)
        for target, source in fds.items():
            _libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), source, target)
        env = {key: value for key, value in os.environ.items()
               if key not in ("__CFBundleIdentifier", "XPC_SERVICE_NAME", "PYTHONPATH")}
        argv = [executable, *args]
        c_argv = (ctypes.c_char_p * (len(argv) + 1))(*[a.encode() for a in argv], None)
        c_env = (ctypes.c_char_p * (len(env) + 1))(
            *[f"{k}={v}".encode() for k, v in env.items()], None)
        pid = ctypes.c_int()
        error = _libc.posix_spawn(ctypes.byref(pid), executable.encode(), ctypes.byref(actions),
                                  ctypes.byref(attr), c_argv, c_env)
        os.close(null)
        if error:
            raise OSError(error, os.strerror(error), executable)
        return pid.value
    finally:
        _libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        _libc.posix_spawnattr_destroy(ctypes.byref(attr))


def high_fd(fd):
    """Move a pipe end above the child's 3/4 so dup2 never maps an fd onto itself."""
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return moved


# ---------------------------------------------------------------- CDP over the pipe

class Pipe:
    """Chrome DevTools protocol on fds 3/4: NUL-terminated JSON messages."""

    def __init__(self, read_fd, write_fd):
        self.read_fd, self.write_fd = read_fd, write_fd
        self.lock = threading.Lock()
        self.next_id = 0
        self.waiters = {}

    def send(self, method, params=None, session=None, wait=False, timeout=10):
        with self.lock:
            self.next_id += 1
            message_id = self.next_id
            waiter = None
            if wait:
                waiter = self.waiters[message_id] = [threading.Event(), None]
            message = {"id": message_id, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            data = json.dumps(message, separators=(",", ":")).encode() + b"\0"
            view = memoryview(data)
            while view:
                view = view[os.write(self.write_fd, view):]
        if not wait:
            return None
        if not waiter[0].wait(timeout):
            self.waiters.pop(message_id, None)
            raise TimeoutError(method)
        reply = waiter[1]
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    def messages(self):
        buffer = b""
        while True:
            chunk = os.read(self.read_fd, 1 << 16)
            if not chunk:
                return
            buffer += chunk
            *complete, buffer = buffer.split(b"\0")
            for raw in complete:
                message = json.loads(raw)
                if "id" in message:
                    waiter = self.waiters.pop(message["id"], None)
                    if waiter:
                        waiter[1] = message
                        waiter[0].set()
                    elif "error" in message:
                        log(f"cdp error: {message['error']}")
                else:
                    yield message


class Injector:
    def __init__(self, pipe, routes):
        self.pipe = pipe
        self.routes = routes
        self.workers = concurrent.futures.ThreadPoolExecutor(4)
        self.patterns = [{"urlPattern": url, "requestStage": stage}
                         for url, (stage, _) in routes.items()]
        self.served = 0
        self.seen = set()

    def start(self):
        self.pipe.send("Target.setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
        })

    def handle(self, message):
        method = message.get("method")
        params = message.get("params", {})
        session = message.get("sessionId")
        if method == "Target.attachedToTarget":
            self.attached(params)
        elif method == "Fetch.requestPaused":
            self.paused(session, params)
        elif method == "Runtime.bindingCalled" and params.get("name") == BINDING:
            self.workers.submit(self.binding, session, params)

    def attached(self, params):
        session = params["sessionId"]
        info = params["targetInfo"]
        if info.get("type") == "page":
            if self.patterns:
                self.pipe.send("Fetch.enable", {"patterns": self.patterns}, session)
            # The renderer restores bindings and new-document scripts after a process swap
            # only while Runtime / Page are enabled.
            self.pipe.send("Runtime.enable", {}, session)
            self.pipe.send("Runtime.addBinding", {"name": BINDING}, session)
            if self.patterns:
                self.pipe.send("Page.enable", {}, session)
                self.pipe.send("Page.addScriptToEvaluateOnNewDocument",
                               {"source": BOOTSTRAP}, session)
            if not params.get("waitingForDebugger"):
                # Already loaded before the launcher attached: load again with the patches.
                self.pipe.send("Page.reload", {}, session)
            log(f"attached {info.get('url') or info.get('type')}")
        if params.get("waitingForDebugger"):
            self.pipe.send("Runtime.runIfWaitingForDebugger", {}, session)

    def paused(self, session, params):
        request_id = params["requestId"]
        url = params["request"]["url"]
        route = self.routes.get(url.split("?", 1)[0].split("#", 1)[0])
        status = params.get("responseStatusCode")
        if not route or (status is not None and status != 200):
            self.pipe.send("Fetch.continueRequest", {"requestId": request_id}, session)
            return
        _, payload = route
        headers = [h for h in params.get("responseHeaders") or []
                   if h["name"].lower() not in ("content-length", "content-encoding", "etag")]
        if not any(h["name"].lower() == "content-type" for h in headers):
            headers.append({"name": "Content-Type", "value": content_type(url)})
        self.pipe.send("Fetch.fulfillRequest", {
            "requestId": request_id,
            "responseCode": 200,
            "responseHeaders": headers,
            "body": base64.b64encode(payload).decode(),
        }, session)
        self.served += 1
        if url not in self.seen:
            self.seen.add(url)
            log(f"served {url}")

    def binding(self, session, params):
        context = params.get("executionContextId")
        call_id = None
        try:
            request = json.loads(params.get("payload") or "{}")
            logging = request.get("op") == "log"
            if not logging:
                call_id = int(request.get("id"))
            origin = self.pipe.send("Runtime.evaluate", {
                "expression": "location.href", "contextId": context, "returnByValue": True,
            }, session, wait=True).get("result", {}).get("value", "")
            if not str(origin).startswith(APP_ORIGIN):
                raise RuntimeError("forbidden sender")
            if logging:
                log_outcome(request.get("entry") or {})
                return
            ok, value = True, provider_request(request)
        except Exception as error:
            ok, value = False, f"{type(error).__name__}: {error}"
            log(f"suggestion request failed: {value[:300]}")
        if call_id is None:
            return
        expression = (f"globalThis.__lpsResolve?.({call_id},{json.dumps(ok)},"
                      f"{json.dumps(value, ensure_ascii=False)})")
        try:
            self.pipe.send("Runtime.evaluate", {"expression": expression, "contextId": context},
                           session)
        except OSError:
            pass


# ---------------------------------------------------------------- commands

def command_daemon(args):
    app = Path(args.app)
    if LOG.exists() and LOG.stat().st_size > LOG_LIMIT:
        LOG.write_text("")
    try:
        started = time.monotonic()
        routes, located = build_routes(app)
        log(f"patches ready in {time.monotonic() - started:.1f}s: "
            f"{len(routes)} files, settings {located['settings_chunk']}")
    except Exception as error:
        routes = {}
        log(f"patches unavailable: {error}\n{traceback.format_exc()}")
        notify("未加载补丁，按原版打开", str(error)[:180])

    child_read, ours_write = os.pipe()
    ours_read, child_write = os.pipe()
    child_read, child_write = high_fd(child_read), high_fd(child_write)
    ours_read, ours_write = high_fd(ours_read), high_fd(ours_write)
    os.chdir("/")
    pid = spawn_app(str(app / "Contents/MacOS/ChatGPT"), ["--remote-debugging-pipe"],
                    {3: child_read, 4: child_write})
    os.close(child_read)
    os.close(child_write)
    write_state({"daemon_pid": os.getpid(), "app_pid": pid, "app": str(app),
                 "patched": bool(routes), "patch_id": codex_tw.patch_id(),
                 "started": time.time()})
    log(f"started {app} pid {pid}")

    pipe = Pipe(ours_read, ours_write)
    injector = Injector(pipe, routes)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    try:
        injector.start()
        for message in pipe.messages():
            try:
                injector.handle(message)
            except OSError:
                raise
            except Exception:
                log(f"handler error: {traceback.format_exc()}")
    except OSError as error:
        log(f"pipe closed: {error}")
    _, status = os.waitpid(pid, 0)
    log(f"app exited ({status}); served {injector.served} patched files")
    if read_state().get("daemon_pid") == os.getpid():
        STATE.unlink(missing_ok=True)
    # Sparkle relaunches the app after an update without the pipe; the watch agent, when
    # installed, has reopened it through the launcher by now.
    time.sleep(10)
    pids = codex_pids()
    if pids and not launcher_owns(pids):
        notify("ChatGPT 已在没有补丁的情况下重新打开", "用启动器重新打开即可恢复建议和宽度设置")
    return 0


def ask_restart(reason="ChatGPT 正在运行，但不是通过启动器打开的，建议和宽度设置未加载。"):
    script = (
        f'display dialog "{reason}要退出并重新打开吗？" with title {json.dumps(TITLE)} '
        'buttons {"取消", "重新打开"} default button "重新打开" cancel button "取消"'
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return result.returncode == 0 and "重新打开" in result.stdout


def quit_codex(timeout=60):
    subprocess.run(["osascript", "-e", f'tell application id "{BUNDLE_ID}" to quit'],
                   capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not codex_pids():
            return True
        time.sleep(0.5)
    return False


def command_launch(args):
    app = Path(args.app)
    pids = codex_pids()
    # The daemon reads the patches once at startup, so edits to assets/ only reach a new instance.
    stale = launcher_owns(pids) and read_state().get("patch_id") != codex_tw.patch_id()
    if launcher_owns(pids) and not stale:
        subprocess.run(["open", read_state().get("app") or str(app)], capture_output=True)
        print("ChatGPT 已通过启动器运行")
        return 0
    if pids:
        reason = "ChatGPT 运行的是旧版补丁，assets/ 里的修改尚未生效。" if stale else None
        if not (args.restart or ask_restart(*filter(None, [reason]))):
            print("已取消")
            return 1
        if not quit_codex():
            print("ChatGPT 没有退出，请手动退出后再试")
            return 1
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as stream:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "daemon", "--app", str(app)],
            stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
            start_new_session=True, close_fds=True,
        )
    deadline = time.time() + 30
    while time.time() < deadline:
        state = read_state()
        if pid_alive(state.get("app_pid")) and pid_alive(state.get("daemon_pid")):
            print("ChatGPT 已打开" + ("" if state.get("patched") else "（补丁未加载，见日志）"))
            return 0
        time.sleep(0.3)
    print(f"启动超时，见 {LOG}")
    return 1


def command_install_app(args):
    """A small Codex.app that runs `launch`, for the Dock, Spotlight and Launchpad."""
    app = Path(args.app)
    wrapper = Path(args.path).expanduser()
    contents = wrapper / "Contents"
    if wrapper.exists():
        info = plistlib.loads((contents / "Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != WRAPPER_ID:
            raise Abort(f"{wrapper} exists and is not the launcher app")
        subprocess.run(["rm", "-rf", str(wrapper)], check=True)
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    source = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    icon = source.get("CFBundleIconFile")
    if icon:
        icon = icon if icon.endswith(".icns") else f"{icon}.icns"
        (contents / "Resources/AppIcon.icns").write_bytes(
            (app / "Contents/Resources" / icon).read_bytes())
    # The current icon is in Assets.car, with light, dark and tinted variants; macOS picks the
    # one matching the icon style setting. AppIcon.icns is only the fallback.
    icon_name = {}
    if source.get("CFBundleIconName") and (app / "Contents/Resources/Assets.car").exists():
        (contents / "Resources/Assets.car").write_bytes(
            (app / "Contents/Resources/Assets.car").read_bytes())
        icon_name = {"CFBundleIconName": source["CFBundleIconName"]}
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        **icon_name,
        "CFBundleIdentifier": WRAPPER_ID,
        "CFBundleName": wrapper.stem,
        "CFBundleDisplayName": wrapper.stem,
        "CFBundleExecutable": "launch",
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "AppIcon",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSUIElement": True,  # ChatGPT shows its own Dock icon once it runs
    }))
    script = contents / "MacOS/launch"
    script.write_text(
        "#!/bin/sh\n"
        f'exec {codex_tw.PYTHON} "{Path(__file__).resolve()}" launch --app "{app}"\n'
    )
    script.chmod(0o755)
    subprocess.run(["codesign", "--force", "--sign", "-", str(wrapper)],
                   check=True, capture_output=True)
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                    "LaunchServices.framework/Support/lsregister", "-f", str(wrapper)],
                   capture_output=True)
    print(f"installed {wrapper}")
    return 0


def ps_field(pid, field):
    return subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def from_launcher(pid):
    """True for an app process spawned by a launcher daemon, also before it writes STATE."""
    parent = ps_field(pid, "ppid")
    return parent.isdigit() and "codex_launch.py daemon" in ps_field(parent, "command")


def command_watch(args):
    """Reopen ChatGPT through the launcher whenever it starts some other way.

    Covers the Dock, Finder, `open` and Sparkle's relaunch after an update. Only processes
    that start while the watcher runs are taken over, so an instance already in use is left
    alone. A new instance has no state yet, so it is stopped with SIGTERM right away.
    """
    app = Path(args.app)
    executable = str(app / "Contents/MacOS/ChatGPT")
    known = set(codex_pids())
    log(f"watching {app}; leaving {sorted(known) or 'nothing'} alone")
    while True:
        time.sleep(args.interval)
        pids = codex_pids()
        fresh = [pid for pid in pids if pid not in known]
        known = set(pids)
        if not fresh or launcher_owns(pids):
            continue
        fresh = [pid for pid in fresh
                 if ps_field(pid, "comm") == executable and not from_launcher(pid)]
        if not fresh:
            continue
        log(f"pid {fresh} started without the launcher; reopening")
        for pid in fresh:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 15
        while time.time() < deadline and any(pid_alive(pid) for pid in fresh):
            time.sleep(0.2)
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "launch", "--app", str(app),
             "--restart"], capture_output=True, text=True)
        log(f"launch: {(result.stdout + result.stderr).strip()}")
        known = set(codex_pids())


def command_install_agent(args):
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    WATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{AGENT_LABEL}"], capture_output=True)
    AGENT_PLIST.write_bytes(plistlib.dumps({
        "Label": AGENT_LABEL,
        "ProgramArguments": [codex_tw.PYTHON, str(Path(__file__).resolve()), "watch",
                             "--app", str(Path(args.app))],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        # The launcher daemons it starts outlive the agent.
        "AbandonProcessGroup": True,
        "StandardOutPath": str(WATCH_LOG),
        "StandardErrorPath": str(WATCH_LOG),
    }))
    subprocess.run(["launchctl", "bootstrap", domain, str(AGENT_PLIST)], check=True)
    print(f"installed {AGENT_LABEL}")
    return 0


def command_uninstall_agent(_args):
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
                   capture_output=True)
    AGENT_PLIST.unlink(missing_ok=True)
    print(f"removed {AGENT_LABEL}")
    return 0


def command_status(_args):
    pids = codex_pids()
    state = read_state()
    print(json.dumps({
        "codex_pids": pids,
        "via_launcher": launcher_owns(pids),
        "patches_current": state.get("patch_id") == codex_tw.patch_id() if state else None,
        "launcher": state or None,
        "log": str(LOG),
    }, indent=2, ensure_ascii=False))
    return 0


def command_provider(args):
    """Show or set the model provider used for prompt suggestions."""
    settings = codex_tw.SETTINGS
    if args.name:
        lines = settings.read_text().splitlines() if settings.exists() else []
        entry = f"provider = {json.dumps(args.name)}"
        kept = [line for line in lines if not line.lstrip().startswith("provider")
                or "=" not in line or line.split("=", 1)[0].strip() != "provider"]
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("\n".join([entry, *kept]) + "\n")
    try:
        codex = codex_tw.read_toml(codex_tw.CODEX_HOME / "config.toml")
    except FileNotFoundError:
        codex = {}
    try:
        base, _token = codex_tw.suggestion_provider()
        resolved, error = base, None
    except RuntimeError as failure:
        resolved, error = None, str(failure)
    print(json.dumps({
        "provider": codex_tw.settings().get("provider"),
        "available": sorted(codex.get("model_providers") or {}),
        "base_url": resolved,
        "error": error,
        "settings": str(settings),
    }, indent=2, ensure_ascii=False))
    return 0 if resolved else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.set_defaults(func=command_launch, app=str(APP), restart=False)
    sub = parser.add_subparsers(dest="command")
    launch = sub.add_parser("launch", help="open ChatGPT through the launcher (default)")
    launch.add_argument("--app", default=str(APP))
    launch.add_argument("--restart", action="store_true",
                        help="quit a ChatGPT opened some other way, or one running outdated "
                             "patches, without asking")
    launch.set_defaults(func=command_launch)
    daemon = sub.add_parser("daemon", help="run ChatGPT in the foreground (used by launch)")
    daemon.add_argument("--app", default=str(APP))
    daemon.set_defaults(func=command_daemon)
    sub.add_parser("status", help="show whether ChatGPT runs through the launcher").set_defaults(
        func=command_status)
    install = sub.add_parser("install-app", help="create Codex.app that opens ChatGPT through "
                                                 "the launcher")
    install.add_argument("--path", default=str(WRAPPER))
    install.add_argument("--app", default=str(APP))
    install.set_defaults(func=command_install_app)
    watch = sub.add_parser("watch", help="reopen ChatGPT through the launcher when it starts "
                                         "some other way (run by the agent)")
    watch.add_argument("--app", default=str(APP))
    watch.add_argument("--interval", type=float, default=0.3)
    watch.set_defaults(func=command_watch)
    agent = sub.add_parser("install-agent", help="run `watch` as a launchd agent at login")
    agent.add_argument("--app", default=str(APP))
    agent.set_defaults(func=command_install_agent)
    sub.add_parser("uninstall-agent", help="remove the watch agent").set_defaults(
        func=command_uninstall_agent)
    provider = sub.add_parser("provider", help="show or set the model provider for prompt "
                                               "suggestions")
    provider.add_argument("name", nargs="?",
                          help="a [model_providers.<name>] table in ~/.codex/config.toml")
    provider.set_defaults(func=command_provider)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
