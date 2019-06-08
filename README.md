Improved ShadowsocksR Client
===========

Feature
------
* Load from a .ssr file with a ssr:// link
* Run from a ssr:// link
* Asyncio based eventloop
* Query DNS on startup
* Support test multiple ssr config

Example
------
To run from a SSR link:

    python3 ./shadowsocks/local.py -L ssr://ABCDEFGHIJKLMNOPQRSTUVWXYZ

To run from a config file:

    python3 ./shadowsocks/local.py -L /path/to/config.json