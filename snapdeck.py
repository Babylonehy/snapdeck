#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["lxml>=5"]
# ///
"""
snapdeck.py — Convert a "Claude Design" deck export into a fully offline,
directly-playable presentation bundle.

A Claude Design export is a folder containing:
  <name>.dc.html   page with <x-dc><helmet>…</helmet>
                   <x-import component-from-global-scope="deck-stage"
                             from="./deck-stage.js" width=… height=…> …<section>×N… </x-import></x-dc>
  support.js       the dc-runtime (NOT needed offline — excluded)
  deck-stage.js    the <deck-stage> custom element (self-contained vanilla JS)
  assets/ uploads/  images / GIFs referenced by the slides

The <deck-stage> element renders the whole presentation from its slotted
<section> children using only deck-stage.js (no support.js / x-import / React),
so we rebuild a standalone HTML of that shape, localize the Google Fonts, copy
only the referenced assets, and add a one-click launcher + a dual-screen
presenter (speaker-notes) view.

Usage:
    python3 snapdeck.py <export-dir-or-.dc.html> [-o OUTDIR]
        --rail                   keep the thumbnail sidebar (default: hidden / full-bleed)
        --no-annotate            drop the laser-pointer / pen annotation toolbar
        --button-label TEXT      fullscreen button text (default "▶ Fullscreen")
        --fonts {mirror,system}  default mirror (download Google fonts → fonts/)
        --no-launcher            skip 双击放映.command / serve.sh
        --no-presenter           skip presenter.html + sync shim
        --no-render              skip the render pass (templated slides may be incomplete)
        --chrome PATH            Chrome/Chromium binary for the render pass
        --deck-index N           pick one deck when the file has several
        --deck-stage-js PATH     explicit deck-stage.js if not found next to the html
        --force                  overwrite an output dir that doesn't end in -offline

Slides built from dc-runtime templates ({{…}}) or nested components are
rendered once with their runtime (headless Chrome, served over localhost —
needs internet for the deck's own React/Babel/fonts) and the expanded DOM is
snapshotted, so the offline bundle isn't missing template-generated content.
"""
import argparse
import concurrent.futures as cf
import functools
import hashlib
import http.server
import json
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote

try:
    import lxml.html
except ImportError:
    sys.exit("This tool needs lxml.  Install it with:  python3 -m pip install lxml")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


# ─────────────────────────────────────────────────────────── parsing ──

class Deck:
    """One <deck-stage>/x-import deck parsed out of a .dc.html."""
    def __init__(self):
        self.width = 1920
        self.height = 1080
        self.no_rail = False
        self.sections = []     # list of HTML strings (verbatim <section>…</section>)
        self.notes = []        # list[str], one per section
        self.notes_json = None # original #speaker-notes JSON text if the source used one


def _find_decks(tree):
    """Return deck container elements, most-specific selector first."""
    for xp in ("//x-import[@component-from-global-scope='deck-stage']",
               "//deck-stage",
               "//x-import[@from]"):
        hits = tree.xpath(xp)
        if xp.endswith("[@from]"):
            hits = [h for h in hits if str(h.get("from", "")).endswith("deck-stage.js")]
        if hits:
            return hits
    return []


def _collect_head_styles(tree):
    """Helmet <style> text (keyframes + base rules) and Google-Font <link> hrefs."""
    styles = [s.text or "" for s in tree.xpath("//helmet//style")]
    if not styles:                      # be forgiving: any top-level <style> outside the deck
        styles = [s.text or "" for s in tree.xpath("//head//style")]
    font_links = []
    for ln in tree.xpath("//link[@href]"):
        href = ln.get("href", "")
        if "fonts.googleapis.com" in href and ln.get("rel", "stylesheet") != "preconnect":
            if href not in font_links:
                font_links.append(href)
    return "\n".join(t for t in styles if t.strip()), font_links


def _extract_notes(deck_el, sections):
    """Mirror deck-stage _loadNotes precedence: data-speaker-notes, else #speaker-notes JSON[i]."""
    json_notes, json_text = None, None
    for tag in deck_el.getroottree().xpath("//script[@id='speaker-notes']"):
        json_text = tag.text or ""
        try:
            parsed = json.loads(json_text)
            if isinstance(parsed, list):
                json_notes = parsed
        except Exception:
            pass
    notes = []
    for i, sec in enumerate(sections):
        attr = sec.get("data-speaker-notes")
        if attr is not None:
            notes.append(attr)
        elif json_notes and i < len(json_notes) and isinstance(json_notes[i], str):
            notes.append(json_notes[i])
        else:
            notes.append("")
    return notes, (json_text if json_notes is not None else None)


