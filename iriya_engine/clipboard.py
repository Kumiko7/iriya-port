"""Copy text to the system clipboard (the テキストフック option, for texthookers).

On Windows this uses the Win32 clipboard directly: the packaged build has two
SDL2.dll copies, and SDL_SetClipboardText from the one pygame doesn't use fails.
Elsewhere it uses SDL's SDL_SetClipboardText (UTF-8) from pygame's SDL library."""
import ctypes
import ctypes.util
import glob
import logging
import os
import sys

log = logging.getLogger(__name__)

_set = None
_tried = False


def _load_sdl():
    import pygame
    here = os.path.dirname(pygame.__file__)
    cands = ['SDL2.dll', 'SDL2', 'libSDL2-2.0.so.0', 'libSDL2-2.0.0.dylib']
    for pat in ('SDL2*.dll', 'libSDL2*.so*', 'libSDL2*.dylib'):
        for d in (here, os.path.join(here, '.dylibs'), os.path.join(os.path.dirname(here), 'pygame.libs')):
            cands += sorted(glob.glob(os.path.join(d, pat)))
    found = ctypes.util.find_library('SDL2')
    if found:
        cands.append(found)
    for name in cands:
        try:
            lib = ctypes.CDLL(name)
            fn = lib.SDL_SetClipboardText
        except (OSError, AttributeError):
            continue
        fn.argtypes = [ctypes.c_char_p]
        fn.restype = ctypes.c_int
        return lambda text: fn(text.encode('utf-8')) == 0
    return None


def _win32(text):
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalAlloc.restype = wintypes.HGLOBAL
    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    if not u.OpenClipboard(None):
        return False
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(0x0002, size)          # GMEM_MOVEABLE
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, size)
        k.GlobalUnlock(h)
        return bool(u.SetClipboardData(13, h))  # CF_UNICODETEXT
    finally:
        u.CloseClipboard()


def copy(text):
    """Put text on the clipboard; returns True if it worked."""
    global _set, _tried
    if not _tried:
        _tried = True
        if sys.platform == 'win32':
            _set = _win32
        else:
            try:
                _set = _load_sdl()
            except Exception:
                _set = None
        if _set is None:
            log.warning('no clipboard available for the text hook')
    if _set is None:
        return False
    try:
        if _set(text):
            return True
        log.warning('clipboard: copy failed')
        return False
    except Exception as e:
        log.warning('clipboard: %s', e)
        return False
