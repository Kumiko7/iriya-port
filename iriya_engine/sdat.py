"""Nintendo SDAT reader limited to STRM (streamed IMA-ADPCM / PCM) tracks."""
import struct
import numpy as np

_STEPS = np.array([
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767], dtype=np.int32)
_INDEX = np.array([-1, -1, -1, -1, 2, 4, 6, 8], dtype=np.int32)


def _adpcm_blocks(blocks, nsamp):
    """Decode a (nblocks, blocklen) uint8 array of IMA-ADPCM blocks in parallel.

    Each block starts with a 4-byte header (s16 initial sample, u8 step index).
    Returns int16 array (nblocks, nsamp).
    """
    hist = blocks[:, 0].astype(np.int32) | (blocks[:, 1].astype(np.int32) << 8)
    hist = np.where(hist >= 0x8000, hist - 0x10000, hist)
    idx = np.clip(blocks[:, 2].astype(np.int32), 0, 88)
    data = blocks[:, 4:]
    nib = np.empty((blocks.shape[0], data.shape[1] * 2), dtype=np.int32)
    nib[:, 0::2] = data & 15
    nib[:, 1::2] = data >> 4
    nib = nib[:, :nsamp]
    out = np.empty((blocks.shape[0], nib.shape[1]), dtype=np.int16)
    for i in range(nib.shape[1]):
        n = nib[:, i]
        step = _STEPS[idx]
        diff = step >> 3
        diff = diff + np.where(n & 1, step >> 2, 0)
        diff = diff + np.where(n & 2, step >> 1, 0)
        diff = diff + np.where(n & 4, step, 0)
        hist = np.where(n & 8, np.maximum(hist - diff, -32767), np.minimum(hist + diff, 32767))
        idx = np.clip(idx + _INDEX[n & 7], 0, 88)
        out[:, i] = hist
    return out


class Strm:
    def __init__(self, name, data):
        self.name = name
        h = 0x10  # HEAD block
        (self.enc, self.loop, self.channels, _, self.rate, _time, self.loop_start,
         self.nsamples, self.data_off, self.nblocks, self.block_len, self.block_samples,
         self.last_len, self.last_samples) = struct.unpack_from('<BBBBHHIIIIIIII', data, h + 8)
        self.raw = data

    def decode(self):
        """Return int16 numpy array of shape (nsamples, channels)."""
        d = self.raw
        ch = self.channels
        base = self.data_off
        bl, nb = self.block_len, self.nblocks
        chans = []
        # Blocks are interleaved: block0 ch0, block0 ch1, block1 ch0, ...
        for c in range(ch):
            full = []
            if nb > 1:
                arr = np.frombuffer(d, dtype=np.uint8, count=(nb - 1) * bl * ch, offset=base)
                arr = arr.reshape(nb - 1, ch, bl)[:, c, :]
                full.append(arr)
            last_off = base + (nb - 1) * bl * ch + c * self.last_len
            last = np.frombuffer(d, dtype=np.uint8, count=self.last_len, offset=last_off)
            if self.enc == 2:
                pcm = []
                if full:
                    pcm.append(_adpcm_blocks(full[0], self.block_samples).reshape(-1))
                pcm.append(_adpcm_blocks(last[None, :], self.last_samples).reshape(-1))
                chans.append(np.concatenate(pcm))
            elif self.enc == 1:
                parts = [f.view('<i2').reshape(-1) for f in full] + [last.view('<i2')]
                chans.append(np.concatenate(parts))
            else:
                parts = [f.view(np.int8).reshape(-1) for f in full] + [last.view(np.int8)]
                chans.append((np.concatenate(parts).astype(np.int16) << 8))
        n = min(len(c) for c in chans)
        return np.stack([c[:n] for c in chans], axis=1)[:self.nsamples]


class SDAT:
    def __init__(self, data):
        self.data = data
        symb, _, info, _, fat, _, _, _ = struct.unpack_from('<8I', data, 0x10)
        # STRM symbols (record 7)
        r = struct.unpack_from('<8I', data, symb + 8)[7]
        cnt = struct.unpack_from('<I', data, symb + r)[0]
        self.names = []
        for i in range(cnt):
            o = struct.unpack_from('<I', data, symb + r + 4 + 4 * i)[0]
            self.names.append(data[symb + o:data.index(b'\0', symb + o)].decode() if o else f'STRM_{i}')
        ir = struct.unpack_from('<8I', data, info + 8)[7]
        icnt = struct.unpack_from('<I', data, info + ir)[0]
        self.file_ids = []
        self.volumes = []
        for i in range(icnt):
            o = struct.unpack_from('<I', data, info + ir + 4 + 4 * i)[0]
            if o:
                fid, _, vol, pri, ply = struct.unpack_from('<HHBBB', data, info + o)
            else:
                fid, vol = None, 127
            self.file_ids.append(fid)
            self.volumes.append(vol)
        self.fat = []
        n = struct.unpack_from('<I', data, fat + 8)[0]
        for i in range(n):
            self.fat.append(struct.unpack_from('<II', data, fat + 12 + i * 16))

    def __len__(self):
        return len(self.file_ids)

    def strm(self, i):
        fid = self.file_ids[i]
        o, s = self.fat[fid]
        return Strm(self.names[i], self.data[o:o + s])
