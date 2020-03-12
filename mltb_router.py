import requests, sys, os, json, asyncio, logging

logger = logging.getLogger('mltb')

class PerState:
    def __init__(self, spath = os.path.expanduser("~/.mltb/state.json")):
        self.spath = spath
        self.__state = {}

        if not os.path.exists(os.path.dirname(spath)):
            os.mkdir(os.path.dirname(spath))

        if os.path.isfile(spath):
            with open(spath) as sfile:
                self.__state = json.load(sfile)

    def __update(self):
        with open(self.spath, 'w+') as sfile:
            json.dump(self.__state, sfile)

    def subscribe_dev_list(self):
        if self.__state.get("subscribe_dev_list", None):
            return (self.__state["subscribe_dev_list"]["cmd"],
                    self.__state["subscribe_dev_list"]["list"])
        else:
            return None, []

    def subscribe_dev_add(self, item):
        cmd, uid = item
        if not self.__state.get("subscribe_dev_list", None):
            self.__state["subscribe_dev_list"] = {"cmd": cmd, "list": []}

        if uid not in self.__state["subscribe_dev_list"]["list"]:
            self.__state["subscribe_dev_list"]["list"].append(uid)
            self.__update()

    def subscribe_dev_del(self, item):
        __, uid = item
        if not self.__state.get("subscribe_dev_list", None):
            return

        if uid in self.__state["subscribe_dev_list"]["list"]:
            self.__state["subscribe_dev_list"]["list"].remove(uid)
            self.__update()

class Notify:
    def __init__(self):
        self.__list = []

    def list(self):
        return self.__list

class NotifyWriter:
    def __init__(self, notify):
        self.__notify = notify

    def register(self, call, args):
        if (call, args) not in self.__notify.list():
            self.__notify.list().append((call, args))

    def unregister(self, call, args):
        if (call, args) in self.__notify.list():
            self.__notify.list().remove((call, args))

class NotifyReader:
    def __init__(self, notify):
        self.__notify = notify

    def handle(self, ctx):
        for call, args in self.__notify.list():
            call(*args, ctx = ctx)

    def get_handlers(self):
        return self.__notify.list()

async def opt_await_call(call, args):
    return await call(*args) if asyncio.iscoroutinefunction(call) else call(*args)

class Obj:
    def __init__(self, **entries):
        self.__dict__.update(entries)

class NetDevs:
    def __init__(self, devs, translation = {}):
        self.devs = list(devs)
        self.translation = translation

        fmt_dev = lambda dev: {
            "id": dev["deviceID"],
            "device": dev["model"]["deviceType"],
            "devtype": dev["model"].get("manufacturer"),
            "name": dev.get("friendlyName"),
            "net": dev.get("connections"),
            "kname": self.translation.get(dev["deviceID"], None),
        }
        self.__fmt_devs = [fmt_dev(dev) for dev in devs]
        self.__keys = set(d["id"] for d in self.__fmt_devs)

    def info(self, mask = False):
        if not mask:
            return self.__fmt_devs

        return [d for d in self.__fmt_devs if d["id"] in (mask ^ self.__keys)]

    def keys(self):
        return self.__keys

class Router:
    def __init__(self, event_loop, uid_table, ip="192.168.1.1"):
        self.loop = event_loop
        self.url = "http://%s/JNAP/" % ip
        self.uid_table = uid_table if uid_table else {}

    def __request(self, action):
        headers = {
            "X-JNAP-Action": "http://linksys.com/jnap" + action,
        }
        try:
            resp = requests.post(self.url, data=json.dumps({}), headers=headers).json()
        except requests.exceptions.RequestException as e:
            logger.error(e)
            return {}

        if resp["result"] == "OK":
            return resp["output"]

        logger.error("Bad response: " + resp["result"])
        return {}

    def __all_registered_devs(self):
        return self.__request("/devicelist/GetDevices").get("devices", None)

    def __online_macs(self):
        for m in self.__request("/networkconnections/GetNetworkConnections").get("connections", []):
            yield m["macAddress"]

    def __dev_filter(self, devs, flist, key = lambda x:x):
        return NetDevs([d for d in devs if key(d) in flist], translation = self.uid_table)

    def __online_devs(self,
            mac = lambda d: d["connections"][0]["macAddress"] if d.get("connections") else None):
        if not (all_devs := self.__all_registered_devs()):
            return None
        if not (online_macs := list(self.__online_macs())):
            return None
        return self.__dev_filter(all_devs, online_macs, key = mac)

    async def online_devs(self):
        return await self.loop.run_in_executor(None, self.__online_devs)

