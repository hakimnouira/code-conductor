"""Code Conductor Desktop Launcher & Process Controller.

Starts backend and frontend services, opens the browser,
and provides a clean one-click Shutdown / Turn Off button.
"""

import os
import sys
import time
import subprocess
import threading
import webbrowser
import tkinter as tk
from tkinter import messagebox

# Determine project root
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    # Check if run from Desktop or from project root
    if not os.path.exists(os.path.join(APP_DIR, "backend")):
        # Default fallback to D:\work\code-conductor
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

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

backend_proc = None
frontend_proc = None


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
        self.root.geometry("460x390")
        self.root.minsize(460, 390)
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
        status_frame = tk.Frame(root, bg="#0c0e14", padx=20, pady=16)
        status_frame.pack(fill="x")

        # Backend Status
        b_card = tk.Frame(status_frame, bg="#181c28", padx=14, pady=10, relief="flat", highlightthickness=1, highlightbackground="#2d3345")
        b_card.pack(fill="x", pady=(0, 10))

        self.b_dot = tk.Label(b_card, text="●", font=("Segoe UI", 12), fg="#f59e0b", bg="#181c28")
        self.b_dot.pack(side="left", padx=(0, 8))

        b_text_frame = tk.Frame(b_card, bg="#181c28")
        b_text_frame.pack(side="left")

        b_name = tk.Label(b_text_frame, text="Backend Server", font=("Segoe UI", 10, "bold"), fg="#e5e7eb", bg="#181c28")
        b_name.pack(anchor="w")

        self.b_status = tk.Label(b_text_frame, text="Starting on http://127.0.0.1:8765...", font=("Segoe UI", 8), fg="#9ca3af", bg="#181c28")
        self.b_status.pack(anchor="w")

        # Frontend Status
        f_card = tk.Frame(status_frame, bg="#181c28", padx=14, pady=10, relief="flat", highlightthickness=1, highlightbackground="#2d3345")
        f_card.pack(fill="x")

        self.f_dot = tk.Label(f_card, text="●", font=("Segoe UI", 12), fg="#f59e0b", bg="#181c28")
        self.f_dot.pack(side="left", padx=(0, 8))

        f_text_frame = tk.Frame(f_card, bg="#181c28")
        f_text_frame.pack(side="left")

        f_name = tk.Label(f_text_frame, text="Web Interface", font=("Segoe UI", 10, "bold"), fg="#e5e7eb", bg="#181c28")
        f_name.pack(anchor="w")

        self.f_status = tk.Label(f_text_frame, text="Starting on http://127.0.0.1:5173...", font=("Segoe UI", 8), fg="#9ca3af", bg="#181c28")
        self.f_status.pack(anchor="w")

        # Action Buttons Frame
        btn_frame = tk.Frame(root, bg="#0c0e14", padx=20, pady=12)
        btn_frame.pack(fill="both", expand=True)

        self.open_btn = tk.Button(
            btn_frame,
            text="🌐 Open in Browser",
            font=("Segoe UI", 10, "bold"),
            bg="#4f46e5",
            fg="#ffffff",
            activebackground="#4338ca",
            activeforeground="#ffffff",
            relief="flat",
            pady=8,
            cursor="hand2",
            command=self.open_browser,
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
            pady=8,
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

        # Auto-open browser
        time.sleep(1.0)
        webbrowser.open("http://127.0.0.1:5173")

    def open_browser(self):
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
