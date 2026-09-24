import ipaddress

from app.core.config import settings


def client_ip(request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer not in settings.trusted_proxy_ips:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    try:
        chain = [
            str(ipaddress.ip_address(value.strip()))
            for value in forwarded.split(",")
            if value.strip()
        ]
    except ValueError:
        return peer
    for address in reversed(chain):
        if address not in settings.trusted_proxy_ips:
            return address
    return peer
