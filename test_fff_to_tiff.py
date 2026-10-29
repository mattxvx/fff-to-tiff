import plistlib
import struct
from pathlib import Path
import numpy as np
import pytest
import tifffile

import fff_to_tiff


def _make_synthetic_fff(path: Path, byteorder: str = '>', dpi: int = 3200) -> np.ndarray:
    """Create a synthetic .fff file with Imacon/FlexColor private tags for testing."""
    pixels = np.arange(16 * 16 * 3, dtype=byteorder + 'u2').reshape(16, 16, 3)
    plist_bytes = plistlib.dumps({
        'CurrentIx': 0,
        'ImageSettings': [{'Name': 'TestSetting', 'ImageDescription': {'Resolution': dpi}}],
    })
    tag_50457 = struct.pack('>I', len(plist_bytes)) + plist_bytes
    iptc_payload = b'\x1c\x02\x37\x00\x0820261029\x1c\x02\x3c\x00\x06094300'

    tifffile.imwrite(
        path,
        pixels,
        byteorder=byteorder,
        photometric='rgb',
        planarconfig='contig',
        compression=None,
        extratags=[
            (33723, 7, len(iptc_payload), iptc_payload, True),
            (50457, 7, len(tag_50457), tag_50457, True),
        ],
    )
    return pixels


def test_end_to_end_lossless_conversion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify lossless conversion, DPI extraction from plist, and tag preservation."""
    src = tmp_path / "scan.fff"
    dst = tmp_path / "out.tif"
    expected_pixels = _make_synthetic_fff(src, byteorder='>', dpi=2040)

    monkeypatch.setattr("sys.argv", ["fff_to_tiff.py", str(src), "-o", str(dst)])
    fff_to_tiff.main()

    assert dst.exists()
    with tifffile.TiffFile(dst) as tf:
        assert np.array_equal(tf.pages[0].asarray(), expected_pixels)
        assert tf.pages[0].get_resolution() == (2040.0, 2040.0)
        assert 50457 in tf.pages[0].tags


def test_refuses_to_overwrite_source_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that --force refuses to overwrite the source file."""
    src = tmp_path / "scan.fff"
    _make_synthetic_fff(src)

    monkeypatch.setattr("sys.argv", ["fff_to_tiff.py", str(src), "-o", str(src), "--force"])
    with pytest.raises(SystemExit, match="refusing to overwrite source file"):
        fff_to_tiff.main()


def test_truncated_file_raises_value_error(tmp_path: Path) -> None:
    """Verify that a truncated TIFF file raises ValueError instead of struct.error."""
    bad = tmp_path / "truncated.fff"
    bad.write_bytes(b"MM\x00*\x00\x00\x00\x10")  # IFD offset points past EOF
    with pytest.raises(ValueError, match="unexpected EOF"):
        fff_to_tiff.read_ifd0(str(bad))


def test_corrupt_flexcolor_plist_falls_back_gracefully() -> None:
    """Verify that a corrupt or truncated tag 50457 returns None gracefully without crashing."""
    tags = {50457: (7, 2, b"\x00\x01")}  # Fewer than 4 bytes
    assert fff_to_tiff.flexcolor_settings(tags) is None


def test_little_endian_conversion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that MD5 calculation and conversion work on little-endian ('<') TIFF files."""
    src = tmp_path / "scan_le.fff"
    dst = tmp_path / "out_le.tif"
    expected_pixels = _make_synthetic_fff(src, byteorder='<', dpi=1600)

    monkeypatch.setattr("sys.argv", ["fff_to_tiff.py", str(src), "-o", str(dst)])
    fff_to_tiff.main()

    assert dst.exists()
    with tifffile.TiffFile(dst) as tf:
        assert tf.byteorder == '<'
        assert np.array_equal(tf.pages[0].asarray(), expected_pixels)


def test_partial_file_cleaned_up_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that the temporary .partial file is removed if writing or verification fails."""
    src = tmp_path / "scan.fff"
    dst = tmp_path / "out.tif"
    _make_synthetic_fff(src)

    def _failing_tiff_file(*args, **kwargs):
        raise RuntimeError("simulated verification failure")

    monkeypatch.setattr("tifffile.TiffFile", _failing_tiff_file)
    monkeypatch.setattr("sys.argv", ["fff_to_tiff.py", str(src), "-o", str(dst)])

    with pytest.raises(SystemExit, match="simulated verification failure"):
        fff_to_tiff.main()

    assert not dst.exists()
    assert not (tmp_path / "out.tif.partial").exists()


