"""Lossless conversion of Hasselblad/Imacon Flextight .fff (3F) scans to standard TIFF.

The .fff format is a big-endian TIFF whose main image (IFD0) is the uncompressed,
unprocessed scanner data (16-bit RGB). FlexColor's rendering (inversion, curves,
crop, rotation...) is only stored as settings in private tags and is NOT applied.

This script:
  * copies the IFD0 pixel data bit-for-bit into a new TIFF (Deflate/ZIP + horizontal
    predictor, both lossless),
  * sets the real scan resolution (from the FlexColor settings) instead of the
    placeholder 72 "no unit" written by FlexColor,
  * keeps the IPTC / Photoshop metadata and the Hasselblad private tags (FlexColor
    settings plist, scan info, preview thumbnail) so nothing from the source is lost,
  * re-reads the output and verifies that every pixel and every copied tag is identical
    before giving the file its final name.

The source file is opened read-only and never modified.

Usage:
    python fff_to_tiff.py INPUT.fff [-o OUTPUT.tif] [--force]
"""
import argparse
import datetime
import hashlib
import os
import plistlib
import struct
import sys
from typing import Any, BinaryIO

import numpy as np
import tifffile

TIFF_TYPES: dict[int, tuple[str, int]] = {
    1: ('B', 1), 2: ('s', 1), 3: ('H', 2), 4: ('I', 4), 5: ('II', 8), 6: ('b', 1),
    7: ('B', 1), 8: ('h', 2), 9: ('i', 4), 10: ('ii', 8), 11: ('f', 4), 12: ('d', 8),
    13: ('I', 4),
}

# Tags of the source IFD0 that are copied verbatim into the output.
COPY_TAGS: dict[int, str] = {
    33723: 'IPTC',
    34152: 'IPTC copy (Imacon)',
    34377: 'Photoshop IRB',
    46277: 'Imacon scan info',
    46279: 'Imacon version/serial',
    50457: 'FlexColor settings plist',
    50458: 'Imacon RGB thumbnail',
}

TagEntry = tuple[int, int, bytes]

# Compressed bytes tifffile reads per batch while streaming strips during verification.
# Its default batches several hundred MB at once; 8 MiB keeps the decode phase small.
VERIFY_READ_BUFFER = 8 << 20


# Helper function to guard against truncated files or out-of-bounds seeks.
def _read_exact(f: BinaryIO, size: int) -> bytes:
    data = f.read(size)
    if len(data) != size:
        raise ValueError(f'unexpected EOF: expected {size} bytes, got {len(data)}')
    return data


def read_ifd0(path: str) -> tuple[str, dict[int, TagEntry]]:
    """Return (byteorder, {tag: (type, count, raw_bytes)}) for the first IFD."""
    with open(path, 'rb') as f:
        hdr = _read_exact(f, 8)
        if hdr[:2] not in (b'MM', b'II'):
            raise ValueError('not a TIFF-based file')
        bo = '>' if hdr[:2] == b'MM' else '<'
        magic, off = struct.unpack(bo + 'HI', hdr[2:8])
        if magic != 42:
            raise ValueError(f'unsupported TIFF variant (magic {magic})')
        f.seek(off)
        n = struct.unpack(bo + 'H', _read_exact(f, 2))[0]
        entries = [_read_exact(f, 12) for _ in range(n)]
        tags: dict[int, TagEntry] = {}
        for e in entries:
            tag, typ, cnt = struct.unpack(bo + 'HHI', e[:8])
            if typ not in TIFF_TYPES:
                # Never silently drop a tag that must be preserved in the master.
                if tag in COPY_TAGS:
                    raise ValueError(f'unsupported TIFF type {typ} for preserved tag {tag}')
                continue
            size = TIFF_TYPES[typ][1] * cnt
            if size <= 4:
                raw = e[8:8 + size]
            else:
                f.seek(struct.unpack(bo + 'I', e[8:12])[0])
                raw = _read_exact(f, size)
            tags[tag] = (typ, cnt, raw)
    return bo, tags


def tag_values(bo: str, entry: TagEntry) -> list[Any] | None:
    typ, cnt, raw = entry
    if typ not in TIFF_TYPES or typ == 2:
        return None
    fmt = TIFF_TYPES[typ][0]
    return list(struct.unpack(bo + fmt * cnt, raw))


