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
* Eventloop based http proxy server by forward to shadowsocks with ssrforward.py

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
    ./shadowsocks/ssrpp.py --sub /path/to/ssrlinks.txt

Start http proxy by forward to shadowsocks

    python3 ./shadowsocks/ssrforward.py

Shortcuts
------

* Delete a item: Ctrl-d

* Copy SSR link: Ctrl-y

* Exit program: Ctrl-c
