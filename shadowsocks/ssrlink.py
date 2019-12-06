from __future__ import absolute_import, division, print_function, \
    with_statement
import sys
import re
import base64
import json

__all__ = (
    'parseLink',
    'addPadding'
)

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


def DecodeUrlSafeBase64(val, exitOnError=True):
    val = val.replace('-', '+').replace('_', '/')
    return decodeToStr(addPadding(val), exitOnError)


def ParseParam(param_str):
    params_dict = {}
    obfs_params = param_str.split('&')
    for p in obfs_params:
        if p.find('=') > 0:
            keyVal = p.split('=')
            key = keyVal[0]
            val = keyVal[1]
            params_dict[key] = val
    return params_dict

# ssr://host:port:protocol:method:obfs:base64pass/?obfsparam=base64&remarks=base64&group=base64&udpport=0&uot=1


def parseSSR(link, local_port=None):
    ssrMatch = re.match(r'^ssr?://([A-Za-z0-9_-]+)', link, re.I)
    if not ssrMatch:
        exit(1)
    data = DecodeUrlSafeBase64(ssrMatch.group(1))
    params_dict = {}
    param_start_pos = data.find('?')
    if param_start_pos > 0:
        params_dict = ParseParam(data[param_start_pos+1:])
        data = data[0:param_start_pos]
    if data.find('/') > 0:
        data = data[0:data.rindex('/')]

    match = re.match(r'^(.+):([^:]+):([^:]*):([^:]+):([^:]*):([^:]+)', data)
    if not match:
        # TODO: throw error
        exit(1)
    server = match.group(1)
    server_port = int(match.group(2))
    protocol = 'origin' if len(match.group(3)) == 0 else match.group(3)
    protocol = protocol.replace('_compatible', '')
    method = match.group(4)
    obfs = 'plain' if len(match.group(5)) == 0 else match.group(5)
    obfs.replace("_compatible", "")
    password = DecodeUrlSafeBase64(match.group(6))
    config = {
        'server': server,
        'server_port': server_port,
        'local_address': '127.0.0.1',
        'local_port': local_port if local_port is not None else 8088,
        'protocol': protocol,
        'method': method,
        'obfs': obfs,
        'password': password
    }
    if 'protoparam' in params_dict:
        protocolparam = DecodeUrlSafeBase64(params_dict['protoparam'])
        config['protocol_param'] = protocolparam
    if 'obfsparam' in params_dict:
        obfsparam = DecodeUrlSafeBase64(params_dict['obfsparam'])
        config['obfs_param'] = obfsparam
    if 'remarks' in params_dict:
        remarks = DecodeUrlSafeBase64(params_dict['remarks'])
        config['remarks'] = remarks
    if 'group' in params_dict:
        group = DecodeUrlSafeBase64(params_dict['group'])
        config['group'] = group
    # 'uot', 'udpport'
    return config


def parseSS(ssURL):
    UrlFinder = re.compile(r'^(?i)ss://([A-Za-z0-9+-/=_@:]+)(#(.+))?', re.I)
    DetailsParser = re.compile(
        r'^((?P<method>.+):(?P<password>.*)@(?P<hostname>.+?):(?P<port>\\d+?))$', re.I)
    match = UrlFinder.match(ssURL)
    if not match:
        raise Exception('FormatException ss')
    base64 = match.group(1)
    match = DetailsParser.match(base64)
    if not match:
        raise Exception('Not Supported Link')
    protocol = 'origin'
    method = match.group('method')
    password = match.group('password')
    server = match.group('hostname')
    server_port = match.group('port')
    group = ""
    config = {
        'protocol': protocol,
        'method': method,
        'password': password,
        'server': server,
        'server_port': server_port,
        'group': group
    }
    return config


def parse_vmess(vmess_link, local_port):
    vmess_match = re.match(r'^vmess://([\w+]+=*)', vmess_link)
    if not vmess_match:
        return None
    data = vmess_match.group(1)
    data = addPadding(data)
    result = base64.b64decode(data).decode('utf-8')
    vmess_config = json.loads(result)
    default_config = {}
    default_config['log'] = {'access': '', 'error': '', 'loglevel': 'error'}
    default_config['dns'] = {'servers': ['1.0.0.1', 'localhost']}
    default_config['routing'] = {
        'domainStrategy': 'IPIfNonMatch',
        'rules': [
            {
                'type': 'field',
                'ip': ['geoip:private', 'geoip:cn'],
                'outboundTag': 'direct'
            },
            {
                'type': 'field',
                'domain': ['geosite:cn'],
                'outboundTag': 'direct'
            }
        ]
    }
    default_config['inbounds'] = [
        {
            'tag': 'socks-in',
            'port': 8088,
            'listen': '::',
            'protocol': 'socks',
            'settings': {'auth': 'noauth', 'udp': True, 'ip': '127.0.0.1'}
        },
        {
            'tag': 'http-in',
            'port': 8090,
            'listen': '::',
            'protocol': 'http'
        }
    ]
    default_config['outbounds'] = [
        {
            'protocol': 'freedom',
            'tag': 'direct',
            'settings': {'domainStrategy': 'UseIP'}
        }
    ]
    vmess = {
        'protocol': 'vmess',
        'description': vmess_config.get('ps', ''),
        'settings': {'vnext': [
            {
                'address': vmess_config['add'],
                'port': int(vmess_config['port']),
                'users': [
                     {
                         'email': 'user@v2ray.com',
                         'id': vmess_config['id'],
                         'alterId': int(vmess_config['aid']),
                         'security': 'auto'
                     }
                     ]
            }
        ]},
        'mux': {'enabled': True},
        'tag': 'proxy',
        'streamSettings': {}
    }
    if vmess_config['net'] == 'ws':
        streamSettings = {
            'network': vmess_config['net'],
            'wsSettings': { 'connectionReuse': True, 'path': vmess_config['path'] }
        }
        if vmess_config['host']:
            streamSettings['wsSettings']['headers'] = {'Host': vmess_config['host']}
        vmess['streamSettings'] = streamSettings
    if vmess_config['tls'] == 'tls':
        vmess['streamSettings']['security'] = 'tls'
        vmess['streamSettings']['tlsSettings'] = {'allowInsecure': True}
    default_config['outbounds'].insert(0, vmess)
    # print(json.dumps(default_config, indent=4, ensure_ascii=False))
    return default_config

def parseLink(link, local_port=None):
    if re.match(r'^ss://', link, re.I):
        return parseSS(link)
    if re.match(r'^ssr://', link, re.I):
        return parseSSR(link, local_port)
    if re.match(r'^vmess://', link, re.I):
        return parse_vmess(link, local_port)
    raise Exception('Not Supported Link')


if __name__ == '__main__':
    # print("Hello World")
    if len(sys.argv) != 2:
        exit()
    base64_link = sys.argv[1]
    config = {}
    if re.match(r'^ss://', base64_link, re.I):
        config = parseSS(base64_link)
    elif re.match(r'^ssr://', base64_link, re.I):
        config = parseSSR(base64_link)
    elif re.match(r'^vmess://', base64_link, re.I):
        config = parse_vmess(base64_link, None)
    else:
        raise Exception('Not Supported Link')
    print(json.dumps(config, indent=4, ensure_ascii=False))
