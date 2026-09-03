import re
def login(c, email="owner@example.com", pw="password123"):
    r = c.get("/login")
    tok = re.search(rb'name="_csrf" value="([^"]+)"', r.data).group(1).decode()
    r = c.post("/login", data={"email": email, "password": pw, "_csrf": tok})
    assert r.status_code == 302, r.data[:300]
    return tok
