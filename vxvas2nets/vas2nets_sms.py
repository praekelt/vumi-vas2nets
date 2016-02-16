from twisted.internet.defer import inlineCallbacks

from vumi.transports.httprpc import HttpRpcTransport


class Vas2NetsSmsTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    """Config for SMS transport."""
    pass


class Vas2NetsSmsTransport(HttpRpcTransport):
    CONFIG_CLASS = Vas2NetsSmsTransportConfig

    EXPECTED_FIELDS = frozenset([
    ])

    transport_type = 'sms'

    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        pass

    @inlineCallbacks
    def handle_outbound_message(self, message):
        pass
