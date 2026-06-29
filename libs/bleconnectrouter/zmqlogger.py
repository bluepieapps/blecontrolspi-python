import json
import os
import signal
import sys
import socket
import syslog
import time
from datetime import datetime
from multiprocessing import Process, current_process
from libs.bleconnectrouter import bpaconfig

import zmq

class L_SystemDNotify:

    @staticmethod
    def notify_ready( exception_message = None):
        if exception_message is None:
            msg = "READY=1\nSTATUS=BPABLEConnectRouter running and accepting connections."
        else:
            msg = "STOPPING=1\nSTATUS=Logger shutodown due to exception"

        sock = None
        notify_socket = os.environ.get('NOTIFY_SOCKET')
        if not notify_socket:
            # Not running under systemd, skip silently
            return

        try:
             # Handle abstract socket names (start with @)
            if notify_socket.startswith('@'):
                notify_socket = '\0' + notify_socket[1:]
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.sendto(msg.encode("utf-8"), notify_socket)
        finally:
            if sock is not None:
                sock.close()

    

class ZmqLogServer(Process):
    """
    Central logging process that receives log records from multiple processes
    over a ZMQ PULL socket and writes them to syslog and/or console.
    """

    DEFAULT_ENDPOINT = bpaconfig.BPA_LOGGER_ENDPOINT

    def __init__(self, print_to_syslog=True, print_to_console=False, need_notify = True):
        super().__init__(daemon=False)
        self.endpoint = ZmqLogServer.DEFAULT_ENDPOINT
        self.print_to_syslog = print_to_syslog
        self.print_to_console = print_to_console
        self._notify = need_notify
        self.keep_listening = False
        self.ctx = None
        self.pull_socket = None

    def _ensure_ipc_dir(self):
        os.makedirs(bpaconfig.RUN_DIR, exist_ok=True)
        try:
            os.chmod(bpaconfig.RUN_DIR, 0o755)
        except PermissionError:
            pass

    def _unlink_endpoint(self):
        if not self.endpoint.startswith("ipc://"):
            return
        sock_path = self.endpoint[len("ipc://"):]
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _setup_socket(self):
        self._ensure_ipc_dir()
        self._unlink_endpoint()
        self.ctx = zmq.Context.instance()
        self.pull_socket = self.ctx.socket(zmq.PULL)
        self.pull_socket.setsockopt(zmq.LINGER, 0)
        self.pull_socket.setsockopt(zmq.RCVHWM, 5000)
        old_umask = os.umask(0o000)
        try:
            self.pull_socket.bind(self.endpoint)
            if self.endpoint.startswith("ipc://"):
                os.chmod(self.endpoint[len("ipc://"):], 0o666)
        finally:
            os.umask(old_umask)

    def _teardown_socket(self):
        if self.pull_socket is not None:
            try:
                self.pull_socket.close(0)
            except Exception:
                pass
            self.pull_socket = None
        self._unlink_endpoint()

    def _on_sigterm(self, signum, frame):
        self.keep_listening = False

    def _format_record(self, record):
        process_name = record.get("process", "unknown-process")
        pid = record.get("pid", "?")
        identifier = record.get("identifier", "")
        func = record.get("func", "")
        level_name = record.get("level_name", "")
        msg = record.get("msg", "")

        left = f"{process_name}[{pid}] {level_name}"
        if identifier and func:
            body = f"{identifier}.{func} - {msg}"
        elif identifier:
            body = f"{identifier} - {msg}"
        elif func:
            body = f"{func} - {msg}"
        else:
            body = f"{msg}"

        return f"{left} {body}"

    def _write_record(self, record):
        line = self._format_record(record)
        if self.print_to_syslog:
            syslog.syslog(line)
        if self.print_to_console:
            print(line)

    def run(self):
        signal.signal(signal.SIGTERM, self._on_sigterm)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if self.print_to_syslog:
            syslog.openlog(ident="bpa-logger", facility=syslog.LOG_DAEMON)

        self._setup_socket()
        self.keep_listening = True
        poller = zmq.Poller()
        poller.register(self.pull_socket, zmq.POLLIN)
        if self._notify: L_SystemDNotify.notify_ready()
        try:
            while self.keep_listening:
                events = dict(poller.poll(250))
                if self.pull_socket not in events:
                    continue

                while True:
                    try:
                        payload = self.pull_socket.recv(flags=zmq.DONTWAIT)
                    except zmq.Again:
                        break
                    except zmq.ZMQError:
                        self.keep_listening = False
                        break

                    try:
                        record = json.loads(payload.decode("utf-8"))
                    except Exception:
                        malformed = {
                            "process": "zmqlogger",
                            "pid": os.getpid(),
                            "level_name": "CRITICAL",
                            "identifier": "ZmqLogServer",
                            "func": "run",
                            "msg": "Malformed log record received",
                            "ts_ns": time.time_ns(),
                        }
                        self._write_record(malformed)
                        continue

                    self._write_record(record)

        except Exception as e:
             if self._notify: 
                msg = f"{type(e).__name__}: {e}"
                L_SystemDNotify.notify_ready(msg)
                
             self._teardown_socket()
             sys.exit(1)
        finally:
            self._teardown_socket()


