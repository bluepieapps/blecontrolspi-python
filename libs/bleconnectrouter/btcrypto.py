
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives import hashes
from cryptography import exceptions as crypto_exceptions
import re
import io
import subprocess
import os
import random
import json
from threading import Timer
from libs.bleconnectrouter.zmqlogger import ZmqLogClient as Log
import pathlib


FILEDIR = pathlib.Path(__file__).parent.resolve()
ROOTDIR = pathlib.Path(__file__).resolve().parents[2]

class PiInfo:
    PWFILE = ROOTDIR /"crypto"
    INFOFILE = FILEDIR / "infopi.json"

    """
    variables and storing needs:
        - password - stores into file name crypto which makes it easy for use to read or update / can be None
    the folowing are stored as json (dict)
        - locked: Ture or False
        - rpi_id: create once to identify the hardware as best as possible (see RPiId class) / can be None
        - las_nonce: stored as integer (max 12 bytes see NonceCouter.MAXNONCE) defaults to 0
    """

    @staticmethod
    def get_hostname():
        result = subprocess.run("hostname", 
                                shell=True,capture_output=True,encoding='utf-8',text=True)
        return result.stdout

    def __init__(self):
        self.password = self.getPassword()  #has the clear text password if crypto file exists with a password, None otherwise
        self.locked = False  # this is the permanent state saved to disk
        self.rpi_id = RPiId().rpi_id
        self.last_nonce = 0
        if not self.getInfoFromFile():
            self.initializePiInfoFile()

    def initializePiInfoFile(self):
        """
        if the file infopi.json does not exists yet,
        set it up with:
            - if crypto file exists and there is a password - set up Locked Pi
                otherwise setup unlocked
        Note: there is no way for the code to change locked status (but user can do so manually) in the file
        Also: if this is called, it overwrites the file if it exists (but json was corrupted)
        """
        pw = self.getPassword()
        self.locked = pw is not None
        Log.log(f"initializePiInfoFile -  {PiInfo.INFOFILE} with locked: {self.locked}, nonce: {self.last_nonce}")
        self.saveInfo()

        
    def getInfoFromFile(self):
        try:
            with open(PiInfo.INFOFILE, 'r', encoding="utf-8") as f:
                dict = json.load(f)
                self.locked = dict["locked"]
                self.last_nonce = dict["last_nonce"]
            return True  
        except FileNotFoundError:
            return False
        except Exception as ex:
            Log.log(f"Error reading file {PiInfo.INFOFILE}: {ex}",
                    level=Log.CRITICAL) 
            return False

    def saveInfo(self): 
        try:
            dict = {"locked":self.locked, "last_nonce":self.last_nonce}
            #creates file if it does not exists
            with open(PiInfo.INFOFILE, "w", encoding='utf8') as f:
                json.dump(dict, f, ensure_ascii=False)
            return True
        except Exception as ex:
            Log.log(f"error writing to file {PiInfo.INFOFILE}: {ex}",
                    level=Log.CRITICAL) 
            return False

    def getPassword(self):
        #if crypto file exists but password is empty string - return None as if file did not exist
        try:
            with open(PiInfo.PWFILE, 'r', encoding="utf-8") as f:
                raw_line = f.readline()
                if raw_line == '' or raw_line == '\n':  # completely empty or only newline
                    Log.log("get password has found no entry in the file")
                    return None
                else:
                    pw = raw_line.rstrip('\n')  # remove only the newline, preserve spaces
                    return pw
        except Exception as ex:
            Log.log(f"getPassord exception: {ex}",
                    level=Log.CRITICAL)
            return None
        
    def prn(self):
        #x+nonce_bytes+rpi_id_bytes
        return f"{'LOCK' if self.locked else 'not_locked'}, {self.rpi_id}, nonce: {self.last_nonce}, {'Has Password' if self.password else 'No password'}"


