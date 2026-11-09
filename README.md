# Price History Tracker — Amazon & Flipkart (web version)

A public website where anyone can paste an Amazon or Flipkart product link and see its price history:
chart, lowest / highest / average price, a "good time to buy?" verdict and an optional target price.

- **No sign-up for visitors.** Each visitor's list is tied to an anonymous cookie. "Sync devices" shows a code
  that opens the same list on another phone or computer.
- **Shared history.** If someone else already tracks a product, a new visitor sees its existing history.
- **Automatic checks.** A free GitHub Actions schedule calls the site every 2 hours and it re-checks any
  product not checked in the last 6 hours.
- **Abuse limits.** Up to 30 products per visitor, add/check rate limits, and only Amazon/Flipkart
  links are ever fetched (every redirect is re-checked).

The original Windows-only version is still in [`windows-local/`](windows-local/).

## Files

| File | What it is |
|---|---|
| `app.py` | Web server (Flask): API, database, rate limits, scheduled-check endpoint |
| `scraper.py` | Reads title, price and image from Amazon / Flipkart pages |
| `static/index.html` | The website |
| `render.yaml` | Tells Render how to run the site |
| `.github/workflows/price-check.yml` | The every-2-hours automatic price check |

---

## Going live (about 20 minutes, all free)

You'll create three free accounts: **Neon** (database), **GitHub** (stores the code + runs the schedule)
and **Render** (runs the website).

### 1. Database — Neon

1. Sign up at <https://neon.com> and create a project. Pick the region closest to your users (e.g. Singapore for India).
2. On the project dashboard click **Connect** and copy the connection string. It looks like
   `postgresql://user:password@ep-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.
   Keep it private — it's the password to your database.

(Why not Render's own database? Render's free Postgres is deleted after 30 days; Neon's free plan doesn't expire.)

### 2. Code — GitHub

1. Sign up at <https://github.com> and click **New repository**. Name it e.g. `price-tracker`, choose
   **Private** (recommended: GitHub pauses schedules in public repos after 60 days without commits), and create it.
2. On the empty repo page click **uploading an existing file**, drag in everything from this folder
   **including the `.github` folder**, and click **Commit changes**.
   - If `.github` doesn't upload (Windows sometimes hides it): click **Add file → Create new file**, type the
     name `.github/workflows/price-check.yml`, paste the contents of that file, and commit.

### 3. Website — Render

1. Sign up at <https://render.com> using **Sign in with GitHub**.
2. Click **New → Blueprint**, give Render access to your `price-tracker` repo, and select it.
3. Render reads `render.yaml` and asks for **DATABASE_URL** — paste the Neon connection string. Click **Apply** / **Deploy**.
4. Wait for the deploy to finish (a few minutes). Your site's address is shown at the top, e.g.
   `https://price-history-tracker.onrender.com`. Open it and add a product to test.

### 4. Automatic price checks — GitHub Actions

1. In Render, open the service → **Environment** and copy the value of **CRON_SECRET**.
2. In GitHub, open your repo → **Settings → Secrets and variables → Actions → New repository secret** and add two secrets:
   - `APP_URL` = your Render address, e.g. `https://price-history-tracker.onrender.com`
   - `CRON_SECRET` = the value you copied
3. Go to the **Actions** tab → **Scheduled price check** → **Run workflow**. After a minute or two it should
   finish green and print something like `{"checked":3,"failed":0,"remaining":0}`.

That's it — share your Render link.

## Updating the site later

Edit or re-upload a file on GitHub (**Add file → Upload files**) and commit. Render redeploys automatically.

## Things to know

- **Store blocking.** Amazon and Flipkart sometimes block requests from cloud servers. When that happens
  the product shows "Check failed" with the reason and is retried on the next schedule. If it happens all
  the time, the site still works but prices update less reliably.
- **First visit can be slow.** Render's free plan puts the site to sleep after 15 minutes without visitors;
  the next visit takes about a minute to wake it.
- **Free-plan limits.** Render free: 750 hours/month (enough for one site). Neon free: 1 GB storage — the app
  only stores a price when it changes (or once a day), so that's plenty for thousands of products.
- **No email alerts yet.** Target prices are shown on the site ("Target hit"); there are no emails or push notifications.
- **Sync codes are like passwords.** Anyone with a visitor's code can view and edit that list.
- History starts when the first person adds a product; past prices can't be fetched.
