from __future__ import absolute_import, division, print_function, \
    with_statement
    
import sys
import re
import base64
import json

# def paddingB64Str(s):
#     return s + '=' * (-len(s) % 4)

def addPadding(s):
    return s + '=' * (-len(s) % 4)

def decodeToStr(encodeData, exitOnError=False):
    result = ''
    try:
        result = base64.b64decode(encodeData).decode('utf-8')
    except:
        print('Error:', sys.exc_info()[0])
        if exitOnError:
            exit(1)
        pass
    return result

def parseKeyVal(keyVal):
    key = keyVal[0]
    val = keyVal[1]
    if len(keyVal[1]) < 1:
        return ""
    if key == 'remarks' or key == 'group':
        return decodeToStr(addPadding(val))
    return val

def parseSSR(link):
    isSSRLink = re.match(r'^ssr?://', link, re.I)
    if not isSSRLink:
        exit(1)
    encodeData = re.sub(r'^ssr?://', '', link, re.I)
    decodeData = decodeToStr(addPadding(encodeData), exitOnError=True)
    splits = decodeData.split('/?')
    baseInfo = splits[0]
    extraInfo = splits[1] if len(splits) > 1 else ""
    values = baseInfo.split(':')
    config = {
        'server': values[0],
        'server_port': values[1],
        'local_address': '127.0.0.1',
        'local_port': 8088,
        'timeout': 300,
        'workers': 1,
        'protocol': values[2],
        'method': values[3],
        'obfs': values[4],
        'password': decodeToStr(addPadding(values[5]))
    }
    entries = extraInfo.split('&')
    for entry in entries:
        keyVal = entry.split('=')
        if len(keyVal[1]) > 0:
            config[keyVal[0]] = keyVal[1]
    return config

if __name__ == '__main__':
    # print("Hello World")
    if len(sys.argv) != 2:
        exit()
    ssrLink = sys.argv[1]
    config = parseSSR(ssrLink)
    print(json.dumps(config, indent=4))