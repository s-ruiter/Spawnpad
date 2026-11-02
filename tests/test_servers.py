SERVER = {
    "name": "pve1",
    "url": "https://pve1.lan:8006/",
    "token_id": "root@pam!proxlaber",
    "token_secret": "s3cr3t-value",
    "verify_tls": False,
}


def test_create_and_list_hides_secret(client):
    r = client.post("/api/servers", json=SERVER)
    assert r.status_code == 201
    listed = client.get("/api/servers")
    assert listed.status_code == 200
    body = listed.text
    assert "s3cr3t-value" not in body and "token_secret" not in body
    assert listed.json()[0]["url"] == "https://pve1.lan:8006"
    assert "s3cr3t-value" not in r.text


def test_duplicate_name_409(client):
    client.post("/api/servers", json=SERVER)
    assert client.post("/api/servers", json=SERVER).status_code == 409


def test_update_without_secret_keeps_old_secret(client, app):
    sid = client.post("/api/servers", json=SERVER).json()["id"]
    upd = {k: v for k, v in SERVER.items() if k != "token_secret"}
    upd["name"] = "pve1-renamed"
    assert client.put(f"/api/servers/{sid}", json=upd).status_code == 200
    row = app.state.conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    assert row["token_secret"] == "s3cr3t-value" and row["name"] == "pve1-renamed"


def test_delete_server(client):
    sid = client.post("/api/servers", json=SERVER).json()["id"]
    assert client.delete(f"/api/servers/{sid}").status_code == 204
    assert client.get("/api/servers").json() == []


def _fake_factory(version):
    import httpx

    from app.proxmox import ProxmoxClient

    def handler(req):
        if req.url.path.endswith("/version"):
            return httpx.Response(200, json={"data": {"version": version}})
        return httpx.Response(200, json={"data": [{"storage": "local"}]})

    return lambda row: ProxmoxClient(
        row["url"], row["token_id"], row["token_secret"], transport=httpx.MockTransport(handler)
    )


def test_server_test_endpoint_reports_version(client, app):
    sid = client.post("/api/servers", json=SERVER).json()["id"]
    app.state.client_factory = _fake_factory("9.0.3")
    r = client.post(f"/api/servers/{sid}/test")
    assert r.status_code == 200 and r.json()["version"] == "9.0.3"


def test_server_test_endpoint_reports_old_version_as_error(client, app):
    sid = client.post("/api/servers", json=SERVER).json()["id"]
    app.state.client_factory = _fake_factory("8.2.1")
    r = client.post(f"/api/servers/{sid}/test")
    assert r.status_code == 502 and "8.2.1" in r.json()["detail"]
