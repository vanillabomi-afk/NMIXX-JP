# XBot (GitHub Actions edition) — no computer required

This version runs entirely on GitHub's free servers. There's no terminal,
no coding, and nothing to keep running on your own machine. GitHub checks
the X account every 5 minutes and posts new activity straight to a Discord
channel via a webhook.

Everything below is done by clicking around on websites.

## What you'll need

- A free [GitHub](https://github.com) account
- A Discord server where you can create webhooks (usually means you're an
  admin/mod there, or the owner)

## Step 1 — Create a GitHub repository with these files

1. Go to https://github.com/new
2. Name it anything (e.g. `x-discord-bot`). Set it to **Private** (recommended —
   keeps your setup out of public view) or Public, either works.
3. Click **Create repository**.
4. On the new repo's page, click **Add file → Upload files**.
5. Drag in every file and folder from this project (`check_and_post.py`,
   `requirements.txt`, `state.json`, and the whole `.github` folder — make
   sure the folder structure is preserved, i.e. it ends up as
   `.github/workflows/check.yml` inside the repo, not loose at the top level).
6. Scroll down, click **Commit changes**.

> If GitHub's upload page flattens your `.github` folder or won't accept
> it via drag-and-drop, use "Create new file" instead and type the path
> `.github/workflows/check.yml` into the filename box — GitHub will create
> the folders for you — then paste in that file's contents.

## Step 2 — Create a Discord webhook

This is simpler than a full bot — no invite links, no permissions to manage.

1. In Discord, go to the channel you want posts sent to.
2. Click the gear icon (**Edit Channel**) → **Integrations** → **Webhooks** → **New Webhook**.
3. Give it a name (e.g. "X Updates") if you like.
4. Click **Copy Webhook URL**. Keep this handy — you'll paste it in Step 3.

## Step 3 — Add your settings as repo secrets

Secrets are how you give the workflow your webhook URL and settings
without putting them in plain text in the code.

1. In your GitHub repo, go to **Settings** (top tab of the repo, not your
   account settings) → **Secrets and variables** → **Actions**.
2. Click **New repository secret** and add each of these one at a time:

| Secret name | Value | Required? |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | the webhook URL you copied in Step 2 | yes |
| `X_USERNAME` | the X/Twitter username to watch, no `@` (e.g. `nasa`) | yes |
| `INCLUDE_REPLIES` | `true` or `false` | no — defaults to `false` |
| `MENTION_ROLE_ID` | a Discord role ID to @mention on new posts | no — leave unset for none |

(To get a role ID: enable Developer Mode in Discord's **Settings → Advanced**,
then right-click the role in **Server Settings → Roles** → **Copy Role ID**.)

## Step 4 — Turn it on

1. In your repo, click the **Actions** tab.
2. You should see a workflow called **"Check X account and post to Discord"**.
   If GitHub shows a banner asking to enable Actions for this repo, click
   **I understand my workflows, go ahead and enable them**.
3. Click into the workflow, click **Run workflow** (top right) → **Run workflow**
   again to confirm. This triggers it immediately instead of waiting for
   the next 5-minute mark.

That first run won't post anything — it just records the account's most
recent post as a starting point, so you don't get flooded with old history.
Every run after that posts only genuinely new activity.

From here it runs itself, every 5 minutes, forever, for free.

## Checking it's working

- **Actions tab** → click the latest run → click the **check** job → click
  **Run check** to expand the log. You'll see lines like `Checking @nasa`
  and either `No new posts.` or `Posted original 12345...`.
- Once the monitored account posts something new, within 5 minutes it
  should appear in your Discord channel as an embed.
- If a run shows a red ❌, click into it and read the log — most likely
  it's a bad/missing secret, or the public Nitter instances are
  temporarily down (see Troubleshooting below).

## Changing the account you're watching

Settings → Secrets and variables → Actions → click `X_USERNAME` → **Update** →
type the new username → save. Takes effect on the next run, no other
changes needed.

## Troubleshooting

- **"DISCORD_WEBHOOK_URL and X_USERNAME must be set"** — you missed a
  secret in Step 3, or the name doesn't match exactly (case-sensitive).
- **"All Nitter instances failed"** — the public Nitter instances this
  script relies on are temporarily down or rate-limiting. This is the one
  real limitation of the free approach (see below) — it usually resolves
  itself within a day; no action needed on your part.
- **Nothing posts even after the account clearly posted something** —
  check the Actions tab for a failed/red run first. If runs are green but
  nothing shows up, double check the webhook URL secret is exactly what
  Discord gave you (it's a full URL, not just an ID).
- **Runs seem to skip / aren't exactly every 5 minutes** — normal. GitHub's
  free scheduler queues cron jobs and can delay them a few minutes during
  busy periods. It always catches up.

## About the "free" tradeoff

This posts via a public [Nitter](https://github.com/zedeus/nitter) instance
rather than X's own (paid) API, since there's no free official API anymore.
Nitter instance uptime varies — most of the time it just works, but public
instances occasionally go down when X changes something on their end. If
you find this happens often enough to be annoying, that's when it'd be
worth moving to a more involved setup (a self-hosted always-on version) —
just let me know and I can help with that.
