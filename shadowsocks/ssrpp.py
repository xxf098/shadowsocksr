#!/usr/bin/python3
import sys
import os
import re
from os import listdir
from os.path import isfile, join, basename
from pathlib import Path
import subprocess
import errno
from urllib.request import urlopen, Request
from urllib.parse import urlsplit
import base64
import curses
import argparse
import traceback

FZF = 'fzf'
BASE_DIR = f'{str(Path.home())}/shadowsocksr'
DEFAULT_SSR_DIR = f'{BASE_DIR}/json/'
SSR_LINK_REGEX = '^ssr?://[a-zA-Z0-9]+'
JSON_FILE_REGEX = '.*\.json$'
SSR_FILE_REGEX = '.*\.ssr$'

FG_COLORS = {
    "black"   : curses.COLOR_BLACK,
    "red"     : curses.COLOR_RED,
    "green"   : curses.COLOR_GREEN,
    "yellow"  : curses.COLOR_YELLOW,
    "blue"    : curses.COLOR_BLUE,
    "magenta" : curses.COLOR_MAGENTA,
    "cyan"    : curses.COLOR_CYAN,
    "white"   : curses.COLOR_WHITE,
    "gray"    : 8
}
BG_COLORS = dict(("on_" + name, value) for (name, value) in FG_COLORS.items())
FG_COLORS["default"]    = curses.COLOR_WHITE
BG_COLORS["on_default"] = curses.COLOR_BLACK
COLOR_COUNT = len(FG_COLORS)
ATTRS = {
    "altcharset" : curses.A_ALTCHARSET,
    "blink"      : curses.A_BLINK,
    "bold"       : curses.A_BOLD,
    "dim"        : curses.A_DIM,
    "normal"     : curses.A_NORMAL,
    "standout"   : curses.A_STANDOUT,
    "underline"  : curses.A_UNDERLINE,
    "reverse"    : curses.A_REVERSE,
}

keyword_style = ("yellow", "bold")
highlight_style = ("on_gray", "cyan", "bold")
leader_style = ("magenta", "bold")


def get_attributes(attrs):
    for attr in attrs:
        if attr in ATTRS:
            yield ATTRS[attr]

def get_fg_color(attrs):
    for attr in attrs:
        if attr in FG_COLORS:
            return FG_COLORS[attr]
    return FG_COLORS["default"]

def get_bg_color(attrs):
    for attr in attrs:
        if attr in BG_COLORS:
            return BG_COLORS[attr]
    return BG_COLORS["on_default"]

