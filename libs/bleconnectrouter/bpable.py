import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
from libs.bleconnectrouter.zmqlogger import ZmqLogClient as Log
from libs.bleconnectrouter.bpautils import dbus_to_python

class Blue:
    adapter_name = ''
    bus = None
    adapter_obj = None
    counter = 1

    @staticmethod
    def set_adapter():
        try:
            found_flag = False
            Blue.bus = dbus.SystemBus()
            obj = Blue.bus.get_object('org.bluez','/')
            obj_interface=dbus.Interface(obj,'org.freedesktop.DBus.ObjectManager')
            all = obj_interface.GetManagedObjects()
            for item in all.items(): #this gives a list of all bluez objects
                Log.log(f"BlueZ Adapter name: {item[0]}")
                # Log.log(f"BlueZ Adapter data: {item[1]}\n")
                Log.log("******************************\n")
                if  (item[0] == '/org/bluez/hci0') or ('org.bluez.LEAdvertisingManager1' in item[1].keys() and 'org.bluez.GattManager1' in item[1].keys() ):
                    #this the bluez adapter1 object that we need
                    Log.log(f"Found BlueZ Adapter name: {item[0]}\n")
                    Log.log(f"Interfaces on chosen object: {list(item[1].keys())}")
                    found_flag = True
                    Blue.adapter_name = item[0]
                    Blue.adapter_obj = Blue.bus.get_object('org.bluez',Blue.adapter_name)
                    #turn_on the adapter - to make sure (on rpi it may already be turned on)
                    props = dbus.Interface(Blue.adapter_obj,'org.freedesktop.DBus.Properties')

                    props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(1))
                    props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(0))
                    props.Set("org.bluez.Adapter1", "PairableTimeout", dbus.UInt32(0))
                    props.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(1))
                    props.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(0))

                    break
            if not found_flag:
                Log.log("No suitable Bluetooth adapter found")
                #raise Exception("No suitable Bluetooth adapter found")
            
        except dbus.exceptions.DBusException as e:
            Log.log(f"DBus error in set_adapter: {str(e)}")
            raise
        except Exception as e:
            Log.log(f"Error in set_adapter: {str(e)}")
            raise


    @staticmethod
    def adv_mgr(): 
        dI = dbus.Interface(Blue.adapter_obj,'org.bluez.LEAdvertisingManager1')
        Log.log(f"dbus Interface - advertizing mgr: {dI}")
        return dbus.Interface(Blue.adapter_obj,'org.bluez.LEAdvertisingManager1')

    @staticmethod
    def gatt_mgr():
        if Blue.adapter_obj is None:
            Log.log("Blue.adapter_obj is None")
        else:
            Log.log(f"adapter oject: {Blue.adapter_obj}")
        dI = dbus.Interface(Blue.adapter_obj,'org.bluez.GattManager1')
        Log.log(f"dbus Interface Gatt mgr : {dI}")
        return dI

    @staticmethod
    def properties_changed(interface, changed, invalidated, path):
        """
        This can be used to detect connection changes from a central device (phone/tablet)
        it is brittle, it often misses connection:true.
        it usually gets connection:false - when device disconnects.
        A better way to detect connections is with characteristic wifi:
            the Android/IOS code will register for notifications upon connection,
            so method StartNotify is called (generic connection actions could be placed there)
            Similarly, before disconnecting, StopNotify is called.
            However, if device has crashed or otherwise lost connection without being programmed to do so,
            StopNotify would be called.  
            In that case, it is possibled - but not garanteed (brittle) - that this will be triggered with
            connection:false - so code that needs to run upon disconnection could be placed here.
        """
        if interface != "org.bluez.Device1":
            return
        Log.log(f"\ncounter={Blue.counter}",level=Log.INFO)
        Log.log(f"path:{path} \n changed:{changed}\n ",
                level=Log.INFO)
        Blue.counter+=1
        try: 
            pythonDict =  dbus_to_python(changed)
            Log.log(f"pythonDict: {pythonDict}")
        except:
            pass
        

