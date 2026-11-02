import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response

from .proxmox import ProxmoxError
from .schemas import ServerIn, ServerOut

router = APIRouter(prefix="/api/servers")


def get_server(conn: sqlite3.Connection, server_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Server niet gevonden")
    return row


def _out(row: sqlite3.Row) -> ServerOut:
    return ServerOut(
        id=row["id"], name=row["name"], url=row["url"],
        token_id=row["token_id"], verify_tls=bool(row["verify_tls"]),
    )


@router.get("")
def list_servers(request: Request) -> list[ServerOut]:
    rows = request.app.state.conn.execute("SELECT * FROM servers ORDER BY name").fetchall()
    return [_out(r) for r in rows]


@router.post("", status_code=201)
def create_server(body: ServerIn, request: Request) -> ServerOut:
    conn = request.app.state.conn
    if not body.token_secret:
        raise HTTPException(422, "token_secret is verplicht")
    try:
        cur = conn.execute(
            "INSERT INTO servers(name,url,token_id,token_secret,verify_tls) VALUES(?,?,?,?,?)",
            (body.name, body.url.rstrip("/"), body.token_id, body.token_secret, int(body.verify_tls)),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Er bestaat al een server met die naam")
    return _out(get_server(conn, cur.lastrowid))


@router.put("/{server_id}")
def update_server(server_id: int, body: ServerIn, request: Request) -> ServerOut:
    conn = request.app.state.conn
    old = get_server(conn, server_id)
    secret = body.token_secret or old["token_secret"]
    try:
        conn.execute(
            "UPDATE servers SET name=?,url=?,token_id=?,token_secret=?,verify_tls=? WHERE id=?",
            (body.name, body.url.rstrip("/"), body.token_id, secret, int(body.verify_tls), server_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Er bestaat al een server met die naam")
    return _out(get_server(conn, server_id))


@router.delete("/{server_id}", status_code=204)
def delete_server(server_id: int, request: Request) -> Response:
    conn = request.app.state.conn
    get_server(conn, server_id)
    conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
    conn.commit()
    return Response(status_code=204)


@router.post("/{server_id}/test")
def test_server(server_id: int, request: Request) -> dict:
    row = get_server(request.app.state.conn, server_id)
    client = request.app.state.client_factory(row)
    try:
        return client.check()
    except ProxmoxError as e:
        raise HTTPException(502, e.detail)
    finally:
        client.close()
