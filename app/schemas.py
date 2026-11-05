from typing import Literal, Optional

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
    scsihw: str = ""  # leeg = standaard van Proxmox
    nics: list[NicConfig]
    extra_disks: list[ExtraDisk] = Field(default_factory=list, max_length=14)
    iso_storage: str | None = None  # storage voor de Day-0 config-drive-ISO
    user_data: str | None = Field(default=None, exclude=True)  # uitgerekende Day-0 config; bevat geheimen, wordt nooit opgeslagen
    start_after: bool = False


class Candidate(BaseModel):
    name: str
    virtual_gb: int


class UploadInfo(BaseModel):
    upload_id: str
    candidates: list[Candidate]


class Day0Network(BaseModel):
    mode: Literal["dhcp", "static"] = "dhcp"
    interface: str = "port1"
    ip: str | None = None          # startadres met prefix, bijv. 10.0.0.11/24; bij aantal > 1 telt het per VM op
    gateway: str | None = None
    dns: list[str] = Field(default_factory=list, max_length=2)


class Day0Spec(BaseModel):
    """Day-0 configuratie (cloud-init) van één VM-blok; wordt per VM uitgerekend (naam, IP, token)."""
    product: Literal["fortigate", "fortimanager", "fortianalyzer", "other"] = "fortigate"
    hostname: str | None = "{name}"
    admin_password: str | None = None
    network: Day0Network = Field(default_factory=Day0Network)
    custom_cli: str = ""
    tokens: list[str] = Field(default_factory=list)   # FortiFlex-tokens, één per VM
    token_mode: Literal["mime", "cli"] = "mime"


class BatchItem(BaseModel):
    upload_id: str
    candidate: str
    count: int = Field(1, ge=1, le=20)
    config: VmConfig
    day0: Day0Spec | None = None


class BatchIn(BaseModel):
    server_id: int
    items: list[BatchItem] = Field(min_length=1, max_length=20)
