

from datetime import datetime
from time import sleep
import threading
import inspect
from libs.bleconnectrouter.zmqlogger import ZmqLogClient as Log

"""
Instructions:
The ControlsActions class is where you decide what happens on the Raspberry Pi when various controls
are emitted from the phone/tablet (BLEControlsPi App).

IMPORTANT:  write down the control "Code" which you have defined for each of your control.
            This is the ctl_code on which you trigger the elif below to take appropirate action

incoming_message_handler():  
    This where you define what Raspberry Pi action to take when a control is actuated on the phone App,
    and sent here via blutooth

controls_update_on_connection_made(): (optional)
    If you have controls that needs to be initialized once the bluetooth connection is establish,
    define their initial value here.
    Note:   this is only called when the bluetooth connection is established 
            (or re-established after disconnection)

controls_update_on_timer(): (optional)
    The ControlsAction class runs an optional timer (settable in initialize method).
    When this timer fires it calls self. controls_update_on_timer().
    Place any regular updates for controls (typically Gauge or TextDisplay) you wish
    to update regularly.
    IMPORTANT: The timer starts running only after the bluetooth connection is established

initialize)():
    Set timer value in seconds here.  (set to None if you do not use the timer feature)
    add any other initialization needed here as well.

on_ble_timeout():
    The main BLEConnectRouter timer has a inactivity timeout (defaults to 30 min),
    after which it shuts down.  
    When ControlChannel is running as a service (default installation), the service should also be shut down.
    This is not enforced via SystemD: instead BLEConnectRouter boradcasts a shutdown messaged, received here.
    This method allows you to run shutdown code before shutting down the ControlsChannels service
*******************************************************

Controls information:
Some controls can be initialized (i.e. they can receive data and update themselves in the phone BLEControlsPi App)

Display Controls:
    - text:  allows you to send a string that is displayed on the phone
    - gauge: allows you to send a value of type Double (float or Int will work - send as a string)
        
Initializable Command types: (active controls that send their values here)
    IMPORTANT: all values are sent as string, and converted to proper type in the phone App.
    - Color Picker : expects a RGB String in the form "#FFFFFF" representing the RGB hexadecinal value of the color
    - Date Picker : expects a string version of a float representing the seconds as timeIntervalSince1970
                    (use selectedDate.timeIntervalSince1970)
    - Picker : expect a string matching one of the options you created in the BLEControls app
    - Slider: expects the string version of an Int between the min/max you set in the app
    - Stepper: expects the string version of an Int between the min/max you set in the app
    - Toggle: expects the string representation of a boolean (str(is_on) : where is_on is either True or False)
                it also accepts a string value of Int 1 or 0.

NonInitalizable Command Types:
    This control will not update their state in the phone App - even if you send an update to them
    - Button

"""


class ControlError(Exception):

    def __init__(self, control_name, message):
        self.control_name = control_name
        self.message = message
        super().__init__(f"{control_name}: {message}")



