def test_index_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Proxlaber" in r.text


def test_static_assets_are_served(client):
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_api_routes_not_shadowed_by_static(client):
    assert client.get("/api/servers").json() == []
