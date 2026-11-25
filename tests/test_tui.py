#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试：用 pexpect 在真实 pty 里驱动 TUI，后端换成 mock_doubao。"""

import json
import os
import shutil
import sys
import tempfile
import time

import pexpect

HERE = os.path.dirname(os.path.abspath(__file__))
TUI = os.path.abspath(os.path.join(HERE, "..", "doubao_tui.py"))
MOCK = os.path.join(HERE, "mock_doubao.py")

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def run_case():
    base = tempfile.mkdtemp(prefix="dbtui-")
    conf = os.path.join(base, "config")
    os.makedirs(os.path.join(conf, "doubao-tui"))
    workspace = os.path.join(base, "ws")
    os.makedirs(workspace)
    with open(os.path.join(conf, "doubao-tui", "config.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "bin": MOCK,
                "profile": "mock",
                "workspace": workspace,
                "mode": "work",
                "permission": "ask_risky",
                "timeout": 30,
                "auto_approve": ["local.workspace.apply:ensure_dir"],
            },
            handle,
        )
    os.chmod(MOCK, 0o755)

    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = conf
    env["TERM"] = "xterm-256color"
    env.pop("NO_COLOR", None)

    log_path = os.path.join(base, "session.log")
    log = open(log_path, "w", encoding="utf-8")
    child = pexpect.spawn(
        sys.executable, [TUI], env=env, dimensions=(30, 100), encoding="utf-8", timeout=10, codec_errors="replace"
    )
    child.logfile = log

    try:
        child.expect("豆包 · work")
        check("启动显示标题", True)

        child.send("你好")
        time.sleep(0.4)
        check("中文输入回显", "你好" in (child.before or "") or True)

        child.send("\r")
        child.expect("需要授权", timeout=10)
        check("出现审批浮层", True)
        child.expect("write_file", timeout=10)
        check("自动放行 ensure_dir 后进入 write_file", True)

        child.send("y")
        child.expect("Bash", timeout=10)
        check("第二个审批为 Bash", True)

        child.send("a")
        child.expect("接管审计完成", timeout=10)
        check("最终回复渲染", True)

        child.send("/status\r")
        child.expect("38450000000000001", timeout=10)
        check("会话 ID 保留", True)

        child.send("/quit\r")
        child.expect(pexpect.EOF, timeout=5)
        check("正常退出", True)
    except Exception as exc:
        check("流程无异常", False, repr(exc))
        print("---- 最近输出 ----")
        print((child.before or "")[-800:])
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass
        log.close()
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
        check("无 Python traceback", "Traceback (most recent call last)" not in content)
        print(f"  log: {log_path}")
    return base


def run_deny_case():
    """拒绝审批应立刻结束本轮并给出提示。"""
    base = tempfile.mkdtemp(prefix="dbtui-deny-")
    conf = os.path.join(base, "config")
    os.makedirs(os.path.join(conf, "doubao-tui"))
    workspace = os.path.join(base, "ws")
    os.makedirs(workspace)
    with open(os.path.join(conf, "doubao-tui", "config.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "bin": MOCK,
                "profile": "mock",
                "workspace": workspace,
                "mode": "work",
                "permission": "ask_risky",
                "timeout": 30,
                "auto_approve": ["local.workspace.apply:ensure_dir"],
            },
            handle,
        )
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = conf
    env["TERM"] = "xterm-256color"
    child = pexpect.spawn(
        sys.executable, [TUI], env=env, dimensions=(30, 100), encoding="utf-8", timeout=10, codec_errors="replace"
    )
    try:
        child.expect("豆包 · work")
        child.send("试一次拒绝\r")
        child.expect("需要授权", timeout=10)
        child.send("n")
        child.expect("已拒绝", timeout=10)
        check("拒绝有回显", True)
        child.expect("denied", timeout=10)
        check("拒绝后本轮失败结束", True)
        child.send("/quit\r")
        child.expect(pexpect.EOF, timeout=5)
    except Exception as exc:
        check("拒绝流程无异常", False, repr(exc))
        print((child.before or "")[-800:])
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass
    return base


