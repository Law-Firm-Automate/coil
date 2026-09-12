#!/usr/bin/env python3
"""MCP server for Coil. Lets an AI assistant use a firm's practice management.

Design, and the reasoning behind it:

  The token is the whole security model. This process holds no credentials of its own
  and cannot widen what it was given. It reads /api/v1/me at startup and registers ONLY
  the tools the token's scopes allow, so a read-only token simply has no tools that
  write. That is better than offering a tool that fails: a model shown a "log time" tool
  will keep trying to use it.

  Redaction happens on the server, not here. By the time a matter reaches this process
  the client's name is already gone, so there is nothing here to leak and no way to
  misconfigure it into leaking. What this file does is TELL the model which mode it is
  in, because a model that sees "Client #4" and does not know it is a redaction will
  cheerfully report that the client is named Client #4.

  Nothing is cached. A practice changes while an assistant is mid-conversation, and a
  stale matter list in a legal tool is worse than a slow one.

Run:
    COIL_URL=https://coil.yourfirm.com COIL_TOKEN=coil_... python coil_mcp.py
"""
import logging
import os
import sys

import httpx

# httpx logs every request line, which on a legal tool means matter ids and search terms
# in whatever captures the server's stderr. Nothing here needs that.
logging.getLogger("httpx").setLevel(logging.WARNING)
from mcp.server.mcpserver import MCPServer

COIL_URL = (os.environ.get("COIL_URL") or "").rstrip("/")
COIL_TOKEN = os.environ.get("COIL_TOKEN") or ""
TIMEOUT = float(os.environ.get("COIL_TIMEOUT", "20"))

if not COIL_URL or not COIL_TOKEN:
    sys.exit("Set COIL_URL and COIL_TOKEN. Create a token in Coil at Settings, API tokens.")


def _client():
    return httpx.Client(
        base_url=f"{COIL_URL}/api/v1",
        headers={"Authorization": f"Bearer {COIL_TOKEN}",
                 "User-Agent": "coil-mcp/1.0 (+https://coil.legal)"},
        timeout=TIMEOUT,
    )


def call(method, path, **kw):
    """One place for every request, so every failure reads the same way to the model.

    Errors are returned rather than raised: an assistant that is told "this token does
    not have the tasks:read scope" can say so, where a traceback just ends the turn.
    """
    try:
        with _client() as c:
            r = c.request(method, path, **kw)
    except httpx.HTTPError as e:
        return {"error": f"Could not reach Coil at {COIL_URL}: {e}"}
    if r.status_code == 401:
        return {"error": "Coil rejected the token. It may have been revoked."}
    if r.status_code == 403:
        return {"error": r.json().get("error", "This token is not allowed to do that.")}
    if r.status_code == 429:
        return {"error": "Coil is rate limiting this token. Wait a minute."}
    if r.status_code >= 400:
        try:
            return {"error": r.json().get("error", f"Coil returned {r.status_code}.")}
        except ValueError:
            return {"error": f"Coil returned {r.status_code}."}
    try:
        return r.json()
    except ValueError:
        return {"error": "Coil returned something that was not JSON."}


# ------------------------------------------------------------------ startup
me = call("GET", "/me")
if "error" in me:
    sys.exit(f"Could not start: {me['error']}")

SCOPES = set(me.get("token", {}).get("scopes") or [])
MODE = me.get("token", {}).get("confidentiality", "full")
FIRM = me.get("firm", {}).get("name", "this firm")
WHO = me.get("user", {}).get("name", "the token owner")

REDACTION_NOTE = (
    "\n\nIMPORTANT: this token runs in WITHHELD mode. Coil removes client identities and "
    "the substance of matters before they reach you. Names like 'Client #4', 'Matter #7' "
    "and the value '[redacted]' are placeholders, NOT real data. Never present them as a "
    "client's name, never guess what they stand for, and if the user needs the real "
    "detail, tell them to look in Coil or to issue a token that sends everything."
    if MODE == "redacted" else
    "\n\nThis token returns unredacted client data. Treat everything you see as "
    "confidential client information and do not repeat it outside this conversation."
)

mcp = MCPServer(
    "coil",
    instructions=(
        f"Practice management for {FIRM}, acting as {WHO}. Matters are addressed by their "
        f"number, like M-1001. Money is in integer cents. Time is in minutes.{REDACTION_NOTE}"
    ),
)


def allowed(scope):
    return scope in SCOPES


# -------------------------------------------------------------------- tools
if allowed("matters:read"):
    @mcp.tool()
    def list_matters(query: str = "", status: str = "open", limit: int = 50) -> dict:
        """List matters. status is open, closed, pending or all."""
        return call("GET", "/matters", params={"q": query, "status": status, "limit": limit})

    @mcp.tool()
    def get_matter(matter_id: int) -> dict:
        """One matter in full, by its numeric id (not its M- number)."""
        return call("GET", f"/matters/{matter_id}")

if allowed("contacts:read"):
    @mcp.tool()
    def list_contacts(query: str = "", limit: int = 50) -> dict:
        """List contacts and clients."""
        return call("GET", "/contacts", params={"q": query, "limit": limit})