class Advertise(dbus.service.Object):

    def __init__(self, index,bleMgr,target_service_uuid):
        self.bleMgr = bleMgr
        self.hostname = bleMgr.pi_hostname 
        self.properties = dict()
        self.properties["Type"] = dbus.String("peripheral")
        self.properties["ServiceUUIDs"] = dbus.Array([target_service_uuid],signature='s')
        self.properties["IncludeTxPower"] = dbus.Boolean(True)
        self.properties["LocalName"] = dbus.String(self.hostname)
        self.properties["Flags"] = dbus.Byte(0x06) 

        #flags: 0x02: "LE General Discoverable Mode"
        #       0x04: "BR/EDR Not Supported"
        self.path = "/org/bluez/advertise" + str(index)
        dbus.service.Object.__init__(self, Blue.bus, self.path)
        self.ad_manager = Blue.adv_mgr() 
        self.registered = False


    def get_properties(self):
        return {"org.bluez.LEAdvertisement1": self.properties}

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return self.get_properties()["org.bluez.LEAdvertisement1"]

    @dbus.service.method("org.bluez.LEAdvertisement1", in_signature='', out_signature='')
    def Release(self):
        Log.log('%s: Released!' % self.path)

    def register_ad_callback(self):
        Log.log("GATT advertisement registered")
        self.registered = True

    def register_ad_error_callback(self,error):
        #Failed to register advertisement: org.bluez.Error.NotPermitted: Maximum advertisements reached
        #now calling for restart if any error occurs here
        global NEED_RESTART
        try:
            NEED_RESTART = True
            errorStr = f"{error}"
            if "Maximum" in errorStr:
                Log.log("advertisement Maximum error - calling for bluetooth service restart ")
            else:
                Log.log("advertisement registration error - other than maximum advertisement - call for restart")
        except:
            pass
        Log.log(f"NEED_RESTART is set to {NEED_RESTART}")
        Log.log(f"Failed to register GATT advertisement {error}")
        Log.log("calling quitBT()")
        self.bleMgr.quitBT()


    def register(self):
        Log.log("Registering advertisement")
        if self.ad_manager is None:
            Log.log("ad_manager is None")
            self.registered = False
            self.register_ad_error_callback("")
        else:
            self.ad_manager.RegisterAdvertisement(self.get_path(), {},
                                        reply_handler=self.register_ad_callback,
                                        error_handler=self.register_ad_error_callback)
            self.registered = True
        
        
    def unregister(self):
        Log.log(f"De-Registering advertisement - path: {self.get_path()}")
        self.ad_manager.UnregisterAdvertisement(self.get_path())
        try:
            dbus.service.Object.remove_from_connection(self)
        except Exception as ex:
            Log.log(f"{ex}")
    


