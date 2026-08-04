#!/usr/bin/env python3
"""
Vimm's Lair Vault bulk downloader.

Downloads every game of one or more consoles from https://vimm.net/vault.

Features:
  * Retries with exponential backoff on failures / HTTP errors
  * Configurable throttle (delay) between every HTTP request
  * Resumable downloads (Range) and skip-already-downloaded files
  * Persistent state file so interrupted runs can be resumed safely
  * Works for every system in The Vault (SNES, NES, N64, GB, PS1, ...)

Usage examples:
  python3 vimm_downloader.py --systems SNES --output ./roms
  python3 vimm_downloader.py --systems SNES,NES,N64 --output ./roms --throttle 2
  python3 vimm_downloader.py --systems SNES --letter A --output ./roms --all-versions
"""

import argparse
import base64
import json
import logging
import re
import sys
import time
from pathlib import Path

import requests

BASE_URL = "https://vimm.net"
DL_HOST = "https://dl3.vimm.net"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

GAME_ID_RE = re.compile(r'href\s*=\s*["\']?/vault/(\d+)["\'\s>]')
MEDIA_RE = re.compile(r"let media=\[(.*?)\];", re.S)

LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

log = logging.getLogger("vimm")


class DownloadError(Exception):
    pass


def sanitize_filename(name):
    """Make a filename safe for any filesystem."""
    name = name.replace('"', "").replace("'", "").replace("`", "")
    name = re.sub(r"[\\/:*?<>|\x00-\x1f]+", "_", name).strip()
    return name.strip(". ") or "file"


def parse_content_disposition(value):
    """Return the filename from a Content-Disposition header or None."""
    if not value:
        return None
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value)
    if not m:
        return None
    name = m.group(1).strip()
    if name.startswith("%") and "UTF-8" in value:
        try:
            import urllib.parse
            name = urllib.parse.unquote(name)
        except Exception:
            pass
    return name


