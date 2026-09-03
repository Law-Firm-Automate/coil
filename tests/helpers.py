import re


def _tok(data):
    m = re.search(rb'name="_csrf" value="([^"]+)"', data)
    return m.group(1).decode() if m else None


def login(c, email="owner@example.com", pw="password123"):
    """Log in through the real form and return a CSRF token valid for the new session."""
    r = c.get("/login")
    tok = _tok(r.data)
    r = c.post("/login", data={"email": email, "password": pw, "_csrf": tok})
    assert r.status_code == 302, r.data[:300]
    # auth.login clears the session, so fetch a fresh token from an authenticated page.
    r = c.get("/")
    return _tok(r.data) or tok
