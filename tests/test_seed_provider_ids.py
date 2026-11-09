from conftest import ME, raw_message


def test_seed_provider_ids(http, gmail):
    spec = {
        "users": [
            {
                "email": ME,
                "messages": [
                    {"id": "abc123", "threadId": "def456", "subject": "Loan", "text": "original"},
                    {"id": "abc124", "threadId": "def456", "subject": "Changed subject", "text": "follow up"},
                ],
            }
        ]
    }
    for _ in range(2):
        assert http.post("/_mock/reset").is_success
        assert http.post("/_mock/seed", json=spec).is_success
        # Captured thread membership is authoritative even when subjects differ.
        thread = gmail.users().threads().get(userId="me", id="def456").execute()
        assert {m["id"] for m in thread["messages"]} == {"abc123", "abc124"}
        sent = gmail.users().messages().send(userId="me", body={"threadId": "def456", "raw": raw_message(subject="Re: Loan")}).execute()
        assert sent["threadId"] == "def456"
        assert sent["id"] not in {"abc123", "abc124", "def456"}
