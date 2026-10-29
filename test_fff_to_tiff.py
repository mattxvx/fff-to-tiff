import plistlib
import struct
from pathlib import Path
import numpy as np
import pytest
import tifffile

import fff_to_tiff


def _make_synthetic_fff(path: Path, byteorder: str = '>', dpi: int = 3200) -> np.ndarray:
    """Crea un file .fff sintetico con tag privati Imacon/FlexColor per i test."""
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
    """Verifica conversione lossless, estrazione DPI dal plist e preservazione tag."""
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
    """Verifica che --force non permetta di sovrascrivere il file sorgente."""
    src = tmp_path / "scan.fff"
    _make_synthetic_fff(src)

    monkeypatch.setattr("sys.argv", ["fff_to_tiff.py", str(src), "-o", str(src), "--force"])
    with pytest.raises(SystemExit, match="refusing to overwrite source file"):
        fff_to_tiff.main()


def test_truncated_file_raises_value_error(tmp_path: Path) -> None:
    """Verifica che un file TIFF troncato sollevi ValueError anziché struct.error."""
    bad = tmp_path / "truncated.fff"
    bad.write_bytes(b"MM\x00*\x00\x00\x00\x10")  # Offset IFD punta oltre EOF
    with pytest.raises(ValueError, match="unexpected EOF"):
        fff_to_tiff.read_ifd0(str(bad))


def test_corrupt_flexcolor_plist_falls_back_gracefully() -> None:
    """Verifica che un tag 50457 corrotto o troncato restituisca None senza crash."""
    tags = {50457: (7, 2, b"\x00\x01")}  # Meno di 4 byte
    assert fff_to_tiff.flexcolor_settings(tags) is None


def test_little_endian_conversion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica che il calcolo MD5 e la conversione funzionino anche su TIFF little-endian ('<')."""
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
    """Verifica che il file .partial venga rimosso se la scrittura o la verifica fallisce."""
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
