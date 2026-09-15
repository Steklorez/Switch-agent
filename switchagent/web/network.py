"""Allow only loopback and private home-network clients."""
from ipaddress import ip_address, ip_network

from starlette.responses import JSONResponse

_HOME = tuple(map(ip_network, ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7")))


def is_home_address(value: str) -> bool:
    try:
        address = ip_address(value)
        return any(address in network for network in _HOME)
    except ValueError:
        return False


async def home_network_only(request, call_next):
    if not request.client or not is_home_address(request.client.host):
        return JSONResponse({"detail": "Only home-network clients are allowed"}, status_code=403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail": "Cross-site requests are not allowed"}, status_code=403)
    return await call_next(request)
