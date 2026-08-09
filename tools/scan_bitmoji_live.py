"""Live Bitmoji editor scanner — enumerate every feature panel via AdsPower.

Connects to a running AdsPower profile with the Bitmoji avatar editor open at
https://www.bitmoji.com/avatar/create, walks every feature panel using the
editor's own ``#arrow_btn_forward`` navigation, scrolls each virtualised
``.traits-container.scrollable`` to the bottom, and records every option:

  * img features  -> the distinct value of the query param that varies across
    the tile preview images (e.g. ``hair``, ``hair_tone``, ``nose``, ``top``).
  * colour features -> the distinct SVG swatch ``fill`` hexes.
  * outfit features -> the garment item ids plus the colour swatches shown.

Writes a complete ``data/bitmoji_catalog.json`` (overwriting the partial
MHTML-built one). The editor runs inside the ``sdk.bitmoji.com/web-builder``
iframe; everything below is scoped to that frame.

Usage:
    python tools/scan_bitmoji_live.py            # uses profile k1djc93n
    python tools/scan_bitmoji_live.py <user_id>

Requires AdsPower running with the profile open at the avatar editor, and the
Nyx Suite venv (Playwright installed).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.bitmoji_config import CATALOG_PATH, FEATURES, feature_groups, feature_order  # noqa: E402

PROFILE_ID = sys.argv[1] if len(sys.argv) > 1 else "k1djc93n"
ADSPOWER = "http://local.adspower.net:50325"
EDITOR_HINT = "bitmoji.com/avatar"
FRAME_HINT = "sdk.bitmoji.com/web-builder"

# Params that are constant chrome on every preview URL, never an option id.
CONST_PARAMS = {"scale", "rotation", "cacheable", "ua", "gender", "style", "flow_mode", "client"}

# Panels that are not avatar features — never include them.
SKIP_IDS = {"save", "my_closet", "mycloset"}

# Editor category id -> our FEATURES key (when they differ). Most match 1:1.
ID_ALIASES = {
    "hair": "hair_style",
    "hair_tone": "hair_color",
    "hair_treatment_tone": "hair_treatment",
    "eye": "eye_shape",
    "eyelash": "eye_lashes",
    "pupil_tone": "eye_color",
    "brow": "eyebrows",
    "brow_tone": "eyebrow_color",
    "breast": "chest_size",
    "earring_dual": "paired_earring",
    "nosering": "nose_piercings",
    "eyeshadow_tone": "eyeshadow",
    "blush_tone": "blush",
    "lipstick_tone": "lipstick",
    "hat": "headwear",
    "outfit": "outfits",
    "top": "tops",
    "bottom": "bottoms",
    "one_piece": "dresses",
    "mouth": "lips",
    "earrings": "paired_earring",
    "paired_earrings": "paired_earring",
    "piercings": "nose_piercings",
    "nose_piercing": "nose_piercings",
    "eyelashes": "eye_lashes",
    "brow": "eyebrows",
    "brow_color": "eyebrow_color",
    "brow_tone": "eyebrow_color",
    "body": "body_shape",
    "chest": "chest_size",
    "face": "face_shape",
    "face_proportion": "face_shape",
}


def _get(url: str, timeout: int = 20) -> dict:
    return json.loads(urllib.request.urlopen(url, timeout=timeout).read())


def ws_endpoint() -> str:
    data = _get(f"{ADSPOWER}/api/v1/browser/local-active", timeout=15)
    for item in (data.get("data") or {}).get("list", []):
        if item.get("user_id") == PROFILE_ID:
            return item["ws"]["puppeteer"]
    data = _get(f"{ADSPOWER}/api/v1/browser/start?user_id={PROFILE_ID}")
    if data.get("code") != 0:
        raise SystemExit(f"AdsPower could not start {PROFILE_ID}: {data.get('msg')}")
    return data["data"]["ws"]["puppeteer"]


def editor_frame(page):
    for fr in page.frames:
        if FRAME_HINT in fr.url:
            return fr
    return None


# ---- in-frame JS helpers -------------------------------------------------

JS_ACTIVE_CAT = r"""() => {
  // The visible feature panel: the .avatar-builder-category nearest the panel's
  // left edge with real width.
  let best=null, bestd=1e9;
  for (const c of document.querySelectorAll('.avatar-builder-category')) {
    const r=c.getBoundingClientRect();
    if (r.width>200) { const d=Math.abs(r.left-1090); if (d<bestd){bestd=d;best=c;} }
  }
  if (!best) return null;
  best.setAttribute('data-nyx-active','1');
  const title=(document.querySelector('.category-title .title')||{}).textContent||'';
  return {id: best.id||'', title: title.trim()};
}"""

JS_CLEAR_ACTIVE = r"""() => { document.querySelectorAll('[data-nyx-active]').forEach(e=>e.removeAttribute('data-nyx-active')); }"""

JS_SCROLL_TO = r"""(top) => {
  const c=document.querySelector('[data-nyx-active] .traits-container.scrollable')
        || document.querySelector('[data-nyx-active]');
  if (!c) return {h:0,t:0,ch:0};
  c.scrollTop = top;
  return {h:c.scrollHeight, t:c.scrollTop, ch:c.clientHeight};
}"""

JS_COLLECT = r"""() => {
  // Scoped to the active feature panel, so the big avatar preview (which lives
  // outside .avatar-builder-category) is naturally excluded — no path filter.
  const root=document.querySelector('[data-nyx-active]') || document;
  const imgs=[...root.querySelectorAll('img[src*="preview.bitmoji.com"]')].map(i=>i.src);
  const fills=[...root.querySelectorAll('rect[fill],circle[fill]')]
      .map(e=>e.getAttribute('fill')).filter(f=>/^#[0-9a-f]{6}$/i.test(f));
  return {imgs, fills};
}"""

JS_BASE_AVATAR = r"""() => {
  const img=document.querySelector('img[src*="/avatar/body"]');
  return img ? img.src : "";
}"""

# Outfit colour picker: a vertical strip of div.colour-picker-option with a
# background-color (the standard Bitmoji colour wheel, shared by all garments).
JS_PICKER_COUNT = r"""() => document.querySelectorAll('.colour-picker-option').length"""
JS_CLICK_FIRST_TILE = r"""() => {
  const root=document.querySelector('[data-nyx-active]');
  if(!root) return false;
  const t=[...root.querySelectorAll('[tabindex="0"]')].find(e=>{const r=e.getBoundingClientRect(); return r.x>1000&&r.y>180&&r.width>30;});
  if(t){ t.click(); return true; } return false;
}"""
JS_PICKER_SCROLL = r"""(top) => {
  const opt=document.querySelector('.colour-picker-option');
  const c=opt ? opt.closest('[class*="colour"],[class*="picker"]') : null;
  const sc=(c && c.scrollHeight>c.clientHeight) ? c : (c ? c.parentElement : null);
  if(sc){ sc.scrollTop=top; return {h:sc.scrollHeight, ch:sc.clientHeight}; }
  return {h:0, ch:0};
}"""
JS_PICKER_COLORS = r"""() => {
  const toHex=(s)=>{ const m=s.match(/\d+/g); if(!m||m.length<3) return null;
    return '#'+m.slice(0,3).map(n=>(+n).toString(16).padStart(2,'0')).join(''); };
  const out=[];
  document.querySelectorAll('.colour-picker-option').forEach(e=>{
    const h=toHex(getComputedStyle(e).backgroundColor||''); if(h) out.push(h);
  });
  return out;
}"""

JS_FWD_HAS = r"""() => { const a=document.querySelector('#arrow_btn_forward'); return !!(a && a.querySelector('img')); }"""
JS_BACK_HAS = r"""() => { const a=document.querySelector('#arrow_btn_back'); return !!(a && a.querySelector('img')); }"""
JS_CLICK_FWD = r"""() => { const a=document.querySelector('#arrow_btn_forward'); if(a) a.click(); }"""
JS_CLICK_BACK = r"""() => { const a=document.querySelector('#arrow_btn_back'); if(a) a.click(); }"""


def varying_param(urls: list[str]):
    """Return (param, {value: preview_url}) for the param that varies across tiles."""
    from urllib.parse import urlparse, parse_qs
    vals: dict[str, dict] = {}
    for u in urls:
        try:
            q = parse_qs(urlparse(u).query)
        except Exception:
            continue
        for k, v in q.items():
            if k in CONST_PARAMS:
                continue
            vals.setdefault(k, {})
            vals[k][v[0]] = u
    # the option param is the one with the most distinct values
    best, best_n = None, 1
    for k, m in vals.items():
        if len(m) > best_n:
            best, best_n = k, len(m)
    return (best, vals.get(best, {})) if best else (None, {})


def collect_feature(fr):
    """Sweep the active panel in small steps so every virtualised row passes
    through the render window, re-sweeping until the distinct count is stable."""
    imgs: dict[str, str] = {}
    fills: list[str] = []
    seen_fill = set()

    def grab():
        data = fr.evaluate(JS_COLLECT)
        for u in data["imgs"]:
            imgs[u] = u
        for f in data["fills"]:
            lf = f.lower()
            if lf not in seen_fill:
                seen_fill.add(lf); fills.append(lf)

    STEP = 140  # px — smaller than a tile row so nothing is skipped
    prev_total = -1
    for sweep in range(4):  # up to 4 full top->bottom sweeps
        s = fr.evaluate(JS_SCROLL_TO, 0)
        time.sleep(0.15)
        grab()
        h = s.get("h", 0)
        ch = s.get("ch", 0)
        top = 0
        guard = 0
        while top + ch < h - 2 and guard < 400:
            top += STEP
            s = fr.evaluate(JS_SCROLL_TO, top)
            h = s.get("h", h)
            ch = s.get("ch", ch)
            time.sleep(0.06)
            grab()
            guard += 1
        grab()
        total = len(imgs) + len(fills)
        if total == prev_total:
            break  # no new options across a whole sweep
        prev_total = total
    return list(imgs.values()), fills


def capture_palette(fr) -> list[str]:
    """Capture the outfit colour wheel (.colour-picker-option backgrounds).

    The picker only appears once a garment is selected, so click the first tile
    if needed. The palette is the same standard wheel for every garment, so we
    capture it once and reuse it. Scrolls the strip to gather the full set.
    """
    if fr.evaluate(JS_PICKER_COUNT) == 0:
        fr.evaluate(JS_CLICK_FIRST_TILE)
        time.sleep(1.2)
    colors: list[str] = []
    seen: set[str] = set()

    def grab():
        for c in fr.evaluate(JS_PICKER_COLORS):
            if c not in seen:
                seen.add(c); colors.append(c)

    grab()
    top = 0
    for _ in range(60):
        s = fr.evaluate(JS_PICKER_SCROLL, top)
        h, ch = s.get("h", 0), s.get("ch", 0)
        grab()
        if not h or top + ch >= h - 2:
            break
        top += 160
        time.sleep(0.05)
    return colors


def resolve_key(cat_id: str, title: str) -> str:
    cid = (cat_id or "").strip().lower()
    if cid in FEATURES:
        return cid
    if cid in ID_ALIASES:
        return ID_ALIASES[cid]
    slug = (title or "").strip().lower().replace(" ", "_").replace("-", "_")
    if slug in FEATURES:
        return slug
    if slug in ID_ALIASES:
        return ID_ALIASES[slug]
    return cid or slug


def main():
    ws = ws_endpoint()
    print(f"Profile {PROFILE_ID} CDP: {ws}")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(ws)
        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                if EDITOR_HINT in p.url:
                    page = p
        if not page:
            raise SystemExit("No Bitmoji editor page open. Open the avatar editor in the profile.")
        page.bring_to_front()
        fr = editor_frame(page)
        if not fr:
            raise SystemExit("Editor iframe (sdk.bitmoji.com/web-builder) not found.")

        base_avatar = fr.evaluate(JS_BASE_AVATAR)

        # Rewind to the first feature. Click back unconditionally — the back arrow
        # briefly loses its img child mid-transition, so a has-img check stops early.
        # Clicking an already-disabled arrow is a harmless no-op.
        for _ in range(60):
            fr.evaluate(JS_CLICK_BACK)
            time.sleep(0.18)
        time.sleep(0.5)

        features: dict[str, dict] = {}
        order: list[str] = []
        seen_ids: set[str] = set()
        outfit_palette: list[str] = []  # shared colour wheel, captured once
        print("\nScanning features (forward through the editor)...\n")
        for _ in range(60):
            fr.evaluate(JS_CLEAR_ACTIVE)
            cat = fr.evaluate(JS_ACTIVE_CAT)
            if not cat:
                break
            cid, title = cat["id"], cat["title"]
            marker = cid or title
            if marker in seen_ids:
                break
            seen_ids.add(marker)

            if (cid or "").strip().lower() in SKIP_IDS:
                print(f"  (skip non-feature panel id={cid})")
                if not fr.evaluate(JS_FWD_HAS):
                    break
                fr.evaluate(JS_CLICK_FWD); time.sleep(0.6)
                continue

            key = resolve_key(cid, title)
            imgs, fills = collect_feature(fr)
            param, val_map = varying_param(imgs)

            meta = FEATURES.get(key, {})
            declared_kind = meta.get("kind")
            if fills and not val_map:
                kind = "color"
                options = [{"id": f} for f in fills]
            elif val_map:
                kind = declared_kind if declared_kind in ("img", "outfit") else "img"
                options = [{"id": v, "preview": u} for v, u in val_map.items()]
                if kind == "outfit":
                    if not outfit_palette:
                        pal = capture_palette(fr)
                        if pal:
                            outfit_palette = pal
                            print(f"  (captured outfit colour wheel: {len(pal)} colours)")
                    if outfit_palette:
                        for o in options:
                            o["colors"] = list(outfit_palette)
            elif fills:
                kind = "color"
                options = [{"id": f} for f in fills]
            else:
                kind = declared_kind or "img"
                options = []

            if not options:
                print(f"  (skip empty id={cid} title={title!r})")
                if not fr.evaluate(JS_FWD_HAS):
                    break
                fr.evaluate(JS_CLICK_FWD); time.sleep(0.6)
                continue

            label = meta.get("label") or (title or key.replace("_", " ").title())
            features[key] = {"label": label, "type": kind, "options": options,
                             "editor_id": cid, "param": param or meta.get("param", "")}
            order.append(key)
            print(f"  {key:16s} id={cid or '-':14s} {kind:6s} {len(options):3d} opts  param={param}")

            if not fr.evaluate(JS_FWD_HAS):
                break
            fr.evaluate(JS_CLICK_FWD)
            time.sleep(0.6)

        # Preserve our curated order/groups; append any newly-discovered keys.
        curated = [k for k in feature_order() if k in features]
        extras = [k for k in order if k not in curated]
        final_order = curated + extras

        CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CATALOG_PATH.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": f"live-scan:{PROFILE_ID}",
            "base_avatar": base_avatar,
            "features": features,
            "feature_order": final_order,
            "groups": feature_groups(),
        }, indent=2), encoding="utf-8")
        print(f"\nWrote {CATALOG_PATH} — {len(features)} features (live-scanned)")
        if base_avatar:
            print("base_avatar captured.")
        else:
            print("WARNING: base_avatar empty.")


if __name__ == "__main__":
    main()
