#!/bin/bash
# Install, update or remove codex-plus.
#
#   curl -fsSL https://raw.githubusercontent.com/congee949/codex-plus/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/congee949/codex-plus/main/install.sh | bash -s -- --uninstall
#
# Options: --no-agent, --no-app, --uninstall, --purge (with --uninstall), --help.
# Environment: CODEX_PLUS_HOME (install directory, default ~/.codex-plus),
# CODEX_PLUS_REF (branch or tag, default main), CODEX_PLUS_SOURCE (a local checkout to copy
# instead of downloading), CODEX_PLUS_PYTHON (interpreter).
set -euo pipefail

repo=${CODEX_PLUS_REPO:-congee949/codex-plus}
ref=${CODEX_PLUS_REF:-main}
dest=${CODEX_PLUS_HOME:-$HOME/.codex-plus}
app=/Applications/ChatGPT.app
data="$HOME/Library/Application Support/codex-plus"
wrapper="$HOME/Applications/Codex.app"
action=install agent=1 wrapper_app=1 purge=0

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31m错误：\033[0m%s\n' "$*" >&2; exit 1; }

for arg in "$@"; do
  case $arg in
    --no-agent) agent=0 ;;
    --no-app) wrapper_app=0 ;;
    --uninstall) action=uninstall ;;
    --purge) purge=1 ;;
    -h|--help) echo "用法：install.sh [--no-agent] [--no-app] [--uninstall [--purge]]"; exit 0 ;;
    *) fail "未知参数 $arg" ;;
  esac
done

[ "$(uname -s)" = Darwin ] || fail "codex-plus 只支持 macOS"

find_python() {
  for candidate in "${CODEX_PLUS_PYTHON:-}" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
      echo "$candidate"
      return
    fi
  done
  return 1
}

is_checkout() { [ -f "$1/codex_launch.py" ] && [ -f "$1/codex_tw.py" ]; }

uninstall() {
  python=$(find_python || true)
  if [ -n "$python" ] && is_checkout "$dest"; then
    "$python" "$dest/codex_launch.py" uninstall-agent >/dev/null
  else
    launchctl bootout "gui/$(id -u)/local.codex-plus.watch" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/local.codex-plus.watch.plist"
  fi
  if [ -d "$wrapper" ] && [ "$(defaults read "$wrapper/Contents/Info" CFBundleIdentifier 2>/dev/null)" = local.codex-plus.launcher ]; then
    rm -rf "$wrapper"
  fi
  if is_checkout "$dest"; then rm -rf "$dest"; fi
  if [ "$purge" = 1 ]; then rm -rf "$data"; fi
  say "已卸载。正在运行的 ChatGPT 退出后即恢复原版。"
  [ "$purge" = 1 ] || say "设置和日志保留在 $data"
}

install() {
  [ -d "$app" ] || fail "没有找到 $app，请先安装 Codex 桌面版（ChatGPT.app）"
  [ "$(defaults read "$app/Contents/Info" CFBundleIdentifier 2>/dev/null)" = com.openai.codex ] \
    || fail "$app 不是 Codex 桌面版（com.openai.codex）"
  python=$(find_python) || fail "需要 Python 3.9 或更新版本；运行 xcode-select --install 安装命令行工具后重试"

  if [ -e "$dest" ] && ! is_checkout "$dest"; then
    fail "$dest 已存在且不是 codex-plus，请设置 CODEX_PLUS_HOME 换一个目录"
  fi
  [ ! -e "$dest/.git" ] || fail "$dest 是 git 仓库，请用 git pull 更新"

  staging=$(mktemp -d "${TMPDIR:-/tmp}/codex-plus.XXXXXX")
  trap 'rm -rf "$staging"' EXIT
  if [ -n "${CODEX_PLUS_SOURCE:-}" ]; then
    say "复制 $CODEX_PLUS_SOURCE"
    rsync -a --exclude .git --exclude __pycache__ "$CODEX_PLUS_SOURCE/" "$staging/src/"
  else
    say "下载 $repo@$ref"
    mkdir "$staging/src"
    curl -fsSL "https://github.com/$repo/archive/$ref.tar.gz" | tar -xz -C "$staging/src" --strip-components 1
  fi
  is_checkout "$staging/src" || fail "下载的内容不完整"
  mkdir -p "$(dirname "$dest")"
  if [ -e "$dest" ]; then mv "$dest" "$staging/old"; fi
  mv "$staging/src" "$dest"
  say "已安装到 $dest"

  export CODEX_PLUS_PYTHON=$python
  if [ "$wrapper_app" = 1 ]; then
    "$python" "$dest/codex_launch.py" install-app
  fi
  if [ "$agent" = 1 ]; then
    "$python" "$dest/codex_launch.py" install-agent
  fi
  choose_provider
  say "完成。退出 ChatGPT 后重新打开，设置 → 外观 → 视觉风格里会出现「对话宽度」，设置 → 常规 → 编辑器里会出现「下一句建议」。"
  if [ "$agent" = 0 ]; then
    say "没有安装监视代理：请从「应用程序」或 Spotlight 打开 Codex，直接打开 ChatGPT.app 是原版。"
  fi
}

choose_provider() {
  if "$python" "$dest/codex_launch.py" provider >/dev/null 2>&1; then return; fi
  names=$("$python" -c '
import json, sys
sys.path.insert(0, sys.argv[1])
import codex_tw
try:
    codex = codex_tw.read_toml(codex_tw.CODEX_HOME / "config.toml")
except OSError:
    codex = {}
print(" ".join(sorted(codex.get("model_providers") or {})))' "$dest")
  if [ -z "$names" ] || ! [ -r /dev/tty ]; then
    say "下一句建议还没有可用的模型提供商，配置方法见 https://github.com/$repo/blob/main/docs/guide.md#模型提供商"
    return
  fi
  say "下一句建议需要一个模型提供商。~/.codex/config.toml 里有：$names"
  printf '输入要使用的名字（直接回车跳过）：' >/dev/tty
  read -r choice </dev/tty || choice=
  if [ -n "$choice" ]; then
    "$python" "$dest/codex_launch.py" provider "$choice" >/dev/null || say "没有找到 [model_providers.$choice]，稍后可运行：$python $dest/codex_launch.py provider <名字>"
  fi
}

"$action"
