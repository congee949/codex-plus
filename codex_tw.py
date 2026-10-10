#!/usr/bin/env python3
"""Build a re-signed ChatGPT.app (Codex) with Transcript width and prompt suggestions.

codex_launch.py adds the same features to the official app at runtime and is the default;
this module also provides the shared locators and `restore` (install the official build).

`run` (optionally daily through launchd):
  appcast -> verified official build -> native-feature scan -> locate injection points
  -> rebuild app.asar + Info.plist integrity + framework digest -> re-sign with the Apple Development identity
  -> wait for the app to quit -> launch smoke test -> atomic bundle swap (with backup).
Any failure leaves the installed app untouched and posts a notification.
"""

import argparse
import base64
import collections
import datetime
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

APP = Path("/Applications/ChatGPT.app")
ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
DATA = Path.home() / "Library/Application Support/codex-plus"
STATE_FILE = DATA / "state.json"
LABEL = "local.codex-plus.rebuild"
AGENT_PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
# codex-plus settings: `provider` for prompt suggestions, `sign_team` for the re-signed build.
SETTINGS = DATA / "config.toml"
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
OPENAI_BASE = "https://api.openai.com/v1"


def find_python():
    """Interpreter written into the launcher app and launchd agents; stable across upgrades."""
    for candidate in (os.environ.get("CODEX_PLUS_PYTHON"), "/opt/homebrew/bin/python3",
                      "/usr/local/bin/python3", "/usr/bin/python3"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return sys.executable


PYTHON = find_python()

TEAM_ID = "2DC432GLL2"
BUNDLE_ID = "com.openai.codex"
# Sparkle EdDSA key shipped in the official Info.plist (SUPublicEDKey), pinned here.
SPARKLE_ED_KEY = "mNfr1v9t63BfgDtlw4C8lRvSY6uMggIXABDOCi3tS6k="
APPCAST = "https://updates.oaistatic.com/codex/app/appcast"
MARKER_KEY = "LocalTranscriptWidthPatch"
FRAMEWORK_BINARY = (
    "Contents/Frameworks/Codex Framework.framework/Versions/Current/Codex Framework"
)
# Electron's __asar_integrity section: 32-byte sentinel, used flag, version, SHA256.
DIGEST_SENTINEL = b"AGbevlPCksUGKNL8TSn7wGmJEuJsXb2A"
BLOCK_SIZE = 4 * 1024 * 1024
# Entitlements that require OpenAI's provisioning profile; AMFI kills ad-hoc code carrying them.
RESTRICTED_ENTITLEMENTS = (
    "com.apple.application-identifier",
    "com.apple.developer.team-identifier",
    "com.apple.developer.aps-environment",
    "keychain-access-groups",
)
WIDTHS_PX = {"narrow": 768, "medium": 960, "wide": 1280}
INJECT_VERSION = "6"  # bump when the injection logic below changes
INJECTED_ASSETS = (
    "transcript-width.js", "transcript-width.css",
    "prompt-suggestion.js", "prompt-suggestion.css",
)
PEER_ASSET = "local-app-tools-peer.node"
PEER_MARKER = "LocalAppToolsPeerPatch"


class Abort(Exception):
    """A step refused to continue; the installed app is left as it is."""


# ---------------------------------------------------------------- utilities

def log(message):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def notify(title, message):
    script = (
        f"display notification {json.dumps(message)} "
        f"with title {json.dumps('Codex 宽度补丁')} subtitle {json.dumps(title)}"
    )
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    log(f"notify: {title} - {message}")


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(STATE_FILE)


def read_toml(path):
    """tomllib when available; otherwise tables and single-line string values only."""
    text = Path(path).read_text()
    if tomllib:
        return tomllib.loads(text)
    root = table = {}
    for line in text.splitlines():
        header = re.match(r"\s*(\[\[?)([^\[\]]+)\]\]?\s*(?:#.*)?$", line)
        if header:
            table = {} if header[1] == "[[" else root
            if header[1] == "[":
                for part in re.findall(r'"([^"]*)"|\'([^\']*)\'|([^.\s]+)', header[2]):
                    table = table.setdefault(next(filter(None, part)), {})
            continue
        pair = re.match(r'\s*([\w-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', line)
        if pair:
            table[pair[1]] = pair[2] if pair[2] is not None else pair[3]
    return root


def settings():
    try:
        return read_toml(SETTINGS)
    except FileNotFoundError:
        return {}


def suggestion_provider():
    """(base_url, token) for prompt suggestions.

    `provider` in SETTINGS, else the top-level `model_provider` of ~/.codex/config.toml, names
    a [model_providers.<name>] table there. Without one, an OpenAI API key (OPENAI_API_KEY or
    an API-key login in auth.json) uses api.openai.com.
    """
    try:
        codex = read_toml(CODEX_HOME / "config.toml")
    except FileNotFoundError:
        codex = {}
    providers = codex.get("model_providers") or {}
    chosen = settings().get("provider")
    name = chosen or codex.get("model_provider")
    if chosen and chosen not in providers:
        raise RuntimeError(f"[model_providers.{chosen}] not found in {CODEX_HOME / 'config.toml'}")
    if name in providers:
        provider = providers[name]
        base = str(provider.get("base_url") or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise RuntimeError(f"[model_providers.{name}] has no http(s) base_url")
        token = provider.get("experimental_bearer_token")
        if not token and provider.get("env_key"):
            token = os.environ.get(provider["env_key"])
        return base, token
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        try:
            key = json.loads((CODEX_HOME / "auth.json").read_text()).get("OPENAI_API_KEY")
        except (OSError, ValueError, AttributeError):
            key = None
    if key:
        return OPENAI_BASE, key
    raise RuntimeError(f"No model provider for prompt suggestions; set provider in {SETTINGS}")


def sign_team():
    """Team of the Apple Development identity used for re-signing.

    An Apple-issued team id keeps both keychain checks on "Codex Storage Key" stable across
    builds: the ACL matches the designated requirement and the partition list matches
    `teamid:`, not the per-build cdhash.
    """
    team = str(settings().get("sign_team") or "")
    if not re.fullmatch(r"[A-Z0-9]{10}", team):
        raise Abort(f"Set sign_team to your Apple Development team id in {SETTINGS}")
    return team


def patch_id():
    """Changes whenever the shipped assets or the injection logic change."""
    hasher = hashlib.sha256(INJECT_VERSION.encode())
    for name in (*INJECTED_ASSETS, "local-app-tools-peer.cpp", PEER_ASSET):
        hasher.update((ASSETS / name).read_bytes())
    return hasher.hexdigest()[:12]


def app_running():
    result = subprocess.run(
        ["pgrep", "-f", f"^{APP}/Contents/MacOS/ChatGPT"], capture_output=True,
    )
    return result.returncode == 0


def app_info(app):
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    return {
        "version": info.get("CFBundleShortVersionString"),
        "build": int(info.get("CFBundleVersion", "0")),
        "marker": info.get(MARKER_KEY),
    }


def official_signature(app):
    """True when the bundle is intact and signed by OpenAI's Developer ID."""
    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True,
    )
    if verify.returncode != 0:
        return False
    details = subprocess.run(
        ["codesign", "-dv", "--verbose=2", str(app)], capture_output=True, text=True,
    ).stderr
    return (
        f"TeamIdentifier={TEAM_ID}" in details
        and f"Identifier={BUNDLE_ID}" in details
        and "Signature=adhoc" not in details
    )


def clone_tree(src, dst):
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    run(["cp", "-c", "-R", str(src), str(dst)])  # APFS clone, copy-on-write


# ---------------------------------------------------------------- asar

class Asar:
    def __init__(self, path):
        self.path = path
        with path.open("rb") as stream:
            pickle, header_size, payload_size, string_size = struct.unpack(
                "<4I", stream.read(16),
            )
            if pickle != 4 or payload_size + 4 != header_size:
                raise Abort("Unsupported ASAR header layout")
            self.raw = stream.read(string_size)
        self.header = json.loads(self.raw)
        self.data_start = 8 + header_size

    def node(self, name, create=False):
        node = self.header
        parts = name.split("/")
        for part in parts[:-1]:
            node = node.setdefault("files", {}).setdefault(part, {"files": {}}) \
                if create else node["files"][part]
        if create:
            return node.setdefault("files", {}).setdefault(parts[-1], {})
        return node["files"][parts[-1]]

    def files(self, node=None, prefix=""):
        node = self.header if node is None else node
        for name, child in node.get("files", {}).items():
            path = f"{prefix}{name}"
            if "files" in child:
                yield from self.files(child, f"{path}/")
            elif "offset" in child and not child.get("unpacked"):
                yield path, child

    def read(self, name):
        node = self.node(name)
        with self.path.open("rb") as stream:
            stream.seek(self.data_start + int(node["offset"]))
            return stream.read(node["size"])

    def listdir(self, name):
        return list(self.node(name)["files"])


def file_integrity(content):
    return {
        "algorithm": "SHA256",
        "hash": hashlib.sha256(content).hexdigest(),
        "blockSize": BLOCK_SIZE,
        "blocks": [
            hashlib.sha256(content[offset:offset + BLOCK_SIZE]).hexdigest()
            for offset in range(0, len(content), BLOCK_SIZE)
        ],
    }


def integrity_digest(asar_integrity):
    """SHA256 over each Info.plist entry's key, algorithm and hash, keys sorted."""
    hasher = hashlib.sha256()
    for key in sorted(asar_integrity):
        entry = asar_integrity[key]
        for part in (key, entry["algorithm"], entry["hash"]):
            hasher.update(part.encode())
    return hasher.digest()


# ---------------------------------------------------------------- official build

def http_request(url):
    # The CDN rejects Python's default User-Agent.
    return urllib.request.Request(url, headers={"User-Agent": "Codex Sparkle/2"})


def fetch_appcast(current_version):
    params = {
        "installation_id": (Path.home() / ".codex/installation_id").read_text().strip(),
        "arch": "arm64" if platform.machine() == "arm64" else "x64",
        "app_version": current_version,
        "beta": "false",
        "os-version": platform.mac_ver()[0],
    }
    url = f"{APPCAST}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(http_request(url), timeout=60) as response:
        root = ET.fromstring(response.read())
    ns = {"sparkle": "http://www.andymatuschak.org/xml-namespaces/sparkle"}
    items = []
    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        build = item.findtext("sparkle:version", namespaces=ns)
        if enclosure is None or not build:
            continue
        signature = enclosure.get(f"{{{ns['sparkle']}}}edSignature")
        items.append({
            "build": int(build),
            "version": item.findtext("sparkle:shortVersionString", namespaces=ns),
            "url": enclosure.get("url"),
            "length": int(enclosure.get("length", "0")),
            "ed_signature": signature,
        })
    if not items:
        raise Abort("Appcast has no downloadable items")
    return max(items, key=lambda item: item["build"])


def download_official(item):
    official = DATA / "official" / str(item["build"]) / "ChatGPT.app"
    if official.exists() and official_signature(official):
        return official
    downloads = DATA / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / Path(urllib.parse.urlparse(item["url"]).path).name
    if not archive.exists() or archive.stat().st_size != item["length"]:
        log(f"downloading {item['url']} ({item['length'] / 1e6:.0f} MB)")
        partial = archive.with_suffix(".part")
        with urllib.request.urlopen(http_request(item["url"]), timeout=120) as response, \
                partial.open("wb") as output:
            shutil.copyfileobj(response, output, 8 * 1024 * 1024)
        partial.replace(archive)
    if archive.stat().st_size != item["length"]:
        raise Abort("Downloaded archive has the wrong size")

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(SPARKLE_ED_KEY))
    try:
        key.verify(base64.b64decode(item["ed_signature"]), archive.read_bytes())
    except InvalidSignature:
        archive.unlink()
        raise Abort("Sparkle EdDSA signature check failed; archive discarded")
    log("Sparkle EdDSA signature verified")

    extract = official.parent
    if extract.exists():
        shutil.rmtree(extract)
    extract.mkdir(parents=True)
    run(["ditto", "-x", "-k", str(archive), str(extract)])
    if not official.exists() or not official_signature(official):
        raise Abort("Extracted app is not an intact OpenAI-signed bundle")
    if app_info(official)["build"] != item["build"]:
        raise Abort("Extracted app build differs from the appcast")
    archive.unlink()
    return official


