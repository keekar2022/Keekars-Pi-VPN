# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com

from enum import Enum
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Interface names are re-validated against the live system (psutil/NetworkManager)
# in the route handlers — this pattern only rejects obviously-malformed input.
_IFACE_PATTERN = r"^[a-zA-Z0-9_.-]{1,15}$"

# wg-quick derives the systemd unit/interface name directly from this string,
# so it's kept tighter than _IFACE_PATTERN (no dots) and length-limited to
# what the kernel accepts for an interface name.
_TUNNEL_NAME_PATTERN = r"^[a-zA-Z0-9_-]{1,15}$"
# Flows into a downloaded filename and into log lines — kept to a safe,
# human-readable subset rather than arbitrary free text.
_PEER_DESCRIPTION_PATTERN = r"^[a-zA-Z0-9 _.-]{1,64}$"
_ENDPOINT_HOST_PATTERN = r"^[a-zA-Z0-9.-]{1,255}$"


class InterfaceMethod(str, Enum):
    dhcp = "dhcp"
    static = "static"


class NetworkConfigRequest(BaseModel):
    interface: str = Field(pattern=_IFACE_PATTERN)
    method: InterfaceMethod
    address: Optional[IPv4Interface] = None
    gateway: Optional[IPv4Address] = None
    dns: Optional[list[IPv4Address]] = None

    @field_validator("address")
    @classmethod
    def address_required_for_static(cls, v, info):
        if info.data.get("method") == InterfaceMethod.static and v is None:
            raise ValueError("address is required when method=static")
        return v


class WifiConnectRequest(BaseModel):
    ssid: str = Field(min_length=1, max_length=32)
    # Optional: omit to reconnect an already-known network by its saved
    # profile without changing its password; provide to join a new network
    # or update an existing one's password.
    psk: Optional[str] = Field(default=None, min_length=8, max_length=63)
    # The saved connection profile's NAME, only used for the no-psk
    # reconnect path (`connection up <name>`) — a profile's name is not
    # always its SSID (e.g. a netplan-managed profile), so this can't be
    # assumed to equal `ssid`. Defaults to `ssid` for profiles nmcli
    # created itself, where the two do match.
    connection_name: Optional[str] = Field(default=None, min_length=1, max_length=64)


class RouteRequest(BaseModel):
    destination: IPv4Network
    gateway: Optional[IPv4Address] = None
    interface: str = Field(pattern=_IFACE_PATTERN)
    table: Optional[int] = Field(default=None, ge=1, le=252)


class RouteDeleteRequest(BaseModel):
    destination: IPv4Network
    interface: str = Field(pattern=_IFACE_PATTERN)
    table: Optional[int] = Field(default=None, ge=1, le=252)


class TunnelCreateRequest(BaseModel):
    name: str = Field(pattern=_TUNNEL_NAME_PATTERN)
    address: IPv4Interface
    listen_port: int = Field(ge=1024, le=65535)
    # Hostname/IP clients should dial to reach this tunnel. Optional because
    # it may not be known/provisioned yet; the generated peer config will
    # carry a placeholder comment instead of a real Endpoint line until set.
    endpoint: Optional[str] = Field(default=None, pattern=_ENDPOINT_HOST_PATTERN)
    # Override the AllowedIPs-overlap safety check (see app/wireguard.py) —
    # only meaningful if the operator has confirmed this tunnel's own address
    # deliberately overlaps a locally-connected subnet.
    force: bool = False


class PeerCreateRequest(BaseModel):
    description: str = Field(pattern=_PEER_DESCRIPTION_PATTERN)
    # Subnets this peer should be routed to reach through the tunnel — this
    # becomes the *client's* AllowedIPs in the downloaded config, not the
    # server's (the server's own AllowedIPs for this peer is always just its
    # own auto-assigned tunnel address, computed server-side).
    allowed_ips: list[IPv4Network] = Field(min_length=1)
    preshared_key: bool = False
    keepalive: Optional[int] = Field(default=25, ge=0, le=3600)


class TunnelStateRequest(BaseModel):
    # "disconnect" is a plain stop (client tunnels' Disconnect button) — it
    # deliberately leaves the systemd unit enabled, unlike "deactivate"
    # (disable --now), so a later reboot still auto-reconnects.
    action: Literal["activate", "deactivate", "disconnect"]
    force: bool = False
