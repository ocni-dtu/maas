# Copyright 2015-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""The MAAS WebSockets protocol."""

__all__ = ["WebSocketProtocol"]

from collections import deque
from functools import partial
from http.cookies import SimpleCookie
import json
from typing import Optional
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, load_backend, SESSION_KEY
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from maasserver.eventloop import services
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
from maasserver.websockets import handlers
from maasserver.websockets.websockets import STATUSES
from provisioningserver.logger import LegacyLogger
from provisioningserver.utils import typed
from provisioningserver.utils.twisted import deferred, synchronous
from provisioningserver.utils.url import splithost
from twisted.internet import defer
from twisted.internet.defer import fail, inlineCallbacks
from twisted.internet.protocol import Factory, Protocol
from twisted.python.modules import getModule
from twisted.web.server import NOT_DONE_YET


log = LegacyLogger()


class MSG_TYPE:
    #: Request made from client.
    REQUEST = 0

    #: Response from server.
    RESPONSE = 1

    #: Notify message from server.
    NOTIFY = 2

    #: Connectivity checks
    PING = 3
    PING_REPLY = 4


class RESPONSE_TYPE:
    #:
    SUCCESS = 0

    #:
    ERROR = 1


@typed
def get_cookie(cookies: Optional[str], cookie_name: str) -> Optional[str]:
    """Return the sessionid value from `cookies`."""
    if cookies is None:
        return None
    cookies = SimpleCookie(cookies)
    if cookie_name in cookies:
        return cookies[cookie_name].value
    else:
        return None