# Attributes the dc-runtime / deck-stage stamp onto rendered slides; dropped so
# the snapshot is clean and a fresh deck-stage re-derives them.
RUNTIME_ATTRS = {"data-deck-active", "data-deck-slide", "data-om-validate", "data-dc-tpl"}


def _strip_runtime_attrs(sec):
    for el in sec.iter():
        for a in list(el.attrib):
            if a in RUNTIME_ATTRS:
                del el.attrib[a]


def find_chrome(override):
    cands = [override] if override else []
    cands += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chrome"),
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def render_snapshot(html_path, chrome_override):
    """Serve the export over localhost, render it with its runtime in headless
    Chrome, and return the fully-expanded DOM (templates/components resolved).
    Returns None if Chrome is unavailable or rendering fails."""
    chrome = find_chrome(chrome_override)
    if not chrome:
        log("  ! Chrome/Chromium not found — skipping render pass (pass --chrome PATH)")
        return None

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=str(html_path.parent))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/{quote(html_path.name)}"
        log("  rendering deck with its runtime (expanding templates/components)…")
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--virtual-time-budget=20000", "--dump-dom", url]
        res = subprocess.run(cmd, capture_output=True, timeout=90)
        html = res.stdout.decode("utf-8", "replace")
        if "deck-stage" not in html:
            log("  ! render produced no deck-stage; falling back to raw parse")
            return None
        # Force UTF-8: Chrome's dump-dom has no charset hint, so lxml would
        # otherwise guess Latin-1 and mojibake CJK filenames/text.
        return lxml.html.fromstring(html.encode("utf-8"),
                                    parser=lxml.html.HTMLParser(encoding="utf-8"))
    except Exception as e:
        log(f"  ! render failed ({e}); falling back to raw parse")
        return None
    finally:
        httpd.shutdown()


def parse_export(input_path, deck_index=None, render="auto", chrome=None):
    """Locate the .dc.html, parse it, return (html_path, [Deck,...], head_styles, font_links)."""
    p = Path(input_path)
    if p.is_dir():
        candidates = sorted(p.glob("*.dc.html")) or sorted(p.glob("*.html"))
        if not candidates:
            sys.exit(f"No .dc.html found in {p}")
        html_path = candidates[0]
    else:
        html_path = p
    if not html_path.exists():
        sys.exit(f"File not found: {html_path}")

    raw_tree = lxml.html.fromstring(html_path.read_bytes())
    head_styles, font_links = _collect_head_styles(raw_tree)

    # The raw <section>s may contain dc-runtime templates ({{…}}) or nested
    # components (<x-…>) that only support.js expands. Render the live deck and
    # snapshot the finished DOM so those slides aren't bundled half-empty.
    section_tree = raw_tree
    if render != "never":
        blob = "".join(
            lxml.html.tostring(s, encoding="unicode")
            for el in _find_decks(raw_tree)
            for s in (el.xpath("./section") or el.xpath(".//section")))
        templated = ("{{" in blob) or bool(re.search(r"<x-(?!dc\b|import\b)", blob))
        if render == "always" or templated:
            rendered = render_snapshot(html_path, chrome)
            if rendered is not None:
                section_tree = rendered
            elif templated:
                log("  ! slides use templates ({{…}}) but render was unavailable — "
                    "content may be incomplete (see --chrome / network)")

    deck_els = _find_decks(section_tree)
    if not deck_els:
        sys.exit("No deck found (looked for x-import/deck-stage).")
    if deck_index is not None:
        if not (0 <= deck_index < len(deck_els)):
            sys.exit(f"--deck-index {deck_index} out of range (found {len(deck_els)} decks)")
        deck_els = [deck_els[deck_index]]

    decks = []
    for el in deck_els:
        secs = el.xpath("./section") or el.xpath(".//section")
        if not secs:
            continue
        for s in secs:
            _strip_runtime_attrs(s)
        d = Deck()
        d.width = int(el.get("width") or 1920)
        d.height = int(el.get("height") or 1080)
        d.no_rail = el.get("no-rail") is not None
        d.sections = [lxml.html.tostring(s, encoding="unicode") for s in secs]
        d.notes, d.notes_json = _extract_notes(el, secs)
        decks.append(d)
    if not decks:
        sys.exit("Deck container found but it has no <section> slides.")
    return html_path, decks, head_styles, font_links


def locate_deck_stage_js(html_path, override):
    if override:
        p = Path(override)
        if not p.exists():
            sys.exit(f"--deck-stage-js not found: {p}")
        return p
    here = html_path.parent / "deck-stage.js"
    if here.exists():
        return here
    found = list(html_path.parent.rglob("deck-stage.js"))
    if found:
        return found[0]
    sys.exit("deck-stage.js not found next to the export. Pass --deck-stage-js PATH.")