def prune(directory, keep):
    if not directory.exists():
        return
    entries = sorted(directory.iterdir(), key=lambda path: path.stat().st_mtime)
    for path in entries[:-keep] if keep else entries:
        shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink()


# ---------------------------------------------------------------- native-feature scan

LOCALE_KEY = re.compile(r'"(settings\.[\w.-]+)":`')
MESSAGE = re.compile(r"id:`([\w.-]+)`,defaultMessage:`([^`]{1,120})`")
WIDTH_TOPIC = re.compile(
    r"(transcript|conversation|chat|thread|content|message|reading|column)s?[ _-]?width",
    re.IGNORECASE,
)
KNOWN_UNRELATED = {"quickChat.resizeWidth"}


def scan_native(asar, acked):
    """Return ("high"|"medium"|"none", findings) for an official width setting."""
    findings = []
    for path, _ in asar.files():
        if not (path.startswith("webview/assets/") and path.endswith(".js")):
            continue
        text = asar.read(path).decode("utf-8", "ignore")
        for key in set(LOCALE_KEY.findall(text)):
            if "width" in key.lower():
                level = "high" if WIDTH_TOPIC.search(key) or "appearance" in key else "medium"
                findings.append({"level": level, "signal": f"i18n key {key}", "file": path})
        for message_id, message in set(MESSAGE.findall(text)):
            if message_id in KNOWN_UNRELATED or "width" not in message.lower():
                continue
            if message_id.startswith("settings.") and WIDTH_TOPIC.search(message):
                level = "high"
            elif WIDTH_TOPIC.search(message):
                level = "medium"
            else:
                continue
            findings.append({
                "level": level,
                "signal": f"message {message_id}: {message}",
                "file": path,
            })
    unique = {}
    for finding in findings:
        if finding["signal"] not in acked:
            unique.setdefault(finding["signal"], finding)
    findings = sorted(unique.values(), key=lambda f: (f["level"] != "high", f["signal"]))
    levels = {finding["level"] for finding in findings}
    return ("high" if "high" in levels else "medium" if levels else "none"), findings