if allowed("time:read"):
    @mcp.tool()
    def list_time(matter_id: int = 0, date_from: str = "", date_to: str = "", limit: int = 100) -> dict:
        """Time entries, with the total minutes. Dates are YYYY-MM-DD."""
        p = {"limit": limit}
        if matter_id:
            p["matter_id"] = matter_id
        if date_from:
            p["from"] = date_from
        if date_to:
            p["to"] = date_to
        return call("GET", "/time", params=p)

    @mcp.tool()
    def get_timer() -> dict:
        """The running timer, if there is one."""
        return call("GET", "/timer")

if allowed("time:write"):
    @mcp.tool()
    def log_time(matter_id: int, minutes: int, description: str,
                 date: str = "", billable: bool = True) -> dict:
        """Log a time entry. minutes is a whole number; date defaults to today.

        Read the description back to the user before calling this. Time entries become
        invoices, and a wrong one is a billing error rather than a typo.
        """
        return call("POST", "/time", json={"matter_id": matter_id, "minutes": minutes,
                                           "description": description, "date": date or None,
                                           "billable": billable})

    @mcp.tool()
    def start_timer(matter_id: int, description: str = "") -> dict:
        """Start the timer on a matter."""
        return call("POST", "/timer/start", json={"matter_id": matter_id, "description": description})

    @mcp.tool()
    def stop_timer(description: str = "") -> dict:
        """Stop the running timer and save it as a time entry."""
        return call("POST", "/timer/stop", json={"description": description})

if allowed("invoices:read"):
    @mcp.tool()
    def list_invoices(status: str = "", limit: int = 50) -> dict:
        """Invoices. status is draft, sent, partial, paid or void. Amounts are cents."""
        return call("GET", "/invoices", params={"status": status, "limit": limit})

if allowed("invoices:write"):
    @mcp.tool()
    def draft_invoice(matter_id: int, issued_on: str = "", due_on: str = "") -> dict:
        """Draft an invoice for every unbilled time entry, expense and due milestone on a
        matter. Dates are YYYY-MM-DD; issued_on defaults to today, due_on to issued_on.

        This only ever creates a draft. It never submits, approves or sends. Read the
        totals back to the user before anyone opens the invoice in Coil.
        """
        return call("POST", "/invoices", json={"matter_id": matter_id, "issued_on": issued_on or None,
                                                "due_on": due_on or None})

if allowed("tasks:read"):
    @mcp.tool()
    def list_tasks(matter_id: int = 0, done: bool = False, limit: int = 50) -> dict:
        """Tasks and deadlines."""
        p = {"limit": limit, "done": "true" if done else "false"}
        if matter_id:
            p["matter_id"] = matter_id
        return call("GET", "/tasks", params=p)

if allowed("documents:read"):
    @mcp.tool()
    def list_documents(matter_id: int = 0, limit: int = 50) -> dict:
        """Documents on a matter. Metadata only; Coil never sends file contents here."""
        p = {"limit": limit}
        if matter_id:
            p["matter_id"] = matter_id
        return call("GET", "/documents", params=p)

if allowed("calendar:read"):
    @mcp.tool()
    def list_events(matter_id: int = 0, date_from: str = "", limit: int = 50) -> dict:
        """Calendar events from a date onwards. date_from is YYYY-MM-DD."""
        p = {"limit": limit}
        if matter_id:
            p["matter_id"] = matter_id
        if date_from:
            p["from"] = date_from
        return call("GET", "/calendar", params=p)

if allowed("calendar:write"):
    @mcp.tool()
    def create_event(title: str, starts_at: str, matter_id: int = 0, location: str = "") -> dict:
        """Add a calendar event. starts_at is ISO 8601, e.g. 2026-09-08T14:30:00.

        Court dates and deadlines belong in Coil's own deadline chains, which calculate
        from court rules. Use this for meetings and calls, not for a filing deadline.
        """
        return call("POST", "/calendar", json={"title": title, "starts_at": starts_at,
                                               "matter_id": matter_id or None, "location": location})

if allowed("notes:read"):
    @mcp.tool()
    def list_notes(matter_id: int = 0, limit: int = 50) -> dict:
        """Notes on a matter."""
        p = {"limit": limit}
        if matter_id:
            p["matter_id"] = matter_id
        return call("GET", "/notes", params=p)

if allowed("notes:write"):
    @mcp.tool()
    def add_note(matter_id: int, body: str) -> dict:
        """Add a note to a matter. It is attributed to the token's owner."""
        return call("POST", "/notes", json={"matter_id": matter_id, "body": body})

if allowed("leads:write"):
    @mcp.tool()
    def create_lead(name: str, email: str = "", phone: str = "",
                    matter_type: str = "", message: str = "") -> dict:
        """File a new intake lead. It lands in the intake pipeline for a human to triage."""
        return call("POST", "/leads", json={"name": name, "email": email, "phone": phone,
                                            "matter_type": matter_type, "message": message})


@mcp.tool()
def coil_status() -> dict:
    """What this connection can see and do, and whether client details are withheld.

    Worth calling first: it says which mode you are in, and the user may not know.
    """
    return {"firm": FIRM, "acting_as": WHO, "instance": COIL_URL,
            "confidentiality": MODE,
            "client_details_withheld": MODE == "redacted",
            "scopes": sorted(SCOPES),
            "note": me.get("token", {}).get("note", "")}


if __name__ == "__main__":
    # stderr, not stdout: stdout is the MCP transport and anything printed there is
    # parsed as protocol and breaks the connection.
    print(f"[coil-mcp] {FIRM} as {WHO}, mode={MODE}, scopes={len(SCOPES)}", file=sys.stderr)
    mcp.run()
