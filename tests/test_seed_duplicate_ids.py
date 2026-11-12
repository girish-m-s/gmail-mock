from conftest import ME


def test_duplicate_seed_ids_rejected_before_inserting(http, gmail):
    response = http.post(
        "/_mock/seed",
        json={
            "users": [
                {
                    "email": ME,
                    "messages": [
                        {"id": "abcd", "text": "first"},
                        {"id": "abcd", "text": "duplicate"},
                    ],
                }
            ]
        },
    )
    assert response.status_code == 400
    # Invalid identity batches must not partially populate the mailbox.
    assert gmail.users().messages().list(userId="me").execute()["resultSizeEstimate"] == 0
    spec = {"users": [{"email": ME, "messages": [{"id": "abcd"}]}]}
    assert http.post("/_mock/seed", json=spec).is_success
    assert http.post("/_mock/seed", json=spec).status_code == 400