class Display():

    def __init__(self, screen, data):
        self.stdscr = screen
        self.WIDTH = 0
        self.HEIGHT = 0
        self.PROMPT = 'QUERY> '
        self.has_default_colors = False
        self.selected = 0

        self.data = data
        self.filter_data = data

        self.stdscr.keypad(True)
        curses.raw()
        curses.noecho()
        curses.cbreak()
        curses.nonl()

        curses.start_color()
        curses.use_default_colors()
        FG_COLORS["default"]    = -1
        BG_COLORS["on_default"] = -1
        self.init_color_pairs()
        self.HEIGHT, self.WIDTH = self.stdscr.getmaxyx()
        self.MAX_DISPLAY_COUNT = self.HEIGHT - 2

        self.keyword_style = self.attrs_to_style(keyword_style)
        self.keyword_highlight_style = self.attrs_to_style(keyword_style + highlight_style)
        self.highlight_style = self.attrs_to_style(highlight_style)
        self.leader_style = self.attrs_to_style(leader_style)

        self.return_result = None

    def get_normalized_number(self, number):
        return COLOR_COUNT if number < 0 else number

    def get_pair_number(self, fg, bg):
        if self.has_default_colors:
            # Assume the number of colors is up to 16 (2^4 = 16)
            return self.get_normalized_number(fg) | (self.get_normalized_number(bg) << 4)
        else:
            return self.get_normalized_number(fg) + self.get_normalized_number(bg) * COLOR_COUNT

    def get_color_pair(self, fg, bg):
        return curses.color_pair(self.get_pair_number(fg, bg))

    def attrs_to_style(self, attrs):
        if attrs is None:
            return 0

        style = self.get_color_pair(get_fg_color(attrs), get_bg_color(attrs))
        for attr in get_attributes(attrs):
            style |= attr

        return style

    def display_results(self, query, k):
        start_y = 1
        if k == 'KEY_BACKSPACE':
            query = query[0:-1]
        elif k == 'KEY_DOWN':
            self.selected = min(self.selected+1, len(self.data)-1)
        elif k == 'KEY_UP':
            self.selected = max(0, self.selected-1)
        elif k is not None:
            query = query + k
            self.selected = 0
        filter_results = [ x for x in self.data if re.match(f'.*{query}.*', x, re.I)] if len(query) > 0 else self.data
        self.filter_results = filter_results
        self.selected = min(self.selected, len(filter_results) - 1)
        begin = 0
        if len(filter_results) > self.MAX_DISPLAY_COUNT:
            begin = self.selected-self.MAX_DISPLAY_COUNT if self.selected > self.MAX_DISPLAY_COUNT else 0 
        index = begin
        query_len = len(query)
        max_count = min(begin + self.MAX_DISPLAY_COUNT, len(filter_results)-1)
        preview_results = preview_ssr(filter_results[self.selected]) if len(filter_results) > 0 else []
        preview_start = int(self.WIDTH / 3)
        while index <= max_count:
            result = filter_results[index]
            pos_y = start_y + index - begin
            prev_start = 1
            is_selected = index == self.selected
            line_style = self.highlight_style if is_selected else 0
            leader = '>' if is_selected else ' '
            index = index + 1
            self.stdscr.addnstr(pos_y, 0, leader, 1, self.leader_style)
            # preview ssr
            if index-begin < len(preview_results):
                preview_result = preview_results[index-begin-1]
                self.stdscr.addnstr(pos_y, preview_start, preview_result, self.WIDTH - preview_start)
            if len(query) == 0:
                self.stdscr.addnstr(pos_y, prev_start, result, self.WIDTH, line_style)
                continue
            self.stdscr.addnstr(pos_y, prev_start, result, self.WIDTH, line_style)
            start = result.lower().find(query)
            kw_style = self.keyword_highlight_style if is_selected else self.keyword_style
            while start != -1:
                self.stdscr.addnstr(pos_y, start + 1, result[start:start+query_len], query_len, kw_style)
                prev_start = start + query_len
                start = result.find(query, prev_start)

    def display_prompt(self, k):
        if k is not None:
            if k == 'KEY_BACKSPACE' and len(self.PROMPT) > 7:
                self.PROMPT = self.PROMPT[0:-1]
            elif k == 'KEY_DOWN':
                pass
            elif k == 'KEY_UP':
                pass
            elif re.match('^[a-zA-Z0-9]$', k):
                self.PROMPT = f"{self.PROMPT}{k}"
        self.stdscr.addnstr(0, 0, self.PROMPT, self.WIDTH)
        self.stdscr.move(0, len(self.PROMPT))

    def init_color_pairs(self):
        for fg_s, fg in FG_COLORS.items():
            for bg_s, bg in BG_COLORS.items():
                if not (fg == bg == 0):
                    curses.init_pair(self.get_pair_number(fg, bg), fg, bg)

    def draw_screen(self):
        self.stdscr.clear()
        self.stdscr.addstr(self.PROMPT)
        k = None
        while True:
            self.stdscr.erase()
            if k == '\r': # return
                self.stdscr.refresh()
                return self.filter_results[self.selected]
            self.handle_key(k)
            k = self.stdscr.getkey()

    def handle_key(self, k):
        if k == '\x18': # ctrl-x
            self.stdscr.refresh()
            exit(0)
        if k == '\x04': # ctrl-d
            filename = self.filter_results[self.selected]
            remove_ssr(filename)
            self.rename_ssr(filename)
            k= ''
        if k == '\x02': #ctrl-b
            self.PROMPT = self.PROMPT[0:7]
            k = ''
        if k == '\x1b':
            k = ''
        self.display_results(self.PROMPT[7:], k)
        self.display_prompt(k)
        self.stdscr.refresh()

    #TODO: fix problems
    def rename_ssr(self, removed_name):
        self.filter_results.pop(self.selected)
        if removed_name in self.data:
            self.data.remove(removed_name)
        if re.match(SSR_FILE_REGEX, removed_name):
            regex_name = re.sub('\._\d+_\.', '._(\d+)_.', removed_name)
            number_match = match_multiple_links_filename(removed_name)
            removed_index = int(number_match.group(1))
            for index, name in enumerate(self.filter_results):
                number_match = re.match(regex_name, name)
                if number_match and int(number_match.group(1)) > removed_index:
                    number = number_match.group(1)
                    self.filter_results[index] = re.sub('\._\d+_\.', f'._{int(number_match.group(1))-1}_.', name)