# ──────────────────────────────────────────────────────── font mirror ──

FACE_RE = re.compile(r"@font-face\s*\{(.*?)\}", re.S)
SRC_RE = re.compile(r"src:\s*url\((['\"]?)(https://fonts\.gstatic\.com/[^)'\"]+)\1\)")


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "font").lower()).strip("-") or "font"


def mirror_fonts(font_links, fonts_dir):
    """Download every woff2 referenced by the css2 stylesheets; return rewritten @font-face CSS."""
    if not font_links:
        return ""
    fonts_dir.mkdir(parents=True, exist_ok=True)
    rewritten_blocks, downloads = [], {}  # url -> local filename

    for link in font_links:
        try:
            req = urllib.request.Request(link, headers={"User-Agent": UA})
            css = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        except Exception as e:
            log(f"  ! could not fetch font css ({e}); falling back to system fonts")
            return ""
        for body in FACE_RE.findall(css):
            m = SRC_RE.search(body)
            if not m:
                continue
            url = m.group(2)
            if url not in downloads:
                fam = (re.search(r"font-family:\s*['\"]?([^;'\"]+)", body) or [None, "font"])[1]
                wght = (re.search(r"font-weight:\s*(\d+)", body) or [None, "0"])[1]
                ital = "i" if "italic" in body.lower() else ""
                downloads[url] = f"{_slug(fam)}-{wght}{ital}-{hashlib.md5(url.encode()).hexdigest()[:8]}.woff2"
            local = downloads[url]
            rewritten_blocks.append("@font-face {" + body.replace(url, f"fonts/{local}") + "}")

    # Parallel download — there can be hundreds of tiny CJK unicode-range slices.
    # A single flaky slice must not abort the whole conversion: retry, then skip.
    def fetch(item):
        url, name = item
        dest = fonts_dir / name
        if dest.exists() and dest.stat().st_size:
            return dest.stat().st_size
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                data = urllib.request.urlopen(req, timeout=30).read()
                dest.write_bytes(data)
                return len(data)
            except Exception:
                if attempt == 2:
                    return 0
        return 0

    total, failed = 0, 0
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for sz in ex.map(fetch, downloads.items()):
            total += sz
            failed += (sz == 0)
    if failed:
        log(f"  ! {failed}/{len(downloads)} font slices failed to download (those glyphs fall back to system)")
    log(f"  fonts: {len(downloads) - failed} woff2 files, {human(total)}")
    return "\n".join(rewritten_blocks)


# ──────────────────────────────────────────────────────── asset copy ──

URL_RE = re.compile(r"url\((['\"]?)([^)'\"]+)\1\)")


def collect_assets(decks, head_styles, src_dir, out_dir):
    """Copy only referenced local assets into the bundle, preserving relative paths."""
    refs = set()
    blob = head_styles + "".join("".join(d.sections) for d in decks)
    # <img src>, srcset, video/audio src/poster, and any url(...) in inline/style CSS
    for m in re.finditer(r'(?:src|poster)\s*=\s*"([^"]+)"', blob):
        refs.add(m.group(1))
    for m in re.finditer(r'srcset\s*=\s*"([^"]+)"', blob):
        for part in m.group(1).split(","):
            refs.add(part.strip().split(" ")[0])
    for m in URL_RE.finditer(blob):
        refs.add(m.group(2))

    copied, total = 0, 0
    for ref in sorted(refs):
        if ref.startswith(("data:", "http:", "https:", "#", "//")) or not ref.strip():
            continue
        # lxml percent-encodes non-ASCII paths (e.g. CJK filenames); decode for the
        # filesystem. The HTML keeps the encoded ref — browsers decode it the same way.
        rel = unquote(ref)
        src = (src_dir / rel).resolve()
        try:
            src.relative_to(src_dir.resolve())
        except ValueError:
            continue  # escapes the export tree
        if not src.is_file():
            continue
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
        total += src.stat().st_size
    log(f"  assets: copied {copied} referenced files, {human(total)}")
    return copied


# ────────────────────────────────────────────────────────── builders ──

BASE_CSS = "*{box-sizing:border-box}body{margin:0;background:#000}deck-stage:not(:defined){visibility:hidden}"