# ---------------------------------------------------------------- injection

def locate_changes(asar, runtime=False):
    """Find the Appearance settings chunk by stable i18n ids, not minified names.

    With `runtime`, only the webview files are patched; the launcher serves them over CDP
    and handles the suggestion requests itself, so main, preload and the peer helper stay official.
    """
    assets = "webview/assets"
    anchor = re.compile(
        r"children:\[([\w$]+),\(0,([\w$]+)\.jsx\)\(([\w$]+),\{section:`visual`",
    )
    candidates = []
    for name in asar.listdir(assets):
        if name.endswith(".js"):
            text = asar.read(f"{assets}/{name}").decode("utf-8", "ignore")
            if "id:`settings.appearance.visualStyle`" in text \
                    and "id:`settings.appearance.mode`" in text:
                candidates.append((name, text, list(anchor.finditer(text))))
    # The Visual style card: the Mode row followed by the shared `visual` section.
    cards = [c for c in candidates if c[2]]
    if len(cards) != 1 or len(cards[0][2]) != 1:
        raise Abort(
            "Expected one Visual style card; found "
            f"{[(c[0], len(c[2])) for c in candidates]}"
        )
    name, text, (match,) = cards[0]
    if "export{" not in text and "import{" not in text:
        raise Abort("Appearance chunk is no longer an ES module")
    jsx = match.group(2)

    rows = collections.Counter(re.findall(
        r"\(0," + re.escape(jsx) + r"\.jsx\)\(([\w$]+),\{(?:variant:`\w+`,)?label:", text,
    )).most_common(2)
    if not rows or rows[0][1] < 5 or (len(rows) > 1 and rows[0][1] < 3 * rows[1][1]):
        raise Abort(f"SettingsRow component is ambiguous: {rows}")
    settings_row = rows[0][0]

    hooks = collections.Counter(re.findall(
        r"\(0,([\w$]+)\.use(?:State|Id|Effect|Ref|Memo|Callback|SyncExternalStore)\)", text,
    )).most_common(2)
    if not hooks or hooks[0][1] < 3 or hooks[0][0] == jsx:
        raise Abort(f"React namespace is ambiguous: {hooks}")
    react = hooks[0][0]

    css_ok = any(
        b"--thread-content-max-width" in asar.read(path)
        for path, _ in asar.files()
        if path.startswith(f"{assets}/") and path.endswith(".css")
    )
    if not css_ok:
        raise Abort("Transcript layout token --thread-content-max-width is gone")

    # children:[d,(0,$.jsx)(Fa,…  ->  children:[d,(0,$.jsx)(__ltwRow(fc,$,V),{}),(0,$.jsx)(Fa,…
    # __ltwRow memoizes the component so React never sees a new type between renders.
    row = f",(0,{jsx}.jsx)(__ltwRow({react},{jsx},{settings_row}),{{}})"
    patched = (
        'import{createTranscriptWidthSetting as __ltwMake}from"./transcript-width.js";'
        "var __ltwC;function __ltwRow(r,j,v){return __ltwC??=__ltwMake(r,j,v)}"
        + text[:match.end(1)] + row + text[match.end(1):]
    )
    changes = {f"{assets}/{name}": patched.encode()}

    head = (
        '<link rel="stylesheet" href="./assets/transcript-width.css" />\n'
        '    <script type="module" src="./assets/transcript-width.js"></script>\n'
        '    <link rel="stylesheet" href="./assets/prompt-suggestion.css" />\n'
        '    <script type="module" src="./assets/prompt-suggestion.js"></script>\n'
        "  </head>"
    )
    for entry in asar.listdir("webview"):
        if entry.endswith(".html"):
            html = asar.read(f"webview/{entry}").decode()
            if html.count("</head>") != 1:
                raise Abort(f"Unexpected HTML structure in {entry}")
            changes[f"webview/{entry}"] = html.replace("</head>", head).encode()
    for asset in INJECTED_ASSETS:
        changes[f"{assets}/{asset}"] = (ASSETS / asset).read_bytes()
    panel = locate_panel_layout(asar, assets)
    if panel:
        changes[panel["path"]] = panel["content"]
    else:
        log("warning: summary-panel layout not found; pinned summary may overlap wide text")
    peer_patch = None if runtime else locate_peer_authorizer(asar)
    if peer_patch:
        changes[peer_patch["path"]] = peer_patch["content"]
    suggestion = locate_prompt_suggestion(asar, changes, runtime)
    return changes, {
        "settings_chunk": name,
        "jsx": jsx,
        "react": react,
        "settings_row": settings_row,
        "panel_layout": panel and panel["path"],
        "peer_authorizer": peer_patch and peer_patch["path"],
        **suggestion,
    }