class WebSocketProtocol(Protocol):
    """The web-socket protocol that supports the web UI.

    :ivar factory: Set by the factory that spawned this protocol.
    """

    def __init__(self):
        self.messages = deque()
        self.user = None
        self.request = None
        self.cache = {}
        self.sequence_number = 0

    def connectionMade(self):
        """Connection has been made to client."""
        # Using the provided cookies on the connection request, authenticate
        # the client. If this fails or if the CSRF token can't be found, it
        # will call loseConnection. A websocket connection is only allowed
        # from an authenticated user.

        cookies = self.transport.cookies.decode("ascii")
        d = self.authenticate(
            get_cookie(cookies, "sessionid"), get_cookie(cookies, "csrftoken")
        )

        # Only add the client to the list of known clients if/when the
        # authentication succeeds.
        def authenticated(user):
            if user is None:
                # This user could not be authenticated. No further interaction
                # should take place. The connection is already being dropped.
                pass
            else:
                # This user is a keeper. Record it and process any message
                # that have already been received.
                self.user = user

                # Create the request for the handlers for this connection.
                self.request = HttpRequest()
                self.request.user = self.user
                self.request.META[
                    "HTTP_USER_AGENT"
                ] = self.transport.user_agent
                self.request.META["REMOTE_ADDR"] = self.transport.ip_address

                # XXX newell 2018-10-17 bug=1798479:
                # Check that 'SERVER_NAME' and 'SERVER_PORT' are set.
                # 'SERVER_NAME' and 'SERVER_PORT' are required so
                # `build_absolure_uri` can create an actual absolute URI so
                # that the curtin configuration is valid.  See the bug and
                # maasserver.node_actions for more details.
                #
                # `splithost` will split the host and port from either an
                # ipv4 or an ipv6 address.
                host, port = splithost(str(self.transport.host))
                if host:
                    self.request.META["SERVER_NAME"] = host
                else:
                    self.request.META["SERVER_NAME"] = "localhost"
                if port:
                    self.request.META["SERVER_PORT"] = port
                else:
                    self.request.META["SERVER_PORT"] = 5248

                # Be sure to process messages after the metadata is populated,
                # in order to avoid bug #1802390.
                self.processMessages()
                self.factory.clients.append(self)

        d.addCallback(authenticated)

    def connectionLost(self, reason):
        """Connection to the client has been lost."""
        # If the connection is lost before the authentication happens, the
        # 'client' will not have been added to the list.
        if self in self.factory.clients:
            self.factory.clients.remove(self)

    def loseConnection(self, status, reason):
        """Close connection with status and reason."""
        msgFormat = "Closing connection: {status!r} ({reason!r})"
        log.debug(msgFormat, status=status, reason=reason)
        self.transport._receiver._transport.loseConnection(
            status, reason.encode("utf-8")
        )

    def getMessageField(self, message, field):
        """Get `field` value from `message`.

        Closes connection with `PROTOCOL_ERROR` if `field` doesn't exist
        in `message`.
        """
        if field not in message:
            self.loseConnection(
                STATUSES.PROTOCOL_ERROR,
                "Missing %s field in the received message." % field,
            )
            return None
        return message[field]

    @synchronous
    @transactional
    def getUserFromSessionId(self, session_id):
        """Return the user from `session_id`."""
        session_engine = self.factory.getSessionEngine()
        session_wrapper = session_engine.SessionStore(session_id)
        user_id = session_wrapper.get(SESSION_KEY)
        backend = session_wrapper.get(BACKEND_SESSION_KEY)
        if backend is None:
            return None
        auth_backend = load_backend(backend)
        if user_id is not None and auth_backend is not None:
            user = auth_backend.get_user(user_id)
            # Get the user again prefetching the SSHKey for the user. This is
            # done so a query is not made for each action that is possible on
            # a node in the node listing.
            return (
                User.objects.filter(id=user.id)
                .prefetch_related("sshkey_set")
                .first()
            )
        else:
            return None

    @deferred
    def authenticate(self, session_id, csrftoken):
        """Authenticate the connection.

        - Check that the CSRF token is valid.
        - Authenticate the user using the session id.

        This returns the authenticated user or ``None``. The latter means that
        the connection is being dropped, and that processing should cease.
        """
        # Check the CSRF token.
        tokens = parse_qs(urlparse(self.transport.uri).query).get(b"csrftoken")
        # Convert tokens from bytes to str as the transport sends it
        # as ascii bytes and the cookie is decoded as unicode.
        if tokens is not None:
            tokens = [token.decode("ascii") for token in tokens]
        if tokens is None or csrftoken not in tokens:
            # No csrftoken in the request or the token does not match.
            self.loseConnection(STATUSES.PROTOCOL_ERROR, "Invalid CSRF token.")
            return None

        # Authenticate user.
        def got_user(user):
            if user is None:
                self.loseConnection(
                    STATUSES.PROTOCOL_ERROR, "Failed to authenticate user."
                )
                return None
            else:
                return user

        def got_user_error(failure):
            self.loseConnection(
                STATUSES.PROTOCOL_ERROR,
                "Error authenticating user: %s" % failure.getErrorMessage(),
            )
            return None

        d = deferToDatabase(self.getUserFromSessionId, session_id)
        d.addCallbacks(got_user, got_user_error)

        return d

    def dataReceived(self, data):
        """Received message from client and queue up the message."""
        try:
            message = json.loads(data.decode("utf-8"))
        except ValueError:
            # Only accept JSON data over the protocol. Close the connect
            # with invalid data.
            self.loseConnection(
                STATUSES.PROTOCOL_ERROR, "Invalid data expecting JSON object."
            )
            return ""
        self.messages.append(message)
        self.processMessages()
        return NOT_DONE_YET

    def processMessages(self):
        """Process all the queued messages."""
        if self.user is None:
            # User is not authenticated yet, don't process messages. Once the
            # user is authenticated this method will be called to process the
            # queued messages.
            return []

        # Process all the messages in the queue.
        handledMessages = []
        while len(self.messages) > 0:
            message = self.messages.popleft()
            handledMessages.append(message)
            msg_type = self.getMessageField(message, "type")
            if msg_type is None:
                return handledMessages
            if msg_type not in (MSG_TYPE.REQUEST, MSG_TYPE.PING):
                # Only support request messages from the client.
                self.loseConnection(
                    STATUSES.PROTOCOL_ERROR, "Invalid message type."
                )
                return handledMessages
            if self.handleRequest(message, msg_type) is None:
                # Handling of request has failed, stop processing the messages
                # in the queue because the connection will be lost.
                return handledMessages
        return handledMessages

    def handleRequest(self, message, msg_type=MSG_TYPE.REQUEST):
        """Handle the request message."""
        # Get the required request_id.
        request_id = self.getMessageField(message, "request_id")
        if request_id is None:
            return None

        if msg_type == MSG_TYPE.PING:
            self.sequence_number += 1
            return defer.succeed(
                self.sendResult(
                    request_id=request_id,
                    result=self.sequence_number,
                    msg_type=MSG_TYPE.PING_REPLY,
                )
            )

        # Decode the method to be called.
        msg_method = self.getMessageField(message, "method")
        if msg_method is None:
            return None
        try:
            handler_name, method = msg_method.split(".", 1)
        except ValueError:
            # Invalid method. Method format is "handler.method".
            self.loseConnection(
                STATUSES.PROTOCOL_ERROR, "Invalid method formatting."
            )
            return None

        # Create the handler for the call.
        handler_class = self.factory.getHandler(handler_name)
        if handler_class is None:
            self.loseConnection(
                STATUSES.PROTOCOL_ERROR,
                "Handler %s does not exist." % handler_name,
            )
            return None

        handler = self.buildHandler(handler_class)
        d = handler.execute(method, message.get("params", {}))
        d.addCallbacks(
            partial(self.sendResult, request_id),
            partial(self.sendError, request_id, handler, method),
        )
        return d

    def _json_encode(self, obj):
        """Allow byte strings embedded in the 'result' object passed to
        `sendResult` to be seamlessly decoded.
        """
        if isinstance(obj, bytes):
            return obj.decode(encoding="utf-8", errors="ignore")
        else:
            raise TypeError("Could not convert object to JSON: %r" % obj)

    def sendResult(self, request_id, result, msg_type=MSG_TYPE.RESPONSE):
        """Send final result to client."""
        result_msg = {
            "type": msg_type,
            "request_id": request_id,
            "rtype": RESPONSE_TYPE.SUCCESS,
            "result": result,
        }
        self.transport.write(
            json.dumps(result_msg, default=self._json_encode).encode("ascii")
        )
        return result

    def sendError(self, request_id, handler, method, failure):
        """Log and send error to client."""
        if isinstance(failure.value, ValidationError):
            try:
                # When the error is a validation issue, send the error as a
                # JSON object. The client will use this to JSON to render the
                # error messages for the correct fields.
                error = json.dumps(failure.value.message_dict)
            except AttributeError:
                error = failure.value.message
        else:
            error = failure.getErrorMessage()
        why = "Error on request (%s) %s.%s: %s" % (
            request_id,
            handler._meta.handler_name,
            method,
            error,
        )
        log.err(failure, why)

        error_msg = {
            "type": MSG_TYPE.RESPONSE,
            "request_id": request_id,
            "rtype": RESPONSE_TYPE.ERROR,
            "error": error,
        }
        self.transport.write(
            json.dumps(error_msg, default=self._json_encode).encode("ascii")
        )
        return None

    def sendNotify(self, name, action, data):
        """Send the notify message with data."""
        notify_msg = {
            "type": MSG_TYPE.NOTIFY,
            "name": name,
            "action": action,
            "data": data,
        }
        self.transport.write(
            json.dumps(notify_msg, default=self._json_encode).encode("ascii")
        )

    def buildHandler(self, handler_class):
        """Return an initialised instance of `handler_class`."""
        handler_name = handler_class._meta.handler_name
        handler_cache = self.cache.setdefault(handler_name, {})
        return handler_class(self.user, handler_cache, self.request)


