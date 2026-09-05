"""Bounded LAN WS-Discovery, including cameras that ignore multicast."""

import ipaddress
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote


def _unicast_targets():
    """Sweep small attached LANs; use learned neighbors for larger LANs."""
    import psutil

    networks = set()
    local = set()
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        if name.startswith(('lo', 'docker', 'br-', 'veth', 'tailscale', 'tun', 'wg')):
            continue
        link = stats.get(name.split(':', 1)[0])
        if link is None or not link.isup:
            continue
        for address in addresses:
            if address.family != socket.AF_INET or not address.netmask:
                continue
            interface = ipaddress.IPv4Interface(f'{address.address}/{address.netmask}')
            local.add(interface.ip)
            if interface.ip.is_private and not (
                    interface.ip.is_loopback or interface.ip.is_link_local):
                networks.add(interface.network)

    neighbors = set()
    try:
        with open('/proc/net/arp') as table:
            for line in table.readlines()[1:]:
                fields = line.split()
                if len(fields) >= 6 and int(fields[2], 16) & 2:
                    neighbors.add(ipaddress.IPv4Address(fields[0]))
    except (OSError, ValueError):
        pass

    targets = set()
    for network in sorted(networks):
        candidates = network.hosts() if network.num_addresses <= 256 else sorted(neighbors)
        for address in candidates:
            if address in network and address not in local:
                targets.add(str(address))
                if len(targets) >= 1024:
                    return sorted(targets)
    return sorted(targets)


def _probe():
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        '<s:Header><a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>'
        f'<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>'
        '<a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous'
        '</a:Address></a:ReplyTo>'
        '<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To></s:Header>'
        '<s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>'
        '</d:Probe></s:Body></s:Envelope>'
    ).encode()


def _parse(data, ip):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    cameras = []
    for match in root.iter():
        if match.tag.rsplit('}', 1)[-1] != 'ProbeMatch':
            continue
        fields = {e.tag.rsplit('}', 1)[-1]: (e.text or '').strip()
                  for e in match.iter()}
        identity = fields.get('Address', '')
        if not identity or not fields.get('XAddrs'):
            continue
        scopes = fields.get('Scopes', '').split()
        def scope_value(key):
            return next((unquote(s.split('/' + key + '/', 1)[1])
                         for s in scopes if '/' + key + '/' in s), '')
        cameras.append({'ip': ip, 'uuid': identity.removeprefix('urn:').removeprefix('uuid:'),
                        'name': scope_value('name'), 'hardware': scope_value('hardware'),
                        'xaddr': fields['XAddrs']})
    return cameras


def discover_onvif_cameras(timeout=3):
    """Return the existing camera dictionaries using multicast plus LAN probes."""
    try:
        targets = _unicast_targets()
    except (ImportError, OSError, ValueError) as exc:
        print(f'[WARN] ONVIF unicast target enumeration failed: {exc}')
        targets = []
    print(f'[INFO] Discovering ONVIF cameras ({timeout}s, {len(targets)} unicast targets)...')
    cameras = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        # Receive while sending to avoid overflowing the UDP receive queue.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        # ARP resolution can queue probes for an entire subnet before replies arrive.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        sock.setblocking(False)
        probe = _probe()
        def receive():
            try:
                data, sender = sock.recvfrom(65535)
            except (BlockingIOError, socket.timeout):
                return False
            for camera in _parse(data, sender[0]):
                cameras.setdefault(camera['ip'], camera)
            return True
        for target in ['239.255.255.250', *targets]:
            try:
                sock.sendto(probe, (target, 3702))
            except OSError as exc:
                print(f'[WARN] ONVIF probe send failed: {exc}')
                # A failed interface or one unreachable neighbor must not abort discovery.
                continue
            receive()
        deadline = time.monotonic() + max(0, timeout)
        while time.monotonic() < deadline:
            sock.settimeout(max(0, deadline - time.monotonic()))
            if not receive():
                break
    for camera in cameras.values():
        print(f"[INFO] Found camera: {camera['name']} ({camera['hardware']}) "
              f"at {camera['ip']} uuid={camera['uuid']}")
    return list(cameras.values())
