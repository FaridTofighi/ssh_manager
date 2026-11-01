"""Host templates for pre-filling the add host dialog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class HostTemplate:
    id: str
    label: str
    description: str
    defaults: Dict[str, str]


def built_in_templates() -> List[HostTemplate]:
    return [
        HostTemplate(
            id="generic",
            label="Generic SSH Host",
            description="Blank host with port 22.",
            defaults={
                "port": "22",
                "notes": "Generic SSH host template",
            },
        ),
        HostTemplate(
            id="aws_ec2",
            label="AWS EC2 Host",
            description="ec2-x-x-x-x.compute.amazonaws.com, port 22.",
            defaults={
                "hostname": "ec2-x-x-x-x.compute.amazonaws.com",
                "port": "22",
                "notes": "AWS EC2 instance",
            },
        ),
        HostTemplate(
            id="bare_metal",
            label="Bare Metal / DC Host",
            description="Bare metal/datacenter host on port 22.",
            defaults={
                "port": "22",
                "notes": "Bare-metal/datacenter SSH host",
            },
        ),
    ]