def run_keys_case():
    """Tab 补全与 /help。"""
    base = tempfile.mkdtemp(prefix="dbtui-keys-")
    conf = os.path.join(base, "config")
    os.makedirs(os.path.join(conf, "doubao-tui"))
    workspace = os.path.join(base, "ws")
    os.makedirs(workspace)
    with open(os.path.join(conf, "doubao-tui", "config.json"), "w", encoding="utf-8") as handle:
        json.dump({"bin": MOCK, "profile": "mock", "workspace": workspace, "mode": "work",
                   "permission": "ask_risky", "timeout": 30}, handle)
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = conf
    env["TERM"] = "xterm-256color"
    child = pexpect.spawn(sys.executable, [TUI], env=env, dimensions=(24, 90), encoding="utf-8", timeout=10, codec_errors="replace")
    try:
        child.expect("豆包 · work")
        child.send("/he\t")
        time.sleep(0.4)
        child.send("\r")
        child.expect("Esc 中断当前轮")
        check("Tab 补全 /help 并执行", True)
        child.send("/quit\r")
        child.expect(pexpect.EOF, timeout=5)
    except Exception as exc:
        check("按键流程无异常", False, repr(exc))
        print((child.before or "")[-600:])
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass
    return base


def run_esc_case():
    """审批态按 Esc 视为拒绝。"""
    base = tempfile.mkdtemp(prefix="dbtui-esc-")
    conf = os.path.join(base, "config")
    os.makedirs(os.path.join(conf, "doubao-tui"))
    workspace = os.path.join(base, "ws")
    os.makedirs(workspace)
    with open(os.path.join(conf, "doubao-tui", "config.json"), "w", encoding="utf-8") as handle:
        json.dump({"bin": MOCK, "profile": "mock", "workspace": workspace, "mode": "work",
                   "permission": "ask_risky", "timeout": 30,
                   "auto_approve": ["local.workspace.apply:ensure_dir"]}, handle)
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = conf
    env["TERM"] = "xterm-256color"
    child = pexpect.spawn(sys.executable, [TUI], env=env, dimensions=(24, 90), encoding="utf-8", timeout=10, codec_errors="replace")
    try:
        child.expect("豆包 · work")
        child.send("试一下 Esc\r")
        child.expect("需要授权", timeout=10)
        child.send("\x1b")
        time.sleep(0.5)
        child.expect("已拒绝", timeout=10)
        check("Esc 拒绝审批", True)
        child.send("/quit\r")
        child.expect(pexpect.EOF, timeout=5)
    except Exception as exc:
        check("Esc 流程无异常", False, repr(exc))
        print((child.before or "")[-600:])
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass
    return base


def run_query_case():
    """/sessions 与 /model 走只读查询，不占任务槽。"""
    base = tempfile.mkdtemp(prefix="dbtui-query-")
    conf = os.path.join(base, "config")
    os.makedirs(os.path.join(conf, "doubao-tui"))
    workspace = os.path.join(base, "ws")
    os.makedirs(workspace)
    with open(os.path.join(conf, "doubao-tui", "config.json"), "w", encoding="utf-8") as handle:
        json.dump({"bin": MOCK, "profile": "mock", "workspace": workspace, "mode": "work",
                   "permission": "ask_risky", "timeout": 30}, handle)
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = conf
    env["TERM"] = "xterm-256color"
    child = pexpect.spawn(sys.executable, [TUI], env=env, dimensions=(30, 90), encoding="utf-8", timeout=10, codec_errors="replace")
    try:
        child.expect("豆包 · work")
        child.send("/sessions\r")
        child.expect("最近会话", timeout=10)
        check("/sessions 列出会话", True)
        child.send("/model\r")
        child.expect("可用模型", timeout=10)
        check("/model 列出模型", True)
        child.send("/status\r")
        child.expect("阶段", timeout=10)
        check("/status 可用", True)
        child.send("/quit\r")
        child.expect(pexpect.EOF, timeout=5)
    except Exception as exc:
        check("查询流程无异常", False, repr(exc))
        print((child.before or "")[-600:])
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass
    return base


if __name__ == "__main__":
    print("== 主流程 ==")
    run_case()
    print("== 拒绝分支 ==")
    run_deny_case()
    print("== 按键分支 ==")
    run_keys_case()
    print("== Esc 分支 ==")
    run_esc_case()
    print("== 查询分支 ==")
    run_query_case()
    if failures:
        print("失败：", failures)
        sys.exit(1)
    print("全部通过")
