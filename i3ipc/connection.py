#!/usr/bin/env python3

from .con import Con
from .model import (Event, MessageType, CommandReply, VersionReply, BarConfigReply, OutputReply,
                    WorkspaceReply, ConfigReply, TickEvent, TickReply, WorkspaceEvent, GenericEvent,
                    WindowEvent, BarconfigUpdateEvent, BindingEvent)
from ._private import PubSub, PropsObject

import sys
import errno
import struct
import json
import socket
import os
import subprocess
from threading import Timer, Lock
import time
import Xlib
import Xlib.display

python2 = sys.version_info[0] < 3


class Connection(object):
    """
    This class controls a connection to the i3 ipc socket. It is capable of
    executing commands, subscribing to window manager events, and querying the
    window manager for information about the current state of windows,
    workspaces, outputs, and the i3bar. For more information, see the `ipc
    documentation <http://i3wm.org/docs/ipc.html>`_

    :param str socket_path: The path for the socket to the current i3 session.
        In most situations, you will not have to supply this yourself. Guessing
        first happens by the environment variable :envvar:`I3SOCK`, and, if this is
        empty, by executing :command:`i3 --get-socketpath`.
    :raises Exception: If the connection to ``i3`` cannot be established, or when
        the connection terminates.
    """
    MAGIC = 'i3-ipc'  # safety string for i3-ipc
    _chunk_size = 1024  # in bytes
    _timeout = 0.5  # in seconds
    _struct_header = '=%dsII' % len(MAGIC.encode('utf-8'))
    _struct_header_size = struct.calcsize(_struct_header)

    def __init__(self, socket_path=None, auto_reconnect=False):
        if not socket_path and os.environ.get("_I3IPC_TEST") is None:
            socket_path = os.environ.get("I3SOCK")

        if not socket_path:
            socket_path = os.environ.get("SWAYSOCK")

        if not socket_path:
            try:
                disp = Xlib.display.Display()
                root = disp.screen().root
                i3atom = disp.intern_atom("I3_SOCKET_PATH")
                socket_path = root.get_full_property(i3atom, Xlib.X.AnyPropertyType).value.decode()
            except Exception:
                pass

        if not socket_path:
            raise Exception('Failed to retrieve the i3 or sway IPC socket path')

        if auto_reconnect:
            self.subscriptions = Event.SHUTDOWN
        else:
            self.subscriptions = 0

        self._pubsub = PubSub(self)
        self.props = PropsObject(self)
        self.socket_path = socket_path
        self.cmd_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.cmd_socket.connect(self.socket_path)
        self.cmd_lock = Lock()
        self.sub_socket = None
        self.sub_lock = Lock()
        self.auto_reconnect = auto_reconnect
        self._restarting = False
        self._quitting = False

    def _pack(self, msg_type, payload):
        """
        Packs the given message type and payload. Turns the resulting
        message into a byte string.
        """
        pb = payload.encode('utf-8')
        s = struct.pack('=II', len(pb), msg_type.value)
        return self.MAGIC.encode('utf-8') + s + pb

    def _unpack(self, data):
        """
        Unpacks the given byte string and parses the result from JSON.
        Returns None on failure and saves data into "self.buffer".
        """
        msg_magic, msg_length, msg_type = self._unpack_header(data)
        msg_size = self._struct_header_size + msg_length
        # XXX: Message shouldn't be any longer than the data
        payload = data[self._struct_header_size:msg_size]
        return payload.decode('utf-8', 'replace')

    def _unpack_header(self, data):
        """
        Unpacks the header of given byte string.
        """
        return struct.unpack(self._struct_header, data[:self._struct_header_size])

    def _recv_robust(self, sock, size):
        """
        Receive size from sock, and retry if the recv() call was interrupted.
        (this is only required for python2 compatability)
        """
        while True:
            try:
                return sock.recv(size)
            except socket.error as e:
                if e.errno != errno.EINTR:
                    raise

    def _ipc_recv(self, sock):
        data = self._recv_robust(sock, 14)

        if len(data) == 0:
            # EOF
            return '', 0

        msg_magic, msg_length, msg_type = self._unpack_header(data)
        msg_size = self._struct_header_size + msg_length
        while len(data) < msg_size:
            data += self._recv_robust(sock, msg_length)
        return self._unpack(data), msg_type

    def _ipc_send(self, sock, message_type, payload):
        '''
        Send and receive a message from the ipc.
        NOTE: this is not thread safe
        '''
        sock.sendall(self._pack(message_type, payload))
        data, msg_type = self._ipc_recv(sock)
        return data

    def _wait_for_socket(self):
        # for the auto_reconnect feature only
        socket_path_exists = False
        for tries in range(0, 500):
            socket_path_exists = os.path.exists(self.socket_path)
            if socket_path_exists:
                break
            time.sleep(0.001)

        return socket_path_exists

    def message(self, message_type, payload):
        if python2:
            ErrorType = IOError
        else:
            ErrorType = ConnectionError

        try:
            self.cmd_lock.acquire()
            return self._ipc_send(self.cmd_socket, message_type, payload)
        except ErrorType as e:
            if not self.auto_reconnect:
                raise (e)

            # XXX: can the socket path change between restarts?
            if not self._wait_for_socket():
                raise (e)

            self.cmd_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.cmd_socket.connect(self.socket_path)
            return self._ipc_send(self.cmd_socket, message_type, payload)
        finally:
            self.cmd_lock.release()

    def command(self, payload):
        """
        Send a command to i3. See the `list of commands
        <http://i3wm.org/docs/userguide.html#_list_of_commands>`_ in the user
        guide for available commands. Pass the text of the command to execute
        as the first arguments. This is essentially the same as using
        ``i3-msg`` or an ``exec`` block in your i3 config to control the
        window manager.

        :rtype: List of :class:`CommandReply`.
        """
        data = self.message(MessageType.COMMAND, payload)
        if data:
            return json.loads(data, object_hook=CommandReply)
        else:
            return []

    def get_version(self):
        """
        Get json encoded information about the running i3 instance.  The
        equivalent of :command:`i3-msg -t get_version`. The return
        object exposes the following attributes :attr:`~VersionReply.major`,
        :attr:`~VersionReply.minor`, :attr:`~VersionReply.patch`,
        :attr:`~VersionReply.human_readable`, and
        :attr:`~VersionReply.loaded_config_file_name`.

        Example output:

        .. code:: json

            {'patch': 0,
             'human_readable': '4.12 (2016-03-06, branch "4.12")',
             'major': 4,
             'minor': 12,
             'loaded_config_file_name': '/home/joep/.config/i3/config'}


        :rtype: VersionReply

        """
        data = self.message(MessageType.GET_VERSION, '')
        return json.loads(data, object_hook=VersionReply)

    def get_bar_config(self, bar_id=None):
        """
        Get the configuration of a single bar. Defaults to the first if none is
        specified. Use :meth:`get_bar_config_list` to obtain a list of valid
        IDs.

        :rtype: BarConfigReply
        """
        if not bar_id:
            bar_config_list = self.get_bar_config_list()
            if not bar_config_list:
                return None
            bar_id = bar_config_list[0]

        data = self.message(MessageType.GET_BAR_CONFIG, bar_id)
        return json.loads(data, object_hook=BarConfigReply)

    def get_bar_config_list(self):
        """
        Get list of bar IDs as active in the connected i3 session.

        :rtype: List of strings that can be fed as ``bar_id`` into
            :meth:`get_bar_config`.
        """
        data = self.message(MessageType.GET_BAR_CONFIG, '')
        return json.loads(data)

    def get_outputs(self):
        """
        Get a list of outputs.  The equivalent of :command:`i3-msg -t get_outputs`.

        :rtype: List of :class:`OutputReply`.

        Example output:

        .. code:: python

            >>> i3ipc.Connection().get_outputs()
            [{'name': 'eDP1',
              'primary': True,
              'active': True,
              'rect': {'width': 1920, 'height': 1080, 'y': 0, 'x': 0},
              'current_workspace': '2'},
             {'name': 'xroot-0',
              'primary': False,
              'active': False,
              'rect': {'width': 1920, 'height': 1080, 'y': 0, 'x': 0},
              'current_workspace': None}]
        """
        data = self.message(MessageType.GET_OUTPUTS, '')
        return json.loads(data, object_hook=OutputReply)

    def get_workspaces(self):
        """
        Get a list of workspaces. Returns JSON-like data, not a Con instance.

        You might want to try the :meth:`Con.workspaces` instead if the info
        contained here is too little.

        :rtype: List of :class:`WorkspaceReply`.

        """
        data = self.message(MessageType.GET_WORKSPACES, '')
        return json.loads(data, object_hook=WorkspaceReply)

    def get_tree(self):
        """
        Returns a :class:`Con` instance with all kinds of methods and selectors.
        Start here with exploration. Read up on the :class:`Con` stuffs.

        :rtype: Con
        """
        data = self.message(MessageType.GET_TREE, '')
        return Con(json.loads(data), None, self)

    def get_marks(self):
        """
        Get a list of the names of all currently set marks.

        :rtype: list
        """
        data = self.message(MessageType.GET_MARKS, '')
        return json.loads(data)

    def get_binding_modes(self):
        """
        Returns all currently configured binding modes.

        :rtype: list
        """
        data = self.message(MessageType.GET_BINDING_MODES, '')
        return json.loads(data)

    def get_config(self):
        """
        Currently only contains the "config" member, which is a string
        containing the config file as loaded by i3 most recently.

        :rtype: ConfigReply
        """
        data = self.message(MessageType.GET_CONFIG, '')
        return json.loads(data, object_hook=ConfigReply)

    def send_tick(self, payload=""):
        """
        Sends a tick event with the specified payload. After the reply was
        received, the tick event has been written to all IPC connections which
        subscribe to tick events.

        :rtype: TickReply
        """
        data = self.message(MessageType.SEND_TICK, payload)
        return json.loads(data, object_hook=TickReply)

    def subscribe(self, events):
        events_obj = []
        if events & Event.WORKSPACE:
            events_obj.append("workspace")
        if events & Event.OUTPUT:
            events_obj.append("output")
        if events & Event.MODE:
            events_obj.append("mode")
        if events & Event.WINDOW:
            events_obj.append("window")
        if events & Event.BARCONFIG_UPDATE:
            events_obj.append("barconfig_update")
        if events & Event.BINDING:
            events_obj.append("binding")
        if events & Event.SHUTDOWN:
            events_obj.append("shutdown")
        if events & Event.TICK:
            events_obj.append("tick")

        try:
            self.sub_lock.acquire()
            data = self._ipc_send(self.sub_socket, MessageType.SUBSCRIBE, json.dumps(events_obj))
        finally:
            self.sub_lock.release()
        result = json.loads(data, object_hook=CommandReply)
        self.subscriptions |= events
        return result

    def off(self, handler):
        self._pubsub.unsubscribe(handler)

    def on(self, detailed_event, handler):
        event = detailed_event.replace('-', '_')

        if detailed_event.count('::') > 0:
            [event, __] = detailed_event.split('::')

        # special case: ipc-shutdown is not in the protocol
        if event == 'ipc_shutdown':
            # TODO deprecate this
            self._pubsub.subscribe(event, handler)
            return

        event_type = 0
        if event == "workspace":
            event_type = Event.WORKSPACE
        elif event == "output":
            event_type = Event.OUTPUT
        elif event == "mode":
            event_type = Event.MODE
        elif event == "window":
            event_type = Event.WINDOW
        elif event == "barconfig_update":
            event_type = Event.BARCONFIG_UPDATE
        elif event == "binding":
            event_type = Event.BINDING
        elif event == "shutdown":
            event_type = Event.SHUTDOWN
        elif event == "tick":
            event_type = Event.TICK

        if not event_type:
            raise Exception('event not implemented')

        self.subscriptions |= event_type

        self._pubsub.subscribe(detailed_event, handler)

    def event_socket_setup(self):
        self.sub_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sub_socket.connect(self.socket_path)

        self.subscribe(self.subscriptions)

    def event_socket_teardown(self):
        if self.sub_socket:
            self.sub_socket.shutdown(socket.SHUT_RDWR)
        self.sub_socket = None

    def event_socket_poll(self):
        if self.sub_socket is None:
            return True

        data, msg_type = self._ipc_recv(self.sub_socket)

        if len(data) == 0:
            # EOF
            self._pubsub.emit('ipc_shutdown', None)
            return True

        data = json.loads(data)
        msg_type = 1 << (msg_type & 0x7f)
        event_name = ''
        event = None

        if msg_type == Event.WORKSPACE:
            event_name = 'workspace'
            event = WorkspaceEvent(data, self)
        elif msg_type == Event.OUTPUT:
            event_name = 'output'
            event = GenericEvent(data)
        elif msg_type == Event.MODE:
            event_name = 'mode'
            event = GenericEvent(data)
        elif msg_type == Event.WINDOW:
            event_name = 'window'
            event = WindowEvent(data, self)
        elif msg_type == Event.BARCONFIG_UPDATE:
            event_name = 'barconfig_update'
            event = BarconfigUpdateEvent(data)
        elif msg_type == Event.BINDING:
            event_name = 'binding'
            event = BindingEvent(data)
        elif msg_type == Event.SHUTDOWN:
            event_name = 'shutdown'
            event = GenericEvent(data)
            if event.change == 'restart':
                self._restarting = True
        elif msg_type == Event.TICK:
            event_name = 'tick'
            event = TickEvent(data)
        else:
            # we have not implemented this event
            return

        self._pubsub.emit(event_name, event)

    def main(self, timeout=0):
        self._quitting = False
        while True:
            try:
                self.event_socket_setup()

                timer = None

                if timeout:
                    timer = Timer(timeout, self.main_quit)
                    timer.start()

                while not self.event_socket_poll():
                    pass

                if timer:
                    timer.cancel()
            finally:
                self.event_socket_teardown()

                if self._quitting or not self._restarting or not self.auto_reconnect:
                    return

                self._restarting = False
                # The ipc told us it's restarting and the user wants to survive
                # restarts. Wait for the socket path to reappear and reconnect
                # to it.
                if not self._wait_for_socket():
                    break

    def main_quit(self):
        self._quitting = True
        self.event_socket_teardown()
