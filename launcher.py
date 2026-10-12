"""Code Conductor Desktop Launcher & Process Controller.

Starts backend and frontend services, opens in CloakBrowser (or system browser),
and provides a clean one-click Shutdown / Turn Off button.
"""

import os
import sys
import time
import shutil
import subprocess
import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

# Determine project root
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    if not os.path.exists(os.path.join(APP_DIR, "backend")):
        if os.path.exists(r"D:\work\code-conductor\backend"):
            APP_DIR = r"D:\work\code-conductor"
        elif os.path.exists(r"C:\Users\hakim\Desktop\vs extention\backend"):
            APP_DIR = r"C:\Users\hakim\Desktop\vs extention"
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

VENV_PYTHON = os.path.join(APP_DIR, ".venv", "Scripts", "python.exe")
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = r"D:\work\code-conductor\.venv\Scripts\python.exe"

BACKEND_DIR = os.path.join(APP_DIR, "backend")
WEB_DIR = os.path.join(APP_DIR, "web")
CLOAK_PROFILE_DIR = os.path.join(APP_DIR, ".cloak_profile")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

backend_proc = None
frontend_proc = None

CLOAK_CANDIDATES = [
    Path(r"D:\Downloads\cloakbrowser-windows-x64\chrome.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")),
    Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe")),
]


def find_cloak_browser() -> Path | None:
    """Finds CloakBrowser or chromium browser."""
    for p in CLOAK_CANDIDATES:
        if p.exists() and p.is_file():
            return p
    which_chrome = shutil.which("chrome") or shutil.which("msedge")
    if which_chrome:
        return Path(which_chrome)
    return None


def open_in_cloak(url: str = "http://127.0.0.1:5173") -> bool:
    """Launches URL in CloakBrowser with persistent session and CDP port 9222."""
    exe = find_cloak_browser()
    if not exe:
        return False
    try:
        os.makedirs(CLOAK_PROFILE_DIR, exist_ok=True)
        cmd = [
            str(exe),
            "--remote-debugging-port=9222",
            f"--user-data-dir={CLOAK_PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            url,
        ]
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            cmd,
            creationflags=flags,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def kill_proc_tree(proc):
    if not proc:
        return
    try:
        pid = proc.pid
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            proc.terminate()
    except Exception:
        pass


def kill_existing_on_ports():
    """Cleanly free ports 8765 and 5173 if anything was left behind."""
    if sys.platform != "win32":
        return
    for port in [8765, 5173]:
        try:
            res = subprocess.run(
                f"netstat -ano | findstr :{port}",
                shell=True,
                capture_output=True,
                text=True,
                creationflags=CREATE_NO_WINDOW,
            )
            for line in res.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5 and "LISTENING" in line:
                    pid = parts[-1]
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,
                    )
        except Exception:
            pass


class ConductorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Code Conductor — Launcher")
        self.root.geometry("490x520")
        self.root.minsize(490, 520)
        self.root.configure(bg="#0c0e14")

        # Top Bar
        top_frame = tk.Frame(root, bg="#12151f", padx=20, pady=16)
        top_frame.pack(fill="x")

        title_label = tk.Label(
            top_frame,
            text="⚡ Code Conductor",
            font=("Segoe UI", 16, "bold"),
            fg="#f3f4f6",
            bg="#12151f",
        )
        title_label.pack(anchor="w")

        sub_label = tk.Label(
            top_frame,
            text="AI Engineering Studio & Multi-Agent Orchestrator",
            font=("Segoe UI", 9),
            fg="#9ca3af",
            bg="#12151f",
        )
        sub_label.pack(anchor="w", pady=(2, 0))

        # Status Cards Frame
        status_frame = tk.Frame(root, bg="#0c0e14", padx=20, pady=12)
        status_frame.pack(fill="x")

        # Backend Status Card
        b_card = tk.Frame(status_frame, bg="#181c28", padx=12, pady=8, relief="flat", highlightthickness=1, highlightbackground="#2d3345")
        b_card.pack(fill="x", pady=(0, 6))

        self.b_dot = tk.Label(b_card, text="●", font=("Segoe UI", 11), fg="#f59e0b", bg="#181c28")
        self.b_dot.pack(side="left", padx=(0, 8))

        b_text_frame = tk.Frame(b_card, bg="#181c28")
        b_text_frame.pack(side="left")

        b_name = tk.Label(b_text_frame, text="Backend Server", font=("Segoe UI", 9, "bold"), fg="#e5e7eb", bg="#181c28")
        b_name.pack(anchor="w")

        self.b_status = tk.Label(b_text_frame, text="Starting on http://127.0.0.1:8765...", font=("Segoe UI", 8), fg="#9ca3af", bg="#181c28")
        self.b_status.pack(anchor="w")

        # Frontend Status Card
        f_card = tk.Frame(status_frame, bg="#181c28", padx=12, pady=8, relief="flat", highlightthickness=1, highlightbackground="#2d3345")
        f_card.pack(fill="x", pady=(0, 6))

        self.f_dot = tk.Label(f_card, text="●", font=("Segoe UI", 11), fg="#f59e0b", bg="#181c28")
        self.f_dot.pack(side="left", padx=(0, 8))

        f_text_frame = tk.Frame(f_card, bg="#181c28")
        f_text_frame.pack(side="left")

        f_name = tk.Label(f_text_frame, text="Web Interface", font=("Segoe UI", 9, "bold"), fg="#e5e7eb", bg="#181c28")
        f_name.pack(anchor="w")

        self.f_status = tk.Label(f_text_frame, text="Starting on http://127.0.0.1:5173...", font=("Segoe UI", 8), fg="#9ca3af", bg="#181c28")
        self.f_status.pack(anchor="w")

        # CloakBrowser Status Card
        c_card = tk.Frame(status_frame, bg="#181c28", padx=12, pady=8, relief="flat", highlightthickness=1, highlightbackground="#2d3345")
        c_card.pack(fill="x")

        cloak_exe = find_cloak_browser()
        is_cloak_installed = cloak_exe is not None

        self.c_dot = tk.Label(
            c_card,
            text="●",
            font=("Segoe UI", 11),
            fg="#10b981" if is_cloak_installed else "#f59e0b",
            bg="#181c28",
        )
        self.c_dot.pack(side="left", padx=(0, 8))

        c_text_frame = tk.Frame(c_card, bg="#181c28")
        c_text_frame.pack(side="left")

        c_name = tk.Label(c_text_frame, text="CloakBrowser Stealth Engine", font=("Segoe UI", 9, "bold"), fg="#e5e7eb", bg="#181c28")
        c_name.pack(anchor="w")

        cloak_info = "Ready · Persistent profile & CDP 9222 for Web APIs" if is_cloak_installed else "Not found · Will use system browser"
        self.c_status = tk.Label(c_text_frame, text=cloak_info, font=("Segoe UI", 8), fg="#10b981" if is_cloak_installed else "#9ca3af", bg="#181c28")
        self.c_status.pack(anchor="w")

        # Open Mode Preference Checkbox
        opt_frame = tk.Frame(root, bg="#0c0e14", padx=20, pady=4)
        opt_frame.pack(fill="x")

        self.use_cloak_var = tk.BooleanVar(value=is_cloak_installed)
        self.cloak_check = tk.Checkbutton(
            opt_frame,
            text="🛡️ Auto-open in CloakBrowser (Stealth session + Web AI models)",
            variable=self.use_cloak_var,
            font=("Segoe UI", 9),
            fg="#c7d2fe",
            bg="#0c0e14",
            activebackground="#0c0e14",
            activeforeground="#ffffff",
            selectcolor="#1e1b4b",
        )
        self.cloak_check.pack(anchor="w")

        # Action Buttons Frame
        btn_frame = tk.Frame(root, bg="#0c0e14", padx=20, pady=10)
        btn_frame.pack(fill="both", expand=True)

        self.cloak_btn = tk.Button(
            btn_frame,
            text="🛡️ Open in CloakBrowser",
            font=("Segoe UI", 10, "bold"),
            bg="#4f46e5",
            fg="#ffffff",
            activebackground="#4338ca",
            activeforeground="#ffffff",
            relief="flat",
            pady=8,
            cursor="hand2",
            command=self.open_cloak_action,
        )
        self.cloak_btn.pack(fill="x", pady=(0, 6))

        self.open_btn = tk.Button(
            btn_frame,
            text="🌐 Open in System Browser",
            font=("Segoe UI", 9),
            bg="#1f2937",
            fg="#e5e7eb",
            activebackground="#374151",
            activeforeground="#ffffff",
            relief="flat",
            pady=6,
            cursor="hand2",
            command=self.open_system_browser,
        )
        self.open_btn.pack(fill="x", pady=(0, 8))

        self.shutdown_btn = tk.Button(
            btn_frame,
            text="🛑 SHUTDOWN & TURN OFF",
            font=("Segoe UI", 10, "bold"),
            bg="#b91c1c",
            fg="#ffffff",
            activebackground="#991b1b",
            activeforeground="#ffffff",
            relief="flat",
            pady=8,
            cursor="hand2",
            command=self.shutdown_servers,
        )
        self.shutdown_btn.pack(fill="x")

        # Footer
        footer_label = tk.Label(
            root,
            text="Close this window or click Shutdown to turn off all servers.",
            font=("Segoe UI", 8),
            fg="#6b7280",
            bg="#0c0e14",
            pady=6,
        )
        footer_label.pack(side="bottom")

        # Hook Window Close
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Start services in background thread
        threading.Thread(target=self.start_services, daemon=True).start()

    def start_services(self):
        global backend_proc, frontend_proc
        kill_existing_on_ports()
        time.sleep(0.5)

        # Start Backend
        try:
            backend_cmd = [
                VENV_PYTHON,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
            ]
            backend_proc = subprocess.Popen(
                backend_cmd,
                cwd=BACKEND_DIR,
                creationflags=CREATE_NO_WINDOW,
            )
            time.sleep(1.5)
            self.b_dot.config(fg="#10b981")
            self.b_status.config(text="Online · http://127.0.0.1:8765", fg="#10b981")
        except Exception as exc:
            self.b_dot.config(fg="#ef4444")
            self.b_status.config(text=f"Error: {exc}", fg="#ef4444")

        # Start Frontend
        try:
            frontend_cmd = ["cmd.exe", "/c", "npm", "--prefix", WEB_DIR, "run", "dev"]
            frontend_proc = subprocess.Popen(
                frontend_cmd,
                cwd=APP_DIR,
                creationflags=CREATE_NO_WINDOW,
            )
            time.sleep(2.0)
            self.f_dot.config(fg="#10b981")
            self.f_status.config(text="Online · http://127.0.0.1:5173", fg="#10b981")
        except Exception as exc:
            self.f_dot.config(fg="#ef4444")
            self.f_status.config(text=f"Error: {exc}", fg="#ef4444")

        # Auto-open browser according to preference
        time.sleep(1.0)
        if self.use_cloak_var.get() and find_cloak_browser():
            if not open_in_cloak("http://127.0.0.1:5173"):
                webbrowser.open("http://127.0.0.1:5173")
        else:
            webbrowser.open("http://127.0.0.1:5173")

    def open_cloak_action(self):
        if not open_in_cloak("http://127.0.0.1:5173"):
            messagebox.showinfo("CloakBrowser", "CloakBrowser was not found. Opening in system browser instead.")
            webbrowser.open("http://127.0.0.1:5173")

    def open_system_browser(self):
        webbrowser.open("http://127.0.0.1:5173")

    def shutdown_servers(self):
        global backend_proc, frontend_proc
        if messagebox.askyesno("Confirm Shutdown", "Turn off Code Conductor servers and close application?"):
            self.shutdown_btn.config(text="Shutting down...", state="disabled")
            kill_proc_tree(backend_proc)
            kill_proc_tree(frontend_proc)
            kill_existing_on_ports()
            self.root.destroy()
            sys.exit(0)

    def on_close(self):
        self.shutdown_servers()


def main():
    root = tk.Tk()
    app = ConductorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
