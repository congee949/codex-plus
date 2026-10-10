# 使用指南

[README](../README.md) 介绍了功能和一键安装。这里说明安装选项、打开方式、模型提供商的配置和日常命令。下文的 `codex_launch.py` 都指 `~/.codex-plus/codex_launch.py`，用 `python3` 运行。

## 安装选项

`install.sh` 接受以下参数，通过 `bash -s --` 传入，例如 `curl -fsSL …/install.sh | bash -s -- --no-agent`：

| 参数 | 作用 |
| --- | --- |
| `--no-agent` | 不装监视代理，只能从启动器打开带功能的 Codex |
| `--no-app` | 不创建 `~/Applications/Codex.app` |
| `--uninstall` | 移除监视代理、启动器 App 和安装目录 |
| `--purge` | 与 `--uninstall` 同用，同时删除设置和日志 |

环境变量：`CODEX_PLUS_HOME` 改安装目录（默认 `~/.codex-plus`），`CODEX_PLUS_REF` 安装指定分支或标签（默认 `main`），`CODEX_PLUS_PYTHON` 指定 Python，`CODEX_PLUS_SOURCE` 从本地目录复制而不下载。

安装目录是 git 仓库时，安装脚本不会覆盖它，请在仓库里用 `git pull` 更新。

## 打开方式

两项功能只在 ChatGPT 经由启动器打开时存在。启动器用只有它自己能访问的调试管道打开原版 App，在页面加载时加入功能代码，并转发下一句建议的请求，原理见[实现说明](implementation-notes.md#launcher)。

- **监视代理**（`local.codex-plus.watch`，默认安装）：ChatGPT 以其他方式启动时，包括 Dock、Finder、`open` 和更新后的自动重启，代理会结束这个新进程并经由启动器重新打开，耗时一两秒。代理启动前已在运行的 ChatGPT 不受影响。Dock 里保留 ChatGPT.app 本身即可。
- **启动器 App**：`~/Applications/Codex.app`，图标与 ChatGPT 相同，Spotlight 和启动台里搜 Codex 可以找到。
- **Raycast**：在 Raycast 设置 → Extensions → Script Commands → Add Directories 里添加 `~/.codex-plus/raycast`，之后可以用 **Open Codex** 命令打开，也能绑定快捷键。
- **命令行**：`python3 ~/.codex-plus/codex_launch.py`。

没有监视代理时，直接打开 ChatGPT.app 得到的是原版。如果原版已在运行，启动器会先询问是否退出并重新打开。

启动器只在打开 ChatGPT 时读取一次功能代码，更新 codex-plus 后需要重新打开 ChatGPT 才生效。再次运行启动器时，如果检测到正在运行的是旧版代码，会提示重启。

## 模型提供商

下一句建议向一个兼容 OpenAI Responses API 的服务发送 `GET /models` 和 `POST /responses` 请求，按以下顺序确定服务（`suggestion_provider` 位于 [`codex_tw.py`](../codex_tw.py)）：

1. `~/Library/Application Support/codex-plus/config.toml` 里的 `provider`，指向 `~/.codex/config.toml` 中的 `[model_providers.<名字>]`。
2. `~/.codex/config.toml` 顶层的 `model_provider`，前提是它对应一个 `[model_providers.<名字>]` 表。
3. OpenAI API Key：环境变量 `OPENAI_API_KEY`，或用 API Key 登录 Codex 后 `~/.codex/auth.json` 里保存的 Key，请求发往 `https://api.openai.com/v1`。

提供商表里读取 `base_url`，以及 `experimental_bearer_token` 或 `env_key` 指定的环境变量作为 Token。经由 Dock 或 Spotlight 打开的进程拿不到终端里设置的环境变量，所以 `env_key` 只在从终端打开时有效，写成 `experimental_bearer_token` 更可靠。用 ChatGPT 账号登录且没有自定义提供商时，需要先添加一个，例如：

```toml
# ~/.codex/config.toml
[model_providers.suggest]
name = "suggest"
base_url = "https://api.example.com/v1"
experimental_bearer_token = "sk-..."
```

查看或设置当前提供商：

```bash
python3 ~/.codex-plus/codex_launch.py provider
```

```bash
python3 ~/.codex-plus/codex_launch.py provider suggest
```

不带参数时输出当前解析结果和 `~/.codex/config.toml` 里所有可选的提供商，解析失败时退出码为 1。修改在下一次请求时生效，不需要重启。

设置里的模型列表来自该服务的 `/models`。建议使用所选模型支持的最低推理档位。

## 下一句建议何时出现

规则沿用 Claude Code：线程刚开始、窗口在后台、有待批准的操作时不生成；连续 20 条建议未被采用后降低频率；不是一句简短用户发言的候选会被丢弃。每条规则和原因代码见[实现说明](implementation-notes.md#prompt-suggestion-rules)。

## 命令

| 命令 | 作用 |
| --- | --- |
| `codex_launch.py [launch]` | 经由启动器打开 ChatGPT |
| `codex_launch.py launch --restart` | 同上，但不询问，直接退出非启动器打开或运行旧版代码的 ChatGPT |
| `codex_launch.py status` | 显示 ChatGPT 是否经由启动器运行，以及运行中的代码是否为最新（`patches_current`） |
| `codex_launch.py provider [名字]` | 查看或设置下一句建议的模型提供商 |
| `codex_launch.py install-app [--path P]` | 创建或刷新启动器 App |
| `codex_launch.py install-agent` / `uninstall-agent` | 安装或移除监视代理 |
| `codex_tw.py restore` | 把最新的官方版本装到 `/Applications`，当前 App 备份到 `backups/` |

`codex_tw.py run` 构建重签名副本，代价和步骤见[重签名构建](resigned-build.md)。

## 文件位置

设置、日志、缓存和备份都在 `~/Library/Application Support/codex-plus/`：

| 路径 | 内容 |
| --- | --- |
| `config.toml` | codex-plus 设置：`provider`，以及重签名构建用的 `sign_team` |
| `logs/launcher.log` | 启动器日志，包括每条建议的结果和被跳过、过滤的原因 |
| `logs/watch.log` | 监视代理日志 |
| `runtime-cache/` | 按 App 版本缓存的注入结果 |
| `launcher.json` | 当前启动器进程的状态 |

宽度和建议开关保存在 App 页面的 localStorage 里，卸载后保留，重新安装即恢复。

## 常见问题

**设置里没有新选项。** 运行 `codex_launch.py status`。`via_launcher` 为 `false` 说明 ChatGPT 不是经由启动器打开的，退出后重新打开即可；`patches_current` 为 `false` 时运行 `codex_launch.py launch --restart`。

**ChatGPT 更新后功能消失。** 启动器每次打开时按新版 App 重新定位注入点。定位失败时，ChatGPT 按原版打开并弹出通知，原因写在 `logs/launcher.log`。

**灰字建议一直不出现。** 先运行 `codex_launch.py provider` 确认提供商可用，再在 `logs/launcher.log` 里查找 `suggestion` 开头的行，其中记录了每轮的结果，例如 `early_conversation`、`unfocused` 或请求错误。

## 卸载

```bash
curl -fsSL https://raw.githubusercontent.com/congee949/codex-plus/main/install.sh | bash -s -- --uninstall
```

正在运行的 ChatGPT 退出后即恢复原版。加 `--purge` 同时删除 `~/Library/Application Support/codex-plus/`。
