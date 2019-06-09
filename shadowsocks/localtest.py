import sys
import os
import signal
import select
import time
import argparse
import subprocess
from subprocess import Popen, PIPE
from os import listdir,environ
from sys import argv
from os.path import isfile,join
import re
import random

HEADER = '\033[95m'
OKBLUE = '\033[94m'
OKGREEN = '\033[92m'
WARNING = '\033[93m'
FAIL = '\033[91m'
ENDC = '\033[0m'
BOLD = '\033[1m'
UNDERLINE = '\033[4m'

python = ['python3']
local_port = 8089

def single_test (config, ssr=None):
    client_args = python + ['shadowsocks/local.py', '-v']
    if ssr:
        client_args.extend(['-c', ssr])
        local_port = random.randint(8089,9000)
        client_args.extend(['-l', str(local_port)])
    p1 = Popen(client_args, stdin=PIPE, stdout=PIPE, stderr=PIPE, close_fds=True)
    p3 = None
    p4 = None
    stage = 1

    try:
        local_ready = False
        fdset = [p1.stdout, p1.stderr]
        while True:
            r, w, e = select.select(fdset, [], fdset)
            if e:
                return False

            for fd in r:
                line = fd.readline()
                if not line:
                    if stage == 2 and fd == p3.stdout:
                        stage = 3
                    if stage == 4 and fd == p4.stdout:
                        stage = 5
                if bytes != str:
                    line = str(line, 'utf8')
                # if line.find('DEBUG') < 0 and line.find('INFO') < 0:
                sys.stderr.write(line)
                if line.find('starting local') >= 0:
                    local_ready = True
            
            if stage == 1 and local_ready:
                time.sleep(1)
                p3 = Popen(['curl', 'http://ip-api.com/json',
                        '-x', f'socks5h://localhost:{local_port}',
                        '-m', '15', '--connect-timeout', '5', '-s'],
                        stdin=PIPE, stdout=PIPE, stderr=PIPE, close_fds=True)
                if p3 is not None:
                    fdset.append(p3.stdout)
                    fdset.append(p3.stderr)
                    stage = 2
                else:
                    return False     

            if stage == 3 and p3 is not None:
                fdset.remove(p3.stdout)
                fdset.remove(p3.stderr)
                r = p3.wait()
                if config.should_fail:
                    if r == 0:
                        sys.exit(1)
                else:
                    if r != 0:
                        return False
                return True
                if p4 is not None:
                    fdset.append(p4.stdout)
                    fdset.append(p4.stderr)
                    stage = 4
                else:
                    return False

            if stage == 5:
                r = p4.wait()
                if config.should_fail:
                    if r == 0:
                        return False
                    print('test passed (expecting failure)')
                else:
                    if r != 0:
                        # sys.exit(1)
                        return False
                    # print('test passed')
                    return True
                break
    except:
        return False
    finally:
        for p in [p1]:
            try:
                os.kill(p.pid, signal.SIGINT)
                os.waitpid(p.pid, 0)
            except OSError:
                pass

def kill_old_process():
    output = subprocess.check_output("ps -aux | grep shadowsocks/local.py | grep -v grep | awk '{print $2}'", shell=True)
    pid = output.decode().rstrip()
    if re.match('\d+', pid):
        try:
            os.kill(int(pid), signal.SIGINT)
            os.waitpid(int(pid), 0)
        except OSError:
            pass

def main():
    kill_old_process()
    parser = argparse.ArgumentParser(description='test local')
    parser.add_argument('-c', '--client-conf', type=str, default=None)
    parser.add_argument('--should-fail', action='store_true', default=None)
    parser.add_argument('--tcp-only', action='store_true', default=None)
    config = parser.parse_args()
    ssrs = [f"./json/{f}" for f in listdir('./json') if isfile(join('./json', f)) and re.match('(.*\.ssr$)|(.*\.json$)', f) ]
    # process pool
    for ssr in ssrs:
        test_result = single_test(config, ssr)
        print(f"{OKGREEN if test_result else FAIL}{ssr}:{test_result}{ENDC}")


if __name__ == '__main__':
    main()
