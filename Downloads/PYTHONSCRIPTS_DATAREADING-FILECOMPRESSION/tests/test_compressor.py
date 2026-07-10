from dbarchive.compressor import compress_csv, decompress
from dbarchive.utils import sha256_file


def test_compress_and_decompress_roundtrip(tmp_path):
    csv = tmp_path / "data.csv"
    original = ("col_a,col_b\n" + "value,other\n" * 500).encode("utf-8")
    csv.write_bytes(original)

    gz = tmp_path / "data.csv.gz"
    result = compress_csv(csv, gz)

    assert result.gz_bytes < result.csv_bytes
    assert result.ratio > 0
    assert result.checksum == sha256_file(gz)

    out = tmp_path / "restored.csv"
    decompress(gz, out)
    assert out.read_bytes() == original


def test_empty_csv_ratio_zero(tmp_path):
    csv = tmp_path / "empty.csv"
    csv.write_bytes(b"")
    result = compress_csv(csv, tmp_path / "empty.csv.gz")
    assert result.ratio == 0.0
