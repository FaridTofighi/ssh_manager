"""A large app-store inventory for performance tests and tools/bench_large_inventory.py.

``populate(store)`` adds 3 top groups × 333 deep child groups (``00 - pod`` /
``3 - Devops/AIFA - AI/17-Production``) × 4 hosts: 3,996 hosts, about 1,000
groups. Every 50th host is RDP; the others are SSH.
"""

from __future__ import annotations

from typing import List

TOPS, CHILDREN, PER_GROUP = 3, 333, 4


def child_path(index: int) -> List[str]:
    return [f"{index % 10} - Devops", "AIFA - AI", f"{index}-Production"]


def populate(store, tops: int = TOPS, children: int = CHILDREN, per_group: int = PER_GROUP) -> int:
    """Add the groups and hosts in one transaction; returns the number of hosts."""

    from ophion.inventory.store import NewHost

    hosts = []
    number = 0
    with store.transaction():
        for top in range(tops):
            parent = store.create_group(None, f"{top:02d} - pod")
            for index in range(children):
                group = store.create_group_path(child_path(index), parent.id)
                for _ in range(per_group):
                    number += 1
                    rdp = number % 50 == 0
                    hosts.append(NewHost(group.id, f"node-{number:05d}", f"10.{number // 65536}.{number // 256 % 256}"
                                         f".{number % 256}", user="ops", protocol="rdp" if rdp else "ssh",
                                         metadata={"rdp": {"v": 1}} if rdp else {}))
        store.add_hosts_bulk(hosts)
    return len(hosts)


__all__ = ["populate", "child_path", "TOPS", "CHILDREN", "PER_GROUP"]
