from conftest import ME, raw_message


def test_seed_mailbox_isolation(http, gmail, gmail_for):
    other = "other@example.com"
    assert http.post(
        "/_mock/seed",
        json={
            "users": [
                {"email": ME, "messages": [{"id": "abc", "threadId": "def", "subject": "Loan", "text": "private"}]},
                {"email": other, "messages": [{"id": "abc", "threadId": "def", "subject": "Other", "text": "separate"}]},
            ]
        },
    ).is_success
    other_gmail = gmail_for(other)
    for client, subject in [(gmail, "Loan"), (other_gmail, "Other")]:
        msg = client.users().messages().get(userId="me", id="abc").execute()
        assert next(h["value"] for h in msg["payload"]["headers"] if h["name"] == "Subject") == subject
    gmail.users().messages().send(userId="me", body={"threadId": "def", "raw": raw_message(subject="Re: Loan")}).execute()
    # Identical provider IDs in different mailboxes never share thread membership.
    assert len(other_gmail.users().threads().get(userId="me", id="def").execute()["messages"]) == 1
