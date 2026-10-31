# -*- mode: python ; coding: utf-8 -*-
# Сборка папки dist\DryerCalc\ для установщика (быстрый запуск, без распаковки):
#   pyinstaller --noconfirm DryerCalc_dir.spec   затем   makensis installer.nsi
import os

# v0.7: расчёты на numpy, графики на pyqtgraph. Не нужны: OpenGL (pyqtgraph рисует 2D через QPainter), сетевые и
# прочие модули Qt, matplotlib/scipy, части pyqtgraph для 3D, Jupyter, консоли, примеров.
EXCLUDES = [
    "matplotlib", "scipy", "pandas", "tkinter", "unittest", "ssl", "_ssl", "_hashlib",
    "pyqtgraph.examples", "pyqtgraph.opengl", "pyqtgraph.jupyter", "pyqtgraph.flowchart", "pyqtgraph.console",
    "pyqtgraph.dockarea", "OpenGL", "IPython", "jupyter_rfb", "cupy", "numba", "h5py", "PyQt5", "PyQt6", "PySide2",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtDBus", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtConcurrent", "PySide6.QtXml", "PySide6.QtNetwork", "PySide6.QtPrintSupport",
]
# лишние библиотеки: программный OpenGL (20 МБ), форматы картинок, которые программе не нужны (иконка — PNG, он встроен)
DROP_BINARIES = {
    "opengl32sw.dll", "d3dcompiler_47.dll", "qdirect2d.dll", "qwebp.dll", "qtiff.dll", "qjpeg.dll", "qgif.dll",
    "qico.dll", "qicns.dll", "qsvg.dll", "qsvgicon.dll", "qtga.dll", "qwbmp.dll", "qpdf.dll",
    "qnetworklistmanager.dll", "qtuiotouchplugin.dll", "qschannelbackend.dll", "qcertonlybackend.dll",
}

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[("examples/journal_sample_oct2026.csv", "examples"), ("assets/dryer.png", "assets"), ("README.md", "."), ("docs/manual_ru.txt", "docs"), ("docs/rules_ru.txt", "docs")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
a.binaries = [b for b in a.binaries if os.path.basename(b[0]).lower() not in DROP_BINARIES]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DryerCalc",
    icon="assets/dryer.ico",
    version="version_info.txt",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="DryerCalc")
