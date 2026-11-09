import base64

from conftest import ME


def test_seed_mime_attachment(http, gmail):
    content = b"captured attachment bytes"
    assert http.post(
        "/_mock/seed",
        json={
            "users": [
                {
                    "email": ME,
                    "messages": [
                        {
                            "id": "abc",
                            "threadId": "def",
                            "subject": "Fixture",
                            "attachmentIds": {"1": "captured-attachment"},
                            "headers": {"Message-ID": "<captured@example.com>"},
                            "attachments": [{"filename": "terms.pdf", "data": base64.b64encode(content).decode()}],
                        }
                    ],
                }
            ]
        },
    ).is_success
    result = gmail.users().messages().list(userId="me", q="rfc822msgid:captured@example.com").execute()
    assert result["messages"] == [{"id": "abc", "threadId": "def"}]
    msg = gmail.users().messages().get(userId="me", id="abc").execute()
    part = next(p for p in msg["payload"]["parts"] if p.get("filename") == "terms.pdf")
    assert part["body"]["attachmentId"] == "captured-attachment"
    data = gmail.users().messages().attachments().get(userId="me", messageId="abc", id=part["body"]["attachmentId"]).execute()
    assert base64.urlsafe_b64decode(data["data"]) == content
    raw = gmail.users().messages().get(userId="me", id="abc", format="raw").execute()["raw"]
    assert http.post("/_mock/reset").is_success
    assert http.post("/_mock/seed", json={"users": [{"email": ME, "messages": [{"id": "abc", "threadId": "def", "raw": raw}]}]}).is_success
    # Raw MIME fixtures must survive seeding byte for byte.
    assert gmail.users().messages().get(userId="me", id="abc", format="raw").execute()["raw"] == raw