async def router(loop, notify, opts, period_time = 3):
    logger.info("#Router engine started")
    rt = Router(loop, uid_table = opts.uid_table)

    prev_devs = await rt.online_devs()
    while (True):
        await asyncio.sleep(period_time)

        if not (devs := await rt.online_devs()):
            logger.error("Unexpected error: devs info is not available")
            continue

        if prev_devs.info() == devs.info():
            continue

        logger.debug("Devices changed")
        mask = prev_devs.keys() & devs.keys()
        in_devs, out_devs = devs.info(mask), prev_devs.info(mask)
        for handler, args in notify.get_handlers():
            await opt_await_call(handler, args = (*args, (in_devs, out_devs)))
        prev_devs = devs

class Telegram:

    class Command:
        def __init__(self, it):
            from datetime import datetime
            self.offs = it["update_id"]

            msg = it.get("message", it.get("edited_message", None))
            self.user_id = msg["from"]["id"]
            self.bot = msg["from"]["is_bot"]
            self.user_name = msg["from"]["first_name"]
            self.lang = msg["from"]["language_code"]
            self.type = msg["chat"]["type"]
            self.date = datetime.fromtimestamp(msg["date"])
            self.value = msg["text"].lower()

    def __init__(self, loop, token, cmd_list, access_list):
        self.__url_base = "https://api.telegram.org/bot%s/" % token
        self.__url_get = self.__url_base + "GetUpdates"
        self.__url_send = self.__url_base + "sendMessage"
        self.__loop = loop
        self.__offs = None
        self.__handlers = {}
        self.descs = []
        self.user_white_list = access_list
        self.pstate = PerState()

        for names, call, desc in cmd_list:
            self.__handlers.update({name:call for name in names})
            self.descs += [desc]

    def __request_get(self, url, params):
        try:
            resp = requests.get(url, params).json()
        except requests.exceptions.RequestException as e:
            logger.error(e)
            return []

        if not resp.get("ok", False):
            logger.error(f"Wrong response: {resp}")
            return []
        return resp["result"]

    def __messages(self, timeout):
        params = {'timeout': timeout,
                  'offset': self.__offs,
                  'allowed_updates': ["message"],
        }
        return self.__request_get(self.__url_get, params)

    def __get_chat(self, chat_id, extract_field = None):
        if not (chat_info := self.__request_get(self.__url_base + "getChat", {'chat_id': chat_id})):
            return {}
        return chat_info.get(extract_field, {}) if extract_field else chat_info

    def __async_commands(self, timeout):
        return map(lambda it: self.Command(it), self.__messages(timeout))

    async def commands(self, timeout = 30):
        return await self.__loop.run_in_executor(None, self.__async_commands, timeout)

    def __async_response(self, args):
        user_id, answer = args
        try:
            resp = requests.post(self.__url_send, {
                'chat_id': user_id,
                'text': answer,
            }).json()
        except requests.exceptions.RequestException as e:
            logger.error(e)
            return []

        return resp

    def __unwind_subscribe_list(self, notify, opts, cmd_ctx):
        cmd_value, id_list = cmd_ctx
        for user_id in id_list:
            user_name = self.__get_chat(user_id, extract_field = "first_name")
            if user_name not in self.user_white_list:
                continue;
            #TODO Add check for cmd type to prevent run arbitrary TelegramCommand
            cmd = Obj(**{"value": cmd_value, "user_id": user_id})
            if not (handler := self.__handlers.get(cmd.value)):
                logger.debug(f"Unknown command: {cmd.value}")
                continue
            logger.debug(f"Load - cmd: {cmd_value}, user: {user_name}, user_id: {user_id}")
            handler(cmd, (None, notify, self, None, ))

    def __async_load_pstate(self, args):
        notify, opts = args
        self.__unwind_subscribe_list(notify, opts, self.pstate.subscribe_dev_list())

    async def load_pstate(self, notify, opts):
        await self.__loop.run_in_executor(None, self.__async_load_pstate, (notify, opts,))

    async def response(self, chat_id, answer):
        return await self.__loop.run_in_executor(None, self.__async_response, (chat_id, answer,))

    async def command_response(self, cmd, result):
        resp = await self.response(cmd.user_id, result)
        if not resp.get("ok", False):
            logger.error(f"Something wrong with cmd response to: {cmd.user_name}, resp: {resp}")
        self.__offs = cmd.offs + 1

    async def command_handler(self, cmd, notify, opts):
        if self.user_white_list and (cmd.user_name not in self.user_white_list):
            logger.warning(f"Access denied: {cmd.value} {cmd.offs}")
            return "Access denied"
        if not (handler := self.__handlers.get(cmd.value)):
            logger.debug(f"Unknown command: {cmd.value} offs:{cmd.offs}")
            return "Unknown command"
        return await opt_await_call(handler, args = (cmd, (self.__loop, notify, self, opts, )))

