import json
from twisted.web import http

from twisted.internet.defer import inlineCallbacks

from vumi import log
from vumi.transports.httprpc import HttpRpcTransport


class Vas2NetsSmsTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    """Config for SMS transport."""
    pass


class Vas2NetsSmsTransport(HttpRpcTransport):
    CONFIG_CLASS = Vas2NetsSmsTransportConfig

    EXPECTED_FIELDS = frozenset([
        'sender', 'receiver', 'msgdata', 'recvtime', 'msgid', 'operator'
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

    def respond(self, message_id, code, body=None):
        if body is None:
            body = {}

        self.finish_request(message_id, json.dumps(body), code=code)

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

        self.respond(message_id, http.BAD_REQUEST, {})

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
    def handle_outbound_message(self, message):
        pass