# Main-process side of the prompt suggestions. The renderer CSP only allows OpenAI hosts, so
# the request to the model provider runs here, resolved on every call as in
# suggestion_provider(); only GET /models and POST /responses are forwarded, and only for app
# pages (not remote http(s) frames).
SUGGESTION_MAIN = r"""
;(()=>{try{const electron=require("electron"),fs=require("node:fs"),os=require("node:os"),
path=require("node:path");
function toml(file){
  const tables={"":{}};let table=tables[""];
  let text="";try{text=fs.readFileSync(file,"utf8")}catch{return tables}
  for(const line of text.split(/\r?\n/)){
    const head=line.match(/^\s*(\[\[?)([^\[\]]+)\]\]?\s*(?:#.*)?$/);
    if(head){table=head[1]==="[["?{}:(tables[head[2].trim().replace(/["']/g,"")]??={});continue}
    const kv=line.match(/^\s*([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/);
    if(kv)table[kv[1]]=kv[2]??kv[3];
  }
  return tables;
}
function provider(){
  const home=process.env.CODEX_HOME||path.join(os.homedir(),".codex");
  const codex=toml(path.join(home,"config.toml"));
  const chosen=toml(path.join(os.homedir(),"Library/Application Support/codex-plus/config.toml"))[""].provider;
  const name=chosen||codex[""].model_provider;
  const section=name&&codex["model_providers."+name];
  if(chosen&&!section)throw new Error("[model_providers."+chosen+"] not found in config.toml");
  if(section){
    const base=(section.base_url||"").replace(/\/+$/,"");
    if(!/^https?:\/\//.test(base))throw new Error("[model_providers."+name+"] has no http(s) base_url");
    return{base,token:section.experimental_bearer_token||(section.env_key?process.env[section.env_key]:null)};
  }
  let key=process.env.OPENAI_API_KEY;
  if(!key)try{key=JSON.parse(fs.readFileSync(path.join(home,"auth.json"),"utf8")).OPENAI_API_KEY}catch{}
  if(key)return{base:"https://api.openai.com/v1",token:key};
  throw new Error("No model provider for prompt suggestions");
}
electron.ipcMain.handle("local-prompt-suggestion:request",async(event,request)=>{
  const url=event.senderFrame&&event.senderFrame.url||"";
  if(/^https?:/i.test(url))throw new Error("forbidden sender");
  const{base,token}=provider();
  const headers={"content-type":"application/json"};
  if(token)headers.authorization="Bearer "+token;
  const signal=AbortSignal.timeout(60000);
  if(request&&request.op==="models"){
    const response=await fetch(base+"/models",{headers,signal});
    if(!response.ok)throw new Error("GET /models HTTP "+response.status);
    return await response.json();
  }
  if(request&&request.op==="suggest"){
    const b=request.body||{};
    const body={model:String(b.model||""),instructions:String(b.instructions||""),input:b.input,
      reasoning:b.reasoning,max_output_tokens:b.max_output_tokens,stream:false,store:false};
    const response=await fetch(base+"/responses",{method:"POST",headers,body:JSON.stringify(body),signal});
    const json=await response.json().catch(()=>null);
    if(!response.ok)throw new Error("POST /responses HTTP "+response.status+" "+
      JSON.stringify(json&&json.error||json).slice(0,300));
    return json;
  }
  throw new Error("unknown request");
});
}catch(error){console.error("[local-prompt-suggestion] init failed",error)}})();
"""


def locate_prompt_suggestion(asar, changes, runtime=False):
    """Settings rows in General → Composer, plus (unless `runtime`) an IPC handler in main
    and its preload bridge."""
    assets = "webview/assets"
    anchor = re.compile(r"\(0,([\w$]+)\.jsx\)\(([\w$]+),\{withComposerGroup:!1\}\)")
    found = []
    for name in asar.listdir(assets):
        if name.endswith(".js"):
            text = asar.read(f"{assets}/{name}").decode("utf-8", "ignore")
            if "id:`settings.general.composer.groupTitle`" in text or \
                    "U.composerGroupTitle" in text or "withComposerGroup" in text:
                matches = list(anchor.finditer(text))
                if matches:
                    found.append((name, text, matches))
    if len(found) != 1 or not 1 <= len(found[0][2]) <= 3:
        raise Abort(f"Composer settings group is ambiguous: {[(f[0], len(f[2])) for f in found]}")
    name, text, matches = found[0]
    path = f"{assets}/{name}"
    if path in changes:
        # Appearance (Transcript width) lives in the same chunk; build on that patch.
        text = changes[path].decode()
        matches = list(anchor.finditer(text))
    jsx = {m.group(1) for m in matches}
    if len(jsx) != 1:
        raise Abort(f"Composer settings jsx namespace is ambiguous: {jsx}")
    jsx = jsx.pop()

    rows = collections.Counter(re.findall(
        r"\(0," + re.escape(jsx) + r"\.jsx\)\(([\w$]+),\{label:", text,
    )).most_common(2)
    if not rows or rows[0][1] < 5 or (len(rows) > 1 and rows[0][1] < 3 * rows[1][1]):
        raise Abort(f"Composer SettingsRow is ambiguous: {rows}")
    settings_row = rows[0][0]
    switches = collections.Counter(re.findall(
        r"\(0,[\w$]+\.jsx\)\(([\w$]+),\{(?:\.\.\.[\w$]+,)?checked:", text,
    )).most_common(2)
    if not switches or switches[0][1] < 3 or \
            (len(switches) > 1 and switches[0][1] < 3 * switches[1][1]):
        raise Abort(f"Switch component is ambiguous: {switches}")
    switch = switches[0][0]
    hooks = collections.Counter(re.findall(
        r"\(0,([\w$]+)\.use(?:State|Id|Effect|Ref|Memo|Callback|SyncExternalStore)\)", text,
    )).most_common(2)
    if not hooks or hooks[0][1] < 3:
        raise Abort(f"Composer settings React namespace is ambiguous: {hooks}")
    react = hooks[0][0]
    # The dropdown other General rows use: Dropdown({align,triggerButton:Trigger(...),
    # children:Menu.RadioGroup(...)}), and the secondary toolbar button ("View").
    dropdowns = collections.Counter(re.findall(
        r"\(0,[\w$]+\.jsx\)\(([\w$]+),\{align:`end`,triggerButton:\(0,[\w$]+\.jsx\)\("
        r"([\w$]+),\{\.\.\.[\w$]+,.{0,120}?children:\(0,[\w$]+\.jsx\)\(([\w$]+)\.RadioGroup,",
        text,
    )).most_common(2)
    if not dropdowns or (len(dropdowns) > 1 and dropdowns[0][1] < 2 * dropdowns[1][1]):
        raise Abort(f"Settings dropdown components are ambiguous: {dropdowns}")
    dropdown, trigger, menu = dropdowns[0][0]
    buttons = collections.Counter(re.findall(
        r"\(0,[\w$]+\.jsx\)\(([\w$]+),\{color:`secondary`,size:`toolbar`,", text,
    )).most_common(2)
    if not buttons or (len(buttons) > 1 and buttons[0][1] < 2 * buttons[1][1]):
        raise Abort(f"Settings button component is ambiguous: {buttons}")
    button = buttons[0][0]

    ui = (f"{{SettingsRow:{settings_row},Switch:{switch},Dropdown:{dropdown},"
          f"DropdownTrigger:{trigger},Menu:{menu},Button:{button}}}")
    row = f",(0,{jsx}.jsx)(__lpsRow({react},{jsx},()=>({ui})),{{}})"
    patched = text
    for match in reversed(matches):
        patched = patched[:match.end()] + row + patched[match.end():]
    # The ui object is built lazily: these bindings are module-level lets initialized later.
    changes[path] = (
        'import{createPromptSuggestionSetting as __lpsMake}from"./prompt-suggestion.js";'
        "var __lpsC;function __lpsRow(r,j,u){return __lpsC??=__lpsMake(r,j,u())}"
        + patched
    ).encode()
    located = {
        "suggestion_settings_chunk": name,
        "suggestion_settings_rows": len(matches),
        "suggestion_ui": ui,
    }
    if runtime:
        return located

    mains = [p for p, _ in asar.files()
             if p.startswith(".vite/build/main-") and p.endswith(".js")
             and "/" not in p[len(".vite/build/"):]]
    if len(mains) != 1:
        raise Abort(f"Expected one main bundle, found {mains}")
    main_path = mains[0]
    main = changes.get(main_path) or asar.read(main_path)
    main = main.decode()
    if "require(" not in main[:200] or "local-prompt-suggestion:request" in main:
        raise Abort("Main bundle is not the expected CommonJS module")
    changes[main_path] = _before_source_map(main, SUGGESTION_MAIN).encode()

    preload_path = ".vite/build/preload.js"
    preload = asar.read(preload_path).decode()
    electron = re.match(r'(?:let|const|var) ([\w$]+)=require\("electron"\)', preload)
    if not electron or "exposeInMainWorld(`electronBridge`" not in preload:
        raise Abort("Preload no longer starts with the electron require")
    e = electron.group(1)
    changes[preload_path] = _before_source_map(preload, (
        f';try{{{e}.contextBridge.exposeInMainWorld("__localPromptSuggestion",'
        f'{{request:r=>{e}.ipcRenderer.invoke("local-prompt-suggestion:request",r)}})}}catch{{}}\n'
    )).encode()
    return {**located, "suggestion_main": main_path}