def flexcolor_settings(tags: dict[int, TagEntry]) -> dict[str, Any] | None:
    if 50457 not in tags:
        return None
    raw = tags[50457][2]
    if len(raw) < 4:
        return None
    n = struct.unpack('>I', raw[:4])[0]
    if 4 + n > len(raw):
        return None
    try:
        data = plistlib.loads(raw[4:4 + n])
        return data if isinstance(data, dict) else None
    except (plistlib.InvalidFileException, ValueError, KeyError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('input')
    ap.add_argument('-o', '--output')
    ap.add_argument('--force', action='store_true', help='overwrite an existing output file')
    ap.add_argument('--rows-per-strip', type=int, default=64)
    args = ap.parse_args()
    if args.rows_per_strip < 1:
        ap.error('--rows-per-strip must be >= 1')

    src = os.path.abspath(args.input)
    dst = os.path.abspath(args.output or os.path.splitext(src)[0] + '_negative_master.tif')

    # samefile() below raises FileNotFoundError on a missing source; fail clearly instead.
    if not os.path.isfile(src):
        sys.exit(f'input not found: {src}')
    # Prevent overwriting the source file even when --force is passed.
    if src == dst or (os.path.exists(dst) and os.path.samefile(src, dst)):
        sys.exit(f'refusing to overwrite source file: {src}')
    if os.path.exists(dst) and not args.force:
        sys.exit(f'output exists: {dst} (use --force to overwrite)')

    bo, tags = read_ifd0(src)

    def get_tag(t: int) -> list[Any]:
        if t not in tags:
            sys.exit(f'missing required TIFF tag: {t}')
        vals = tag_values(bo, tags[t])
        if not vals:
            sys.exit(f'invalid or empty TIFF tag: {t}')
        return vals

    width, height = get_tag(256)[0], get_tag(257)[0]
    bps, spp = get_tag(258), get_tag(277)[0]
    compression, photometric = get_tag(259)[0], get_tag(262)[0]
    planar = get_tag(284)[0] if 284 in tags else 1
    offsets, counts = get_tag(273), get_tag(279)
    if len(offsets) != len(counts):
        sys.exit(f'strip offsets/counts length mismatch: {len(offsets)} != {len(counts)}')
    if compression != 1 or spp != 3 or set(bps) != {16} or photometric != 2 or planar != 1:
        sys.exit(
            f'unexpected main image layout: compression={compression} spp={spp} bps={bps} '
            f'photometric={photometric} planar={planar}'
        )
    expected = width * height * spp * 2
    if sum(counts) != expected:
        sys.exit(f'strip byte counts {sum(counts)} != expected {expected}')

    # --- read source pixels (read-only) and hash the raw bytes exactly as stored ----------
    # Preallocate the exact pixel buffer and fill it in place with readinto(): appending
    # `bytes` objects would briefly hold each strip twice (a full image for a single strip).
    raw = bytearray(expected)
    with open(src, 'rb') as f, memoryview(raw) as view:
        pos = 0
        for o, c in zip(offsets, counts):
            f.seek(o)
            n_read = f.readinto(view[pos:pos + c])
            if n_read != c:
                raise ValueError(f'unexpected EOF: expected {c} bytes, got {n_read}')
            pos += c
    src_md5 = hashlib.md5(raw).hexdigest()
    pixels = np.frombuffer(raw, dtype=bo + 'u2').reshape(height, width, 3)

    # --- metadata ------------------------------------------------------------------------
    settings = flexcolor_settings(tags)
    dpi: float | int | None = None
    current_name: str | None = None
    if settings:
        sets = settings.get('ImageSettings', [])
        cur_ix = settings.get('CurrentIx', 0)
        cur = sets[cur_ix] if isinstance(sets, list) and 0 <= cur_ix < len(sets) else None
        if isinstance(cur, dict):
            dpi = cur.get('ImageDescription', {}).get('Resolution')
            current_name = cur.get('Name')
    if not dpi:
        if 282 in tags and len(tags[282][2]) == 8:
            num, den = struct.unpack(bo + 'II', tags[282][2])
            dpi = (num / den) if den != 0 else 72.0
        else:
            dpi = 72.0

    datetime_str: str | None = None
    if 33723 in tags:  # IPTC 2:55 date + 2:60 time (Imacon writes the time as 2:56)
        iptc = tags[33723][2]
        d = iptc.find(b'\x1c\x02\x37')
        t = iptc.find(b'\x1c\x02\x3c')
        if t < 0:
            t = iptc.find(b'\x1c\x02\x38')
        if d >= 0 and d + 13 <= len(iptc):
            date = iptc[d + 5:d + 13].decode('ascii', 'replace')
            time = (
                iptc[t + 5:t + 11].decode('ascii', 'replace')
                if (t >= 0 and t + 11 <= len(iptc))
                else '000000'
            )
            if len(date) == 8 and date.isdigit() and len(time) == 6 and time.isdigit():
                datetime_str = f'{date[:4]}:{date[4:6]}:{date[6:8]} {time[:2]}:{time[2:4]}:{time[4:6]}'

    now_iso = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    description = (
        f'Hasselblad Flextight X5 raw scan - UNPROCESSED NEGATIVE (no inversion, curves, crop or '
        f'rotation applied). Lossless conversion of "{os.path.basename(src)}": pixel data '
        f'bit-identical to the source. Source pixel data MD5 (16-bit big-endian RGB, as stored): '
        f'{src_md5}. Scan resolution {dpi} ppi. Effective bit depth 14 (values are multiples of 4). '
        f'FlexColor settings (current: {current_name!r}) preserved in private tag 50457. '
        f'Converted {now_iso}.'
    )

    extratags: list[tuple[int, int | str, int, Any, bool]] = [
        (271, 's', 0, 'Hasselblad', True),
        (272, 's', 0, 'Flextight X5', True),
    ]
    for code in sorted(COPY_TAGS):
        if code not in tags:
            continue
        typ, cnt, rawv = tags[code]
        if typ in (1, 7):
            extratags.append((code, typ, cnt, rawv, True))
        elif typ == 2:
            extratags.append((code, 's', 0, rawv.rstrip(b'\x00').decode('latin1'), True))
        else:
            fmt = TIFF_TYPES[typ][0]
            extratags.append((code, typ, cnt, struct.unpack(bo + fmt * cnt, rawv), True))

    # --- write to a temporary file, verify, then rename ----------------------------------
    tmp = dst + '.partial'
    try:
        tifffile.imwrite(
            tmp, pixels, byteorder=bo, photometric='rgb', planarconfig='contig',
            compression='zlib', compressionargs={'level': 6}, predictor=True,
            rowsperstrip=args.rows_per_strip,
            resolution=(dpi, dpi), resolutionunit='INCH',
            description=description, software=f'fff_to_tiff.py (tifffile {tifffile.__version__})',
            datetime=datetime_str, extratags=extratags, metadata=None,
        )

        # --- verification ----------------------------------------------------------------
        with tifffile.TiffFile(tmp) as tf:
            page = tf.pages[0]
            problems: list[str] = []
            if page.shape != pixels.shape or page.dtype is None or page.dtype.itemsize != 2:
                problems.append(f'shape/dtype mismatch {page.shape} {page.dtype}')
            else:
                # Decode and check one strip at a time instead of page.asarray(), which would
                # hold a second full-size image plus decoder buffers in memory. tifffile
                # decodes to native byte order, so each strip is converted back to the source
                # byte order `bo` before hashing, keeping the running MD5 comparable to src_md5.
                md5 = hashlib.md5()
                src_dtype = np.dtype(bo + 'u2')
                pixels_equal = True
                y = 0
                for segment, index, shape in page.segments(
                    sort=True, buffersize=VERIFY_READ_BUFFER
                ):
                    if segment is None:
                        problems.append(f'strip at row {y} could not be decoded')
                        break
                    rows = np.ascontiguousarray(segment.reshape(shape[-3:]), dtype=src_dtype)
                    n = rows.shape[0]
                    if index[-3] != y or y + n > height:
                        problems.append(f'unexpected strip layout at row {y}')
                        break
                    if pixels_equal and not np.array_equal(rows, pixels[y:y + n]):
                        pixels_equal = False
                    md5.update(rows)
                    y += n
                else:
                    if y != height:
                        problems.append(f'decoded {y} rows, expected {height}')
                if not pixels_equal:
                    problems.append('pixel data differs')
                if md5.hexdigest() != src_md5:
                    problems.append('pixel MD5 differs')
            for code in COPY_TAGS:
                if code in tags:
                    t_tag = page.tags.get(code)
                    if t_tag is None:
                        problems.append(f'tag {code} missing')
                        continue
                    val = t_tag.value
                    orig_typ, orig_cnt, orig_raw = tags[code]
                    if isinstance(val, (bytes, bytearray)):
                        got = bytes(val)
                    elif isinstance(val, str):
                        got = val.encode('latin1') + b'\x00'
                    else:
                        fmt = TIFF_TYPES[orig_typ][0]
                        vals = list(val) if isinstance(val, (tuple, list)) else [val]
                        got = struct.pack(bo + fmt * orig_cnt, *vals)
                    if got != orig_raw:
                        problems.append(f'tag {code} differs')
            info = dict(
                compression=page.compression.name,
                predictor=page.predictor.name,
                resolution=page.get_resolution(),
                byteorder=tf.byteorder,
            )
        if problems:
            raise RuntimeError('VERIFICATION FAILED, output discarded: ' + '; '.join(problems))
        os.replace(tmp, dst)
    except BaseException as exc:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        if isinstance(exc, RuntimeError):
            sys.exit(str(exc))
        raise

    s_in, s_out = os.path.getsize(src), os.path.getsize(dst)
    print(f'source : {src} ({s_in:,} bytes)')
    print(f'output : {dst} ({s_out:,} bytes, {100 * s_out / s_in:.1f}% of source)')
    print(f'image  : {width} x {height}, RGB 16-bit, {dpi} ppi, {info}')
    print(f'pixels : identical (MD5 {src_md5})')
    copied_list = ', '.join(f'{c} {COPY_TAGS[c]}' for c in sorted(COPY_TAGS) if c in tags)
    print(f'tags   : copied and verified: {copied_list}')


if __name__ == '__main__':
    main()
