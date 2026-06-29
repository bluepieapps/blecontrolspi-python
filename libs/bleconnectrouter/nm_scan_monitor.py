import dbus
from libs.bleconnectrouter.zmqlogger import ZmqLogClient as Log


NM_SERVICE = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"

NM_IFACE = "org.freedesktop.NetworkManager"
NM_DEVICE_IFACE = "org.freedesktop.NetworkManager.Device"
NM_WIFI_IFACE = "org.freedesktop.NetworkManager.Device.Wireless"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"


class NetworkManagerScanMonitor:
    """
    Watches NetworkManager Wi-Fi scan completion by listening for LastScan
    changes on org.freedesktop.NetworkManager.Device.Wireless.

    The owning process must already have dbus.mainloop.glib.DBusGMainLoop set
    as the default and must be running a GLib mainloop.
    """

    def __init__(self, on_scan_ready, interface_name: str = "wlan0"):
        self.on_scan_ready = on_scan_ready
        self.interface_name = interface_name
        self.bus = dbus.SystemBus()
        self.dev_path = self._find_device_path(interface_name)
        self._signal_match = None
        self._last_scan = None

    def _find_device_path(self, interface_name: str) -> str:
        nm_obj = self.bus.get_object(NM_SERVICE, NM_PATH)
        nm = dbus.Interface(nm_obj, NM_IFACE)

        for dev_path in nm.GetDevices():
            dev_obj = self.bus.get_object(NM_SERVICE, dev_path)
            props = dbus.Interface(dev_obj, DBUS_PROPS_IFACE)
            iface = props.Get(NM_DEVICE_IFACE, "Interface")

            if str(iface) == interface_name:
                return str(dev_path)

        raise RuntimeError(
            f"NetworkManager device path not found for {interface_name}"
        )

    def start(self):
        if self._signal_match is not None:
            return

        self._signal_match = self.bus.add_signal_receiver(
            self._on_properties_changed,
            dbus_interface=DBUS_PROPS_IFACE,
            signal_name="PropertiesChanged",
            path=self.dev_path,
            arg0=NM_WIFI_IFACE,
        )
        Log.log(
            f"NetworkManager scan monitor started for "
            f"{self.interface_name} at {self.dev_path}"
        )

    def stop(self):
        if self._signal_match is None:
            return

        try:
            self._signal_match.remove()
        except Exception as err:
            Log.log(f"NetworkManager scan monitor stop failed: {err}",
                    level=Log.CRITICAL)
        finally:
            self._signal_match = None

    def _on_properties_changed(self, interface_name, changed, invalidated):
        if str(interface_name) != NM_WIFI_IFACE:
            return
        if "LastScan" not in changed:
            return

        last_scan = int(changed["LastScan"])
        if last_scan < 0:
            return
        if self._last_scan == last_scan:
            return

        self._last_scan = last_scan
        Log.log(f"NetworkManager scan complete detected: LastScan={last_scan}")
        self.on_scan_ready()