class Style:

    def __init__(self):
        self.has_default_colors = False

    def attrs_to_style(self, attrs):
        if attrs is None:
            return 0

        style = self.get_color_pair(get_fg_color(attrs), get_bg_color(attrs))
        for attr in get_attributes(attrs):
            style |= attr

        return style

    def get_normalized_number(self, number):
        return COLOR_COUNT if number < 0 else number

    def get_pair_number(self, fg, bg):
        if self.has_default_colors:
            # Assume the number of colors is up to 16 (2^4 = 16)
            return self.get_normalized_number(fg) | (self.get_normalized_number(bg) << 4)
        else:
            return self.get_normalized_number(fg) + self.get_normalized_number(bg) * COLOR_COUNT

    def get_color_pair(self, fg, bg):
        return curses.color_pair(self.get_pair_number(fg, bg))

    def setup_color(self):
        curses.start_color()
        curses.use_default_colors()
        FG_COLORS["default"]    = -1
        BG_COLORS["on_default"] = -1
        for fg_s, fg in FG_COLORS.items():
            for bg_s, bg in BG_COLORS.items():
                if not (fg == bg == 0):
                    curses.init_pair(self.get_pair_number(fg, bg), fg, bg)

class SinglePanelDispaly:

    def __init__(self, parent, panel_index, lines=[], left_panel=None):
        self.parent = parent
        self.parent_screen = self.parent.screen
        self.screen = self.parent_screen.derwin(0,0,0,0)
        self.height, self.width = 0, 0
        self.lines = []
        self.panel_index = panel_index
        self.focused = False
        self.left_panel = left_panel
        self.keymap = {
            'KEY_DOWN': self.handle_key_down,
            'KEY_UP': self.handle_key_up,
            'KEY_LEFT': self.handle_key_left,
            'KEY_RIGHT': self.handle_key_right
        }
        self.highlight_style = Style().attrs_to_style(highlight_style)
        self.highlight_index = 0
        self._setup_data()

    def resize(self, start_y, start_x, height, width):
        self.screen.resize(height, width)
        parent_y, parent_x = self.screen.getparyx()
        if start_y != parent_y or start_x != parent_y:
            self.screen.mvderwin(start_y, start_x)
        self.height, self.width = height, width

    def draw(self):
        self._setup_data()
        for line, i in zip(self.lines, range(self.height)):
            style = 0
            if self.focused and i == self.highlight_index:
                style = self.highlight_style
                line = line + ' ' * max(self.width-len(line) - 1, 0)
            self.screen.addnstr(i, 0, line, self.width, style)

    def handle_key_down(self):
        self.highlight_index = min(len(self.lines)-1, self.highlight_index + 1)

    def handle_key_up(self):
        self.highlight_index = max(0, self.highlight_index - 1)

    def handle_key_left(self):
        self.parent.change_foucs(-1)

    def handle_key_right(self):
        self.parent.change_foucs(1)

    def handle_key(self, key):
        if not self.focused:
            return
        if key not in self.keymap:
            return
        self.keymap[key]()

    def _setup_data(self):
        pass

    def get_right(self):
        pass

# add cache
class LeftPanelDispaly(SinglePanelDispaly):

    def _setup_data(self):
        ssrs = get_path_by_time(self.parent.ssr_dir)
        ssrs = [basename(x) for x in ssrs]
        self.lines = ssrs

    def sub_data(self):
        ssr_name = self.lines[self.highlight_index]
        if re.match(JSON_FILE_REGEX, ssr_name):
            return [ssr_name]
        if re.match(SSR_FILE_REGEX, ssr_name):
            ssr_name = f'{self.parent.ssr_dir}/{ssr_name}'
            return get_ssrnames([ssr_name])
        return []

    def get_selectd(self):
        return self.lines[self.highlight_index]

class MiddlePanelDispaly(SinglePanelDispaly):

    def _setup_data(self):
        lines = self.left_panel.sub_data()
        self.lines = lines

    def sub_data(self):
        ssr_name = self.lines[self.highlight_index]
        return preview_ssr(ssr_name)

    def get_selectd(self):
        return self.lines[self.highlight_index]

class RightPanelDispaly(SinglePanelDispaly):

    def _setup_data(self):
        lines = self.left_panel.sub_data()
        self.lines = lines

