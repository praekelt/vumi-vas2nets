import json
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web import http
from vumi.components.session import SessionManager
from vumi.config import ConfigDict, ConfigInt, ConfigText
from vumi.message import TransportUserMessage
from vumi.transports.httprpc import HttpRpcTransport


class Vas2NetsUssdTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    """Config for Dmark USSD transport."""

    ussd_session_timeout = ConfigInt(
        "Number of seconds before USSD session information stored in Redis"
        " expires.",
        default=60, static=True)

    redis_manager = ConfigDict(
        "Redis client configuration.", default={}, static=True)

    ussd_number = ConfigText(
        "The number to dial to access the USSD line. Sets the `from_addr`"
        "attribute on inbound messages.", required=True, static=True)


class Vas2NetsUssdTransport(HttpRpcTransport):
    CONFIG_CLASS = Vas2NetsUssdTransportConfig
    EXPECTED_FIELDS = frozenset([
        'userdata', 'msisdn', 'sessionid',
    ])

    transport_type = 'ussd'

    @inlineCallbacks
    def setup_transport(self):
        yield super(Vas2NetsUssdTransport, self).setup_transport()
        config = self.get_static_config()
        r_prefix = "vumi.transports.vas2nets_ussd:%s" % self.transport_name
        self.session_manager = yield SessionManager.from_redis_config(
            config.redis_manager, r_prefix,
            max_session_length=config.ussd_session_timeout)
        self.ussd_number = config.ussd_number

    def get_request_dict(self, request):
        return {
            'uri': request.uri,
            'method': request.method,
            'path': request.path,
            'content': request.content.read(),
            'headers': dict(request.requestHeaders.getAllRawHeaders()),
        }

    @inlineCallbacks
    def session_event_for_transaction(self, session_id):
        session = yield self.session_manager.load_session(session_id)
        if session:
            yield self.session_manager.save_session(session_id, session)
            returnValue(TransportUserMessage.SESSION_RESUME)
        else:
            yield self.session_manager.create_session(
                session_id, transaction_id=session_id)
            returnValue(TransportUserMessage.SESSION_NEW)

    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        try:
            values, errors = self.get_field_values(
                request, self.EXPECTED_FIELDS)
        except UnicodeDecodeError:
            self.log.msg('Bad request encoding: %r' % request)
            request_dict = self.get_request_dict(request)
            self.finish_request(
                message_id, json.dumps({'invalid_request': request_dict}),
                code=http.BAD_REQUEST)
            yield self.add_status(
                component='request', status='down', type='invalid_encoding',
                message='Invalid encoding', details={'request': request_dict})
            return

        if errors:
            self.log.msg('Unhappy incoming message: %s ' % (errors,))
            yield self.finish_request(
                message_id, json.dumps(errors), code=http.BAD_REQUEST
            )
            yield self.add_status(
                component='request', status='down',
                type='invalid_inbound_fields',
                message='Invalid inbound fields', details=errors)
            return

        yield self.add_status(
            component='request', status='ok', type='request_parsed',
            message='Request parsed',)

        session_event = yield self.session_event_for_transaction(
            values['sessionid'])

        yield self.publish_message(
            message_id=message_id,
            content=values['userdata'],
            from_addr=values['msisdn'],
            from_addr_type='msisdn',
            to_addr=self.ussd_number,
            provider='vas2nets',
            session_event=session_event,
            transport_type=self.transport_type,
            transport_metadata={
                'vas2nets_ussd': {
                    'sessionid': values['sessionid'],
                }
            })

    @inlineCallbacks
    def handle_outbound_message(self, message):
        self.emit("Vas2NetsUssdTransport consuming %r" % (message,))

        missing_fields = self.ensure_message_values(
            message, ['in_reply_to', 'content', 'to_addr'])
        if missing_fields:
            nack = yield self.reject_message(message, missing_fields)
            returnValue(nack)

        endofsession = (
            message["session_event"] == TransportUserMessage.SESSION_CLOSE)
        if endofsession is True:
            yield self.session_manager.clear_session(
                message['transport_metadata']['vas2nets_ussd']['sessionid'])

        response_data = {
            'userdata': message['content'],
            'endofsession': endofsession,
            'msisdn': message['to_addr'],
        }

        response_id = self.finish_request(
            message['in_reply_to'], json.dumps(response_data))

        if response_id is not None:
            ack = yield self.publish_ack(
                user_message_id=message['message_id'],
                sent_message_id=message['message_id'])
            returnValue(ack)
        else:
            nack = yield self.publish_nack(
                user_message_id=message['message_id'],
                sent_message_id=message['message_id'],
                reason="Could not find original request.")
            returnValue(nack)

    def on_down_response_time(self, message_id, time):
        request = self.get_request(message_id)
        # We send different status events for error responses
        if request.code < 200 or request.code >= 300:
            return
        return self.add_status(
            component='response',
            status='down',
            type='very_slow_response',
            message='Very slow response',
            reasons=[
                'Response took longer than %fs' % (
                    self.response_time_down,)
            ],
            details={
                'response_time': time,
            })

    def on_degraded_response_time(self, message_id, time):
        request = self.get_request(message_id)
        # We send different status events for error responses
        if request.code < 200 or request.code >= 300:
            return
        return self.add_status(
            component='response',
            status='degraded',
            type='slow_response',
            message='Slow response',
            reasons=[
                'Response took longer than %fs' % (
                    self.response_time_degraded,)
            ],
            details={
                'response_time': time,
            })

    def on_good_response_time(self, message_id, time):
        request = self.get_request(message_id)
        # We send different status events for error responses
        if request.code < 200 or request.code >= 400:
            return
        return self.add_status(
            component='response',
            status='ok',
            type='response_sent',
            message='Response sent',
            details={
                'response_time': time,
            })

    def on_timeout(self, message_id, time):
        return self.add_status(
            component='response',
            status='down',
            type='timeout',
            message='Response timed out',
            reasons=[
                'Response took longer than %fs' % (
                    self.request_timeout,)
            ],
            details={
                'response_time': time,
            })