def _before_source_map(text, addition):
    marker = text.rfind("\n//# sourceMappingURL=")
    return text + addition if marker < 0 else text[:marker] + "\n" + addition + text[marker:]


def locate_peer_authorizer(asar):
    if not (ASSETS / PEER_ASSET).is_file():
        raise Abort(f"Build the local peer helper first: {ASSETS / PEER_ASSET}")
    pattern = re.compile(
        r"return (?P<fd>[\w$]+)==null\?\{authorized:!1,"
        r"reason:`missing-socket-file-descriptor`\}:"
        r"(?P<addon>[\w$]+)\.authorizeSocketPeer\((?P=fd),(?P<dev>[\w$]+)\)"
    )
    found = []
    for path, _ in asar.files():
        if not path.startswith(".vite/build/main-") or not path.endswith(".js"):
            continue
        text = asar.read(path).decode()
        for match in pattern.finditer(text):
            found.append((path, text, match))
    if len(found) != 1:
        raise Abort(f"App peer-authorizer anchor is ambiguous ({len(found)} matches)")
    path, text, match = found[0]
    fd, addon, dev = (match.group(key) for key in ("fd", "addon", "dev"))
    replacement = (
        f"return {fd}==null?{{authorized:!1,reason:`missing-socket-file-descriptor`}}:"
        f"((fd,dev)=>{{let verdict={addon}.authorizeSocketPeer(fd,dev);"
        "if(verdict.authorized||verdict.reason!==`missing-code-signing-identity`)"
        "return verdict;try{"
        "let paths=require(`node:path`),resources=process.resourcesPath;"
        f"return require(paths.join(resources,`native`,`{PEER_ASSET}`))"
        ".authorizeLocalChild(fd);"
        "}catch{return verdict}})("
        f"{fd},{dev})"
    )
    return {"path": path, "content": (text[:match.start()] + replacement +
                                    text[match.end():]).encode()}


def locate_panel_layout(asar, assets):
    """The pinned summary picks gutter/shift/overlay from a hard-coded column width (736).

    Point it at the live column width so a wider transcript keeps the panel beside the
    text, or shifts / overlays it, instead of drawing the panel on top of the text.
    """
    gutter = re.compile(r"\(([\w$]+)-(\d{3,4})\)/2")
    found = []
    for name in asar.listdir(assets):
        if not name.endswith(".js"):
            continue
        text = asar.read(f"{assets}/{name}").decode("utf-8", "ignore")
        if all(f"`{mode}`" in text for mode in ("overlay", "shift", "gutter")):
            matches = list(gutter.finditer(text))
            if len(matches) == 1 and len(text) < 20000:
                found.append((name, text, matches[0]))
    if len(found) != 1:
        return None
    name, text, match = found[0]
    replacement = (
        f"({match.group(1)}-(globalThis.__ltwColumnWidth?.()??{match.group(2)}))/2"
    )
    patched = text[:match.start()] + replacement + text[match.end():]
    return {"path": f"{assets}/{name}", "content": patched.encode()}


# ---------------------------------------------------------------- build

