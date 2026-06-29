
import argparse
import dbus
# import dbus.mainloop.glib
from gi.repository import GLib

from libs.bleconnectrouter.bpable import *
from libs.bleconnectrouter import bpaconfig
from libs.bleconnectrouter.bpautils import Channels,LogServerStarter,SystemDNotify
from libs.bleconnectrouter.zmqlogger import ZmqLogClient as Log
from libs.bleconnectrouter.pipelineworker import SharedBytes, ConfigSocket, run_pipeline_worker
from libs.bleconnectrouter.nm_scan_monitor import NetworkManagerScanMonitor

import signal
import subprocess
import zmq
from multiprocessing import Process
from collections import deque
import random
import sys
from typing import Dict, List
from pathlib import Path
from time import sleep, monotonic, monotonic_ns

"""
There are two ways to run BPA BLEConnect Router and associated Channel Handlers Processes:
    1. Independent services:
        - the service bpa-bleconnector.service - runs the code in this file:
            - see installer (bpacontrolsinstall-v1.x.y.sh to see how to set up the service)
        - it may be enabled (start at boot) - but it is not necessary.
        Each Channel Processor gets its own service name function-channel.service
            - where function is the first word of the filename (such as wifi-channel.service for wifiChannel.py)
            - see installer (bpacontrolsinstall-v1.x.y.sh to see how to set up the service)
            - These services require bpa-bleconnector.service - forcing it to start 
            - These services should may be enabled, if you want them to start at boot

    2. Integrated:
        - the service: create a service called bpa-bleconnector-integrated.service 
            - the execStart line uses the flag -integrated
            - Through the Channels class:
            - BleManager uses the array CHANNELS_TO_START, and starts each services listed there as a separate process
                using subprocess.Popen.  It also shuts them down in quitBT.

    Note: using conflicts directive, the systemd service Unit file is designed to prevent logServer 
        and normal bleconnector service from starting if the integrated service is starting - which means that
        a chanel service file (defined under the Independent mode)  will not start if integrated service is created and enabled.

"""

#for Integrated running option (only one systemd service)
# Add the channel .py file that needs to be started as a sub process from here
#  if using Integrated running Option - for example
#CHANNELS_TO_START = ["wifiChannel.py","diagnosticChannel.py"]

CHANNELS_TO_START = []
TRACE_TIMING_ENABLED = False

SEPARATOR_HEX = b'\x1e'
SEPARATOR = SEPARATOR_HEX.decode()  # string representation can be concatenated or use in split function
NOTIFY_TIMEOUT = 75  #in ms - used for checking notifications to send
BLE_SERVER_GLIB_TIMEOUT = 2500  # used for checking BLE Server timeout

class ConfigData:
    '''
    A timeout exists that will shutdown the BLE Server 
        if it does not receive commands from the iphone app within this timeout period.
        - BLE_shutdown_time = xx, where xx is the number of minutes for the time out.
        - insert "never" if it nevers time out
    Note that every time a command is received from the ios iphone app 
    - the time out period is reset to zero.
    '''
    START = 0  #time at which we start counting BLE Server usage.
    TIMEOUT = 30*60 #this is in seconds - defaults to 30 minutes
    LOGTOSYSLOG = True
    LOGTOCONSOLE = False
    INTEGRATED = False

    

    @staticmethod
    def initialize():
        parser = argparse.ArgumentParser(
            description="BLE Peripheral - BLEConnect Router Implementation")

        parser.add_argument("--timeout", help="Server timeout in minutes")
        parser.add_argument(
            "--integrated",
            help="Start All Channel Processor & Logger as sub-processes",
            action="store_true"
        )
        parser.add_argument(
            "--console",
            help="Also print log messages to console",
            action="store_true"
        )
        parser.add_argument(
            "--nosyslog",
            help="Do not send log messages to syslog",
            action="store_true"
        )
        parser.add_argument("--loglevel", default="DEBUG",
                    help="Logging level (e.g., DEBUG, INFO, CRITICAL, NEVER)")
        
        try:
            args = parser.parse_args()
            if args.timeout is not None: ConfigData.TIMEOUT = int(args.timeout)*60
            # if user has added nosyslog - then automatically print to console
            #note: if user has added --console, logs still go to syslog as well
            ConfigData.LOGTOCONSOLE = args.console or args.nosyslog
            ConfigData.LOGTOSYSLOG = not args.nosyslog
            ConfigData.INTEGRATED = args.integrated
            Log.initialize(log_level=args.loglevel,process_label="BLEMgr")
        except Exception as e:
            Log.initialize("INFO")
            Log.log(f"command line argument error - using INFO level - error was: {e}",
                    level=Log.INFO)

       

    @staticmethod
    def reset_inactivity_timeout():
        ConfigData.START = monotonic()

    @staticmethod
    def check_inactivity_timeout():
        '''
        returns True if ConfigData.TIMEOUT has elapsed without being reset 
        which indicates no send activity
        note - if ConfigData.TIMEOUT = 0 -> never times out
        '''
        if ConfigData.TIMEOUT == 0:
             return False
        elif monotonic() - ConfigData.START > ConfigData.TIMEOUT:
            return True
        else:
            return False





