import sys

import signal
from libs.bleconnectrouter.zmqlogger import ZmqLogClient as Log #zmq based logger (quick fire and forget)
import subprocess
from time import sleep
from libs.bleconnectrouter.bpaconfig import ZMQ_SYSTEM_TOPIC
from  libs.channels.subscriber_processes import BPAMessageQueueClient,ContextTerminated,BPAError

'''
This files contains the necessary import and the class CtrlChannelMgr which implements
the necessary functions to connect to the BPA BLEConnect Router, communicating over ZMQ,
to receive and send bluetooth message to a phone/tablet with the BLEControls app installed.
(Note: you should not need to modify CtrlChannel)
It also implements the ControlsActions class 
    This is user define: This is where you define the actions that occur when
    the phone.tablet sends a controls command to this Rasperry Pi.
'''



#normally, do not modify code for this class.
# only modify code in the ControlsAction class (actions.py file)
class CtrlChannelMgr:
    MAX_LOG_PAYLOAD_PREVIEW = 160

    def __init__(self,registrar):
        self.channel = BPAMessageQueueClient("bpactrl") #the logical id for this channel
        self.keep_listening = False
        self.registrar = registrar
        Log.log("channel with id: bpactrl is initialized")
    
    def run(self):
        self.keep_listening = True
        signal.signal(signal.SIGTERM, self._on_sigterm)
        signal.signal(signal.SIGINT, self._on_sigterm) #(for control-C in testing)
        Log.log(f"entering listening loop with keep_Listening = {self.keep_listening}")
        while self.keep_listening:
            try:
                _,msg_bytes = self.channel.receive(100) #100 ms timeout
                if msg_bytes is not None:
                    msg_str = msg_bytes.decode("utf-8", errors="replace")
                    # print(msg_str,flush =True)
                    ctl_code, _, value_str = msg_str.partition("\x1e")
                    #ctrl code = 1f is a reserved code sent by the BLEControlPi app 
                    # with payload "READY"  to indicated tgat bluetooth has connected successfuly.
                    if ctl_code == '\x1f' and value_str == "READY":
                        self.registrar.on_bluetooth_connected()
                    else: #normal path - send incoming message to registrar
                        self.registrar.incoming_message_handler(ctl_code,value_str)
                else:
                    if self.channel.ble_router_timed_out:
                        self.registrar.on_ble_timeout()
                        Log.log("received system topic: InactivityTimedOut",
                    level=Log.INFO)
                        break
            except ContextTerminated as ect:
                 Log.log(f"Exiting WifiChannel process due to exception: {ect}",
                    level=Log.CRITICAL)
                 break
            except BPAError as ebpa:
                 Log.log(f"Exiting WifiChannel process due to exception: {ebpa}",
                    level=Log.CRITICAL)
                 break
            except Exception as e:
                Log.log(f"Exiting WifiChannel process due to exception: {e}",
                    level=Log.CRITICAL)
                raise

        #on exiting loop - close
        Log.log(f"exited loop with keep_Listening = {self.keep_listening}")
        self._close()

    def _close(self):
        Log.log("closing channel",
                    level=Log.INFO)
        Log.close()
        self.channel.close()
        self.channel = None

    def send(self, ctl_code,payload):
        #construct the message to send
        # by convention with BLEControls APP separate control_code and value with separator ASCII: 1E
        p_str = str(payload)
        msg_to_send = f"{ctl_code}\x1e{p_str}"
        Log.log(f"sending control code: {ctl_code}, value: {p_str}")
        try:
            self.channel.send(msg_to_send)
        except BPAError as err:
            self.onSendMessageError(err, msg_to_send)

    def onSendMessageError(self, err, payload):
        preview, payload_len = self._payload_preview(payload)
        Log.log(
            f"CRITICAL send error : {err}; payload_len={payload_len}; "
            f"payload_preview={preview}",
                    level=Log.INFO
        )

    def _payload_preview(self, payload):
        if isinstance(payload, bytes):
            payload_len = len(payload)
            preview = payload[:self.MAX_LOG_PAYLOAD_PREVIEW].decode("utf-8", errors="replace")
        else:
            preview = str(payload)
            payload_len = len(preview)
            preview = preview[:self.MAX_LOG_PAYLOAD_PREVIEW]
        return preview, payload_len

    
    def _on_sigterm(self, signum, frame):
        #set keep listening to false will exit the loop at the next 100 ms time out and close the channel
        self.keep_listening = False

    