#TODO: signal publish sub
class MultiPanelDisplay:

    def __init__(self, screen):
        self.ratios = [0.3, 0.25, 0.45]
        self.stop = False
        self.selected_server = ''
        self.screen = screen
        self.height, self.width = self.screen.getmaxyx()
        self.panels = []
        self.ssr_dir = DEFAULT_SSR_DIR
        self._setup_curses()
        self._setup_color()
        self.rebuld()

    def _setup_curses(self):
        self.screen.keypad(True)
        curses.raw()
        curses.noecho()
        curses.cbreak()
        curses.nonl()
        curses.curs_set(0)

    def _setup_color(self):
        Style().setup_color()

    def rebuld(self):
        left = LeftPanelDispaly(self, 0)
        middle = MiddlePanelDispaly(self, 1, left_panel=left)
        middle.focused = True
        right = RightPanelDispaly(self, 2, left_panel=middle)
        self.panels.extend([left, middle, right])
        self.resize()

    def resize(self):
        top, left = 0, 0
        for i, ratio in enumerate(self.ratios):
            width = int(self.width * ratio)
            self.panels[i].resize(top, left, self.height, width)
            left += width

    def draw(self):
        self.screen.clear()
        k = None
        while not self.stop:
            self.screen.erase()
            for panel in self.panels:
                panel.draw()
            self.handle_key()
            self.screen.refresh()
        # change to callback on_result
        return self.selected_server

    def handle_key(self):
        # down KEY_DWON up key_UP left KEY_LEFT right KEY_RIGHT
        key = self.screen.getkey()
        if key == '\r':
            self.stop = True
            self.selected_server = self.panels[1].get_selectd()
        if key not in ['KEY_DOWN', 'KEY_UP', 'KEY_LEFT', 'KEY_RIGHT']:
            return
        for panel in self.panels:
            panel.handle_key(key)

    # direction -1 left 1 right
    def change_foucs(self, direction):
        is_first_foucs = self.panels[0].focused
        if is_first_foucs and direction == 1:
            # self.panels[0].highlight_index = 0
            self.panels[0].focused = False
            self.panels[1].focused = True
        if not is_first_foucs and direction == -1:
            self.panels[1].highlight_index = 0
            self.panels[0].focused = True
            self.panels[1].focused = False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('dir', nargs='?', default=DEFAULT_SSR_DIR)
    parser.add_argument('-p', '--preview')
    parser.add_argument('-s', '--sub')
    parser.add_argument('--name', default=None)
    args = parser.parse_args()
    if args.preview:
        return preview_ssr(args.preview)
    if args.sub:
        return add_subscription(sys.argv[2], args.name)
    ssr_dir = args.dir
    if not os.path.isdir(ssr_dir):
        raise Exception('Path is not a directory')
    ssrs = get_path_by_time(ssr_dir)
    ssr_names = get_ssrnames(ssrs)
    selected_server = select_ssr_names(ssr_names)
    if selected_server is None:
        return
    cmd = build_cmd(selected_server, ssr_dir)
    os.system(cmd)

# TODO: support user pass directory path
# TODO: refactor
# TODO: Sort by modify time
# TODO: JSON Format
ssr_cache = {}
def preview_ssr(filename):
    ssr_dir = DEFAULT_SSR_DIR
    origin_filename = filename
    if origin_filename in ssr_cache:
        return ssr_cache[filename]
    multiple_match = match_multiple_links_filename(filename)
    filename = re.sub('_\d+_\.', '', filename)
    filepath = f'{ssr_dir}{filename}'
    lines = []
    result = []
    if isfile(filepath):
        with open(filepath) as f:
            lines = f.readlines()
    if len(lines) == 0:
        return
    if re.match(JSON_FILE_REGEX, filename):
        for line in lines:
            if re.match('(.*password.*)|(.*server_port.*)', line):
                continue
            result.append(line.rstrip())
        ssr_cache[origin_filename] = result
        return result
    if re.match(SSR_FILE_REGEX, filename):
        line_num = 0 if not multiple_match else int(multiple_match.group(1))
        ssr_link = lines[line_num - 1].rstrip()
        cmd = ['python3', f'{BASE_DIR}/shadowsocks/ssrlink.py', ssr_link]
        output = subprocess.check_output(cmd)
        result.extend(output.decode('utf-8').split('\n'))
        ssr_cache[origin_filename] = result
        return result
    # print(filepath)

def remove_ssr(filename):
    ssr_dir = DEFAULT_SSR_DIR
    del ssr_cache[filename]
    multiple_match = match_multiple_links_filename(filename)
    filename = re.sub('_\d+_\.', '', filename)
    filepath = f'{ssr_dir}{filename}'
    if re.match(JSON_FILE_REGEX, filename) and isfile(filename):
        os.remove(filepath)
    if re.match(SSR_FILE_REGEX, filename):
        lines = []
        if isfile(filepath):
            with open(filepath) as f:
                lines = f.readlines()
        if len(lines) < 2:
            os.remove(filepath)
            return
        if not multiple_match:
            return
        line_num = int(multiple_match.group(1))
        lines.pop(line_num-1)
        with open(filepath, "w") as f:
            for line in lines:
                    f.write(line)

