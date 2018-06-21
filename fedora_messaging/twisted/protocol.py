# This file is part of fedora_messaging.
# Copyright (C) 2018 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

from __future__ import absolute_import, unicode_literals

import logging

import pika
from pika.adapters.twisted_connection import TwistedProtocolConnection
from twisted.internet import defer, error
# twisted.logger is available with Twisted 15+
from twisted.python import log

from .._session import get_message, get_serialized_message
from ..exceptions import Nack, Drop, HaltConsumer, ValidationError


class FedoraMessagingProtocol(TwistedProtocolConnection):
    """A Twisted Protocol for the Fedora Messaging system.

    This protocol builds on the generic pika AMQP protocol to add calls
    specific to the Fedora Messaging implementation.
    """

    name = 'FedoraMessaging:Protocol'

    def __init__(self, parameters, confirms=False):
        """Initialize the protocol.

        Args:
            parameters (pika.ConnectionParameters): The connection parameters.
            confirms (bool): If True, all outgoing messages will require a
                confirmation from the server, and the Deferred returned from
                the publish call will wait for that confirmation.
        """
        TwistedProtocolConnection.__init__(self, parameters)
        self._parameters = parameters
        self._confirms = confirms
        self._channel = None
        self._running = False
        self._queues = set()
        self._message_callback = None
        self.factory = None
        self.ready.addCallback(
            lambda _: self.connectionReady()
        )

    @defer.inlineCallbacks
    def connectionReady(self):
        """Called when the AMQP connection is ready.
        """
        # Create channel
        self._channel = yield self.channel()
        log.msg("AMQP channel created", system=self.name,
                logLevel=logging.DEBUG)
        yield self._channel.basic_qos(prefetch_count=0, prefetch_size=0)
        if self._confirms:
            yield self._channel.confirm_delivery()

    @defer.inlineCallbacks
    def setupRead(self, message_callback):
        """Pass incoming messages to the provided callback.

        Args:
            message_callback (callable): The callable to pass the message to
                when one arrives.
        """
        if not self.factory.bindings:
            return
        self._message_callback = message_callback
        for binding in self.factory.bindings:
            yield self._channel.exchange_declare(
                exchange=binding['exchange'],
                exchange_type='topic', durable=True)
            result = yield self._channel.queue_declare(
                queue=binding['queue_name'],
                durable=True,
                auto_delete=binding.get("queue_auto_delete", False),
                arguments=binding.get('queue_arguments'),
            )
            queue_name = result.method.queue
            yield self._channel.queue_bind(
                queue=queue_name,
                exchange=binding['exchange'],
                routing_key=binding['routing_key'],
            )
            self._queues.add(queue_name)
        log.msg("AMQP bindings declared", system=self.name,
                logLevel=logging.DEBUG)

    @defer.inlineCallbacks
    def _read(self, queue_object):
        while self._running:
            try:
                channel, delivery_frame, properties, body = \
                    yield queue_object.get()
            except (
                    error.ConnectionDone, pika.exceptions.ChannelClosedByClient
                    ):
                # This is deliberate.
                log.msg("Closing the read loop on the producer.",
                        system=self.name, logLevel=logging.DEBUG)
                break
            except pika.exceptions.ChannelClosed as e:
                log.err(e, system=self.name)
                break
            except pika.exceptions.ConsumerCancelled:
                log.msg("Consumer cancelled, quitting the read loop.",
                        system=self.name)
                break
            except Exception as e:
                log.err("Failed getting the next message in the queue, "
                        "stopping.", system=self.name)
                log.err(e, system=self.name)
                break
            if body:
                yield self._on_message(
                    channel, delivery_frame, properties, body)

    @defer.inlineCallbacks
    def _on_message(self, channel, delivery_frame, properties, body):
        """
        Callback when a message is received from the server.

        This method wraps a user-registered callback for message delivery. It
        decodes the message body, determines the message schema to validate the
        message with, and validates the message before passing it on to the
        user callback.

        This also handles acking, nacking, and rejecting messages based on
        exceptions raised by the consumer callback. For detailed documentation
        on the user-provided callback, see the user guide on consuming.

        Args:
            channel (pika.channel.Channel): The channel from which the message
                was received.
            delivery_frame (pika.spec.Deliver): The delivery frame which
                includes details about the message like content encoding and
                its delivery tag.
            properties (pika.spec.BasicProperties): The message properties like
                the message headers.
            body (bytes): The message payload.
            message_callback (callable): the function that will be called with
                the received message as only argument.

        Returns:
            Deferred: fired when the message has been handled.
        """
        log.msg('Message arrived with delivery tag {tag}'.format(
            tag=delivery_frame.delivery_tag
            ), system=self.name, logLevel=logging.DEBUG)
        try:
            message = get_message(delivery_frame.routing_key, properties, body)
        except ValidationError:
            log.msg('Message id {msgid} did not pass validation.'.format(
                msgid=properties.message_id,
            ), system=self.name, logLevel=logging.WARNING)
            yield channel.basic_nack(
                delivery_tag=delivery_frame.delivery_tag, requeue=False)
            return

        try:
            log.msg(
                'Consuming message from topic {topic!r} (id {msgid})'.format(
                    topic=message.topic, msgid=properties.message_id,
                ), system=self.name, logLevel=logging.DEBUG)
            yield defer.maybeDeferred(self._message_callback, message)
        except Nack:
            log.msg('Returning message id {msgid} to the queue'.format(
                msgid=properties.message_id,
            ), system=self.name, logLevel=logging.WARNING)
            yield channel.basic_nack(
                delivery_tag=delivery_frame.delivery_tag, requeue=True)
        except Drop:
            log.msg('Dropping message id {msgid}'.format(
                msgid=properties.message_id,
            ), system=self.name, logLevel=logging.WARNING)
            yield channel.basic_nack(
                delivery_tag=delivery_frame.delivery_tag, requeue=False)
        except HaltConsumer:
            log.msg(
                'Consumer indicated it wishes consumption to halt, '
                'shutting down', system=self.name, logLevel=logging.WARNING)
            yield self.stopProducing()
        except Exception:
            log.err("Received unexpected exception from consumer callback",
                    system=self.name)
            log.err(system=self.name)
            yield channel.basic_nack(
                delivery_tag=0, multiple=True, requeue=True)
            yield self.stopProducing()
        else:
            yield channel.basic_ack(delivery_tag=delivery_frame.delivery_tag)

    @defer.inlineCallbacks
    def publish(self, message, exchange):
        """
        Publish a :class:`fedora_messaging.message.Message` to an `exchange`_
        on the message broker.

        Args:
            message (message.Message): The message to publish.
            exchange (str): The name of the AMQP exchange to publish to

        .. _exchange: https://www.rabbitmq.com/tutorials/amqp-concepts.html#exchanges
        """
        body, routing_key, properties = get_serialized_message(message)
        yield self._channel.publish(exchange, body, routing_key, properties)

    @defer.inlineCallbacks
    def resumeProducing(self):
        """
        Starts or resumes the retrieval of messages from the server queue.

        This method starts receiving messages from the server, they will be
        passed to the consumer callback.

        Returns:
            Deferred: fired when the production is ready to start
        """
        # Start consuming
        self._running = True
        for queue_name in self._queues:
            queue_object, consumer_tag = yield self._channel.basic_consume(
                queue=queue_name)
            self._read(queue_object).addErrback(log.err, system=self.name)
        log.msg("AMQP consumer is ready",
                system=self.name, logLevel=logging.DEBUG)

    def pauseProducing(self):
        """
        Pause the reception of messages. Does not disconnect from the server.

        Message reception can be resumed with :meth:`resumeProducing`.

        Returns:
            Deferred: fired when the production is paused.
        """
        if self._channel is None:
            return
        if not self._running:
            return
        # Exit the read loop and cancel the consumer on the server.
        self._running = False
        for consumer_tag in self._channel.consumer_tags:
            yield self._channel.basic_cancel(consumer_tag)
        # Make sure all the queues are closed. On older versions of Pika, the
        # ClosableDeferredQueues were not closed when the consumer was
        # cancelled.
        # TODO: remove this when the new version of Pika has been available for
        # a while.
        for queue in self._channel._consumers.items():
            if not isinstance(queue, set()):
                break
            # Old version of pika, loop again:
            for q in queue:
                q.close(pika.exceptions.ConsumerCancelled())
        log.msg("Paused retrieval of messages for the server queue",
                system=self.name, logLevel=logging.DEBUG)

    @defer.inlineCallbacks
    def stopProducing(self):
        """
        Stop producing messages and disconnect from the server.

        Returns:
            Deferred: fired when the production is stopped.
        """
        if self._channel is None:
            return
        if self._running:
            yield self.pauseProducing()
        if not self._impl.is_closed:
            log.msg("Disconnecting from the Fedora Messaging broker",
                    system=self.name, logLevel=logging.DEBUG)
            yield self.close()
        self._channel = None