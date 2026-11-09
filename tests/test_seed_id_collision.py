from gmail_mock import seed
from gmail_mock.store import Store


def test_generated_ids_skip_future_seed_message_and_thread_ids(monkeypatch):
    monkeypatch.setattr("gmail_mock.util.secrets.randbelow", lambda _: 0)
    store = Store("me@example.com")
    seed.load(
        store,
        {
            "users": [
                {
                    "email": "me@example.com",
                    "messages": [
                        {"subject": "Generated first"},
                        {"id": "1920000000001000", "threadId": "1920000000002000"},
                    ],
                }
            ]
        },
    )
    box = store.mailboxes["me@example.com"]
    # Both IDs are reserved before the first automatic allocation.
    assert set(box.messages) == {"1920000000003000", "1920000000001000"}
    assert set(box.threads) == {"1920000000003000", "1920000000002000"}
