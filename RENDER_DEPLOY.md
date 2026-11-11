# Deploy the Affiliate Engine on Render (free tier)

## What this is
The engine code, ready to deploy on Render's free tier. Once live, it gets a
public URL like `https://affiliate-engine-xxxx.onrender.com`. That URL is what
the short links point at, and what sportsbooks send postbacks to.

## Honest limits of the free tier
- **It sleeps when idle.** After ~15 minutes with no traffic, the first
  request takes about 50 seconds to wake it up. Fine for links and postbacks.
- **Its database resets on restarts.** The engine stores clicks and
  conversions in a small SQLite file that lives only while the service runs.
  A restart or redeploy wipes it. Nothing live is flowing yet, so nothing is
  lost — when real money starts flowing we move the data somewhere permanent.
- **No card needed.** The free tier asks for no payment details.

## Steps (you do these — they need your accounts)

### 1. Make a free Render account
Go to https://render.com and sign up with your email. Free, no card.

### 2. Make a free GitHub account (skip if you have one)
Go to https://github.com/signup. Then create a new repository called
`affiliate-engine` (public or private, either works).

### 3. Upload the code to GitHub
Unzip the package Alfie gave you on your computer. On your new repo page,
drag the whole unzipped folder into the browser window (or use
Add file → Upload files) and commit. The package contains only:
`affiliate_engine/`, `requirements.txt`, `render.yaml`,
`RENDER_DEPLOY.md`, `README.md`. It contains NO secrets — no `.env`,
no passwords, no payout addresses.

### 4. Deploy on Render
In Render: New → Web Service → connect your GitHub account and pick the
`affiliate-engine` repo. Render finds `render.yaml` and fills in the
settings itself (free plan, install, start command). Click Deploy.
It takes a few minutes the first time.

### 5. Send Alfie the URL
When it is live, Render shows a URL like
`https://affiliate-engine-xxxx.onrender.com`. Send it to Alfie and he will:
- point the short-link host at it,
- regenerate the landing-page links,
- check the live `/health` page.

## If the deploy fails
Copy the error from Render's deploy log and send it to Alfie. The most
likely fixes are the Python version or the start command — both are in
`render.yaml` and take a minute to change.
