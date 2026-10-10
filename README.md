# codex-plus

给官方 Codex 桌面版（macOS 上的 ChatGPT.app）加两项 Claude 风格的功能，不修改 App 本身：

- **对话宽度**：设置 → 外观 → 视觉风格里选窄 / 中 / 宽（768 / 960 / 1280 px），对话记录和输入框一起变宽。
- **下一句建议**：每轮回复结束后，由一个模型预测你接下来可能发的话，以灰字显示在空输入框里。按 Tab 填入，按 Esc 关闭，直接打字即可替换。在设置 → 常规 → 编辑器里开关并选择模型。

App 保持 OpenAI 原版签名，内置更新、App tools 和钥匙串照常工作。

## 安装

需要 macOS、安装在 `/Applications` 的 Codex 桌面版，以及 Python 3.9 或更新版本（装过 Xcode 命令行工具或 Homebrew Python 即可）。在终端运行：

```bash
curl -fsSL https://raw.githubusercontent.com/congee949/codex-plus/main/install.sh | bash
```

安装脚本把 codex-plus 放到 `~/.codex-plus`，在 `~/Applications` 创建一个 Codex.app 启动器，并装上一个登录后常驻的监视代理。之后退出 ChatGPT 再打开，从 Dock、Spotlight 或启动器打开都带有这两项功能。

也可以让 Codex、Claude Code 等 Agent 代为安装和配置：

```bash
npx skills add congee949/codex-plus
```

然后对 Agent 说「安装 codex-plus」。

## 下一句建议用哪个模型

建议请求走一个兼容 OpenAI Responses API 的模型提供商。默认使用 `~/.codex/config.toml` 当前的 `model_provider`；用 OpenAI API Key 登录的 Codex 直接使用 OpenAI。要指定 `[model_providers.<名字>]` 中的某一个，运行：

```bash
python3 ~/.codex-plus/codex_launch.py provider <名字>
```

完整规则见[使用指南](docs/guide.md#模型提供商)。

## 更新与卸载

重新运行安装命令即可更新。卸载：

```bash
curl -fsSL https://raw.githubusercontent.com/congee949/codex-plus/main/install.sh | bash -s -- --uninstall
```

## 文档

- [使用指南](docs/guide.md)：安装选项、打开方式、模型提供商、命令和常见问题
- [实现说明](docs/implementation-notes.md)：注入方式和下一句建议的规则（英文）
- [重签名构建](docs/resigned-build.md)：把功能直接写进 App 副本的高级模式（英文）

## 许可证

[MIT](LICENSE)
