import json
from types import SimpleNamespace

import pytest

from scripts.resolve_prod_host import (
    PROD_FOLDER_ID,
    PROD_VM_ID,
    public_ssh_host,
    resolve_prod_host,
    verify_ssh_host,
)


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
        assert command == [
            "yc",
            "compute",
            "instance",
            "get",
            "--id",
            PROD_VM_ID,
            "--format",
            "json",
        ]
        assert kwargs["timeout"] == 30
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "id": PROD_VM_ID,
                    "folder_id": PROD_FOLDER_ID,
                    "network_interfaces": [
                        {
                            "primary_v4_address": {
                                "one_to_one_nat": {
                                    "address": next(addresses),
                                }
                            }
                        }
                    ],
                }
            )
        )

    monkeypatch.setattr("scripts.resolve_prod_host.subprocess.run", cloud)
    assert resolve_prod_host() == "lebwa82@192.0.2.1"
    assert resolve_prod_host() == "lebwa82@192.0.2.2"


def test_resolver_rejects_another_vm(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.resolve_prod_host.subprocess.run",
        lambda *a, **kw: SimpleNamespace(
            stdout=json.dumps({"id": "different-vm", "folder_id": PROD_FOLDER_ID})
        ),
    )
    with pytest.raises(ValueError, match="different project VM"):
        resolve_prod_host()


@pytest.mark.parametrize("host", ["192.0.2.1", "lebwa82@192.0.2.1"])
def test_verify_ssh_checks_actual_instance_metadata(monkeypatch, host) -> None:
    def ssh(command, **kwargs):
        assert command[0] == "ssh"
        assert command[-2] == "lebwa82@192.0.2.1"
        assert command[-1].endswith("/computeMetadata/v1/instance/id")
        assert kwargs["capture_output"] and kwargs["check"] and kwargs["timeout"] == 20
        return SimpleNamespace(stdout=PROD_VM_ID + "\n")

    monkeypatch.setattr("scripts.resolve_prod_host.subprocess.run", ssh)
    assert verify_ssh_host(host) == "lebwa82@192.0.2.1"


def test_verify_ssh_rejects_other_machine(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.resolve_prod_host.subprocess.run",
        lambda *a, **kw: SimpleNamespace(stdout="some-other-vm"),
    )
    with pytest.raises(ValueError, match="not the project VM"):
        verify_ssh_host("192.0.2.1")


@pytest.mark.parametrize("host", ["-oProxyCommand=bad", "user@host", "root;false@192.0.2.1"])
def test_verify_ssh_rejects_invalid_target_before_connecting(monkeypatch, host) -> None:
    def forbidden(*a, **kw):
        pytest.fail("SSH should not run for an invalid target")

    monkeypatch.setattr("scripts.resolve_prod_host.subprocess.run", forbidden)
    with pytest.raises(ValueError):
        verify_ssh_host(host)


def test_current_deploy_targets_chatwoot_and_verifies_host_before_upload() -> None:
    from pathlib import Path

    recipe = (
        Path("justfile").read_text().split('deploy-prod host="": check', 1)[1].split("\n\n", 1)[0]
    )
    assert "scripts/deploy_chatwoot_test.sh" in recipe
    script = Path("scripts/deploy_chatwoot_test.sh").read_text()
    assert script.index("--verify-ssh") < script.index("git archive")