SYNC_SHIM = """
<script>
/* presenter sync — re-broadcast slide changes and accept nav commands.
   Reliable only over a real origin (the launcher's http://127.0.0.1), not file://. */
(function () {
  if (typeof BroadcastChannel === 'undefined') return;
  var ch = new BroadcastChannel('deck');
  function deck() { return document.querySelector('deck-stage'); }
  document.addEventListener('slidechange', function (e) {
    ch.postMessage({ type: 'slide', index: e.detail.index, total: e.detail.total });
  });
  ch.onmessage = function (m) {
    var d = deck(), x = m.data; if (!d || !x) return;
    if (x.type === 'go' && typeof x.index === 'number') d.goTo(x.index);
    else if (x.type === 'next') d.next();
    else if (x.type === 'prev') d.prev();
    else if (x.type === 'reset') d.reset();
    else if (x.type === 'hello') ch.postMessage({ type: 'slide', index: d.index, total: d.length });
  };
})();
</script>
"""


FULLSCREEN_SHIM = """
<script>
/* Playback page: a visible "全屏放映" button enters fullscreen on click (a user
   gesture, which browsers require — a page can't self-fullscreen on load).
   Pressing → also enters fullscreen. The button hides while in fullscreen and
   comes back on exit so you can re-enter. */
(function () {
  var NAV = { ArrowRight: 1, ArrowLeft: 1, ' ': 1, Spacebar: 1, PageDown: 1, PageUp: 1, Enter: 1 };
  function inFs() { return document.fullscreenElement || document.webkitFullscreenElement; }
  function enter() {
    var e = document.documentElement, rq = e.requestFullscreen || e.webkitRequestFullscreen;
    if (rq) { try { var p = rq.call(e); if (p && p.catch) p.catch(function () {}); } catch (x) {} }
  }
  var btn;
  function sync() {
    if (!btn) return;
    var show = !inFs();
    btn.style.opacity = show ? '1' : '0';
    btn.style.pointerEvents = show ? 'auto' : 'none';
  }
  function armKey(e) {
    if (!NAV[e.key] || inFs()) return;     // first → only fullscreens, doesn't advance
    enter(); e.preventDefault(); e.stopImmediatePropagation();
    window.removeEventListener('keydown', armKey, true);
  }
  function start() {
    btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = __BTN_LABEL__;
    btn.setAttribute('style', 'position:fixed;right:22px;bottom:22px;z-index:2147483647;'
      + 'background:#1d3a8a;color:#fff;border:0;border-radius:999px;cursor:pointer;'
      + 'font:600 16px/1 -apple-system,sans-serif;padding:13px 22px;'
      + 'box-shadow:0 6px 20px rgba(0,0,0,.28);transition:opacity .25s');
    btn.addEventListener('click', function (e) { e.preventDefault(); e.stopImmediatePropagation(); enter(); });
    document.body.appendChild(btn);
    sync();
    window.addEventListener('keydown', armKey, true);
    document.addEventListener('fullscreenchange', sync);
    document.addEventListener('webkitfullscreenchange', sync);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
</script>
"""


ANIM_REPLAY_SHIM = """
<script>
/* Replay a slide's CSS animations when it becomes the active slide. deck-stage
   mounts every slide at load, so entrance animations (seqFade/injectIn/…) fire
   once while the slide is still hidden and are finished by the time you
   navigate in — making the slide look static/"incomplete". Restarting them on
   activation makes the motion play on arrival, matching the live deck. */
(function () {
  function replay(slide) {
    if (!slide) return;
    var els = slide.querySelectorAll('*');
    for (var i = 0; i < els.length; i++) {
      var el = els[i], anim = el.style.animation;
      if (!anim || anim === 'none') continue;
      el.style.animation = 'none';
      void el.offsetWidth;          // reflow so the restart registers
      el.style.animation = anim;
    }
  }
  function activeSlide() {
    var d = document.querySelector('deck-stage');
    return d ? d.querySelector('section[data-deck-active]') : null;
  }
  document.addEventListener('slidechange', function () {
    requestAnimationFrame(function () { replay(activeSlide()); });
  });
})();
</script>
"""


