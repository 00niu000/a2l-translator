#!/usr/bin/env python3
"""
A2L 翻译器 — 完整构建脚本（v2.9.5 优化版）
输出：
  1. dist/A2L_Translator.exe              — 绿色免安装版
  2. dist/A2L_Translator_Setup_vX.X.X.exe — Windows 安装包（可选）

性能优化：
  - 使用 --optimize 2 减小 exe 体积
  - 排除不必要的库减小包体
  - 智能检测 Python 环境
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

# ── 图标检测（可选，不存在也能构建）──
ICON_CANDIDATES = [
    SRC / "A2L_Translator.ico",
    SRC / "WinOLS_Toolkit.ico",
    SRC / "icon.ico",
]
ICON = None
for candidate in ICON_CANDIDATES:
    if candidate.exists():
        ICON = candidate
        break

_version_path = SRC / "VERSION"
__version__ = _version_path.read_text().strip() if _version_path.exists() else "0.0.0"
if __version__.lower().startswith("v"):
    __version__ = __version__[1:]

SETUP_EXE = DIST / f"A2L_Translator_Setup_v{__version__}.exe"

# ── 智能 Python 检测（不再硬编码路径）──
def find_python():
    """查找最优的 Python 解释器"""
    candidates = []
    # 1. 当前运行的解释器
    candidates.append(sys.executable)
    # 2. 常用虚拟环境路径
    for pattern in [
        Path.home() / ".workbuddy/binaries/python/envs/default/Scripts/python.exe",
        Path.home() / "AppData/Local/Programs/Python/Python*/python.exe",
        Path("C:/Python3*/python.exe"),
    ]:
        matches = list(Path.home().parent.glob(str(pattern)) if "*" in str(pattern) else [pattern])
        for m in matches:
            if m.exists() and m not in candidates:
                candidates.append(m)
    # 3. PATH 中的 python
    for cmd in ["python3", "python"]:
        result = subprocess.run(["where", cmd], capture_output=True, text=True)
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                p = Path(line.strip())
                if p.exists() and p not in candidates:
                    candidates.append(p)

    for py in candidates:
        try:
            r = subprocess.run([str(py), "--version"], capture_output=True, text=True, timeout=5)
            ver = r.stdout.strip() if r.returncode == 0 else ""
            if ver:
                print(f"  检测到 Python: {py} ({ver})")
                return Path(py)
        except Exception:
            continue
    return Path(sys.executable)

VENV_PYTHON = find_python()


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
        "--optimize", "2",          # 字节码优化压缩
        "--clean",
        "--noconfirm",
    ]
    # 排除不必要的大型库减小体积
    for exclude in ["matplotlib", "numpy", "pandas", "PIL", "scipy", "tensorflow", "torch"]:
        args += ["--exclude-module", exclude]

    if ICON and ICON.exists():
        args += ["--icon", str(ICON)]
    if extra_args:
        args += extra_args
    args.append(str(SRC / entry))

    print(f"  PyInstaller: {name} ...")
    print(f"  这可能需要 2-5 分钟，请耐心等待...")

    t0 = time.time()
    result = subprocess.run(args, cwd=str(SRC), capture_output=True, text=True)
    elapsed = time.time() - t0

    if result.returncode != 0:
        print(f"  ✗ 打包失败！（耗时 {elapsed:.0f}s）")
        tail = (result.stderr + result.stdout)[-2000:]
        print(tail)
        return None

    exe = DIST / f"{name}.exe"
    if exe.exists():
        size = exe.stat().st_size / (1024 * 1024)
        print(f"  ✓ 生成: {name}.exe ({size:.1f} MB) — 耗时 {elapsed:.0f}s")
        return exe
    return None


def clean_temp() -> None:
    for d in [BUILD]:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            print(f"  ✕ 已删除 {d.name}/")
    for spec in SRC.glob("*.spec"):
        spec.unlink()


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
    print("\n提示：请确保已安装 PyInstaller：pip install pyinstaller")
    sys.exit(1)
exe_size = exe.stat().st_size / (1024 * 1024)

# ══════════════════════════════════════════════════════════
#  3. 清理 PyInstaller 临时文件
# ══════════════════════════════════════════════════════════
step("3/5 清理 PyInstaller 临时文件")
clean_temp()

# ══════════════════════════════════════════════════════════
#  4. 构建安装包（如果 setup_wizard.py 存在）
# ══════════════════════════════════════════════════════════
step("4/5 构建安装包")
setup_wizard = SRC / "setup_wizard.py"

if setup_wizard.exists():
    temp_exe = SRC / "A2L_Translator.exe"
    shutil.copy2(str(FINAL_EXE), str(temp_exe))
    print(f"  ✓ 复制主程序到项目根目录（供打包）")

    setup = run_pyinstaller(
        "A2L_Translator_Setup",
        "setup_wizard.py",
        ["--add-data", f"A2L_Translator.exe;."]
    )

    temp_exe.unlink(missing_ok=True)

    generated = DIST / "A2L_Translator_Setup.exe"
    if generated.exists() and setup:
        if SETUP_EXE.exists():
            SETUP_EXE.unlink()
        shutil.move(str(generated), str(SETUP_EXE))
        print(f"  ✓ 已重命名为 A2L_Translator_Setup_v{__version__}.exe")
else:
    print(f"  ⓘ setup_wizard.py 不存在，跳过安装包构建")
    print(f"  💡 如需安装包功能，请创建 setup_wizard.py（参考 NSIS 或 Inno Setup）")

# ══════════════════════════════════════════════════════════
#  5. 最终清理
# ══════════════════════════════════════════════════════════
step("5/5 最终清理")
clean_temp()

# ══════════════════════════════════════════════════════════
#  完成
# ══════════════════════════════════════════════════════════
step("✓ 构建完成！")

total = len(BUILTIN_GLOSSARY) + len(GERMAN_GLOSSARY)

print(f"")
print(f"  绿色版: A2L_Translator.exe ({exe_size:.1f} MB)")
if SETUP_EXE.exists():
    s = SETUP_EXE.stat().st_size / (1024 * 1024)
    print(f"  安装包: A2L_Translator_Setup_v{__version__}.exe ({s:.1f} MB)")
else:
    print(f"  安装包: 未生成（缺少 setup_wizard.py）")
print(f"  术语库: {len(BUILTIN_GLOSSARY)} EN + {len(GERMAN_GLOSSARY)} DE = {total} 条")
print(f"  版本号: v{__version__}")
print(f"")
print(f"  用法：")
print(f"    绿色版 → 双击 A2L_Translator.exe 直接运行")
if SETUP_EXE.exists():
    print(f"    安装包 → 双击 Setup 进入安装向导，安装后可卸载")