class Application(dbus.service.Object):
    def __init__(self,bleMgr):
        self.bleMgr = bleMgr
        self.path = "/"
        self.services = []
        self.next_index = 0
        self.registered = False
        dbus.service.Object.__init__(self, Blue.bus, self.path)
        self.service_manager = Blue.gatt_mgr()

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        self.services.append(service)


    @dbus.service.method("org.freedesktop.DBus.ObjectManager", out_signature = "a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            chrcs = service.get_characteristics()
            for chrc in chrcs:
                response[chrc.get_path()] = chrc.get_properties()
                descs = chrc.get_descriptors()
                for desc in descs:
                    response[desc.get_path()] = desc.get_properties()
        return response

    def register_app_callback(self):
        Log.log("GATT application registered")
        self.registered = True


    def register_app_error_callback(self, error):
        #failing to register will call for restart 
        global NEED_RESTART
        NEED_RESTART = True
        Log.log("Failed to register application: " + str(error),
                    level=Log.INFO)
        Log.log(f"app registration handler has set NEED_RESTART to {NEED_RESTART}")
        Log.log("calling quitBT()")
        self.bleMgr.quitBT()
       
    def register(self):
        #adapter = BleTools.find_adapter(self.bus)
        #service_manager = dbus.Interface(self.bus.get_object(BLUEZ_SERVICE_NAME, adapter),GATT_MANAGER_IFACE)
        if self.service_manager is None:
            Log.log("service_manager is None")
            self.registered =False
            self.register_app_error_callback("")
        else:
            Log.log(f"service_manager: {self.service_manager}")
            self.service_manager.RegisterApplication(self.get_path(), {},
                    reply_handler=self.register_app_callback,
                    error_handler=self.register_app_error_callback)
            #callbacks not called until mainloop starts
            #if we get here, service_manager is not none - if error callback - it will stop main loop
            self.registered = True
        
    def unregister(self):
        Log.log(f"De-Registering Application - path: {self.get_path()}")
        try:
            for service in self.services:
                service.close() #this closes crypto thread and zmq sockets
                service.deinit()
        except Exception as exs:
            Log.log(f"exception trying to deinit service",
                    level=Log.CRITICAL)
            Log.log(f"{exs}",
                    level=Log.CRITICAL)
        try:
            self.service_manager.UnregisterApplication(self.get_path())
        except Exception as exa:
            Log.log(f"exception trying to unregister Application",
                    level=Log.CRITICAL)
            Log.log(f"{exa}",
                    level=Log.CRITICAL)
        try:
            dbus.service.Object.remove_from_connection(self)
        except Exception as exrc:
            Log.log(f"dbus exception trying to remove object from connection",
                    level=Log.CRITICAL)
            Log.log(f"{exrc}",
                    level=Log.CRITICAL)
        

class Service(dbus.service.Object):
    #PATH_BASE = "/org/bluez/example/service"
    PATH_BASE = "/org/bluez/service"

    def __init__(self, index, uuid, primary):
        self.path = self.PATH_BASE + str(index)
        self.uuid = uuid
        self.primary = primary
        self.characteristics = []
        dbus.service.Object.__init__(self, Blue.bus, self.path)

    def deinit(self):
        Log.log(f"De-init Service  - path: {self.path}")
        for characteristic in self.characteristics:
            characteristic.deinit()
        try:
            dbus.service.Object.remove_from_connection(self)
        except Exception as ex:
            Log.log(f"{ex}",
                    level=Log.CRITICAL)

    def get_properties(self):
        return {
                "org.bluez.GattService1": {
                        'UUID': self.uuid,
                        'Primary': self.primary,
                        'Characteristics': dbus.Array(
                                self.get_characteristic_paths(),
                                signature='o'),
                        'Secure': dbus.Array([], signature='s')  # Empty array means no security required
                }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic):
        self.characteristics.append(characteristic)

    def get_characteristic_paths(self):
        result = []
        for characteristic in self.characteristics:
            result.append(characteristic.get_path())
        return result

    def get_characteristics(self):
        return self.characteristics

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return self.get_properties()["org.bluez.GattService1"]

class Characteristic(dbus.service.Object):

    def __init__(self, index, uuid, flags, service):
        self.path = service.path + '/char' + str(index)
        self.uuid = uuid
        self.service = service
        self.flags = flags
        self.descriptors = []
        dbus.service.Object.__init__(self, Blue.bus, self.path)

    def deinit(self):
        Log.log(f"De-init Characteristic  - path: {self.path}")
        for descriptor in self.descriptors:
            descriptor.deinit()
        try:
            dbus.service.Object.remove_from_connection(self)
        except Exception as ex:
            Log.log(f"{ex}",
                    level=Log.CRITICAL)

    def get_properties(self):
        return {
                "org.bluez.GattCharacteristic1": {
                        'Service': self.service.get_path(),
                        'UUID': self.uuid,
                        'Flags': self.flags,
                        'Descriptors': dbus.Array(
                                self.get_descriptor_paths(),
                                signature='o'),
                        'RequireAuthentication': dbus.Boolean(False),
                        'RequireAuthorization': dbus.Boolean(False),
                        'RequireEncryption': dbus.Boolean(False),
                }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_descriptor(self, descriptor):
        self.descriptors.append(descriptor)

    def get_descriptor_paths(self):
        result = []
        for desc in self.descriptors:
            result.append(desc.get_path())
        return result

    def get_descriptors(self):
        return self.descriptors

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return self.get_properties()["org.bluez.GattCharacteristic1"]

    @dbus.service.method("org.bluez.GattCharacteristic1", in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        Log.log('Default ReadValue called, returning error')

    @dbus.service.method("org.bluez.GattCharacteristic1", in_signature='aya{sv}')
    def WriteValue(self, value, options):
        Log.log('Default WriteValue called, returning error')

    @dbus.service.method("org.bluez.GattCharacteristic1")
    def StartNotify(self):
        Log.log('Default StartNotify called, returning error')

    @dbus.service.method("org.bluez.GattCharacteristic1")
    def StopNotify(self):
        Log.log('Default StopNotify called, returning error')

    @dbus.service.signal("org.freedesktop.DBus.Properties", signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    def add_timeout(self, timeout, callback):
        GLib.timeout_add(timeout, callback)

class Descriptor(dbus.service.Object):
    def __init__(self, index,uuid, flags, characteristic):
        self.path = characteristic.path + '/desc' + str(index)
        self.uuid = uuid
        self.flags = flags
        self.chrc = characteristic
        dbus.service.Object.__init__(self, Blue.bus, self.path)

    def deinit(self):
        Log.log(f"De-init Descriptor  - path: {self.path}")
        try:
            dbus.service.Object.remove_from_connection(self)
        except Exception as ex:
            Log.log(f"{ex}")

    def get_properties(self):
        return {
                "org.bluez.GattDescriptor1": {
                        'Characteristic': self.chrc.get_path(),
                        'UUID': self.uuid,
                        'Flags': self.flags,
                        'Secure': dbus.Array([], signature='s') 
                }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return self.get_properties()["org.bluez.GattDescriptor1"]

    @dbus.service.method("org.bluez.GattDescriptor1", in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        Log.log('Default ReadValue called, returning error')

    @dbus.service.method("org.bluez.GattDescriptor1", in_signature='aya{sv}')
    def WriteValue(self, value, options):
        Log.log('Default WriteValue called, returning error')

