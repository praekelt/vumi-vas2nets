import json
import treq

from twisted.web import http
from twisted.internet.defer import inlineCallbacks, returnValue, CancelledError

from vumi import log
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

    username = ConfigText(
        "Username to use for outbound messages",
        required=True)

    password = ConfigText(
        "Password to use for outbound messages",
        required=True)


class Vas2NetsSmsTransport(HttpRpcTransport):
    CONFIG_CLASS = Vas2NetsSmsTransportConfig

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
        'ERR-52': 'system_error',
    }

    SEND_FAIL_REASONS = {
        'ERR-11': 'Missing username (ERR-11)',
        'ERR-12': 'Missing password (ERR-12)',
        'ERR-13': 'Missing destination (ERR-13)',
        'ERR-14': 'Missing sender id (ERR-14)',
        'ERR-15': 'Missing message (ERR-15)',
        'ERR-21': 'Ender id too long (ERR-21)',
        'ERR-33': 'Invalid login (ERR-33)',
        'ERR-41': 'Insufficient credit (ERR-41)',
        'ERR-70': 'Invalid destination number (ERR-70)',
        'ERR-52': 'System error (ERR-52)'
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

    def get_outbound_params(self, message):
        params = {
            'username': self.config['username'],
            'password': self.config['password'],
            'sender': message['from_addr'],
            'receiver': message['to_addr'],
            'message': message['content'],
        }

        id = get_in(message, 'transport_metadata', 'vas2nets_sms', 'msgid')

        # from docs:
        # If MO Message ID is validated, MT will not be charged.
        # Only one free MT is allowed for each MO.
        if id is not None:
            params['message_id'] = id

        return params

    def get_send_fail_reason(self, error):
        return self.SEND_FAIL_REASONS.get(
            error, "Unknown request failure: %s" % (error,))

    def get_send_fail_type(self, error):
        return self.SEND_FAIL_TYPES.get(error, 'request_fail_unknown')

    def respond(self, message_id, code, body=None):
        if body is None:
            body = {}

        self.finish_request(message_id, json.dumps(body), code=code)

    def send_message(self, message):
        return treq.get(
            url=self.config['outbound_url'],
            params=self.get_outbound_params(message),
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

        log.error('Bad request encoding: %r' % req)

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

        log.error(
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
            returnValue((yield self.reject_message(message, missing_fields)))

        try:
            resp = yield self.send_message(message)
        except CancelledError:
            yield self.handle_send_timeout(message)

        # NOTE: we are assuming here that they send us a non-200 response for
        # error cases (this is not mentioned in the docs)
        if resp.code == http.OK:
            returnValue((yield self.handle_outbound_success(message, resp)))
        else:
            returnValue((yield self.handle_outbound_fail(message, resp)))

    @inlineCallbacks
    def handle_send_timeout(self, message):
        yield self.add_status(
            component='outbound',
            status='down',
            type='request_timeout',
            message='Request timeout')

    @inlineCallbacks
    def handle_outbound_success(self, message, resp):
        ack = yield self.publish_ack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'])

        yield self.add_status(
            component='outbound',
            status='ok',
            type='request_success',
            message='Request successful')

        returnValue(ack)

    @inlineCallbacks
    def handle_outbound_fail(self, message, resp):
        error = (yield resp.content())
        reason = self.get_send_fail_reason(error)

        nack = yield self.publish_nack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'],
            reason=reason)

        yield self.add_status(
            component='outbound',
            status='down',
            type=self.get_send_fail_type(error),
            message=reason)

        returnValue(nack)


def get_in(data, *keys):
    for key in keys:
        data = data.get(key)

        if data is None:
            return None

    return data
