# 📸 Snapdeck

**Turn a Claude Design deck into a fully-offline presentation you can run anywhere — one command, no server, no internet, with a built-in presenter view.**

Claude Design decks (`*.dc.html` exports) normally need a local server *and* a live internet connection (Google Fonts) to present, and they have no speaker-notes view. Snapdeck takes one of those exports and produces a self-contained folder you just double-click to present — fonts, images, GIFs and the deck engine all bundled in.

---

## What you get

Running Snapdeck on an export produces a `*-offline/` folder containing:

| File | What it is |
|------|------------|
| `index.html` | The deck — full-bleed, keyboard nav (`←/→/Space/digits`), animations & GIFs, with a **▶ 全屏放映 / Fullscreen** button. Works by double-click, **fully offline**. |
| `presenter.html` | **Presenter view** — current slide, next slide, speaker notes, and a timer; stays in sync with the audience window. |
| `双击放映.command` | One-click launcher (macOS): starts a local offline server and opens both windows. `serve.sh` is the portable equivalent. |
| `fonts/ assets/ uploads/` | All fonts and referenced media, localized so nothing is fetched from the network. |
| `deck-stage.js` | The deck runtime, copied from your export. |

## Quick start

```bash
# 1. install the one dependency
python3 -m pip install lxml

# 2. convert your export (a folder, or the .dc.html inside it)
python3 snapdeck.py path/to/your-deck.dc.html

# 3. present
open your-deck-offline/双击放映.command     # macOS one-click
#    → 放映页打开后，点右下角「▶ 全屏放映」即可全屏开讲；演讲者视图在另一屏看备注
```

> The conversion step needs internet **once** (to download fonts and render the deck). The resulting bundle is 100% offline — verify it by turning off Wi-Fi and opening `index.html`.

## Options

```
python3 snapdeck.py <export-dir-or-.dc.html> [options]

  -o, --out DIR        output directory (default ./<name>-offline)
  --rail               keep the thumbnail sidebar (default: hidden / full-bleed)
  --fonts mirror|system  mirror = download the real fonts (default); system = use the OS fonts
  --no-presenter       skip presenter.html
  --no-launcher        skip the .command / serve.sh launcher
  --no-fullscreen      don't show the fullscreen button on the playback page
  --no-render          skip the render pass (faster, but template-driven slides may be incomplete)
  --chrome PATH        Chrome/Chromium binary for the render pass
  --deck-index N       pick one deck when a file contains several
```

## How it works

1. **Render once, snapshot the result.** Some slides are built from the dc-runtime's `{{…}}` templates/components, so Snapdeck renders the live deck once in headless Chrome and captures the *finished* DOM — no half-empty template slides.
2. **Cut the cord.** It mirrors the Google Fonts locally, copies only the referenced images/GIFs, and inlines everything so the page makes **zero network requests**.
3. **Keep the magic.** The original `deck-stage` engine drives playback (scaling, keyboard nav, print-to-PDF), and a tiny shim **replays each slide's animations when you arrive on it** — so entrance/looping motion plays during the talk, not silently at load.
4. **Add a presenter.** A generated `presenter.html` mirrors the audience deck over a `BroadcastChannel`, showing the next slide, your notes, and a timer.

## Requirements

- **Python 3.8+** and **lxml** (`pip install lxml`).
- **Google Chrome / Chromium** — for the render pass and for `--also`-style headless work. (Skip with `--no-render` if your deck has no templates.)
- **Internet at convert time** — to fetch fonts and render. Not needed afterwards.
- A **Claude Design export** as input: a folder containing the `*.dc.html` plus its `deck-stage.js`.

## Notes & limits

- Snapdeck does **not** ship `deck-stage.js` — that's Claude Design's runtime; it's read from *your* export and copied into *your* bundle.
- A picture-heavy deck stays picture-heavy: if your GIFs are 80 MB, the bundle is ~80 MB. (Convert big GIFs to MP4 beforehand if you want it smaller.)
- Programmatic fullscreen isn't allowed on page load by any browser, so the playback page enters fullscreen on your first click / `→` (that's what the button is for).

---

## 中文快速开始

把 Claude Design 导出的 deck 一键转成**完全离线**、可直接放映的文件夹：

```bash
python3 -m pip install lxml                 # 装唯一依赖
python3 snapdeck.py 你的deck.dc.html         # 转换（转换时需联网一次）
open 你的deck-offline/双击放映.command        # 双击放映
```

产物：`index.html`（全屏放映页，点右下角「▶ 全屏放映」即全屏）、`presenter.html`（演讲者视图：当前页 + 下一页 + 备注 + 计时器，双屏同步）、本地字体/图片/GIF，断网也能放。原理：先用无头 Chrome 把带模板的 deck 渲染展开并快照，再把字体和素材本地化、动画在切到该页时重新播放。

---

## License

[MIT](LICENSE)
