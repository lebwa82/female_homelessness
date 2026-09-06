import json
from types import SimpleNamespace

import pytest

from scripts.resolve_prod_host import PROD_FOLDER_ID, PROD_VM_ID, public_ssh_host, resolve_prod_host


def test_public_ssh_host_reads_the_vm_nat_address() -> None:
    instance = {
        "network_interfaces": [
            {
                "primary_v4_address": {
                    "one_to_one_nat": {"address": "111.88.152.227"},
                }
            }
        ]
    }

    assert public_ssh_host(instance) == "lebwa82@111.88.152.227"


def test_public_ssh_host_rejects_a_vm_without_a_nat_address() -> None:
    with pytest.raises(ValueError, match="public IPv4"):
        public_ssh_host({"network_interfaces": [{"primary_v4_address": {}}]})


def test_resolver_uses_permanent_id_and_reads_each_new_address(monkeypatch) -> None:
    addresses = iter(["192.0.2.1", "192.0.2.2"])

    def cloud(command, **kwargs):
        assert command == ["yc", "compute", "instance", "get", "--id", PROD_VM_ID, "--format", "json"]
        assert kwargs["timeout"] == 30
        return SimpleNamespace(stdout=json.dumps({
            "id": PROD_VM_ID, "folder_id": PROD_FOLDER_ID,
            "network_interfaces": [{"primary_v4_address": {"one_to_one_nat": {
                "address": next(addresses),
            }}}],
        }))

    monkeypatch.setattr("scripts.resolve_prod_host.subprocess.run", cloud)
    assert resolve_prod_host() == "lebwa82@192.0.2.1"
    assert resolve_prod_host() == "lebwa82@192.0.2.2"


def test_resolver_rejects_another_vm(monkeypatch) -> None:
    monkeypatch.setattr("scripts.resolve_prod_host.subprocess.run", lambda *a, **kw:
        SimpleNamespace(stdout=json.dumps({"id": "different-vm", "folder_id": PROD_FOLDER_ID})))
    with pytest.raises(ValueError, match="different project VM"):
        resolve_prod_host()