ANNOTATE_SHIM = """
<style>
  #sd-canvas { position:fixed; inset:0; z-index:2147483100; pointer-events:none; touch-action:none; }
  #sd-laser { position:fixed; z-index:2147483590; width:18px; height:18px; border-radius:50%;
    background:radial-gradient(circle,rgba(255,40,40,.95) 0%,rgba(255,40,40,.5) 42%,rgba(255,40,40,0) 70%);
    box-shadow:0 0 14px 5px rgba(255,30,30,.55); pointer-events:none; transform:translate(-50%,-50%); display:none; }
  #sd-tools { position:fixed; left:18px; bottom:18px; z-index:2147483600; display:flex; gap:5px; align-items:center;
    background:rgba(20,22,26,.82); border-radius:999px; padding:6px 8px; opacity:.45; transition:opacity .2s;
    -webkit-backdrop-filter:blur(6px); backdrop-filter:blur(6px); user-select:none;
    font:600 13px/1 -apple-system,'Noto Sans SC',sans-serif; }
  #sd-tools:hover { opacity:1; }
  #sd-tools button { background:transparent; color:#e8eaed; border:0; border-radius:999px; cursor:pointer;
    padding:7px 11px; font:inherit; line-height:1; }
  #sd-tools button:hover { background:rgba(255,255,255,.12); }
  #sd-tools button.active { background:#1d3a8a; color:#fff; }
  #sd-tools .dot { width:16px; height:16px; padding:0; border:2px solid rgba(255,255,255,.35); }
  #sd-tools .dot.active { border-color:#fff; transform:scale(1.15); }
  #sd-tools .sep { width:1px; height:16px; background:rgba(255,255,255,.18); margin:0 3px; }
  @media print { #sd-canvas, #sd-laser, #sd-tools { display:none !important; } }
</style>
<canvas id="sd-canvas"></canvas>
<div id="sd-laser"></div>
<div id="sd-tools">
  <button data-mode="off" class="active" title="Cursor (Esc)">Cursor</button>
  <button data-mode="laser" title="Laser pointer (L)">Laser</button>
  <span class="sep"></span>
  <button class="dot" data-color="#ff2d2d" style="background:#ff2d2d" title="Pen — red (P)"></button>
  <button class="dot" data-color="#ffd23f" style="background:#ffd23f" title="Pen — yellow"></button>
  <button class="dot" data-color="#3b82f6" style="background:#3b82f6" title="Pen — blue"></button>
  <span class="sep"></span>
  <button data-mode="erase" title="Eraser (E)">Erase</button>
  <button data-act="clear" title="Clear (Del)">Clear</button>
</div>
<script>
/* Presentation annotation: laser pointer, freehand pen (3 colors), eraser,
   clear. Toolbar bottom-left; works in fullscreen; ink clears on slide change.
   Shortcuts: L laser · P pen · E erase · Esc cursor · Del clear. */
(function () {
  var cv = document.getElementById('sd-canvas'), ctx = cv.getContext('2d');
  var laser = document.getElementById('sd-laser'), tools = document.getElementById('sd-tools');
  var mode = 'off', color = '#ff2d2d', drawing = false, last = null;
  var dpr = Math.max(1, window.devicePixelRatio || 1);
  function resize() {
    dpr = Math.max(1, window.devicePixelRatio || 1);
    cv.width = innerWidth * dpr; cv.height = innerHeight * dpr;
    cv.style.width = innerWidth + 'px'; cv.style.height = innerHeight + 'px';
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  }
  resize(); addEventListener('resize', resize);
  function drawable() { return mode === 'pen' || mode === 'erase'; }
  function refresh() {
    cv.style.pointerEvents = drawable() ? 'auto' : 'none';
    cv.style.cursor = drawable() ? 'crosshair' : 'default';
    laser.style.display = mode === 'laser' ? 'block' : 'none';
    tools.querySelectorAll('[data-mode]').forEach(function (b) { b.classList.toggle('active', b.dataset.mode === mode); });
    tools.querySelectorAll('.dot').forEach(function (d) { d.classList.toggle('active', mode === 'pen' && d.dataset.color === color); });
  }
  function setMode(m) { mode = m; refresh(); }
  function setPen(c) { color = c; mode = 'pen'; refresh(); }
  function clear() { ctx.clearRect(0, 0, cv.width, cv.height); }
  function P(e) { return [e.clientX * dpr, e.clientY * dpr]; }
  cv.addEventListener('pointerdown', function (e) {
    if (!drawable()) return; drawing = true; last = P(e); cv.setPointerCapture(e.pointerId);
  });
  cv.addEventListener('pointermove', function (e) {
    if (!drawing) return; var p = P(e);
    ctx.globalCompositeOperation = mode === 'erase' ? 'destination-out' : 'source-over';
    ctx.strokeStyle = color; ctx.lineWidth = (mode === 'erase' ? 28 : 3.5) * dpr;
    ctx.beginPath(); ctx.moveTo(last[0], last[1]); ctx.lineTo(p[0], p[1]); ctx.stroke(); last = p;
  });
  function end() { drawing = false; }
  cv.addEventListener('pointerup', end); cv.addEventListener('pointercancel', end);
  addEventListener('pointermove', function (e) {
    if (mode === 'laser') { laser.style.left = e.clientX + 'px'; laser.style.top = e.clientY + 'px'; }
  }, true);
  tools.addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return;
    e.stopPropagation();
    if (b.dataset.act === 'clear') clear();
    else if (b.dataset.color) setPen(b.dataset.color);
    else if (b.dataset.mode) setMode(b.dataset.mode);
    b.blur();   // so Space/→ go back to slide navigation, not re-trigger the button
  });
  addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target; if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    var k = e.key.toLowerCase();
    if (k === 'l') setMode('laser');
    else if (k === 'p') setPen(color);
    else if (k === 'e') setMode('erase');
    else if (k === 'escape') setMode('off');
    else if (k === 'delete' || (k === 'backspace')) { clear(); e.preventDefault(); }
  });
  document.addEventListener('slidechange', clear);   // ink is per-slide
})();
</script>
"""


