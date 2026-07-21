#!/usr/bin/env python3
"""
A2L 翻译器 — 完整构建脚本
输出：
  1. dist/A2L_Translator.exe              — 绿色免安装版
  2. dist/A2L_Translator_Setup_v2.3.0.exe — Windows 安装包
"""

import subprocess
import shutil
import sys
import time
from pathlib import Path

from glossary_data import BUILTIN_GLOSSARY, GERMAN_GLOSSARY

SRC = Path(__file__).parent
BUILD = SRC / "build"
DIST = SRC / "dist"
FINAL_EXE = DIST / "A2L_Translator.exe"
ICON = SRC / "WinOLS_Toolkit.ico"

_version_path = SRC / "VERSION"
__version__ = _version_path.read_text().strip() if _version_path.exists() else "0.0.0"
# 去重前导 v（VERSION 文件已包含 v 时避免 vvX.Y.Z）
if __version__.lower().startswith("v"):
    __version__ = __version__[1:]

SETUP_EXE = DIST / f"A2L_Translator_Setup_v{__version__}.exe"

VENV_PYTHON = Path("C:/Users/w/.workbuddy/binaries/python/envs/default/Scripts/python.exe")
if not VENV_PYTHON.exists():
    VENV_PYTHON = sys.executable


def step(msg: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print(f"{'=' * 60}")


def run_pyinstaller(name: str, entry: str, extra_args: list[str] | None = None) -> Path | None:
    """运行 PyInstaller，返回生成的 exe 路径"""
    args = [
        str(VENV_PYTHON), "-m", "PyInstaller",
        "--noconsole",
        "--onefile",
        "--name", name,
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "winreg",
        "--clean",
        "--noconfirm",
    ]
    if ICON.exists():
        args += ["--icon", str(ICON)]
    if extra_args:
        args += extra_args
    args.append(str(SRC / entry))

    print(f"  PyInstaller: {name} ...")
    result = subprocess.run(args, cwd=str(SRC), capture_output=True, text=True)

    if result.returncode != 0:
        print("  ✗ 打包失败！")
        tail = (result.stderr + result.stdout)[-2000:]
        print(tail)
        return None

    exe = DIST / f"{name}.exe"
    if exe.exists():
        size = exe.stat().st_size / (1024 * 1024)
        print(f"  ✓ 生成: {name}.exe ({size:.1f} MB)")
        return exe
    return None


def clean_temp() -> None:
    for d in [BUILD]:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            print(f"  ✕ 已删除 {d.name}/")
    spec = SRC / "A2L_Translator.spec"
    if spec.exists():
        spec.unlink()
    spec2 = SRC / "A2L_Translator_Setup.spec"
    if spec2.exists():
        spec2.unlink()


# ══════════════════════════════════════════════════════════
#  1. 清理
# ══════════════════════════════════════════════════════════
step("1/5 清理旧构建")
for d in [BUILD, DIST]:
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        print(f"  ✕ 已删除 {d.name}/")
DIST.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════
#  2. 构建主程序 exe
# ══════════════════════════════════════════════════════════
step("2/5 构建主程序 A2L_Translator.exe")

exe = run_pyinstaller(
    "A2L_Translator",
    "A2L_Translator_GUI.pyw",
    ["--hidden-import", "glossary_data",
     "--hidden-import", "dictionary_resources",
     "--hidden-import", "baidu_api"]
)
if exe is None:
    sys.exit(1)
exe_size = exe.stat().st_size / (1024 * 1024)

# ══════════════════════════════════════════════════════════
#  3. 清理 PyInstaller 临时文件
# ══════════════════════════════════════════════════════════
step("3/5 清理 PyInstaller 临时文件")
clean_temp()

# ══════════════════════════════════════════════════════════
#  4. 构建安装包
# ══════════════════════════════════════════════════════════
step("4/5 构建安装包 A2L_Translator_Setup.exe")

# PyInstaller 需要在项目目录下找到 A2L_Translator.exe 才能 --add-data
# 所以先复制一份到项目根目录
temp_exe = SRC / "A2L_Translator.exe"
shutil.copy2(str(FINAL_EXE), str(temp_exe))
print(f"  ✓ 复制主程序到项目根目录（供打包）")

setup = run_pyinstaller(
    "A2L_Translator_Setup",
    "setup_wizard.py",
    ["--add-data", f"A2L_Translator.exe;."]
)

# 删除临时文件
temp_exe.unlink(missing_ok=True)

# 重命名为带版本号的名称
generated = DIST / "A2L_Translator_Setup.exe"
if generated.exists() and setup:
    if SETUP_EXE.exists():
        SETUP_EXE.unlink()
    shutil.move(str(generated), str(SETUP_EXE))
    print(f"  ✓ 已重命名为 A2L_Translator_Setup_v{__version__}.exe")

# ══════════════════════════════════════════════════════════
#  5. 最终清理
# ══════════════════════════════════════════════════════════
step("5/5 最终清理")
clean_temp()
# 删除可能的重复 spec
for s in SRC.glob("*.spec"):
    s.unlink()

# ══════════════════════════════════════════════════════════
#  完成
# ══════════════════════════════════════════════════════════
step("✓ 构建完成！")

total = len(BUILTIN_GLOSSARY) + len(GERMAN_GLOSSARY)

print(f"")
print(f"  📦 绿色版: A2L_Translator.exe ({exe_size:.1f} MB)")
if SETUP_EXE.exists():
    s = SETUP_EXE.stat().st_size / (1024 * 1024)
    print(f"  💿 安装包: A2L_Translator_Setup_v{__version__}.exe ({s:.1f} MB)")
else:
    print(f"  ⚠ 安装包未生成")
print(f"  📖 术语库: {len(BUILTIN_GLOSSARY)} EN + {len(GERMAN_GLOSSARY)} DE = {total} 条")
print(f"  🔖 版本号: v{__version__}")
print(f"")
print(f"  绿色版 → 双击 A2L_Translator.exe 直接运行")
if SETUP_EXE.exists():
    print(f"  安装包 → 双击 Setup 进入安装向导")
    print(f"           安装后可卸载（开始菜单 / 控制面板）")
