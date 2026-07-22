#!/usr/bin/env python3
"""
A2L/KP 文件翻译工具 - 简约版
============================
拖放文件 → 一键翻译 → 导出
"""

import sys, os, re, json, csv, time, ssl, threading, tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.parse, urllib.error

# DPI
import ctypes
try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
except: pass

# ── 模块加载 ──
from glossary_data import BUILTIN_GLOSSARY, GERMAN_GLOSSARY, SMART_KEYWORDS
from dictionary_resources import MultiSourceDictionary, get_dictionary
from baidu_api import baidu_translate_batch
from a2l_translator import build_glossary_index, translate_with_glossary_fast
from fuzzy_engine import (
    deep_fuzzy_search,
    _GENERAL_WORD_BANK, _SPELL_VARIANTS, normalize_spelling,
)
from translation_memory import (
    load_translation_memory, save_translation_memory,
    apply_tm, update_tm_from_items, load_custom_glossary, merge_glossary,
)

# ── 颜色 ──
C = {"bg":"#FAFAFA","card":"#FFFFFF","text":"#1a1a1a","sub":"#888888",
     "accent":"#2563EB","accent_h":"#1D4ED8","border":"#E5E5E5","ok":"#16A34A","warn":"#EA580C"}

# ═══════════════════════════════════════════════════
# 核心函数（从原版精简）
# ═══════════════════════════════════════════════════

PATTERNS = [
    ("MEASUREMENT", re.compile(r'/begin\s+MEASUREMENT\s+(\w+)\s+"([^"]*)"', re.I), 2, 1),
    ("CHARACTERISTIC", re.compile(r'/begin\s+CHARACTERISTIC\s+(\w+)\s+"([^"]*)"', re.I), 2, 1),
    ("FUNCTION", re.compile(r'/begin\s+FUNCTION\s+(\w+)\s+"([^"]*)"', re.I), 2, 1),
    ("GROUP", re.compile(r'/begin\s+GROUP\s+(\w+)\s+"([^"]*)"', re.I), 2, 1),
    ("AXIS_PTS", re.compile(r'/begin\s+AXIS_PTS\s+(\w+)\s+"([^"]*)"', re.I), 2, 1),
    ("COMPU_METHOD", re.compile(r'/begin\s+COMPU_METHOD\s+(\w+)\s+"([^"]*)"', re.I), 2, 1),
    ("COMPU_VTAB", re.compile(r'/begin\s+COMPU_VTAB\s+(\w+)\s+"([^"]*)"', re.I), 2, 1),
    ("COMPU_VTAB_RANGE", re.compile(r'/begin\s+COMPU_VTAB_RANGE\s+(\w+)\s+"([^"]*)"', re.I), 2, 1),
    ("PROJECT", re.compile(r'/begin\s+PROJECT\s+(\w+)\s+"([^"]*)"', re.I), 2, 1),
    ("MODULE", re.compile(r'/begin\s+MODULE\s+(\w+)\s+"([^"]*)"', re.I), 2, 1),
    ("HEADER", re.compile(r'/begin\s+HEADER\s+"([^"]*)"', re.I), 1, 0),
]
COMMENT_RE = re.compile(r'/\*([\s\S]*?)\*/|//([^\r\n]*)')

SKIP_PATTERNS = [
    re.compile(r'^[0-9+\-*/\s().,eE]+$'),
    re.compile(r'^%[0-9.]*[dfexs]$', re.I),
    re.compile(r'^0x[0-9a-fA-F]+$'),
    re.compile(r'^[A-Z_]{2,20}$'),
]

def is_skippable(t):
    t = t.strip()
    if len(t) < 2: return True
    for p in SKIP_PATTERNS:
        if p.match(t): return True
    return False

