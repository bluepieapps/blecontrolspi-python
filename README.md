# blecontrolspi-python

This repo contains the Python Code that **must** be installed on the Raspberry Pi or compatible linux box in order to run the BLEControlsPi App.

## The BLEControlsPi App

The BLEControlsPi App allows you to remotely control the Raspberry Pi from your iPhone/iPad via bluetooth.

With BLEControlsPi app,  you create the controls, such as buttons, sliders, steppers, pickers, and text input on your iPhone/iPad.
When you manipulate the controls, their value is sent to the Raspberry Pi in real time using bluetooth (BLE) - no wifi connection needed.

You can also build a dashboard, with display controls such as Gauges and text displays. The Raspberry Pi updates the iPhone/iPad in real time as well.

On the Raspberry Pi, after installing the Python Code provided here, you create and register handlers for each control defined in your BLEControlsPi app. These handlers dictate what happens on the Raspberry Pi when you use the controls on the iPhone/iPad.

### Documentation

App usage and information on Python Code Handlers can be found on 
[bluepieapps.com](https://bluepieapps.com/Control-Pi-over-bluetooth/)

How to - videos: *(opens on You Tube)*

- <a href="https://youtu.be/8y3t2luLw3k" target="_blank">IOS App (Apple) video</a>
- <a href="https://youtu.be/Zh7frO5oyYQ" target="_blank">Android App video</a>

### App Stores

The BLEControlsPi App is available: 

- For iOS: on the <a href="https://apps.apple.com/be/app/blecontrolspi/id6774251191"  target="_blank">Apple App Store</a> for iPhone and iPad. 
- For Android: on the <a href="https://play.google.com/store/apps/details?id=com.bluepieapps.blecontrolspi"  target="_blank">Google Play Store</a> for phones and tablets.     

## License - no Warranty

This code is provided AS IS (no warranty - see LICENSE).  It is licensed under the **MIT License** which is provided in this repo. 

## How to Install The Python Code on the Raspberry Pi:

An automated bash script is provided that downloads the python code, and installs the necessary dependencies on the Raspberry Pi.

Run the following in terminal on the Raspberry Pi (*most people use SSH for Headless Raspberry Pi*).

First, ensure that your RPi is up to date by running these commands:
```
sudo apt update
sudo apt upgrade --yes
sudo reboot
```

Then, run the installer script with this curl command, to set up btwifiset on your Pi:
```
curl -fL https://github.com/bluepieapps/blecontrolspi-python/releases/download/v1.1.0/bpablecontrolsinstall-v1.1.0.sh | bash
```

### Installer will ask you:

Where you want to install the python code: the default is `/usr/local/bluepieapps`
>it is recommended to just accept the default

Set the inactivity timeout: the default is 30 minutes
>The code is run as a SystemD service. If no data is exchanged between the iPhone/iPad and the Raspberry Pi,
the service will shutdown after this inactivity timeout.  It is recommended to shut down bluetooth BLE advertising when you do not need the code runnning.

Set an encryption password/key: the default is the current Raspberry Pi hostname.
>controls values are encrypted using this key that is shared between the BLEControlsPi App on the iPhone/iPad and the Raspberry Pi.
This is to prevent a third party from downloading the BLEControlsPi app and controling your Raspberry Pi.  It is recommended that you set something different then the hostname since the hostname is displayed in the app.  
>Note: a simple utility is provided (btpassword.py) which allows you to change the password on the Raspberry Pi after installation.

Enable the service to start on boot:
> if you say yes to this, the service that runs the python code will start automatically after the Raspberry Pi boots up (see next section)

## How to run the code after installation:

The installer creates a service named `bpa-controls-channel.service` located at /etc/systemd/system/

If you selected to enable start on boot during the installation process, this service starts on every boot of the Raspberry Pi.
> it is recommended to let the system start on boot. It will shutdown automatically after 30 minutes (or what ever timeout you selected) if you do not use it.

If you typed "N" (no), you will need to start the service manually on the Raspberry Pi *before* you can use the BLEControlsApp on the iPhone/iPad.
>note that even if you typed Y (yes) to start on boot - you may still need to restart the service if you allowed it to shut down due to inactivity.

To start the service manually - run: 
```
sudo systemctl start bpa-controls-channel
```

To stop the service - run: 
```
sudo systemctl stop bpa-controls-channel
```

To see if the services are running - run :
```
systemctl status bpa-controls-channel
systemctl status bpa-bleconnectrouter.service
```

>A second service is created by the installer to run the BLE related code (under sub directory `libs`). You do not need to start this because the main service `bpa-controls-channel` starts it automatically.  This is where the heavy lifting occurs (including logging - see next section).

## Logging

By default, the systems logs many event in DEBUG level mode. You can change this in the code (to: INFO, CRITICAL) to get lighter logs.
The logs are stored in syslog.

To see the logs:
```
journalctl -u bpa-bleconnectrouter -n 200 --no-pager
```
or:
```
journalctl SYSLOG_IDENTIFIER=bpa-logger -n 200 --no-pager 
```