class WifiSetService(Service):

    def __init__(self, index,main_loop,zmq_context, glib_context):
        self.main_loop = main_loop #this exists only so characteristics can set it as their mainloop
        
        Service.__init__(self, index, bpaconfig.UUID_WIFISET, True)
        self.notify_characteristic = WifiDataCharacteristic(0,self)
        self.add_characteristic(self.notify_characteristic)
        self.add_characteristic(InfoCharacteristic(1,self))
        # context for ZMq control queue
        self.zmq_ctx = zmq_context
        self.main_ctx = glib_context
        self._from_worker_q = deque()  # worker -> BLE notification chunks
        self._trace_counter = 0
        self._ble_trace_roundtrips = {}
        self._worker_pair_addr = bpaconfig.BPA_WORKER_PAIR_ENDPOINT
        # Bidirectional ZMQ link used for communication between the BLE service
        # process and the pipeline worker child process.
        self._worker_link = self.zmq_ctx.socket(zmq.PAIR)
        self._worker_link.setsockopt(zmq.LINGER, 0)
        self._worker_link.connect(self._worker_pair_addr)
        self._worker_watch = None
        self._nm_scan_monitor = None
        self._attach_worker_watch()
        # ---- Draining state (GLib thread) ----
        self._drain_scheduled = False
        # ---- Worker child process ----
        self.sharedBytes = SharedBytes() # shared Pi info cache used by the worker process and the BLE process
        config_socket = ConfigSocket(
            pair_addr=self._worker_pair_addr,
            poll_timeout_ms = 500
        )
        # The pipeline worker is started as a child process owned by the BLE service.
        self._worker = Process(
            target=run_pipeline_worker,
            args=(self.sharedBytes, config_socket),
            daemon=False
        )
        self._worker.start()
       


    def close(self):
        # 1) signal worker link so it exits quickly
        try:
            self._worker_link.send_multipart([b"STOP"], flags=zmq.DONTWAIT)
        except zmq.ZMQError as e:
            Log.log(f"WifiSetService.close: STOP send failed: errno={e.errno}, str={e}",
                    level=Log.CRITICAL)

        # 2) join
        self._worker.join(timeout=3.0)
        if self._worker.is_alive():
            self._worker.terminate()
            self._worker.join(timeout=1.0)
        # 3) close worker link
        self._detach_worker_watch()
        try:
            self._worker_link.close(0)
        except Exception:
            pass
        Log.log("service closing - after join")

    def on_received_bluetooth_data(self, raw_bytes: bytes):
        """
        Called by wifi characteristic WriteValue.
        Runs on GLib thread. Priority: forward toward worker immediately.
        """
        self._trace_counter = 1 if self._trace_counter >= 255 else self._trace_counter + 1
        trace_id = bytes((self._trace_counter, 0))

        if TRACE_TIMING_ENABLED:
            now_ns = monotonic_ns()
            self._ble_trace_roundtrips[trace_id[0]] = {
                "origin_trace": trace_id,
                "write_rx_ns": now_ns,
                "worker_rx_ns": None,
                "notify_start_ns": None,
                "notify_done_ns": None,
                "in_len": len(raw_bytes),
            }
        try:
            self._worker_link.send_multipart([b"BLE_IN", trace_id, raw_bytes], flags=zmq.DONTWAIT)
        except zmq.ZMQError:
            if TRACE_TIMING_ENABLED:
                self._ble_trace_roundtrips.pop(trace_id[0], None)
            pass

    def broadcast_system_msg(self):
        try:
            system_channel_bytes = bpaconfig.ZMQ_SYSTEM_TOPIC.encode("utf-8", errors="replace")
            self._worker_link.send_multipart(
                [b"PUBLISH_TO_CHANNEL", system_channel_bytes, b"\x00\x00", b"InactivityTimedOut"],
                flags=zmq.DONTWAIT
            )
        except zmq.Again:
            Log.log("ScanReady publish request dropped: worker link busy",
                    level=Log.INFO)
        except zmq.ZMQError as err:
            Log.log(f"ScanReady publish request failed: {err}",
                    level=Log.INFO)


    def _attach_worker_watch(self):
        '''
        Registers the parent-side worker link with the GLib mainloop so the
        service can react when the child process sends data back.
        '''
        fd = self._worker_link.getsockopt(zmq.FD)
        self._worker_watch = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            fd,
            GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
            self._on_worker_link_ready,
            None,
            None
        )

    def _detach_worker_watch(self):
        if self._worker_watch is not None:
            try:
                GLib.source_remove(self._worker_watch)
            except Exception:
                pass
        self._worker_watch = None

    def _schedule_notification_drain(self):
        '''
        Schedules the outbound BLE notification drain on the GLib loop if one
        is not already pending.
        '''
        if not self._drain_scheduled:
            self._drain_scheduled = True
            GLib.timeout_add(0, self._drain_one_notification)

    def _handle_worker_message(self, parts):
        '''
        Decodes one typed message received from the worker process and queues
        outbound BLE notifications when present.
        '''
        if len(parts) < 2:
            return None

        msg_type = parts[0]
        if msg_type == b"NOTIFY_OUT":
            if len(parts) >= 3:
                trace_id = parts[1]
                payload = parts[2]
                if TRACE_TIMING_ENABLED:
                    self._mark_ble_from_worker(trace_id, payload)
                self._from_worker_q.append((trace_id, payload))
            else:
                payload = parts[1]
                self._from_worker_q.append((None, payload))
            return "notify"
        elif msg_type == b"NMDBUS":
            self._ensure_nm_scan_monitor()
            return "set scan results"
        
        return None

    def _ensure_nm_scan_monitor(self):
        if self._nm_scan_monitor is not None:
            return

        try:
            self._nm_scan_monitor = NetworkManagerScanMonitor(
                on_scan_ready=self._on_nm_scan_ready,
                interface_name="wlan0"
            )
            self._nm_scan_monitor.start()
        except Exception as err:
            self._nm_scan_monitor = None
            Log.log(f"NetworkManager scan monitor setup failed: {err}",
                    level=Log.INFO)

    def _on_nm_scan_ready(self):
        try:
            self._worker_link.send_multipart(
                [b"PUBLISH_TO_CHANNEL", b"bpawifi", b"\x00\x00", b"\x1eScanReady"],
                flags=zmq.DONTWAIT
            )
        except zmq.Again:
            Log.log("ScanReady publish request dropped: worker link busy",
                    level=Log.INFO)
        except zmq.ZMQError as err:
            Log.log(f"ScanReady publish request failed: {err}",
                    level=Log.INFO)

    def _on_worker_link_ready(self, fd, condition, user_data=None, priority=None):
        '''
        Handles readability on the worker link by pulling a bounded number of
        messages from the child process and queuing any outbound notifications.
        '''
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            return False

        received_any = False
        for _ in range(8):
            try:
                parts = self._worker_link.recv_multipart(flags=zmq.DONTWAIT)
            except zmq.Again:
                break
            except zmq.ZMQError:
                return False

            result = self._handle_worker_message(parts)
            if result == "notify":
                received_any = True

        if received_any:
            self._schedule_notification_drain()

        return True

    def _drain_one_notification(self):
        """
        Sends EXACTLY ONE notification chunk then yields.
        Called repeatedly by GLib timeout source (0ms).
        """
        try:
            trace_id, payload = self._from_worker_q.popleft()
        except IndexError:
            self._drain_scheduled = False
            return False
        if TRACE_TIMING_ENABLED:
            self._mark_notify_start(trace_id)
        self.notify_characteristic.send_notification(payload)
        if TRACE_TIMING_ENABLED:
            self._mark_notify_done_if_complete(trace_id, payload)

        if not self._from_worker_q:
            self._drain_scheduled = False
            return False
        return True

    def _mark_ble_from_worker(self, trace_id: bytes, payload: bytes):
        ''' this is only called when TRACE_TIMING_ENABLED is True'''
        trace_info = self._get_ble_trace_info(trace_id)
        if trace_info is None:
            return
        now_ns = monotonic_ns()
        trace_info["response_trace"] = trace_id
        trace_info["worker_rx_ns"] = now_ns
        trace_info["out_len"] = len(payload)
        Log.log(
            f"TRACE stage=BLE_RX_FROM_PIPE trace={self._trace_hex(trace_id)} mono_ns={now_ns} len={len(payload)}",
            identifier="Trace",
            level=Log.INFO
        )

    def _mark_notify_start(self, trace_id: bytes):
        ''' this is only called when TRACE_TIMING_ENABLED is True'''
        trace_info = self._get_ble_trace_info(trace_id)
        if trace_info is None:
            return
        if trace_info["notify_start_ns"] is None:
            trace_info["notify_start_ns"] = monotonic_ns()

    def _mark_notify_done_if_complete(self, trace_id: bytes, payload: bytes):
        ''' this is only called when TRACE_TIMING_ENABLED is True'''
        trace_info = self._get_ble_trace_info(trace_id)
        if trace_info is None:
            return
        if self._queue_has_same_trace(trace_id):
            return

        trace_info["notify_done_ns"] = monotonic_ns()
        total_us = self._delta_us(trace_info["write_rx_ns"], trace_info["notify_done_ns"])
        wait_for_worker_us = self._delta_us(trace_info["write_rx_ns"], trace_info["worker_rx_ns"])
        notify_queue_us = self._delta_us(trace_info["worker_rx_ns"], trace_info["notify_start_ns"])
        notify_send_us = self._delta_us(trace_info["notify_start_ns"], trace_info["notify_done_ns"])
        Log.log(
            f"TRACE stage=BLE_ROUNDTRIP_DONE trace={self._trace_hex(trace_info.get('response_trace', trace_id))} "
            f"total_us={total_us} wait_worker_us={wait_for_worker_us} "
            f"notify_queue_us={notify_queue_us} notify_send_us={notify_send_us} "
            f"in_len={trace_info.get('in_len')} out_len={trace_info.get('out_len', len(payload))}",
            identifier="Trace",
            level=Log.INFO
        )
        self._ble_trace_roundtrips.pop(trace_id[0], None)

    def _get_ble_trace_info(self, trace_id: bytes):
        if not trace_id:
            return None
        if len(trace_id) < 1:
            return None
        return self._ble_trace_roundtrips.get(trace_id[0])

    def _queue_has_same_trace(self, trace_id: bytes):
        for queued_trace_id, _ in self._from_worker_q:
            if queued_trace_id == trace_id:
                return True
        return False

    def _trace_hex(self, trace_id: bytes):
        if not trace_id:
            return "0000"
        return trace_id.hex()

    def _delta_us(self, start_ns, end_ns):
        if start_ns is None or end_ns is None:
            return None
        return max(0, (end_ns - start_ns) // 1000)

    def _send_notification(self, payload_bytes: bytes):
        """
        Convert payload bytes -> dbus.Byte array and emit PropertiesChanged on notify characteristic.
        """
        Log.log(f"sending {len(payload_bytes)} bytes as bluetooth notification")
        self.notify_characteristic.send_notification(payload_bytes)
        value = dbus.Array((dbus.Byte(b) for b in payload_bytes), signature="y")
        self.notify_characteristic.PropertiesChanged(
            "org.bluez.GattCharacteristic1",
            {"Value": value},
            []
        )

class InfoCharacteristic(Characteristic):
    def __init__(self, index,service):
        Characteristic.__init__(self, index,bpaconfig.UUID_INFO,["read"], service)
        self.add_descriptor(InfoDescriptor(0,self))
        self.mainloop = service.main_loop

    def convertInfo(self,data):
        #this is only use for logging 
        msg = ""
        try: 
            prefix = data.decode("utf8")
        except:
            prefix = ""
        if prefix == "NoPassword": return "NoPassword"

        try:
            prefix = data[0:4].decode("utf8")
        except:
            prefix = ""
        if prefix == "LOCK" and len(data)>17:
            msg = prefix
            msg += str(int.from_bytes(data[4:16], byteorder='little', signed=False))
            msg += data[16:].hex()
            return msg
        if  len(data)>13:
            msg = str(int.from_bytes(data[0:12], byteorder='little', signed=False))
            msg += data[12:].hex()
        return msg


    def ReadValue(self, options):
        value = []
        msg_bytes = self.service.sharedBytes.get()
        for b in msg_bytes:
            value.append(dbus.Byte(b))
        Log.log(f'ios is reading PiInfo: {self.convertInfo(msg_bytes)}')
        return value


class InfoDescriptor(Descriptor):
    INFO_DESCRIPTOR_UUID = "2901"
    INFO_DESCRIPTOR_VALUE = "Pi Information"

    def __init__(self, index, characteristic):
        Descriptor.__init__(
                self, index, self.INFO_DESCRIPTOR_UUID,
                ["read"],
                characteristic)

    def ReadValue(self, options):
        value = []
        desc = self.INFO_DESCRIPTOR_VALUE

        for c in desc:
            value.append(dbus.Byte(c.encode()))
        return value

class WifiDataCharacteristic(Characteristic):

    def __init__(self, index,service):
        self.notifying = False
        # self.last_notification = -1
        Characteristic.__init__(self, index,bpaconfig.UUID_DATA,["notify", "read","write"], service)
        self.add_descriptor(InfoWifiDescriptor(0,self))
        self.mainloop = service.main_loop

    def send_notification(self,msg_bytes):
        if not self.notifying: return
        value = dbus.Array((dbus.Byte(b) for b in msg_bytes), signature="y")
        self.PropertiesChanged(
            "org.bluez.GattCharacteristic1",
            {"Value": value},
            []
        )
        ConfigData.reset_inactivity_timeout()
        Log.log(f"notification sent {len(msg_bytes)} bytes")

    def StartNotify(self):
        Log.log(f'ios has started notifications for wifi/data characteristic',
                    level=Log.INFO)
        if self.notifying:
            return
        self.notifying = True
        # self.add_timeout(NOTIFY_TIMEOUT, self.info_wifi_callback)

    def StopNotify(self):
        Log.log(f'ios has stopped notifications for wifi/data characteristic',
                    level=Log.INFO)
        self.notifying = False

    def ReadValue(self, options):
        #phone/tablet should not call read - this is a remnant of version one for wifi.
        #if it does - it will return a msg that appears to be a notification with the word EMPTY
        value = []
        msg = SEPARATOR+'EMPTY' 
        #TODO - should this be encrypted? - not used...
        msg_bytes = msg.encode()
        for b in msg_bytes:
            value.append(dbus.Byte(b))
        Log.log(f'ios is reading AP msg: {msg}')
        return value

    def WriteValue(self, value, options):
        #this is called by Bluez when the client (IOS) has written a value to the server (RPI)
        value_python_bytes = bytearray(value)
        self.service.on_received_bluetooth_data(value_python_bytes)
        Log.log(f'received from phone -  bluetooth msg of {len(value_python_bytes)} bytes')
        # self.service.notifications.notifications.append(value_python_bytes)
        ConfigData.reset_inactivity_timeout()  # any data received from iphone resets the BLE Server timeout

class InfoWifiDescriptor(Descriptor):
    INFO_WIFI_DESCRIPTOR_UUID = "2901"
    INFO_WIFI_DESCRIPTOR_VALUE = "AP-List, Status, write:SSID=xxxPW=yyy"

    def __init__(self, index, characteristic):
        Descriptor.__init__(
                self, index, self.INFO_WIFI_DESCRIPTOR_UUID,
                ["read"],
                characteristic)

    def ReadValue(self, options):
        value = []
        desc = self.INFO_WIFI_DESCRIPTOR_VALUE

        for c in desc:
            value.append(dbus.Byte(c.encode()))
        return value


class BLEManager:

    def __init__(self):

        self.channels = None
        self.logServer = None
        ConfigData.initialize()
        self.mainloop = GLib.MainLoop()
        self.counter = 0
        self.pi_hostname = self.get_hostname()
        
    def check_locks(self):
        '''
        tries to acquires file locks for running the ble advertizement and zmq management per bpaconfig
        keeps reference to the class (so they can be released) if lock are acquired / return True
        returns false if not
        '''
        #first check the ble advetising
        self.ble_lock = bpaconfig.BLELock()
        lock_acquired =  self.ble_lock.acquire()
        if not lock_acquired: return False

        #next check zmq management (namespacing)
        self.zmq_lock = bpaconfig.ZMQLock()
        lock_acquired = self.zmq_lock.acquire()
        if not lock_acquired:
            self.ble_lock.release()
            return False
        #both locks acquire - continue:
        return True


    def setUpSignals(self):
        try:
            signal.signal(signal.SIGTERM, self.graceful_quit)
            signal.signal(signal.SIGINT, self.graceful_quit)  # for Ctrl+C
        except:
            pass
       
    def get_hostname(self):
        result = subprocess.run("hostname", 
                                shell=True,capture_output=True,encoding='utf-8',text=True)
        if result.stdout:
            return result.stdout
        else:
            return f"unknown_{random.randint(1, 1000)}"

    def quitBT(self):
        Log.log(f"quitting Bluetooth - NEED_RESTART is {NEED_RESTART}",
                level=Log.INFO)
        if getattr(self, "_quitting", False):
            return False  # don't run again
        self._quitting = True


        try:
            if self.advert: 
                Log.log("calling advertisement de-registration")
                self.advert.unregister()
        except Exception as ex:
            Log.log(f"error unregistering advertisement {ex}",
                level=Log.CRITICAL)
        try:
            if self.app: 
                Log.log("calling application de-registration")
                #note: this closes and deregister services.
                self.app.unregister()
        except Exception as exx:
            Log.log(f"error unregistering application {exx}",
                    level=Log.CRITICAL)
            sleep(1)
       
        Log.close()
        sleep(0.1)

        #stop logServer child process
        self.logServer.stop()

        # if running integrated stop all services
        if ConfigData.INTEGRATED and self.channels is not None:
            self.channels.stop_all()

        self.zctx.term() #should not block as sockets have been closed with call to .wifiset_service.close()
        self.ble_lock.release()
        self.zmq_lock.release()
        print("quitting mainloop", flush = True)
        self.mainloop.quit()
        return False  # run once


    def graceful_quit(self,signum,frame):
        Log.log("stopping main loop on SIGTERM received",
                    level=Log.INFO)
        GLib.idle_add(self.quitBT)
    
    def timeout_manager(self):

        if ConfigData.check_inactivity_timeout():
            Log.log("BLE Server timeout - exiting...",
                    level=Log.INFO)
            #boradcast message to all channels
            if getattr(self, "wifiset_service", False):
                self.wifiset_service.broadcast_system_msg()
            GLib.idle_add(self.quitBT)
            return False
        else:
            return True

    

    def run(self):
        #Start LogServer as child subprocess
        self.logServer = LogServerStarter(ConfigData.LOGTOSYSLOG,
                                         ConfigData.LOGTOCONSOLE)
        self.logServer.start()
        sleep(0.5)
        Log.log("\n******************************************************",
                    level=Log.INFO)
        Log.log("** Starting BPA BLE Connect Router service",
                    level=Log.INFO)
        Log.log("** Version date: June 29 2026 **\n",
                    level=Log.INFO)
        #Log.log(f'BTwifiSet timeout: {int(ConfigData.TIMEOUT/60)} minutes')
        Log.log("starting BLE Server")
        ConfigData.reset_inactivity_timeout()
        
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

        Blue.set_adapter()
        Blue.bus.add_signal_receiver(Blue.properties_changed,
                    dbus_interface = "org.freedesktop.DBus.Properties",
                    signal_name = "PropertiesChanged",
                    arg0 = "org.bluez.Device1",
                    path_keyword = "path")

                    
        self.app = Application(self)
        #added passing a reference to the session dbus so service can register the userapp dbus listener when needed
        # justTesting = True
         #set up zmq context 
        self.zctx = zmq.Context.instance()
        self.wifiset_service = WifiSetService(0,self.mainloop,self.zctx,GLib.MainContext.default())
        self.app.add_service(self.wifiset_service)
        self.app.register()
        if self.app.registered:
            self.advert = Advertise(0,self,bpaconfig.UUID_WIFISET)
            self.advert.register()
            if not self.advert.registered: return # adv mgr was none - so quit now without trying mainloop
        else:
            return # gatt mgr was none - so quit now without trying mainloop
       
        self.setUpSignals()
        
        try:
            #BLE_SERVER_GLIB_TIMEOUT: how often to check if service duration timeout has occurred
            GLib.timeout_add(BLE_SERVER_GLIB_TIMEOUT, self.timeout_manager)
            #Launch channel processor & logger as sub processes if running integrated
            if ConfigData.INTEGRATED:
                self.channels = Channels(CHANNELS_TO_START)
                self.channels.start_all()
                Log.log("starting main loop: run mode = Integrated")
            else:
                Log.log("starting main loop: run mode = Independent service -> notify systemD")
                SystemDNotify.notify_ready()

            self.mainloop.run()
        except Exception as e:
            Log.log(f"exception while running mainloop - exiting: {e}",
                    level=Log.CRITICAL)
            try:
                GLib.idle_add(self.quitBT)
            except Exception:
                self.quitBT()   # last resort


#Globals for RESTART Management
NEED_RESTART = False
restart_count = 0

def btRestart():
        cmd = "systemctl stop bluetooth"
        print("stopping bluetooth service", flush = True)
        rstop = subprocess.run(cmd, shell=True,text=True, timeout = 10)
        sleep(1)
        cmd = "systemctl start bluetooth"
        print(f"starting bluetooth service - restart count = {restart_count}", flush = True)
        rstart = subprocess.run(cmd, shell=True,text=True, timeout = 10)
        sleep(1)
        cmd = "systemctl --no-pager status bluetooth"
        print("checking bluetooth")
        s = subprocess.run(cmd, shell=True, capture_output=True,encoding='utf-8',text=True, timeout=10)
        print(s, flush = True)



if __name__ == "__main__":
    NEED_RESTART = True
    while NEED_RESTART:
        NEED_RESTART = False
        blemgr = BLEManager()
        if not blemgr.check_locks(): 
            print(f"Cannort Start - a process is already running BLEConnect Router", flush = True)
            break
        blemgr.run()  #blocks until mainloop is quit (comes back here after quitBT)
        if NEED_RESTART:
            print(f"ble manager has exited with need restart = {NEED_RESTART}, current restart count: {restart_count}")
        restart_count += 1
        if restart_count >= 3:
            print(f"too many restart - exisitng with error", flush = True)
            #allow only two restart of bluetooth (from advertisement error: maximum exceeded)
            # in case we get one for failed app register and one for failed advert register
            #had this point NEED_RESTART may have been set - but only allow it if attempts are < 3
            sys.exit(1)
        if NEED_RESTART: 
            btRestart() #resatrts bluetooth, then while loop here restarts ble manager/mainloop
        else:
            print("normal exit\nBPA BLEConnectRouter says: So long and thanks for all the fish", flush = True)
            