def build_index_html(deck, title, font_css, head_styles, with_shim, auto_fs=True,
                     button_label="▶ Fullscreen", annotate=True):
    head = [
        '<!doctype html><html><head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>{title}</title>',
        f'<style>{BASE_CSS}</style>',
        f'<style>{head_styles}</style>',
    ]
    if font_css:
        head.append(f'<style>{font_css}</style>')
    head.append('</head><body>')
    rail = " no-rail" if deck.no_rail else ""
    body = [f'<deck-stage width="{deck.width}" height="{deck.height}"{rail}>',
            "\n".join(deck.sections),
            '</deck-stage>']
    if deck.notes_json:
        body.append(f'<script id="speaker-notes" type="application/json">{deck.notes_json}</script>')
    body.append(ANIM_REPLAY_SHIM)
    if annotate:
        body.append(ANNOTATE_SHIM)
    if auto_fs:
        body.append(FULLSCREEN_SHIM.replace("__BTN_LABEL__", json.dumps(button_label)))
    if with_shim:
        body.append(SYNC_SHIM)
    body.append('<script src="deck-stage.js"></script>')
    body.append('</body></html>')
    return "\n".join(head + body)


def build_launcher(with_presenter):
    open_lines = ['  open "http://127.0.0.1:$PORT/index.html"']
    if with_presenter:
        open_lines = ['  open "http://127.0.0.1:$PORT/presenter.html"',
                      '  sleep 0.3',
                      '  open "http://127.0.0.1:$PORT/index.html"']
    opens = "\n".join(open_lines)
    command = f"""#!/bin/bash
# Double-click to play this deck fully offline (local server, no internet needed).
cd "$(cd "$(dirname "$0")" && pwd)" || exit 1
PORT=8000
while lsof -i ":$PORT" >/dev/null 2>&1; do PORT=$((PORT+1)); done
python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT INT TERM
sleep 0.6
if command -v open >/dev/null 2>&1; then
{opens}
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://127.0.0.1:$PORT/index.html"
fi
echo ""
echo "  ▶  Serving at http://127.0.0.1:$PORT"
echo "     放映页（index.html）已打开 → 点右下角的全屏按钮（或按 →）即全屏"
{'echo "     演讲者视图（presenter.html）：备注 + 下一页 + 计时器，留在本机屏幕"' if with_presenter else 'true'}
echo "     关闭此窗口或按 Ctrl-C 结束放映。"
wait $SRV
"""
    return command


