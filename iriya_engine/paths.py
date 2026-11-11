"""Where the port looks for things.

Your own game files live in a `data/` folder next to the game (the repository
root, or the folder of IriyaPort.exe in a build):

    data/roms/          the two .nds files (any file names, subfolders allowed)
    data/upscaled/      the 高画質 art made by tools/upscale_assets.py
    data/himawari_bgm/  the alternative soundtrack made by tools/bgm/extract_himawari.py
    data/himawari/      your copy of Himawari (only read by the extraction tool)
    data/epub/          the light-novel EPUBs for the bonus chapters

Saves, settings and progress are per user, in %APPDATA%/IriyaPort (or
~/.local/share/IriyaPort). This module doesn't import pygame, so the tools can
use it too.
"""
import logging
import os
import sys
import time

APP_NAME = 'IriyaPort'


def app_root():
    """The repository root, or the folder of the executable in a frozen build."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_root():
    return os.path.join(app_root(), 'data')


def data_path(*parts):
    return os.path.join(data_root(), *parts)


def search_dirs(extra=()):
    """Folders to search for ROMs, EPUBs and the alternative soundtrack: any
    given on the command line first, then data/, data/roms/, the game folder
    and the current directory."""
    root = app_root()
    out = []
    for d in list(extra) + [data_root(), data_path('roms'), root, os.getcwd()]:
        d = os.path.abspath(d)
        if d not in out:
            out.append(d)
    return out


def user_dir():
    """Per-user folder for saves, settings and progress (created on demand)."""
    base = os.environ.get('APPDATA') or os.path.expanduser('~/.local/share')
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def upscaled_dir():
    """data/upscaled when present, else the older per-user location."""
    d = data_path('upscaled')
    if os.path.isdir(d):
        return d
    base = os.environ.get('APPDATA') or os.path.expanduser('~/.local/share')
    return os.path.join(base, APP_NAME, 'upscaled')


def write_file(path, data):
    """Replace a save file through a temporary one. A file briefly held by another program (a virus
    scanner, cloud sync, a second copy of the port) is retried; if it stays locked the save is skipped
    with a warning (the next one writes everything again). Returns True when written."""
    tmp = path + '.tmp'
    for attempt in range(5):
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, path)
            return True
        except OSError as ex:
            err = ex
            time.sleep(0.02 * (attempt + 1))
    logging.getLogger('paths').warning('could not save %s: %s', os.path.basename(path), err)
    return False