class NonceCounter:
    # numNonce is a 96 bit unsigned integer corresponds to max integer of 79228162514264337593543950335 (2 to the 96 power minus 1)
    MAXNONCE = 2 ** 64 -1
    '''
    maintains and increment a nonce of 12 bytes - 96 bit 
    the 4 most significant bytes are used for the connected iPhone identifier
    the least significant 8 bytes are the actual message counter.
    RPi always sends a nonce with identifier = 0
    if increment goes above max value for 64 bit
    looped is set to True, and counter restarts at zero
    Note: the logic to handle a looped counter has not yet been written.
        this event should not happen in the btwifiset usage.

    fot init: last_nonce is the 64 bit message counter saved on disk when previous session ended (infopi.json)

    Last received management:
        - iphone use 4 bytes of 12 bytes nonce as identifier.
        - RPi keeps track of last received for each connected iPhone (there can be more than one)
            using last_received_dict
        - when iPhone disconnects - it should send a disconnect message - if RPi is Locked - the identifier is included:
            when iPhone announces disconnection - remove key in dictionary
    '''
    def __init__(self,last_nonce):
        #last_nonce is normally saved on disk as Long
        self.num_nonce = last_nonce+2  #num_nonce is the RPi message counter
        self.looped = False
        self.last_received_dict = {}  #key is iphone identifier, value is last received 8 bytes message counter from iphone Nonce
        self._useAES = False #assume using chacha as default

    def removeIdentifier(self,x_in_bytes):
        """
        convenience method to cleanup identifiers when phone/tablet device disconnects
        this is called, only if device sends a quit message registered in crypto manager
        before disconnecting.
        Not absolutely necessary - previous connected devices will only accumulate in the last_receive_dict
        """
        identifier_bytes = x_in_bytes[8:]
        key = str(int.from_bytes(identifier_bytes, byteorder='little', signed=False))
        Log.log(f"Removing identifier form nonce dict: {key}")
        self.last_received_dict.pop(key, None)

    def checkLastReceived(self,x_in_bytes):
        '''
        checks last received
            if x_in_bytes passed in here is less or equal to current last receive - do nothing and return None
            otherwise, update and return the numerical value

        return True if nonce is good, false if it is stale
        '''
        try:
            message_counter_bytes = x_in_bytes[0:8]
            identifier_bytes = x_in_bytes[8:]
            message_counter = int.from_bytes(message_counter_bytes, byteorder='little', signed=False)
            identifier_str = str(int.from_bytes(identifier_bytes, byteorder='little', signed=False))
            Log.log(f"nonce received: {message_counter} - for identifier: {identifier_str}")
            #if first time seeing this identifier - just accept the nonce as is 
            if identifier_str not in self.last_received_dict:
                self.last_received_dict[identifier_str] = message_counter
                Log.log("this is a new identifier - added to last_received_dict")
                return True
            else :
                if message_counter <= self.last_received_dict[identifier_str]:
                    Log.log(f"stale nonce: last received = {self.last_received_dict[identifier_str]} - ignoring message",
                    level=Log.INFO)
                    return False
                else:
                    Log.log(f"updating last received to {message_counter}")
                    self.last_received_dict[identifier_str] = message_counter
                    return True
        except Exception as ex:
            Log.log(f"last receive check error: {ex}",
                    level=Log.CRITICAL)
            return False

    def increment(self):
        if self.num_nonce >= NonceCounter.MAXNONCE:
            self.num_nonce = 0
            self.looped = True
        else:
            self.num_nonce += 1

    def next_even(self): 
        self.increment()
        if self.num_nonce % 2 > 0:
            self.increment()
        return self.num_nonce

    @property
    def bytes(self):
        #signed is False by default
        # mapping num_nonce to 12 bytes means the 4 most significant bytes are always 0
        return self.num_nonce.to_bytes(12, byteorder='little')
    
    @property
    def padded_bytes(self):
        #used for Android AES encryption
        #signed is False by default
        # mapping num_nonce to 16 bytes means the 8 most significant bytes are always 0
        return self.num_nonce.to_bytes(16, byteorder='little')

    @property
    def useAES(self):
        return self._useAES

    @useAES.setter
    def useAES(self, value):
        self._useAES = value