class Registrar:

    _VALUE_TYPES = {"none", "text", "int", "bool", "timestamp"}

    def __init__(self):
        self._control_handlers = {}
        self._on_connected_handler = None
        self._on_timer_handler = None
        self. on_shutdown_handler = None
        self.timer = RepeatingTimer(seconds = 0, callback = self.controls_update_on_timer )


        # Public methods to register control handlers, send updates to the phone App, and set timer.  
    # These are called from the ActionController

    # Message handlers - One for each control type you want to support.  Edit/remove as needed.
    def register_event_handler(self, control_name, handler):
        self._register_control_handler(control_name, "none", handler)

    def register_string_handler(self, control_name, handler):
        self._register_control_handler(control_name, "text", handler)

    def register_int_handler(self, control_name, handler):
        self._register_control_handler(control_name, "int", handler)

    def register_bool_handler(self, control_name, handler):
        self._register_control_handler(control_name, "bool", handler)

    def register_date_time_handler(self, control_name, handler):
        self._register_control_handler(control_name, "timestamp", handler)


    # --- Private methods to process incoming messages and convert values, etc---
    def incoming_message_handler(self,ctl_code,value_str):
        """
        the phone App always send a two parts message over Bluetooth:
            - ctl_code is the code you have defined in the BLEControls app, for each control.
            - value_str is the value of the control sent via bluetooth
                * it always arrive as a string. if you expect a number/date etc.  - convert it.

        Examples for each command (control) type that can be defined in theBLEControls App.
        Edit/remove as needed.
        Just add more elif to match the code of each control you defined in the app.

        Types:
        - Button(control-name)
        - SendText(control-name, text-string)
        - Slider(control-name, value-str) - Value must convert to integer
        - Stepper(contro-name, value-str) - Value must convert to integer
        - Picker(control-name, text-str)
        - OnOffSwitch(control-name, value-str) - Either "true" or "false"
        - Date(control-name, value-str) - Value must be 'YYYYmmdd HH:MM:SS'
        - Color(control-name, value-str) - Value must be HEX-RGB - For example '#FF00CC'
        """

        Log.log(f"Received NAME={ctl_code}; VALUE={value_str}")
        entry = self._control_handlers.get(ctl_code)
        if entry is None:
            Log.log(f"Received unregistered control message for {ctl_code}, value: {value_str}",
                    level=Log.INFO)
            return

        value_type, handler, takes_value = entry

        try:
            typed_value = self._convert_value(ctl_code, value_type, value_str)
            if takes_value:
                handler(ctl_code, typed_value)
            else:
                handler(ctl_code)
        except ValueError as e:
            Log.log(f" wrong value type: {value_type}  was registered incorrectly for control: {ctl_code}",
                    level=Log.CRITICAL)
        except Exception as e:
            Log.log(f"Error processing control message: {e}",
                    level=Log.CRITICAL)

    def _register_control_handler(self, control_name, value_type, handler):
        if value_type not in self._VALUE_TYPES:
            raise ValueError(f"Unsupported value type: {value_type}")

        param_count = len(inspect.signature(handler).parameters)
        if param_count not in (1, 2):
            raise ValueError("Handler must accept (name) or (name, value).")

        takes_value = param_count == 2
        self._control_handlers[control_name] = (value_type, handler, takes_value)

    def _convert_value(self, ctl_code, value_type, value_str):
        if value_type == "none":
            return None

        if value_type == "text":
            return value_str

        if value_type == "int":
            try:
                return int(value_str)
            except ValueError:
                raise ValueError(f"Control {ctl_code} sent non integer value {value_str}.")

        if value_type == "bool":
            normalized = value_str.lower()
            if normalized in ("true", "1"):
                return True
            if normalized in ("false", "0"):
                return False
            raise ValueError(f"Control {ctl_code} sent invalid toggle value {value_str}.")

        if value_type == "timestamp":
            try:
                timestamp = float(value_str)
                return datetime.fromtimestamp(timestamp)
            except ValueError:
                raise ValueError(f"Control {ctl_code} sent invalid date/time value {value_str}.")

        raise ValueError(f"Unhandled value type: {value_type}")
    

    def register_on_bluetooth_connection(self, handler):
        self._on_connected_handler = handler

    def on_bluetooth_connected(self):
        """
        This function is called automatically once, when the BLEControls app has 
        successfully connected (or re-connected) to bluetooth.
        This is where the timer should be started.
        Also send controls initial value form here when needed:
         this example sends CPU temperature to a display Gauge control with code = Gauge1
        """
        Log.log("bluetooth connection was established",
                    level=Log.INFO)
        #start timer for regular updates (does nothing if not set via register)
        self.timer.start()
        if self._on_connected_handler:
            self._on_connected_handler()

       


    def register_timer_handler(self, duration_seconds, handler):
        # Update the repeating timer interval in seconds.
        if duration_seconds is None or duration_seconds < 0:
            self.timer.set_seconds(0)
            return
        
        self.timer.set_seconds(duration_seconds)
        self._on_timer_handler = handler

    def controls_update_on_timer(self):
        """
        if timer is not None, this is called every time the timer fires,
            starting after the bluetooth connection is established
        Place updates to control you want to send regularly (based on timer set initialize)
        this examples sends:
            - memory usage is send to a DisplayText control:  code = Text1
            - cpu temp is sent to a Gauge control: code = Gauge1
            (Note: implement these controls in your app to see the example in action)
        """
        try:
            if self._on_timer_handler:
                self._on_timer_handler()
        except ValueError as e:
            Log.log(f"Exception in on_timer: {e}",
                    level=Log.CRITICAL)



    
    def register_shutdown_handler(self,handler):
        self.on_shutdown_handler = handler   

    def on_ble_timeout(self):
        """
        BLEConnectRouter has shutdown - the CtrlChannelMgr should shutdown.
        Insert anu cleanup code needed before calling close.
        """
        if self.on_shutdown_handler:
            self.on_shutdown_handler()
        Log.log("ble router has shut down due to inactivity time out - exiting...",
                    level=Log.INFO)
        #IMPORTANT - leave this ine here
        self.timer.stop()


class RepeatingTimer:
    def __init__(self, seconds=None, callback=None):
        self.seconds = seconds
        self.callback = callback
        self._timer = None

    def start(self):
        Log.log(f"timer start is called")
        self.stop()
        if self.seconds is None or self.seconds == 0:
            Log.log(f"seconds is zero or None - ignoring... ")
            return
        self._schedule_next()

    def stop(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def set_seconds(self, seconds):
        self.stop()
        self.seconds = seconds
        self.start()

    def _schedule_next(self):
        if self.seconds is None or self.seconds == 0:
            return
        try:
            self._timer = threading.Timer(self.seconds, self._run)
            self._timer.daemon = True
            self._timer.start()
        except Exception as e:
             Log.log(f"RepeatingTimer schedule next: {e}",
                    level=Log.CRITICAL)
        

    def _run(self):
        self._timer = None
        try:
            self.callback()
        except Exception as e:
             Log.log(f"RepeatingTimer callback error: {e}",
                    level=Log.CRITICAL)
        self._schedule_next()


