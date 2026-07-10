import hashlib

from dbarchive.utils import (
    human_bytes,
    render_table,
    sha256_file,
    tokenize,
    valid_identifier,
)


def test_human_bytes():
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(500) == "500 B"


def test_tokenize_camel_and_snake():
    assert tokenize("CreatedDate") == ["created", "date"]
    assert tokenize("order_ship_date") == ["order", "ship", "date"]
    assert tokenize("dob") == ["dob"]
    assert tokenize("col2") == ["col", "2"]


def test_valid_identifier():
    assert valid_identifier("a_1")
    assert not valid_identifier("1a")
    assert not valid_identifier("a;b")
    assert not valid_identifier("a b")


def test_sha256_file(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"hello world")
    assert sha256_file(p) == hashlib.sha256(b"hello world").hexdigest()


def test_render_table_contains_headers():
    out = render_table(["A", "B"], [["1", "2"]], color=False)
    assert "A" in out and "B" in out and "1" in out


def test_render_table_empty():
    assert "no data" in render_table(["A"], [], color=False)
