import os
import signal
from dataclasses import dataclass
from typing import Optional, Tuple, Callable
from multiprocessing import Array, Lock, Value
import zmq

from libs.bleconnectrouter.service_zmq_manager import  ServiceProcessBridge
from libs.bleconnectrouter.zmqlogger import ZmqLogClient as Log
from libs.bleconnectrouter.btcrypto import BTCryptoManager,PiInfo

SEPARATOR_HEX = b'\x1e'
SEPARATOR = SEPARATOR_HEX.decode()  # string representation can be concatenated or use in split function

CHANNEL_SEP_BYTES = bytes([0x1C, 0x7C, 0x1F, 0x7C, 0x1C])  # FS|US|FS - FS = File Separator code, US = Unit Separator code from ASCII
REQ_ACK_OK = b"OK"

'''
Note on how pipeline worker dispatches and receives messages from Logical Channel handler (running as separate processes)

The pipeline worker receives bytes from bluetooth writes and sends then to decryption. 
After decryption, it extracts the logical channel id from
the inbound message and publishes the payload on the ZMQ PUB socket
(self.process_bridge.pub_socket, used through self.process_bridge.publish(...)) with that
channel id as the topic. A channel handler process subscribes to its channel id, reads
the payload, processes it, and can send any reply back on the response ZMQ REQ socket
(BPAMessageQueueClient.send(...); the handler receives with BPAMessageQueueClient.receive(...)
or BPAMessageQueueClient.receive_noblock()). The pipeline worker reads that reply from its
response REP socket (self.process_bridge.response_pull, read in self._drain_logic_responses()),
sends an immediate b"OK" ACK, and only then converts it
into one or more BLE notification payloads, and sends those back to the BLE
service process.
'''

class Notifications:
    """
    Builds BLE notification payloads from a logical channel id (`target`) and
    a message (`msg`).
    Inputs:
        - `target`: logical channel identifier as `str` or `bytes`
        - `msg`: message payload as `str` or `bytes`
    Output:
        - returns a list of bytes payloads containing one or more notification payloads
        - returns `None` if either input cannot be converted to string/bytes
    Notification format:
        - Single-part message:
            <SEP><target>:<msg>
          where:
            - <SEP> is `SEPARATOR_HEX` (`b'\\x1e'`)
            - `target` is the logical channel id
            - `msg` is the payload bytes
        - Multipart message:
            <SEP>multi<target>:<messageCounter>|<chunk_number>|<total_chunks>|<chunk>
          where:
            - <SEP> is the character form of `SEPARATOR_HEX`
            - `messageCounter` identifies the logical multipart message
            - `chunk_number` starts at 1
            - `total_chunks` is the total number of chunks in the message
            - `chunk` is one UTF-8-safe slice of the original message
    Chunking:
        - Messages whose UTF-8 byte length is 130 bytes or less are sent as a
          single notification payload.
        - Longer messages are split into chunks of about 130 bytes.
        - Chunking avoids splitting in the middle of a multibyte UTF-8 character
          by truncating bytes and decoding with `errors='ignore'`.
    Counter behavior:
        - `messageCounter` starts at 1
        - it is incremented only for multipart messages
        - `reset()` sets `messageCounter` back to 1
    This class only constructs notification payloads. It does not perform
    encryption, transport, queuing, or BLE transmission.
    """


    def __init__(self):
        self.messageCounter = 1

    def reset(self):
        self.messageCounter = 1


    def get_target_and_message_as_strings(self, msg, target):
        msg_str = None
        if isinstance(msg, str):
            msg_str = msg
        elif isinstance(msg, bytes):
            msg_str = msg.decode("utf-8", errors="replace")
        target_str = None
        if isinstance(target, str):
            target_str = target
        elif isinstance(target, bytes):
            target_str = target.decode("utf-8", errors="replace")
        return (msg_str,target_str)

    def str_bytes_helper(self,x):
        """ returns tupple (str_version, bytes_version) 
        retruns None,None if x is neither string nor bytes"""
        if isinstance(x, str):
            return (x,x.encode("utf-8", errors="replace"))
        elif isinstance(x, bytes):
            return (x.decode("utf-8", errors="replace"),x)
        else:
            return (None,None)

    def makeNotifications(self,msg,target):
        """
        see class docstring for notification messge construction details
        Agrs:
            - target: logical channel id  (in bytes or string)
            - msg: message (in bytes or string)

        this will chunk messages if longer than 130 bytes, and send multiple notifications as a multipart message
        notifications are sent to encryption (crypto worker)
        """
        target_str, target_bytes = self.str_bytes_helper(target)
        if not target_str: return
        msg_str, msg_bytes = self.str_bytes_helper(msg)
        if msg_str is None: return
        Log.log(f"makeNotifications has received {target_str}:{msg_str}")

        if len(msg_bytes) <= 130 :
            msg_to_send_bytes = SEPARATOR_HEX + target_bytes + b':' + msg_bytes
            return [msg_to_send_bytes]
        
        # manage process fro messages longer than 130 bytes
        chunked_notifications = [] #bytes
        chunked_json_list = self.make_chunks(msg_str,[])
        if len(chunked_json_list) == 1: #should not happen
            msg_to_send_bytes = SEPARATOR_HEX + target_bytes + b':' + msg_bytes
            return [msg_to_send_bytes]
        else:
            self.messageCounter += 1
            total = len(chunked_json_list)
            Log.log(f"creating multi part message - target: {target_str}; number of parts: {total}")
            for i in range(total):
                prefix = f"multi{target_str}:{self.messageCounter}|{i+1}|{total}|"
                chunk_to_send = SEPARATOR + prefix + chunked_json_list[i]
                Log.log(f"Sending: {chunk_to_send}")
                chunked_notifications.append(chunk_to_send.encode("utf-8", errors="replace"))
            return chunked_notifications

    def make_chunks(self,msg,to_send):
        '''
        This function splits a string into chunks of about 130 bytes, without breaking in the middle of a multibyte UTF-8 characters.
            It uses recursion to handle the full message.
            It avoids:
                encoding errors (errors='replace' and 'ignore')
                character truncation (cuts only at full character boundaries).
        to_send: is a list of chunks (strings) - that is updated in place here.
        '''
        # returns a list of chunks , each a string
        bmsg = msg.encode(encoding = 'UTF-8', errors = 'replace') #inserts question mark if character cannot be encoded
        #truncate at 130 bytes
        btruncated = bmsg[0:130]
        #reconvert to string - ignoring the last bytes if not encodable because truncation might have cut the unicode not on a boundary
        chunk_str = btruncated.decode('utf-8',errors='ignore')
        #get the remainder of the msg (after safe reconversion to string of the chunk
        remainder = msg[len(chunk_str):]
        #add the chunked string to the list
        to_send.append(chunk_str)

        if remainder: 
            #if there is a remaninder - re-apply chunking on it, passing in the list of chunks (to_send) so far
            #thus to_send list of chucnks grows after each recursive call.
            return(self.make_chunks(remainder,to_send))
        else:
            return list(to_send)

