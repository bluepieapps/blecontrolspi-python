"""Shared BPA socket endpoint configuration."""

"""
if reusing the code on RPi that may have BluePieApps installed,
change NAMESPACE and generate new UUID for Service and characteristics,
if there is a chance that the bluepieapps cpde is running (as a service normally),
to avoid collisions/failures of running the main code at the same time.
"""

import fcntl
import os
import sys

#USe a company/developper identifier here
NAMESPACE = "bluepieapps"

#BLE UUID section

""" fro reference, these are the old ones used in btwifiset/BTBerryWifi: 
they are changed for this version of the library
UUID_WIFISET = 'fda661b6-4ad0-4d5d-b82d-13ac464300ce'  
UUID_WIFIDATA = 'e622b297-6bfe-4f35-938e-39abfb697ac3' 
UUID_INFO = '62d77092-41bb-49a7-8e8f-dc254767e3bf' 
""" 

UUID_WIFISET = 'a3fd7ce6-662e-44e3-a942-28d731b27663'  # service WifiSet 
UUID_DATA = '39e6f796-4d4b-4f2b-ad8f-dd5cd881dd80' # characteristic ChannelData
UUID_INFO = '68ed2df3-c084-4453-af82-bfdcdb15e614'    # characteristic Info

#Locks/System - Do not modify:
RUN_DIR = f"/run/{NAMESPACE}"
UUID_PART = "".join(UUID_WIFISET.split("-")[-2:])
ZMQ_LOCK_FILE = f"{RUN_DIR}/zmq-router.lock"
BLE_LOCK_FILE = f"/run/bpa-ble-{UUID_PART}.lock"
ZMQ_SYSTEM_TOPIC = "bpa\x1fsystem"
# Socket section ------------------------------------


# Socket: ServiceProcessBridge.pub_socket
# Role: Pipeline publishes inbound BLE/channel messages to logical channel subscribers.
# Socket: BPAMessageQueueClient.sub_socket
# Role: Channel handlers subscribe to logical channel topics.
BPA_PUB_SOCKET_PATH = f"{RUN_DIR}/pub.sock"

# Socket: ServiceProcessBridge.response_pull
# Role: Receives channel-process replies and sends immediate OK ACK.
# Socket: BPAMessageQueueClient.req_socket
# Role: Channel handlers send responses and notifications back toward BLE.
BPA_RESP_SOCKET_PATH = f"{RUN_DIR}/resp.sock"

# Socket: WifiSetService._worker_link
# Role: Parent-side control/data link between BLE service and pipeline worker.
# Socket: PipelineWorker.parent_link
# Role: Worker-side peer of WifiSetService._worker_link.
BPA_WORKER_PAIR_ENDPOINT = f"ipc://{RUN_DIR}/btservice-worker-pair.sock"

# Socket: ZmqLogServer.pull_socket
# Role: Central log receiver.
# Socket: ZmqLogClient._push_socket
# Role: Per-process log sender.
BPA_LOGGER_ENDPOINT = f"ipc://{RUN_DIR}/logger.sock"



class BLELock:
    def __init__(self):
        self._fd = None
        self.ensure_dir()

    def ensure_dir(self):
        os.makedirs(RUN_DIR, exist_ok=True)
        try:
            os.chmod(RUN_DIR, 0o755)  # owner rwx, group rx, others rx
        except PermissionError:
            # If running as non-root and dir already exists with stricter perms, ignore
            pass

    def acquire(self):
        # create file if missing
        self._fd = open(BLE_LOCK_FILE, "a")

        try:
            fcntl.flock(
                self._fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB
            )
            return True
        except BlockingIOError:
            print(
                f"CITICAL - CANNOT START BPA BLE ConnectRouter\nalready in use with service UUID {UUID_WIFISET}",
                flush = True
            )
            self._fd.close()
            self._fd = None
            return False
            

    def release(self):
        if self._fd:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None


class ZMQLock:
    def __init__(self):
        self._fd = None

    def acquire(self):
        # create file if missing
        self._fd = open(ZMQ_LOCK_FILE, "a")

        try:
            fcntl.flock(
                self._fd,
                fcntl.LOCK_EX | fcntl.LOCK_NB
            )
            return True
        except BlockingIOError:
            print(
                f"CITICAL - CANNOT START:\nBPA BLE ConnectRouter already running in namespace: {NAMESPACE}",
                flush = True
            )
            self._fd.close()
            self._fd = None
            return False

    def release(self):
        if self._fd:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None
