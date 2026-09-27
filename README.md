# REPOCKET

Two tools for REPOCKET — Refurbished Mobiles & Devices Trader:

- **[Trade Desk](docs/TRADE_DESK.md)**: a WhatsApp assistant that talks to ITAD suppliers and device
  buyers in Deepak's voice, qualifies them, collects stock lists and requirements, and shows which trades
  clear your margin. Python/FastAPI + Claude + WhatsApp Cloud API. Setup guide: [`docs/TRADE_DESK.md`](docs/TRADE_DESK.md).
- **Stock Blast Studio** (below): branded stock posters for WhatsApp, LinkedIn and email. The Trade Desk
  can open it pre-filled with the lines you want to blast.

---

# Stock Blast Studio

A lightweight, browser-based tool for creating branded **stock-blast** posts for
**REPOCKET — Refurbished Mobiles & Devices Trader**. Fill in your stock, pick an
orientation and colour theme, then copy the image to your clipboard or download it
as a JPG — ready to blast on WhatsApp, LinkedIn, Instagram or email.

**Live app:** https://deepak-mobi-hub.github.io/stock-post/

## Features

- **Live poster preview** with the REPOCKET logo baked in.
- **4 orientations** — Square (1:1), Post (4:5), Story (9:16) and Wide (16:9), sized
  for social + messaging.
- **8 colour themes** — brand blue, navy, midnight, electric, emerald, sunset, slate
  and a clean light look.
- **Editable stock list** — model, storage, grade, quantity and price per line
  (up to 8 lines), with quick headline presets (Stock Blast, New Arrivals, Price
  Drop, Flash Sale, Bulk Offer).
- **One-click copy** — puts the image straight on your clipboard to paste anywhere.
- **Download** as JPG or PNG.
- **100% client-side** — everything renders in your browser on an HTML canvas.
  No uploads, no accounts, no dependencies.

## How to use

1. Open the live app (link above).
2. Choose an **orientation** and **colour theme**.
3. Set your **headline** (or tap a preset) and edit the **stock lines**.
4. Add your **contact details** in the footer fields.
5. Hit **Copy image** to paste into a chat, or **Download JPG** to save.

## Running locally

No build step — it's a single `index.html`. Just open it in a browser, or serve it:

```bash
python3 -m http.server 8000
# then visit http://localhost:8000
```

## Deployment

Pushing to the deployment branch triggers the GitHub Actions workflow in
`.github/workflows/deploy.yml`, which publishes the site to GitHub Pages.

> First-time setup: in **Settings → Pages**, set **Source** to **GitHub Actions**
> if it isn't already. The workflow attempts to enable this automatically on its
> first run.
