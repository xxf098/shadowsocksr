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

SSR Client
------
![alt text](https://raw.githubusercontent.com/xxf098/shadowsocksr/xxf098/master/img/ssrpp.jpg)

Example
------
To run from a SSR link:

    python3 ./shadowsocks/local.py -L ssr://ABCDEFGHIJKLMNOPQRSTUVWXYZ

To run from a config file:

    python3 ./shadowsocks/local.py -L /path/to/config.json

Start ssr client

    ./shadowsocks/ssrpp.py

Add ssr subscription

    ./shadowsocks/ssrpp.py --sub https://subscription.ssr.com
