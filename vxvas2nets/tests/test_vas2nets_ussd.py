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

    def assert_ack(self, ack, reply):
        self.assertEqual(ack.payload['event_type'], 'ack')
        self.assertEqual(ack.payload['user_message_id'], reply['message_id'])
        self.assertEqual(ack.payload['sent_message_id'], reply['message_id'])

    def assert_nack(self, nack, reply, reason):
        self.assertEqual(nack.payload['event_type'], 'nack')
        self.assertEqual(nack.payload['user_message_id'], reply['message_id'])
        self.assertEqual(nack.payload['nack_reason'], reason)

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
        self.transport.close_request(msg['message_id'])

    @inlineCallbacks
    def test_inbound_message_end_of_session(self):
        '''If an inbound message comes in that signals the end of the session,
        the session_event should be a SESSION_CLOSE, and the session should
        be removed.'''
        yield self.transport.session_manager.create_session('4', foo='bar')
        response = yield self.tx_helper.mk_request(
            userdata='test', endofsession='true', msisdn='+123', sessionid='4')
        self.assertEqual(json.loads(response.delivered_body), {
            'endofsession': True,
            'userdata': '',
            'msisdn': '+123',
        })
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        self.assert_message(
            msg, content='test', from_addr='+123', from_addr_type='msisdn',
            provider='vas2nets',
            session_event=TransportUserMessage.SESSION_CLOSE,
            transport_metadata={'vas2nets_ussd': {'sessionid': '4'}})
        self.assertEqual(self.transport.get_request(msg['message_id']), None)
        session = yield self.transport.session_manager.load_session('4')
        self.assertEqual(session, {})

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
        self.transport.close_request(msg['message_id'])

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

    @inlineCallbacks
    def test_inbound_resume_and_reply_with_end(self):
        '''When we reply to a resumed session with a message to close the
        session, the endofsession field should be True.'''
        yield self.transport.session_manager.create_session('4')

        d = self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        self.assert_message(
            msg, session_event=TransportUserMessage.SESSION_RESUME,
            content='test')

        reply = msg.reply('test reply', continue_session=False)
        self.tx_helper.dispatch_outbound(reply)
        response = yield d
        self.assertEqual(json.loads(response.delivered_body), {
            'endofsession': True,
            'userdata': 'test reply',
            'msisdn': '+123',
        })
        self.assertEqual(response.code, 200)

        [ack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assert_ack(ack, reply)

    @inlineCallbacks
    def test_inbound_resume_and_reply_with_resume(self):
        '''When we reply to a resumed session with a message to keep open the
        session, the endofsession field should be False.'''
        yield self.transport.session_manager.create_session('4')

        d = self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        self.assert_message(
            msg, session_event=TransportUserMessage.SESSION_RESUME,
            content='test')

        reply = msg.reply('test reply', continue_session=True)
        self.tx_helper.dispatch_outbound(reply)
        response = yield d
        self.assertEqual(json.loads(response.delivered_body), {
            'endofsession': False,
            'userdata': 'test reply',
            'msisdn': '+123',
        })
        self.assertEqual(response.code, 200)

        [ack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assert_ack(ack, reply)

    @inlineCallbacks
    def test_nack_insufficient_message_fields(self):
        '''When there are missong fields in a reply message, then we should
        respond with an appropriate nack message.'''
        reply = self.tx_helper.make_outbound(
            None, message_id='23', in_reply_to=None)
        self.tx_helper.dispatch_outbound(reply)
        [nack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assert_nack(nack, reply, 'Missing fields: in_reply_to, content')

    @inlineCallbacks
    def test_nack_http_http_response_failure(self):
        '''When we reply to a session that is no longer connected, then an
        appropriate nack message should be sent.'''
        reply = self.tx_helper.make_outbound(
            'There are some who call me ... Tim!', message_id='23',
            in_reply_to='some-number')
        self.tx_helper.dispatch_outbound(reply)
        [nack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assert_nack(
            nack, reply, 'Could not find original request.')

    @inlineCallbacks
    def test_status_quick_response(self):
        '''Ok status event should be sent if the response is quick.'''
        d = self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        yield self.tx_helper.clear_dispatched_statuses()

        self.tx_helper.dispatch_outbound(msg.reply('foo'))
        yield d

        [status] = yield self.tx_helper.get_dispatched_statuses()
        self.assertEqual(status['status'], 'ok')
        self.assertEqual(status['component'], 'response')
        self.assertEqual(status['message'], 'Response sent')
        self.assertEqual(status['type'], 'response_sent')

    @inlineCallbacks
    def test_status_degraded_slow_response(self):
        '''A degraded status event should be sent if the response took longer
        than 1 second.'''
        d = self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        yield self.tx_helper.clear_dispatched_statuses()

        self.clock.advance(self.transport.response_time_degraded + 0.1)

        self.tx_helper.dispatch_outbound(msg.reply('foo'))
        yield d

        [status] = yield self.tx_helper.get_dispatched_statuses()
        self.assertEqual(status['status'], 'degraded')
        self.assertTrue(
            str(self.transport.response_time_degraded) in status['reasons'][0])
        self.assertEqual(status['component'], 'response')
        self.assertEqual(status['type'], 'slow_response')
        self.assertEqual(status['message'], 'Slow response')

    @inlineCallbacks
    def test_status_down_very_slow_response(self):
        '''A down status event should be sent if the response took longer
        than 10 seconds.'''
        d = self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        yield self.tx_helper.clear_dispatched_statuses()

        self.clock.advance(self.transport.response_time_down + 0.1)

        self.tx_helper.dispatch_outbound(msg.reply('foo'))
        yield d

        [status] = yield self.tx_helper.get_dispatched_statuses()
        self.assertEqual(status['status'], 'down')
        self.assertTrue(
            str(self.transport.response_time_down) in status['reasons'][0])
        self.assertEqual(status['component'], 'response')
        self.assertEqual(status['type'], 'very_slow_response')
        self.assertEqual(status['message'], 'Very slow response')

    @inlineCallbacks
    def test_no_response_status_for_message_not_found(self):
        '''If we cannot find the starting timestamp for a message, no status
        message should be sent'''
        reply = self.tx_helper.make_outbound(
            'There are some who call me ... Tim!', message_id='23',
            in_reply_to='some-number')
        self.tx_helper.dispatch_outbound(reply)
        statuses = yield self.tx_helper.get_dispatched_statuses()
        self.assertEqual(len(statuses), 0)

    @inlineCallbacks
    def test_no_good_status_event_for_bad_responses(self):
        '''If the http response is not a good (200-399) response, then a
        status event shouldn't be sent, because we send different status
        events for those errors.'''
        d = self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')

        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        yield self.tx_helper.clear_dispatched_statuses()

        self.transport.finish_request(msg['message_id'], '', code=500)

        yield d

        statuses = yield self.tx_helper.get_dispatched_statuses()
        self.assertEqual(len(statuses), 0)

    @inlineCallbacks
    def test_no_degraded_status_event_for_bad_responses(self):
        '''If the http response is not a good (200-399) response, then a
        status event shouldn't be sent, because we send different status
        events for those errors.'''
        d = self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')

        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        yield self.tx_helper.clear_dispatched_statuses()

        self.clock.advance(self.transport.response_time_degraded + 0.1)

        self.transport.finish_request(msg['message_id'], '', code=500)

        yield d

        statuses = yield self.tx_helper.get_dispatched_statuses()
        self.assertEqual(len(statuses), 0)

    @inlineCallbacks
    def test_no_down_status_event_for_bad_responses(self):
        '''If the http response is not a good (200-399) response, then a
        status event shouldn't be sent, because we send different status
        events for those errors.'''
        d = self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')

        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        yield self.tx_helper.clear_dispatched_statuses()

        self.clock.advance(self.transport.response_time_down + 0.1)

        self.transport.finish_request(msg['message_id'], '', code=500)

        yield d

        statuses = yield self.tx_helper.get_dispatched_statuses()
        self.assertEqual(len(statuses), 0)

    @inlineCallbacks
    def test_status_down_timeout(self):
        '''A down status event should be sent if the response timed out'''
        d = self.tx_helper.mk_request(
            userdata='test', endofsession=False, msisdn='+123', sessionid='4')
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        yield self.tx_helper.clear_dispatched_statuses()

        self.clock.advance(self.transport.request_timeout + 0.1)

        self.tx_helper.dispatch_outbound(msg.reply('foo'))
        yield d

        [status] = yield self.tx_helper.get_dispatched_statuses()
        self.assertEqual(status['status'], 'down')
        self.assertTrue(
            str(self.transport.request_timeout) in status['reasons'][0])
        self.assertEqual(status['component'], 'response')
        self.assertEqual(status['type'], 'timeout')
        self.assertEqual(status['message'], 'Response timed out')
        self.assertEqual(status['details'], {
            'response_time': self.transport.request_timeout + 0.1,
        })
