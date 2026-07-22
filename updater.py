#!/usr/bin/env python3
"""
A2L 翻译器 — 自动更新模块
==========================
- 启动时自动检查 GitHub 最新版本
- 发现新版本 → 后台下载 → 自动替换
- 支持绿色版（替换自身 exe）
"""

import sys
import os
import json
import time
import shutil
import urllib.request
import urllib.error
import tempfile
import threading
from pathlib import Path

_SRC = Path(__file__).parent
_CONFIG_PATH = _SRC / "update_config.json"
_VERSION_PATH = _SRC / "VERSION"


def get_current_version():
    """读取当前版本号"""
    try:
        return _VERSION_PATH.read_text().strip()
    except Exception:
        return "v0.0.0"


def get_config():
    """读取更新配置"""
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {
            "update_url": "https://raw.githubusercontent.com/00niu000/a2l-translator/main/update_config.json",
            "download_url": "https://github.com/00niu000/a2l-translator/releases/latest/download/A2L_Translator.exe",
            "auto_check": True,
            "check_interval_days": 1,
        }


def save_config(config):
    """保存更新配置"""
    try:
        with open(_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def check_for_update(callback=None):
    """
    检查 GitHub 是否有新版本。
    在后台线程中运行，完成后调用 callback(version_info)。

    Returns: None (异步)
    """
    config = get_config()
    update_url = config.get("update_url", "")

    if not update_url:
        if callback:
            callback(None)
        return

    def _check():
        try:
            req = urllib.request.Request(
                update_url,
                headers={"User-Agent": "A2L-Translator-Updater/2.9.5"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                remote_config = json.loads(resp.read().decode("utf-8"))

            remote_version = remote_config.get("current_version", "v0.0.0")
            current_version = get_current_version()
            download_url = remote_config.get("download_url", config.get("download_url", ""))

            # 版本比较
            def parse_version(v):
                v = v.lstrip("v")
                parts = v.split(".")
                return tuple(int(p) for p in parts if p.isdigit())

            current_tuple = parse_version(current_version)
            remote_tuple = parse_version(remote_version)

            has_update = remote_tuple > current_tuple

            info = {
                "current": current_version,
                "latest": remote_version,
                "has_update": has_update,
                "download_url": download_url,
            }

            # 更新最后检查时间
            config["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_config(config)

            if callback:
                callback(info)

        except Exception as e:
            if callback:
                callback({"error": str(e)})

    threading.Thread(target=_check, daemon=True).start()


def download_and_update(download_url, progress_callback=None, done_callback=None):
    """
    下载新版本并替换当前 exe。
    绿色版：下载到临时目录 → 替换自身 → 提示重启

    Args:
        download_url: 下载地址
        progress_callback: 进度回调 (percent, downloaded_mb, total_mb)
        done_callback: 完成回调 (success, message)
    """

    def _download():
        try:
            # 目标路径
            if getattr(sys, 'frozen', False):
                target_path = Path(sys.executable)
            else:
                target_path = Path(sys.argv[0]).resolve()

            # 下载到临时目录
            tmp_dir = Path(tempfile.gettempdir())
            tmp_file = tmp_dir / f"A2L_Translator_update_{int(time.time())}.exe"

            if progress_callback:
                progress_callback(0, 0, 0)

            req = urllib.request.Request(
                download_url,
                headers={"User-Agent": "A2L-Translator-Updater/2.9.5"}
            )

            with urllib.request.urlopen(req, timeout=120) as resp:
                total_size = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 8192

                with open(tmp_file, 'wb') as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size > 0:
                            pct = int(downloaded / total_size * 100)
                            progress_callback(pct, downloaded / 1024 / 1024, total_size / 1024 / 1024)

            # 验证下载的文件（简单检查大小）
            if tmp_file.stat().st_size < 100000:
                raise ValueError("下载文件过小，可能不完整")

            # 替换策略：先备份，再替换
            backup = Path(str(target_path) + ".bak")

            # 备份当前版本
            if target_path.exists():
                if backup.exists():
                    backup.unlink()
                shutil.copy2(str(target_path), str(backup))

            # 替换为新版本
            shutil.move(str(tmp_file), str(target_path))

            # 清理备份
            if backup.exists():
                try:
                    backup.unlink()
                except Exception:
                    pass

            if done_callback:
                done_callback(True, "更新完成，请重启应用")

        except Exception as e:
            if done_callback:
                done_callback(False, str(e))

    threading.Thread(target=_download, daemon=True).start()


# ══════════════════════════════════════════════════════════
#  GUI 集成
# ══════════════════════════════════════════════════════════

def check_and_notify(root, silent=False):
    """
    检查更新并在 GUI 中显示通知。
    如果 silent=True，只在有更新时才弹窗。

    用于 GUI 启动时调用。
    """

    def on_result(info):
        if info is None:
            return
        if "error" in info:
            if not silent:
                print(f"[更新检查失败] {info['error']}")
            return

        if info["has_update"]:
            import tkinter.messagebox as messagebox
            result = messagebox.askyesno(
                "发现新版本",
                f"当前版本: {info['current']}\n"
                f"最新版本: {info['latest']}\n\n"
                f"是否立即下载更新？"
            )
            if result and info["download_url"]:
                # 显示下载进度窗口
                _show_download_progress(root, info["download_url"])
        else:
            if not silent:
                import tkinter.messagebox as messagebox
                messagebox.showinfo("已是最新版本", f"当前版本 {info['current']} 已是最新")

    check_for_update(callback=on_result)


def _show_download_progress(root, download_url):
    """显示下载进度对话框"""
    import tkinter as tk
    from tkinter import ttk

    win = tk.Toplevel(root)
    win.title("下载更新")
    win.geometry("400x150")
    win.resizable(False, False)
    win.transient(root)
    win.grab_set()

    tk.Label(win, text="正在下载最新版本...",
             font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(16, 8))

    status_var = tk.StringVar(value="连接中...")
    tk.Label(win, textvariable=status_var,
             font=("Microsoft YaHei UI", 9), fg="#64748B").pack()

    progress = ttk.Progressbar(win, mode="determinate", length=320)
    progress.pack(pady=(8, 12))

    def on_progress(pct, dl_mb, total_mb):
        win.after(0, lambda: progress.configure(value=pct))
        if total_mb > 0:
            win.after(0, lambda: status_var.set(f"已下载 {dl_mb:.1f} / {total_mb:.1f} MB ({pct}%)"))

    def on_done(success, msg):
        if success:
            win.after(0, lambda: status_var.set("更新完成！"))
            win.after(0, lambda: tk.messagebox.showinfo("更新完成", f"请手动重启应用以应用更新。\n\n当前应用仍然可以使用。"))
        else:
            win.after(0, lambda: status_var.set(f"下载失败: {msg}"))
            win.after(0, lambda: tk.messagebox.showerror("更新失败", f"下载失败:\n{msg}"))
        win.after(500, win.destroy)

    download_and_update(download_url, progress_callback=on_progress, done_callback=on_done)


# ══════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"当前版本: {get_current_version()}")
    print("正在检查更新...")

    def on_result(info):
        if info is None:
            print("检查失败")
        elif "error" in info:
            print(f"错误: {info['error']}")
        elif info["has_update"]:
            print(f"发现新版本: {info['latest']} (当前: {info['current']})")
            print(f"下载地址: {info['download_url']}")
            ans = input("是否下载更新? [y/N] ")
            if ans.lower() == 'y':
                download_and_update(
                    info["download_url"],
                    progress_callback=lambda p, d, t: print(f"  下载中: {p}% ({d:.1f}/{t:.1f} MB)", end="\r"),
                    done_callback=lambda s, m: print(f"\n{'完成' if s else '失败'}: {m}")
                )
                while threading.active_count() > 1:
                    time.sleep(0.5)
        else:
            print(f"已是最新版本 ({info['current']})")

    check_for_update(callback=on_result)
    time.sleep(3)  # 等待异步检查完成
