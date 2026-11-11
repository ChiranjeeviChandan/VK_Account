"""A floating command bar that stays above every window on the laptop.

    felix overlay

Type a request, press Enter, read the answer as it streams. Esc tucks the bar
away; clicking it brings it back. It talks to the running Felix Core on this
machine. Bind `felix overlay` to a keyboard shortcut in your system settings
to summon it from anywhere.

Uses Tk, which ships with Python on Windows and macOS (on Linux install the
`python3-tk` package).
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from .client import CoreClient, CoreUnavailable

WIDTH = 620
COLORS = {"bg": "#14161a", "panel": "#1d2026", "text": "#e8eaed", "muted": "#9aa0a6", "accent": "#7aa2f7",
          "ok": "#2e7d5b", "no": "#8a3b3b"}


def run(client: CoreClient) -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("The overlay needs Tk. On Linux: sudo apt install python3-tk. "
              "Or use the web overlay: open the dashboard and choose Overlay.")
        return 1
    if not client.running():
        raise CoreUnavailable("Felix is not running. Start it with: felix start")
    Overlay(tk, client).loop()
    return 0


class Overlay:
    def __init__(self, tk: Any, client: CoreClient) -> None:
        self.tk, self.client = tk, client
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.root = root = tk.Tk()
        root.title("Felix")
        root.configure(bg=COLORS["bg"])
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        screen = root.winfo_screenwidth()
        root.geometry(f"{WIDTH}x54+{max(0, (screen - WIDTH) // 2)}+48")
        root.resizable(False, False)

        self.entry = tk.Entry(root, font=("Segoe UI", 14), bg=COLORS["panel"], fg=COLORS["text"],
                              insertbackground=COLORS["text"], relief="flat", highlightthickness=1,
                              highlightbackground=COLORS["panel"], highlightcolor=COLORS["accent"])
        self.entry.pack(fill="x", padx=10, pady=(10, 8), ipady=7)
        self.entry.insert(0, "")
        self.status = tk.Label(root, text="Ask Felix - Enter to send, Esc to tuck away", anchor="w",
                               bg=COLORS["bg"], fg=COLORS["muted"], font=("Segoe UI", 9))
        self.output = tk.Text(root, wrap="word", height=12, bg=COLORS["bg"], fg=COLORS["text"], relief="flat",
                              font=("Segoe UI", 11), padx=12, pady=6, state="disabled", cursor="arrow")
        self.actions = tk.Frame(root, bg=COLORS["bg"])

        self.entry.bind("<Return>", self.send)
        root.bind("<Escape>", lambda event: root.iconify())
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        self.entry.focus_set()
        root.after(60, self.pump)

    def loop(self) -> None:
        self.root.mainloop()

    # -- sending -----------------------------------------------------------------
    def send(self, event: Any = None) -> None:
        text = self.entry.get().strip()
        if not text or self.busy:
            return
        self.busy = True
        self.entry.delete(0, "end")
        self.show(f"You: {text}\n\n", clear=True)
        self.set_actions([])
        threading.Thread(target=self.work, args=(text,), daemon=True).start()

    def work(self, text: str) -> None:
        """Runs off the UI thread; hands results back through the queue."""
        try:
            result = self.client.chat(text, thread="overlay", on_delta=lambda piece: self.events.put(("delta", piece)))
            self.events.put(("done", result))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "delta":
                    self.show(payload)
                elif kind == "done":
                    self.busy = False
                    engine = payload.get("engine", "")
                    self.status.configure(text={"claude": "Claude", "local": "Local model", "rules": "Offline commands",
                                                "policy": "Done"}.get(engine, engine))
                    self.set_actions(payload.get("approvals") or [])
                else:
                    self.busy = False
                    self.show(f"\n{payload}\n")
        except queue.Empty:
            pass
        self.root.after(60, self.pump)

    # -- drawing -----------------------------------------------------------------
    def show(self, text: str, clear: bool = False) -> None:
        if not self.output.winfo_ismapped():
            self.output.pack(fill="both", expand=True, padx=2)
            self.status.pack(fill="x", padx=12, pady=(0, 6))
            self.root.geometry(f"{WIDTH}x340")
        self.output.configure(state="normal")
        if clear:
            self.output.delete("1.0", "end")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def set_actions(self, approvals: list[dict[str, Any]]) -> None:
        for child in self.actions.winfo_children():
            child.destroy()
        if not approvals:
            self.actions.pack_forget()
            return
        self.actions.pack(fill="x", padx=12, pady=(0, 8), before=self.status)
        for approval in approvals:
            label = f"#{approval['id']}"
            self.tk.Button(self.actions, text=f"Approve {label}", bg=COLORS["ok"], fg="white", relief="flat",
                           command=lambda a=approval: self.decide(a, True)).pack(side="left", padx=(0, 6))
            self.tk.Button(self.actions, text=f"Cancel {label}", bg=COLORS["no"], fg="white", relief="flat",
                           command=lambda a=approval: self.decide(a, False)).pack(side="left", padx=(0, 12))

    def decide(self, approval: dict[str, Any], yes: bool) -> None:
        try:
            if yes:
                message = self.client.post(f"/api/approvals/{approval['id']}/approve").get("message", "Done.")
            else:
                self.client.post(f"/api/approvals/{approval['id']}/deny")
                message = f"Cancelled #{approval['id']}."
        except Exception as exc:
            message = str(exc)
        self.show(f"\n{message}\n")
        self.set_actions([])
