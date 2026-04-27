import time
import json
import subprocess
import threading
import os
import sys
import signal
import argparse
import atexit
from datetime import datetime

try:
    import psutil
except ImportError:
    print("缺少 psutil 库。请先运行 ./setup.sh 安装依赖环境。")
    sys.exit(1)

try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:
    print("缺少 tkinter 库。请在系统上安装 python3-tk (如: sudo apt-get install python3-tk)。")
    sys.exit(1)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
MONITOR_LOCK_FILE = "/tmp/memory-monitor-daemon.pid"
_shutdown_flag = False

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"未找到配置文件 {CONFIG_FILE}，请检查！")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
    if "command" in config.get("critical", {}):
        old_cmd = config["critical"].pop("command")
        config["critical"]["commands"] = [old_cmd] if old_cmd else []
    return config

def _acquire_monitor_lock():
    if os.path.exists(MONITOR_LOCK_FILE):
        try:
            with open(MONITOR_LOCK_FILE, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            return False
        except (ValueError, OSError):
            pass
    with open(MONITOR_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_monitor_lock)
    return True

def _release_monitor_lock():
    try:
        if os.path.exists(MONITOR_LOCK_FILE):
            os.remove(MONITOR_LOCK_FILE)
    except OSError:
        pass

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def show_popup(title, message):
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.focus_force()
        messagebox.showwarning(title, message, parent=root)
        root.destroy()
    except Exception as e:
        log(f"弹窗显示失败: {e} (如果在纯终端无 GUI 环境下运行，将无法显示弹窗)")

def run_command(command, sudo_password):
    try:
        log(f"正在执行紧急配置指令: {command}")
        run_cmd = command
        pwd_input = None
        if "sudo " in run_cmd and sudo_password:
            run_cmd = run_cmd.replace("sudo ", "sudo -S ")
            pwd_input = sudo_password + "\n"
        if pwd_input:
            result = subprocess.run(run_cmd, shell=True, input=pwd_input, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        else:
            result = subprocess.run(run_cmd, shell=True, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if result.returncode == 0:
            log("命令执行成功。")
        else:
            err = (result.stderr or "").strip() or f"退出码 {result.returncode}"
            log(f"命令执行失败: {err}")
    except subprocess.TimeoutExpired:
        log("命令执行超时 (15s)，跳过。")
    except Exception as e:
        log(f"执行命令时发生未知错误: {e}")

def signal_handler(signum, frame):
    global _shutdown_flag
    log(f"收到信号 {signum}，正在安全退出...")
    _shutdown_flag = True

def main():
    if not _acquire_monitor_lock():
        print("Ubuntu 内存监控服务已在运行中！(锁文件: {})".format(MONITOR_LOCK_FILE))
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Ubuntu 内存监控服务")
    parser.add_argument("--sudo-pwd", type=str, default=None, help="Sudo 密码 (仅内存暂存)")
    args = parser.parse_args()
    sudo_password = args.sudo_pwd

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log("Ubuntu 内存监控服务已启动...")
    log(f"读取配置文件: {CONFIG_FILE}")

    warned = False
    action_taken = False
    current_cmd_index = 0
    cmd_cooldown_until = 0
    exhausted_logged = False

    while not _shutdown_flag:
        try:
            config = load_config()
            check_interval = config.get("check_interval_seconds", 3)
            warn_cfg = config.get("warning", {})
            crit_cfg = config.get("critical", {})

            mem = psutil.virtual_memory()
            percent = mem.percent

            crit_threshold = crit_cfg.get("threshold_percent", 90.0)
            warn_threshold = warn_cfg.get("threshold_percent", 80.0)
            cmds = crit_cfg.get("commands", [])

            if crit_cfg.get("enabled", True) and percent >= crit_threshold:
                if not action_taken:
                    cmd_display = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(cmds)) if cmds else "(无)"
                    log(f"[危急] 当前内存使用率达到 {percent}%！(阈值: {crit_threshold}%)")
                    threading.Thread(
                        target=show_popup,
                        args=("危急内存警告", f"内存使用率已达 {percent}%！\n\n即将执行紧急清理指令：\n{cmd_display}")
                    ).start()
                    action_taken = True
                    warned = True

                if current_cmd_index < len(cmds) and time.time() >= cmd_cooldown_until:
                    cmd = cmds[current_cmd_index]
                    log(f"[阶梯执行 {current_cmd_index + 1}/{len(cmds)}]: {cmd}")
                    run_command(cmd, sudo_password)
                    current_cmd_index += 1
                    cmd_cooldown_until = time.time() + 15
                    log(f"等待 15 秒后检测内存状态，再决定是否执行下一条指令...")
                elif current_cmd_index >= len(cmds) and cmds:
                    if not exhausted_logged:
                        log("⚠️ 所有干预指令已执行完毕，但内存依然处于危急状态！")
                        exhausted_logged = True

            elif warn_cfg.get("enabled", True) and percent >= warn_threshold:
                if not warned:
                    log(f"[警告] 当前内存使用率达到 {percent}%！(阈值: {warn_threshold}%)")
                    threading.Thread(
                        target=show_popup,
                        args=("内存不足警告", f"当前内存使用率偏高：{percent}%\n请尽快保存您的工作或关闭部分应用！")
                    ).start()
                    warned = True

            else:
                if percent < warn_threshold - 5:
                    warned = False
                if percent < crit_threshold - 5:
                    if action_taken:
                        log("==== 内存恢复安全水平，自动重置干预状态 ====")
                    action_taken = False
                    current_cmd_index = 0
                    exhausted_logged = False

            time.sleep(check_interval)

        except Exception as e:
            log(f"监控主循环发生异常: {e}")
            time.sleep(5)

    log("监控服务已退出。")

if __name__ == "__main__":
    main()
