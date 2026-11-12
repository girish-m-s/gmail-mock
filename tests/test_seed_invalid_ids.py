import pytest
from conftest import ME


@pytest.mark.parametrize("field,value", [("id", ""), ("id", None), ("id", 123), ("threadId", "not-hex")])
def test_seed_invalid_ids(http, gmail, field, value):
    response = http.post("/_mock/seed", json={"users": [{"email": ME, "messages": [{field: value}]}]})
    assert response.status_code == 400
    assert gmail.users().messages().list(userId="me").execute()["resultSizeEstimate"] == 0
