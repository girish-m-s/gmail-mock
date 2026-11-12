import pytest
from conftest import ME


@pytest.mark.parametrize("mapping", [{"1": "same", "2": "same"}, {"9": "missing"}, {"1": ""}, ["invalid"]])
def test_seed_invalid_attachments(http, gmail, mapping):
    response = http.post(
        "/_mock/seed",
        json={
            "users": [
                {
                    "email": ME,
                    "messages": [
                        {
                            "id": "abc",
                            "attachmentIds": mapping,
                            "attachments": [{"filename": "a", "content": "a"}, {"filename": "b", "content": "b"}],
                        }
                    ],
                }
            ]
        },
    )
    assert response.status_code == 400
    # Invalid maps cannot insert an unreadable message or overwrite attachment bytes.
    assert gmail.users().messages().list(userId="me").execute()["resultSizeEstimate"] == 0