class SharedBytes:
    """
    share a value in bytes
    """
    def __init__(self, initial = b'', max_size: int = 128):
        self._lock = Lock()
        self._length = Value('I', 0)
        self._buffer = Array('B', max_size, lock=False)
        self.set(initial)

    def get(self):
        with self._lock:
            Log.log("getting sharedBytes (piInfo)")
            size = self._length.value
            return bytes(self._buffer[:size])

    def set(self, value_bytes):
        with self._lock:
            Log.log(f"setting piInfo on shared bytes {value_bytes}")
            use_bytes = bytes(value_bytes)
            max_size = len(self._buffer)
            if len(use_bytes) > max_size:
                use_bytes = use_bytes[:max_size]
            self._buffer[:len(use_bytes)] = use_bytes
            self._length.value = len(use_bytes)

@dataclass(frozen=True)
class ConfigSocket:
    '''
    Holds the runtime configuration needed by the pipeline worker process,
    including the ZMQ context, stop signal object, and parent/child link address.
    '''
    pair_addr: str
    poll_timeout_ms: int = 500

@dataclass(frozen=True)
class ConfigTests:
    # pass any of these for testing (None = use defaults)
    notif: Optional[Notifications] = None
    crypto: Optional[BTCryptoManager] = None
    bridge: Optional[ServiceProcessBridge] = None
    poller: Optional[zmq.Poller] = None

