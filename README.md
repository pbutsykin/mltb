# Multi Language Telegram Bot
Telegram bot for monitoring devices in a wifi network in real time

This bot is designed to monitor devices in the wifi network for linksys
rotuters. A vulnerability in linksys is used to obtain information about
devices in the network, so there's no need to even login to the router,
it's enough just to be in the same network as the router.

Supported commands:
Help:
d, devices - Get current devices list
r, register - Register device changes notification
u, unregister - Unregister device changes notification
h, help - Command list

Example:
```
@User:
d

@Bot:
1. Computer: LG Electronics Inc.
2. Mobile: SAMSUNG ELECTRO-MECHANICS(THAILAND)
3. Phone: Apple Inc.

@User:
r

@Bot:
notification registered

>> Mobile: SAMSUNG ELECTRO-MECHANICS(THAILAND) (offline)
```