class VimmDownloader:
    def __init__(self, output, throttle=1.5, retries=5, backoff=3.0, timeout=30,
                 all_versions=False, skip_existing=True, resume=True,
                 state_path=None, letters=None):
        self.output = Path(output)
        self.throttle = max(0.0, float(throttle))
        self.retries = max(1, int(retries))
        self.backoff = max(0.1, float(backoff))
        self.timeout = int(timeout)
        self.all_versions = bool(all_versions)
        self.skip_existing = bool(skip_existing)
        self.resume = bool(resume)
        self.letters = letters
        self.state_path = Path(state_path) if state_path else Path.cwd() / ".vimm_downloader_state.json"
        self.state = {}
        self.stats = {"listed": 0, "downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._load_state()

    # ------------------------------------------------------------------ state
    def _load_state(self):
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text())
                log.info("Loaded state file with %d recorded downloads", len(self.state))
            except (ValueError, OSError) as exc:
                log.warning("Could not read state file %s: %s", self.state_path, exc)

    def _save_state(self):
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True))
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("Could not write state file %s: %s", self.state_path, exc)

    # ---------------------------------------------------------------- helpers
    def _throttle(self):
        if self.throttle > 0:
            time.sleep(self.throttle)

    def _request(self, url, method="GET", referer=None, retries=None, **kw):
        retries = retries if retries is not None else self.retries
        last_exc = None
        for attempt in range(1, retries + 1):
            self._throttle()
            try:
                req_headers = dict(self.session.headers)
                if referer:
                    req_headers["Referer"] = referer
                extra_headers = kw.pop("headers", None)
                if extra_headers:
                    req_headers.update(extra_headers)
                resp = self.session.request(method, url, headers=req_headers,
                                            timeout=self.timeout, **kw)
                if resp.status_code in (200, 206):
                    return resp
                if resp.status_code == 400:
                    log.error("HTTP 400 on %s (possible bot/ad-blocker check). Aborting this URL.", url)
                    raise DownloadError("HTTP 400 blocked by server: " + url)
                last_exc = DownloadError("HTTP %d for %s" % (resp.status_code, url))
                resp.close()
            except (requests.RequestException, DownloadError) as exc:
                last_exc = exc
                log.warning("Attempt %d/%d failed for %s: %s",
                            attempt, retries, url, exc)
            if attempt < retries:
                wait = self.backoff * (2 ** (attempt - 1))
                log.info("Retrying %s in %.1fs", url, wait)
                time.sleep(wait)
        raise last_exc

    # -------------------------------------------------------------- discovery
    def get_letters(self, system):
        """Return the list of letter sections available for a system."""
        if self.letters:
            chosen = set(self.letters)
            return [c for c in ["#"] + LETTERS if c in chosen]
        url = "%s/vault/%s" % (BASE_URL, system)
        html = self._request(url).text
        found = set()
        if re.search(r"section=number", html):
            found.add("#")
        pat = re.compile(r'href="/vault/' + re.escape(system) + r'/([A-Z])["\s]')
        found.update(m.group(1) for m in pat.finditer(html))
        return [c for c in ["#"] + LETTERS if c in found]

    def list_game_ids(self, system, letter):
        """Return the sorted unique game IDs in a letter section."""
        if letter == "#":
            url = "%s/vault/?p=list&system=%s&section=number" % (BASE_URL, system)
        else:
            url = "%s/vault/%s/%s" % (BASE_URL, system, letter)
        html = self._request(url, referer="%s/vault/%s" % (BASE_URL, system)).text
        ids = sorted({int(i) for i in GAME_ID_RE.findall(html) if int(i) != 999999})
        return ids

    def get_media(self, game_id):
        """Fetch a game page and parse its downloadable media entries."""
        url = "%s/vault/%d" % (BASE_URL, game_id)
        html = self._request(url, referer="%s/vault" % BASE_URL).text
        m = MEDIA_RE.search(html)
        if not m:
            return []
        try:
            return json.loads("[" + m.group(1) + "]")
        except ValueError:
            return []

    def pick_media(self, game_id, media):
        """Select which media entries to download for a game."""
        if not media:
            return []
        if self.all_versions:
            return [e for e in media if self._media_has_content(e)]
        # default: the entry pre-selected by the site (first with content)
        for entry in media:
            if self._media_has_content(entry):
                return [entry]
        return []

    @staticmethod
    def _media_has_content(entry):
        try:
            return int(entry.get("Zipped", 0)) > 0 or int(entry.get("AltZipped", 0)) > 0
        except (TypeError, ValueError):
            return False

    def decode_title(self, entry):
        try:
            return base64.b64decode(entry["GoodTitle"]).decode("utf-8", "replace")
        except Exception:
            return entry.get("GoodTitle", "unknown")

    # --------------------------------------------------------------- download
    def _recorded_filename(self, game_id, media_id):
        rec = self.state.get(str(game_id), {}).get(str(media_id))
        return sanitize_filename(rec["file"]) if rec and rec.get("file") else None

    def _target_filename(self, system, game_id, entry):
        """Resolve the on-disk filename for a media entry (HEAD first)."""
        recorded = self._recorded_filename(game_id, entry["ID"])
        if recorded:
            return recorded
        title = sanitize_filename(self.decode_title(entry))
        head_url = "%s/?mediaId=%s" % (DL_HOST, entry["ID"])
        try:
            resp = self._request(head_url, method="HEAD",
                                 referer="%s/vault/%d" % (BASE_URL, game_id))
            fname = parse_content_disposition(resp.headers.get("Content-Disposition"))
            resp.close()
            if fname:
                return sanitize_filename(fname)
        except Exception:
            pass
        return title + ".zip"

    def download_media(self, system, game_id, entry):
        media_id = entry["ID"]
        url = "%s/?mediaId=%s" % (DL_HOST, media_id)
        title = sanitize_filename(self.decode_title(entry))
        game_dir = self.output / system / str(game_id)
        game_dir.mkdir(parents=True, exist_ok=True)
        filename = self._target_filename(system, game_id, entry)
        path = game_dir / filename

        # skip if this media is already recorded and the file exists
        recorded = self._recorded_filename(game_id, media_id)
        if self.skip_existing and recorded == filename and path.exists() and path.stat().st_size > 0:
            log.info("[%d] skipping (exists): %s", game_id, path.name)
            return True, "skipped"

        # avoid name collision with a file recorded for a different version
        other = None
        for mid, rec in self.state.get(str(game_id), {}).items():
            if mid != str(media_id) and rec.get("file") == filename:
                other = mid
                break
        if other is not None:
            stem = path.stem
            filename = "%s (%s)%s" % (stem, media_id, path.suffix)
            path = game_dir / filename
            recorded = self._recorded_filename(game_id, media_id)
            if self.skip_existing and recorded == filename and path.exists() and path.stat().st_size > 0:
                log.info("[%d] skipping (exists): %s", game_id, path.name)
                return True, "skipped"

        existing = path.stat().st_size if (self.resume and path.exists()) else 0
        headers = {"Range": "bytes=%d-" % existing} if existing > 0 else None
        log.info("[%d] downloading %s (%s)", game_id, title, entry.get("ZippedText", "?"))

        resp = None
        try:
            resp = self._request(url, method="GET", referer="%s/vault/%d" % (BASE_URL, game_id),
                                 headers=headers, stream=True, retries=self.retries)
            if resp.status_code == 206 and existing:
                mode = "ab"
            else:
                mode = "wb"
                existing = 0
            with open(path, mode) as fh:
                for chunk in resp.iter_content(chunk_size=262144):
                    if chunk:
                        fh.write(chunk)
                        self.stats["bytes"] += len(chunk)
            resp.close()
        except Exception as exc:
            if resp is not None:
                resp.close()
            log.error("[%d] download failed: %s", game_id, exc)
            return False, "failed"

        if path.stat().st_size == 0:
            log.warning("[%d] downloaded empty file, removing: %s", game_id, path.name)
            try:
                path.unlink()
            except OSError:
                pass
            return False, "failed"

        if not self._looks_valid(path):
            log.warning("[%d] downloaded content looks like an error page, removing: %s",
                        game_id, path.name)
            try:
                path.unlink()
            except OSError:
                pass
            return False, "failed"

        # record state
        self.state.setdefault(str(game_id), {})[str(media_id)] = {
            "file": filename,
            "size": path.stat().st_size,
            "title": title,
            "media_id": str(media_id),
        }
        self._save_state()
        return True, "downloaded"

    @staticmethod
    def _looks_valid(path):
        """Reject files that are actually HTML error / block pages."""
        try:
            with open(path, "rb") as fh:
                head = fh.read(512)
        except OSError:
            return False
        low = head[:128].lower()
        if b"<!doctype html" in low or b"<html" in low or b"<head>" in low:
            return False
        return True

    # ------------------------------------------------------------------ driver
    def run_system(self, system):
        letters = self.get_letters(system)
        if not letters:
            log.warning("No letter sections found for system %s", system)
            return
        log.info("System %s: %d section(s): %s", system, len(letters), " ".join(letters))
        out_dir = self.output / system
        out_dir.mkdir(parents=True, exist_ok=True)
        for letter in letters:
            try:
                game_ids = self.list_game_ids(system, letter)
            except Exception as exc:
                log.error("Could not list section %s/%s: %s", system, letter, exc)
                continue
            log.info("  %s/%s: %d game(s)", system, letter, len(game_ids))
            for game_id in game_ids:
                self.stats["listed"] += 1
                try:
                    media = self.get_media(game_id)
                    picks = self.pick_media(game_id, media)
                    if not picks:
                        log.info("[%d] no downloadable media (upload-only?), skipping", game_id)
                        continue
                    for entry in picks:
                        ok, reason = self.download_media(system, game_id, entry)
                        if ok:
                            self.stats["downloaded" if reason == "downloaded" else "skipped"] += 1
                        else:
                            self.stats["failed"] += 1
                except Exception as exc:
                    self.stats["failed"] += 1
                    log.error("[%d] unexpected error: %s", game_id, exc)

    def run(self, systems):
        for system in systems:
            self.run_system(system)
        self._save_state()
        log.info("DONE. listed=%d downloaded=%d skipped=%d failed=%d bytes=%d",
                 self.stats["listed"], self.stats["downloaded"],
                 self.stats["skipped"], self.stats["failed"], self.stats["bytes"])


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Bulk downloader for Vimm's Lair The Vault")
    p.add_argument("--systems", required=True,
                   help="Comma-separated console codes, e.g. SNES,NES,N64 (or just SNES)")
    p.add_argument("--output", default="./roms", help="Output directory (default: ./roms)")
    p.add_argument("--throttle", type=float, default=1.5,
                   help="Seconds to wait between HTTP requests (default: 1.5)")
    p.add_argument("--retries", type=int, default=5,
                   help="Max retries per request (default: 5)")
    p.add_argument("--backoff", type=float, default=3.0,
                   help="Base backoff seconds, doubled per retry (default: 3)")
    p.add_argument("--timeout", type=int, default=30,
                   help="Per-request timeout in seconds (default: 30)")
    p.add_argument("--letter", default=None,
                   help="Only process these letter sections, e.g. 'A,C,#'")
    p.add_argument("--all-versions", action="store_true",
                   help="Download every version/disc of each game, not just the default")
    p.add_argument("--no-resume", action="store_true",
                   help="Do not resume partial files (restart them instead)")
    p.add_argument("--always-download", action="store_true",
                   help="Re-download files even if they already exist")
    p.add_argument("--state", default=None,
                   help="Path to the state file (default: ./.vimm_downloader_state.json)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)-7s %(message)s")
    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    if not systems:
        log.error("No systems given")
        return 2
    letters = None
    if args.letter:
        letters = [c.strip().upper() for c in args.letter.split(",") if c.strip()]

    dl = VimmDownloader(
        output=args.output,
        throttle=args.throttle,
        retries=args.retries,
        backoff=args.backoff,
        timeout=args.timeout,
        all_versions=args.all_versions,
        skip_existing=not args.always_download,
        resume=not args.no_resume,
        state_path=args.state,
        letters=letters,
    )
    try:
        dl.run(systems)
    except KeyboardInterrupt:
        log.info("Interrupted; state saved, run again to resume.")
        dl._save_state()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
