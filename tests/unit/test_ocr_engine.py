import json

from kyc_tool.adapters.ocr import JsonScanOcrEngine


def test_reads_json_scan_fields():
    data = json.dumps({"fields": {"name": "Acme Ltd", "number": "12345678"}}).encode()
    assert JsonScanOcrEngine().extract(data, "registration_certificate") == {
        "name": "Acme Ltd",
        "number": "12345678",
    }


def test_tolerates_binary_upload():
    # A real PDF/PNG is not UTF-8 JSON; the engine must degrade to "no fields"
    # rather than raising UnicodeDecodeError and crashing the adapter.
    png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    assert JsonScanOcrEngine().extract(png_header, "registration_certificate") == {}


def test_tolerates_non_object_json():
    assert JsonScanOcrEngine().extract(b"[1, 2, 3]", "x") == {}
