from typing import Optional

from pydantic import BaseModel


class ServerIn(BaseModel):
    name: str
    url: str
    token_id: str
    token_secret: Optional[str] = None
    verify_tls: bool = True


class ServerOut(BaseModel):
    id: int
    name: str
    url: str
    token_id: str
    verify_tls: bool
