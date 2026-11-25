#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doubao-tui — 给社区版 doubao-cli 套一个终端对话界面。

后端仍然是 doubao-cli 的 headless 会话（账号登录、走免费额度），
本程序只负责把 create/send/wait 三段式命令收进一个常驻的聊天界面里，
并把 CLI 的终端审批提示转成界面内的确认条。

零第三方依赖，只需要 Python 3.8+ 和已经登录的 doubao 命令。
"""

from __future__ import annotations

import glob
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from typing import List, Optional

APP_NAME = "doubao-tui"
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "doubao-tui"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# ---------------------------------------------------------------- 终端基础

ESC = "\x1b"
ALT_ON = ESC + "[?1049h"
ALT_OFF = ESC + "[?1049l"
CUR_HIDE = ESC + "[?25l"
CUR_SHOW = ESC + "[?25h"

C_RESET = ESC + "[0m"
C_DIM = ESC + "[2m"
C_BOLD = ESC + "[1m"
C_REV = ESC + "[7m"
C_USER = ESC + "[36m"
C_SYS = ESC + "[90m"
C_ERR = ESC + "[31m"
C_OK = ESC + "[32m"
C_WARN = ESC + "[33m"
C_APPR = ESC + "[30;103m"
C_INPUT = ESC + "[36m"

ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def visible_len(text: str) -> int:
    return sum(char_width(ch) for ch in strip_ansi(text))


def char_width(ch: str) -> int:
    code = ord(ch)
    if code < 32 or 0x7F <= code < 0xA0:
        return 0
    if (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE10 <= code <= 0xFE19
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x1F300 <= code <= 0x1F9FF
        or 0x20000 <= code <= 0x3FFFD
    ):
        return 2
    return 1


def truncate(text: str, width: int) -> str:
    """按显示宽度截断，保留 ANSI 前缀（调用方自己保证颜色成对）。"""
    if width <= 0:
        return ""
    out = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def wrap_text(text: str, width: int) -> List[str]:
    """按显示宽度折行；保留原文里的显式换行。"""
    if width <= 1:
        width = 1
    lines: List[str] = []
    for raw in text.split("\n"):
        if raw == "":
            lines.append("")
            continue
        cur = ""
        cur_w = 0
        for ch in raw:
            w = char_width(ch)
            if cur_w + w > width:
                lines.append(cur)
                cur = ""
                cur_w = 0
                if ch == " ":
                    continue
            cur += ch
            cur_w += w
        lines.append(cur)
    return lines


# ---------------------------------------------------------------- 配置


DEFAULT_CONFIG = {
    "bin": "doubao",
    "profile": "termux",
    "workspace": "",
    "mode": "work",
    "permission": "ask_risky",
    "timeout": 600,
    "auto_approve": ["local.workspace.apply:ensure_dir", "local.workspace.apply:ensure_file"],
}

# 界面沿用官方的三档命名，底层转换成社区 CLI 的参数
PERM_TO_CLI = {
    "ask_risky": "AskOnRisk",
    "ask_always": "AlwaysAsk",
    "always_allow": "FullAccess",
    "AskOnRisk": "AskOnRisk",
    "AlwaysAsk": "AlwaysAsk",
    "FullAccess": "FullAccess",
}
PERM_LABELS = {
    "ask_risky": "风险操作时确认",
    "ask_always": "始终确认",
    "always_allow": "允许所有操作",
}
MODES = ("work", "chat", "goal")
COMMAND_HINTS = [
    "/help", "/new", "/resume", "/sessions", "/model", "/stop", "/status",
    "/mode", "/perm", "/ws", "/profile", "/clear", "/quit",
]


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            cfg.update(json.load(handle))
    except FileNotFoundError:
        pass
    except Exception as exc:  # 配置坏了不该让程序起不来
        sys.stderr.write(f"配置读取失败（{exc}），使用默认值\n")
    return cfg


def save_config(cfg: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as exc:
        sys.stderr.write(f"配置保存失败：{exc}\n")


# ---------------------------------------------------------------- 会话运行器


OPEN_RE = re.compile(r"\[y/N\]\s*$")
APPROVE_RE = re.compile(r"\[local runtime: (.*?); (.*?)\] (Approve|Deny) (.+?)\? \[y/N\]", re.S)

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class Turn:
    """一次提交的完整生命周期。"""

    argv: List[str]
    label: str
    proc: Optional[subprocess.Popen] = None
    master: int = -1
    conversation_id: str = ""
    run_id: str = ""
    started_at: float = 0.0
    phase: str = "准备"
    raw_err: str = ""
    stdout_buf: str = ""
    result: Optional[dict] = None
    error: str = ""
    exit_code: Optional[int] = None
    approval: Optional[dict] = None
    finished: bool = False


class Runner:
    """把 doubao-cli 的一次调用跑在 pty 里，收集输出与审批。"""

    def __init__(self, config: dict, events):
        self.config = config
        self.events = events  # queue.Queue，给 UI 线程用
        self.turn: Optional[Turn] = None
        self.lock = __import__("threading").Lock()

    # -- 构建命令

    def _base(self) -> List[str]:
        cfg = self.config
        argv = [cfg["bin"], "headless", "sessions"]
        return argv

    def _common_flags(self) -> List[str]:
        cfg = self.config
        flags = ["--runtime", "local"]
        workspace = cfg.get("workspace") or os.getcwd()
        flags += ["--workspace", workspace]
        flags += ["--permission", PERM_TO_CLI.get(str(cfg.get("permission") or "ask_risky"), "AskOnRisk")]
        if cfg.get("profile"):
            flags += ["--profile", str(cfg["profile"])]
        return flags

    def build_create(self, message: str) -> List[str]:
        cfg = self.config
        argv = self._base() + ["create", message, "--mode", cfg.get("mode") or "work"]
        argv += self._common_flags()
        argv += ["--wait", "--timeout", str(int(cfg.get("timeout") or 600)), "--json"]
        return argv

    def build_send(self, conversation_id: str, message: str) -> List[str]:
        cfg = self.config
        argv = self._base() + ["send", conversation_id, message]
        argv += self._common_flags()
        argv += ["--wait", "--timeout", str(int(cfg.get("timeout") or 600)), "--json"]
        return argv

    def build_wait(self, conversation_id: str, run_id: str = "") -> List[str]:
        argv = self._base() + ["wait", conversation_id]
        if run_id:
            argv += ["--run", run_id]
        if self.config.get("profile"):
            argv += ["--profile", str(self.config["profile"])]
        argv += ["--timeout", str(int(self.config.get("timeout") or 600)), "--json"]
        return argv

    # -- 生命周期

    def start(self, argv: List[str], label: str) -> bool:
        with self.lock:
            if self.turn and not self.turn.finished:
                return False
            self.turn = Turn(argv=argv, label=label)
        turn = self.turn
        try:
            master, slave = pty.openpty()
            # 让子进程看到与界面相同的窗口尺寸
            try:
                size = shutil.get_terminal_size()
                import fcntl
                import struct

                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size.lines, size.columns, 0, 0))
            except Exception:
                pass
            env = dict(os.environ)
            env.pop("NO_COLOR", None)
            env["TERM"] = "xterm-256color"
            proc = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=slave,
                close_fds=True,
                env=env,
                start_new_session=True,
            )
            os.close(slave)
            os.set_blocking(master, False)
            turn.proc = proc
            turn.master = master
            turn.started_at = time.time()
            turn.phase = "提交中"
        except FileNotFoundError:
            turn.error = f"找不到命令：{argv[0]}（先确认 doubao 已安装并在 PATH 里）"
            turn.finished = True
            self.events.put(("turn_end", turn))
            return True
        except Exception as exc:
            turn.error = f"启动失败：{exc}"
            turn.finished = True
            self.events.put(("turn_end", turn))
            return True

        __import__("threading").Thread(target=self._pump, args=(turn,), daemon=True).start()
        __import__("threading").Thread(target=self._watch_receipt, args=(turn,), daemon=True).start()
        self.events.put(("turn_start", turn))
        return True

    def _pump(self, turn: Turn) -> None:
        proc = turn.proc
        master = turn.master
        err_text = ""
        consumed = 0
        master_open = True
        stdout_open = True
        while True:
            watched = []
            if master_open:
                watched.append(master)
            if stdout_open:
                watched.append(proc.stdout.fileno())
            if not watched:
                break
            try:
                ready, _, _ = select.select(watched, [], [], 0.2)
            except (OSError, ValueError):
                break
            for fd in ready:
                if fd == master:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        chunk = b""
                    if chunk == b"":
                        master_open = False
                        continue
                    text = chunk.decode("utf-8", "replace")
                    err_text += text
                    turn.raw_err = (turn.raw_err + strip_ansi(text))[-4000:]
                    clean = strip_ansi(err_text)
                    if turn.approval is None and OPEN_RE.search(clean[consumed:]):
                        turn.approval = self._parse_approval(clean)
                        turn.phase = "待审批"
                        consumed = len(clean)
                        self.events.put(("approval", turn))
                else:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        chunk = b""
                    if chunk == b"":
                        stdout_open = False
                        continue
                    turn.stdout_buf += chunk.decode("utf-8", "replace")
            if proc.poll() is not None and not ready:
                # 进程已退出且当前没有可读事件：把残留读干净即可收尾
                self._drain(turn)
                break
        turn.exit_code = proc.poll()
        if not turn.finished:
            self._finalize(turn)
        if not turn.finished:
            self._finalize(turn)

    def _drain(self, turn: Turn) -> None:
        proc = turn.proc
        master = turn.master
        master_open = True
        stdout_open = True
        for _ in range(64):
            watched = []
            if master_open:
                watched.append(master)
            if stdout_open:
                watched.append(proc.stdout.fileno())
            if not watched:
                break
            try:
                ready, _, _ = select.select(watched, [], [], 0.05)
            except (OSError, ValueError):
                break
            if not ready:
                break
            for fd in ready:
                if fd == master:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        chunk = b""
                    if chunk == b"":
                        master_open = False
                    else:
                        turn.raw_err = (turn.raw_err + strip_ansi(chunk.decode("utf-8", "replace")))[-4000:]
                else:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        chunk = b""
                    if chunk == b"":
                        stdout_open = False
                    else:
                        turn.stdout_buf += chunk.decode("utf-8", "replace")

    def _finalize(self, turn: Turn) -> None:
        turn.finished = True
        try:
            os.close(turn.master)
        except Exception:
            pass
        payload = self._extract_json(turn.stdout_buf)
        if payload is not None:
            turn.result = payload
            turn.conversation_id = str(payload.get("conversationId") or turn.conversation_id or "")
            turn.run_id = str(payload.get("runId") or turn.run_id or "")
        text = self._reply_text(payload) if payload else ""
        if text:
            turn.phase = "完成"
        elif payload is not None and payload.get("status") in ("completed", "running", "waiting_input", "unknown"):
            turn.phase = "未拿到回复"
        elif turn.error:
            turn.phase = "失败"
        else:
            status = (payload or {}).get("status") or ""
            if status == "failed":
                turn.phase = "失败"
            elif status:
                turn.phase = status
            else:
                turn.phase = "结束"
        if payload and payload.get("error"):
            turn.error = str(payload["error"])
        if turn.exit_code not in (0, None) and not turn.error:
            tail = turn.raw_err.strip().splitlines()
            turn.error = tail[-1][:200] if tail else f"命令退出码 {turn.exit_code}"
        self.events.put(("turn_end", turn))

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """stdout 里取最外层的回执 JSON（不能误取 reply 之类的内层对象）。"""
        if not text.strip():
            return None
        text = strip_ansi(text).strip()
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
        except Exception:
            pass
        candidates: List[dict] = []
        for start, char in enumerate(text):
            if char != "{":
                continue
            depth = 0
            in_str = False
            escaped = False
            for pos in range(start, len(text)):
                ch = text[pos]
                if in_str:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(text[start : pos + 1])
                        except Exception:
                            break
                        if isinstance(obj, dict):
                            candidates.append(obj)
                        break
        for obj in candidates:
            if any(key in obj for key in ("conversationId", "runId", "platform", "status")):
                return obj
        return candidates[0] if candidates else None

    @staticmethod
    def _reply_text(payload: Optional[dict]) -> str:
        if not payload:
            return ""
        reply = payload.get("reply")
        if isinstance(reply, dict):
            text = reply.get("text") or ""
            if text:
                return text
        if isinstance(reply, str):
            return reply
        for key in ("answer", "text", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    # -- 审批

    def _parse_approval(self, clean: str) -> dict:
        tail = clean[-2000:]
        match = None
        for match in APPROVE_RE.finditer(tail):
            pass
        info = {"raw": clean[-2000:], "tool": "", "operation": "", "risk": "", "permission": "", "full_access": False}
        if match:
            info["permission"] = match.group(1)
            info["risk"] = match.group(2)
            head = match.group(4).strip()
            if ":" in head:
                info["tool"], info["operation"] = [part.strip() for part in head.split(":", 1)]
            else:
                info["tool"] = head
                info["operation"] = head
        else:
            idx = tail.rfind("[local runtime:")
            if idx != -1:
                info["raw"] = tail[idx:]
            if "Enable FullAccess" in tail:
                info["full_access"] = True
                info["tool"] = "FullAccess"
            elif "Approve " in tail:
                frag = tail[tail.rfind("Approve ") + len("Approve ") :]
                info["operation"] = frag.split(";")[0].strip()
        return info

    def approve(self, allowed: bool) -> None:
        turn = self.turn
        if not turn or turn.finished or turn.approval is None:
            return
        try:
            os.write(turn.master, b"y\n" if allowed else b"n\n")
        except OSError:
            pass
        turn.approval = None
        turn.phase = "执行中"
        self.events.put(("turn_update", turn))

    def reject_all_marker(self) -> None:
        """让后续轮次不再询问：写入一次 n 只影响当前，这里留作占位。"""

    def stop(self) -> None:
        turn = self.turn
        if turn and turn.proc and turn.proc.poll() is None:
            try:
                turn.proc.terminate()
            except Exception:
                pass
            turn.phase = "已中止"
            self.events.put(("turn_update", turn))

    # -- 回执增量

    def _watch_receipt(self, turn: Turn) -> None:
        """轮询本地回执文件，给界面提供“正在生成”的增量文本。"""
        conf_dir = os.environ.get("DOUBAO_CLI_CONFIG_DIR") or os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "doubao-cli"
        )
        while not turn.finished:
            if turn.conversation_id and turn.run_id:
                pattern = os.path.join(conf_dir, "turns", "*", f"{turn.conversation_id}-{turn.run_id}.json")
                for path in glob.glob(pattern)[:1]:
                    try:
                        with open(path, "r", encoding="utf-8") as handle:
                            data = json.load(handle)
                        answer = data.get("answer") or ""
                        if answer:
                            turn.result = dict(turn.result or {}, answer=answer)
                            self.events.put(("delta", turn))
                    except Exception:
                        pass
            time.sleep(0.7)

    # -- 会话切换

    def adopt(self, conversation_id: str, run_id: str = "") -> None:
        turn = self.turn
        if turn is None:
            turn = Turn(argv=[], label="")
            self.turn = turn
        turn.conversation_id = conversation_id
        turn.run_id = run_id
        turn.finished = True

    # -- 只读查询（不占 turn 槽，也不涉及审批）

    def query(self, argv: List[str], formatter, timeout: int = 20) -> None:
        def work() -> None:
            try:
                result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
                out = (result.stdout or "").strip()
                if out:
                    message = formatter(out)
                else:
                    message = (result.stderr or "").strip() or "没有输出"
            except Exception as exc:  # 查询失败不应打断会话
                message = f"查询失败：{exc}"
            self.events.put(("notice", message))

        threading.Thread(target=work, daemon=True).start()

    def list_sessions(self) -> None:
        argv = [
            self.config["bin"], "headless", "sessions", "list",
            "--limit", "10", "--json",
        ]
        if self.config.get("profile"):
            argv += ["--profile", str(self.config["profile"])]

        def formatter(out: str) -> str:
            data = json.loads(out)
            rows = data.get("sessions") or []
            if not rows:
                return "没有可用的会话"
            lines = ["最近会话："]
            for row in rows[:10]:
                sid = row.get("id") or row.get("conversationId") or ""
                title = (row.get("title") or "未命名")[:24]
                mark = "*" if sid == self.config.get("last_conversation") else " "
                lines.append(f" {mark}{sid}  {title}")
            lines.append("用 /resume <会话ID> 切换")
            return "\n".join(lines)

        self.query(argv, formatter)

    def list_models(self) -> None:
        argv = [self.config["bin"], "headless", "models", "--json"]
        if self.config.get("profile"):
            argv += ["--profile", str(self.config["profile"])]

        def formatter(out: str) -> str:
            data = json.loads(out)
            rows = data.get("models") or data.get("items") or []
            if not rows:
                return "没有取到模型列表"
            lines = ["可用模型："]
            for row in rows[:12]:
                if isinstance(row, str):
                    lines.append(f"  {row}")
                else:
                    name = row.get("name") or row.get("id") or ""
                    effort = row.get("reasoning") or row.get("effort") or ""
                    lines.append(f"  {name}{('  reasoning: ' + str(effort)) if effort else ''}")
            lines.append("用 /model <名称> 切换")
            return "\n".join(lines)

        self.query(argv, formatter)

    def select_model(self, name: str) -> None:
        argv = [self.config["bin"], "headless", "model", "select", name, "--json"]
        if self.config.get("profile"):
            argv += ["--profile", str(self.config["profile"])]

        def formatter(out: str) -> str:
            data = json.loads(out)
            model = data.get("model") or data.get("name") or name
            effort = data.get("reasoning") or ""
            return f"模型已切到 {model}" + (f"（reasoning {effort}）" if effort else "")

        self.query(argv, formatter)


# ---------------------------------------------------------------- UI


@dataclass
class Message:
    role: str  # user / assistant / system / tool / error
    text: str
    stamp: str = ""


@dataclass
class UIState:
    messages: List[Message] = field(default_factory=list)
    status: str = "空闲"
    phase: str = "空闲"
    conversation_id: str = ""
    run_id: str = ""
    started_at: float = 0.0
    approval: Optional[dict] = None
    live: str = ""
    input_text: str = ""
    cursor: int = 0
    history: List[str] = field(default_factory=list)
    history_idx: int = -1
    scroll: int = 0


class Terminal:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.saved = None

    def enter(self) -> None:
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        sys.stdout.write(ALT_ON + CUR_HIDE + ESC + "[2J")
        sys.stdout.flush()

    def leave(self) -> None:
        sys.stdout.write(CUR_SHOW + C_RESET + ALT_OFF)
        sys.stdout.flush()
        if self.saved:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def size(self):
        return shutil.get_terminal_size()

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()


class App:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.term = Terminal()
        self.state = UIState()
        self.runner = Runner(config, __import__("queue").Queue())
        self.queue = self.runner.events
        self.running = True
        self.need_render = True
        self.frame = 0
        self.message_scroll = 0
        self.last_activity = time.time()
        self.deferred = ""  # 被审批打断的输入
        self._pending = b""  # 跨 read 的未完成按键/UTF-8 字节
        self._pending_since = 0.0
        self._exit_armed_until = 0.0
        self._auto_backup: Optional[list] = None

    # -- 主循环

    def run(self) -> int:
        self.term.enter()
        signal.signal(signal.SIGWINCH, self._on_resize)
        self._boot_messages()
        try:
            self._loop()
        finally:
            self.term.leave()
        return 0

    def _on_resize(self, *_args) -> None:
        self.need_render = True

    def _boot_messages(self) -> None:
        ws = self.config.get("workspace") or os.getcwd()
        profile = self.config.get("profile") or "default"
        self.state.messages.append(
            Message(role="system", text=f"豆包 CLI · 终端会话\n工作区 {ws}\nprofile {profile} · 权限 {self.config.get('permission')}")
        )
        self.state.messages.append(Message(role="system", text="/help 查看按键与命令"))
        last = self.config.get("last_conversation")
        if last:
            self.state.messages.append(Message(role="system", text=f"上次会话 {last} · 用 /resume 续等，或 /sessions 看列表"))

    def _loop(self) -> None:
        last_tick = 0.0
        while self.running:
            timeout = 0.1
            try:
                ready, _, _ = select.select([self.term.fd], [], [], timeout)
            except InterruptedError:
                ready = []
            if ready:
                self._read_keys()
            self._drain_events()
            now = time.time()
            if self._pending == b"\x1b" and self._pending_since and now - self._pending_since > 0.3:
                # 单独按下 Esc（后面没有后续转义字节）
                self._pending = b""
                self._pending_since = 0.0
                self._interrupt()
            if now - last_tick > 0.25:
                last_tick = now
                if self.state.approval or (self.state.started_at and not self.state.approval):
                    self.need_render = True
                    self.frame += 1
            if self.need_render:
                self._render()
                self.need_render = False

    def _read_keys(self) -> None:
        try:
            data = os.read(self.term.fd, 1024)
        except OSError:
            return
        if not data:
            return
        self._handle_bytes(data)

    def _handle_bytes(self, data: bytes) -> None:
        buf = self._pending + data
        self._pending = b""
        i = 0
        n = len(buf)
        while i < n:
            byte = buf[i]
            if byte == 0x1B:
                parsed = self._parse_escape(buf[i:])
                if parsed is None:
                    self._pending = buf[i:]
                    self._pending_since = time.time()
                    return
                seq, used = parsed
                self._handle_escape(seq)
                i += used
                continue
            if byte in (0x0A, 0x0D):
                # 0x0A = Ctrl+J 换行；在 raw 模式下 Enter 通常是 0x0D
                if byte == 0x0D:
                    self._submit()
                else:
                    self._insert("\n")
                i += 1
                continue
            if byte in (0x7F, 0x08):
                self._backspace()
                i += 1
                continue
            if byte < 0x20:
                self._handle_control(byte)
                i += 1
                continue
            if byte >= 0x80:
                # 多字节字符；若在 read 边界被截断，先留着等下一批
                for length in (2, 3, 4):
                    chunk = buf[i : i + length]
                    if len(chunk) < length:
                        self._pending = buf[i:]
                        self._pending_since = time.time()
                        return
                    try:
                        text = chunk.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    self._insert(text)
                    i += length
                    break
                else:
                    i += 1
                continue
            self._insert(buf[i : i + 1].decode("utf-8", "ignore"))
            i += 1

    def _parse_escape(self, data: bytes):
        if len(data) < 2:
            return None
        second = data[1]
        if second == ord("["):
            j = 2
            while j < len(data) and not (0x40 <= data[j] <= 0x7E):
                j += 1
            if j >= len(data):
                return None
            seq = data[2 : j + 1].decode("ascii", "ignore")
            return seq, j + 1
        if second == ord("O"):
            if len(data) < 3:
                return None
            return data[2:3].decode("ascii", "ignore"), 3
        return "", 2

    def _handle_escape(self, seq: str) -> None:
        if self.state.approval is not None:
            return
        if seq == "":
            self._interrupt()
        elif seq in ("A",):
            self._history_up()
        elif seq in ("B",):
            self._history_down()
        elif seq in ("C",):
            self.state.cursor = min(len(self.state.input_text), self.state.cursor + 1)
            self.need_render = True
        elif seq in ("D",):
            self.state.cursor = max(0, self.state.cursor - 1)
            self.need_render = True
        elif seq == "3~":
            if self.state.cursor < len(self.state.input_text):
                self.state.input_text = (
                    self.state.input_text[: self.state.cursor] + self.state.input_text[self.state.cursor + 1 :]
                )
                self.need_render = True
        elif seq == "5~":
            self.message_scroll += 1
            self.need_render = True
        elif seq == "6~":
            self.message_scroll = max(0, self.message_scroll - 1)
            self.need_render = True
        elif seq == "H":
            self.state.cursor = 0
            self.need_render = True
        elif seq == "F":
            self.state.cursor = len(self.state.input_text)
            self.need_render = True

    def _interrupt(self) -> None:
        if self.state.approval is not None:
            self._answer(False)
            return
        if self.state.started_at:
            self.runner.stop()
            self._system("已中断当前轮（Esc）")
            self.need_render = True
        else:
            self._system("当前没有正在运行的任务")
            self.need_render = True

    def _complete(self) -> None:
        text = self.state.input_text
        if not text.startswith("/"):
            return
        fragment = text.split(" ", 1)[0]
        matches = [item for item in COMMAND_HINTS if item.startswith(fragment)]
        if len(matches) == 1:
            self.state.input_text = matches[0] + " "
            self.state.cursor = len(self.state.input_text)
        elif matches:
            self._system("候选：" + "  ".join(matches))
        self.need_render = True

    def _handle_control(self, code: int) -> None:
        if code == 0x03:  # Ctrl+C
            if self.state.approval is not None:
                self._answer(False)
            elif self.state.started_at:
                self.runner.stop()
                self._system("已中断当前轮（Ctrl+C）")
            elif self.state.input_text:
                self.state.input_text = ""
                self.state.cursor = 0
                self.need_render = True
            elif time.time() < self._exit_armed_until:
                self.running = False
            else:
                self._exit_armed_until = time.time() + 2.5
                self._system("再按一次 Ctrl+C 退出")
                self.need_render = True
        elif code == 0x04:  # Ctrl+D
            self.running = False
        elif code == 0x09:  # Tab
            self._complete()
        elif code == 0x0C:  # Ctrl+L
            self.need_render = True
        elif code == 0x15:  # Ctrl+U
            self.state.input_text = ""
            self.state.cursor = 0
            self.need_render = True

    def _insert(self, text: str) -> None:
        if self.state.approval is not None:
            return
        text = text.replace("\r", "")
        cur = self.state.cursor
        self.state.input_text = self.state.input_text[:cur] + text + self.state.input_text[cur:]
        self.state.cursor = cur + len(text)
        self.need_render = True

    def _backspace(self) -> None:
        if self.state.approval is not None:
            return
        cur = self.state.cursor
        if cur > 0:
            self.state.input_text = self.state.input_text[: cur - 1] + self.state.input_text[cur:]
            self.state.cursor = cur - 1
            self.need_render = True

    def _history_up(self) -> None:
        if not self.state.history:
            return
        if self.state.history_idx == -1:
            self.state.history_idx = len(self.state.history) - 1
        else:
            self.state.history_idx = max(0, self.state.history_idx - 1)
        self.state.input_text = self.state.history[self.state.history_idx]
        self.state.cursor = len(self.state.input_text)
        self.need_render = True

    def _history_down(self) -> None:
        if self.state.history_idx == -1:
            return
        self.state.history_idx += 1
        if self.state.history_idx >= len(self.state.history):
            self.state.history_idx = -1
            self.state.input_text = ""
            self.state.cursor = 0
        else:
            self.state.input_text = self.state.history[self.state.history_idx]
            self.state.cursor = len(self.state.input_text)
        self.need_render = True

    # -- 事件

    def _drain_events(self) -> None:
        while True:
            try:
                kind, turn = self.queue.get_nowait()
            except Exception:
                break
            if kind == "turn_start":
                self.state.started_at = turn.started_at
                self.state.phase = "提交中"
                self.state.conversation_id = turn.conversation_id or self.state.conversation_id
                self.state.run_id = turn.run_id
                self.need_render = True
            elif kind == "approval":
                self.state.approval = turn.approval
                self.state.phase = "待审批"
                self.need_render = True
                self._auto_approve_if_needed()
            elif kind == "delta":
                answer = (turn.result or {}).get("answer") or ""
                if answer:
                    self.state.live = answer
                    self.need_render = True
            elif kind == "turn_update":
                self.state.phase = turn.phase
                self.need_render = True
            elif kind == "notice":
                self._system(str(turn))
                self.need_render = True
            elif kind == "turn_end":
                self._finish_turn(turn)

    def _auto_approve_if_needed(self) -> None:
        approval = self.state.approval
        if not approval:
            return
        tool = approval.get("tool") or ""
        operation = approval.get("operation") or ""
        head = operation.split("(")[0].strip()
        key = f"{tool}:{head}"
        allowed = set(self.config.get("auto_approve") or [])
        if "*" in allowed or key in allowed:
            self.state.messages.append(Message(role="system", text=f"自动批准 · {tool} · {head}", stamp=self._now()))
            self.runner.approve(True)
            self.state.approval = None
            self.need_render = True

    def _finish_turn(self, turn: Turn) -> None:
        self.state.started_at = 0.0
        self.state.approval = None
        self.state.live = ""
        self.state.phase = turn.phase
        if turn.conversation_id:
            self.state.conversation_id = turn.conversation_id
            self.config["last_conversation"] = turn.conversation_id
            save_config(self.config)
        if turn.run_id:
            self.state.run_id = turn.run_id
        if self._auto_backup is not None:
            self.config["auto_approve"] = self._auto_backup
            self._auto_backup = None
        text = Runner._reply_text(turn.result)
        if text:
            self.state.messages.append(Message(role="assistant", text=text, stamp=self._now()))
        elif turn.error:
            message = turn.error
            lowered = message.lower()
            if "terminated" in lowered or "socket hang up" in lowered:
                message = "连接中断（terminated）。任务可能仍在服务端运行：用 /resume 续等，不要重发同一条消息"
            elif "timeout" in lowered:
                message = "等待超时，任务没有被取消：用 /resume 续等同一轮"
            self.state.messages.append(Message(role="error", text=message, stamp=self._now()))
        else:
            status = (turn.result or {}).get("status") or ""
            hint = ""
            if status in ("running", "unknown", "waiting_input"):
                hint = "任务已受理但未确认完成。输入 /resume 续等（复用同一会话，不会重发消息）"
            self.state.messages.append(
                Message(role="system", text=(f"本轮结束 · {turn.phase}" + (f" · {hint}" if hint else "")), stamp=self._now())
            )
        self.state.scroll = 0
        self.need_render = True

    # -- 提交

    def _submit(self) -> None:
        if self.state.approval is not None:
            return
        text = self.state.input_text.strip()
        self.state.input_text = ""
        self.state.cursor = 0
        if not text:
            return
        if text.startswith("/"):
            self._command(text)
            return
        if self.state.started_at:
            self._system("上一轮还没结束，先等它完成或按 Ctrl+C 中止")
            self.state.input_text = text
            self.state.cursor = len(text)
            return
        self.state.history.append(text)
        self.state.history_idx = -1
        self.state.messages.append(Message(role="user", text=text, stamp=self._now()))
        self.state.live = ""
        if self.state.conversation_id:
            argv = self.runner.build_send(self.state.conversation_id, text)
            label = "send"
        else:
            argv = self.runner.build_create(text)
            label = "create"
        if not self.runner.start(argv, label):
            self._system("已有任务在跑，先等它结束")
            return
        self.message_scroll = 0
        self.need_render = True

    def _resume(self) -> None:
        if not self.state.conversation_id:
            self._system("还没有会话 ID，先用 /sessions 看列表")
            return
        if self.state.started_at:
            self._system("当前已有任务在跑")
            return
        argv = self.runner.build_wait(self.state.conversation_id, self.state.run_id)
        if self.runner.start(argv, "wait"):
            self._system("续等同一轮任务")
            self.need_render = True

    def _switch_session(self, conversation_id: str) -> None:
        conversation_id = conversation_id.strip()
        if not re.fullmatch(r"\d{12,24}", conversation_id):
            self._system("会话 ID 应为 12–24 位数字")
            return
        if self.state.started_at:
            self._system("当前有任务在跑，先等它结束或按 Esc 中断")
            return
        self.state.conversation_id = conversation_id
        self.state.run_id = ""
        self.runner.adopt(conversation_id, "")
        self.config["last_conversation"] = conversation_id
        save_config(self.config)
        self._system(f"已切到会话 {conversation_id}，下一条消息会发到这里；有未完成轮就用 /resume 续等")
        self.need_render = True

    def _command(self, line: str) -> None:
        parts = line[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("q", "quit", "exit"):
            self.running = False
        elif cmd in ("h", "help", "?"):
            self._system(
                "按键：Enter 发送 · Ctrl+J 换行 · Esc 中断当前轮 · Tab 补全命令 · Ctrl+C 退出\n"
                "命令：/new 新会话 · /sessions 会话列表 · /resume [ID] 续等或切换 · /stop 中止\n"
                "　　　/model [名称] 查看或切换模型 · /mode work|chat|goal\n"
                "　　　/perm ask_risky|ask_always|always_allow · /ws <目录> · /profile <名字>\n"
                "　　　/status 状态 · /clear 清屏 · /quit 退出"
            )
        elif cmd == "new":
            self.state.conversation_id = ""
            self.state.run_id = ""
            self.runner.adopt("", "")
            self._system("下一个消息会开启新会话")
        elif cmd == "resume":
            if rest:
                self._switch_session(rest)
            else:
                self._resume()
        elif cmd == "stop":
            self.runner.stop()
            self._system("已请求中止")
        elif cmd == "status":
            self._system(
                f"会话 {self.state.conversation_id or '无'} · run {self.state.run_id or '无'} · "
                f"阶段 {self.state.phase} · 工作区 {self.config.get('workspace') or os.getcwd()}"
            )
        elif cmd == "mode":
            if rest in ("chat", "work", "goal"):
                self.config["mode"] = rest
                save_config(self.config)
                self._system(f"模式改为 {rest}（下一条消息生效）")
            else:
                self._system("用法 /mode work|chat|goal")
        elif cmd == "perm":
            if rest in PERM_LABELS:
                self.config["permission"] = rest
                save_config(self.config)
                self._system(f"权限模式：{rest}（{PERM_LABELS[rest]}）")
            else:
                self._system("用法 /perm ask_risky|ask_always|always_allow")
        elif cmd == "ws":
            if rest and os.path.isdir(os.path.expanduser(rest)):
                self.config["workspace"] = os.path.abspath(os.path.expanduser(rest))
                save_config(self.config)
                self._system(f"工作区改为 {self.config['workspace']}")
            else:
                self._system("用法 /ws /绝对/或/相对路径")
        elif cmd == "profile":
            self.config["profile"] = rest
            save_config(self.config)
            self._system(f"profile 改为 {rest or 'default'}")
        elif cmd == "clear":
            self.state.messages = []
            self.state.messages.append(Message(role="system", text="已清屏"))
        elif cmd == "sessions":
            self.runner.list_sessions()
            self._system("正在读取会话列表…")
        elif cmd == "model":
            if rest:
                self.runner.select_model(rest)
                self._system(f"正在切换模型到 {rest}…")
            else:
                self.runner.list_models()
                self._system("正在读取模型列表…")
        elif cmd == "r":
            self._resume()
        else:
            self._system(f"未知命令 /{cmd}，输入 /help")
        self.need_render = True

    def _answer(self, allowed: bool) -> None:
        if self.state.approval is None:
            return
        label = "已允许" if allowed else "已拒绝"
        self.state.messages.append(Message(role="system", text=f"{label} · {self.state.approval.get('tool', '')}", stamp=self._now()))
        self.state.approval = None
        self.runner.approve(allowed)
        self.need_render = True

    def _system(self, text: str) -> None:
        self.state.messages.append(Message(role="system", text=text, stamp=self._now()))

    @staticmethod
    def _now() -> str:
        return time.strftime("%H:%M")

    # -- 渲染

    def _render(self) -> None:
        size = self.term.size()
        width = max(20, size.columns)
        height = max(10, size.lines)

        approval_rows = 4 if self.state.approval else 0
        msg_rows = max(2, height - 2 - approval_rows - 4)

        lines: List[str] = []
        lines.extend(self._compose_header(width))
        lines.extend(self._compose_messages(width, msg_rows))
        if self.state.approval:
            lines.extend(self._compose_approval(width, approval_rows))
        lines.extend(self._compose_input(width))
        lines.append(C_DIM + self._status_line(width) + C_RESET)

        input_row = height - 2
        col = self._input_cursor_col(width)
        if self.state.approval:
            col = 1

        parts = []
        for index in range(height):
            content = lines[index] if index < len(lines) else ""
            parts.append(ESC + f"[{index + 1};1H" + content + ESC + "[K")
        frame = "".join(parts) + ESC + "[J"
        frame += ESC + f"[{input_row};{col}H"
        frame += CUR_HIDE if self.state.approval else CUR_SHOW
        self.term.write(frame)

    def _compose_header(self, width: int) -> List[str]:
        if self.state.started_at:
            spin = SPINNER[self.frame % len(SPINNER)]
            state = f"{spin} {self.state.phase} {int(time.time() - self.state.started_at)}s"
        else:
            state = f"● {self.state.phase}"
        title = f" 豆包 · {self.config.get('mode') or 'work'}"
        if self.state.conversation_id:
            title += f" · {self.state.conversation_id}"
        right = f" {state} "
        pad = width - visible_len(title) - visible_len(right)
        if pad < 0:
            title = truncate(title, max(1, width - visible_len(right)))
            pad = max(0, width - visible_len(title) - visible_len(right))
        head = C_BOLD + title + C_RESET + " " * pad + C_DIM + right + C_RESET
        return [head, C_DIM + "─" * width + C_RESET]

    def _compose_input(self, width: int) -> List[str]:
        if self.state.approval is not None:
            hint = " 审批中 · y 允许一次 · a 始终允许 · n 拒绝 "
            fill = " " * max(0, width - 2 - visible_len(hint))
            middle = C_WARN + "│" + truncate(hint, width - 2) + C_DIM + fill + "│" + C_RESET
            return [
                C_DIM + "╭" + "─" * max(0, width - 2) + "╮" + C_RESET,
                middle,
                C_DIM + "╰" + "─" * max(0, width - 2) + "╯" + C_RESET,
            ]
        avail = max(1, width - 5)
        text = self.state.input_text.replace("\n", " ↵ ")
        start = 0
        while visible_len(text[start : self.state.cursor]) > avail and start < self.state.cursor:
            start += 1
        shown = truncate(text[start:], avail)
        fill = " " * max(0, width - 5 - visible_len(shown))
        middle = C_DIM + "│" + C_RESET + C_INPUT + " > " + C_RESET + shown + fill + C_DIM + "│" + C_RESET
        return [
            C_DIM + "╭" + "─" * max(0, width - 2) + "╮" + C_RESET,
            middle,
            C_DIM + "╰" + "─" * max(0, width - 2) + "╯" + C_RESET,
        ]

    def _input_cursor_col(self, width: int) -> int:
        if self.state.approval:
            return 1
        text = self.state.input_text.replace("\n", " ↵ ")
        start = 0
        avail = max(1, width - 5)
        while visible_len(text[start : self.state.cursor]) > avail and start < self.state.cursor:
            start += 1
        return 5 + visible_len(text[start : self.state.cursor])

    def _status_line(self, width: int) -> str:
        if width < 56:
            left = " Esc 中断 · /help "
        else:
            left = " Esc 中断 · Tab 补全 · / 命令 · Ctrl+C 退出 "
        right = f" {self.state.phase} "
        if self.state.started_at:
            right = f" {self.config.get('permission')} · {int(time.time() - self.state.started_at)}s "
        else:
            right = f" {self.config.get('permission')} "
        pad = width - visible_len(left) - visible_len(right)
        if pad < 0:
            return truncate(left, width)
        return left + " " * pad + right

    def _compose_messages(self, width: int, rows: int) -> List[str]:
        blocks: List[List[str]] = []
        for message in self.state.messages:
            blocks.append(self._format_message(message, width))
        if self.state.live:
            live_lines = wrap_text(self.state.live, width - 2)
            prefix = [C_SYS + " 生成中 " + C_RESET + C_DIM + "▍" + C_RESET]
            blocks.append([prefix[0]] + [" " + line for line in live_lines])
        if self.state.started_at and not self.state.live and not self.state.approval:
            spin = SPINNER[self.frame % len(SPINNER)]
            blocks.append([C_DIM + f" {spin} 等待回复…" + C_RESET])

        flat: List[str] = []
        for block in blocks:
            flat.extend(block)
        # 滚动：0 表示贴底
        if self.message_scroll:
            keep = max(0, len(flat) - rows - self.message_scroll)
            flat = flat[keep : keep + rows]
        else:
            flat = flat[-rows:]
        # 顶部对齐
        while len(flat) < rows:
            flat.insert(0, "")
        return flat[:rows]

    def _format_message(self, message: Message, width: int) -> List[str]:
        stamp = f" · {message.stamp}" if message.stamp else ""
        if message.role == "user":
            out = [C_USER + f"› 你{stamp}" + C_RESET]
            colour = ""
        elif message.role == "assistant":
            out = [C_BOLD + f"豆包{stamp}" + C_RESET]
            colour = ""
        elif message.role == "error":
            out = [C_ERR + f"! {message.text}{stamp}" + C_RESET, ""]
            return out
        else:
            text = wrap_text(message.text, max(1, width - 3))
            out = []
            for index, line in enumerate(text):
                prefix = "· " if index == 0 else "  "
                out.append(C_SYS + prefix + line + C_RESET)
            out.append("")
            return out
        for line in wrap_text(message.text, max(1, width - 2)):
            out.append((colour + "  " + line + C_RESET) if colour else "  " + line)
        out.append("")
        return out

    def _compose_approval(self, width: int, rows: int) -> List[str]:
        approval = self.state.approval or {}
        tool = approval.get("tool") or "操作"
        operation = approval.get("operation") or approval.get("raw", "")
        risk = approval.get("risk") or ""
        if approval.get("full_access"):
            title = " ⚠ 本轮请求完全访问权限 "
            operation = "允许后命令可读取、修改或删除文件，并继承环境变量"
            keys = " y 允许   n 拒绝 "
        else:
            title = f" ⚠ 需要授权 · {tool} "
            keys = " y 允许一次   a 始终允许   n 拒绝 "
            if risk and "approval-required" not in risk:
                keys += f"  ({risk})"
        lines = [C_APPR + truncate(title + " " * max(0, width - visible_len(title)), width) + C_RESET]
        wrapped = wrap_text(operation, max(1, width - 3))
        detail = wrapped[: max(1, rows - 2)]
        for line in detail:
            lines.append(C_WARN + " " + line + C_RESET)
        if len(wrapped) > len(detail):
            lines[-1] = C_WARN + " …" + C_RESET
        lines.append(C_DIM + truncate(keys, width) + C_RESET)
        while len(lines) < rows:
            lines.append("")
        return lines[:rows]


# ---------------------------------------------------------------- 入口


def find_bin(name: str) -> Optional[str]:
    path = shutil.which(name)
    return path


def run_doctor(config: dict) -> int:
    """不进入界面，逐项检查运行前提。"""

    ok = True

    def line(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        print(("  ok   " if passed else "  FAIL ") + name + (f"  {detail}" if detail else ""))
        if not passed:
            ok = False

    print("doubao-tui 自检")
    print(f"  python {sys.version.split()[0]}")
    print(f"  workspace {config.get('workspace') or os.getcwd()}")
    print()

    workspace = config.get("workspace") or os.getcwd()
    line("工作区存在且可读", os.path.isdir(workspace) and os.access(workspace, os.R_OK), workspace)

    base = [config["bin"], "headless"]
    profile = config.get("profile")
    suffix = ["--profile", str(profile)] if profile else []

    def call(args: List[str], timeout: int = 25):
        try:
            return subprocess.run(base + args + suffix, capture_output=True, text=True, timeout=timeout)
        except Exception as exc:
            print(f"    （{args[0]} 调用失败：{exc}）")
            return None

    result = call(["status", "--json"])
    if result is not None:
        try:
            data = json.loads((result.stdout or "").strip() or "{}")
            line("账号已登录", bool(data.get("loggedIn")), f"accountId {data.get('accountId') or '-'}")
        except Exception:
            line("登录状态解析", False, ((result.stdout or "") + (result.stderr or ""))[:80])

    result = call(["runtimes", "--json"])
    if result is not None:
        try:
            data = json.loads((result.stdout or "").strip() or "{}")
            rows = data.get("runtimes") or data.get("items") or []
            local = next(
                (row for row in rows if (row.get("name") or row.get("id") or row.get("runtime")) == "local"),
                None,
            )
            if local:
                line(
                    "本地执行通道",
                    bool(local.get("available", True)),
                    f"{local.get('engine') or ''} / {local.get('hostPlatform') or ''}".strip(" /"),
                )
            else:
                line("本地执行通道", False, "runtimes 里没有 local")
        except Exception:
            line("执行通道解析", False, ((result.stdout or "") + (result.stderr or ""))[:80])

    print()
    print("结论：" + ("全部通过，可以启动 doubao-tui" if ok else "有项目未通过，先修好再启动界面"))
    return 0 if ok else 1


def main(argv: List[str]) -> int:
    config = load_config()
    if not config.get("workspace"):
        config["workspace"] = os.getcwd()
    bin_path = find_bin(str(config.get("bin") or "doubao"))
    if not bin_path:
        sys.stderr.write(
            f"找不到 {config.get('bin')}。先安装社区版 CLI 并登录：\n"
            "  npm i -g doubao-cli@0.23.3\n"
            "  doubao headless login --profile termux\n"
        )
        return 2
    config["bin"] = bin_path

    if "--doctor" in argv:
        return run_doctor(config)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.stderr.write("doubao-tui 必须在真实终端里运行\n")
        return 2

    app = App(config)
    # 审批热键由主循环统一处理：y / n / a
    original = app._handle_bytes

    def handle(data: bytes) -> None:
        if app.state.approval is not None:
            for byte in data:
                if byte in (ord("y"), ord("Y")):
                    app._answer(True)
                    return
                if byte in (ord("n"), ord("N")):
                    app._answer(False)
                    return
                if byte in (ord("a"), ord("A")):
                    if app._auto_backup is None:
                        app._auto_backup = list(app.config.get("auto_approve") or [])
                    app.config["auto_approve"] = ["*"]
                    app._system("本轮后续请求全部放行（本轮结束后恢复）")
                    app._answer(True)
                    return
                if byte == 0x03:
                    app.runner.stop()
                    app._system("已中止当前任务")
                    return
                if byte == 0x1B:
                    # 审批态下按 Esc 视为拒绝并中止本轮
                    app._answer(False)
                    app.runner.stop()
                    return
            return
        original(data)

    app._handle_bytes = handle  # type: ignore[method-assign]
    return app.run()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