class RPiId:
    # FILERPIID = "rpiid"

    def __init__(self):
        self.rpi_id = self.createComplexRpiID()

    def createComplexRpiID(self):
        try:
            cpuId = self.getCpuId()
            wifiId = self.getMacAddressNetworking()
        except Exception as e:
            Log.log(f"exception detected: {e}",
                    level=Log.CRITICAL)
        complexId = cpuId if cpuId is not None else ""
        complexId += wifiId if wifiId is not None else ""
        # complexId += btId if btId is not None else ""
        if complexId == "" : 
            Log.log("no identifier found for this RPi - generating random id")
            complexId = str(int.from_bytes(random.randbytes(12), byteorder='little', signed=False))
        return self.hashTheId(complexId)

    def hashTheId(self,id_str):
        #return the hex representeion of the hash
        m = hashes.Hash(hashes.SHA256())
        m.update(id_str.encode(encoding = 'UTF-8', errors = 'strict'))
        hash_bytes = m.finalize()
        hash_hex = hash_bytes.hex()
        return hash_hex

    
    def getCpuId(self):
        with io.open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
            matches = re.findall(r"^(Hardware|Revision|Serial)\s*:\s*(.+)$", text, re.M)
            use_id = "".join([x[1] for x in matches])
            return use_id or None


    def getNewCpuId(self):
        out = subprocess.run(r'cat /proc/cpuinfo | grep "Serial\|Revision\|Hardware"', shell=True,capture_output=True,encoding='utf-8',text=True).stdout
        matches = re.findall(r"^(Hardware|Revision|Serial)\s+:\s(.+)", out,re.M)  
        use_id = "".join([x[1] for x in matches])
        if len(use_id) ==0: return None
        return use_id

    #don't use /etc/machine-id - it is generated on install - i.e if user re-istalls on a card it will change
    # def getCpuId(self):
    #     #first look for a cpu serial 
    #     str = subprocess.run("cat /proc/cpuinfo | grep Serial", shell=True,capture_output=True,encoding='utf-8',text=True).stdout
    #     if len(str) > 0 :
    #         #this stirps the leading zeros if any
    #         cpu_id = re.findall(':\s*(\S+)', str)
    #     if len(cpu_id) == 1:
    #         return cpu_id[0] if len(cpu_id[0]) > 0 else None
    #     else: 
    #         return None
    
    def getAdapterAddress(self,adapter):
        try:
            with open(f"{adapter}/address", 'r', encoding="utf-8") as f:
                found_id = f.read().rstrip('\n')
                return None if (found_id ==  "00:00:00:00:00:00" or found_id == "") else found_id
        except Exception as e:
            return None
    
    def getMacAddressNetworking(self):
        """
        look for ethernet adpater first and use address, if not look for wireless adapter and get address
        this is less robust since if user has removable adapters - they could change in which case
        user would need to re-establish password for RPI which display different MAC/ID
        - full blown RPi will have internet adapter on board.
        - smaller Rpi lie "zero" may have only wifi - or nothing
        """

        found_id = None

        #shortcut - most RPi have either eth0 or wlan0 - so try these two first
        eth0 = "/sys/class/net/eth0"
        wlan0 = "/sys/class/net/wlan0"
        #since this was written to allow the user to set a wifi SSID and password via bluetooth
        #in most cases we can expect the wlan0 adapter to exists - so always use that first
        if os.path.isdir(wlan0):
            found_id = self.getAdapterAddress(wlan0)
        if found_id is not None: return found_id
        if os.path.isdir(eth0):
            found_id = self.getAdapterAddress(eth0)
        if found_id is not None: return found_id
        
        Log.log("neither wlan0 nor eth0 found - searching interfaces",
                    level=Log.INFO)
        #for differnet linux OS - name maybe different - use this to find ethernet and wifi adapters if they exists
        try: 
            interfaces = [ f.path for f in os.scandir("/sys/class/net") if f.is_dir() ]
            wireless_interfaces = []
            ethernet_interfaces = []
            #wireless devices have the empty directory "wireless" in their directory, ethernet devices do not
            for interface in interfaces:
                if os.path.isdir(f"{interface}/wireless"): 
                    wireless_interfaces.append(interface)
                else:
                    ethernet_interfaces.append(interface)
            
            for interfaces in (ethernet_interfaces, wireless_interfaces):
                interfaces.sort()
                for interface in interfaces:
                        found_id = self.getAdapterAddress(interface)
                if found_id is not None: return found_id
        except: pass

        return None

    # def getMacAdressBluetooth(self):
    #     """
    #     although we are garanteed to find a mac address for bluetooth - it is not garanteed that this mac address will not change
    #     """
    #     str = subprocess.run("bluetoothctl list", shell=True,capture_output=True,encoding='utf-8',text=True).stdout
    #     #this finds all interfaces but ignores lo
    #     mac = re.findall('^Controller\s+([0-9A-Fa-f:-]+)\s+', str)
    #     if len(mac) == 1:
    #         if len(mac[0]) > 0 : 
    #             return mac[0]
        
    #     return None
    
    # def get_bt_mac_cli(self,timeout=0.5):
    #     try:
    #         res = subprocess.run(
    #             ["bluetoothctl", "show"],
    #             capture_output=True, text=True,
    #             stdin=subprocess.DEVNULL, timeout=timeout, check=False
    #         )
    #     except subprocess.TimeoutExpired:
    #         return None

    #     # Typical line: "Controller AA:BB:CC:DD:EE:FF (public)"
    #     m = re.search(r'^Controller\s+([0-9A-Fa-f:]{17})\b', res.stdout, re.M)
    #     return m.group(1) if m else None
    
