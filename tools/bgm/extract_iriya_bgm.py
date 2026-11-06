"""Extract all Nintendo DS BGM tracks from Iriya no Sora ROMs into data/iriya_bgm/.
Uses GameData SDAT STRM extraction, writes WAV, and encodes to OGG Vorbis with ffmpeg.
"""
import io
import json
import os
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
PORT_DIR = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, PORT_DIR)

from iriya_engine.rom import find_roms
from iriya_engine.assets import GameData
from iriya_engine import paths

GLOSS = {
    'NITIJO': '日常', 'IRIYA': '伊里野', 'AKIHO': '晶穂', 'MAYUMI': '椎名真由美', 'SUIZENJI': '水前寺',
    'YOKAN': '予感', 'GENSOU': '幻想', 'YUMEUTSUTSU': '夢現', 'SEIJYAKU': '静寂', 'KEIHOU': '警報',
    'ROUKA': '廊下', 'ENBAN': '円盤', 'AISETSU': '哀切', 'TOUSOU': '逃走', 'GAKUSAI': '学祭',
    'INBOU': '陰謀', 'KONWAKU': '困惑', 'APPAKU': '圧迫', 'LEMON': 'レモン', 'SENNYU': '潜入',
    'KENSOU': '喧騒', 'DATURYOKU': '脱力', 'SHOUSOU': '焦燥', 'KINPAKU': '緊迫', 'CITY': '街',
    'FUAN': '不安', 'KANDOU': '感動', 'MOUSOU': '妄想', 'MAIMU': 'マイム・マイム', 'SINSEKAI': '新世界',
    'KESSEN': '決戦', 'IKARIBAKUHATU': '怒り爆発', 'NAZO': '謎', 'BONYOU': '凡庸', 'HOUKAI': '崩壊',
    'HOUKAIMAE': '崩壊前', 'KINKYUU': '緊急', 'OMOTAIKEI': '重たい系', 'SASUPENSUKEI': 'サスペンス系',
    'TYUUKA': '中華', 'YUUSEN': '優先',
}


def gloss(name):
    parts = name[4:].split('_')
    g = GLOSS.get(parts[0], '')
    extra = {'ARR': 'アレンジ', 'ARR2': 'アレンジ2', 'B': 'B', 'SHORT': '短', 'RE': 'リテイク'}
    tail = ' '.join(extra.get(p, p) for p in parts[1:])
    return (g + (' ' + tail if tail else '')).strip()


def main():
    OUT_DIR = paths.data_path('iriya_bgm')
    os.makedirs(OUT_DIR, exist_ok=True)

    roms = find_roms(paths.search_dirs())
    games = {gid: GameData(gid, roms[gid]) for gid in ('iriya1', 'iriya2') if gid in roms}

    ds_tracks = {}
    for gid, g in games.items():
        for sid, n in enumerate(g.sdat.names):
            name = str(n)
            if name.startswith('BGM'):
                ds_tracks.setdefault(name, {})[gid] = sid

    print(f"Found {len(ds_tracks)} unique BGM tracks across ROMs.")

    metadata = {}
    tmp_wav = os.path.join(OUT_DIR, '_tmp.wav')

    for name in sorted(ds_tracks.keys()):
        g_map = ds_tracks[name]
        # Prefer iriya2 if available, else iriya1
        gid = 'iriya2' if 'iriya2' in g_map else 'iriya1'
        sid = g_map[gid]
        g = games[gid]

        pcm_info = g.pcm(sid)
        if pcm_info is None:
            print(f"Warning: could not get PCM for {name} ({gid}:{sid})")
            continue

        pcm_data, loop_sample, st_name = pcm_info
        ogg_filename = f"{name}.ogg"
        ogg_path = os.path.join(OUT_DIR, ogg_filename)

        with wave.open(tmp_wav, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(pcm_data.tobytes())

        cmd = ['ffmpeg', '-y', '-i', tmp_wav, '-c:a', 'libvorbis', '-q:a', '5', ogg_path]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        duration = len(pcm_data) / 44100.0
        metadata[name] = {
            'file': ogg_filename,
            'rate': 44100,
            'samples': len(pcm_data),
            'duration_sec': round(duration, 2),
            'loop_sample': loop_sample,
            'games': g_map,
            'gloss': gloss(name)
        }
        print(f"Extracted {name} -> {ogg_filename} ({duration:.1f}s, loop={loop_sample})")

    if os.path.exists(tmp_wav):
        os.remove(tmp_wav)

    meta_path = os.path.join(OUT_DIR, 'tracks.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=1, ensure_ascii=False)

    print(f"\nAll {len(metadata)} tracks extracted successfully to {OUT_DIR}")


if __name__ == '__main__':
    main()
