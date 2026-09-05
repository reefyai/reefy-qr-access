"""Discovery regressions using invented LANs and camera identities."""

import socket
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch, mock_open

import onvif_discovery as discovery


def response(identity='synthetic-camera'):
    return (f'<Envelope><ProbeMatch><EndpointReference><Address>urn:uuid:{identity}'
            '</Address></EndpointReference><XAddrs>http://10.20.30.9/onvif/device_service'
            '</XAddrs><Scopes>onvif://www.onvif.org/name/Test%20Camera '
            'onvif://www.onvif.org/hardware/TestModel</Scopes></ProbeMatch></Envelope>').encode()


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.queue = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def setsockopt(self, *args):
        pass

    def setblocking(self, *args):
        pass

    def settimeout(self, *args):
        pass

    def sendto(self, data, destination):
        self.sent.append(destination)
        if destination[0] == '239.255.255.250':
            self.queue.append((response('other-camera'), ('10.20.30.8', 3702)))
        elif destination[0] == '10.20.30.9':
            self.queue.extend([(response(), ('10.20.30.9', 3702))] * 2)
        else:
            raise OSError('unreachable neighbor')

    def recvfrom(self, size):
        if self.queue:
            return self.queue.pop(0)
        raise BlockingIOError()


class DiscoveryTests(unittest.TestCase):
    def test_unicast_finds_missing_camera_despite_multicast_success(self):
        sock = FakeSocket()
        with patch.object(discovery, '_unicast_targets', return_value=['10.20.30.7', '10.20.30.9']), \
                patch.object(discovery.socket, 'socket', return_value=sock):
            cameras = discovery.discover_onvif_cameras()
        self.assertEqual([c['uuid'] for c in cameras], ['other-camera', 'synthetic-camera'])
        self.assertEqual(cameras[1]['name'], 'Test Camera')
        self.assertEqual(set(cameras[1]), {'ip', 'uuid', 'name', 'hardware', 'xaddr'})
        self.assertTrue(sock.closed)

    def test_multicast_retained_when_interfaces_unavailable(self):
        with patch.object(discovery, '_unicast_targets', side_effect=OSError('unavailable')), \
                patch.object(discovery.socket, 'socket', return_value=FakeSocket()):
            self.assertEqual(len(discovery.discover_onvif_cameras()), 1)

    def test_response_flood_respects_deadline(self):
        sock = FakeSocket()
        sock.recvfrom = lambda size: (b'<noise/>', ('10.20.30.8', 3702))
        with patch.object(discovery, '_unicast_targets', return_value=[]), \
                patch.object(discovery.socket, 'socket', return_value=sock), \
                patch.object(discovery.time, 'monotonic', side_effect=[0, 1, 2, 4]):
            self.assertEqual(discovery.discover_onvif_cameras(timeout=3), [])
        self.assertTrue(sock.closed)

    def test_malformed_packet_does_not_hide_valid_reply(self):
        self.assertEqual(discovery._parse(b'not xml', '10.20.30.9'), [])
        self.assertEqual(discovery._parse(b'<Hello/>', '10.20.30.9'), [])
        self.assertEqual(discovery._parse(response(), '10.20.30.9')[0]['uuid'], 'synthetic-camera')

    def test_namespaced_response(self):
        payload = response().replace(b'<Envelope>', b'<Envelope xmlns="urn:example:discovery">')
        self.assertEqual(discovery._parse(payload, '10.20.30.9')[0]['hardware'], 'TestModel')

    def test_unique_message_ids(self):
        self.assertNotEqual(discovery._probe(), discovery._probe())

    def test_two_lan_subnets_and_large_network_neighbors(self):
        def addr(ip, mask):
            return SimpleNamespace(family=socket.AF_INET, address=ip, netmask=mask)
        fake = SimpleNamespace(
            net_if_stats=lambda: {name: SimpleNamespace(isup=name != 'eth2')
                                  for name in ['eth0', 'eth1', 'eth2', 'docker0']},
            net_if_addrs=lambda: {
                'eth0': [addr('10.20.30.1', '255.255.255.252')],
                'eth0:0': [addr('10.20.31.1', '255.255.255.252')],
                'eth1': [addr('10.40.0.1', '255.255.0.0')],
                'eth2': [addr('10.50.0.1', '255.255.255.0')],
                'docker0': [addr('172.17.0.1', '255.255.255.0')]})
        arp = 'header\n10.40.0.9 0x1 0x2 02:00:00:00:00:09 * eth1\n'
        with patch.dict(sys.modules, {'psutil': fake}), patch('builtins.open', mock_open(read_data=arp)):
            self.assertEqual(discovery._unicast_targets(), ['10.20.30.2', '10.20.31.2', '10.40.0.9'])


if __name__ == '__main__':
    unittest.main()