PRESENTER_HTML = r"""<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>演讲者视图 · __TITLE__</title>
<style>__HEADSTYLE__</style>
<style>
  html,body{height:100%}
  body{margin:0;background:#0d0f12;color:#e8eaed;font-family:-apple-system,'Noto Sans SC',sans-serif;
       display:grid;grid-template-rows:auto 1fr;height:100vh;overflow:hidden}
  .bar{display:flex;align-items:center;gap:18px;padding:12px 20px;background:#16181c;border-bottom:1px solid #23262b}
  .bar .timer{font:700 30px/1 'JetBrains Mono',monospace;letter-spacing:.04em}
  .bar .count{font:600 18px/1 'JetBrains Mono',monospace;color:#9aa6bd}
  .bar .spacer{flex:1}
  .bar button{background:#23262b;color:#e8eaed;border:1px solid #33373e;border-radius:8px;
              padding:9px 16px;font:600 15px/1 inherit;cursor:pointer}
  .bar button:hover{background:#2c3037}
  .bar button.accent{background:#1d3a8a;border-color:#1d3a8a}
  .grid{display:grid;grid-template-columns:1.55fr 1fr;gap:18px;padding:18px;min-height:0}
  .col{display:flex;flex-direction:column;min-height:0;gap:10px}
  .lbl{font:600 13px/1 'Manrope',sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#7a849a}
  .frame{background:#000;border:1px solid #23262b;border-radius:10px;overflow:hidden;position:relative}
  .stage{position:absolute;top:0;left:0;background:#fff;transform-origin:top left}
  .stage > section{position:absolute!important;inset:0!important;width:100%!important;height:100%!important;
                   box-sizing:border-box!important;overflow:hidden}
  .cur{flex:1;min-height:0}
  .nxt{height:34%;min-height:0}
  .notes{flex:1;min-height:0;background:#16181c;border:1px solid #23262b;border-radius:10px;
         padding:18px 20px;overflow:auto;font:400 22px/1.6 'Noto Sans SC',sans-serif;white-space:pre-wrap}
  .notes:empty::before{content:'（本页无演讲者备注）';color:#5b6472}
</style>
</head><body>
  <div class="bar">
    <span class="timer" id="timer">00:00</span>
    <button id="ttoggle">暂停</button>
    <button id="treset">归零</button>
    <span class="count" id="count">1 / 1</span>
    <span class="spacer"></span>
    <button id="prev">← 上一页</button>
    <button class="accent" id="next">下一页 →</button>
  </div>
  <div class="grid">
    <div class="col">
      <div class="lbl">当前页</div>
      <div class="frame cur"><div class="stage" id="curStage"></div></div>
    </div>
    <div class="col">
      <div class="lbl">下一页</div>
      <div class="frame nxt"><div class="stage" id="nxtStage"></div></div>
      <div class="lbl">演讲者备注</div>
      <div class="notes" id="notes"></div>
    </div>
  </div>
<script>
var SECTIONS = __SECTIONS_JSON__;
var NOTES = __NOTES_JSON__;
var DW = __DESIGN_W__, DH = __DESIGN_H__;
var idx = 0, total = SECTIONS.length;
var ch = (typeof BroadcastChannel !== 'undefined') ? new BroadcastChannel('deck') : null;

function fit(frameSel, stage) {
  var frame = stage.parentElement;
  var s = Math.min(frame.clientWidth / DW, frame.clientHeight / DH);
  stage.style.width = DW + 'px'; stage.style.height = DH + 'px';
  stage.style.transform = 'scale(' + s + ')';
  stage.style.left = ((frame.clientWidth - DW * s) / 2) + 'px';
  stage.style.top = ((frame.clientHeight - DH * s) / 2) + 'px';
}
function render() {
  var cur = document.getElementById('curStage'), nxt = document.getElementById('nxtStage');
  cur.innerHTML = SECTIONS[idx] || '';
  nxt.innerHTML = (idx + 1 < total) ? SECTIONS[idx + 1] : '<section style="display:flex;align-items:center;justify-content:center;font:600 64px sans-serif;color:#bbb">— 结束 —</section>';
  document.getElementById('notes').textContent = NOTES[idx] || '';
  document.getElementById('count').textContent = (idx + 1) + ' / ' + total;
  fit('.cur', cur); fit('.nxt', nxt);
}
function goLocal(i) { idx = Math.max(0, Math.min(total - 1, i)); render(); }

document.getElementById('next').onclick = function () { if (ch) ch.postMessage({type:'next'}); goLocal(idx + 1); };
document.getElementById('prev').onclick = function () { if (ch) ch.postMessage({type:'prev'}); goLocal(idx - 1); };
document.addEventListener('keydown', function (e) {
  if (e.key === 'ArrowRight' || e.key === ' ' || e.key === 'PageDown') { document.getElementById('next').click(); e.preventDefault(); }
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { document.getElementById('prev').click(); e.preventDefault(); }
});
if (ch) {
  ch.onmessage = function (m) { if (m.data && m.data.type === 'slide') { if (typeof m.data.total === 'number') total = m.data.total; goLocal(m.data.index); } };
  ch.postMessage({ type: 'hello' });
}
window.addEventListener('resize', render);

// timer
var elapsed = 0, running = true, t0 = Date.now();
function fmt(s){var m=Math.floor(s/60),x=s%60;return (m<10?'0':'')+m+':'+(x<10?'0':'')+x;}
setInterval(function(){ if(running){ elapsed = Math.floor((Date.now()-t0)/1000); document.getElementById('timer').textContent = fmt(elapsed);} }, 250);
document.getElementById('ttoggle').onclick = function(){ running=!running; if(running){t0=Date.now()-elapsed*1000;} this.textContent = running?'暂停':'继续'; };
document.getElementById('treset').onclick = function(){ elapsed=0; t0=Date.now(); document.getElementById('timer').textContent='00:00'; };

render();
</script>
</body></html>"""


def build_presenter_html(deck, title, font_css, head_styles):
    headstyle = BASE_CSS + "\n" + head_styles + ("\n" + font_css if font_css else "")
    sections_json = json.dumps(deck.sections, ensure_ascii=False).replace("</", "<\\/")
    notes_json = json.dumps(deck.notes, ensure_ascii=False).replace("</", "<\\/")
    return (PRESENTER_HTML
            .replace("__TITLE__", title)
            .replace("__HEADSTYLE__", headstyle)
            .replace("__SECTIONS_JSON__", sections_json)
            .replace("__NOTES_JSON__", notes_json)
            .replace("__DESIGN_W__", str(deck.width))
            .replace("__DESIGN_H__", str(deck.height)))