def build_bundle(app, changes, marker):
    archive_path = app / "Contents/Resources/app.asar"
    info_path = app / "Contents/Info.plist"
    asar = Asar(archive_path)
    info = plistlib.loads(info_path.read_bytes())
    old_integrity = json.loads(json.dumps(info["ElectronAsarIntegrity"]))
    if hashlib.sha256(asar.raw).hexdigest() != \
            old_integrity["Resources/app.asar"]["hash"]:
        raise Abort("Base app.asar does not match its Info.plist integrity")

    source_nodes = {path: json.dumps(node, sort_keys=True) for path, node in asar.files()}
    data_length = archive_path.stat().st_size - asar.data_start
    appended = []
    for path, payload in changes.items():
        if path in source_nodes:
            node = asar.node(path)
            if "link" in node:
                raise Abort(f"Expected a packed regular file: {path}")
        else:
            node = asar.node(path, create=True)
            node.clear()
        node.update({
            "offset": str(data_length),
            "size": len(payload),
            "integrity": file_integrity(payload),
        })
        data_length += len(payload)
        appended.append(payload)

    raw = json.dumps(asar.header, separators=(",", ":"), ensure_ascii=False).encode()
    padding = (-len(raw)) % 4
    payload_size = 4 + len(raw) + padding
    temporary = archive_path.with_suffix(".asar.tmp")
    with temporary.open("xb") as output:
        output.write(struct.pack("<4I", 4, 4 + payload_size, payload_size, len(raw)))
        output.write(raw)
        output.write(b"\0" * padding)
        with archive_path.open("rb") as source:
            source.seek(asar.data_start)
            shutil.copyfileobj(source, output, 8 * 1024 * 1024)
        for payload in appended:
            output.write(payload)

    rebuilt = Asar(temporary)
    for path, node in rebuilt.files():
        recorded = node.get("integrity")
        if path in changes:
            if rebuilt.read(path) != changes[path]:
                raise Abort(f"Rebuilt member differs: {path}")
        elif json.dumps(node, sort_keys=True) != source_nodes.get(path):
            raise Abort(f"Unrelated ASAR metadata changed: {path}")
        if recorded and path in changes:
            if recorded != file_integrity(rebuilt.read(path)):
                raise Abort(f"Member integrity mismatch: {path}")
    temporary.replace(archive_path)

    new_integrity = json.loads(json.dumps(old_integrity))
    new_integrity["Resources/app.asar"]["hash"] = hashlib.sha256(rebuilt.raw).hexdigest()
    info["ElectronAsarIntegrity"] = new_integrity
    info[MARKER_KEY] = marker
    # Sparkle cannot install over a re-signed bundle (Team ID mismatch); this tool updates it.
    info["SUEnableAutomaticChecks"] = False
    info["SUAllowsAutomaticUpdates"] = False
    if any(PEER_ASSET.encode() in payload for payload in changes.values()):
        helper = ASSETS / PEER_ASSET
        shutil.copy2(helper, app / "Contents/Resources/native" / PEER_ASSET)
        info[PEER_MARKER] = {
            "version": 1, "sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        }
    info_path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML, sort_keys=False))
    update_framework_digest(app, old_integrity, new_integrity)


def update_framework_digest(app, old_integrity, new_integrity):
    """The runtime rejects an Info.plist integrity dict whose digest the framework lacks."""
    binary = app / FRAMEWORK_BINARY
    load_commands = run(["otool", "-l", str(binary)]).stdout
    match = re.search(
        r"sectname __asar_integrity\n\s+segname \S+\n\s+addr 0x[0-9a-f]+\n"
        r"\s+size (0x[0-9a-f]+)\n\s+offset (\d+)",
        load_commands,
    )
    if not match:
        raise Abort("Framework has no __asar_integrity section")
    size, offset = int(match.group(1), 16), int(match.group(2))
    with binary.open("r+b") as stream:
        stream.seek(offset)
        section = stream.read(size)
        if size != 66 or section[:32] != DIGEST_SENTINEL or section[32:34] != b"\1\1":
            raise Abort("Unsupported __asar_integrity layout")
        if section[34:] != integrity_digest(old_integrity):
            raise Abort("Framework digest does not match the official Info.plist")
        stream.seek(offset + 34)
        stream.write(integrity_digest(new_integrity))


def ensure_app_tools_signing_compatible(app):
    """Require the bounded local-child verifier before re-signing App tools."""
    authorizer = app / "Contents/Resources/native/browser-use-peer-authorization.node"
    if not authorizer.is_file():
        return
    binary = authorizer.read_bytes()
    if all(marker in binary for marker in (
        b"authorizeSocketPeer", b"missing-code-signing-identity",
        b"untrusted-code-signing-identity",
    )):
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        helper = app / "Contents/Resources/native" / PEER_ASSET
        marker = info.get(PEER_MARKER, {})
        if (marker.get("version") != 1 or not helper.is_file() or
                marker.get("sha256") != hashlib.sha256(helper.read_bytes()).hexdigest()):
            raise Abort("Local signing requires the verified App-tools peer helper")
        asar = Asar(app / "Contents/Resources/app.asar")
        matches = [path for path, _ in asar.files()
                   if path.startswith(".vite/build/main-") and path.endswith(".js")
                   and b".authorizeLocalChild(" in asar.read(path)]
        if len(matches) != 1:
            raise Abort("Local App-tools peer helper is not connected")


def signing_identity():
    """SHA-1 of the valid Apple Development identity for sign_team() that expires last."""
    team = sign_team()
    valid = set(re.findall(r'^\s*\d+\) ([0-9A-F]{40}) "Apple Development: ',
                           run(["security", "find-identity", "-v", "-p", "codesigning"]).stdout,
                           re.M))
    listing = run(["security", "find-certificate", "-a", "-c", "Apple Development", "-Z", "-p"])
    candidates = []
    for sha1, pem in re.findall(r"SHA-1 hash: ([0-9A-F]{40})\n(-----BEGIN CERTIFICATE-----"
                                r".*?-----END CERTIFICATE-----)", listing.stdout, re.S):
        if sha1 not in valid:
            continue
        fields = run(["/usr/bin/openssl", "x509", "-noout", "-subject", "-enddate"],
                     input=pem).stdout
        expiry = re.search(r"notAfter=(.+)", fields)
        if f"/OU={team}/" in fields and expiry:
            moment = datetime.datetime.strptime(expiry[1].strip(), "%b %d %H:%M:%S %Y %Z")
            candidates.append((moment, sha1))
    if not candidates:
        raise Abort(f"No valid Apple Development identity for team {team}; "
                    "renew it in Xcode → Settings → Accounts")
    return max(candidates)[1]


def sign_bundle(app):
    """Re-sign inside-out with the Apple Development identity; every loader of the framework gets DLV."""
    ensure_app_tools_signing_compatible(app)
    identity = signing_identity()
    versions = app / "Contents/Frameworks/Codex Framework.framework/Versions/Current"
    helpers = sorted((versions / "Helpers").iterdir())
    for target in [*helpers, app]:
        entitlements = subprocess.run(
            ["codesign", "-d", "--entitlements", "-", "--xml", str(target)],
            capture_output=True,
        ).stdout
        values = plistlib.loads(entitlements) if entitlements.strip() else {}
        for key in RESTRICTED_ENTITLEMENTS:
            values.pop(key, None)
        values["com.apple.security.cs.disable-library-validation"] = True
        with tempfile.NamedTemporaryFile(suffix=".plist", delete=False) as handle:
            handle.write(plistlib.dumps(values))
        try:
            if target == app:
                run(["codesign", "--force", "--sign", identity, "--timestamp=none",
                     str(versions)])
            run([
                "codesign", "--force", "--sign", identity, "--timestamp=none",
                "--options", "runtime",
                "--entitlements", handle.name, str(target),
            ])
        finally:
            os.unlink(handle.name)
    run(["codesign", "--verify", "--deep", "--strict", str(app)])
    details = run(["codesign", "-dv", str(app)])
    if f"TeamIdentifier={sign_team()}" not in details.stdout + details.stderr:
        raise Abort(f"Signed bundle does not carry team {sign_team()}")


