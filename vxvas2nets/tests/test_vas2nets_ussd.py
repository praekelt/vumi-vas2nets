import json
from twisted.internet.defer import inlineCallbacks
from twisted.internet.task import Clock
import urllib

from vumi.message import TransportUserMessage
from vumi.tests.helpers import VumiTestCase
from vumi.transports.httprpc.tests.helpers import HttpRpcTransportHelper

from vxvas2nets import Vas2NetsUssdTransport


class TestVas2NetsUssdTransport(VumiTestCase):
    @inlineCallbacks
    def setUp(self):
        self.clock = Clock()
        self.patch(Vas2NetsUssdTransport, 'get_clock', lambda _: self.clock)

        self.config = {
            'web_port': 0,
            'web_path': '/api/v1/vas2nets/ussd/',
            'publish_status': True,
            'ussd_number': '*123*45#',
        }
        self.tx_helper = self.add_helper(
            HttpRpcTransportHelper(Vas2NetsUssdTransport))
        self.transport = yield self.tx_helper.get_transport(self.config)
        self.transport_url = self.transport.get_transport_url(
            self.config['web_path'])

    def assert_message(self, msg, **field_values):
        for field, expected_value in field_values.iteritems():
            self.assertEqual(msg[field], expected_value)

    @inlineCallbacks
    def test_inbound_message(self):
        '''If there is a new request created, there should be a new inbound
        message.'''
        self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        self.assert_message(
            msg, content='test', from_addr='+123', from_addr_type='msisdn',
            provider='vas2nets',
            session_event=TransportUserMessage.SESSION_NEW,
            transport_metadata={'vas2nets_ussd': {'sessionid': '4'}})
        # Close the request to properly clean up the test
        self.transport.finish_request(msg['message_id'], '')

    @inlineCallbacks
    def test_inbound_status(self):
        '''A status should be sent if the message was decoded correctly'''
        self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        [status] = yield self.tx_helper.get_dispatched_statuses()

        self.assertEqual(status['status'], 'ok')
        self.assertEqual(status['component'], 'request')
        self.assertEqual(status['type'], 'request_parsed')
        self.assertEqual(status['message'], 'Request parsed')

        # Close the request to properly clean up the test
        self.transport.finish_request(msg['message_id'], '')

    @inlineCallbacks
    def test_inbound_cannot_decode(self):
        '''If the content cannot be decoded, an error should be sent back'''
        userdata = "Who are you?".encode('utf-32')
        response = yield self.tx_helper.mk_request(
            userdata=userdata, endofsession=False, msisdn='+123',
            sessionid='4')
        self.assertEqual(response.code, 400)

        body = json.loads(response.delivered_body)
        request = body['invalid_request']
        self.assertEqual(request['content'], '')
        self.assertEqual(request['path'], self.config['web_path'])
        self.assertEqual(request['method'], 'GET')
        self.assertEqual(request['headers']['Connection'], ['close'])
        encoded_str = urllib.urlencode({'userdata': userdata})
        self.assertTrue(encoded_str in request['uri'])

    @inlineCallbacks
    def test_inbound_cannot_decode_status(self):
        '''If the request cannot be decoded, a status event should be sent'''
        userdata = "Who are you?".encode('utf-32')
        yield self.tx_helper.mk_request(
            userdata=userdata, endofsession=False, msisdn='+123',
            sessionid='4')

        [status] = self.tx_helper.get_dispatched_statuses()
        self.assertEqual(status['component'], 'request')
        self.assertEqual(status['status'], 'down')
        self.assertEqual(status['type'], 'invalid_encoding')
        self.assertEqual(status['message'], 'Invalid encoding')

        request = status['details']['request']
        self.assertEqual(request['content'], '')
        self.assertEqual(request['path'], self.config['web_path'])
        self.assertEqual(request['method'], 'GET')
        self.assertEqual(request['headers']['Connection'], ['close'])
        encoded_str = urllib.urlencode({'userdata': userdata})
        self.assertTrue(encoded_str in request['uri'])

    @inlineCallbacks
    def test_request_with_missing_parameters(self):
        '''If there are missing parameters, an error should be sent back'''
        response = yield self.tx_helper.mk_request()

        body = json.loads(response.delivered_body)
        self.assertEqual(set(['missing_parameter']), set(body.keys()))
        self.assertEqual(
            sorted(body['missing_parameter']),
            ['endofsession', 'msisdn', 'sessionid', 'userdata'])
        self.assertEqual(response.code, 400)

    @inlineCallbacks
    def test_status_with_missing_parameters(self):
        '''If the request has missing parameters, a status should be sent'''
        yield self.tx_helper.mk_request()

        [status] = yield self.tx_helper.get_dispatched_statuses()
        self.assertEqual(status['status'], 'down')
        self.assertEqual(status['component'], 'request')
        self.assertEqual(status['type'], 'invalid_inbound_fields')
        self.assertEqual(
            sorted(status['details']['missing_parameter']),
            ['endofsession', 'msisdn', 'sessionid', 'userdata'])

    @inlineCallbacks
    def test_request_with_unexpected_parameters(self):
        '''If the request has unexpected parameters, an error should be sent
        back'''
        response = yield self.tx_helper.mk_request(
            userdata='', endofsession=False, msisdn='+123', sessionid='4',
            unexpected_p1='', unexpected_p2='')

        self.assertEqual(response.code, 400)
        body = json.loads(response.delivered_body)
        self.assertEqual(set(['unexpected_parameter']), set(body.keys()))
        self.assertEqual(
            sorted(body['unexpected_parameter']),
            ['unexpected_p1', 'unexpected_p2'])

    @inlineCallbacks
    def test_status_with_unexpected_parameters(self):
        '''A request with unexpected parameters should send a TransportStatus
        with the relevant details.'''
        yield self.tx_helper.mk_request(
            userdata='', endofsession=False, msisdn='+123', sessionid='4',
            unexpected_p1='', unexpected_p2='')

        [status] = yield self.tx_helper.get_dispatched_statuses()
        self.assertEqual(status['status'], 'down')
        self.assertEqual(status['component'], 'request')
        self.assertEqual(status['type'], 'invalid_inbound_fields')
        self.assertEqual(sorted(status['details']['unexpected_parameter']), [
            'unexpected_p1', 'unexpected_p2'])
