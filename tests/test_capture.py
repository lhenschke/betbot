import json

from betbot.browser import _capture_summary, _redact_body, _redact_headers


def test_redacts_secrets_in_json_bodies():
    body = json.dumps({"email": "a@b.c", "password": "hunter2", "nested": {"accessToken": "x", "stake": 5}})
    out = json.loads(_redact_body(body))
    assert out["password"] == "<redacted>" and out["nested"]["accessToken"] == "<redacted>"
    assert out["email"] == "a@b.c" and out["nested"]["stake"] == 5


def test_redacts_secrets_in_form_bodies():
    out = _redact_body("username=lee&password=hunter2&remember=1")
    assert "hunter2" not in out and "username=lee" in out and "remember=1" in out


def test_redacts_headers():
    h = _redact_headers({"Cookie": "a", "Authorization": "b", "X-Session-Id": "c", "Accept": "json"})
    assert h == {"Cookie": "<redacted>", "Authorization": "<redacted>", "X-Session-Id": "<redacted>",
                 "Accept": "json"}


def test_summary_is_compact():
    recs = [{"type": "xhr", "method": "GET", "url": f"https://x/api/odds?i={i}", "status": 200,
             "request": None, "response": {"big": "y" * 10000}} for i in range(10)]
    recs.append({"click": [{"tag": "BUTTON", "testid": "place", "text": "Place Bet"}]})
    s = _capture_summary(recs)
    assert s.count("GET https://x/api/odds") == 2 and "CLICK BUTTON[place]" in s and len(s) < 5000