def parse_file(filepath):
    """Parse A2L or KP file, return (entries, content)"""
    path = Path(filepath)
    is_kp = path.suffix.lower() == '.kp'

    if is_kp:
        from kp_parser import parse_kp_header, extract_translatable as kp_extract
        with open(filepath, 'rb') as f:
            data = f.read()
        info = parse_kp_header(data)
        entries = kp_extract(info)
        return entries, data

    # A2L
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(filepath, 'r', encoding='latin-1') as f:
            content = f.read()

    entries = []
    seen = set()
    cid = [0]

    for typ, regex, dg, ng in PATTERNS:
        for m in regex.finditer(content):
            desc = m.group(dg)
            name = m.group(ng) if ng > 0 else ""
            full = m.group(0)
            sp = m.start() + full.rfind(desc)
            ep = sp + len(desc)
            if is_skippable(desc): continue
            key = (sp, ep)
            if key in seen: continue
            seen.add(key)
            cid[0] += 1
            entries.append({"id": cid[0], "type": typ, "name": name, "original": desc, "translated": "", "status": "untranslated", "start": sp, "end": ep})

    for m in COMMENT_RE.finditer(content):
        body = (m.group(1) or m.group(2) or "").strip()
        if len(body) < 3 or is_skippable(body): continue
        sp = m.start() + 2
        ep = m.end() if m.group(1) else m.start() + len(m.group(0))
        key = (sp, ep)
        if key in seen: continue
        seen.add(key)
        cid[0] += 1
        entries.append({"id": cid[0], "type": "COMMENT", "name": "", "original": m.group(1) or m.group(2), "translated": "", "status": "untranslated", "start": sp, "end": ep})

    entries.sort(key=lambda x: x["start"])
    return entries, content

def rebuild_a2l(content, entries):
    translated = [i for i in entries if i["translated"] and i["translated"] != i["original"]]
    if not translated: return content
    translated.sort(key=lambda x: x["start"])
    parts, last = [], 0
    for item in translated:
        parts.append(content[last:item["start"]])
        parts.append(item["translated"])
        last = item["end"]
    parts.append(content[last:])
    return "".join(parts)