class WebSocketFactory(Factory):
    """Factory for WebSocketProtocol."""

    protocol = WebSocketProtocol

    def __init__(self, listener):
        self.handlers = {}
        self.clients = []
        self.listener = listener
        self.cacheHandlers()
        self.registerNotifiers()

    def startFactory(self):
        """Register for RPC events."""
        self.registerRPCEvents()

    def stopFactory(self):
        """Unregister RPC events."""
        self.unregisterRPCEvents()

    def getSessionEngine(self):
        """Returns the session engine being used by Django.

        Used by the protocol to validate the sessionid.
        """
        return getModule(settings.SESSION_ENGINE).load()

    def cacheHandlers(self):
        """Cache all the websocket handlers."""
        for name in dir(handlers):
            # Ignore internals
            if name.startswith("_"):
                continue
            # Only care about class that have _meta attribute, as that
            # means its a handler.
            cls = getattr(handlers, name)
            if not hasattr(cls, "_meta"):
                continue
            meta = cls._meta
            # Skip over abstract handlers as they only provide helpers for
            # children classes and should not be exposed over the channel.
            if meta.abstract:
                continue
            if (
                meta.handler_name is not None
                and meta.handler_name not in self.handlers
            ):
                self.handlers[meta.handler_name] = cls

    def getHandler(self, name):
        """Return handler by name from the handler cache."""
        return self.handlers.get(name)

    def registerNotifiers(self):
        """Registers all of the postgres channels in the handlers."""
        for handler in self.handlers.values():
            for channel in handler._meta.listen_channels:
                self.listener.register(
                    channel, partial(self.onNotify, handler, channel)
                )

    @inlineCallbacks
    def onNotify(self, handler_class, channel, action, obj_id):
        for client in self.clients:
            handler = client.buildHandler(handler_class)
            data = yield deferToDatabase(
                self.processNotify, handler, channel, action, obj_id
            )
            if data is not None:
                (name, client_action, data) = data
                client.sendNotify(name, client_action, data)

    @transactional
    def processNotify(self, handler, channel, action, obj_id):
        return handler.on_listen(channel, action, obj_id)

    def registerRPCEvents(self):
        """Register for connected and disconnected events from the RPC
        service."""
        rpc_service = services.getServiceNamed("rpc")
        rpc_service.events.connected.registerHandler(self.updateRackController)
        rpc_service.events.disconnected.registerHandler(
            self.updateRackController
        )

    def unregisterRPCEvents(self):
        """Unregister from connected and disconnected events from the RPC
        service."""
        rpc_service = services.getServiceNamed("rpc")
        rpc_service.events.connected.unregisterHandler(
            self.updateRackController
        )
        rpc_service.events.disconnected.unregisterHandler(
            self.updateRackController
        )

    def updateRackController(self, ident):
        """Called when a rack controller connects or disconnects from this
        region over the RPC connection.

        This is hard-coded to call the `ControllerHandler` as at the moment
        it is the only handler that needs this event.
        """
        d = self.sendOnNotifyToController(ident)
        d.addErrback(
            log.err,
            "Failed to send 'update' notification for rack controller(%s) "
            "when RPC event fired." % ident,
        )
        return d

    def sendOnNotifyToController(self, system_id):
        """Send onNotify to the `ControllerHandler` for `system_id`."""
        rack_handler = self.getHandler("controller")
        if rack_handler is None:
            return fail("Unable to get the 'controller' handler.")
        else:
            return self.onNotify(
                rack_handler, "controller", "update", system_id
            )
