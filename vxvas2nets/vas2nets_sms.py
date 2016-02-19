import json
import treq
from twisted.web import http

from twisted.internet.defer import inlineCallbacks, returnValue

from vumi import log
from vumi.config import ConfigText
from vumi.transports.httprpc import HttpRpcTransport


class Vas2NetsSmsTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    """Config for SMS transport."""

    outbound_url = ConfigText(
        "Url to use for outbound messages",
        required=True)

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

    def get_outbound_url(self, message):
        id = get_in(message, 'transport_metadata', 'vas2nets_sms', 'msgid')

        if id is not None:
            return self.config['reply_outbound_url']
        else:
            return self.config['outbound_url']

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

    def respond(self, message_id, code, body=None):
        if body is None:
            body = {}

        self.finish_request(message_id, json.dumps(body), code=code)

    def send_message(self, message):
        return treq.get(
            url=self.get_outbound_url(message),
            params=self.get_outbound_params(message))

    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        log.msg("asfsdf")

        try:
            vals, errors = self.get_field_values(request, self.EXPECTED_FIELDS)
        except UnicodeDecodeError:
            yield self.handle_decode_error(message_id, request)
            return

        if errors:
            yield self.handle_bad_request_fields(message_id, request, errors)
        else:
            yield self.handle_inbound_message(message_id, request, vals)

    def handle_decode_error(self, message_id, request):
        req = self.get_request_dict(request)

        log.error('Bad request encoding: %r' % req)

        self.respond(message_id, http.BAD_REQUEST, {'invalid_request': req})

        # TODO publish status

    def handle_bad_request_fields(self, message_id, request, errors):
        req = self.get_request_dict(request)

        log.error(
            "Bad request fields for inbound message: %s %s"
            % (errors, req,))

        self.respond(message_id, http.BAD_REQUEST, errors)

        # TODO publish status

    @inlineCallbacks
    def handle_inbound_message(self, message_id, request, vals):
        yield self.publish_message(
            **self.get_message_dict(message_id, vals))

        self.respond(message_id, http.OK, {})

        # TODO publish status

    @inlineCallbacks
    def handle_outbound_message(self, message):
        missing_fields = self.ensure_message_values(
            message, self.EXPECTED_MESSAGE_FIELDS)

        if missing_fields:
            returnValue((yield self.reject_message(message, missing_fields)))

        # TODO status event for request timeout
        # TODO status event for succcessful requests
        resp = yield self.send_message(message)

        # NOTE: we are assuming here that they send us a non-200 response for
        # error cases (this is not mentioned in the docs)
        if resp.code == http.OK:
            returnValue((yield self.handle_outbound_success(message, resp)))
        else:
            returnValue((yield self.handle_outbound_fail(message, resp)))

    @inlineCallbacks
    def handle_outbound_success(self, message, resp):
        ack = yield self.publish_ack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'])

        returnValue(ack)

    @inlineCallbacks
    def handle_outbound_fail(self, message, resp):
        nack = yield self.publish_nack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'],
            reason=self.get_nack_reason((yield resp.content())))

        returnValue(nack)


def get_in(data, *keys):
    for key in keys:
        data = data.get(key)

        if data is None:
            return None

    return data
