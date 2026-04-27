import tkinter as tk
from tkinter import ttk, messagebox
import psutil
import json
import os
import sys
import threading
import subprocess
import time
import signal
import logging
import atexit

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "monitor.log")
LOCK_FILE = "/tmp/memory-monitor.pid"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def _acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            return False
        except (ValueError, OSError):
            pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_lock)
    return True

def _release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except OSError:
        pass

class MemoryMonitorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Ubuntu 内存监控器 (阶梯干预版)")
        self.root.geometry("620x720")
        self.root.resizable(False, False)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 状态变量
        self.is_monitoring = False
        self.warned = False
        
        # 阶梯执行控制变量
        self.current_cmd_index = 0
        self.is_cmd_running = False
        self.cmd_cooldown_until = 0
        self._shutdown_flag = False

        self.load_config()
        self.build_ui()
        self.update_loop()
        
        # 初始化加载一次进程列表
        self._async_refresh_processes()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
                if "command" in self.config.get("critical", {}):
                    old_cmd = self.config["critical"].pop("command")
                    self.config["critical"]["commands"] = [old_cmd] if old_cmd else []
                    with open(CONFIG_FILE, "w", encoding="utf-8") as fw:
                        json.dump(self.config, fw, indent=4, ensure_ascii=False)
                    logging.info("已将旧格式 'command' 迁移为 'commands' 数组并写盘。")
            except Exception:
                self.default_config()
        else:
            self.default_config()

    def default_config(self):
        self.config = {
            "check_interval_seconds": 3,
            "warning": {"enabled": True, "threshold_percent": 80.0},
            "critical": {"enabled": True, "threshold_percent": 90.0, "commands": ["sudo systemctl stop elasticsearch", "killall chrome"]}
        }

    def save_config(self):
        try:
            warn_val = float(self.warn_var.get())
            crit_val = float(self.crit_var.get())
            
            if warn_val >= crit_val:
                messagebox.showerror("错误", "警告阈值必须小于危急阈值！")
                return

            self.config["warning"]["threshold_percent"] = warn_val
            self.config["critical"]["threshold_percent"] = crit_val
            
            raw_cmds = self.cmd_text.get("1.0", tk.END).strip().split('\n')
            cmds = [cmd.strip() for cmd in raw_cmds if cmd.strip()]
            self.config["critical"]["commands"] = cmds
            
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            
            self.log_status("配置已保存。")
            messagebox.showinfo("成功", "配置已保存！")
        except ValueError:
            messagebox.showerror("错误", "阈值必须是有效的数字！")

    def build_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        
        # 创建标签页容器
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # ================= 标签页 1：监控与干预 =================
        self.tab_monitor = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_monitor, text="🛡️ 监控与干预")

        # --- 实时内存展示区 ---
        frame_top = ttk.LabelFrame(self.tab_monitor, text=" 实时内存状态 ", padding=10)
        frame_top.pack(fill="x", padx=10, pady=5)

        self.mem_lbl = ttk.Label(frame_top, text="当前内存使用率: --%", font=("Arial", 16, "bold"))
        self.mem_lbl.pack(pady=5)

        self.progress = ttk.Progressbar(frame_top, length=540, mode='determinate')
        self.progress.pack(pady=5)

        # --- 配置区 ---
        frame_cfg = ttk.LabelFrame(self.tab_monitor, text=" 报警与阶梯干预配置 ", padding=10)
        frame_cfg.pack(fill="x", padx=10, pady=5)

        frame_thresh = ttk.Frame(frame_cfg)
        frame_thresh.pack(fill="x", pady=5)
        ttk.Label(frame_thresh, text="警告阈值 (%):").pack(side="left")
        self.warn_var = tk.StringVar(value=str(self.config["warning"]["threshold_percent"]))
        ttk.Entry(frame_thresh, textvariable=self.warn_var, width=8).pack(side="left", padx=5)

        ttk.Label(frame_thresh, text="危急阈值 (%):").pack(side="left", padx=(20, 0))
        self.crit_var = tk.StringVar(value=str(self.config["critical"]["threshold_percent"]))
        ttk.Entry(frame_thresh, textvariable=self.crit_var, width=8).pack(side="left", padx=5)

        ttk.Label(frame_cfg, text="干预指令 (每行一条，从上到下按优先级执行):").pack(anchor="w", pady=(10, 2))
        self.cmd_text = tk.Text(frame_cfg, height=4, width=68, font=("Consolas", 10))
        self.cmd_text.pack(fill="x", pady=2)
        cmds_str = "\n".join(self.config["critical"].get("commands", []))
        self.cmd_text.insert("1.0", cmds_str)

        frame_pwd = ttk.Frame(frame_cfg)
        frame_pwd.pack(fill="x", pady=5)
        ttk.Label(frame_pwd, text="Sudo 密码 (提权用, 仅内存暂存不落盘):", foreground="#e67e22").pack(side="left")
        self.sudo_pwd_var = tk.StringVar()
        ttk.Entry(frame_pwd, textvariable=self.sudo_pwd_var, width=15, show="*").pack(side="left", padx=10)

        ttk.Button(frame_cfg, text="保存配置", command=self.save_config).pack(pady=5)

        # --- 执行日志区 ---
        frame_log = ttk.LabelFrame(self.tab_monitor, text=" 阶梯干预执行日志 ", padding=10)
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_listbox = tk.Listbox(frame_log, height=8, font=("Consolas", 9), bg="#1e1e1e", fg="#00ff00")
        self.log_listbox.pack(side="left", fill="both", expand=True)
        
        scrollbar = ttk.Scrollbar(frame_log, orient="vertical", command=self.log_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_listbox.config(yscrollcommand=scrollbar.set)

        self.log_status("系统初始化完毕，等待启动监控。")

        # --- 控制区 ---
        frame_ctrl = ttk.Frame(self.tab_monitor, padding=10)
        frame_ctrl.pack(fill="x")

        self.status_lbl = ttk.Label(frame_ctrl, text="● 监控已暂停", foreground="gray", font=("Arial", 12, "bold"))
        self.status_lbl.pack(side="left", pady=5)

        self.toggle_btn = ttk.Button(frame_ctrl, text="▶ 启动监控", command=self.toggle_monitor)
        self.toggle_btn.pack(side="right", padx=5)

        self.reset_btn = ttk.Button(frame_ctrl, text="↺ 重置状态", command=self.manual_reset)
        self.reset_btn.pack(side="right", padx=5)


        # ================= 标签页 2：进程管理 =================
        self.tab_process = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_process, text="⚙️ 进程管理")

        ttk.Label(self.tab_process, text="⚠️ 提示：列表不会自动刷新，防止在您选中进程时发生跳动导致误杀。", foreground="gray").pack(anchor="w", padx=10, pady=5)

        # --- 进程树状图 ---
        frame_tree = ttk.Frame(self.tab_process)
        frame_tree.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("pid", "name", "mem_percent", "mem_mb")
        self.tree = ttk.Treeview(frame_tree, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("pid", text="PID")
        self.tree.heading("name", text="进程名称")
        self.tree.heading("mem_percent", text="内存占比")
        self.tree.heading("mem_mb", text="实际内存")

        self.tree.column("pid", width=80, anchor="center")
        self.tree.column("name", width=220, anchor="w")
        self.tree.column("mem_percent", width=100, anchor="center")
        self.tree.column("mem_mb", width=120, anchor="center")

        tree_scroll = ttk.Scrollbar(frame_tree, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)

        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        self.tree_tooltip = None
        self.tree.bind("<Motion>", self._on_tree_motion, add="+")
        self.tree.bind("<Leave>", self._on_tree_leave, add="+")

        # --- 进程控制区 ---
        frame_proc_ctrl = ttk.Frame(self.tab_process)
        frame_proc_ctrl.pack(fill="x", padx=10, pady=10)

        ttk.Button(frame_proc_ctrl, text="🔄 刷新列表 (按内存排序)", command=self.refresh_processes).pack(side="left", padx=5)
        ttk.Button(frame_proc_ctrl, text="❌ 结束选中进程", command=self.kill_selected_process).pack(side="right", padx=5)


    # ================= 功能逻辑 =================

    def refresh_processes(self):
        threading.Thread(target=self._do_refresh_processes, daemon=True).start()

    def _async_refresh_processes(self):
        self.refresh_processes()

    def _do_refresh_processes(self):
        """在后台线程中收集进程数据，然后回到主线程更新 UI"""
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'memory_percent', 'memory_info',
                                       'cmdline', 'username']):
            try:
                info = p.info
                if info['memory_percent'] is not None and info['memory_percent'] > 0.05:
                    procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        procs.sort(key=lambda x: x['memory_percent'] or 0, reverse=True)

        WRAPPER_NAMES = {'java', 'python', 'python3', 'node', 'ruby', 'php'}

        def _build_display_name(p):
            name = p['name'] or ''
            cmdline = p['cmdline'] or []
            username = p['username'] or ''
            if name.lower() in WRAPPER_NAMES and len(cmdline) > 1:
                visible = cmdline[1]
                if len(visible) > 60:
                    visible = visible[-57:] + '...'
                return f"{name} [{visible}]"
            if username and username != os.environ.get('USER', ''):
                return f"{name} ({username})"
            return name

        def _update_tree():
            for row in self.tree.get_children():
                self.tree.delete(row)
            for p in procs[:100]:
                mem_mb = p['memory_info'].rss / (1024 * 1024) if p['memory_info'] else 0
                self.tree.insert("", "end", values=(
                    p['pid'],
                    _build_display_name(p),
                    f"{p['memory_percent']:.1f}%",
                    f"{mem_mb:.1f} MB"
                ))
        self.root.after(0, _update_tree)

    def _on_tree_motion(self, event):
        row_id = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row_id or col != '#2':
            self._destroy_tooltip()
            return
        item = self.tree.item(row_id)
        full_text = item['values'][1] if item['values'] else ''
        if not full_text:
            self._destroy_tooltip()
            return
        if self.tree_tooltip and self.tree_tooltip._tooltip_text == full_text:
            return
        self._destroy_tooltip()
        tw = tk.Toplevel(self.tree)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        label = tk.Label(tw, text=full_text, font=("Consolas", 10),
                         bg="#ffffcc", fg="#333333", relief="solid", borderwidth=1,
                         padx=6, pady=2)
        label.pack()
        tw._tooltip_text = full_text
        x = event.x_root + 16
        y = event.y_root + 10
        tw.geometry(f"+{x}+{y}")
        self.tree_tooltip = tw

    def _on_tree_leave(self, event):
        self._destroy_tooltip()

    def _destroy_tooltip(self):
        if self.tree_tooltip:
            try:
                self.tree_tooltip.destroy()
            except tk.TclError:
                pass
            self.tree_tooltip = None

    def kill_selected_process(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先在列表中选择一个要结束的进程！")
            return

        item = self.tree.item(selected[0])
        try:
            pid = int(item['values'][0])
        except (ValueError, TypeError):
            messagebox.showerror("错误", "无法获取有效的 PID。")
            return
        name = item['values'][1]

        if not messagebox.askyesno("危险操作确认", f"确定要强制结束进程\n【{name}】 (PID: {pid}) 吗？\n未保存的数据可能会丢失！"):
            return

        def _kill_via_sudo():
            pwd = self.sudo_pwd_var.get()
            if not pwd:
                messagebox.showerror("权限拒绝", f"结束进程 {name} (PID:{pid}) 需要管理员权限。\n请在【监控与干预】标签页中输入 Sudo 密码后再试！")
                return False
            try:
                result = subprocess.run(
                    ["sudo", "-S", "kill", "-9", str(pid)],
                    input=pwd + "\n", text=True,
                    capture_output=True, timeout=10
                )
                if result.returncode == 0:
                    return True
                else:
                    err = (result.stderr or "").strip()
                    messagebox.showerror("提权失败", f"Sudo 密码错误或权限不足。\n{err}")
                    return False
            except subprocess.TimeoutExpired:
                messagebox.showerror("超时", "Sudo 命令执行超时，请重试。")
                return False
            except Exception as e:
                messagebox.showerror("错误", f"提权命令执行失败: {e}")
                return False

        try:
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=3)
            except psutil.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)
            self.log_status(f"手动结束进程成功: {name} (PID: {pid})")
            messagebox.showinfo("成功", f"进程 {name} 已结束。")
            self.refresh_processes()

        except psutil.NoSuchProcess:
            self.log_status(f"进程 {name} (PID: {pid}) 已不存在，刷新列表。")
            messagebox.showinfo("提示", f"进程 {name} (PID: {pid}) 已退出。")
            self.refresh_processes()

        except psutil.AccessDenied:
            if _kill_via_sudo():
                self.log_status(f"通过 Sudo 结束进程成功: {name} (PID: {pid})")
                messagebox.showinfo("成功", f"已通过管理员权限强杀进程 {name}。")
                self.refresh_processes()

        except Exception as e:
            messagebox.showerror("错误", f"无法结束进程: {e}")

    def log_status(self, msg):
        time_str = time.strftime("%H:%M:%S")
        self.log_listbox.insert(tk.END, f"[{time_str}] {msg}")
        self.log_listbox.yview(tk.END)
        logging.info(msg)

    def manual_reset(self):
        self.current_cmd_index = 0
        self.is_cmd_running = False
        self.cmd_cooldown_until = 0
        self.warned = False
        self._exhausted_logged = False
        self.log_status("==== 状态已手动重置 ====")

    def auto_reset(self):
        if self.current_cmd_index > 0:
            self.current_cmd_index = 0
            self.is_cmd_running = False
            self.cmd_cooldown_until = 0
            self.log_status("==== 内存恢复安全水平，自动重置 ====")

    def toggle_monitor(self):
        self.is_monitoring = not self.is_monitoring
        if self.is_monitoring:
            self.status_lbl.config(text="● 监控运行中", foreground="green")
            self.toggle_btn.config(text="■ 停止监控")
            self.load_config()
            self.cmd_cooldown_until = 0
            self.log_status("监控已启动。")
        else:
            self.status_lbl.config(text="● 监控已暂停", foreground="gray")
            self.toggle_btn.config(text="▶ 启动监控")
            self.log_status("监控已暂停。")

    def show_popup(self, title, msg, is_critical=False):
        top = tk.Toplevel(self.root)
        top.transient(self.root)
        top.attributes("-topmost", True)
        top.overrideredirect(True)
        top.configure(bg="#000000")
        w, h = 520, 320
        sw = top.winfo_screenwidth()
        sh = top.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        top.geometry(f"{w}x{h}+{x}+{y}")

        if is_critical:
            bg_color = "#c0392b"
            accent = "#e74c3c"
            icon = "\u26a0\ufe0f"
            header_text = "\u26a0\ufe0f 危急内存警告"
        else:
            bg_color = "#e67e22"
            accent = "#f39c12"
            icon = "\u26a0\ufe0f"
            header_text = "\u26a0\ufe0f 内存不足警告"

        inner = tk.Frame(top, bg=bg_color, highlightbackground=accent,
                         highlightthickness=4, highlightcolor=accent,
                         padx=20, pady=20)
        inner.pack(fill="both", expand=True, padx=2, pady=2)

        tk.Label(inner, text=icon, font=("Arial", 42), bg=bg_color, fg="white").pack(pady=(10, 0))
        tk.Label(inner, text=header_text, font=("Arial", 18, "bold"),
                 bg=bg_color, fg="white").pack(pady=(0, 10))

        tk.Label(inner, text=msg, font=("Arial", 13, "bold"),
                 bg=bg_color, fg="white", wraplength=440, justify="center",
                 pady=6).pack()

        btn_frame = tk.Frame(inner, bg=bg_color)
        btn_frame.pack(pady=(15, 5))
        ok_btn = tk.Button(btn_frame, text="   我 知 道 了   ", font=("Arial", 13, "bold"),
                           bg=accent, fg="white", activebackground="#d35400", activeforeground="white",
                           relief="flat", bd=0, padx=20, pady=6,
                           cursor="hand2", command=top.destroy)
        ok_btn.pack()

        self._flash_border(inner, accent, bg_color, 0)

    def _flash_border(self, frame, accent, bg, count):
        if count >= 8 or not frame.winfo_exists():
            frame.configure(highlightbackground=accent, highlightthickness=4)
            return
        if count % 2 == 0:
            frame.configure(highlightbackground="white", highlightthickness=6)
        else:
            frame.configure(highlightbackground=accent, highlightthickness=4)
        self.root.after(300, self._flash_border, frame, accent, bg, count + 1)

    def execute_next_command(self, cmds):
        self.is_cmd_running = True
        cmd = cmds[self.current_cmd_index]
        cmd_num = self.current_cmd_index + 1
        total_cmds = len(cmds)
        
        def task():
            self.log_status(f"正在执行 [{cmd_num}/{total_cmds}]: {cmd}")
            try:
                run_cmd = cmd
                pwd_input = None
                pwd_val = self.sudo_pwd_var.get()

                if "sudo " in run_cmd and pwd_val:
                    run_cmd = run_cmd.replace("sudo ", "sudo -S ")
                    pwd_input = pwd_val + "\n"

                if pwd_input:
                    result = subprocess.run(
                        run_cmd, shell=True, input=pwd_input, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
                    )
                else:
                    result = subprocess.run(
                        run_cmd, shell=True, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
                    )

                if result.returncode == 0:
                    self.log_status(f"[{cmd_num}/{total_cmds}] 执行完毕。")
                else:
                    err_msg = result.stderr.strip() if result.stderr else f"退出码 {result.returncode}"
                    self.log_status(f"[{cmd_num}/{total_cmds}] 执行失败: {err_msg}")

            except subprocess.TimeoutExpired:
                self.log_status(f"[{cmd_num}/{total_cmds}] 执行超时 (15s)！跳过。")
            except Exception as e:
                self.log_status(f"[{cmd_num}/{total_cmds}] 未知错误: {e}")
            finally:
                self.current_cmd_index += 1
                self.is_cmd_running = False
                self.cmd_cooldown_until = time.time() + 15
                self.log_status("等待 15 秒后检测内存状态，再决定是否执行下一条指令...")

        threading.Thread(target=task, daemon=True).start()

    def _signal_handler(self, signum, frame):
        logging.info(f"收到信号 {signum}，正在安全退出...")
        self._shutdown_flag = True
        _release_lock()
        self.root.after(0, self.root.destroy)

    def _on_close(self):
        if self.is_monitoring:
            self.root.iconify()
            self.log_status("窗口已最小化到后台，监控继续运行中。点击任务栏图标可恢复窗口。")
        else:
            if messagebox.askyesno("退出确认", "确定要退出 Ubuntu 内存监控器吗？"):
                self._shutdown_flag = True
                _release_lock()
                self.root.destroy()

    def update_loop(self):
        if self._shutdown_flag:
            return
        mem = psutil.virtual_memory()
        percent = mem.percent

        self.progress['value'] = percent
        self.mem_lbl.config(text=f"当前内存使用率: {percent}%")

        if self.is_monitoring:
            try:
                warn_thresh = float(self.warn_var.get())
                crit_thresh = float(self.crit_var.get())
                cmds = self.config["critical"].get("commands", [])

                if percent >= crit_thresh:
                    if not self.is_cmd_running and time.time() >= self.cmd_cooldown_until:
                        if self.current_cmd_index < len(cmds):
                            self.show_popup("危急警告", f"内存达 {percent}%！\n正在执行第 {self.current_cmd_index + 1} 条干预指令...", is_critical=True)
                            self.execute_next_command(cmds)
                        else:
                            if not getattr(self, "_exhausted_logged", False):
                                self.log_status("⚠️ 所有干预指令已执行完毕，但内存依然处于危急状态！")
                                self.show_popup("内存崩溃警告", "所有干预措施已耗尽，请手动干预！", is_critical=True)
                                self._exhausted_logged = True
                elif percent >= warn_thresh:
                    if not self.warned:
                        self.show_popup("内存警告", f"内存已达 {percent}%！请注意清理！", is_critical=False)
                        self.warned = True
                else:
                    if percent < warn_thresh - 5:
                        self.warned = False
                    if percent < crit_thresh - 5:
                        self.auto_reset()
                        self._exhausted_logged = False
            except ValueError:
                pass 

        self.root.after(1000, self.update_loop)

if __name__ == "__main__":
    if not _acquire_lock():
        _root = tk.Tk()
        _root.withdraw()
        messagebox.showwarning("已在运行", "Ubuntu 内存监控器已在运行中！\n请先关闭已有实例再重新启动。")
        _root.destroy()
        sys.exit(0)
    root = tk.Tk()
    app = MemoryMonitorGUI(root)
    root.mainloop()