class AndroidAES:
    @staticmethod
    def encrypt(plaintext_bytes, key,nonce_counter):
        # Generate a random 16-byte IV
        iv = nonce_counter.padded_bytes
        
        # Create a padder
        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(plaintext_bytes) + padder.finalize()
        
        # Create an encryptor
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        
        # Encrypt the padded data
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()
        # print(''.join('{:02x}'.format(x) for x in ciphertext))
        #always return a 12 byte nonce (to match chachapoly implementation
        return nonce_counter.bytes + ciphertext

    @staticmethod
    def decrypt(ciphertext, key):
        # Extract the IV (first 12 bytes)
        iv = ciphertext[:12]
        iv += bytes.fromhex("00000000")  #cypher text arrives with 12 bytes nonce - pad it to 16
        ciphertext = ciphertext[12:]
        
        # Create a decryptor
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        
        # Decrypt the ciphertext
        padded_data = decryptor.update(ciphertext) + decryptor.finalize()
        
        # Create an unpadder
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded_data) + unpadder.finalize()
        return plaintext

class BTCrypto:
    """
    class to encrypt a string or decrypt a cypher (bytes) using ChaCha20Poly1305 
    initialise it with the password (read from disk)
    password is hashed here to make key
    always pass NonceCounter instance to encrypt or decrypt:
        encrypt will increment counter to get the next nonce for encryption
        decrypt will record last_nonce (received) if message is decoded correctly
    note: nonce_counter is single instance maintained by BtCryptoManager - which is instantiated at start

    Android vs iOS:
    iOS was developped first using the latest encryption (Chacha20Poly1305)
    Android: some of the older devices do not have access to ChaCha... so AES is used instead.
    Since iOS App is already published, ChaCha... needs to be supported.
    Since the code always react to a request from phone device, decrypt is always called first,
        follwed by an encrypted response (Notification).
    To support both encryption, when an encrypted message is received, both encryption are tried (iOS first):
        - if they both fail we raise an exception (as before android)
        - if one passes, the flag "useAES" is set accordingly.
        note:   the flag useAES is store with nonce counter - which is instantiated only once.  
                it cannot be sotred in BTCrypto since this class is re-instantiated everytime the encryption changes.
        - when the encryption is then used for the response, it selects the correct encryption based on this flag.
        - Note: the flag is set every time a decryption occur so the following encryption(s) always match.
    This will however cause problem if two devices of different type (one iOS, one Android) connect at the same time:
        Since notifications go to all devices registered, the device that is idle while the other request and encrypted action,
        will received an encrypted message it cannot decrypt, and assume that it's password is stale:
            - it will disconnect
            - it will erase the password from the device.
            - it will warn the user asking for the password.
                - if user enters the password, this will be sent to the RPi, and be accepted, but the response
                will go the the previous device, which will then see an undecryptable message and disconnect as well 
            - users will basically block each other until one stops entering the password.
    A notice will be provided on the blog to explain that multiple devices of diffrent types connecting at the same time is not suppported.
    """

    def __init__(self,pw):
        self.password = pw
        self.hashed_pw = self.makeKey256(pw)

    def makeKey256(self,key):
        m = hashes.Hash(hashes.SHA256())
        m.update(key.encode(encoding = 'UTF-8', errors = 'strict'))
        return m.finalize()
    
    def encryptForSending(self,message_bytes,nonce_counter):
        #TODO: this is not supported if two devices with different encryption are used
        #last device to send data sets encryption - so next device will receive data that cannot be decrypted.
        #none_counter of type NonceCounter
        Log.log(f'current nonce is: {nonce_counter.num_nonce}')
        nonce_counter.next_even()
        nonce = nonce_counter.bytes
        if nonce_counter.useAES:
            Log.log(f'encrypting with AES')
            return AndroidAES.encrypt(message_bytes,self.hashed_pw,nonce_counter)
        else:
            #IOS uses chachapoly
            Log.log(f'encrypting with ChaCha')
            chacha = ChaCha20Poly1305(self.hashed_pw)
            ct = chacha.encrypt(nonce, message_bytes,None)
            return nonce+ct 
        
    def decryptAES(self,cypher,nonce_counter):
        """
        returns the decrypted message
        raises error if cannot be decrypted (wrong password/key or wrong encryption method)
        if Nonce is stale (replay attack) - returns a empty/blank b'' 
        """
        try:
            nonce_bytes = cypher[0:12]
            message = AndroidAES.decrypt(cypher,self.hashed_pw) 
            if not nonce_counter.useAES: Log.log(f'AES encryption detected') # only warn if changing encryption
            nonce_counter.useAES = True
            if nonce_counter.checkLastReceived(nonce_bytes) :return message
            #if nonce was stale return a blank message which will be ignored
            return b""
        except Exception as ex: 
            Log.log(f"crypto decrypt error (AES): {ex}")
            raise ex

    def decryptChaCha(self,cypher,nonce_counter):
        """
        returns the decrypted message
        raises error if cannot be decrypted (wrong password/key or wrong encryption method)
        if Nonce is stale (replay attack) - returns a empty/blank b'' 
        """
        #combined message arrives with nonce (12 bytes first)
        #this returns the encode message as utf8 encoded bytes -> so btwifi characteristic can process them as before - including SEPARATOR 
        #raise the error after printing the message - so it is caught in the calling method

        #************ below is for ios chachapoly
        nonce_bytes = cypher[0:12]
        ct = bytes(cypher[12:])
        chacha = ChaCha20Poly1305(self.hashed_pw)
        try:
            message = chacha.decrypt(nonce_bytes, ct,None)
            if nonce_counter.useAES : Log.log(f'ChaCha encryption detected') #only warn if changing encryption
            nonce_counter.useAES = False
            #checkLastReceived updates the last receive dictionary if nonce is OK (ie not stale)
            if nonce_counter.checkLastReceived(nonce_bytes) : return message
            #if nonce was stale return a blank message which will be ignored
            return b""
        except crypto_exceptions.InvalidTag as invTag:
            Log.log("crypto Invalid tag - cannot decode")
            raise invTag
        except Exception as ex: 
            Log.log(f"crypto decrypt error(ChaCha): {ex}",
                    level=Log.CRITICAL)
            raise ex
        
    def decryptFromReceived(self,cypher,nonce_counter):
        """
        tries to decrypt with previously use encryption type (AES: Android, chachaPoly: IOS)
        raise error if cannot decrypt
        """
        #always try the previous known encryption most use case only have one phone connected
        #Log.log(f"current decryption with  {'AES' if nonce_counter.useAES else 'ChaCha'}")
        if nonce_counter.useAES:
            Log.log("decrypting attempt with AES")
            try:
                encBytes = self.decryptAES(cypher,nonce_counter)
            except Exception as ex:
                try:
                    Log.log("decrypting attempt Failed with AES - trying ChachaPoly")
                    encBytes = self.decryptChaCha(cypher,nonce_counter)
                except:
                    raise ex
        else:
            Log.log("decrypting attempt with ChachaPoly")
            try:
                encBytes = self.decryptChaCha(cypher,nonce_counter)
            except Exception as ex2:
                try:
                    Log.log("decrypting attempt Failed with AES - trying AES")
                    encBytes = self.decryptAES(cypher,nonce_counter)
                except:
                    raise ex2
        return encBytes