# ─────────────────────────────────────────────────────────────── main ──

def main():
    ap = argparse.ArgumentParser(description="Convert a Claude Design deck export to an offline bundle.")
    ap.add_argument("input", help="the export folder or the .dc.html file")
    ap.add_argument("-o", "--out", help="output directory (default ./<name>-offline)")
    ap.add_argument("--rail", action="store_true",
                    help="keep the thumbnail sidebar (default: hidden / full-bleed)")
    ap.add_argument("--fonts", choices=["mirror", "system"], default="mirror")
    ap.add_argument("--no-launcher", action="store_true")
    ap.add_argument("--no-presenter", action="store_true")
    ap.add_argument("--deck-index", type=int, default=None)
    ap.add_argument("--deck-stage-js", default=None)
    ap.add_argument("--no-render", action="store_true",
                    help="skip the render pass (faster, but templated slides may be incomplete)")
    ap.add_argument("--chrome", default=None, help="path to Chrome/Chromium for the render pass")
    ap.add_argument("--no-fullscreen", action="store_true",
                    help="don't show the fullscreen button on the playback page")
    ap.add_argument("--button-label", default="▶ Fullscreen",
                    help='text on the playback page fullscreen button (default: "▶ Fullscreen")')
    ap.add_argument("--no-annotate", action="store_true",
                    help="don't add the laser-pointer / pen annotation toolbar")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    render_mode = "never" if args.no_render else "auto"
    html_path, decks, head_styles, font_links = parse_export(
        args.input, args.deck_index, render_mode, args.chrome)
    src_dir = html_path.parent
    stem = re.sub(r"\.dc$", "", html_path.stem)
    out_dir = Path(args.out) if args.out else (src_dir / f"{stem}-offline")
    out_dir = out_dir.resolve()

    if out_dir == src_dir.resolve():
        sys.exit("Refusing to write into the source export directory.")
    if out_dir.exists():
        if not (out_dir.name.endswith("-offline") or args.force):
            sys.exit(f"{out_dir} exists and does not end in -offline; pass --force to overwrite.")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    log(f"Source : {html_path.name}  ({len(decks)} deck, {len(decks[0].sections)} slides, "
        f"{decks[0].width}x{decks[0].height})")
    log(f"Output : {out_dir}")

    # deck-stage.js runtime
    ds_js = locate_deck_stage_js(html_path, args.deck_stage_js)
    shutil.copy2(ds_js, out_dir / "deck-stage.js")

    # fonts
    font_css = ""
    if args.fonts == "mirror":
        font_css = mirror_fonts(font_links, out_dir / "fonts")
    if not font_css:
        log("  fonts: using system fallback (no @font-face emitted)")

    # assets
    collect_assets(decks, head_styles, src_dir, out_dir)

    with_presenter = not args.no_presenter
    multi = len(decks) > 1
    for i, deck in enumerate(decks):
        deck.no_rail = not args.rail   # full-bleed by default; --rail keeps the sidebar
        name = "index.html" if i == 0 else f"index-{i+1}.html"
        title = stem if not multi else f"{stem} ({i+1})"
        (out_dir / name).write_text(
            build_index_html(deck, title, font_css, head_styles,
                             with_presenter and not multi, auto_fs=not args.no_fullscreen,
                             button_label=args.button_label, annotate=not args.no_annotate),
            encoding="utf-8")

    # presenter (single-deck only; multi-deck keyboard/channel would collide)
    if with_presenter and not multi:
        (out_dir / "presenter.html").write_text(
            build_presenter_html(decks[0], stem, font_css, head_styles), encoding="utf-8")
    elif with_presenter and multi:
        log("  presenter: skipped (multiple decks)")

    # launcher
    if not args.no_launcher:
        cmd = build_launcher(with_presenter and not multi)
        cmd_path = out_dir / "双击放映.command"
        cmd_path.write_text(cmd, encoding="utf-8")
        cmd_path.chmod(0o755)
        sh_path = out_dir / "serve.sh"
        sh_path.write_text(cmd, encoding="utf-8")
        sh_path.chmod(0o755)

    log("")
    log("✅ Done. To present:")
    if not args.no_launcher:
        log(f'   双击  "{out_dir.name}/双击放映.command"  → 本地服务启动并打开浏览器')
    log(f'   或离线双击  "{out_dir.name}/index.html"  直接放映（单窗口）')


if __name__ == "__main__":
    main()