def test_missing_source_gives_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that a missing input exits cleanly instead of raising FileNotFoundError from samefile()."""
    dst = tmp_path / "out.tif"
    dst.write_bytes(b"existing")  # dst exists, src does not: the case that used to crash samefile()
    missing = tmp_path / "does_not_exist.fff"
    monkeypatch.setattr("sys.argv", ["fff_to_tiff.py", str(missing), "-o", str(dst), "--force"])
    with pytest.raises(SystemExit, match="input not found"):
        fff_to_tiff.main()
    assert dst.read_bytes() == b"existing"


@pytest.mark.parametrize("rows", ["0", "-1"])
def test_invalid_rows_per_strip_rejected(rows: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that --rows-per-strip < 1 is rejected by argparse (exit code 2) before any I/O."""
    monkeypatch.setattr("sys.argv", ["fff_to_tiff.py", "x.fff", "--rows-per-strip", rows])
    with pytest.raises(SystemExit) as exc:
        fff_to_tiff.main()
    assert exc.value.code == 2


def test_preserved_tag_with_unknown_type_is_not_silently_dropped(tmp_path: Path) -> None:
    """Verify that a COPY_TAGS entry with an unsupported TIFF type aborts instead of being skipped."""
    src = tmp_path / "scan.fff"
    pixels = np.zeros((4, 4, 3), dtype='>u2')
    tifffile.imwrite(src, pixels, byteorder='>', photometric='rgb', planarconfig='contig',
                     extratags=[(46279, 1, 4, b'\x01\x02\x03\x04', True)])

    # Patch the type field of the 46279 IFD entry to an undefined TIFF type (99).
    data = bytearray(src.read_bytes())
    ifd = struct.unpack('>I', data[4:8])[0]
    n = struct.unpack('>H', data[ifd:ifd + 2])[0]
    for i in range(n):
        pos = ifd + 2 + 12 * i
        if struct.unpack('>H', data[pos:pos + 2])[0] == 46279:
            data[pos + 2:pos + 4] = struct.pack('>H', 99)
            break
    else:
        pytest.fail("tag 46279 not found in synthetic file")
    src.write_bytes(bytes(data))

    with pytest.raises(ValueError, match="unsupported TIFF type 99 for preserved tag 46279"):
        fff_to_tiff.read_ifd0(str(src))


@pytest.mark.parametrize("byteorder", ['>', '<'])
def test_multi_strip_verification_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, byteorder: str
) -> None:
    """Verify strip-by-strip verification across several strips, including a partial last strip."""
    src = tmp_path / "scan.fff"
    dst = tmp_path / "out.tif"
    expected_pixels = _make_synthetic_fff(src, byteorder=byteorder)  # 16 rows -> strips 5,5,5,1

    monkeypatch.setattr("sys.argv", ["fff_to_tiff.py", str(src), "-o", str(dst), "--rows-per-strip", "5"])
    fff_to_tiff.main()

    with tifffile.TiffFile(dst) as tf:
        assert len(tf.pages[0].dataoffsets) == 4
        assert np.array_equal(tf.pages[0].asarray(), expected_pixels)


def test_verification_detects_corrupted_pixel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that a single altered pixel in the last strip fails verification and leaves no output."""
    src = tmp_path / "scan.fff"
    dst = tmp_path / "out.tif"
    _make_synthetic_fff(src)

    real_imwrite = tifffile.imwrite

    def _corrupting_imwrite(path, data, **kwargs):
        tampered = np.array(data)
        tampered[-1, -1, -1] ^= 4  # flip one bit in the final row (last, partial strip)
        return real_imwrite(path, tampered, **kwargs)

    monkeypatch.setattr("tifffile.imwrite", _corrupting_imwrite)
    monkeypatch.setattr("sys.argv", ["fff_to_tiff.py", str(src), "-o", str(dst), "--rows-per-strip", "5"])

    with pytest.raises(SystemExit, match="pixel data differs; pixel MD5 differs"):
        fff_to_tiff.main()

    assert not dst.exists()
    assert not (tmp_path / "out.tif.partial").exists()