class RequestCounter:

    def  __init__(self):
        self.kind = "normal"  # also use "garbled" and "lock_request"
        self.val = 0

    def _setCounterGarbled(self):
        self.kind = "garbled"
        self.val = 0

    def _setCounterRequest(self):
        self.kind = "lock_request"
        self.val = 0

    def incrementCounter(self,what_kind):
        #always increment counter before taking action/checking max
        #return True if maximum has been reached
        max_garbled = 2 #number of allowable tries
        max_request = 3 #number of allowable tries
        if self.kind == "normal": 
            if what_kind == "garbled": self._setCounterGarbled()
            if what_kind == "lock_request": self._setCounterRequest()
            return False
        self.val += 1
        if self.kind == "garbled": return self.val > max_garbled
        if self.kind == "lock_request": return self.val > max_request

    def resetCounter(self):
        self.kind = "normal"
        self.val = 0

    
class BTCryptoManager:
    """
    meant to be a singleton instantiated when code starts
    code is untested with multiple connections - but if multiple connections are allowed
    BTCryptoManager is available to all connections which implies:
        - if RPi is locked and requires encryption - it applies to all connection
        - if RPi is unlocked - all connections communicate in clear until Pi is locked

    when RPi receives a encrypted message while unlocked, or a garbled message while locked:
        - the decrypting method will automatically call the unknown() method - to process it and decide the response
            adn stores it in the unknown_response property
        - it will return unknown as decrypted message so Chracteristic can process it and call the register_ssid() on its service.
        - when the service sees this "unknown" - it fetches the response for the processed cypher in the
            unknown_response property and send it via notification.

        - device_quit_msg:  setup if device sends a specific message indicating it is about to disconnect.
            this serves to remove the device identifier in Nonce counter last_received_dict
            not strictly necessary; it will have a new identifier.
    """

    def __init__(self,shared_bytes_class_instance):
        # note: device_quit_msg is no longer used/passed in
        # self.quitting_msg = device_quit_msg
        try:
            self.pi_info = PiInfo() 
            self.nonce_counter = NonceCounter(self.pi_info.last_nonce)
            self._pending_unlock_plaintext = None
            if self.pi_info.locked and self.pi_info.password is not None: 
                self.crypto = BTCrypto(self.pi_info.password)
            else:
                self.crypto = None
            self.set_shared_bytes(shared_bytes_class_instance)
        except Exception as e:
            Log.log(f"crypto mgr init error {e}",
                    level=Log.CRITICAL )
        Log.log(f"initialized crypto manager - {self.pi_info.rpi_id}")

    def set_shared_bytes(self,shared_bytes):
        """
        args: shared_bytes is a Class that implements a shared memory space with appropriate locks between the thread
                that CryptoManager runs on, and the main thread (that BLE classes run on)
        implements this method to access (with self.shared_bytes.set(info_in_bytes) 
        """
        self.sharedBytes = shared_bytes #class that exposes thread safe get/set method to update _value (bytes) - for piinfo
        self.setPiInfo() #initialized shared_bytes with current piInfo

    def closeBTConnection(self):
        pass

    def getinformation(self):
        # returns bytes showing locked or not + rpi_id, or NoPassword if crypto file is empty
        if self.pi_info.password == None:
            Log.log("pi info has no password")
            return "NoPassword".encode()
        rpi_id_bytes = bytes.fromhex(self.pi_info.rpi_id)
        nonce_bytes = self.nonce_counter.num_nonce.to_bytes(12, byteorder='little')
        if self.pi_info.locked:
            x = "LOCK".encode() #defaults to utf8
            return x+nonce_bytes+rpi_id_bytes
        else:
            return  nonce_bytes+rpi_id_bytes
        
    def setPiInfo(self):
        piInfo_bytes = self.getinformation()
        self.sharedBytes.set(piInfo_bytes)
        
    
    def disableCrypto(self): 
        """
        this is called if user is already using encryption (RPI is locked)
        and has requested the correct bluetooth code : "UnLock"

        """
        if self.pi_info.locked:
            self.pi_info.locked = False
            self.crypto = None
            #note: password remains if it exists - in case pi becomes locked again
            #always save when going to unlocked.
            self.pi_info.saveInfo()
            self.setPiInfo()


    def encrypt(self,message_bytes):
        """
        Args: message_bytes is plain text encoded to bytes in utf8
            - expected to arrive via zmq when this class is running a separate background thread
            - note: different from integrated original version that used a string

        returns bytes ready to be sent
        this is the oonly place where nonce counter is incremented / so call setPiInfo here
        """
        # in case this is called directly with a string
        if isinstance(message_bytes,str): message_bytes = message_bytes.encode(errors="replace")
        if self.crypto == None: 
            return message_bytes
        else:
            cypher = self.crypto.encryptForSending(message_bytes,self.nonce_counter)
            self.pi_info.last_nonce = self.nonce_counter.num_nonce
            self.setPiInfo()
            return b'\x1d'+cypher
        
    def check_act_LockRequest(self,cypher):
        '''
        check if an encrypted message was received while unlocked that can be correctly decrypted as "LockRequest"
        if so, turn on encryption and return true
        if not - do nothing and return false.
        Note:   even if the message can be correctly decoded, it is ignored unless it is "LockRequest"
                this code does not accepts encrypted messages from the phone app when pi is unlocked,   
                unless it is the specific LockRequest message which serves to lock the pi - and force all future communication to be encrypted.
        '''
        if self.crypto is not None: return False #this method should not be called when RPi is locked

        try:
            #turn on encryption mode
            self.pi_info.locked = True
            self.crypto = BTCrypto(self.pi_info.password) 
            #decrypt message
            msg_bytes = self.crypto.decryptFromReceived(cypher,self.nonce_counter) #clear msg in bytes or empty bytearray if nonce is stale
            #note legacy BTBerryWifi app uses separator in front of command
            if msg_bytes == b'\x1eLockRequest':
                Log.log("received LockRequest - RPi has been locked")
                self.pi_info.saveInfo() #save the lock state (in infopi.json)
                self.setPiInfo()
                return True
        except Exception as ex:
            #exception is raised if message cannot be decrypted in eiter AES or Chacha
                Log.log(f"crypto decrypt error: {ex}")

        self.pi_info.locked = False
        self.crypto = None        
        return False

        
    def decrypt(self,cypher,forceDecryption = False):
        '''
        Args: cypher is encrypted messgage in bytes
        reminder: if pi is not locked (in infopi.json), regardles of whether a password exists or not (in crypto file)
                  self.crypto is None, meaning text is sent in clear and expected to be received in clear.
                  It is the responsibility of the phone app to query the pi info (getinformation), to find out if pi is locked or not.
                    
                    if Pi has no password, or is unlocked (not using encryption) - it will attempt to decode 
                    arriving bytes via decode in utf8.  that text is sent back to btwifi.  
                    it could fail because iphone is sending encrypted, or bluetooth channel has corrupted the channel,
                    in this case the message decrypted sent back to btwifi is "Garbled"

                    if pi is encrypting, and the message cannot be decoded it is either because iphone, is sending in clear, 
                    or iphone has wrong password or bluetooth channel has mangled the message.  in al cases, 
                    this send the decoded message "Garbled" back to btwifi.

                    if the message can be decoded but the nonce is stale, and empty byte is sent back (b'')

                    if phone is sending encrypted when unlocked - it will receive "Garbled" even if it could be decrypted.
                    (it could be sending a LockRequest - this is handled elsewhere)
        '''

        if self.crypto == None: #path when RPi is not Locked - sending text in clear
            try:
                #check if it can be decoded  with utf8 (it should be unless iphone is sending encrypted messages and pi is unlocked)
                clear = cypher.decode() # defaults to utf8 and strict error mode - should fail if encrypted msg
                Log.log(f" received clear text: {clear}")
                return cypher
            except: 
                #either phone is sending an encrypted request when it should be sending in clear 
                # or bluetooth channel has garbled the message
                Log.log("decrypting error: expecting clear text but cannot convert with utf8 - returning Garbled")
                return b'Garbled'
        else:
            #path when RPi is locked and sending.receiving encrypted data
            try:
                #this raises error if cannot be decrypted in either AES or chachaPoly
                #if nonce is stale - replay attack: returns empty b''(no error)
                #it is up to bluetooth handler to decide what to do with a blank message
                msg_bytes = self.crypto.decryptFromReceived(cypher,self.nonce_counter)
                Log.log(f"decrypted message is: {msg_bytes}")
                return msg_bytes 
                
            except Exception as ex:
                #exception is raised if message cannot be decrypted in eiter AES or Chacha
                Log.log(f"crypto decrypt error: {ex}")
                return  b'Garbled'
            
    def close(self):
        print("closing crypto",flush=True)
        self.pi_info.saveInfo()


           



   