url_pattern = re.compile(
        r'^(?:http|ftp)s?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE
        )
def add_subscription(url, new_filename=None):
    if re.match(url_pattern, url):
        add_subscription_from_url(url)
    else:
        add_subscription_from_file(url, new_filename)

def add_subscription_from_file(src_file, new_filename):
    if not isfile(src_file):
        raise Exception(f'Invalid filepath: {src_file}')
    if new_filename is None:
        filename = basename(src_file)
        filename = re.sub('\.txt$', '.ssr', filename)
    else:
        filename = new_filename
    with open(src_file, 'r') as f:
        data = f.read()
    write_ssr_data_to_file(data, filename)

def add_subscription_from_url(url):
    data = request_url(url)
    write_ssr_data_to_file(data, f"{urlsplit(url).netloc}.ssr")

def write_ssr_data_to_file(data, filename):
    if not data.endswith('=='):
        data = data + '=='
    decode_data = base64.b64decode(data)
    filename = f'{DEFAULT_SSR_DIR}{filename}'
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(decode_data.decode('utf-8'))

def request_url(url):
    if not re.match(url_pattern, url):
        raise Exception(f'Invalid url {url}')
    req = Request( url, data=None,
    headers={ 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/35.0.1916.47 Safari/537.36' }
    )
    r = urlopen(req)
    data = r.read().decode(r.info().get_param('charset') or 'utf-8')
    return data

#TODO: more sort method
def get_path_by_time(dir):
    ssrs = []
    with os.scandir(dir) as it:
        ssrs = [(entry.path, entry.stat().st_mtime) for entry in it if entry.is_file() if re.match('(.*\.ssr$)|(.*\.json$)', entry.name) ]
        ssrs.sort(key=lambda x: x[1], reverse=True)
        ssrs = [x[0] for x in ssrs]
    return ssrs

ssr_names_cache = {}
def get_ssrnames(ssrs):
    ssr_names = []
    for ssr in ssrs:
        filename = basename(ssr)
        if filename in ssr_names_cache:
            return ssr_names_cache[filename]
        if re.match(JSON_FILE_REGEX, ssr):
            ssr_names.append(filename)
        if re.match(SSR_FILE_REGEX, ssr):
            with open(ssr) as f:
                lines = f.readlines()
                if len(lines) == 1:
                    ssr_names.append(filename)
                    continue
                name_parts = filename.split('.')
                name_parts.insert(-1, '0')
                new_names = []
                for line in lines:
                    if re.match(SSR_LINK_REGEX, line):
                        name_parts[-2] = '_' + str(len(new_names) + 1) + '_'
                        new_names.append('.'.join(name_parts))
                ssr_names.extend(new_names)
                ssr_names_cache[filename] = new_names
    return ssr_names

def select_ssr_names(names):
    try:
        screen = curses.initscr()
        display = MultiPanelDisplay(screen)
        display.rebuld()
        result = display.draw()
        return result
    finally:
        screen.keypad(0)
        curses.echo()
        curses.nocbreak()
        curses.endwin()

def build_cmd(ssr_name, ssr_dir):
    cmd = f'python3 {BASE_DIR}/shadowsocks/local.py'
    if re.match(JSON_FILE_REGEX, ssr_name):
        cmd = f'{cmd} -c {ssr_dir}{ssr_name}'
    if re.match(SSR_FILE_REGEX, ssr_name):
        match = match_multiple_links_filename(ssr_name)
        if not match:
            cmd = f'{cmd} -c {ssr_dir}{ssr_name}'
        else:
            line_num = match.group(1)
            line_num = int(line_num)
            ssr_name = re.sub('_\d+_\.', '', ssr_name)
            ssr_path = f'{ssr_dir}{ssr_name}'
            with open(ssr_path) as f:
                lines = f.readlines()
                ssr_link = lines[line_num - 1].rstrip()
                if re.match(SSR_LINK_REGEX, ssr_link):
                    cmd = f'{cmd} -L {ssr_link}'
    return cmd

def match_multiple_links_filename(filename):
    match = re.match('.*\._(\d+)_\.ssr?$', filename)
    return match

# TODO: confirm handle all key fuzzy search
# TODO: Display as a pip local module
# TODO: Update every 1s range
# TODO: count call_back delete
if __name__ == '__main__':
    main()
