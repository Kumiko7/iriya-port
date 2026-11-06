# PyInstaller spec for the port: a windowed one-folder build, dist/IriyaPort/.
# Run it through packaging/build.py, which also copies your data/ folder in.
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(ROOT, 'iriya.py')],
    pathex=[ROOT],
    hiddenimports=collect_submodules('iriya_engine'),
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
