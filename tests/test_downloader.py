import base64
import json
import tempfile
from pathlib import Path

import vimm_downloader as vd


def test_sanitize_filename():
    assert vd.sanitize_filename('Mortal Kombat (USA).zip') == 'Mortal Kombat (USA).zip'
    assert vd.sanitize_filename('a/b:c*d?e<f>g|h') == 'a_b_c_d_e_f_g_h'
    assert vd.sanitize_filename('   ') == 'file'
    assert vd.sanitize_filename('...test...') == 'test'
    assert vd.sanitize_filename('file\x00name') == 'file_name'


def test_parse_content_disposition():
    assert vd.parse_content_disposition('attachment; filename="Game (USA).zip"') == 'Game (USA).zip'
    assert vd.parse_content_disposition('attachment; filename=game.zip') == 'game.zip'
    assert vd.parse_content_disposition(None) is None
    assert vd.parse_content_disposition('') is None
    # UTF-8 encoded
    val = "attachment; filename*=UTF-8''Game%20USA.zip"
    assert vd.parse_content_disposition(val) == 'Game USA.zip'


def test_decode_title():
    title = "Super Mario World (USA)"
    entry = {"GoodTitle": base64.b64encode(title.encode()).decode()}
    dl = vd.VimmDownloader(output=tempfile.mkdtemp(), throttle=0, retries=1)
    assert dl.decode_title(entry) == title


def test_media_has_content():
    assert vd.VimmDownloader._media_has_content({"Zipped": "1"}) is True
    assert vd.VimmDownloader._media_has_content({"Zipped": "0", "AltZipped": "1"}) is True
    assert vd.VimmDownloader._media_has_content({"Zipped": "0"}) is False
    assert vd.VimmDownloader._media_has_content({}) is False


def test_pick_media_default():
    dl = vd.VimmDownloader(output=tempfile.mkdtemp(), throttle=0, retries=1, all_versions=False)
    media = [{"ID": "1", "Zipped": "0"}, {"ID": "2", "Zipped": "1"}, {"ID": "3", "Zipped": "1"}]
    picks = dl.pick_media(123, media)
    assert len(picks) == 1
    assert picks[0]["ID"] == "2"


def test_pick_media_all_versions():
    dl = vd.VimmDownloader(output=tempfile.mkdtemp(), throttle=0, retries=1, all_versions=True)
    media = [{"ID": "1", "Zipped": "0"}, {"ID": "2", "Zipped": "1"}, {"ID": "3", "Zipped": "1"}]
    picks = dl.pick_media(123, media)
    assert len(picks) == 2


def test_looks_valid(tmp_path):
    valid = tmp_path / "valid.zip"
    valid.write_bytes(b"PK\x03\x04 some zip content")
    assert vd.VimmDownloader._looks_valid(valid) is True

    invalid = tmp_path / "invalid.html"
    invalid.write_bytes(b"<!doctype html><html><body>blocked</body></html>")
    assert vd.VimmDownloader._looks_valid(invalid) is False

    invalid2 = tmp_path / "invalid2.html"
    invalid2.write_bytes(b"<html><head><title>403</title></head>")
    assert vd.VimmDownloader._looks_valid(invalid2) is False


def test_game_id_regex():
    html = '<a href="/vault/12345">Game</a> <a href="/vault/67890/">Other</a> <a href="/vault/999999">bad</a>'
    ids = [int(i) for i in vd.GAME_ID_RE.findall(html)]
    assert 12345 in ids
    assert 67890 in ids
    assert 999999 in ids  # regex finds it, filtering is done later


def test_state_persistence(tmp_path):
    state_file = tmp_path / "state.json"
    dl = vd.VimmDownloader(output=str(tmp_path / "roms"), throttle=0, retries=1, state_path=str(state_file))
    dl.state["1"] = {"2": {"file": "test.zip", "size": 123}}
    dl._save_state()
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["1"]["2"]["file"] == "test.zip"

    dl2 = vd.VimmDownloader(output=str(tmp_path / "roms"), throttle=0, retries=1, state_path=str(state_file))
    assert dl2.state["1"]["2"]["file"] == "test.zip"


def test_parse_args_dry_run():
    args = vd.parse_args(["--systems", "SNES", "--dry-run"])
    assert args.dry_run is True
    assert args.systems == "SNES"

    args2 = vd.parse_args(["--systems", "SNES,NES", "--letter", "A,C,#"])
    assert args2.letter == "A,C,#"
