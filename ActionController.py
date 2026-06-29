
"""
********* Do not remove or rename the class. ****************

current version: 1.1.0 - June 26, 2026

Action Controller:
    Works in tandem with the BLEControlsPi App running on a phone or tablet to send and receive control messages over bluetooth.

    Modify/add handlers to this class to manage the response to controls messages received over bluetooth 
        from the BLeControlsPi App running on a phone or tablet,
        and to send messages to display controls.

    It is a two step process:
        - First register a handler (callback) function for each control you have defined in your BLEControlsPi App on the phone/tablet.
        - Second define what the handler will do when it receives a value from the phone/tablet
        Note: There are also a few specific handlers provided in this class not tied to a specific control (see below)

 1. Register handlers:
    You are provided with a reference to the Registrar class (self._registrar) 
    See the current function registerHandlers() for examples of how to register handlers 
        for each control you have defined in your BLEControlsPi App.
    You must call the appropirate registration method for each control type, as shown in the function registerHandlers().
    For reference:
    Controls must register a callback handler using by the proper "type" of handler( string, bool, int, timestamp):
        - Button      -> register_event_handler
        - Input Text, Color, picker -> register_string_handler
        - Slider,Stepper  -> register_int_handler
        - Toggle      -> register_bool_handler
        - Date        -> register_string_handler
    Note: if you fail to use the correct type handler registration, ControlError is raised.  

    This example code ( registerHandlers() ) registers handlers for one of each type of control, and a timer handler.
    Add/remove handlers as needed to match the controls you have in your App.

Once your have registered callback handlers for each control, write the code that handles what to do 
    with the control's received value in the callback handler function you defined for that control.

If you need to send a value to a control on the phone/tablet, use self.com_manager and its send method.
    - this code provides a convenience function send(control_code, value) to send a value to a control on the phone/tablet.

Specialty convenience handlers you may use ("out of the box"):
    a) register_timer_handler: 
        - this sets a periodic timer, which calls the handler you register.
            in this example, we call on_timer() which sends a counter value to the phone/tablet.

    b) register_on_connected: 
        - run specific code when the phone/tablets connect bluetooth.
        - runs only once per connection 
            - useful for example to send initial values to controls on the phone/tablet when it connects to the raspberry pi.

    c) register_on_ble_quit:
        - ble will exit after a settable inactivity timeout, 
            which causes this channel process to quit as well.
        - run a synchronous callback, to perform cleanup before, this proces exits.
        note: in this example, we have implemented the registration of a callback self.on_shutdown,
              but the handler itself does nothing.

              
    Code Notes:
    - ActionController class is the entry point to the Controls Channel classes (libs/channels/controlsChannel.py)
    - it should be started via the service file: bpa-controls-channel.service (in /etc/systemd/system)
        - this was created automatically if you used the installer bpablecontrolsinstall-v1.x.y.sh, from the Github repo
        - the service requires another service to be running first: bpa-bleconnect-router.service (in /etc/systemd/system),
          and is setup to ensure that service starts first, and is running before this service starts.
    - If you are testing debugging, you can run this file directly from the command line (python3 ActionController.py),
        but you must ensure that the bpa-bleconnect-router.service is running first:
        start it with: sudo systemctl start bpa-bleconnect-router.service
        Note that it is not enabled to start automatically on boot, so you must start it manually if you are testing/debugging.
        To be clear: You do not need to start the bpa-controls-channel.service, if you are running the bpa-controls-channel.service.
"""

from libs.channels.controlsChannel import CtrlChannelMgr
from libs.channels.controlsHandlerRegistration import Registrar
from libs.bleconnectrouter.zmqlogger import ZmqLogClient as Log

