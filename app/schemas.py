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


class NicConfig(BaseModel):
    bridge: str
    vlan: int | None = None
    model: str = "virtio"


class VmConfig(BaseModel):
    name: str
    node: str
    datastore: str
    import_storage: str
    disk_gb: int
    vmid: int | None = None
    cores: int = 2
    memory_mb: int = 2048
    cpu_type: str = "host"
    bios: str = "seabios"
    machine: str = "pc"
    scsihw: str = "virtio-scsi-single"
    nics: list[NicConfig]
    start_after: bool = False


class Candidate(BaseModel):
    name: str
    virtual_gb: int


class UploadInfo(BaseModel):
    upload_id: str
    candidates: list[Candidate]