class ZmqLogClient:
    """
    Process-local logger used by application processes.
    Each process initializes its own PUSH socket and sends structured log
    records to the central logger process.
    """

    DEBUG = 10
    INFO = 20
    CRITICAL = 30
    NEVER = 100

    # For debuging: do not send just prints
    IS_DEBUGGING = False

    current_level = DEBUG
    endpoint = ZmqLogServer.DEFAULT_ENDPOINT
    process_label = current_process().name
    _ctx = None
    _push_socket = None
    _pid = os.getpid()
    _drop_count = 0
    _initialized = False

    @staticmethod
    def logLevel():
        if ZmqLogClient.current_level == ZmqLogClient.DEBUG: return "DEBUG"
        if ZmqLogClient.current_level == ZmqLogClient.INFO: return "INFO"
        if ZmqLogClient.current_level == ZmqLogClient.CRITICAL: return "CRITICAL"
        if ZmqLogClient.current_level == ZmqLogClient.NEVER: return "NEVER"

    @staticmethod
    def initialize(log_level="DEBUG", process_label=None):
        level_dict = {
            "DEBUG": ZmqLogClient.DEBUG,
            "INFO": ZmqLogClient.INFO,
            "CRITICAL": ZmqLogClient.CRITICAL,
            "NEVER": ZmqLogClient.NEVER,
        }

        ZmqLogClient.current_level = level_dict.get(log_level, ZmqLogClient.INFO)
        ZmqLogClient.process_label = process_label or current_process().name
        ZmqLogClient._pid = os.getpid()
        ZmqLogClient._ctx = zmq.Context.instance()

        if ZmqLogClient._push_socket is not None:
            try:
                ZmqLogClient._push_socket.close(0)
            except Exception:
                pass

        ZmqLogClient._push_socket = ZmqLogClient._ctx.socket(zmq.PUSH)
        ZmqLogClient._push_socket.setsockopt(zmq.LINGER, 0)
        ZmqLogClient._push_socket.setsockopt(zmq.SNDHWM, 2000)
        ZmqLogClient._push_socket.connect(ZmqLogClient.endpoint)
        ZmqLogClient._initialized = True

    @staticmethod
    def log(msg, identifier='', level=DEBUG, get_func_name=True):
        if not ZmqLogClient._initialized:
            return
        if level < ZmqLogClient.current_level:
            return

        try:
            if ZmqLogClient.IS_DEBUGGING:
                print(msg)
                return
            func_name = sys._getframe(1).f_code.co_name if get_func_name else ''
            record = {
                "level": level,
                "level_name": (
                    "" if level == ZmqLogClient.DEBUG else
                    "INFO" if level == ZmqLogClient.INFO else
                    "CRITICAL" if level == ZmqLogClient.CRITICAL else
                    str(level)
                ),
                "process": ZmqLogClient.process_label or current_process().name,
                "pid": os.getpid(),
                "identifier": identifier,
                "func": func_name,
                "msg": str(msg),
            }
        
            payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
            ZmqLogClient._push_socket.send(payload, flags=zmq.DONTWAIT)
        except zmq.Again:
            ZmqLogClient._drop_count += 1
        except Exception:
            pass

    @staticmethod
    def close():
        print("closing log push socket", flush=True)
        if ZmqLogClient._push_socket is not None:
            try:
                ZmqLogClient._push_socket.close(0)
            except Exception:
                pass
            ZmqLogClient._push_socket = None
        ZmqLogClient._initialized = False


if __name__ == "__main__":
    ZmqLogServer().run()
