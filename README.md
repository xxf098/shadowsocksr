Improved ShadowsocksR Client
===========

Feature
------
* Load from a .ssr file with a ssr:// link
* Run from a ssr:// link
* Asyncio based eventloop
* Query DNS on startup
* Support test multiple ssr configs
* A command line ssr client ssrpp.py

Example
------
To run from a SSR link:

    python3 ./shadowsocks/local.py -L ssr://ABCDEFGHIJKLMNOPQRSTUVWXYZ

To run from a config file:

    python3 ./shadowsocks/local.py -L /path/to/config.json

Start command line client

    ./shadowsocks/ssrpp.py

Add ssr subscription

    ./shadowsocks/ssrpp.py --sub https://subscription.ssr.com
