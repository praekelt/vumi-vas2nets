# -*- encoding: utf-8 -*-

import json
from urllib import urlencode
from twisted.web import http

from twisted.internet.task import Clock
from twisted.internet.defer import inlineCallbacks

from vumi.tests.helpers import VumiTestCase
from vumi.transports.httprpc.tests.helpers import HttpRpcTransportHelper
from vumi.tests.utils import LogCatcher

from vxvas2nets import Vas2NetsSmsTransport


class TestVas2NetsSmsTransport(VumiTestCase):
    @inlineCallbacks
    def setUp(self):
        self.clock = Clock()
        self.patch(Vas2NetsSmsTransport, 'get_clock', lambda _: self.clock)

        self.config = {
            'web_port': 0,
            'web_path': '/api/v1/vas2nets/sms/',
            'publish_status': True,
        }

        self.tx_helper = self.add_helper(
            HttpRpcTransportHelper(Vas2NetsSmsTransport))

        self.transport = yield self.tx_helper.get_transport(self.config)
        self.transport_url = self.transport.get_transport_url(
            self.config['web_path'])

    def get_host(self):
        addr = self.transport.web_resource.getHost()
        return '%s:%s' % (addr.host, addr.port)

    def assert_contains_items(self, obj, items):
        for name, value in items.iteritems():
            self.assertEqual(obj[name], value)

    def assert_uri(self, actual_uri, path, params):
        actual_path, actual_params = actual_uri.split('?')
        self.assertEqual(actual_path, path)

        self.assertEqual(
            sorted(actual_params.split('&')),
            sorted(urlencode(params).split('&')))

    @inlineCallbacks
    def test_inbound(self):
        res = yield self.tx_helper.mk_request(
            sender='+123',
            receiver='456',
            msgdata='hi',
            operator='MTN',
            recvtime='2012-02-27 19-50-07',
            msgid='789')

        self.assertEqual(res.code, http.OK)

        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)

        self.assert_contains_items(msg, {
            'from_addr': '+123',
            'from_addr_type': 'msisdn',
            'to_addr': '456',
            'content': 'hi',
            'provider': 'MTN',
            'transport_metadata': {
                'vas2nets_sms': {'msgid': '789'}
            }
        })

    @inlineCallbacks
    def test_inbound_decode_error(self):
        with LogCatcher() as lc:
            res = yield self.tx_helper.mk_request(
                sender='+123',
                receiver='456',
                msgdata=u'ポケモン'.encode('utf-16'),
                operator='MTN',
                recvtime='2012-02-27 19-50-07',
                msgid='789')

        [error] = lc.errors[0]['message']
        self.assertTrue("Bad request encoding" in error)

        req = json.loads(res.delivered_body)['invalid_request']

        self.assert_contains_items(req, {
            'method': 'GET',
            'path': self.config['web_path'],
            'content': '',
            'headers': {
                'Connection': ['close'],
                'Host': [self.get_host()]
            }
        })

        self.assert_uri(req['uri'], self.config['web_path'], {
            'sender': '+123',
            'receiver': '456',
            'msgdata': u'ポケモン'.encode('utf-16'),
            'operator': 'MTN',
            'recvtime': '2012-02-27 19-50-07',
            'msgid': '789'
        })

    @inlineCallbacks
    def test_inbound_bad_params(self):
        with LogCatcher() as lc:
            res = yield self.tx_helper.mk_request(
                sender='+123',
                foo='456',
                operator='MTN',
                recvtime='2012-02-27 19-50-07',
                msgid='789')

        [error] = lc.errors[0]['message']
        self.assertTrue("Bad request fields for inbound message" in error)
        self.assertTrue("foo" in error)
        self.assertTrue("msgdata" in error)
        self.assertTrue("receiver" in error)

        self.assert_contains_items(json.loads(res.delivered_body), {
            'unexpected_parameter': ['foo'],
            'missing_parameter': ['msgdata', 'receiver'],
        })
