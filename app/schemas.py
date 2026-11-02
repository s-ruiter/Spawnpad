from typing import Optional

from pydantic import BaseModel, Field


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


class ExtraDisk(BaseModel):
    size_gb: int = Field(ge=1)
    datastore: str | None = None  # leeg = zelfde datastore als de boot-disk


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
    extra_disks: list[ExtraDisk] = Field(default_factory=list, max_length=14)
    start_after: bool = False


class Candidate(BaseModel):
    name: str
    virtual_gb: int


class UploadInfo(BaseModel):
    upload_id: str
    candidates: list[Candidate]
