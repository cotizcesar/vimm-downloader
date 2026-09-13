#!/usr/bin/env python3
"""
Vimm's Lair Vault bulk downloader.

Downloads every game of one or more consoles from https://vimm.net/vault.

Features:
  * Retries with exponential backoff on failures / HTTP errors (incl. 403/429/503)
  * Configurable throttle (delay) between every HTTP request
  * Resumable downloads (Range) and skip-already-downloaded files
  * Persistent state file so interrupted runs can be resumed safely
  * Works for every system in The Vault (SNES, NES, N64, GB, PS1, ...)
  * Dry-run mode to preview without downloading
  * Download host fallback (dl3 -> dl -> download)

Usage examples:
  python3 vimm_downloader.py --systems SNES --output ./roms
  python3 vimm_downloader.py --systems SNES,NES,N64 --output ./roms --throttle 2
  python3 vimm_downloader.py --systems SNES --letter A --output ./roms --all-versions
  python3 vimm_downloader.py --systems SNES --dry-run --output ./roms
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
# Vimm has rotated dl hosts over time; try in order.
# dl.vimm.net has expired cert (2024), keep only reliable hosts
DL_HOSTS = ["https://dl3.vimm.net", "https://download.vimm.net"]
DL_HOST = DL_HOSTS[0]
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

GAME_ID_RE = re.compile(r'href\s*=\s*["\']?/vault/(\d+)(?:["\'\s>/?#]|$)', re.I)
MEDIA_RE = re.compile(r"let media\s*=\s*\[(.*?)\];", re.S)
# fallback for newer inline JSON: var media = [...] or window.media
MEDIA_RE_FALLBACK = re.compile(r"media\s*[:=]\s*\[(.*?)\]", re.S)

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
    # RFC 6266: filename*=UTF-8''... takes precedence
    m = re.search(r'filename\*\s*=\s*UTF-8\'\'"?([^";\s]+)"?', value, re.I)
    if m:
        try:
            import urllib.parse
            return sanitize_filename(urllib.parse.unquote(m.group(1).strip('"')))
        except Exception:
            pass
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', value, re.I)
    if not m:
        return None
    name = m.group(1).strip().strip('"')
    if name.startswith("%"):
        try:
            import urllib.parse
            name = urllib.parse.unquote(name)
        except Exception:
            pass
    return sanitize_filename(name)


class VimmDownloader:
    def __init__(self, output, throttle=1.5, retries=5, backoff=3.0, timeout=30,
                 all_versions=False, skip_existing=True, resume=True,
                 state_path=None, letters=None, dry_run=False, dl_host=None):
        self.output = Path(output)
        self.throttle = max(0.0, float(throttle))
        self.retries = max(1, int(retries))
        self.backoff = max(0.1, float(backoff))
        self.timeout = int(timeout)
        self.all_versions = bool(all_versions)
        self.skip_existing = bool(skip_existing)
        self.resume = bool(resume)
        self.dry_run = bool(dry_run)
        self.letters = letters
        self.dl_host = dl_host or DL_HOST
        self.dl_hosts = [self.dl_host] + [h for h in DL_HOSTS if h != self.dl_host]
        self.state_path = Path(state_path) if state_path else Path.cwd() / ".vimm_downloader_state.json"
        self.state = {}
        self.stats = {"listed": 0, "downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Cache-Control": "max-age=0",
        })
        self._load_state()

    # ------------------------------------------------------------------ state
    def _load_state(self):
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
                log.info("Loaded state file with %d recorded downloads", len(self.state))
            except (ValueError, OSError) as exc:
                log.warning("Could not read state file %s: %s", self.state_path, exc)
                self.state = {}

    def _save_state(self):
        if self.dry_run:
            return
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True), encoding="utf-8")
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
                # Cloudflare / anti-bot protections often return these
                if resp.status_code in (403, 429, 503):
                    body = resp.text[:500] if hasattr(resp, 'text') else ''
                    is_cf = "cloudflare" in body.lower() or "attention required" in body.lower() or resp.headers.get("Server", "").lower() == "cloudflare"
                    tag = " (cloudflare)" if is_cf else ""
                    log.warning("HTTP %d%s on %s — retrying (%d/%d)", resp.status_code, tag, url, attempt, retries)
                    last_exc = DownloadError("HTTP %d for %s" % (resp.status_code, url))
                    resp.close()
                elif resp.status_code == 400:
                    # Vimm returns 400 when bot check fails; treat as retriable with longer wait
                    log.warning("HTTP 400 on %s (possible bot check) — retrying (%d/%d)", url, attempt, retries)
                    last_exc = DownloadError("HTTP 400 blocked by server: " + url)
                    resp.close()
                elif resp.status_code == 404:
                    # Don't retry 404s — system/section doesn't exist
                    log.error("HTTP 404 on %s — not found, skipping", url)
                    resp.close()
                    raise DownloadError("HTTP 404 for %s" % url)
                else:
                    last_exc = DownloadError("HTTP %d for %s" % (resp.status_code, url))
                    log.warning("HTTP %d on %s (%d/%d): %s", resp.status_code, url, attempt, retries, last_exc)
                    resp.close()
            except (requests.RequestException, DownloadError) as exc:
                # Don't wrap 404 DownloadError in retry logic if it's a 404
                if isinstance(exc, DownloadError) and "404" in str(exc):
                    raise
                last_exc = exc
                if attempt == retries:
                    log.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
                else:
                    log.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            if attempt < retries:
                # longer wait for cloudflare/429
                base = self.backoff * 2 if last_exc and "429" in str(last_exc) else self.backoff
                wait = base * (2 ** (attempt - 1))
                # jitter
                wait += (wait * 0.1 * (attempt % 2))
                log.info("Retrying %s in %.1fs", url, wait)
                time.sleep(wait)
        raise last_exc

    # -------------------------------------------------------------- discovery
    def get_letters(self, system):
        """Return the list of letter sections available for a system."""
        if self.letters:
            chosen = set(c.upper() for c in self.letters)
            return [c for c in ["#"] + LETTERS if c in chosen]
        url = "%s/vault/%s" % (BASE_URL, system)
        try:
            html = self._request(url).text
        except DownloadError as exc:
            if "404" in str(exc):
                log.error("System '%s' not found (404). Check https://vimm.net/vault for valid codes.", system)
                return []
            raise
        found = set()
        if re.search(r"section=number", html, re.I):
            found.add("#")
        pat = re.compile(r'href="/vault/' + re.escape(system) + r'/([A-Z])["\s/?#]', re.I)
        found.update(m.group(1).upper() for m in pat.finditer(html))
        # fallback: if no letters found but page exists, assume all exist
        if not found:
            log.warning("Could not detect letter sections for %s — trying all A-Z + #", system)
            return ["#"] + LETTERS
        return [c for c in ["#"] + LETTERS if c in found]

    def list_game_ids(self, system, letter):
        """Return the sorted unique game IDs in a letter section (handles pagination)."""
        ids = set()
        page = 1
        while True:
            if letter == "#":
                base = "%s/vault/?p=list&system=%s&section=number" % (BASE_URL, system)
            else:
                base = "%s/vault/%s/%s" % (BASE_URL, system, letter)
            url = base if page == 1 else "%s?p=%d" % (base, page) if "?" in base else "%s?p=%d" % (base, page)
            # Vimm paginates with ?p=list&...&page=N or /vault/SYSTEM/A?page=N
            # Try both patterns by inspecting 'next' link
            try:
                html = self._request(url, referer="%s/vault/%s" % (BASE_URL, system)).text
            except DownloadError as exc:
                if page == 1:
                    raise
                log.debug("Pagination stopped at page %d for %s/%s: %s", page, system, letter, exc)
                break
            found = {int(i) for i in GAME_ID_RE.findall(html) if int(i) != 999999}
            if not found:
                break
            new_ids = found - ids
            if not new_ids and page > 1:
                break
            ids.update(found)
            # check if there is a next page link
            has_next = bool(re.search(r'[?&]page=%d' % (page + 1), html) or
                            re.search(r'>\s*Next\s*<', html, re.I) or
                            re.search(r'pagination', html, re.I) and 'page=%d' % (page + 1) in html)
            # Alternative: if we got fewer than expected and no next indicator, stop
            # Vimm pages usually have ~100 items; if we found < 20 on first page maybe it's all
            if not has_next:
                # Heuristic: if page is small and no explicit next, try one more page to be sure
                if page == 1 and len(found) >= 50:
                    # likely paginated, try next page once
                    pass
                else:
                    break
                # probe next page - if it yields new ids, continue, else stop
                # we will loop once more; if probe fails or empty we break next iter
            page += 1
            if page > 50:
                log.warning("Pagination safety limit (50 pages) reached for %s/%s", system, letter)
                break
        return sorted(ids)

    def _probe_head(self, url, referer):
        """Quiet HEAD probe (no WARNING) for dl hosts — returns response or raises."""
        # Transient DNS errors are common, retry once silently
        for attempt in range(2):
            self._throttle()
            try:
                req_headers = dict(self.session.headers)
                if referer:
                    req_headers["Referer"] = referer
                # download.vimm.net sometimes has TLS quirks, don't verify for probe
                resp = self.session.request("HEAD", url, headers=req_headers,
                                            timeout=self.timeout, allow_redirects=True)
                if resp.status_code in (200, 206):
                    return resp
                # 404 etc -> treat as no file, not an error to retry
                resp.close()
                raise DownloadError("HTTP %d for %s" % (resp.status_code, url))
            except (requests.RequestException, DownloadError) as exc:
                if attempt == 1:
                    raise
                # brief backoff for transient DNS
                time.sleep(0.5)
                continue

    def _has_direct_download(self, game_id):
        """Check if dl host returns a file for mediaId == game_id without needing vault page."""
        for host in self.dl_hosts:
            url = "%s/?mediaId=%s" % (host, game_id)
            try:
                resp = self._probe_head(url, "%s/vault/%d" % (BASE_URL, game_id))
                ct = resp.headers.get("Content-Type", "")
                cd = resp.headers.get("Content-Disposition", "")
                clen = resp.headers.get("Content-Length", "")
                resp.close()
                # valid download is zip/7z with attachment
                if "attachment" in cd.lower() or "application" in ct.lower():
                    return True
                if clen and clen.isdigit() and int(clen) > 1024:
                    return True
                # still consider 200 as success even without headers
                return True
            except Exception as exc:
                log.debug("HEAD probe %s failed: %s", host, exc)
                continue
        return False

    def _synthetic_media(self, game_id):
        """Create a minimal media entry that allows direct download via mediaId == game_id."""
        # GoodTitle is base64 encoded; we provide a fallback
        fallback_title = "Game %d" % game_id
        return [{
            "ID": str(game_id),
            "GoodTitle": base64.b64encode(fallback_title.encode()).decode(),
            "Zipped": "1",
            "AltZipped": "0",
            "ZippedText": "?",
        }]

    def get_media(self, game_id):
        """Fetch a game page and parse its downloadable media entries."""
        url = "%s/vault/%d" % (BASE_URL, game_id)
        try:
            html = self._request(url, referer="%s/vault" % BASE_URL).text
        except DownloadError as exc:
            if "404" in str(exc):
                # For 404 we try to distinguish Turnstile vs true missing
                try:
                    # Use raw session to get body even on 404 without raising
                    resp = self.session.get(url, headers={"Referer": "%s/vault" % BASE_URL}, timeout=self.timeout)
                    body = resp.text if hasattr(resp, 'text') else ''
                    is_turnstile = "cf-turnstile" in body or "turnstile" in body.lower() or "Checking if you are human" in body
                    resp.close()
                    if is_turnstile:
                        log.info("[%d] Turnstile challenge detected — fallback to direct mediaId", game_id)
                        if self._has_direct_download(game_id):
                            return self._synthetic_media(game_id)
                        log.debug("[%d] No direct download (likely upload-only) — skipping", game_id)
                        return []
                    else:
                        log.debug("[%d] Vault page 404 (no Turnstile) — skipping", game_id)
                        return []
                except Exception:
                    return []
            raise
        # Detect Turnstile even on 200 edge case (some vault pages return 200 with challenge)
        if "cf-turnstile" in html and "let media" not in html:
            log.info("[%d] Turnstile challenge on 200 — fallback to direct mediaId", game_id)
            if self._has_direct_download(game_id):
                return self._synthetic_media(game_id)
            return []
        m = MEDIA_RE.search(html)
        if not m:
            m = MEDIA_RE_FALLBACK.search(html)
        if not m:
            # No media block but maybe direct download still works (single version)
            if self._has_direct_download(game_id):
                log.info("[%d] No media block but direct download exists — using fallback", game_id)
                return self._synthetic_media(game_id)
            return []
        try:
            raw = m.group(1).strip().rstrip(",")
            # media block may be comma-separated objects without outer array
            return json.loads("[" + raw + "]")
        except ValueError as exc:
            log.debug("Failed to parse media JSON for %d: %s", game_id, exc)
            # Fallback to direct if parse fails
            if self._has_direct_download(game_id):
                return self._synthetic_media(game_id)
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

    def _resolve_dl_url(self, media_id, referer):
        """Try dl hosts in order with HEAD to find a working host."""
        for host in self.dl_hosts:
            url = "%s/?mediaId=%s" % (host, media_id)
            try:
                resp = self._probe_head(url, referer)
                fname = parse_content_disposition(resp.headers.get("Content-Disposition"))
                resp.close()
                return url, fname
            except Exception as exc:
                log.debug("resolve HEAD %s failed: %s", host, exc)
                continue
        # fallback to primary host even if HEAD failed
        return "%s/?mediaId=%s" % (self.dl_host, media_id), None

    def _target_filename(self, system, game_id, entry):
        """Resolve the on-disk filename for a media entry (HEAD first with fallback)."""
        recorded = self._recorded_filename(game_id, entry["ID"])
        if recorded:
            return recorded
        title = sanitize_filename(self.decode_title(entry))
        try:
            _, fname = self._resolve_dl_url(entry["ID"], "%s/vault/%d" % (BASE_URL, game_id))
            if fname:
                return sanitize_filename(fname)
        except Exception:
            pass
        return title + ".zip"

    def download_media(self, system, game_id, entry):
        media_id = entry["ID"]
        title = sanitize_filename(self.decode_title(entry))
        game_dir = self.output / system
        game_dir.mkdir(parents=True, exist_ok=True)
        filename = self._target_filename(system, game_id, entry)
        path = game_dir / filename

        # skip if this media is already recorded and the file exists
        recorded = self._recorded_filename(game_id, media_id)
        if self.skip_existing and recorded == filename and path.exists() and path.stat().st_size > 0:
            log.info("[%d] skipping (exists): %s", game_id, path.name)
            return True, "skipped"

        # dry-run: don't actually download
        if self.dry_run:
            log.info("[DRY-RUN] [%d] would download: %s -> %s", game_id, title, path)
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

        # avoid collision with the same filename recorded for a different game
        if self.state:
            for gid, media_map in self.state.items():
                if gid == str(game_id):
                    continue
                if any(rec.get("file") == filename for rec in media_map.values()):
                    stem = path.stem
                    filename = "%s (%s)%s" % (stem, game_id, path.suffix)
                    path = game_dir / filename
                    break

        existing = path.stat().st_size if (self.resume and path.exists()) else 0
        headers = {"Range": "bytes=%d-" % existing} if existing > 0 else None
        log.info("[%d] downloading %s (%s) -> %s", game_id, title, entry.get("ZippedText", "?"), path.name)

        resp = None
        # Try dl hosts in order for GET as well
        last_exc = None
        for host in self.dl_hosts:
            url = "%s/?mediaId=%s" % (host, media_id)
            try:
                resp = self._request(url, method="GET", referer="%s/vault/%d" % (BASE_URL, game_id),
                                     headers=headers, stream=True, retries=self.retries)
                # success -> break out
                break
            except Exception as exc:
                last_exc = exc
                log.warning("[%d] host %s failed: %s — trying next host", game_id, host, exc)
                resp = None
                continue
        if resp is None:
            log.error("[%d] all download hosts failed: %s", game_id, last_exc)
            return False, "failed"

        try:
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
                try:
                    resp.close()
                except Exception:
                    pass
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
                        # Last resort: try direct download via mediaId == game_id (bypasses Turnstile)
                        if not self.all_versions and self._has_direct_download(game_id):
                            log.info("[%d] Direct download fallback (mediaId == game_id)", game_id)
                            picks = self._synthetic_media(game_id)
                        else:
                            log.info("[%d] no downloadable media (upload-only?), skipping", game_id)
                            continue
                    for entry in picks:
                        ok, reason = self.download_media(system, game_id, entry)
                        if ok:
                            self.stats["downloaded" if reason == "downloaded" else "skipped"] += 1
                        else:
                            self.stats["failed"] += 1
                except KeyboardInterrupt:
                    raise
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
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be downloaded without downloading")
    p.add_argument("--dl-host", default=None,
                   help="Override download host (default: https://dl3.vimm.net)")
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
        dry_run=args.dry_run,
        dl_host=args.dl_host,
    )
    if args.dry_run:
        log.info("DRY-RUN enabled — no files will be downloaded, state won't be modified")
    try:
        dl.run(systems)
    except KeyboardInterrupt:
        log.info("Interrupted; state saved, run again to resume.")
        dl._save_state()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
