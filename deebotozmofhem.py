import asyncio
from crypt import crypt
import uuid, base64
from cryptography.fernet import Fernet
import cryptography
import debugpy
import aiohttp
import logging
from aiohttp import ClientError
from deebotozmo.ecovacs_api import EcovacsAPI
from deebotozmo.commands import (Charge, Clean, CleanArea, FanSpeedLevel, SetFanSpeed, GetCachedMapInfo, GetStats, GetPos, GetCleanLogs, GetCleanInfo, GetMajorMap)
from deebotozmo.ecovacs_mqtt import EcovacsMqtt
from deebotozmo.events import (BatteryEvent, MapEvent, FanSpeedEvent, StatsEvent, StatusEvent, RoomsEvent, CleanLogEvent, WaterInfoEvent, CustomCommandEvent)
from deebotozmo.vacuum_bot import VacuumBot, VacuumState
from deebotozmo.util import md5
from deebotozmo.commands.clean import CleanAction, CleanMode
import random
import string

logger = logging.getLogger(__name__)


from .. import fhem
from .. import generic

class deebotozmofhem(generic.FhemModule):
    def __init__(self, logger):
        super().__init__(logger)

        attr_config = {
            "username": {
                "default": "",
                "format": "string",
                "help": "Set Username with Login Command",
            },
            "botid": {
                "default": "0",
                "format": "string",
                "help": "ID of the Bot",
            },
            "debug": {
                "default": "off",
                "format": "string",
                "help": "debug",
            },
            "areas":{
                "default": "0,Kaffee,-4849.000000,-1569.000000,-3548.000000,-2373.000000;",
                "format": "string",
                "help": "Custom Areas in Format ID,Name,x1,y1,x2,y2;",
            },
            "cleanings":{
                "default": "1",
                "format": "string",
                "help": "Number of cleaning runs",
            },
            "autoconnect":{
                "default": "off",
                "options": "on,off",
                "help": "Automatic connection",
            },
        }
        self.set_attr_config(attr_config)

        set_config = {
            "password": {
                "args": ["password"],
                "params": {
                    "password": {"default":"password", "format": "string"}   
                }
            },
            "connect":{}
        }
        self.set_set_config(set_config)
        self.cleanings = 0
        self.session = None
        self.cipher_suite = Fernet(base64.urlsafe_b64encode(uuid.UUID(int=uuid.getnode()).bytes * 2))
        
    
    # FHEM FUNCTION
    async def Define(self, hash, args, argsh):
        await super().Define(hash, args, argsh)
        if len(args) != 4:
            return "Usage: define vacuumcleaner fhempy deebotozmo username"
        self.hash['username'] = args[3]
        self.cleanings = self._attr_cleanings
        await fhem.readingsBeginUpdate(hash)
        await fhem.readingsBulkUpdateIfChanged(hash, "state", "on")
        await fhem.readingsBulkUpdateIfChanged(hash, "cleanings", self.cleanings)
        await fhem.readingsEndUpdate(hash, 1)
        if self._attr_debug == "on":
            debugpy.listen(("192.168.1.50",1108))
        if self._attr_autoconnect == "on":
            await self.set_connect(hash, '')
        


    async def set_password(self, hash, params):
        # user can specify mode as mode=eco or just eco as argument
        # params['mode'] contains the mode provided by user
        password = params["password"]
        ciphered_text = await self.write_password(hash,password.encode()) 
        await fhem.readingsSingleUpdate(hash, "password", ciphered_text, 1)
        
    async def set_connect(self, hash, params):
        try: 
            self.username = self.hash['username']
            if self.username == "null":
                await fhem.readingsSingleUpdate(hash, "state", "Unable to read username. define [name] fhempy deebotozmofhem [username]",1)
                return "Unable to read username. define [name] fhempy deebotozmofhem [username]"
            pw = await self.read_password(hash)
            if pw == "null":
                await fhem.readingsSingleUpdate(hash, "state", "Unable to read stored password. Set password again!",1)
                return "Unable to read stored password. Set password again!"
            self.pw = md5(pw) 
            self.create_async_task(self.setup_deebotozmo())
        except (cryptography.fernet.InvalidToken):
             await fhem.readingsSingleUpdate(hash, "state", "Unable to read stored password. Set password again!",1)
             return "Unable to read stored password. Set password again!"

    async def write_password(self, hash, password):
        # no params argument here, as set_off doesn't have arguments defined in set_list_conf
        ciphered_text = self.cipher_suite.encrypt(password)
        with open(hash['NAME'] + ".pw", 'wb') as file_object:  file_object.write(ciphered_text)
        return ciphered_text

    async def read_password(self, hash):
        # no params argument here, as set_off doesn't have arguments defined in set_list_conf
        try:
            with open(hash['NAME'] + ".pw", 'rb') as file_object:
                for line in file_object:
                    encryptedpwd = line
            uncipher_text = (self.cipher_suite.decrypt(encryptedpwd))
            password = bytes(uncipher_text).decode("utf-8") #convert to string
            return password
        except FileNotFoundError as e:
            return "null"        
        
    async def setup_deebotozmo(self):
        email = self.username
        password_hash = self.pw
        continent = "eu"
        country = "de"
        device_id = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(12))

        self.session = aiohttp.ClientSession()
        

        api = EcovacsAPI(self.session, device_id, email , password_hash , continent=continent, country=country,
                    verify_ssl=False)
        try:
            await api.login() 
        except RuntimeError as e:
            await fhem.readingsSingleUpdate(self.hash, "state", e.args[0] , 1)
            self.session = None

        await fhem.readingsSingleUpdate(self.hash, "state", "connected" , 1)
        devices_ = await api.get_devices()   
        await fhem.readingsSingleUpdate(self.hash, "Devices", len(devices_) , 1)
        deviceInfo = ""
        for idx, device in enumerate(devices_):
            deviceInfo += "ID: " + str(idx) + ", Name:" + device.nick + ", Devicename: " + device.device_name + "\n"

 
        await fhem.readingsSingleUpdate(self.hash, "DeviceInfo", deviceInfo , 1)

        id = (int(self._attr_botid))
        auth = await api.get_request_auth()
        self.bot = VacuumBot(self.session, auth, devices_[id], continent=continent, country=country, verify_ssl=False)
        mqtt = EcovacsMqtt(continent=continent, country=country)
        await mqtt.initialize(auth)
        await mqtt.subscribe(self.bot)

        async def on_battery(event: BatteryEvent):
            # Do stuff on battery event
            # Battery full
            await fhem.readingsSingleUpdate(self.hash, "Battery", event.value , 1)

            pass
        
        async def on_map(_: MapEvent) -> None:
            # Do stuff on battery event
            # Battery full
            #await fhem.readingsSingleUpdate(self.hash, "Map" , '<html><img src="data:image/png;base64,' + self.bot.map.get_base64_map(500).decode('ascii') + '"/></html>', 1)
            await fhem.readingsSingleUpdate(self.hash, "MapEvent" , "MapEvent", 1)
            pass

        async def on_stats(event: StatsEvent):

            await fhem.readingsSingleUpdate(self.hash, "StatsEvent" , "StatsEvent" , 1)

        
        async def on_status(event: StatusEvent):
            if event.state == VacuumState.CLEANING:
                status = "Cleaning"
            elif event.state == VacuumState.IDLE:
                status = "Idle"
            elif event.state == VacuumState.RETURNING:
                status = "Returning"
            elif event.state == VacuumState.DOCKED:
                status = "Docked"
            elif event.state == VacuumState.ERROR:
                status = "Error"
            elif event.state == VacuumState.PAUSED:
                status = "Paused"
            await fhem.readingsSingleUpdate(self.hash, "Status" , status, 1)
        

        async def on_water(event: WaterInfoEvent):
            await fhem.readingsSingleUpdate(self.hash, "Wipermode" , event.mop_attached, 1)
            await fhem.readingsSingleUpdate(self.hash, "Waterlevel" , event.amount, 1)
        

        async def on_cleanLog(event: CleanLogEvent):
            await fhem.readingsSingleUpdate(self.hash, "CleanLogEvent" , "CleanLogEvent", 1)
        
        async def on_fan_speed(event: FanSpeedEvent):
            await fhem.readingsSingleUpdate(self.hash, "FanSpeed" , event.speed , 1)

        async def on_rooms(event: RoomsEvent):
            RoomInfo = ""
            for room in event.rooms:
                RoomInfo += "ID: " + str(room.id) + ", Name:" + room.subtype + "\n"
            await fhem.readingsSingleUpdate(self.hash, "Rooms" , RoomInfo, 1)
        
        self.bot.events.map.subscribe(on_map)
        self.bot.events.battery.subscribe(on_battery)
        self.bot.events.stats.subscribe(on_stats)
        self.bot.events.status.subscribe(on_status)
        self.bot.events.water_info.subscribe(on_water)
        self.bot.events.clean_logs.subscribe(on_cleanLog)
        self.bot.events.fan_speed.subscribe(on_fan_speed)
        self.bot.events.rooms.subscribe(on_rooms)
        self.bot.events.map.request_refresh()
        self._conf_set.update({
            "clean":{},
            "clean_custom_area":{
                "args": ["area"],
                "params":  { "area" : { "default" : "0", "foramat": "string"}}
            },
            "clean_spot_areas":{
                "args": ["areas"],
                "params":  { "areas" : { "default" : "0", "foramat": "string"}}
            },
            "cleanings":{
                "args": ["cleanings"],
                "options": "1,2"
            },
            "fanspeed":{
                "args": ["fan"],
                "options": "quiet,normal,max,max+"
            },
            "charge":{},
            "pause":{},
            "resume":{},
            "stop":{},
            "map":{}
        })
        
        await self.bot.execute_command(GetCleanInfo())
           
    async def set_clean(self, hash, params):
        await self.bot.execute_command(Clean(CleanAction.START))
    
    async def set_cleanings(self, hash, params):
        self.cleanings = int(params['cleanings'])
        await fhem.readingsSingleUpdate(hash, "cleanings", self.cleanings, 1)

    async def set_clean_spot_areas(self, hash, params):
        await self.bot.execute_command(CleanArea(CleanMode.SPOT_AREA, params['areas'] , self.cleanings))

    async def set_clean_custom_area(self, hash, params):
        id = int(params['area'])
        areas = self._attr_areas.split(';')
        for area in areas:
            if area:
                areaValues = area.split(',')
                if int(areaValues[0]) == id:
                    await self.bot.execute_command(CleanArea(CleanMode.CUSTOM_AREA, areaValues[2] + "," + areaValues[3] + "," + areaValues[4] + "," +areaValues[5], self.cleanings))

    async def set_charge(self, hash, params):
        await self.bot.execute_command(Charge())

    async def set_pause(self, hash, params):
        await self.bot.execute_command(Clean(CleanAction.PAUSE))

    async def set_resume(self, hash, params):
        await self.bot.execute_command(Clean(CleanAction.RESUME))

    async def set_stop(self, hash, params):
        await self.bot.execute_command(Clean(CleanAction.STOP))

    async def set_map(self, hash, params):
        self.create_async_task(self.display_loop())

    async def set_fanspeed(self, hash, params):
        if(params['fan'] == "quiet"):
           await self.bot.execute_command(SetFanSpeed(FanSpeedLevel.QUIET))
        elif(params['fan'] == "normal"):
           await self.bot.execute_command(SetFanSpeed(FanSpeedLevel.NORMAL))
        elif(params['fan'] == "max"):
           await self.bot.execute_command(SetFanSpeed(FanSpeedLevel.MAX))
        elif(params['fan'] == "max+"):
           await self.bot.execute_command(SetFanSpeed(FanSpeedLevel.MAX_PLUS))

    async def display_loop(self):
        while True:
            img = self.bot.map.get_base64_map(400).decode('ascii')
            await fhem.readingsSingleUpdate(self.hash, "Map" , '<html><img src="data:image/png;base64,' + img + '" width="400"/></html>', 1) 
            #await fhem.readingsSingleUpdate(self.hash, "Map" , 'Map', 1) 
            
