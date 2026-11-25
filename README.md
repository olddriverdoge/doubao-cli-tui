# doubao-cli-tui

给社区版 [`doubao-cli`](https://www.npmjs.com/package/doubao-cli)（账号登录、走免费额度）套一个终端界面。

后端仍然是 `doubao headless sessions create/send/wait`，本程序只做三件事：

1. 把三段式的命令收进一个常驻的聊天界面；
2. 把 CLI 打印到 stderr 的终端审批提示转成界面内的确认条；
3. 从 stdout 的 `--json` 回执里取出会话 ID 和回复，自动续接下一轮。

零第三方依赖，只需要 Python 3.8+ 和一个已经登录的 `doubao` 命令。

---

## 为什么需要它

官方 `@bytedance/doubao-cli`（2.38.x）的 `artifacts.json` 只发布三个平台：

```json
"platforms": {
  "linux-x64": {...},
  "darwin-arm64": {...},
  "win32-x64": {...}
}
```

没有 `linux-arm64`，所以 Termux（Android aarch64）装不上官方 TUI。

社区 `doubao-cli` 是纯 JS，能在 Termux 跑，但它的定位是 **headless HTTP / automation 后端**：命令面是 `sessions / projects / models / mcp / automations / runtimes`，没有交互界面，也没有 `stream-json`。

于是这个项目补上缺失的那一层：**不改变后端，只补前端**。

---

## 依赖

- Python 3.8+（Termux 自带 `python` 即可）
- Node.js 22+（社区 CLI 要求）
- 已登录的 `doubao` 命令

```bash
npm i -g doubao-cli@0.23.3
doubao headless login --profile termux
doubao headless runtimes --json        # 应看到 local available / hostPlatform android
```

## 安装

```bash
git clone https://github.com/olddriverdoge/doubao-cli-tui.git ~/doubao-cli-tui
mkdir -p ~/bin
ln -sf ~/doubao-cli-tui/doubao_tui.py ~/bin/doubao-tui
chmod +x ~/doubao-cli-tui/doubao_tui.py
doubao-tui
```

首次启动会写 `~/.config/doubao-tui/config.json`，可以直接改，也可以在界面里用 `/ws`、`/perm`、`/mode` 改。

```json
{
  "bin": "doubao",
  "profile": "termux",
  "workspace": "/data/data/com.termux/files/home/dsh/photos_mask_web",
  "mode": "work",
  "permission": "ask_risky",
  "timeout": 600,
  "auto_approve": [
    "local.workspace.apply:ensure_dir",
    "local.workspace.apply:ensure_file"
  ]
}
```

- `permission` 用官方的三档命名：`ask_risky` / `ask_always` / `always_allow`，内部映射成社区 CLI 的 `AskOnRisk` / `AlwaysAsk` / `FullAccess`。
- `auto_approve` 里的操作不弹窗、直接放行。默认只放行“建目录 / 建空文件”这类脚手架动作，写文件和跑命令仍会问。

## 键位

| 按键 | 行为 |
| --- | --- |
| `Enter` | 发送 |
| `Ctrl+J` | 输入换行 |
| `Esc` | 中断当前轮；审批态下等于拒绝 |
| `Tab` | 补全 `/` 命令 |
| `Ctrl+C` | 有任务时中断，有输入时清空，都没有时连按两次退出 |
| `Ctrl+D` | 退出 |
| `↑` / `↓` | 输入历史 |
| `PgUp` / `PgDn` | 翻看消息 |

## 命令

```
/new                开新会话
/resume             续等当前会话的未完成轮（不会重发消息）
/sessions           会话列表
/stop               中止当前轮
/status             会话 ID、阶段、工作区
/mode work|chat|goal
/perm ask_risky|ask_always|always_allow
/ws <目录>
/profile <名字>
/clear
/quit
```

## 工作方式

```
TUI 进程
 ├─ stdin/stdout：raw 模式，自绘全屏
 └─ 每轮起一个子进程
     ├─ stdin  → pty 从端  （CLI 用 process.stdin.isTTY 判断能否交互）
     ├─ stderr → pty 从端  （审批提示写在这里，必须也是 TTY）
     └─ stdout → 普通管道  （--json 回执，干净可解析）
```

三个关键点：

1. **必须用 pty**。CLI 的 `createHeadlessApproval` 用 `Boolean(stdin.isTTY && stderr.isTTY)` 判断能否交互，非 TTY 时**静默拒绝**所有审批，任务会直接失败。用管道接 stderr 是不行的。
2. **审批按文本匹配**。CLI 的提示格式固定为：
   ```
   [local runtime: AskOnRisk; approval-required] Approve Bash: command: ls; cwd /path? [y/N]
   ```
   匹配到 `[y/N]` 就把提示捞出来，用户按键后往 pty 写 `y\n` / `n\n`。
3. **stdout 单独走管道**。这样 `--json` 的回执不会和审批文本混在一起；回执里带 `conversationId` / `runId` / `reply.text`，用于续接下一轮。

## 测试

不需要登录，用 `tests/mock_doubao.py` 复刻 CLI 的审批与输出协议：

```bash
python3 tests/test_tui.py
```

覆盖四个分支：完整审批流、拒绝审批、Tab 补全、Esc 拒绝。测试用 `pexpect` 在真实 pty 里驱动界面，不截图。

```
== 主流程 ==
  ok   启动显示标题
  ok   中文输入回显
  ok   出现审批浮层
  ok   自动放行 ensure_dir 后进入 write_file
  ok   第二个审批为 Bash
  ok   最终回复渲染
  ok   会话 ID 保留
  ok   正常退出
  ok   无 Python traceback
== 拒绝分支 ==
  ok   拒绝有回显
  ok   拒绝后本轮失败结束
== 按键分支 ==
  ok   Tab 补全 /help 并执行
== Esc 分支 ==
  ok   Esc 拒绝审批
全部通过
```

## 已知限制

- **审批文案依赖 CLI 版本**。升级 `doubao-cli` 后，提示格式变化会让匹配失效；失效表现为审批条不出现、任务卡住，此时降级用 `always_allow` 或回退版本。
- **没有逐 token 流式**。社区 CLI 不提供 `stream-json`，界面只能显示阶段、耗时和本地回执里的阶段性文本。
- **协议是逆向的**。上游接口变化会导致整个后端失效，这不是本项目的可控范围。
- **只负责聊天与审批**。模型切换、MCP 注册、定时任务这些仍要用 `doubao headless ...` 命令。

## License

MIT
