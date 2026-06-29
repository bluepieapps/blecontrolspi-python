import zmq
import json
from libs.bleconnectrouter.zmqlogger import ZmqLogClient as Log
from libs.bleconnectrouter import bpaconfig

REQ_ACK_TIMEOUT_MS = 300
REQ_ACK_OK = b"OK"

class ContextTerminated(Exception):
    """Raised when 0MQ context was terminated."""
    def __init__(self):
        super().__init__(f"Unrecoverable Error: 0MQ Context was Terminated. All Queues were closed.")

class BPAError(Exception):
    #surface errors caught herein
    def __init__(self, code,message):
        self.code = code
        super().__init__(f"{code}: {message}")

class ChannelIdValidation:
    #returns the passed in chanel ids (topics)  as a list if they are all valid, or else raise an error
    #accepts a single string or a list of string
    #ignores repeated strings in input list
    @staticmethod
    def check(logical_channel_ids):
        """
        Args: logical_channel_ids MUST be either a string, or a  list of string only - non-blank
        raises TypeError if something else is passed in
        raises ValueError if any string is blank
        returns list of validated strings (if one string is passed in, returns list of one element)
        """
        Log.log(f"ChannelIdValidation: start with: {logical_channel_ids}")
        #topic validation
        topics = []
        if not logical_channel_ids:
            raise ValueError("logical_channel_id must be provided (cannot be blank nor empty list)")
        if isinstance(logical_channel_ids, str):
            if logical_channel_ids == "crypto": 
                            raise BPAError("Reserved Channel Id word",
                                           f"{logical_channel_ids} is a reserved word and cannot be used as a logical channel identifier")
            topics.append(logical_channel_ids)
        elif isinstance(logical_channel_ids, list):
            #we have a non empty list - check elements and build topic array
            for i, t in enumerate(logical_channel_ids):
                if not isinstance(t, str):
                    raise TypeError(f"logical_channel_id element #{i} must be a String, got {type(t).__name__}")
                elif not t:
                     raise ValueError(f"logical_channel_id element #{i} cannot be blank")
                else: #ok to use this topic
                    if t not in topics:
                        if t in {"crypto","bpawifi"}: 
                            raise BPAError("Reserved Channel Id word",
                                           f"{t} is a reserved word and cannot be used as a logical channel identifier")
                        topics.append(t)
        else: #neither list not str:
            raise TypeError(f"logical_channel_id must be either a string or list of string, got {type(logical_channel_ids).__name__} instead")
        
        Log.log(f"ChannelIdValidation: returning {topics}")
        return topics


