import tempfile
from pathlib import Path

import flatten_roms as fr


def test_safe_move_no_collision(tmp_path):
    plat = tmp_path / "SNES"
    plat.mkdir()
    src = tmp_path / "src.zip"
    src.write_text("data")
    # simulate game_id folder case: src is from sub/1234
    dest = fr.safe_move(src, plat, "1234")
    assert dest == plat / "src.zip"
    assert dest.exists()
    assert not src.exists()


def test_safe_move_collision(tmp_path):
    plat = tmp_path / "NES"
    plat.mkdir()
    existing = plat / "game.zip"
    existing.write_text("old")
    src = tmp_path / "game.zip"
    src.write_text("new")
    dest = fr.safe_move(src, plat, "999")
    assert dest == plat / "game (999).zip"
    assert dest.exists()
    assert existing.exists()


def test_flatten_platform(tmp_path):
    roms = tmp_path / "roms"
    snes = roms / "SNES"
    snes.mkdir(parents=True)
    # old layout: SNES/1004/Game.zip and SNES/1005/Game.zip (same name)
    g1 = snes / "1004"
    g1.mkdir()
    (g1 / "Super Mario.zip").write_text("a")
    g2 = snes / "1005"
    g2.mkdir()
    (g2 / "Super Mario.zip").write_text("b")

    moved = fr.flatten_platform(snes, dry_run=False)
    assert moved == 2
    assert (snes / "Super Mario.zip").exists()
    assert (snes / "Super Mario (1005).zip").exists()
    # empty dirs removed
    assert not g1.exists()
    assert not g2.exists()


def test_flatten_dry_run(tmp_path):
    roms = tmp_path / "roms"
    plat = roms / "GB"
    plat.mkdir(parents=True)
    sub = plat / "123"
    sub.mkdir()
    (sub / "game.zip").write_text("x")
    moved = fr.flatten_platform(plat, dry_run=True)
    assert moved == 1
    # dry-run doesn't move
    assert (sub / "game.zip").exists()
    assert not (plat / "game.zip").exists()
