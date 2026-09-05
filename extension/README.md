# Coil Timer (Chrome extension)

A small Manifest V3 extension that talks to Coil's REST API: search open matters, start and stop a timer, log
minutes directly, and (optionally) capture the time you spend in browser tabs as suggestions you accept or dismiss
in Coil. No build step; the files in this folder are loaded as-is.

## Install (load unpacked)

1. In Coil, open Settings > API tokens and create a token with the read and write scope (capture needs write). Copy it; it is shown once.
2. In Chrome, open `chrome://extensions`, turn on Developer mode (top right), click Load unpacked, and pick this
   `extension/` folder.
3. Click the Coil Timer icon, open Connection, paste your Coil URL (for example `https://coil.yourfirm.com`, or
   `http://localhost:5055` while developing) and the token, then Save and test.

The URL and token are kept in `chrome.storage.local` on this machine only.

## Use

- Type two or more characters in Matter to search by number, name, case number or client, then click a result.
- Start timer runs Coil's per-user timer. The popup can be closed; the timer keeps running on the server.
- Stop and log turns the elapsed time into a time entry rounded up to the next six minutes, same as the web app.
- Log time posts a fixed number of minutes with the description without using the timer.

## Time capture (suggestions, not automatic billing)

`background.js` is a service worker that watches which tab is in front. When you move to another tab, switch
windows, or go idle for five minutes, the time on the previous tab becomes a segment. Segments of two minutes or
more are queued and posted to `POST /api/v1/capture` every ten minutes (and whenever you open the popup). Coil turns
them into pending suggestions at Time suggestions (`/time/suggestions`) with a guessed matter: a matter number in the
title or address (M-1002), else a client or matter name. Same title within 30 minutes merges into one suggestion.
Nothing is logged until you accept a suggestion there; accepting rounds up to six minutes at your rate.

In the popup, open Capture:

- **Capture time from the active tab** turns the worker on or off (default on).
- **Only these domains** is an allowlist, one domain per line. Blank means everything except the denylist.
- **Never capture** is the denylist. It starts with obvious personal sites (Facebook, Instagram, YouTube, Netflix,
  Reddit and so on); edit it freely.
- The popup shows how many suggestions are waiting in Coil and how many segments are queued locally.

Only the tab title and address are sent, never page content. The queue lives in `chrome.storage.local`; a token
error (401/403) drops the queued batch instead of retrying forever, so fix the token and carry on.

Endpoints used: `GET /api/v1/me`, `GET /api/v1/matters?q=`, `POST /api/v1/timer/start`, `POST /api/v1/timer/stop`,
`POST /api/v1/time`, `POST /api/v1/capture`, `GET /api/v1/capture/pending`. Revoke the token in Settings > API tokens
to cut the extension off.
