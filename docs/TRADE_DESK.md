# RePocket Trade Desk: WhatsApp assistant

The Trade Desk talks to ITAD suppliers and device buyers on WhatsApp in Deepak's voice. It builds the
relationship, asks qualifying questions, collects stock lists and buyer requirements, and tells you which
trades clear your margin.

## What it does

| | |
|---|---|
| **Talks like you** | Replies follow `tradedesk/persona/playbook.md`: your tone, your company facts, your qualifying questions. Every draft you edit and every message you type yourself is fed back as a style example. |
| **Qualifies** | Works out supplier / buyer / both, then asks one or two natural questions per message: what they handle, volumes, grades, data wipe & locks, location, VAT, how they sell, what they want to buy, target prices, keyboard layouts. |
| **Collects stock** | Reads Excel / CSV stock lists sent on WhatsApp (header on any row, several tabs, "Price EUR" columns, "Cosmetic" grades), and PDFs or photos of lists via Claude. A supplier's new list replaces their old one. |
| **Finds trades** | Matches supplier stock against buyer requirements (same model, storage, acceptable grade), converts currencies, deducts costs and shows margin, profit and the **max bid** when a supplier says "make me an offer". |
| **Keeps you in control** | *Approve* mode (default): every reply comes to your WhatsApp as a draft; reply `send 12` to send it. *Auto* mode: routine replies go out on their own, but anything about prices, deals or meetings still comes to you. |
| **Never gives away the desk** | Never quotes prices or bids, never names suppliers/buyers to each other, never invents stock. Stock blasts show your sell price only. |
| **Follows up** | Nudges at 3, 7 and 14 days (templates when WhatsApp's 24h window has closed), then marks the lead dormant. |
| **Outreach** | Import a CSV of people who agreed to hear from you; it sends your approved intro template (daily limit, weekdays only) and handles the replies. |
| **Daily digest** | 7am summary on your WhatsApp: new conversations, drafts waiting, best trades, follow-ups due. |

### Your WhatsApp commands

Send these from your own WhatsApp (the number in `OWNER_WHATSAPP`) to the business number:

```
drafts                  list drafts waiting for you
send 12                 send draft 12 as written
edit 12 <your text>     send your version instead (it learns from this)
skip 12                 discard draft 12
opps                    best trades right now
stock latitude 5420     who has it, at what price
say Markus: <text>      message a contact yourself
pause Markus / resume Markus
digest
```

**Stock lists that arrive by email:** forward the file to the business number from your WhatsApp with the
caption `from Markus` (it's his stock) or `wants Elena` (it's her want list). It's read, filed under that
contact and matched straight away.

### The dashboard

Open your app's URL and log in with user `deepak` + `DASHBOARD_PASSWORD`.

- **Today**: drafts to approve, things that need you, headline numbers
- **Deals**: every supplier ↔ buyer match with margin, profit and max bid; tick lines and create a buyer-facing stock blast (text + one click into Stock Blast Studio)
- **Pipeline**: every contact, stage, what we know about them, full conversation, their stock and wants
- **Stock / Demand**: all supplier lines and buyer requirements; upload lists that came in by email
- **Outreach**: import prospects, see the queue
- **Simulator**: chat with the assistant as a pretend supplier or buyer before it talks to anyone real
- **Settings**: approve/auto mode, target margin, costs, FX

## Setting it up

You need four things: a WhatsApp Business number on Meta's Cloud API, a Claude API key, somewhere to run
the app, and about an hour.

### 1. Claude API key
Create one at <https://console.anthropic.com> → API keys. The assistant uses Claude Opus 5 by default,
with a server-side fallback in case a request is declined. Expect a few cents per reply (the playbook is
prompt-cached); set a monthly spend limit in the console. `CLAUDE_EFFORT=low` or a cheaper `CLAUDE_MODEL`
cut costs further if the replies still sound right to you.

### 2. WhatsApp Business (Meta)
1. In [Meta Business Suite](https://business.facebook.com) make sure Mobi-Hub has a verified business.
2. At <https://developers.facebook.com> → **Create app** → type **Business** → add the **WhatsApp** product.
3. **WhatsApp → API Setup**: add your business phone number (a number not currently registered on the
   normal WhatsApp app, or migrate one). Note the **Phone number ID**.
4. **Business Settings → System users**: create a system user, give it the app with
   `whatsapp_business_messaging` and `whatsapp_business_management`, and generate a **permanent token**.
5. **App settings → Basic**: copy the **App secret**.

### 3. Run it
The simplest host is [Render](https://render.com): **New → Blueprint**, pick this repo, and fill in:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from step 1 |
| `WHATSAPP_TOKEN` | permanent token from step 2.4 |
| `WHATSAPP_PHONE_NUMBER_ID` | from step 2.3 |
| `WHATSAPP_APP_SECRET` | from step 2.5 |
| `OWNER_WHATSAPP` | your own number, e.g. `+44 7…` |
| `WHATSAPP_VERIFY_TOKEN`, `DASHBOARD_PASSWORD` | Render generates these; copy them from the service's Environment tab |

It needs the Starter plan because the database sits on a persistent disk. Any host that runs a Docker
container with a persistent volume works too (Railway, Fly.io, a small VPS). Run a single instance.

### 4. Connect the webhook
Meta app → **WhatsApp → Configuration → Webhook**:
- Callback URL: `https://<your-app>.onrender.com/webhook`
- Verify token: your `WHATSAPP_VERIFY_TOKEN`
- Subscribe to the **messages** field.

### 5. Submit your message templates
WhatsApp only lets a business start a conversation (or write after 24h of silence) with pre-approved
templates. In **WhatsApp Manager → Message templates** create:

**`repocket_intro`**, category *Marketing*, English
> Hi {{1}} 👋 I'm Deepak from Mobi-Hub. We're building RePocket, our laptops, MacBooks & tablets line
> across Europe, and we both buy and sell. Would you be open to swapping stock lists to see where there's
> a good fit?

Buttons: quick reply "Yes, let's connect" and the marketing opt-out button.

**`repocket_followup`**, category *Marketing*, English
> Hi {{1}}, just floating my note back up in case it slipped past. Happy to share our current stock or
> take a look at yours whenever suits. Deepak

**`tradedesk_alert`** (optional), category *Utility*, English, then set `WA_TEMPLATE_OWNER_ALERT=tradedesk_alert`
> Trade Desk update: {{1}}. Reply here to see details.

If you change the wording in Meta, update `TEMPLATE_PREVIEWS` in `tradedesk/engine.py` so the transcript
the assistant reads matches what was actually sent.

### 6. Go live gently
1. Message the business number from your own WhatsApp: "hi". Alerts and drafts reach you as normal
   messages for 24h after you last wrote (or via the `tradedesk_alert` template).
2. Use the **Simulator** to rehearse a few supplier and buyer conversations. Adjust the playbook until it
   sounds like you.
3. Stay in **Approve** mode for the first couple of weeks. Every edit you make teaches it.
4. Switch to **Auto** in Settings when you're happy. Commercial topics still come to you.

## Before you switch it on

- **Internal sign-off.** Get the playbook and the two templates approved internally before the assistant
  writes to anyone on the company's behalf.
- **WhatsApp rules.** Only message people who gave you their number and are happy to hear from you on
  WhatsApp. The outreach importer only queues contacts marked as consented. Quality ratings drop fast when
  people block or report a number, so keep the daily limit modest.
- **Opt-outs are automatic.** "STOP", "unsubscribe", "don't contact me" (and German/French/Spanish
  equivalents) are confirmed once, in your words, and the person is removed from all follow-ups and outreach.
- **Being clear it's an assistant.** In Auto mode the first message it sends without your review says it's
  your assistant, and it never denies being one when asked. The EU AI Act's transparency rules for AI that
  talks to people are due to apply from August 2026; check the current position with whoever handles legal.
- **GDPR.** Data lives in your own database. "Erase their data" on a contact deletes everything but keeps
  their number suppressed so they're never contacted again.
- **The repo is public.** Don't commit contacts, prices or a `.env` file. To keep your playbook private,
  put it on the server and set `PLAYBOOK_PATH`.

## Running locally

```bash
pip install -r requirements-dev.txt
DEV_MODE=true ANTHROPIC_API_KEY=sk-... uvicorn tradedesk.main:app --reload
# open http://localhost:8000 and try the Simulator
pytest -q
```

Without WhatsApp credentials the app runs in dry-run mode: nothing is sent, which is fine for the
dashboard and Simulator.

## How it's built

```
tradedesk/
  persona/playbook.md   voice, company facts, qualifying questions, hard rules  ← edit this
  brain.py              one Claude call per turn → reply + structured updates (JSON schema enforced)
  engine.py             inbound → debounce → opt-out → stock files → Claude → draft/send → match → alert
  matcher.py            supplier stock × buyer requirements → margin, profit, max bid
  normalize.py          grades, model keys ("iPhone13 Pro 128gb A+" == "Apple iPhone 13 Pro (128 GB)"), prices
  stock.py              Excel/CSV stock list reader
  whatsapp.py           Meta Cloud API client, webhook parsing & signature check
  app.py / main.py      FastAPI: webhook, dashboard API, scheduler (follow-ups, outreach, digest)
  static/dashboard.html the dashboard
```
