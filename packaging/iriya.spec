# PyInstaller spec for the port: a windowed one-folder build, dist/IriyaPort/.
# Run it through packaging/build.py, which also copies your data/ folder in.
import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(ROOT, 'iriya.py')],
    pathex=[ROOT],
    binaries=collect_dynamic_libs('unicorn'),            # unicorn.dll for the PARACAP-S07 shooter
    # unicorn imports its per-architecture classes by name at run time
    hiddenimports=collect_submodules('iriya_engine') + collect_submodules('unicorn'),
    excludes=['tkinter', 'PIL', 'torch', 'matplotlib', 'scipy', 'pandas', 'IPython', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='IriyaPort',
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name='IriyaPort', upx=False)