class BPAMessageQueueClient:
    """
    This class implements the Logical data Channel(s) that can subscribe to the underlying 0MQ implementation of the btmsg service
    to receive messages from the phone/tablet over bluetooth, and to send messages to the phone/tablet.
    exposes methods:
    - init:  pass in at least one logical_channel_id (see discussion below)
    - receive: blocks until timeout - waiting for messages stamped with one of logical_channel_id (use in a while loop)
    - receive_noblock:  check underlying 0MQ to see if a message was received. (use in a polling pattern)
    - send: send string or bytes to phone over bluetooth.  message is stamped with logical_channel_id internally - see discussion below.
    - registerLogicalChannels: add one or a list of logical_data_channel_id to be listened for / send with which this class will manage
    - removeLogicalChannels: remove the logical_channel id - fom the class.  Messages can no longer be sent with this id, and any arriving messages bearing this id is ignored.
    - close: unsubscribe from )MQ and release ressources
    
    Notes on logical_channel_id:
        this message queue is designed to pass messages to the BLE Peripheral library (systemd service),
        which are then sent to a bluetooth ("central") device such as a phone or tablet.
        the specified logical_channel_id is used to stamp the message being sent via Bluetooth (using the btmsg service).

        Similarly, the central bluetooth phone/tablet stamps the message (with this same logical_channel_id,) which is received (over bluetooth) by the btmsg service, 
        and is published over 0MQ - using the logical_channel_id - which this channel is listening to.

        Note that the central bluetooth messaging library (Kotlin/Swift) must be set up with the same logical_channel_id created here. 
            (there is no mechanism provided to create a logical_channel_id here - and have the iOS/Android library create the corresponding channel on the phone/table)
        It is the responsibility of the developper to ensure that the central device bluetooth channel id uses the same logical_channel_id being created here.

        this class allows the use of an array of logical_channel_id (String) for cases where the python process being created manages more than one logical_channel_id:
        if the processing class uses only one logical_channel_id - meassages can be sent using the send method without passing the logical_channel_id 
            (as the methods defaults to using the unique logica_chanel_id that is created)
        If the processing class defines a list of more than one logical_channel_id, the send method must pass the logical_channel_id that must be use to stamp the message.

        Note that  a single logical_channel_id, or a list of logical_channel_id can be added/removed after class is initialized,
        however at least one topic must be passed in when initializing the class

    Internal information on 0MQ - sockets used:
      - sub_socket: SUB, connects to BLE's PUB (recv messages from BLE)
      - req_socket: REQ, connects to BLE's REP (send messages to BLE and wait for immediate ACK)
      - Controlclient._sock: DEALER, control (registration) socket - used to register topics with Publisher

    Subscription to general broadcast:  
        The ble Router services publishes general messages intended for all channels regardless of their id.
        Currently the only message sent is "TimedOut" 
        which indicates that the ble router service is shutting down due to inactivity.
        * Inactivity time out is set via --timeout x (x = minutes) and default to 30 minutes
            on the execStart line of the bpa-bleconnectrouter.service file (located in /etc/systemd/system)
        on reception - this class sets a flag : ble_router_timed_out which can be inspected by the Channel Class
        and can exit, or ask to start the service via systemctl start (after a suitable delay).
    """
    def __init__(self, logical_channel_ids, recv_hwm=1000, send_hwm=1000):
        """
        Args: logical_channel_ids must be single string, or list of string:  no string can be blank or eroor is raised
        """
        self.req_socket = None
        self.topics = ChannelIdValidation.check(logical_channel_ids)
        self.recv_hwm = recv_hwm
        self.send_hwm = send_hwm
        self._pending_inbound_trace_ids = {}
        self._outbound_trace_counter = 0
        self.ctx = zmq.Context.instance()
        self.setupDataSockets(self.ctx)
        self.ble_router_timed_out = False

    def setupDataSockets(self,ctx):
        pub_path=bpaconfig.BPA_PUB_SOCKET_PATH
        resp_path=bpaconfig.BPA_RESP_SOCKET_PATH
        self.pub_endpoint = f"ipc://{pub_path}"
        self.resp_endpoint = f"ipc://{resp_path}"

        # SUB: receive from BLE publisher
        self.sub_socket = self.ctx.socket(zmq.SUB)
        self.sub_socket.setsockopt(zmq.RCVHWM, self.recv_hwm)
        self.sub_socket.setsockopt(zmq.LINGER, 0)
        self.sub_socket.connect(self.pub_endpoint)

        #subscribe to general ble router system message topics
        t_system = bpaconfig.ZMQ_SYSTEM_TOPIC.encode("utf-8", errors="replace")
        self.sub_socket.setsockopt(zmq.SUBSCRIBE, t_system)

        # Subscribe to topics passed in the init call
        for t in self.topics:
            t = t.encode("utf-8", errors="replace")
            self.sub_socket.setsockopt(zmq.SUBSCRIBE, t)

        self._build_req_socket()

        # Optional poller for timed receive
        self._poller = zmq.Poller()
        self._poller.register(self.sub_socket, zmq.POLLIN)

    def _build_req_socket(self):
        if self.req_socket is not None:
            try:
                self.req_socket.close(0)
            except Exception:
                pass
            self.req_socket = None

        self.req_socket = self.ctx.socket(zmq.REQ)
        self.req_socket.setsockopt(zmq.SNDHWM, self.send_hwm)
        self.req_socket.setsockopt(zmq.LINGER, 0)
        self.req_socket.setsockopt(zmq.IMMEDIATE, 1)
        self.req_socket.setsockopt(zmq.SNDTIMEO, REQ_ACK_TIMEOUT_MS)
        self.req_socket.setsockopt(zmq.RCVTIMEO, REQ_ACK_TIMEOUT_MS)
        self.req_socket.connect(self.resp_endpoint)

    def registerAdditionalLogicalChannels(self, logical_channel_ids):  
        """
        helper function to register extra topics (channel id) after initialization is complete.
        Args: logical_channel_ids - str or list of str
        register additional(s) Channel ID(s)
        pass in as single string, or list of String - if any string is blank: will raise error
        Channels already registered will be ignored.
        Returns True if topic was registered, False if topic was ignored
        """
        #ignore chanel id if already in the list of topics
        valid_new_topics = ChannelIdValidation.check(logical_channel_ids)
        if self.req_socket is not None:
            #service is already started and sockets established:
            for topic in valid_new_topics:
                if topic not in self.topics and self.sub_socket:
                    self.topics.append(topic)
                    #first subscribe topic to data channel
                    t_bytes = topic.encode("utf-8", errors="replace")
                    self.sub_socket.setsockopt(zmq.SUBSCRIBE, t_bytes) #subscribe this topic to sub socket
                    
            Log.log("BPAMessageQueueClient-registerLogicalChannels: {logical_channel_ids}")
            return True
        else: 
            return False

    def removeLogicalChannels(self,logical_channel_ids):
        """
        remove/de-register Channel ID(s)
        pass in as single string, or list of String
        ID not already registered will be ignored.
        """
        topics_to_remove = []
        if isinstance(logical_channel_ids, str):
            topics_to_remove.append(logical_channel_ids)
        elif isinstance(logical_channel_ids, list):
            topics_to_remove = logical_channel_ids
        else:
            return
        for topic in topics_to_remove:
            if topic in self.topics and self.sub_socket:
                self.topics.remove(topic)
                t_bytes = topic.encode("utf-8", errors="replace")
                self.sub_socket.setsockopt(zmq.UNSUBSCRIBE, t_bytes) #unsubscribe from SUB socket


    

    # ----- sending back to BLE (your main class consumes on its REP socket) -----
    def send(self, payload, channel_id = None): 
        """
        Args: 
            channel_id of type: str:
                - must match a registered logical channel id or exception is raised
                - can be None: if only one channel is registered, that channel id is always used by default
            payload can be:
                - str or bytes
                - Object that can be JSON stringified (like dict or list)
                - if object cannot be stringified as json - error is raised
       
        note: string and stringified objects are converted to bytes with utf8 for sending.
        Sends one multipart REQ transaction and waits for a b"OK" reply from the BLE service REP socket.
        If the reply is not received within the bounded timeout, the message is treated as dropped.
        Raises errors:
            - Send Timeout: request could not be handed to the service socket within the timeout
            - ACK Timeout: service did not acknowledge receipt within the timeout
            - Invalid ACK: service replied with an unexpected payload
            - Context Terminated: unrecoverable error: class needs to be re-instantiated
            - ZMQ Error: transport/state error during the REQ/REP transaction
            - JSON Conversion Failed: if an object fails Json stringification ( json.dumps() )
        """
        if channel_id is None:
            if self.topics: channel_id = self.topics[0]

        if channel_id not in self.topics:
            raise BPAError("Unknown Channel ID",f"{channel_id} is not registered")
        
        t_bytes = channel_id.encode("utf-8", errors="replace")
        if isinstance(payload, str):
            payload = payload.encode("utf-8", errors="replace")
        elif isinstance(payload, bytes):
            pass
        else: #try to json stringify the object:
            try:
                json_str = json.dumps(payload, ensure_ascii=False)
            except Exception as e:
                raise BPAError("JSON Conversion Failed",f"{e}")
            payload = json_str.encode("utf-8", errors="replace")

        inbound_trace_id = self._pending_inbound_trace_ids.pop(channel_id, b"\x00")
        if not inbound_trace_id:
            inbound_trace_id = b"\x00"
        elif len(inbound_trace_id) > 1:
            inbound_trace_id = inbound_trace_id[:1]
        self._outbound_trace_counter = 1 if self._outbound_trace_counter >= 255 else self._outbound_trace_counter + 1
        trace_id = inbound_trace_id + bytes((self._outbound_trace_counter,))
        try:
            try:
                self.req_socket.send_multipart([t_bytes, trace_id, payload])
            except zmq.Again:
                self._build_req_socket()
                Log.log("Send Timeout- message was not sent - rebuilding socket",level = Log.CRITICAL)
                return False
            try:
                ack = self.req_socket.recv()
            except zmq.Again:
                self._build_req_socket()
                Log.log("ACK Timeoutmessage was not acknowledged - rebuilding socket",level = Log.CRITICAL)
                return False

            if ack != REQ_ACK_OK:
                self._build_req_socket()
                Log.log(f"Invalid ACK - unexpected response: {ack!r}",level = Log.CRITICAL)
                return False
            
        except zmq.ContextTerminated:
            self.close()
            raise ContextTerminated()
        except zmq.ZMQError as e:
            if e.errno == zmq.ENOTSOCK:
                self.close()
                raise ContextTerminated()
            raise BPAError("ZMQ Error",f"{e}")
        
        return True

    # ----- receiving from BLE (published topic+payload) -----

    def receive(self, timeout_ms):
        """
        blocks until timeout (milliseconds)
        If timeout_ms is None, blocks forever until message arrives; 
        If timeout = 0 same as receive_noblock() (returns immediately after checking for message).
            - returns tupple (topic:str, message:bytes) if a message arrives during the timeout period
            - returns (None,None) if no message found within timeout period
        raise zmq.ContextTerminated error if context was terminated (by calling close() )
        """
        try:
            events = dict(self._poller.poll(timeout=timeout_ms))
        except zmq.ContextTerminated:
            self.close()
            raise ContextTerminated()

        if self.sub_socket in events and (events[self.sub_socket] & zmq.POLLIN):
            return self.receive_noblock()
        
        return (None,None)
    
    def receive_noblock(self):
        """
        non blocking receive - return (topic:str, message:bytes) if a message exists in the queue
        returns (None,None) if no message exists at this time
        raise zmq.ContextTerminated error if context was terminated (by calling close() )
        """
        try:
            parts = self.sub_socket.recv_multipart(flags=zmq.DONTWAIT)
            if len(parts) >= 3:
                topic_bytes, trace_id, payload = parts[0], parts[1], parts[2]
            else:
                topic_bytes, payload = parts[0], parts[1]
                trace_id = b"\x00\x00"
            topic = topic_bytes.decode("utf-8", errors="replace")
            if trace_id:
                self._pending_inbound_trace_ids[topic] = trace_id[:1]
            if topic == bpaconfig.ZMQ_SYSTEM_TOPIC:
                self.ble_router_timed_out = True
                return (None,None)
            else:
                return (topic,payload) #topic is a string, payload is bytes
        except zmq.Again:
            return (None,None)
        except zmq.ContextTerminated:
            self.close()
            raise ContextTerminated()

    def close(self):
        if self.topics: self.removeLogicalChannels(self.topics)
        try:
            if self.sub_socket is not None:
                self.sub_socket.close(0) #this unsubscribes everyting as well
        finally:
            self.sub_socket = None
        try:
            if self.req_socket is not None:
                self.req_socket.close(0)
        finally:
            self.req_socket = None
        try:
            self.ctx.term()
        finally:
            self.ctx = None
