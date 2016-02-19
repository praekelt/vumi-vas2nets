# -*- encoding: utf-8 -*-

import json
from urllib import urlencode
from urlparse import urljoin

from twisted.web import http
from twisted.internet import reactor
from twisted.internet.task import Clock
from twisted.internet.defer import inlineCallbacks
from twisted.web.client import HTTPConnectionPool

import treq

from vumi.tests.helpers import VumiTestCase
from vumi.transports.httprpc.tests.helpers import HttpRpcTransportHelper
from vumi.tests.utils import LogCatcher
from vumi.tests.utils import MockHttpServer

from vxvas2nets import Vas2NetsSmsTransport


class TestVas2NetsSmsTransport(VumiTestCase):
    @inlineCallbacks
    def setUp(self):
        self.clock = Clock()
        self.patch(Vas2NetsSmsTransport, 'get_clock', lambda _: self.clock)

        self.remote_request_handler = lambda _: 'OK.1234'
        self.remote_server = MockHttpServer(self.remote_handle_request)
        yield self.remote_server.start()
        self.addCleanup(self.remote_server.stop)

        self.config = {
            'web_port': 0,
            'web_path': '/api/v1/vas2nets/sms/',
            'publish_status': True,
            'outbound_url': urljoin(self.remote_server.url, 'nonreply'),
            'reply_outbound_url': urljoin(self.remote_server.url, 'reply'),
            'username': 'root',
            'password': 't00r',
        }

        self.tx_helper = self.add_helper(
            HttpRpcTransportHelper(Vas2NetsSmsTransport))

        self.transport = yield self.tx_helper.get_transport(self.config)
        self.transport_url = self.transport.get_transport_url(
            self.config['web_path'])

        connection_pool = HTTPConnectionPool(reactor, persistent=False)
        treq._utils.set_global_pool(connection_pool)

    def capture_remote_requests(self, response='OK.1234'):
        def handler(req):
            reqs.append(req)
            return response

        reqs = []
        self.remote_request_handler = handler
        return reqs

    def remote_handle_request(self, req):
        return self.remote_request_handler(req)

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

        body = json.loads(res.delivered_body)

        self.assertEqual(body['unexpected_parameter'], ['foo'])

        self.assertEqual(
            sorted(body['missing_parameter']),
            ['msgdata', 'receiver'])

    @inlineCallbacks
    def test_outbound_non_reply(self):
        reqs = self.capture_remote_requests()

        msg = yield self.tx_helper.make_dispatch_outbound(
            from_addr='456',
            to_addr='+123',
            content='hi')

        [req] = reqs

        self.assertTrue(req.uri.startswith('/nonreply'))
        self.assertEqual(req.method, 'GET')
        self.assertEqual(req.args, {
            'username': ['root'],
            'message': ['hi'],
            'password': ['t00r'],
            'sender': ['456'],
            'receiver': ['+123'],
        })

        [ack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['user_message_id'], msg['message_id'])
        self.assertEqual(ack['sent_message_id'], msg['message_id'])

    @inlineCallbacks
    def test_outbound_reply(self):
        reqs = self.capture_remote_requests()

        yield self.tx_helper.mk_request(
            sender='+123',
            receiver='456',
            msgdata='hi',
            operator='MTN',
            recvtime='2012-02-27 19-50-07',
            msgid='789')

        [in_msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)

        msg = in_msg.reply('hi back')
        yield self.tx_helper.dispatch_outbound(msg)

        [req] = reqs
        self.assertTrue(req.uri.startswith('/reply'))
        self.assertEqual(req.method, 'GET')
        self.assertEqual(req.args, {
            'username': ['root'],
            'message': ['hi back'],
            'password': ['t00r'],
            'sender': ['456'],
            'receiver': ['+123'],
            'message_id': ['789']
        })

        [ack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assertEqual(ack['event_type'], 'ack')
        self.assertEqual(ack['user_message_id'], msg['message_id'])
        self.assertEqual(ack['sent_message_id'], msg['message_id'])

    @inlineCallbacks
    def test_outbound_known_error(self):
        def handler(req):
            req.setResponseCode(400)
            [error] = req.args['message']
            return error

        errors = [
            'ERR-11',
            'ERR-12',
            'ERR-13',
            'ERR-14',
            'ERR-15',
            'ERR-21',
            'ERR-33',
            'ERR-41',
            'ERR-70',
            'ERR-52',
        ]

        nack_reasons = {}
        self.remote_request_handler = handler

        for error in errors:
            msg = yield self.tx_helper.make_dispatch_outbound(
                from_addr='456',
                to_addr='+123',
                content=error)

            [nack] = yield self.tx_helper.wait_for_dispatched_events(1)
            self.tx_helper.clear_dispatched_events()

            self.assertEqual(nack['event_type'], 'nack')
            self.assertEqual(nack['user_message_id'], msg['message_id'])
            self.assertEqual(nack['sent_message_id'], msg['message_id'])

            nack_reasons[error] = nack['nack_reason']

        self.assertEqual(nack_reasons, {
            'ERR-11': 'Missing username (ERR-11)',
            'ERR-12': 'Missing password (ERR-12)',
            'ERR-13': 'Missing destination (ERR-13)',
            'ERR-14': 'Missing sender id (ERR-14)',
            'ERR-15': 'Missing message (ERR-15)',
            'ERR-21': 'Ender id too long (ERR-21)',
            'ERR-33': 'Invalid login (ERR-33)',
            'ERR-41': 'Insufficient credit (ERR-41)',
            'ERR-70': 'Invalid destination number (ERR-70)',
            'ERR-52': 'System error (ERR-52)',
        })

    @inlineCallbacks
    def test_outbound_unknown_error(self):
        def handler(req):
            req.setResponseCode(400)
            return 'foo'

        self.remote_request_handler = handler

        msg = yield self.tx_helper.make_dispatch_outbound(
            from_addr='456',
            to_addr='+123',
            content='hi')

        [nack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assertEqual(nack['event_type'], 'nack')
        self.assertEqual(nack['user_message_id'], msg['message_id'])
        self.assertEqual(nack['sent_message_id'], msg['message_id'])
        self.assertEqual(nack['nack_reason'], 'Unknown: foo')

    @inlineCallbacks
    def test_outbound_missing_fields(self):
        msg = yield self.tx_helper.make_dispatch_outbound(
            from_addr='456',
            to_addr='+123',
            content=None)

        [nack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assertEqual(nack['event_type'], 'nack')
        self.assertEqual(nack['user_message_id'], msg['message_id'])
        self.assertEqual(nack['sent_message_id'], msg['message_id'])
        self.assertEqual(nack['nack_reason'], 'Missing fields: content')
