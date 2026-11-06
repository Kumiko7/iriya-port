"""Build a standalone copy of the port in dist/IriyaPort/.

    python packaging/build.py            # build + copy your data/ folder in
    python packaging/build.py --no-data  # the program only
    python packaging/build.py --zip      # also write dist/IriyaPort.zip

The program is frozen with PyInstaller (Python, pygame and numpy included, so
the target PC needs nothing installed). Then the parts of data/ the game reads
at runtime are copied next to IriyaPort.exe: roms/, upscaled/, himawari_bgm/
and epub/. Your Himawari copy (data/himawari/) is only needed by the
extraction tool and is left out.

The result contains your ROMs and other game files: it is for your own PCs,
don't share it.
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(ROOT, 'dist')
WORK = os.path.join(ROOT, 'build')
APP = os.path.join(DIST, 'IriyaPort')
RUNTIME_DATA = ('roms', 'upscaled', 'himawari_bgm', 'epub')
SKIP_FILES = ('usage.json',)            # the BGM matcher's cache


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--no-data', action='store_true', help="don't copy data/ into the build")
    ap.add_argument('--zip', action='store_true', help='also write dist/IriyaPort.zip')
    args = ap.parse_args()

    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', DIST, '--workpath', WORK, os.path.join(HERE, 'iriya.spec')],
                   check=True, cwd=ROOT)

    if not args.no_data:
        for sub in RUNTIME_DATA:
            src = os.path.join(ROOT, 'data', sub)
            if not os.path.isdir(src):
                print('skipping missing', os.path.relpath(src, ROOT))
                continue
            dst = os.path.join(APP, 'data', sub)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SKIP_FILES))
            print('copied', os.path.relpath(src, ROOT))

    if args.zip:
        out = shutil.make_archive(APP, 'zip', DIST, 'IriyaPort')
        print('wrote', os.path.relpath(out, ROOT))
    print('done:', os.path.relpath(os.path.join(APP, 'IriyaPort.exe'), ROOT))


if __name__ == '__main__':
    main()
