"""A rich inventory for the SSH Manager export / import tests (with secrets planted where they must not leak)."""

from dataclasses import dataclass
from pathlib import Path

from ophion.inventory.store import InventoryStore, NewHost

SENTINEL = "S3NT1NEL-secret-value"


@dataclass
class FakeCredential:
    id: str
    name: str
    username: str
    private: bool = False


def make_store(folder: Path) -> InventoryStore:
    return InventoryStore(folder / "inventory.db")  # opened by the constructor


def build_rich(store: InventoryStore):
    """Returns ``(ids, group_colors, host_colors, credentials_by_host_id, unused)``."""

    prod = store.create_group(None, "Prod", note="production servers")
    databases = store.create_group(prod.id, "Databases")
    replicas = store.create_group(databases.id, "Replicas")
    store.create_group(prod.id, "Empty")
    windows = store.create_group(None, "Windows")
    files = store.create_group(None, "Files")
    store.create_group(None, "سرورها")  # an RTL name, empty

    bastion = store.add_host(NewHost(prod.id, "bastion", "bastion.example.org", 22, "jump"))
    web = store.add_host(NewHost(
        prod.id, "web-01", "web1.example.org", 2222, "deploy",
        options={"proxyjump": "jump@bastion.example.org", "identityfile": "~/.ssh/id_ed25519",
                 "Password": SENTINEL, "pwd": SENTINEL,
                 "proxycommand": f"sshpass -p {SENTINEL} ssh -W %h:%p jump"},
        metadata={"tags": ["web", "frontend"], "notes": "main site", "environment": "production",
                  "owner": "ops", "service": "nginx", "permissions": {"read_only": True},
                  "default_session_profile_id": "profile-1"}))
    db1 = store.add_host(NewHost(databases.id, "db-01", "db1.internal", 22, "postgres"))
    db2 = store.add_host(NewHost(replicas.id, "db-02", "db2.internal", None, "postgres"))
    dc = store.add_host(NewHost(
        windows.id, "dc-01", "dc1.corp.example", 3389, "administrator", protocol="rdp",
        metadata={"rdp": {"v": 1, "domain": "CORP", "via_ssh": bastion.id, "width": 1600,
                          "pinned_sha256": ["AB:CD:" + SENTINEL],
                          "gateway": {"host": "", "port": 443, "credential_id": "gw-" + SENTINEL},
                          "gateway_password": SENTINEL}}))
    ftp = store.add_host(NewHost(files.id, "ftp-old", "ftp.example.org", 21, "anonymous", protocol="ftp",
                                 metadata={"transfer": {"v": 1, "anonymous": True, "anonymous_password": "me@x",
                                                        "ftp_password": SENTINEL}}))
    ftps = store.add_host(NewHost(files.id, "ftps-new", "ftps.example.org", 990, "uploader", protocol="ftps",
                                  metadata={"transfer": {"v": 1, "tls_mode": "implicit",
                                                         "pinned_sha256": "11:22:" + SENTINEL}}))
    sftp = store.add_host(NewHost(files.id, "sftp-1", "sftp.example.org", 22, "deploy", protocol="sftp"))
    vnc = store.add_host(NewHost(files.id, "vnc-1", "vnc.example.org", 5900, "", protocol="vnc",
                                 metadata={"future": {"x": 1}}))
    ids = {"prod": prod.id, "databases": databases.id, "replicas": replicas.id, "windows": windows.id,
           "files": files.id, "bastion": bastion.id, "web": web.id, "db1": db1.id, "db2": db2.id, "dc": dc.id,
           "ftp": ftp.id, "ftps": ftps.id, "sftp": sftp.id, "vnc": vnc.id}
    group_colors = {"Prod": "#aa0000", "Prod/Databases": "#00aa00", "Prod/Databases/Replicas": "#0000AA",
                    "Windows": "not-a-colour"}
    host_colors = {"Prod::Databases/Replicas::db-02": "#123456", "Files::::sftp-1": "#abcdef"}
    shared = FakeCredential("cred-ops", "Ops account", "deploy")
    pg = FakeCredential("cred-pg", "Postgres", "postgres")
    private = FakeCredential("cred-private", "host:web", "deploy", private=True)
    credentials = {web.id: shared, sftp.id: shared, db1.id: pg, db2.id: pg, bastion.id: private}
    return ids, group_colors, host_colors, credentials, {}
