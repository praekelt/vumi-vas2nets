# -*- encoding: utf-8 -*-

import json
from urllib import urlencode

from twisted.web import http
from twisted.internet import reactor
from twisted.internet.task import Clock
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web.client import HTTPConnectionPool
from twisted.web.server import NOT_DONE_YET

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

        self.tx_helper = self.add_helper(
            HttpRpcTransportHelper(Vas2NetsSmsTransport))

        connection_pool = HTTPConnectionPool(reactor, persistent=False)
        treq._utils.set_global_pool(connection_pool)

    @inlineCallbacks
    def mk_transport(self, **kw):
        config = {
            'web_port': 0,
            'web_path': '/api/v1/vas2nets/sms/',
            'publish_status': True,
            'outbound_url': self.remote_server.url,
            'username': 'root',
            'password': 't00r',
        }
        config.update(kw)

        transport = yield self.tx_helper.get_transport(config)
        self.patch(transport, 'get_clock', lambda _: self.clock)
        returnValue(transport)

    def patch_reactor_call_later(self):
        self.patch(reactor, 'callLater', self.clock.callLater)

    def capture_remote_requests(self, response='OK.1234'):
        def handler(req):
            reqs.append(req)
            return response

        reqs = []
        self.remote_request_handler = handler
        return reqs

    def remote_handle_request(self, req):
        return self.remote_request_handler(req)

    def get_host(self, transport):
        addr = transport.web_resource.getHost()
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

    def assert_request_params(self, transport, req, params):
        self.assert_contains_items(req, {
            'method': 'GET',
            'path': transport.config['web_path'],
            'content': '',
            'headers': {
                'Connection': ['close'],
                'Host': [self.get_host(transport)]
            }
        })

        self.assert_uri(req['uri'], transport.config['web_path'], params)

    @inlineCallbacks
    def test_inbound(self):
        yield self.mk_transport()

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

        [status] = self.tx_helper.get_dispatched_statuses()

        self.assert_contains_items(status, {
            'status': 'ok',
            'component': 'inbound',
            'type': 'request_success',
            'message': 'Request successful',
        })

    @inlineCallbacks
    def test_inbound_decode_error(self):
        transport = yield self.mk_transport()

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

        self.assert_request_params(transport, req, {
            'sender': '+123',
            'receiver': '456',
            'msgdata': u'ポケモン'.encode('utf-16'),
            'operator': 'MTN',
            'recvtime': '2012-02-27 19-50-07',
            'msgid': '789'
        })

        [status] = self.tx_helper.get_dispatched_statuses()

        self.assert_contains_items(status, {
            'status': 'down',
            'component': 'inbound',
            'type': 'request_decode_error',
            'message': 'Bad request encoding',
        })

        self.assert_request_params(transport, status['details']['request'], {
            'sender': '+123',
            'receiver': '456',
            'msgdata': u'ポケモン'.encode('utf-16'),
            'operator': 'MTN',
            'recvtime': '2012-02-27 19-50-07',
            'msgid': '789'
        })

    @inlineCallbacks
    def test_inbound_bad_params(self):
        transport = yield self.mk_transport()

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

        self.assertEqual(
            body['unexpected_parameter'],
            ['foo'])

        self.assertEqual(
            sorted(body['missing_parameter']),
            ['msgdata', 'receiver'])

        [status] = self.tx_helper.get_dispatched_statuses()

        self.assert_contains_items(status, {
            'status': 'down',
            'component': 'inbound',
            'type': 'request_bad_fields',
            'message': 'Bad request fields',
        })

        self.assert_request_params(transport, status['details']['request'], {
            'sender': '+123',
            'foo': '456',
            'operator': 'MTN',
            'recvtime': '2012-02-27 19-50-07',
            'msgid': '789'
        })

        self.assertEqual(
            status['details']['errors']['unexpected_parameter'],
            ['foo'])

        self.assertEqual(
            sorted(status['details']['errors']['missing_parameter']),
            ['msgdata', 'receiver'])

    @inlineCallbacks
    def test_outbound_non_reply(self):
        yield self.mk_transport()
        reqs = self.capture_remote_requests()

        msg = yield self.tx_helper.make_dispatch_outbound(
            from_addr='456',
            to_addr='+123',
            content='hi')

        [req] = reqs
        self.assertEqual(req.method, 'GET')
        self.assertEqual(req.args, {
            'username': ['root'],
            'message': ['hi'],
            'password': ['t00r'],
            'sender': ['456'],
            'receiver': ['+123'],
        })

        [ack] = yield self.tx_helper.wait_for_dispatched_events(1)

        self.assert_contains_items(ack, {
            'user_message_id': msg['message_id'],
            'sent_message_id': msg['message_id'],
        })

        [status] = self.tx_helper.get_dispatched_statuses()

        self.assert_contains_items(status, {
            'status': 'ok',
            'component': 'outbound',
            'type': 'request_success',
            'message': 'Request successful',
        })

    @inlineCallbacks
    def test_outbound_reply(self):
        yield self.mk_transport()
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
        self.tx_helper.clear_dispatched_statuses()
        yield self.tx_helper.dispatch_outbound(msg)

        [req] = reqs
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

        self.assert_contains_items(ack, {
            'user_message_id': msg['message_id'],
            'sent_message_id': msg['message_id'],
        })

        [status] = self.tx_helper.get_dispatched_statuses()

        self.assert_contains_items(status, {
            'status': 'ok',
            'component': 'outbound',
            'type': 'request_success',
            'message': 'Request successful',
        })

    @inlineCallbacks
    def test_outbound_known_error(self):
        def handler(req):
            req.setResponseCode(400)
            [error] = req.args['message']
            return error

        transport = yield self.mk_transport()
        self.remote_request_handler = handler

        nacks = {}
        statuses = {}

        for error in transport.SEND_FAIL_TYPES.iterkeys():
            msg = yield self.tx_helper.make_dispatch_outbound(
                from_addr='456',
                to_addr='+123',
                content=error)

            [nack] = yield self.tx_helper.wait_for_dispatched_events(1)
            [status] = self.tx_helper.get_dispatched_statuses()
            self.tx_helper.clear_dispatched_events()
            self.tx_helper.clear_dispatched_statuses()
            nacks[error] = nack
            statuses[error] = status

            self.assert_contains_items(nack, {
                'event_type': 'nack',
                'user_message_id': msg['message_id'],
                'sent_message_id': msg['message_id'],
            })

            self.assert_contains_items(status, {
                'status': 'down',
                'component': 'outbound',
            })

        self.assertEqual(
            map_get(nacks, 'nack_reason'),
            transport.SEND_FAIL_REASONS)

        self.assertEqual(
            map_get(statuses, 'message'),
            transport.SEND_FAIL_REASONS)

        self.assertEqual(
            map_get(statuses, 'type'),
            transport.SEND_FAIL_TYPES)

    @inlineCallbacks
    def test_outbound_unknown_error(self):
        def handler(req):
            req.setResponseCode(400)
            return 'foo'

        yield self.mk_transport()
        self.remote_request_handler = handler

        msg = yield self.tx_helper.make_dispatch_outbound(
            from_addr='456',
            to_addr='+123',
            content='hi')

        [nack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assert_contains_items(nack, {
            'event_type': 'nack',
            'user_message_id': msg['message_id'],
            'sent_message_id': msg['message_id'],
            'nack_reason': 'Unknown request failure: foo',
        })

        [status] = self.tx_helper.get_dispatched_statuses()

        self.assert_contains_items(status, {
            'status': 'down',
            'component': 'outbound',
            'type': 'request_fail_unknown',
            'message': 'Unknown request failure: foo',
        })

    @inlineCallbacks
    def test_outbound_missing_fields(self):
        yield self.mk_transport()

        msg = yield self.tx_helper.make_dispatch_outbound(
            from_addr='456',
            to_addr='+123',
            content=None)

        [nack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assert_contains_items(nack, {
            'event_type': 'nack',
            'user_message_id': msg['message_id'],
            'sent_message_id': msg['message_id'],
            'nack_reason': 'Missing fields: content',
        })

    @inlineCallbacks
    def test_outbound_timeout(self):
        self.remote_request_handler = lambda _: NOT_DONE_YET
        yield self.mk_transport(outbound_request_timeout=3)

        msg = self.tx_helper.make_outbound(
            from_addr='456',
            to_addr='+123',
            content='hi')

        self.patch_reactor_call_later()
        d = self.tx_helper.dispatch_outbound(msg)

        self.clock.advance(2)
        self.assertEqual(self.tx_helper.get_dispatched_statuses(), [])

        self.clock.advance(2)
        yield d

        [status] = self.tx_helper.get_dispatched_statuses()

        self.assert_contains_items(status, {
            'status': 'down',
            'component': 'outbound',
            'type': 'request_timeout',
            'message': 'Request timeout',
        })


def map_get(collection, key):
    return dict((k, d.get(key)) for (k, d) in collection.iteritems())
