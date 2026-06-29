import zmq
from gi.repository import GLib
import threading,os,errno
from libs.bleconnectrouter.zmqlogger import ZmqLogClient as Log
from libs.bleconnectrouter import bpaconfig

REQ_ACK_TIMEOUT_MS = 300


class MockCrypto:

    def decrypt(self,msg):
        return msg + " - DECRYPTED".encode("utf-8")
    
    def encrypt(self,msg):
        return msg + " - ENCRYPTED".encode("utf-8")



#This section is for communication with seprate process - uses Pub-Sub (outgoing) and Push-Pull (incoming)



class ServiceProcessBridge:
    """
    Manages:
      - pub_socket: BLE publishes to multiple subscribers (binds, ipc)
      - response_pull: BLE REP socket receives multipart requests from others and replies with immediate ACKs (binds, ipc)
      - worker poll loop is attached to response_pull FD only
      - func_to_call is a method in the implementing class - typically WifiService,
            that accepts topic_as_bytes, and message_as bytes (topic is the logical chanel id)
    """
     
    def __init__(self, ctx: zmq.Context):
        self.zmq_context = ctx
        self.pub_socket = None
        self.response_pull = None
        # self._watch_response = None
        # self._method = func_to_call
        self._pub_path  = bpaconfig.BPA_PUB_SOCKET_PATH
        self._resp_path = bpaconfig.BPA_RESP_SOCKET_PATH
        self.ensure_ipc_dir()
        self.set_or_reset_pub()
        self.set_or_reset_pull()

    def ensure_ipc_dir(self):
        os.makedirs(bpaconfig.RUN_DIR, exist_ok=True)
        try:
            os.chmod(bpaconfig.RUN_DIR, 0o755)  # owner rwx, group rx, others rx
        except PermissionError:
            # If running as non-root and dir already exists with stricter perms, ignore
            pass

    def _unlink(self,zmq_socket,path):
        if zmq_socket is None: return
        if path is None:
            if zmq_socket == self.response_pull:   
                path = self._resp_path
            elif zmq_socket == self.pub_socket:
                path = self._pub_path
            if path is None: return
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            if e.errno != errno.ENOENT:
                #TODO: log error
                raise


    def bind_socket(self,zmq_socket):
        #inlink if it was previously bound
        if zmq_socket is None: return
        path = None
        if zmq_socket == self.response_pull:   
            path = self._resp_path
        elif zmq_socket == self.pub_socket:
            path = self._pub_path
        if path is None: return
        self._unlink(zmq_socket,path)
        #now set umask and bind desired socket
        old_umask = os.umask(0o000)  # set to 0000, returns the previous value
        try:
            zmq_socket.bind(f"ipc://{path}")
            os.chmod(path, 0o666)   # srw-rw-rw-
        finally:
            os.umask(old_umask)  # restore previous umask


    def set_or_reset_pub(self):
        if self.pub_socket:
            try: self.pub_socket.close(0)
            except Exception: pass
            self.pub_socket = None

        self.pub_socket = self.zmq_context.socket(zmq.PUB)
        self.pub_socket.setsockopt(zmq.SNDHWM, 1000)  # burst tolerance for fan-out
        self.pub_socket.setsockopt(zmq.LINGER, 0)
        self.bind_socket(self.pub_socket)


    def set_or_reset_pull(self):
        # detach watch before closing sockets
        # if self._watch_response is not None:
        #     self._detach_watch()
       
        # (Re)bind REP (others -> BLE)
        if self.response_pull:
            try: self.response_pull.close(0)
            except Exception: pass
            self.response_pull = None

        self.response_pull = self.zmq_context.socket(zmq.REP)
        self.response_pull.setsockopt(zmq.RCVHWM, 1000)
        self.response_pull.setsockopt(zmq.LINGER, 0)
        self.response_pull.setsockopt(zmq.SNDTIMEO, REQ_ACK_TIMEOUT_MS)
        self.bind_socket(self.response_pull)
    
    def publish(self, topic: str | bytes, payload: bytes, trace_id: bytes = b"") -> bool:
        """
        Send multipart [topic][trace_id][payload] to all current subscribers.
        - topic: str or bytes (will be encoded as UTF-8 if str)
        - payload: raw bytes
        """
        if not self.pub_socket:
            return False
        if isinstance(topic, str):
            topic = topic.encode("utf-8")
        try:
            self.pub_socket.send_multipart([topic, trace_id, payload], flags=zmq.DONTWAIT)
            return True
        except zmq.Again:
            # HWM reached: drop (publisher never blocks)/ msg not sent
            return False
        except zmq.ContextTerminated:
            self.close()
            raise   #so Service is aware context is terminated and can restart queues if needed / note that everything is closed though

    def close(self):
        # self._detach_watch()
        if self.pub_socket is not None:
            try: 
                self.pub_socket.close(0)
                self._unlink( self.pub_socket,None)
            except Exception: pass
        if self.response_pull is not None:
            try: 
                self.response_pull.close(0)
                self._unlink( self.response_pull,None)
            except Exception: pass








