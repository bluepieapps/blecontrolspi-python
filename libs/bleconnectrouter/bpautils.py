import dbus
from typing import Dict, List
from pathlib import Path
import subprocess
import os
import socket

import subprocess
import sys
from pathlib import Path
from typing import List, Dict

from libs.bleconnectrouter.zmqlogger import ZmqLogServer, ZmqLogClient as Log

class LogServerStarter:
     
    def __init__(self, log_syslog,log_console):
        self.logger_process = None
        self._syslog = log_syslog
        self._console = log_console

    def start(self):
        self.logger_process = ZmqLogServer(
            print_to_syslog=self._syslog,
            print_to_console=self._console,
            need_notify=False
            )
        self.logger_process.start()
        Log.initialize()

    def stop(self):
        if self.logger_process is not None:
            self.logger_process.terminate()
            self.logger_process.join(timeout=2.0)

        try:
            Log.close()
        except:
            pass

class Channels:
    """
        use this when run_mode is INTEGRATED (as opposed to INDEPENDENT_SERVICES)
        to start the logger process and the channel processors as subprocesses of BleManager
        - channel_files: list like ["diagnosticChannel.py", "wifiChannel.py"]
    """

    def __init__(self, channel_files: List[str]):
        self.channels_dir = Path(__file__).resolve().parent / "channels"
        self.channel_files = channel_files
        self.processes: Dict[str, subprocess.Popen] = {}

    # -------------------------
    # START
    # -------------------------
    def start_all(self):

        for filename in self.channel_files:
            try:
                self.start(filename)
            except FileNotFoundError as e:
                path = self.channels_dir / filename
                Log.log(f"Does NOT EXIST: {filename}: {path}",level=Log.CRITICAL)

    def start(self, filename: str):
        path = self.channels_dir / filename

        if not path.exists():
            raise FileNotFoundError(f"Channel file not found: {path}")

        # already running?
        if filename in self.processes:
            proc = self.processes[filename]
            if proc.poll() is None:
                return

        proc = subprocess.Popen(
            [sys.executable, str(path)],
            cwd=str(self.channels_dir)
        )

        self.processes[filename] = proc

    # -------------------------
    # STOP
    # -------------------------
    def stop_all(self):
        for proc in self.processes.values():
            if proc.poll() is None:
                proc.terminate()

        for proc in self.processes.values():
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)

        self.processes.clear()


class SystemDNotify:

    @staticmethod
    def notify_ready():
        msg = "READY=1\nSTATUS=BPABLEConnectRouter running and accepting connections."
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



def dbus_to_python(data):
    '''
        convert dbus data types to python native data types
    '''
    if isinstance(data, dbus.String):
        data = str(data)
    elif isinstance(data, dbus.Boolean):
        data = bool(data)
    elif isinstance(data, dbus.Int64):
        data = int(data)
    elif isinstance(data, dbus.Double):
        data = float(data)
    elif isinstance(data, dbus.Array):
        data = [dbus_to_python(value) for value in data]
    elif isinstance(data, dbus.Dictionary):
        new_data = dict()
        for key in data.keys():
            new_data[dbus_to_python(key)] = dbus_to_python(data[key])
        data = new_data
    return data 

