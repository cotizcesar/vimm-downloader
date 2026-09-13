#!/usr/bin/env python3
"""
flatten_roms.py — Repara descargas hechas con el layout antiguo.

Mueve todos los archivos descargados de sus subcarpetas por juego al folder
raiz de su plataforma, dejando solo una carpeta por consola:

  roms/SNES/1004/Game (USA).zip   ->   roms/SNES/Game (USA).zip
  roms/NES/834/Super Mario.zip    ->   roms/NES/Super Mario.zip

Si dos juegos coinciden en nombre, se anade el ID del juego al final:
  "Super Mario.zip" -> "Super Mario (834).zip"

Uso:
  python3 flatten_roms.py --input ./roms
  python3 flatten_roms.py --input ./roms --dry-run
"""

import argparse
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("flatten")


def safe_move(src, dest_dir, game_id):
    """Move src into dest_dir, resolving name collisions with the game ID."""
    dest = dest_dir / src.name
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        dest = dest_dir / ("%s (%s)%s" % (stem, game_id, suffix))
    # If still collides, append counter
    counter = 1
    base = dest
    while dest.exists():
        counter += 1
        dest = base.with_name("%s_%d%s" % (base.stem, counter, base.suffix))
        if counter > 100:
            raise FileExistsError("Too many collisions: %s" % dest)
    src.replace(dest)
    return dest


def flatten_platform(plat_dir, dry_run=False):
    moved = 0
    for sub in sorted(p for p in plat_dir.iterdir() if p.is_dir()):
        for f in sorted(p for p in sub.rglob("*") if p.is_file()):
            log.info("move: %s -> %s", f, plat_dir / f.name)
            if not dry_run:
                try:
                    safe_move(f, plat_dir, sub.name)
                    moved += 1
                except FileExistsError as exc:
                    log.error("%s", exc)
            else:
                moved += 1
    if not dry_run:
        # remove now-empty directories (bottom-up, recursive)
        for sub in sorted(plat_dir.rglob("*"), reverse=True):
            if sub.is_dir() and not any(sub.iterdir()):
                try:
                    sub.rmdir()
                    log.info("removed empty dir: %s", sub)
                except OSError:
                    pass
        # also check direct children that became empty
        for sub in sorted(plat_dir.iterdir(), reverse=True):
            if sub.is_dir() and not any(sub.iterdir()):
                try:
                    sub.rmdir()
                except OSError:
                    pass
    return moved


def main(argv=None):
    global log
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)-7s %(message)s")
    root = Path(args.input)
    if not root.is_dir():
        log.error("Input directory does not exist: %s", root)
        return 2

    total = 0
    for plat in sorted(p for p in root.iterdir() if p.is_dir()):
        n = flatten_platform(plat, dry_run=args.dry_run)
        log.info("Platform %s: %d file(s) moved", plat.name, n)
        total += n
    log.info("DONE. %d file(s) processed (%s)", total,
             "dry-run, nothing changed" if args.dry_run else "files moved")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Flatten old per-game folders into one folder per platform")
    p.add_argument("--input", required=True,
                   help="Root folder containing one subfolder per platform (e.g. ./roms)")
    p.add_argument("--dry-run", action="store_true",
                   help="Only show what would be done, make no changes")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
