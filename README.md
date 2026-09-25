# NeeDoh Stock Checker

Every 5 minutes this checks UK shops for **any NeeDoh product** and sends a push
notification to your phone when something comes into stock. It runs on GitHub's
computers ("GitHub Actions"), so it keeps working when your computer is off.

**What you get:** a push like *"NeeDoh in stock at Bigjigs Toys! NeeDoh Nice Cube, £6.99"*.
Tap it to open the product page.

**When you get alerted:**
- A product goes from **sold out** to **in stock**
- A **new** NeeDoh product appears and is in stock

You won't be alerted again for the same product until it sells out and comes back.

---

## Setup (about 10 minutes)

### Step 1: Install ntfy on your phone

[ntfy](https://ntfy.sh) is a free app that receives push notifications. You don't need an account.

1. Install **ntfy** from the [App Store (iPhone)](https://apps.apple.com/app/ntfy/id1625396347)
   or [Google Play (Android)](https://play.google.com/store/apps/details?id=io.heckel.ntfy).
2. Make up a **topic name**. This is like a private channel name. Anyone who knows it can see
   your alerts, so make it hard to guess, for example `needoh-alerts-k7q2m9x4`.
   Use only letters, numbers, `-` and `_`.
3. In the app, tap **+** (Subscribe to topic), type your topic name, and tap **Subscribe**.
   Leave the server as `ntfy.sh`.
4. **Android only:** if ntfy asks to ignore battery optimisation, say yes, so alerts arrive instantly.

Quick test: open `https://ntfy.sh/YOUR-TOPIC-NAME` in a web browser, type a message at the
bottom and press send. It should pop up on your phone.

### Step 2: Tell GitHub your topic name (as a "secret")

The code is public, but your topic name should stay private. GitHub "secrets" hide it.

1. Go to your repository: <https://github.com/pllloyd-byte/Needoh>
2. Click **Settings** (top menu of the repo) → **Secrets and variables** (left side) → **Actions**.
3. Click **New repository secret**.
   - **Name:** `NTFY_TOPIC`
   - **Secret:** your topic name from Step 1 (e.g. `needoh-alerts-k7q2m9x4`)
4. Click **Add secret**.

### Step 3: Turn on the checker

1. Click the **Actions** tab in your repository.
   If you see a green button like *"I understand my workflows, go ahead and enable them"*, click it.
2. On the left, click **Check NeeDoh stock**.
3. Click **Run workflow** (on the right), tick **"Just send a test notification to my phone"**,
   and click the green **Run workflow** button. Within a minute you should get a
   *"NeeDoh stock checker test"* notification.
4. Run it once more **without** the tick. This first real check records what's currently in
   stock and sends you a *"NeeDoh stock checker is watching"* summary. After that it runs
   by itself every 5 minutes.

That's it! 🎉

> **Important:** GitHub only runs the 5-minute schedule from the repository's **default branch**
> (usually `main`). The checker files must be on that branch. If the **Run workflow**
> button doesn't appear, that's the likely reason.

---

## Good to know

- **Timing:** GitHub treats "every 5 minutes" as a target. When GitHub is busy, runs can be
  10–15 minutes apart. That's normal.
- **Keep the repository public.** Public repositories get GitHub Actions for free. A private one
  would use up the free allowance in about a week at this frequency. The only things visible
  publicly are the code and `state.json` (a list of NeeDoh products and whether they're in stock).
  Your topic name stays secret.
- **The `state.json` file** is the checker's memory. It saves what was in stock last time, so it
  knows when something is new. The bot updates it automatically. You'll see commits called
  *"Update stock state"*. Don't delete it, or you'll get a fresh "watching" summary and lose the memory.
- **If GitHub pauses it:** GitHub turns off scheduled jobs after 60 days with no activity. The
  checker saves a small update each month to prevent this. If you ever get an email saying the
  workflow was disabled, go to **Actions → Check NeeDoh stock** and click **Enable workflow**.
- **If a shop stops working:** each run shows a table of results. Go to **Actions**, click a
  run, and scroll down. The five main shops send you a warning push if they fail for about 2 hours
  in a row, and another push when they recover. Mulberry Bush, Menkind and Urban Outfitters are
  "best effort": they may block automated checks, and if they do, the checker just logs it and
  carries on.
- **To pause alerts:** go to **Actions → Check NeeDoh stock**, click the **⋯** button, then **Disable workflow**.

## Shops checked

| Shop | How |
|---|---|
| Jukupop | Shopify product feed ([collection](https://jukupop.com/collections/needoh-squishy-fidget-toys-shop-stress-balls-fidget-fun)) |
| Funky Fidgets | Shopify product feed ([collection](https://funkyfidgetsshop.co.uk/collections/needoh-brand)) |
| So Realistic | Shopify product feed ([collection](https://www.sorealistic.co.uk/collections/needoh)) |
| Bigjigs Toys | Shopify product feed ([collection](https://www.bigjigstoys.co.uk/collections/nee-doh)), only items mentioning NeeDoh. If the collection disappears, it searches the whole shop instead |
| Plush Paradise | Shopify product feed ([collection](https://plushparadise.co.uk/collections/needoh)). Same fallback as Bigjigs |
| Mulberry Bush | Reads the [NeeDoh page](https://www.mulberrybush.co.uk/needoh) and product pages (best effort) |
| Menkind | Reads the [NeeDoh category](https://www.menkind.co.uk/toys/fidget-toys/needoh) and search (best effort) |
| Urban Outfitters | Reads [search results](https://www.urbanoutfitters.com/en-gb/search?q=needoh) (best effort, often blocks bots) |

To add or change a shop, edit `shops.json`. Any Shopify shop can be added by copying one of
the `"type": "shopify"` entries and changing the name, `base_url` and collection name.
The collection name is the part after `/collections/` in the shop's web address.

## For tinkering (optional)

You need Python 3.9+ on your computer. There's nothing else to install.

```bash
python checker.py --dry-run                              # check the shops and print everything, send nothing
NTFY_TOPIC=your-topic python checker.py --test-notification
python -m unittest discover tests                        # run the tests
```
