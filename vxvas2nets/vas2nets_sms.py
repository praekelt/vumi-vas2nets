import re
import json
import treq

from twisted.web import http
from twisted.internet.defer import inlineCallbacks, CancelledError
from twisted.internet.error import ConnectingCancelledError
from twisted.web._newclient import ResponseNeverReceived

from vumi.config import ConfigText, ConfigInt
from vumi.transports.httprpc import HttpRpcTransport


class Vas2NetsSmsTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    """Config for SMS transport."""

    outbound_url = ConfigText(
        "Url to use for outbound messages",
        required=True)

    outbound_request_timeout = ConfigInt(
        "Timeout duration in seconds for requests for sending messages, or "
        "null for no timeout",
        default=None)

    reply_outbound_url = ConfigText(
        "Url to use for reply outbound messages",
        required=True)

    username = ConfigText(
        "Username to use for outbound messages",
        required=True)

    password = ConfigText(
        "Password to use for outbound messages",
        required=True)


class Vas2NetsSmsTransport(HttpRpcTransport):
    CONFIG_CLASS = Vas2NetsSmsTransportConfig
    ERROR_RE = re.compile(r'^(?P<code>ERR-\d+) (?P<message>.*)$')

    EXPECTED_FIELDS = frozenset([
        'sender',
        'receiver',
        'msgdata',
        'recvtime',
        'msgid',
        'operator'
    ])

    EXPECTED_MESSAGE_FIELDS = frozenset([
        'from_addr',
        'to_addr',
        'content'
    ])

    SEND_FAIL_TYPES = {
        'ERR-11': 'missing_username',
        'ERR-12': 'missing_password',
        'ERR-13': 'missing_destination',
        'ERR-14': 'missing_sender_id',
        'ERR-15': 'missing_message',
        'ERR-21': 'ender_id_too_long',
        'ERR-33': 'invalid_login',
        'ERR-41': 'insufficient_credit',
        'ERR-70': 'invalid_destination_number',
        'ERR-51': 'invalid_message_id',
        'ERR-52': 'system_error',
    }

    ENCODING = 'utf-8'

    transport_type = 'sms'

    def get_request_dict(self, request):
        return {
            'uri': request.uri,
            'method': request.method,
            'path': request.path,
            'content': request.content.read(),
            'headers': dict(request.requestHeaders.getAllRawHeaders()),
        }

    def get_message_dict(self, message_id, vals):
        return {
            'message_id': message_id,
            'from_addr': vals['sender'],
            'from_addr_type': 'msisdn',
            'to_addr': vals['receiver'],
            'content': vals['msgdata'],
            'provider': vals['operator'],
            'transport_type': self.transport_type,
            'transport_metadata': {'vas2nets_sms': {'msgid': vals['msgid']}}
        }

    def get_send_url(self, message):
        if self.use_mo_response_url() and self.is_mo_response(message):
            return self.config['reply_outbound_url']
        else:
            return self.config['outbound_url']

    def get_send_params(self, message):
        params = {
            'username': self.config['username'],
            'password': self.config['password'],
            'sender': message['from_addr'],
            'receiver': message['to_addr'],
            'message': message['content'],

            # From docs:
            # flag indicating normal text message
            # 0 or 1  Note: 1=flash, 0=text (This is optional, the default is 0
            # i.e text)
            # From testing, it appears they are expecting this value to always
            # be 1
            'message_type': '1',
        }

        id = get_in(message, 'transport_metadata', 'vas2nets_sms', 'msgid')

        # from docs:
        # If MO Message ID is validated, MT will not be charged.
        # Only one free MT is allowed for each MO.
        if id is not None and self.use_mo_response_url():
            params['message_id'] = id

        return params

    def get_send_fail_type(self, code):
        return self.SEND_FAIL_TYPES.get(code, 'request_fail_unknown')

    def use_mo_response_url(self):
        return self.config.get('reply_outbound_url') is not None

    def is_mo_response(self, message):
        return get_in(message, 'transport_metadata', 'vas2nets_sms', 'msgid')

    def get_nack_reason(self, error):
        description = {
            'ERR-11': 'Missing username',
            'ERR-12': 'Missing password',
            'ERR-13': 'Missing destination',
            'ERR-14': 'Missing sender id',
            'ERR-15': 'Missing message',
            'ERR-21': 'Ender id too long',
            'ERR-33': 'Invalid login',
            'ERR-41': 'Insufficient credit',
            'ERR-70': 'Invalid destination number',
            'ERR-52': 'System error'
        }.get(error)

        if description is not None:
            return "%s (%s)" % (description, error)
        else:
            return "Unknown: %s" % (error,)

    def get_send_fail_reason(self, error):
        return self.SEND_FAIL_REASONS.get(
            error, "Unknown request failure: %s" % (error,))

    def get_send_status(self, content):
        match = self.ERROR_RE.search(content)

        if match is None:
            return {
                'code': None,
                'message': content
            }
        else:
            return {
                'code': match.group('code'),
                'message': match.group('message'),
            }

    def respond(self, message_id, code, body=None):
        if body is None:
            body = {}

        self.finish_request(message_id, json.dumps(body), code=code)

    def send_message(self, message):
        return treq.get(
            url=self.get_send_url(message),
            params=self.get_send_params(message),
            timeout=self.config.get('outbound_request_timeout'))

    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        try:
            vals, errors = self.get_field_values(request, self.EXPECTED_FIELDS)
        except UnicodeDecodeError:
            yield self.handle_decode_error(message_id, request)
            return

        if errors:
            yield self.handle_bad_request_fields(message_id, request, errors)
        else:
            yield self.handle_inbound_message(message_id, request, vals)

    @inlineCallbacks
    def handle_decode_error(self, message_id, request):
        req = self.get_request_dict(request)
        self.log.error('Bad request encoding: %r' % req)
        self.respond(message_id, http.BAD_REQUEST, {'invalid_request': req})

        yield self.add_status(
            component='inbound',
            status='down',
            type='request_decode_error',
            message='Bad request encoding',
            details={'request': req})

    @inlineCallbacks
    def handle_bad_request_fields(self, message_id, request, errors):
        req = self.get_request_dict(request)

        self.log.error(
            "Bad request fields for inbound message: %s %s"
            % (errors, req,))

        self.respond(message_id, http.BAD_REQUEST, errors)

        yield self.add_status(
            component='inbound',
            status='down',
            type='request_bad_fields',
            message='Bad request fields',
            details={
                'request': req,
                'errors': errors
            })

    @inlineCallbacks
    def handle_inbound_message(self, message_id, request, vals):
        yield self.publish_message(
            **self.get_message_dict(message_id, vals))

        self.respond(message_id, http.OK, {})

        yield self.add_status(
            component='inbound',
            status='ok',
            type='request_success',
            message='Request successful')

    @inlineCallbacks
    def handle_outbound_message(self, message):
        missing_fields = self.ensure_message_values(
            message, self.EXPECTED_MESSAGE_FIELDS)

        if missing_fields:
            yield self.reject_message(message, missing_fields)
            return

        try:
            resp = yield self.send_message(message)
        except (ResponseNeverReceived, ConnectingCancelledError,
                CancelledError):
            yield self.handle_send_timeout(message)
            return

        content = yield resp.content()
        status = self.get_send_status(content)
        self.emit('Vas2Nets response for %s: %s, status: %s' % (
            message['message_id'], content, status))

        if resp.code == http.OK and status['code'] is None:
            yield self.handle_outbound_success(message)
        else:
            yield self.handle_outbound_fail(message, status)

    def emit(self, log):
        if self.get_static_config().noisy:
            self.log.info(log)

    @inlineCallbacks
    def handle_send_timeout(self, message):
        self.emit('Timing out: %s' % (message,))
        yield self.publish_nack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'],
            reason='Request timeout')

        yield self.add_status(
            component='outbound',
            status='down',
            type='request_timeout',
            message='Request timeout')

    @inlineCallbacks
    def handle_outbound_success(self, message):
        self.emit('Outbound success: %s' % (message,))
        yield self.publish_ack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'])

        yield self.add_status(
            component='outbound',
            status='ok',
            type='request_success',
            message='Request successful')

    @inlineCallbacks
    def handle_outbound_fail(self, message, status):
        self.emit('Outbound fail: %s' % (message,))
        yield self.publish_nack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'],
            reason=status['message'])

        yield self.add_status(
            component='outbound',
            status='down',
            type=self.get_send_fail_type(status['code']),
            message=status['message'])


def get_in(data, *keys):
    for key in keys:
        data = data.get(key)

        if data is None:
            return None

    return data