# ═══════════════════════════════════════════════════
# 简约 GUI
# ═══════════════════════════════════════════════════

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("A2L/KP 翻译工具")
        self.root.geometry("900x620")
        self.root.minsize(600, 400)
        self.root.configure(bg=C["bg"])

        self.filepath = tk.StringVar()
        self.entries = []
        self.original_content = None
        self.is_processing = False
        self.glossary = {}
        self.glossary_index = None
        self.tm = {}
        self.tm = {}
        self.glossary = {}
        self.glossary_index = None

        self._load_dictionaries()
        self._build_ui()
        self._auto_check_update()

    def _load_dictionaries(self):
        self.glossary = dict(BUILTIN_GLOSSARY)
        self.glossary.update(GERMAN_GLOSSARY)
        custom = load_custom_glossary()
        if custom: self.glossary = merge_glossary(self.glossary, custom)
        self.glossary_index = build_glossary_index(self.glossary)
        self.tm = load_translation_memory()
        # Load API config
        self.cfg_path = (Path(__file__).parent if not getattr(sys, 'frozen', False) else Path(sys.executable).parent) / "config.json"
        self.cfg = {}
        try:
            if self.cfg_path.exists():
                self.cfg = json.load(open(self.cfg_path, encoding='utf-8'))
        except: pass
        self.api_choice = tk.StringVar(value=self.cfg.get("api", "baidu"))
        self.baidu_appid = self.cfg.get("baidu_appid", "")
        self.baidu_secret = self.cfg.get("baidu_secret", "")
        self.deepl_key = self.cfg.get("deepl_key", "")
        self.google_key = self.cfg.get("google_key", "")

    def _auto_check_update(self):
        try:
            from updater import check_and_notify
            self.root.after(3000, lambda: check_and_notify(self.root, silent=True))
        except: pass

    # ── UI ──

    def _build_ui(self):
        # Top bar
        top = tk.Frame(self.root, bg=C["card"], height=64)
        top.pack(fill="x", padx=0, pady=0)
        top.pack_propagate(False)

        inner = tk.Frame(top, bg=C["card"])
        inner.pack(fill="both", padx=20, pady=10)

        tk.Label(inner, text="A2L/KP 翻译工具", font=("Microsoft YaHei UI", 16, "bold"),
                 fg=C["text"], bg=C["card"]).pack(side="left")

        tk.Label(inner, text="拖放文件  |  一键翻译  |  导出结果",
                 font=("Microsoft YaHei UI", 9), fg=C["sub"], bg=C["card"]).pack(side="left", padx=16)

        # API status button
        has_api = self.baidu_appid or self.deepl_key or self.google_key
        api_name = self.cfg.get("api", "baidu").capitalize()
        self.api_btn = tk.Button(inner, text=f"API: {api_name}", command=self._open_settings,
                                  font=("Microsoft YaHei UI", 8), bg=C["ok"] if has_api else C["warn"],
                                  fg="white", borderwidth=0, cursor="hand2", padx=10, pady=2)
        self.api_btn.pack(side="right")

        # File bar
        file_bar = tk.Frame(self.root, bg=C["bg"])
        file_bar.pack(fill="x", padx=20, pady=(12, 0))

        self.file_entry = tk.Entry(file_bar, textvariable=self.filepath,
                                    font=("Consolas", 10), relief="solid", borderwidth=1,
                                    fg=C["text"], bg=C["card"])
        self.file_entry.pack(side="left", fill="x", expand=True, ipady=5)

        tk.Button(file_bar, text="选择文件", command=self._select_file,
                  font=("Microsoft YaHei UI", 9), bg=C["card"], fg=C["text"],
                  borderwidth=1, relief="solid", cursor="hand2", padx=14, pady=4).pack(side="left", padx=6)

        self.btn_translate = tk.Button(file_bar, text="▶ 翻译", command=self._translate,
                                        font=("Microsoft YaHei UI", 10, "bold"),
                                        bg=C["accent"], fg="white", borderwidth=0,
                                        cursor="hand2", padx=20, pady=4, activebackground=C["accent_h"])
        self.btn_translate.pack(side="left", padx=4)

        self.btn_export = tk.Button(file_bar, text="导出", command=self._export,
                                     font=("Microsoft YaHei UI", 9),
                                     bg=C["card"], fg=C["text"], borderwidth=1, relief="solid",
                                     cursor="hand2", padx=14, pady=4, state="disabled")
        self.btn_export.pack(side="left", padx=2)

        # Progress
        self.progress = ttk.Progressbar(self.root, mode="indeterminate", length=200)
        self.progress.pack(fill="x", padx=20, pady=(6, 0))

        # Table
        table_frame = tk.Frame(self.root, bg=C["bg"])
        table_frame.pack(fill="both", expand=True, padx=20, pady=(6, 8))

        columns = ("#", "类型", "名称", "原文", "译文", "状态")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings",
                                  selectmode="extended", height=12)
        self.tree.pack(side="left", fill="both", expand=True)

        widths = [40, 100, 120, 250, 250, 80]
        for col, w in zip(columns, widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, minwidth=30)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.bind("<Double-1>", self._edit_cell)

        # Stats bar
        self.status_var = tk.StringVar(value="就绪 — 选择 A2L 或 KP 文件开始")
        tk.Label(self.root, textvariable=self.status_var, font=("Microsoft YaHei UI", 9),
                 fg=C["sub"], bg=C["bg"]).pack(side="left", padx=20, pady=(0, 8))

        self.stats_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.stats_var, font=("Microsoft YaHei UI", 9, "bold"),
                 fg=C["accent"], bg=C["bg"]).pack(side="right", padx=20, pady=(0, 8))

        # Drop target (tkinterdnd2 optional)
        try:
            self.root.drop_target_register("DND_Files")
            self.root.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    # ── Settings ──

    def _open_settings(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("API 设置")
        dlg.geometry("460x420")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, text="翻译 API 配置", font=("Microsoft YaHei UI", 13, "bold")).pack(pady=(12, 2))
        tk.Label(dlg, text="配置后自动保存，可随时切换引擎", font=("Microsoft YaHei UI", 9), fg=C["sub"]).pack()

        # API selection
        sel_frame = tk.Frame(dlg, bg="white")
        sel_frame.pack(pady=(10, 4), padx=20, fill="x")
        tk.Label(sel_frame, text="翻译引擎", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        choices = [("百度翻译 (汽车领域模型)", "baidu"),
                   ("DeepL (德/英→中最佳)", "deepl"),
                   ("Google 翻译 (覆盖面最广)", "google")]
        for label, val in choices:
            tk.Radiobutton(sel_frame, text=label, variable=self.api_choice, value=val,
                          font=("Microsoft YaHei UI", 9), bg="white", anchor="w").pack(anchor="w", pady=2)

        # Baidu
        bd = tk.LabelFrame(dlg, text="百度翻译", font=("Microsoft YaHei UI", 9, "bold"), padx=8, pady=4)
        bd.pack(pady=(8, 2), padx=20, fill="x")
        baidu_appid = tk.StringVar(value=self.baidu_appid)
        baidu_secret = tk.StringVar(value=self.baidu_secret)
        tk.Label(bd, text="APP ID", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        tk.Entry(bd, textvariable=baidu_appid, font=("Consolas", 9), width=50).pack(fill="x", ipady=2)
        tk.Label(bd, text="Secret", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        tk.Entry(bd, textvariable=baidu_secret, font=("Consolas", 9), width=50, show="*").pack(fill="x", ipady=2)

        # DeepL
        dl = tk.LabelFrame(dlg, text="DeepL (推荐德/英→中)", font=("Microsoft YaHei UI", 9, "bold"), padx=8, pady=4)
        dl.pack(pady=(4, 2), padx=20, fill="x")
        deepl_key = tk.StringVar(value=self.deepl_key)
        tk.Label(dl, text="API Key", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        tk.Entry(dl, textvariable=deepl_key, font=("Consolas", 9), width=50, show="*").pack(fill="x", ipady=2)
        tk.Label(dl, text="注册: deepl.com/pro-api  |  免费 50万字符/月", font=("Microsoft YaHei UI", 7), fg=C["sub"]).pack(anchor="w")

        # Google
        gl = tk.LabelFrame(dlg, text="Google 翻译", font=("Microsoft YaHei UI", 9, "bold"), padx=8, pady=4)
        gl.pack(pady=(4, 2), padx=20, fill="x")
        google_key = tk.StringVar(value=self.google_key)
        tk.Label(gl, text="API Key", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        tk.Entry(gl, textvariable=google_key, font=("Consolas", 9), width=50, show="*").pack(fill="x", ipady=2)
        tk.Label(gl, text="注册: cloud.google.com/translate  |  免费 50万字符/月", font=("Microsoft YaHei UI", 7), fg=C["sub"]).pack(anchor="w")

        def save():
            self.api_choice_val = self.api_choice.get()
            self.baidu_appid = baidu_appid.get().strip()
            self.baidu_secret = baidu_secret.get().strip()
            self.deepl_key = deepl_key.get().strip()
            self.google_key = google_key.get().strip()
            self.cfg.update({
                "api": self.api_choice_val,
                "baidu_appid": self.baidu_appid,
                "baidu_secret": self.baidu_secret,
                "deepl_key": self.deepl_key,
                "google_key": self.google_key,
            })
            try:
                json.dump(self.cfg, open(self.cfg_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
            except: pass
            has_api = self.baidu_appid or self.deepl_key or self.google_key
            self.api_btn.config(text=f"API: {self.api_choice_val.capitalize()}",
                               bg=C["ok"] if has_api else C["warn"])
            dlg.destroy()

        tk.Button(dlg, text="保存配置", command=save, font=("Microsoft YaHei UI", 10, "bold"),
                  bg=C["accent"], fg="white", borderwidth=0, padx=32, pady=5,
                  cursor="hand2").pack(pady=10)

    # ── Actions ──

    def _select_file(self):
        path = filedialog.askopenfilename(
            title="选择 A2L 或 KP 文件",
            filetypes=[("A2L/KP 文件", "*.a2l;*.kp"), ("所有文件", "*.*")]
        )
        if path:
            self.filepath.set(path)
            self._load_file()

    def _on_drop(self, event):
        files = self.root.tk.splitlist(event.data)
        if files:
            self.filepath.set(files[0])
            self._load_file()

    def _load_file(self):
        path = self.filepath.get()
        if not path or not os.path.isfile(path):
            self.status_var.set("文件不存在")
            return
        if self.is_processing: return

        self.is_processing = True
        self.status_var.set("加载中...")
        self.progress.start()
        self.btn_translate.config(state="disabled")

        def worker():
            try:
                entries, content = parse_file(path)
                # Quick glossary match
                for entry in entries:
                    if not entry.get("translated"):
                        result = translate_with_glossary_fast(
                            entry["original"], self.glossary, self.glossary_index)
                        if result:
                            entry["translated"] = result
                            entry["status"] = "auto"
                self.entries = entries
                self.original_content = content
                self.root.after(0, self._update_table)
                self.root.after(0, lambda: self.status_var.set(
                    f"已加载 {os.path.basename(path)} — {len(entries)} 条"))
                self.root.after(0, lambda: self.btn_translate.config(state="normal", text="▶ 完整翻译"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("错误", str(e)))
                self.root.after(0, lambda: self.status_var.set("加载失败"))
            finally:
                self.root.after(0, self.progress.stop)
                self.root.after(0, lambda: setattr(self, "is_processing", False))

        threading.Thread(target=worker, daemon=True).start()

    def _translate(self):
        if not self.entries: return
        if self.is_processing: return

        self.is_processing = True
        self.btn_translate.config(state="disabled")
        self.btn_export.config(state="disabled")
        self.status_var.set("翻译中...")
        self.progress.start()
        self.progress.config(mode="indeterminate")

        def worker():
            # TM
            tm_hits = apply_tm(self.entries, self.tm)
            # Smart
            smart_count = 0
            for entry in self.entries:
                if entry.get("translated"): continue
                if entry["type"] in ("COMPU_METHOD", "FUNCTION"): continue
                result = deep_fuzzy_search(entry["original"], self.glossary, self.tm, threshold=0.50)
                if result[0]:
                    entry["translated"] = result[0]
                    entry["status"] = "auto"
                    smart_count += 1

            # API translation for remaining
            api_count = 0
            remaining = [e for e in self.entries if not e.get("translated") and e["type"] not in ("COMPU_METHOD",)]
            if remaining and len(remaining) > 0:
                api = self.cfg.get("api", "baidu")
                texts = [e["original"].strip() for e in remaining]
                result = None

                if api == "deepl" and self.deepl_key:
                    from deepl_api import deepl_translate_batch
                    result = deepl_translate_batch(texts, "auto", "zh-CN", self.deepl_key)
                elif api == "google" and self.google_key:
                    from google_api import google_translate_batch
                    result = google_translate_batch(texts, "auto", "zh-CN", self.google_key)
                elif self.baidu_appid and self.baidu_secret:
                    from baidu_api import baidu_translate_batch
                    result = baidu_translate_batch(texts, "auto", "zh-CN", self.baidu_appid, self.baidu_secret)

                if result:
                    for i, entry in enumerate(remaining):
                        if i < len(result) and result[i] and result[i] != entry["original"]:
                            entry["translated"] = result[i]
                            entry["status"] = "auto"
                            api_count += 1

            # Word-level fallback for remaining
            word_map = {}
            word_map.update({k.lower(): v for k, v in _GENERAL_WORD_BANK.items() if v})
            word_map.update(_SPELL_VARIANTS)
            for en, zh in self.glossary.items():
                ews = en.split(); zws = zh.split()
                if len(ews) == len(zws):
                    for ew, zw in zip(ews, zws):
                        wl = ew.lower().strip(".,;:()[]{}<>!?/\\-_")
                        if len(wl) >= 2 and wl not in word_map:
                            word_map[wl] = zw.strip("，。；：")
            wc = 0
            for entry in self.entries:
                if entry.get("translated") or entry["type"] in ("COMPU_METHOD",): continue
                words = entry["original"].split()
                tw = []
                for w in words:
                    cw = w.lower().strip(".,;:()[]{}<>!?/\\-_")
                    tw.append(word_map.get(cw, w))
                result = " ".join(tw)
                if result != entry["original"]:
                    entry["translated"] = result
                    entry["status"] = "fallback"
                    wc += 1

            # Update TM
            update_tm_from_items(self.tm, self.entries)

            total = sum(1 for e in self.entries if e.get("translated"))
            self.root.after(0, lambda: self.status_var.set(
                f"翻译完成 — {total}/{len(self.entries)} 条已翻译"))
            self.root.after(0, self._update_table)
            self.root.after(0, lambda: self.btn_export.config(state="normal"))
            self.root.after(0, lambda: self.btn_translate.config(state="normal", text="▶ 重新翻译"))
            self.root.after(0, self.progress.stop)

        threading.Thread(target=worker, daemon=True).start()

    def _update_table(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        translated = 0
        for i, entry in enumerate(self.entries):
            has_t = bool(entry.get("translated"))
            if has_t: translated += 1
            status = "✓" if has_t else ""
            vals = (entry["id"], entry["type"], entry["name"][:24] if entry["name"] else "-",
                    entry["original"][:60], (entry.get("translated") or "")[:60], status)
            tag = "translated" if has_t else ""
            self.tree.insert("", "end", values=vals, tags=(tag,))
        self.tree.tag_configure("translated", background="#F0FDF4")
        self.stats_var.set(f"{translated}/{len(self.entries)} 已翻译")

    def _edit_cell(self, event):
        item = self.tree.selection()
        if not item: return
        col = self.tree.identify_column(event.x)
        if col != "#5": return  # Only allow editing translation column
        vals = list(self.tree.item(item[0], "values"))
        entry_id = int(vals[0])

        def save(new_val):
            vals[4] = new_val
            self.tree.item(item[0], values=vals)
            for entry in self.entries:
                if entry["id"] == entry_id:
                    entry["translated"] = new_val
                    break
            self._update_stats()

        # Simple inline edit dialog
        dlg = tk.Toplevel(self.root)
        dlg.title("编辑译文")
        dlg.geometry("400x120")
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(dlg, text=f"原文: {vals[3][:60]}", font=("Microsoft YaHei UI", 9),
                 wraplength=380).pack(padx=10, pady=(10, 4))
        var = tk.StringVar(value=vals[4])
        e = tk.Entry(dlg, textvariable=var, font=("Microsoft YaHei UI", 10), width=50)
        e.pack(padx=10, pady=4); e.focus()
        tk.Button(dlg, text="保存", command=lambda: (save(var.get()), dlg.destroy()),
                  bg=C["accent"], fg="white", borderwidth=0, padx=16, pady=2).pack(pady=6)

    def _update_stats(self):
        translated = sum(1 for e in self.entries if e.get("translated"))
        self.stats_var.set(f"{translated}/{len(self.entries)} 已翻译")

    def _export(self):
        if not self.entries: return
        path = filedialog.asksaveasfilename(
            title="导出翻译文件",
            defaultextension=".a2l",
            filetypes=[("A2L/KP 文件", "*.a2l;*.kp"), ("所有文件", "*.*")]
        )
        if not path: return

        self.status_var.set("导出中...")
        try:
            is_kp = path.lower().endswith('.kp')
            if is_kp and isinstance(self.original_content, bytes):
                from kp_parser import rebuild_kp
                new_data = rebuild_kp(self.original_content, self.entries)
                with open(path, 'wb') as f: f.write(new_data)
            else:
                content = rebuild_a2l(self.original_content, self.entries)
                with open(path, 'w', encoding='utf-8') as f: f.write(content)
            self.status_var.set(f"已导出 → {os.path.basename(path)}")
            messagebox.showinfo("导出成功", f"{os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
            self.status_var.set("导出失败")


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