# ---------------------------------------------------------------- smoke test

CDP_PROBE = r"""
const [port, chunk, suggestionChunk] = process.argv.slice(2);
const deadline = Date.now() + 60000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let target;
while (!target && Date.now() < deadline) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    target = list.find((t) => t.type === "page" && t.url.startsWith("app://-/index.html"));
  } catch {}
  if (!target) await sleep(500);
}
if (!target) { console.log(JSON.stringify({ error: "main window never loaded" })); process.exit(0); }
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
const expression = `(async () => {
  for (let i = 0; i < 100 && !document.body?.dataset.localTranscriptWidth; i++)
    await new Promise((r) => setTimeout(r, 200));
  const body = document.body;
  let chunkError = null;
  try { await import("./assets/${chunk}"); } catch (e) { chunkError = String(e); }
  try { await import("./assets/${suggestionChunk}"); } catch (e) { chunkError ??= String(e); }
  const bridge = window.__localPromptSuggestion;
  let models = null;
  try { models = (await bridge.request({ op: "models" }))?.data?.length ?? 0; }
  catch (e) { models = String(e).slice(0, 200); }
  return JSON.stringify({
    suggestBridge: typeof bridge?.request === "function",
    suggestModule: Boolean(document.querySelector('script[src$="prompt-suggestion.js"]')),
    models,
    width: body?.dataset.localTranscriptWidth ?? null,
    token: getComputedStyle(body).getPropertyValue("--thread-content-max-width").trim(),
    chunkError,
  });
})()`;
ws.send(JSON.stringify({ id: 1, method: "Runtime.evaluate",
  params: { expression, awaitPromise: true, returnByValue: true } }));
ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.id !== 1) return;
  console.log(message.result?.result?.value ?? JSON.stringify({ error: JSON.stringify(message) }));
  process.exit(0);
};
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def smoke_test(app, settings_chunk, suggestion_chunk):
    """Launch the staged app (the real profile is used: owl ignores --user-data-dir)."""
    port = free_port()
    logs = DATA / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stderr_path = logs / "smoke-test.log"
    env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}
    with stderr_path.open("w") as stderr, tempfile.NamedTemporaryFile(
        "w", suffix=".mjs", delete=False,
    ) as probe:
        probe.write(CDP_PROBE)
        probe.flush()
        process = subprocess.Popen(
            [str(app / "Contents/MacOS/ChatGPT"), "--enable-logging=stderr",
             f"--remote-debugging-port={port}"],
            stdout=stderr, stderr=subprocess.STDOUT, env=env, start_new_session=True,
        )
        try:
            result = subprocess.run(
                ["node", probe.name, str(port), settings_chunk, suggestion_chunk],
                capture_output=True, text=True, timeout=120,
            )
            tool_deadline = time.monotonic() + 35
            tool_logs = []
            while time.monotonic() < tool_deadline and process.poll() is None:
                log_root = Path.home() / "Library/Logs/com.openai.codex"
                candidates = list(log_root.glob(f"*/*/*/*-{process.pid}-t0-*.log"))
                tool_logs = [
                    str(path) for path in candidates
                    if any("server=codex_app " in line and "status=ready" in line
                           for line in path.read_text(errors="ignore").splitlines())
                ]
                if tool_logs:
                    break
                time.sleep(1)
            alive = process.poll() is None
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=20)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            os.unlink(probe.name)
    output = stderr_path.read_text(errors="ignore")
    fatal = [line for line in output.splitlines() if ":FATAL:" in line]
    if fatal:
        raise Abort(f"Staged app crashed: {fatal[0][-200:]}")
    if not alive:
        raise Abort("Staged app exited during the smoke test")
    if not tool_logs:
        raise Abort("Staged App tools did not become ready; the bundle is not installed")
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise Abort(f"Smoke probe failed: {result.stdout[-300:]} {result.stderr[-300:]}")
    if report.get("error"):
        raise Abort(f"Smoke probe: {report['error'][:300]}")
    if report["chunkError"]:
        raise Abort(f"Patched settings chunk failed to load: {report['chunkError']}")
    if not report.get("suggestBridge") or not report.get("suggestModule"):
        raise Abort(f"Prompt suggestion bridge not available: {report}")
    width = report["width"]
    if width not in WIDTHS_PX or report["token"] != f"{WIDTHS_PX[width]}px":
        raise Abort(f"Width layout not applied: {report}")
    report["app_tools_ready"] = True
    report["app_tools_logs"] = tool_logs
    log(f"smoke test passed: {report}")
    return report


# ---------------------------------------------------------------- install

def wait_for_quit(hours):
    deadline = time.time() + hours * 3600
    notified = False
    while app_running():
        if time.time() >= deadline:
            return False
        if not notified:
            notify("等待退出", "新版已准备好，退出 ChatGPT 后会自动安装")
            notified = True
        time.sleep(120)
    return True


def install_bundle(staged, label):
    if app_running():
        raise Abort("ChatGPT is running; refusing to swap the bundle")
    backups = DATA / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backups / f"ChatGPT-{label}-{stamp}.app"
    os.rename(APP, backup)
    try:
        os.rename(staged, APP)
    except OSError:
        os.rename(backup, APP)
        raise
    subprocess.run([
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister", "-f", str(APP),
    ], check=False, capture_output=True)
    prune(backups, keep=2)
    log(f"installed; previous app kept at {backup}")


def set_builtin_updater(enabled):
    """User defaults override Info.plist, so the built-in Sparkle switches are set here too."""
    flag = "true" if enabled else "false"
    for key in ("SUEnableAutomaticChecks", "SUAutomaticallyUpdate"):
        subprocess.run(["defaults", "write", BUNDLE_ID, key, "-bool", flag],
                       check=False, capture_output=True)


# ---------------------------------------------------------------- commands

def official_base(installed, state):
    """Newest verified official bundle, reusing the installed app when it is official."""
    try:
        item = fetch_appcast(installed["version"] or "0")
    except Exception as error:  # network or feed problems
        log(f"appcast unavailable: {error}")
        item = None
    if official_signature(APP) and (item is None or installed["build"] >= item["build"]):
        cache = DATA / "official" / str(installed["build"]) / "ChatGPT.app"
        if not (cache.exists() and official_signature(cache)):
            clone_tree(APP, cache)
        return cache
    if item is None:
        cached = sorted((DATA / "official").glob("*/ChatGPT.app"),
                        key=lambda p: int(p.parent.name)) if (DATA / "official").exists() else []
        if cached and official_signature(cached[-1]):
            return cached[-1]
        raise Abort("No appcast and no cached official build")
    return download_official(item)


def command_run(args):
    state = load_state()
    if state.get("native", {}).get("level") == "high" and not args.force:
        log("official Transcript width detected earlier; nothing to do")
        return 0
    installed = app_info(APP)
    marker = patch_id()
    base = official_base(installed, state)
    base_info = app_info(base)
    log(f"installed {installed['version']} ({installed['build']}) marker={installed['marker']}; "
        f"official {base_info['version']} ({base_info['build']}); patch {marker}")
    state.update({"last_check": datetime.datetime.now().isoformat(timespec="seconds"),
                  "official": base_info})

    if (installed["marker"] == marker and installed["build"] == base_info["build"]
            and not args.force):
        log("already up to date")
        save_state(state)
        return 0

    asar = Asar(base / "Contents/Resources/app.asar")
    level, findings = scan_native(asar, set(state.get("acked_signals", [])))
    report_dir = DATA / "scans"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{base_info['build']}.json").write_text(json.dumps(
        {"version": base_info, "level": level, "findings": findings},
        indent=2, ensure_ascii=False,
    ))
    if level != "none":
        state["native"] = {"level": level, "build": base_info["build"], "findings": findings}
        save_state(state)
        if installed["marker"] or installed["build"] < base_info["build"] \
                or not official_signature(APP):
            if not wait_for_quit(args.wait_hours):
                raise Abort("ChatGPT still running; official build not installed yet")
            staged = DATA / "stage" / f"{base_info['build']}-official" / "ChatGPT.app"
            clone_tree(base, staged)
            install_bundle(staged, f"{installed['version']}")
            set_builtin_updater(True)
        if level == "high":
            notify("官方已提供对话宽度", "已安装纯官方版并停用补丁和每日检查")
            uninstall_agent(quiet=True)
        else:
            notify("疑似官方已提供对话宽度",
                   f"已安装纯官方版。确认不是后运行: codex_tw.py ack ；详情见 scans/{base_info['build']}.json")
        return 0

    changes, located = locate_changes(asar)
    log(f"injection points: {located}")
    staged = DATA / "stage" / f"{base_info['build']}-{marker}" / "ChatGPT.app"
    clone_tree(base, staged)
    build_bundle(staged, changes, marker)
    sign_bundle(staged)
    log(f"staged bundle built and signed: {staged}")
    if args.no_install:
        if not app_running():
            smoke_test(staged, located["settings_chunk"], located["suggestion_settings_chunk"])
        log("--no-install: stopping before the bundle swap")
        return 0

    if not wait_for_quit(args.wait_hours):
        state["pending"] = str(staged)
        save_state(state)
        notify("待安装", "ChatGPT 一直在运行，下次检查时再安装")
        return 0
    smoke_test(staged, located["settings_chunk"], located["suggestion_settings_chunk"])
    install_bundle(staged, f"{installed['version']}")
    set_builtin_updater(False)
    shutil.rmtree(staged.parent, ignore_errors=True)
    state.pop("pending", None)
    state["installed"] = {**app_info(APP), "located": located}
    save_state(state)
    prune(DATA / "official", keep=2)
    prune(DATA / "stage", keep=0)
    notify("已更新", f"{base_info['version']} 已安装，对话宽度设置可用")
    return 0


def command_status(_args):
    installed = app_info(APP)
    print(json.dumps({
        "installed": installed,
        "official_signature": official_signature(APP),
        "current_patch": patch_id(),
        "agent_installed": AGENT_PLIST.exists(),
        "state": load_state(),
    }, indent=2, ensure_ascii=False))
    return 0


def command_restore(args):
    """Install the newest official build, without the patch."""
    installed = app_info(APP)
    base = official_base(installed, load_state())
    if official_signature(APP) and not installed["marker"] \
            and app_info(base)["build"] <= installed["build"]:
        log("installed app is already official")
        return 0
    if not wait_for_quit(args.wait_hours):
        raise Abort("Quit ChatGPT first")
    staged = DATA / "stage" / "restore" / "ChatGPT.app"
    clone_tree(base, staged)
    install_bundle(staged, f"{installed['version']}")
    set_builtin_updater(True)
    log("official build installed")
    return 0


def command_ack(_args):
    state = load_state()
    native = state.get("native")
    if not native:
        print("no pending native-feature findings")
        return 0
    state.setdefault("acked_signals", []).extend(f["signal"] for f in native["findings"])
    state.pop("native", None)
    save_state(state)
    print(f"acknowledged {len(native['findings'])} signal(s); next run will patch again")
    return 0


def install_agent(_args):
    logs = DATA / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [PYTHON, str(ROOT / "codex_tw.py"), "run", "--wait-hours", "3"],
        "StartCalendarInterval": {"Hour": 4, "Minute": 30},
        "StandardOutPath": str(logs / "run.log"),
        "StandardErrorPath": str(logs / "run.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "ProcessType": "Background",
    }))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    run(["launchctl", "bootstrap", domain, str(AGENT_PLIST)])
    log(f"launchd agent {LABEL} installed (daily 04:30)")
    return 0


def uninstall_agent(_args=None, quiet=False):
    if AGENT_PLIST.exists():
        AGENT_PLIST.unlink()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                   capture_output=True)
    if not quiet:
        log("launchd agent removed")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="check, patch, test and install if needed")
    run_parser.add_argument("--force", action="store_true", help="rebuild even if current")
    run_parser.add_argument("--no-install", action="store_true",
                            help="build, sign and smoke-test only")
    run_parser.add_argument("--wait-hours", type=float, default=0,
                            help="wait this long for ChatGPT to quit before installing")
    run_parser.set_defaults(func=command_run)
    restore_parser = sub.add_parser("restore", help="install the newest official build")
    restore_parser.add_argument("--wait-hours", type=float, default=0)
    restore_parser.set_defaults(func=command_restore)
    sub.add_parser("status", help="show installed and cached state").set_defaults(
        func=command_status)
    sub.add_parser("ack", help="mark suspected native-feature signals as unrelated").set_defaults(
        func=command_ack)
    sub.add_parser("install-agent", help="run daily at 04:30 via launchd").set_defaults(
        func=install_agent)
    sub.add_parser("uninstall-agent", help="stop the daily run").set_defaults(
        func=uninstall_agent)
    args = parser.parse_args()
    try:
        return args.func(args)
    except Abort as error:
        notify("未完成，已保持现有 App", str(error)[:180])
        return 2
    except Exception as error:
        notify("脚本出错，已保持现有 App", f"{type(error).__name__}: {error}"[:180])
        raise


if __name__ == "__main__":
    sys.exit(main())