class ActionController:

    def __init__(self):
        self._registrar = Registrar()
        self.com_manager = CtrlChannelMgr(self._registrar)
        self.registerHandlers()


    def registerHandlers(self):
        '''
        This is the part to modify:
        regiter handlers for each of your defined buttons in the BLEControlPi App on the phone/tablet, 
        as well as special events.
        Below is an example for each possible control and events: add/edit/remove per your needs

        there are two steps:
            1) register a handler here for each button (identified by its control code)
            2) below this function in the class: create a def for each handler you define here.
        '''
        # REgister callback handlers for each controls you defined nin the BLEControlsBT app
        self._registrar.register_event_handler     ("Button1",  self.on_button1)
        self._registrar.register_string_handler    ("Input1",   self.on_input1)
        self._registrar.register_int_handler       ("Slider1",  self.on_slider1)
        self._registrar.register_int_handler       ("Stepper1", self.on_stepper1)
        self._registrar.register_string_handler    ("Picker1",  self.on_picker1)
        self._registrar.register_bool_handler      ("On/Off1",  self.on_toggle1)
        self._registrar.register_date_time_handler ("Date1",    self.on_date1)
        self._registrar.register_string_handler    ("Color1",   self.on_color1)

        # below - register special events callsbacks
        """
            register a timer to periodically send data collected 
            from the raspberry pi via bluetooth 
                - comment it out or set it to 0 to prevent the timer from running
        """
        self._registrar.register_timer_handler(duration_seconds =  15, handler = self.on_timer)
        
        '''
        use this to register a callback handler to run when phone/tablet 
        establishes a bluetooth connection with the raspberry pi
        '''
        self._registrar.register_on_bluetooth_connection(self.on_bluetooth_has_connected)

        '''
        use this if you need to run clean up code before the procees exits
        which occurs after a default timeout = 30 minutes of inactivity (no bluetooth messages)
        '''
        self._registrar.register_shutdown_handler(self.on_shutdown)



    # Section: callback Handlers *****************************************************
    # Add / edit callbacks to define actions taken when controls values are received.

    # button callbacks do not receive a value you only get a callback that the button was pressed on the phone/tablet  
    def on_button1(self, name):
        self.log(f"Received button press: name={name}")

    #all others callback pass the value received from the phone/tablet over bluetooth
    def on_input1(self, name, value):
        self.log(f"Received text: name={name}; value={value}")

    def on_slider1(self, name, int_value):
        self.log(f"Received slider update: name={name}; value={int_value}")

    def on_stepper1(self, name, int_value):
        self.log(f"Received stepper update: name={name}; value={int_value}")

    def on_picker1(self, name, choice):
        self.log(f"Received picker update: name={name}; value={choice}")

    def on_toggle1(self, name, is_on):
        self.log(f"Received toggle update: name={name}; is_on={is_on}")

    def on_date1(self, name, date_time):
        self.log(f"Received date/time update: name={name}; value={date_time}")

    def on_color1(self, name, hex_rgb):
        self.log(f"Received color update: name={name}; color={hex_rgb}")

    # section Send info to Raspberry Pi

    #callback for timer - send info on a regular schedule, set it with register_timer_handler 
    def on_timer(self):
        #This timer examples sends a counter value (loops up to 10) to control code: Text1, 
        # and sends the current cpu temp to control code "Gauge1", on the phone/tablet.  

        #part 1: counter value management and sending
        self.log(f"Received timer_update")
        #this initializes self.counter on first call - if not done in __init__:
        cnt = getattr(self, "counter", None)
        if cnt is None:
            self.counter = 0

        self.counter = self.counter + 1
        if self.counter > 10:
            self.counter = 1
        msg = f"Counter = {self.counter}"

        # this sends the msg (counter value) to the control with code "Text1" on the phone/tablet
        self.send("Text1", msg)

        # part #2: send the current cpu temperature to the control with code "Gauge1" on the phone/tablet
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_c = int(f.read()) / 1000.0
            self.send("Gauge1",temp_c)


    def on_bluetooth_has_connected(self):
        '''
        typically use this to send initial values to controls that need to sync their state 
            with the current state of the Raspberry Pi
            In this example, we send the cpu temperature to control Gauge1 
            note: remove this if you are using Gauge1 control for something else
        '''
         #set the value of desired controls
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_c = int(f.read()) / 1000.0
            self.send("Gauge1",temp_c)

    #Section start and shutdown

    def start(self):
        self.com_manager.run()

    def on_shutdown(self):
        '''
            this is called if you set up register_shutdown_handler
            insert cleanup code before this procees ends
            Note:
                the handler is called if the inactivity timeout (default 30 minutes)
                occurs. 
                At that point the BLEConnectRouter exists, and the Channel Handler code (this)
                also exits.
                The timeout is set in the  bpa-controls-channel.service file (/etc/systemd/system)
        '''
        pass


    # utility send: 
    # #use this from anywhere in your code to send a value to a controls on the phone (via bluetooth)
    def send(self,control_code,value):
        self.com_manager.send(control_code,value)
       


    def on_error(self, message, name, value):
        self.log(f"Error: {message}: name={name}; value={value}")

    def log(self, message):
        Log.log(message)



if __name__ == "__main__":
    #Change log level here (INFO, CRITICAL, NEVER) as desired to get less log output
    Log.initialize(log_level="DEBUG",process_label="ControlsChannel")
    print(f"Starting Controls Action Controller with log level: {Log.logLevel()}",flush = True)
    action_controller = ActionController()
    action_controller.start()
    print("Action Controller has terminated",flush = True)