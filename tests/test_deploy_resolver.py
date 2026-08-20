import pytest

from scripts.resolve_prod_host import public_ssh_host


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
