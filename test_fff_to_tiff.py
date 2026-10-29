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
