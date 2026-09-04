# Coil Timer (Chrome extension)

A small Manifest V3 popup that talks to Coil's REST API: search open matters, start and stop a timer, or log
minutes directly. No build step; the three files in this folder are loaded as-is.

## Install (load unpacked)

1. In Coil, open Settings > API tokens and create a token with the read and write scope. Copy it; it is shown once.
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

Endpoints used: `GET /api/v1/me`, `GET /api/v1/matters?q=`, `POST /api/v1/timer/start`, `POST /api/v1/timer/stop`,
`POST /api/v1/time`. Revoke the token in Settings > API tokens to cut the extension off.