class TelegramCommands:
    @staticmethod
    def __default_dev_name(dev):
        if devname := dev.get("kname", False):
            return devname
        elif dev.get("devtype", False):
            return dev["device"] + ": " + dev["devtype"]
        else:
            return dev["name"]

    @staticmethod
    def __get_dev_fmt(devlist, def_fmt = lambda x:x, prefix = False, postfix = False):
        postfix_fmt = postfix if postfix else ""
        prefix_fmt = prefix if prefix else ""
        for n, dev in enumerate(devlist, 1):
            if not prefix:
                prefix_fmt = ("%u. "%n)
            yield ("%s%s%s\n" %(prefix_fmt, def_fmt(dev), postfix_fmt))

    @classmethod
    async def get_dev_handler(cls, cmd, ctx):
        loop, _, __, opts = ctx
        return "".join(cls.__get_dev_fmt(
            devlist = (await Router(loop, opts.uid_table).online_devs()).info(),
            def_fmt = cls.__default_dev_name)
        )
    @classmethod
    async def reponse_changed_devices(cls, tlg, chat_id, ctx):
        logger.debug("Notify changed devices!")
        online_devs, offline_devs = ctx
        changes = "".join([*cls.__get_dev_fmt(
            online_devs, prefix = "<< ", postfix = " (online)", def_fmt = cls.__default_dev_name),
                          *cls.__get_dev_fmt(
            offline_devs, prefix = ">> ", postfix = " (offline)", def_fmt = cls.__default_dev_name)
        ])
        await tlg.response(chat_id, changes)

    @classmethod
    def register_dev_list_handler(cls, cmd, ctx):
        _, notify, tlg, __= ctx
        notify.register(cls.reponse_changed_devices, (tlg, cmd.user_id,))
        tlg.pstate.subscribe_dev_add((cmd.value, cmd.user_id, ))
        return "notification registered"

    @classmethod
    def unregister_dev_list_handler(cls, cmd, ctx):
        _, notify, tlg, __ = ctx
        notify.unregister(cls.reponse_changed_devices, (tlg, cmd.user_id,))
        tlg.pstate.subscribe_dev_del((cmd.value, cmd.user_id, ))
        return "notification unregistered"

    @staticmethod
    def help_handler(cmd, ctx):
        _, __, tlg, ___ = ctx
        return "Help:\n" + ("\n".join(tlg.descs))

    @classmethod
    def list(cls):
        return [ #TODO add command type flag
            (["d", "devices"], cls.get_dev_handler, "d, devices - Get current devices list"),
            (["r", "register"], cls.register_dev_list_handler, "r, register - Register device changes notification"),
            (["u", "unregister"], cls.unregister_dev_list_handler, "u, unregister - Unregister device changes notification"),
            (["h", "help"], cls.help_handler, "h, help - Command list"),
        ]

async def telega(loop, notify, opts):
    logger.info("#Telegram started")
    tlg = Telegram(loop, opts.token,
                   cmd_list = TelegramCommands.list(),
                   access_list = opts.access_list)
    await tlg.load_pstate(notify, opts)
    while (True):
        for cmd in await tlg.commands(timeout = 60*5):
            logger.debug(f"<< IN message from {cmd.user_name}:\n{cmd.value}")
            result = await tlg.command_handler(cmd, notify, opts)
            await tlg.command_response(cmd, result)
            logger.debug(f">> OUT message to {cmd.user_name}:\n{result}")
    logger.info("Telegram shutdown")

def main(argv):
    from optparse import OptionParser

    parser = OptionParser(usage = "usage: %prog --token=val [options]")
    parser.add_option("-d", "--debug", action = "store_true", default = False,
                      help = "Enable debug information")
    parser.add_option("-t", "--token", action = "store", type = "string",
                      help = "Telegram token")
    parser.add_option("-a", "--access_list", action = "callback", type = "string",
                      callback = lambda o, _, v, p: setattr(p.values, o.dest, v.split(',')),
                      help = "User permission list")
    parser.add_option("-u", "--uid_table", action = "store", type = "string",
                      help="Path to file with table translation of uid's in json format")
    (opts, _) = parser.parse_args()
    if not opts.token:
        parser.error('--token option requires an argument')

    if opts.uid_table:
        with open(opts.uid_table, 'r') as f:
            opts.uid_table = json.loads(f.read())

    logging.basicConfig(
        format = "[%(asctime)-15s] %(levelname)-7s:%(funcName)-8s:%(lineno)-4s %(message)s",
    )
    logger.setLevel(logging.DEBUG if opts.debug else logging.WARNING)

    logger.info("~~MLT Boot~~")
    loop = asyncio.get_event_loop()
    notify = Notify()
    try:
        task = [
            asyncio.ensure_future(router(loop, NotifyReader(notify), opts)),
            asyncio.ensure_future(telega(loop, NotifyWriter(notify), opts)),
        ]
        loop.run_until_complete(asyncio.gather(*task))
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        logger.info("finish")

if __name__ == '__main__':
     main(sys.argv)