class PipelineWorker:
    """
    This class is responsible for dispatching messages to the correct logical channel id (topic).
    It receives BLE payloads from the parent process over the parent/child ZMQ link,
    decrypts them, extracts the logical channel id from messages that use the
    <topic>CHANNEL_SEP<payload> structure, and publishes the payload to the
    appropriate handler on the data-plane ZMQ sockets.
    """

    def __init__(
        self,
        kick_glib: Optional[Callable[[], None]],
        sharedBytes: SharedBytes,
        configSocket:ConfigSocket,
        configTests: Optional[ConfigTests] = None
    ):
        Log.initialize(log_level="DEBUG",process_label="Pipeline")
        self.kick_glib = kick_glib
        self.sharedBytes = sharedBytes

        self.ctx = zmq.Context.instance()
        
        self. poll_timeout_ms =  configSocket.poll_timeout_ms
        self.pair_addr = configSocket.pair_addr

        self.parent_link = None
        self.poller = zmq.Poller()
        self.legacy_app_active = False

        # internal classes (can be mocked via ConfigTest)
        ct = configTests
        self.notifications = ct.notif if ct and ct.notif else Notifications()
        #setup crypto manager
        self.cryptoMgr = (ct.crypto if ct and ct.crypto else BTCryptoManager(self.sharedBytes))
        #setup subscriber processes zmq:
        self.process_bridge = ct.bridge if ct and ct.bridge else ServiceProcessBridge(self.ctx)

    def _setup_sockets_and_poller(self):
        '''
        Creates the bidirectional parent/child control link and registers
        all worker-owned sockets with the worker poller.
        '''
        self._unlink_pair_socket()
        self.parent_link = self.ctx.socket(zmq.PAIR)
        self.parent_link.setsockopt(zmq.LINGER, 0)
        self.parent_link.bind(self.pair_addr)
        self.poller.register(self.parent_link, zmq.POLLIN)
        self.poller.register(self.process_bridge.response_pull, zmq.POLLIN)

    def _unlink_pair_socket(self):
        if not self.pair_addr.startswith("ipc://"):
            return
        sock_path = self.pair_addr[len("ipc://"):]
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def run(self):
        self._setup_sockets_and_poller()
        try:
            while True:
                self.step()
        finally:
            self._close()

    def step(self):
        events = dict(self.poller.poll(timeout=self.poll_timeout_ms))

        # Highest priority: parent messages received over the parent/child link.
        if self.parent_link in events and (events[self.parent_link] & zmq.POLLIN):
            self._drain_parent_messages()

        # Secondary: logic responses -> encrypt -> chunk -> enqueue to svc
        if self.process_bridge.response_pull in events and (events[self.process_bridge.response_pull] & zmq.POLLIN):
            self._drain_logic_responses()

    def _drain_logic_responses(self):
        n = 0
        try:
            parts = self.process_bridge.response_pull.recv_multipart(flags=zmq.DONTWAIT)
            if len(parts) >= 3:
                topic_bytes, trace_id, payload_bytes = parts[0], parts[1], parts[2]
            else:
                topic_bytes, payload_bytes = parts[0], parts[1]
                trace_id = b""
            self.process_bridge.response_pull.send(REQ_ACK_OK)
            self.on_message_from_process(topic_bytes, payload_bytes, trace_id)
        except zmq.Again:
            return  #nothing left to read
        except zmq.ContextTerminated:
                Log.log("exit due to zmq context terminated error")
                raise
        except zmq.ZMQError as e:
            Log.log(f"critical error: failed response ACK transaction on resp.sock: {e}",
                    level=Log.CRITICAL)
            # handle other fatal socket errors
            try:
                self.poller.unregister(self.process_bridge.response_pull)
            except KeyError:
                pass
            try:
                self.process_bridge.set_or_reset_pull()
                self.poller.register(self.process_bridge.response_pull, zmq.POLLIN)
            except zmq.ZMQError as e:
                #if context is terminated when this is called, ZMQError is raised
                Log.log("exit due error while reseting bridge: {e}",
                    level=Log.CRITICAL)
                raise


    # -------- parent / BLE inbound --------

    def _drain_parent_messages(self):
        '''
        Receives typed commands from the parent process and dispatches them
        to the appropriate worker-side action.
        '''
        while True:
            try:
                parts = self.parent_link.recv_multipart(flags=zmq.DONTWAIT)
            except zmq.Again:
                break
            except zmq.ContextTerminated:
                Log.log("exit due to zmq context terminated error",
                    level=Log.CRITICAL)
                raise

            if not parts:
                continue

            msg_type = parts[0]
            if msg_type == b"STOP":
                Log.log("received STOP - exiting",
                    level=Log.INFO)
                raise SystemExit
                return
            if msg_type == b"BLE_IN" and len(parts) > 2:
                self._process_ble_inbound(parts[1], parts[2])
            elif msg_type == b"PUBLISH_TO_CHANNEL" and len(parts) >= 4:
                self.process_bridge.publish(parts[1], parts[3], parts[2])

    def _process_ble_inbound(self, trace_id: bytes, raw: bytes):
        """
        Process one BLE->worker payload delivered over the parent link.
        """
        #attempt to decrypt normally
        decrypted = self._decrypt(raw)
        if decrypted is None: #should not happen
            return
        
        """
        Managing logic of crypto response to CheckIn, or any garbled message:
        if pi is locked - and decrypting attempt failed, it is either wrong password or bluetooth channel corruption,
        either way send back  - stamped as crypto:
            - if there is no Password (we should be in unlocked) -> send No Password
                - the app should not have sent a checkin message in unlocked mode - but if it did, the app nneds to recognized there is No Password: 
                    Important - this should not happen unless the app has a problem: it will have not seen LOCK in piInfo
            - if there is a password, the app will have sent CheckIn but it is either wrong password or bluetooth corruption:
                - send Locked - if piInfo says Lock or unlocked if piInfo does not have LOCK
                - if LOCK: the word Lock will not be decoded there - and besides it is looking for CheckedIn
                    (there is the case where it has the right password but we coulf not decrypt here due to blutooth channel corruption
                    In this case - the app will deconde Locked - and no it has the right password)
                - if send unlocked, again the app should no that pi is unlocked and not attempt to decode - if it sees unlocked, 
                it will no that it's message sent in clear was garbled by bluetooth connection - and should tale appropriate action 
                 - such as resending.
        The point of this analysis is:  if a message sent to decryption comes back Garbled, even if it happens after it has 
        been established that the password is OK - always respond with crypto channel stamp and send one of 3 applicable response:
            - NoPAssword, Lockec or unlocked
        """
        
        if decrypted == b'Garbled':
            if self.cryptoMgr.crypto is None:
            #in unlocked mode - app set its password and sent LockRequest (no channel) encrypted
            # since pi is unlocked - it failed decryption and returned Garbled
            #this attempts to decrypt - and if it sees LockRequest - it knows to lock itself
            # the appropriate response is to send Locked, encrypted - which tells the app that
            #the rewuset was accepted and processed
                if self.cryptoMgr.check_act_LockRequest(raw):
                    #if succesful - pi is locked, setPiInfo was done, send correct response which will be encrypted
                    self.on_decrypted_message(b'Locked', trace_id,True)
                    return
                #note - if cannot decode as LockRequest - move on with the normal Garbled response

            if self.cryptoMgr.pi_info.password is None:
                # no encryption is possible - send back NoPassword
                self.on_decrypted_message(b'NoPassword', trace_id, True)
            else:
                #There is a password - send the current lock status back
                reply = b'Unlocked' if self.cryptoMgr.crypto is None else b'Locked'
                self.on_decrypted_message(reply, trace_id,True)
        
        else:
            """normal operation:decryption did not fail
            - if process is hanshaking the crypt - message will have no channel stamp and command is "CheckIn"
            - if crypto check is over - normal communication:
                - BLEConnect library sends a stamped message with logical channel if
                - legacy BtBerryWifi app sends wifi commans with no stamp (we will need to add it next
            """
            self.on_decrypted_message(decrypted, trace_id)


    # -------- helpers --------

    def on_decrypted_message(self,msg_bytes, trace_id: bytes = b"",isForCrypto: bool = False):
        #this is the decrypted message from crypto
        """
        messages other than crypto related message (checkin etc.) arrive with the logical channel id
        prefix_bytes, sep_found, msg_bytes = msg_bytes.partition(CHANNEL_SEP_BYTES)
            set up in the Kotlin/IOS code, and separated from the message with 
            so they need to be extracted before deciding where to send them
        note: to be compatible with original BTBerryWifi app - which sends messages for wifi without
                a prefix - a legacy mode can be set which inserts the correct bpawifi prefix
        """

        if isForCrypto:
            #those as the response due to Garbled decryption
            self.handle_crypto_messages(msg_bytes, trace_id)
            return

        #now check the possible crypto commands which arrive without a prefix
        elif msg_bytes == b'\x1eUnlockRequest' or msg_bytes == b'\x1eCheckIn' or msg_bytes == b'CheckIn':
            self.handle_crypto_messages(msg_bytes, trace_id)
            return

        # from here normally should have a channel id unless it is the legacy BTBerryWifi app
        #extract channel id: (note: returns first part, the actual separator if found, then paylaod)
        prefix_bytes, sep_found, use_msg_bytes = msg_bytes.partition(CHANNEL_SEP_BYTES)
        #note: if no separator, then the entire message ends up in prefix_bytes, and the other two are blank
        if sep_found == b'' or use_msg_bytes == b'':
            #there was no prefix - assume legacy and send to wifi by adding bpawifi
            #also set the legacy flag (to catch return message and patch them)
            Log.log(f"no prefix to message ${msg_bytes} - adding bpawifi")
            self.legacy_app_active = True
            prefix_bytes = b"bpawifi" 
            use_msg_bytes = msg_bytes
        elif prefix_bytes == b'bpawifi':
            # special rare case where an updated app is using the correct bpawifi data channel
            # in which case if a legacy app was also using it - remove the flag
            self.legacy_app_active = False

        # Publish directly to the data plane; if nobody is subscribed, ZMQ drops the message.
        self.process_bridge.publish(prefix_bytes, use_msg_bytes, trace_id)
        
    def handle_crypto_messages(self,msg_bytes, trace_id: bytes = b""):
        """
            if encrypted message sent to decryption was CheckIn, and is decrypted correctly
            it arrives here decoded, and we need to respond with CheckedIn (which will also be encrypted)
            Any other message (whether from a logicl channel, or that CheckIn message) will either have returned:
                - the original message decrypted, with a prefic channel id (and does not come here)
                - the word Garbled, when decryption has failed
                - one of the Lock/Unlock process response
            for the last two options we sent that back to device over bluetooth - so it can react 
        """
        to_send = ['Garbled', b'Locked', b'NoPassword', b'Unlocked']
        if msg_bytes in to_send:
                Log.log(f"sending to device: {msg_bytes} from crypto ")
                self.on_message_from_process(b"crypto", msg_bytes, trace_id)
        elif msg_bytes == b'\x1eUnlockRequest':
            #this arrived while pi was locked:
            # first send the encypted notification : pi is unlocking
            self.on_message_from_process(b"crypto", b'Unlocking', trace_id)
            #then unlock the pi, so all other message go out in clear
            self.cryptoMgr.disableCrypto()
        elif msg_bytes == b'\x1eCheckIn': #legacy CheckIn message
            self.legacy_app_active = True
            self.on_message_from_process(b"crypto", b'CheckedIn', trace_id)
        elif msg_bytes == b'CheckIn':
            self.on_message_from_process(b"crypto", b'CheckedIn', trace_id)
            # self.notifications.makeNotifications('CheckedIn',"crypto")
        else:
            #should not happen except for blank b'' for stale nonce
             Log.log(f"received: {msg_bytes} with no channel id  / msg discarded",
                    level=Log.INFO)

    def on_message_from_process(self,target_bytes,message_bytes, trace_id: bytes = b""):
        """
        callback for Pull zmq from Processes.
        send messages to be passed on to bluetooth characteristic via Notification process
        maleNotifications always append the notification marker and the topic (crypto, bpawifi, ot user topic)
        """
        #this is to match existing published BTBerryWifi app
        if self.legacy_app_active and target_bytes == b"bpawifi":
            target_bytes = b"wifi"

        #Channel StartUp Messages section (process messages that cause an action in pipeline worker instead of a notification over bluetooth)
        msg,target = self.notifications.get_target_and_message_as_strings(message_bytes,target_bytes)
        if target is not None and msg is not None:
            if target == "bpawifi":
                if msg == "NMDBUS":
                    #need to setup dbus scan complete monitoring
                    try:
                        self.parent_link.send_multipart([b"NMDBUS", trace_id, b""], flags=zmq.DONTWAIT)
                    except zmq.Again:
                        pass
        else:
            #no point in trying to manage notifications:
            return
        
        #get notifications (list of chuncks possibly)
        notifications = self.notifications.makeNotifications(message_bytes,target_bytes)
        for notification in notifications:
            encrypted = self._encrypt(notification)
            try:
                self.parent_link.send_multipart([b"NOTIFY_OUT", trace_id, encrypted], flags=zmq.DONTWAIT)
            except zmq.Again:
                break

    def _close_sockets(self):
        if self.parent_link is not None:
            try:
                self.poller.unregister(self.parent_link)
                self.poller.unregister(self.process_bridge.response_pull)
                self.parent_link.close(0)
            except Exception:
                pass
        self.process_bridge.close()

    def _close(self):
        self.cryptoMgr.close()
        self._close_sockets()
        Log.close()


    def _decrypt(self, raw: bytes) -> Optional[bytes]:
        clear_msg = self.cryptoMgr.decrypt(raw)
        return clear_msg

    def _encrypt(self, plaintext: bytes) -> Optional[bytes]:
        encrypted = self.cryptoMgr.encrypt(plaintext)
        return encrypted


def run_pipeline_worker(shared_bytes: SharedBytes, configSocket: ConfigSocket) -> None:
    '''
    Process entrypoint that constructs the worker with its runtime configuration
    and runs its main event loop.
    '''
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    worker = PipelineWorker(
        kick_glib=None,
        sharedBytes=shared_bytes,
        configSocket=configSocket
    )
    worker.run()
