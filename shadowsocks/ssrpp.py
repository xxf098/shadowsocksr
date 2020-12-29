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
import asyncio
from functools import reduce
import operator
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
import ssrlink
import socket
from struct import pack, unpack
from collections import deque
import json
import time
import concurrent.futures

FZF = 'fzf'
BASE_DIR = f'{str(Path.home())}/shadowsocksr'
DEFAULT_SSR_DIR = f'{BASE_DIR}/json/'
V2RAY_DIR= f'{str(Path.home())}/v2ray/'
SSR_LINK_REGEX = '^ssr?://[a-zA-Z0-9]+'
VMESS_LINK_REGEX = '^vmess://[a-zA-Z0-9\n]+'
JSON_FILE_REGEX = '.*\.json$'
SSR_FILE_REGEX = '.*\.ssr$'
VMESS_FILE_REGEX = '.*\.vmess$'
DEFAULT_PROXY = 'https://127.0.0.1:8087'

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
highlight_style_not_focus = ("on_gray", "white")
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

class Setting:

    def __init__(self):
        self.hide_fields = ['password', 'server_port']
        self.basedir = BASE_DIR
        self.ratios = [0.35, 0.25, 0.4]

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
        self.height, self.width, self.x, self.y = 0, 0, 0, 0
        self.lines = []
        self.panel_index = panel_index
        self.focused = False
        self.left_panel = left_panel
        self.padding = 1
        self.need_redraw = True
        self.highlight_style = Style().attrs_to_style(highlight_style)
        self.highlight_style_not_focus = Style().attrs_to_style(highlight_style_not_focus)
        self.highlight_index = 0
        self._setup_data()

    def resize(self, start_y, start_x, height, width):
        self.screen.resize(height, width)
        parent_y, parent_x = self.screen.getparyx()
        if start_y != parent_y or start_x != parent_y:
            self.screen.mvderwin(start_y, start_x)
        self.height, self.width = height, width
        self.y, self.x = (start_y, start_x)

    def draw(self):
        if not self.need_redraw:
            return
        self._setup_data()
        self._draw_lines()
        y, x = self.screen.getmaxyx()
        if y != self.y or x != self.x:
            self.screen.mvwin(self.y, self.x)
        self.screen.refresh()

    def _draw_lines(self):
        self.screen.erase()
        for line, i in zip(self.lines, range(self.height)):
            style, line = self.get_highlight_line(line) if i == self.highlight_index else (0, line)
            self.screen.addnstr(i, self.padding, line, self.width - self.padding, style)

    def handle_key_down(self):
        self.highlight_index = min(len(self.lines)-1, self.highlight_index + 1)

    def handle_key_up(self):
        self.highlight_index = max(0, self.highlight_index - 1)

    def handle_key_left(self):
        self.parent.change_foucs(-1)

    def handle_key_right(self):
        self.parent.change_foucs(1)

    def handle_delete(self):
        pass

    def handle_copy(self):
        pass

    def handle_key(self, key):
        pass

    def _setup_data(self):
        pass

    def get_highlight_line(self, line):
        style = self.highlight_style_not_focus
        if self.focused:
            line = line + ' ' * max(self.width-len(line) - self.padding, 0)
            style = self.highlight_style
        return style, line

# add cache
class LeftPanelDispaly(SinglePanelDispaly):

    def _setup_data(self):
        if len(self.lines) > 0:
            return
        ssrs = get_path_by_time(self.parent.ssr_dir)
        ssrs = [basename(x) for x in ssrs]
        self.lines = ssrs

    def preview_data(self):
        ssr_name = self.lines[self.highlight_index]
        if re.match(JSON_FILE_REGEX, ssr_name):
            return [ssr_name]
        if re.match(SSR_FILE_REGEX, ssr_name) or re.match(VMESS_FILE_REGEX, ssr_name):
            ssr_name = f'{self.parent.ssr_dir}/{ssr_name}'
            return get_ssrnames([ssr_name])
        return []

    def get_selectd(self):
        return self.lines[self.highlight_index]

    def handle_delete(self):
        ssr_name = self.lines[self.highlight_index]
        remove_ssr(ssr_name)
        self.lines.pop(self.highlight_index)
        self.highlight_index = max(0, self.highlight_index - 1)
        if ssr_name in ssr_names_cache:
            del ssr_names_cache[ssr_name]
    
    def handle_copy(self):
        data = self.preview_data()
        if len(data) > 0:
            data = '\n'.join(data)
            os.system(f'echo "{data}" | xsel --clipboard')

    def draw(self):
        super().draw()
        self.need_redraw = self.focused

    def get_highlight_line(self, line):
        style = self.highlight_style_not_focus
        if self.focused:
            index_str = str(self.highlight_index)
            line = line + ' ' * max(self.width-len(line) - self.padding -len(index_str), 0) + index_str
            style = self.highlight_style
        return style, line

class MiddlePanelDispaly(SinglePanelDispaly):

    def _setup_data(self):
        lines = self.left_panel.preview_data()
        self.lines = lines

    def preview_data(self):
        if not self.lines:
            return []
        ssr_name = self.lines[self.highlight_index]
        preview_data = preview_vmess(ssr_name) if re.match(VMESS_FILE_REGEX, ssr_name) else preview_ssr(ssr_name)
        return preview_data

    def get_selectd(self):
        return self.lines[self.highlight_index]

    def handle_delete(self):
        ssr_name = self.lines[self.highlight_index]
        remove_ssr(ssr_name)
        self.lines.pop(self.highlight_index)
        self.highlight_index = max(0, self.highlight_index - 1)
        if ssr_name in ssr_cache:
            del ssr_names_cache[ssr_name]

    def handle_copy(self):
        ssr_name = self.lines[self.highlight_index]
        ssr_link = ssr_link_cache[ssr_name]
        if len(ssr_link) > 0:
            os.system(f'echo "{ssr_link}" | xsel --clipboard')

    def _draw_lines(self):
        lines = self.lines
        skip = self.highlight_index - self.height + 2 if self.highlight_index + 2 - self.height >= 0 else 0
        irange = min(len(lines)-skip, self.height)
        if not self.focused or (self.focused and skip > 0):
            self.screen.erase()
        for i in range(irange):
            i = i+ skip
            line = lines[i]
            if self.focused:
                if self.highlight_index > 0 and abs(i-self.highlight_index) > 1 and skip == 0:
                    continue
                curses.setsyx(i, self.x)
                self.screen.clrtoeol()
            style = 0
            line = line[0:(self.width-self.padding-1)] if (len(line) > self.width-self.padding-1) else line
            if i == self.highlight_index:
                style = self.highlight_style_not_focus
                if self.focused:
                    line = line + ' ' * max(self.width-len(line) - self.padding, 0)
                    style = self.highlight_style
            self.screen.addnstr(i-skip, self.padding, line, self.width - self.padding, style)

class RightPanelDispaly(SinglePanelDispaly):

    def __init__(self, parent, panel_index, lines=[], left_panel=None):
        SinglePanelDispaly.__init__(self, parent, panel_index, lines, left_panel)
        self.highlight_index = -1

    def _setup_data(self):
        lines = self.left_panel.preview_data()
        self.lines = lines

    def _draw_lines(self):
        self.screen.erase()
        for line, i in zip(self.lines, range(self.height)):
            style, line = self.get_highlight_line(line) if i == self.highlight_index else (0, line)
            max_len = self.width - self.padding
            ch_count = sum([ord(x) > 0x3000 for x in line])
            line = line[0:max_len-ch_count]
            self.screen.addnstr(i, self.padding, line, max_len, style)

class StatusBar:

    def __init__(self, parent):
        self.parent = parent
        self.parent_screen = self.parent.screen
        self.screen = self.parent_screen.derwin(0,0,0,0)
        self.height, self.width, self.x, self.y = 0, 0, 0, 0
        self.padding = 1

    def resize(self, start_y, start_x, height, width):
        self.screen.resize(height, width)
        parent_y, parent_x = self.screen.getparyx()
        if start_y != parent_y or start_x != parent_y:
            self.screen.mvderwin(start_y, start_x)
        self.height, self.width = height, width
        self.y, self.x = (start_y, start_x)

    def draw(self):
        y, x = self.screen.getmaxyx()
        if y != self.y or x != self.x:
            self.screen.mvwin(self.y, self.x)
        max_len = self.width - self.padding
        self.screen.erase()
        self.screen.addnstr(0, self.padding, self.get_status(), max_len)
        self.screen.refresh()

    # cache
    def get_status(self):
        (ssr_name, count_status) = self.parent.get_status()
        ssr_path = join(DEFAULT_SSR_DIR, ssr_name)
        if (re.match('.*_\d+_\.', ssr_name)):
            ssr_path=join(DEFAULT_SSR_DIR, re.sub('_(\d+)_\.ssr$', 'ssr', ssr_name))
            ssr_name=re.sub('_(\d+)_\.ssr$', 'ssr:\g<1>', ssr_name)
        ctime = ''
        if (os.path.isfile(ssr_path)):
            ctime=datetime.fromtimestamp(os.path.getctime(ssr_path)).strftime('%Y-%m-%d')
        return f'{DEFAULT_SSR_DIR}{ssr_name}\t{count_status}\t{ctime}'

#TODO: signal publish sub
class MultiPanelDisplay:

    def __init__(self, screen):
        self.ratios = [0.35, 0.25, 0.4]
        self.stop = False
        self.selected_server = ''
        self.screen = screen
        self.height, self.width = self.screen.getmaxyx()
        self.panels = []
        self.statusbar = None
        self.ssr_dir = DEFAULT_SSR_DIR
        self._setup_curses()
        self._setup_color()
        self.rebuild()
        key_bindings = create_key_bindings(self)
        self.key_processor = KeyProcessor(key_bindings)
        self.app = None

    def _setup_curses(self):
        self.screen.keypad(True)
        curses.raw()
        curses.noecho()
        curses.cbreak()
        curses.nonl()
        curses.curs_set(0)

    def _setup_color(self):
        Style().setup_color()

    def rebuild(self):
        self.panels = []
        left = LeftPanelDispaly(self, 0)
        middle = MiddlePanelDispaly(self, 1, left_panel=left)
        middle.focused = True
        right = RightPanelDispaly(self, 2, left_panel=middle)
        self.panels.extend([left, middle, right])
        self.statusbar = StatusBar(self)
        self.resize()

    def resize(self):
        top, left = 1, 0
        for i, ratio in enumerate(self.ratios):
            width = int(self.width * ratio)
            self.panels[i].resize(top, left, self.height-1, width)
            left += width
        self.statusbar.resize(0, 0, 1, self.width)

    def draw(self):
        self.screen.clear()
        while not self.stop:
            # self.screen.erase()
            self.redraw()
        return self.selected_server
    
    def pre_draw(self, app=None):
        self.app = app
        self.rebuild()
        self.screen.clear()

    def redraw(self):
        for panel in self.panels:
            panel.draw()
        self.statusbar.draw()
        self.handle_key()
        self.screen.refresh()

    def get_status(self):
        if (len(self.panels) != 3):
            return ''
        left = self.panels[0]
        middle = self.panels[1]
        ssr_name = middle.lines[middle.highlight_index]
        return (ssr_name, f'{len(left.lines)}:{len(middle.lines)}')

    def handle_key(self):
        key = self.screen.getkey()
        self.key_processor.feed(key)
        self.key_processor.process_keys()

    # direction -1 left 1 right
    def change_foucs(self, direction):
        is_first_foucs = self.panels[0].focused
        if is_first_foucs and direction == 1:
            # self.panels[0].highlight_index = 0
            self.panels[0].focused = False
            self.panels[1].focused = True
        if not is_first_foucs and direction == -1:
            self.panels[1].highlight_index = 0
            self.panels[0].need_redraw = True
            self.panels[0].focused = True
            self.panels[1].focused = False

session_display = None
class KeyPressEvent:

    def __init__(self, keys):
        self.keys = keys
        self.display = session_display

# TODO: process multiple keys
class KeyProcessor:

    def __init__(self, key_bindings):
        self.input_queue = deque()
        self._bindings = key_bindings

    def feed(self, key):
        self.input_queue.append(key)
    
    def feed_multiple(self, keys):
        self.input_queue.extend(keys)

    def process_keys(self):
        key =  self.input_queue.popleft()
        binding = self._bindings.get_binding_for_keys([key])
        if binding:
            binding.call(KeyPressEvent([key]))

class Binding:

    def __init__(self, keys, handler):
        self.keys = keys
        self.handler = handler
    
    def call(self, event):
        self.handler(event)

class KeyBindings:
    def __init__(self):
        self._bindings = []
    
    @property
    def bindings(self):
        return self._bindings
    
    def add(self, *keys):

        keys = [self._parse_key(key) for key in keys]
        def decorator(func):
            self.bindings.append(Binding(keys, func))
            return func
        return decorator
    
    def get_binding_for_keys(self, keys):
        match = None
        for binding in self._bindings:
            if len(keys) != len(binding.keys):
                continue
            found = True
            for i, j in zip(keys, binding.keys):
                if i != j:
                    found = False
                    break
            if found:
                match = binding 
        return match
            
    # TODO:
    def _parse_key(self, key):
        KEY_ALIASES = {
            'up': 'KEY_UP',
            'down': 'KEY_DOWN',
            'left': 'KEY_LEFT',
            'right': 'KEY_RIGHT',
            'enter': '\r',
            'delete': '\x04',
            'c-y': '\x19'
        }
        return KEY_ALIASES.get(key, key)

def create_key_bindings(display):

    kb = KeyBindings()
    def handle_panel(pos=[]):
        def handle_focused(func):
            def handle_key(*args, **kwargs):
                for panel in display.panels:
                    if not panel.focused:
                        continue
                    if ('left' in pos and not isinstance(panel, LeftPanelDispaly)) and \
                        ('middle' in pos and not isinstance(panel, MiddlePanelDispaly)):
                        continue
                    func(panel)
            return handle_key
        return handle_focused

    @kb.add('down')
    @handle_panel()
    def keydown(panel):
        panel.highlight_index = min(len(panel.lines)-1, panel.highlight_index + 1)

    @kb.add('up')
    @handle_panel()
    def keyup(panel):
        panel.highlight_index = max(0, panel.highlight_index - 1)

    @kb.add('left')
    @handle_panel()
    def keyleft(panel):
        display.change_foucs(-1)

    @kb.add('right')
    @handle_panel()
    def keyright(panel):
        display.change_foucs(1)

    @kb.add('delete')
    @handle_panel(['left', 'middle'])
    def delete_item(panel):
        panel.handle_delete()

    @kb.add('c-y')
    @handle_panel(['left', 'middle'])
    def copy_item(panel):
        panel.handle_copy()

    @kb.add('enter')
    def enter(event):
        display.stop = True
        result = display.panels[1].get_selectd()
        display.selected_server = result
        display.app.exit(result)

    return kb

# TODO: run_in_terminal
class Application:

    def __init__(self, layout):
        self._invalidated = False
        self.loop = None
        self.future = None
        self._is_running = False
        self.layout = layout

    def run(self):
        loop = asyncio.get_event_loop()
        coro = self.run_async()
        result = loop.run_until_complete(coro)
        return result

    async def run_async(self):
        if self._is_running:
            raise Exception('Application is already running.')
        self._is_running = True
        loop = asyncio.get_event_loop()
        f = loop.create_future()
        # set_result
        # set_exception
        self.future = f
        self.loop = loop
        self._pre_draw()
        self._redraw()
        try:
            result = await f
        finally:
            self._is_running = False
        return result

    def invalidate(self):
        if self._invalidated:
            return
        else:
            self._invalidated = True
        def redraw():
            self._invalidated = False
            self._redraw()
        def schedule_redraw():
            self.call_soon_threadsafe(redraw, max_postpone_time=0.01)
        schedule_redraw()
    
    def _pre_draw(self):
        self.layout.pre_draw(app=self)

    def _redraw(self):
        if not self._is_running:
            return
        self.layout.redraw()
        self.invalidate()

    def call_soon_threadsafe(self, func, max_postpone_time=None):
        loop = asyncio.get_event_loop()
        if max_postpone_time is None:
            loop.call_soon_threadsafe(func)
        max_postpone_until = time.time() + max_postpone_time
        def schedule():
            if not loop._ready:
                func()
                return

            if time.time() > max_postpone_until:
                func()
                return
            loop.call_soon_threadsafe(schedule)
        loop.call_soon_threadsafe(schedule)
    
    def exit(self, result=None, exception=None):
        if self.future is None:
            raise Exception('Application is not runing')
        
        if self.future.done():
            raise Exception('Return value already set')
        
        if exception is not None:
            self.future.set_exception()
        else:
            self.future.set_result(result)

# TODO: pager
def main():
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument('dir', nargs='?', default=DEFAULT_SSR_DIR)
        parser.add_argument('-p', '--preview')
        parser.add_argument('-s', '--sub')
        parser.add_argument('--proxy', nargs='?', default=argparse.SUPPRESS)
        parser.add_argument('--update', default=None)
        parser.add_argument('--check', nargs='?', help='check port open')
        parser.add_argument('--name', default=None)
        parser.add_argument('--lite', action='store_true')
        args = parser.parse_args()
        if args.preview:
            return preview_ssr(args.preview)
        if args.sub:
            proxy = None
            if ('proxy' in args):
                proxy = args.proxy if args.proxy is not None else DEFAULT_PROXY
            return add_subscription(args.sub, args.name, proxy)
        if args.update:
            update_type = args.update
            return update_subscription(update_type)
        if args.check:
            return check_connection()
    except KeyboardInterrupt:
        print('Operation Cancelled\n')
        return 0
    ssr_dir = args.dir
    if not os.path.isdir(ssr_dir):
        raise Exception('Path is not a directory')
    ssrs = get_path_by_time(ssr_dir)
    get_ssrnames(ssrs)
    selected_server = select_ssr_names()
    if selected_server is None:
        return
    cmd = build_cmd(selected_server, ssr_dir, args.lite)
    if cmd:
        os.system(cmd)

# TODO: support user pass directory path
# TODO: Sort by modify time
# TODO: JSON Format
ssr_cache = {}
ssr_link_cache = {}
ignore_regex = '.*("password"|"server_port").*'
def preview_ssr(filename, is_get_link=False):
    ssr_dir = DEFAULT_SSR_DIR
    origin_filename = filename
    if origin_filename in ssr_cache:
        return ssr_cache[filename] if not is_get_link else ssr_link_cache[filename]
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
        result = [replace_hide_field(x) for x in lines]
        ssr_cache[origin_filename] = result
        return result
    if re.match(SSR_FILE_REGEX, filename):
        line_num = 0 if not multiple_match else int(multiple_match.group(1))
        ssr_link = lines[line_num - 1].rstrip()
        #TODO: replce with ssrlink
        cmd = ['python3', f'{BASE_DIR}/shadowsocks/ssrlink.py', ssr_link]
        output = subprocess.check_output(cmd)
        result.extend([replace_hide_field(x) for x in output.decode('utf-8').split('\n')])
        ssr_cache[origin_filename] = result
        ssr_link_cache[origin_filename] = ssr_link
        return result
    # print(filepath)

def preview_vmess(filename):
    ssr_dir = DEFAULT_SSR_DIR
    vmess_link = get_vmess_link(filename, ssr_dir)
    if not vmess_link:
        return ['Not Found']
    vmess_match = re.match(r'^vmess://([A-Za-z0-9_/+-]+=*)', vmess_link)
    data = vmess_match.group(1)
    result = ssrlink.DecodeUrlSafeBase64(data)
    vmess_config = json.loads(result)
    vmess_config['id'] = '******'
    display_data = json.dumps(vmess_config, indent=4, ensure_ascii=False)
    return display_data.split('\n')

def replace_hide_field(x):
    if re.match('\s+"server_port":\s+\d+,?$', x, re.I):
        result = re.sub(':\s+\d+', ': 0', x)
        return result
    if re.match('\s+"password":\s+.+,?$', x, re.I):
        result = re.sub(':\s+".*"', ': "******"', x)
        return result
    return x

def remove_ssr(filename):
    ssr_dir = DEFAULT_SSR_DIR
    if filename in ssr_cache:
        del ssr_cache[filename]
    multiple_match = match_multiple_links_filename(filename)
    filename = re.sub('_\d+_\.', '', filename)
    filepath = f'{ssr_dir}{filename}'
    if re.match(JSON_FILE_REGEX, filename) and isfile(filename):
        os.remove(filepath)
    if re.match(SSR_FILE_REGEX, filename) or re.match(VMESS_FILE_REGEX, filename):
        lines = []
        if isfile(filepath):
            with open(filepath) as f:
                lines = f.readlines()
        if len(lines) < 2 or not multiple_match:
            os.remove(filepath)
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
def add_subscription(url, new_filename=None, proxy=None):
    if re.match(url_pattern, url):
        add_subscription_from_url(url)
    elif os.path.exists(url):
        add_subscription_from_file(url, new_filename)
    else:
        raise Exception(f'Not Supported f{url}')

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
    filename = f'{DEFAULT_SSR_DIR}{filename}'
    ssr_pattern = re.compile(r'(ssr://[a-zA-Z0-9_]+)[\s\n]', re.IGNORECASE)
    if re.match(ssr_pattern, data):
        matches = re.findall(ssr_pattern, data)
        write_ssr_links_to_file(matches, filename)
    else:
        write_ssr_data_to_file(data, filename, None)

def add_subscription_from_url(url, proxy=None):
    try:
        data = request_url(url, proxy)
        filename = f'{DEFAULT_SSR_DIR}{urlsplit(url).netloc}'
        write_ssr_data_to_file(data, filename, url)
    except Exception as e:
        print(e)
    return url

def write_ssr_data_to_file(data, filename, url):
    if not data.endswith('=='):
        data = data + '=='
    decode_data = base64.b64decode(data)
    decode_data = decode_data.decode('utf-8')
    extension = 'vmess' if re.match(r'^vmess://', decode_data) else 'ssr'
    if not re.match(r'(^ssr?://|^vmess://)', decode_data):
        raise Exception('Not Validated Data')
    filename = f'{filename}.{extension}'
    with open(filename, 'w', encoding='utf-8') as f:
        if re.search(r'\s+ssr?:', decode_data):
            decode_data=re.sub(r'\s+(ssr?:)', r'\n\1', decode_data)
        if re.search(r'\n+', decode_data):
            decode_data=re.sub(r'\n+', r'\n', decode_data)
        f.write(decode_data)
        if url is not None:
            f.write(url)

def write_ssr_links_to_file(links, filename):
    with open(filename, 'w', encoding='utf-8') as f:
        for link in links:
            f.write("%s\n" % link)

def write_subscribe_url(url, filename):
    with open(filename, 'a', encoding='utf-8') as f:
        f.write(url)

def request_url(url, proxy):
    if not re.match(url_pattern, url):
        raise Exception(f'Invalid url {url}')
    req = Request( url, data=None,
    headers={ 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/35.0.1916.47 Safari/537.36' }
    )
    if proxy:
        req.set_proxy(proxy, 'https')
        req.set_proxy(proxy, 'http')
    r = urlopen(req)
    data = r.read().decode(r.info().get_param('charset') or 'utf-8')
    return data

# update by number
def update_subscription(update_type):
    ssr_files = get_path_by_time(DEFAULT_SSR_DIR)
    urls = get_subscribe_urls(ssr_files)
    urls = [u for url in urls if len(url) > 0 for u in url ]
    request_urls(urls)

def get_subscribe_urls(filepaths):
    with ThreadPoolExecutor(max_workers = 4) as executor:
      results = executor.map(get_subscribe_url, filepaths)
    return results

def get_subscribe_url(filepath):
    with open(filepath, 'r') as f:
        lines = f.readlines()
        return [x for x in lines if re.match(url_pattern, x)]

def request_urls(urls):
    if len(urls) == 0:
        return
    with ThreadPoolExecutor(max_workers = 4) as executor:
      results = executor.map(add_subscription_from_url, urls)
    return results

def check_connection():
    ssr_files = get_path_by_time(DEFAULT_SSR_DIR)
    ssr_files = [x for x in ssr_files if re.match('.*\.ssr$', x)]
    with ThreadPoolExecutor(max_workers = 4) as executor:
      results = executor.map(read_all_links, ssr_files)
    configs = [x for x in results]
    with ThreadPoolExecutor(max_workers = 3) as executor:
        results = executor.map(check_config, configs)    
    # print(configs)

def read_all_links(ssr_filename):
    filename = basename(ssr_filename)
    with open(ssr_filename) as f:
        lines = f.readlines()
        return {filename: [ssrlink.parseLink(x) for x in lines if re.match('^ssr://',x)]}

def check_config(config):
    with ThreadPoolExecutor(max_workers = 4) as executor:
        results = executor.map(check_port_open, list(config.values())[0])
    print(list(config.keys())[0])
    for x in results:
        print(x)

def check_port_open(config):
    try:
        server=config['server']
        server_port=config['server_port']
        if server == 'www.google.com':
            return False
        if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", server):
            ip_addr=server
        else:
            ip_addr=socket.gethostbyname(server)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.3)
        result = sock.connect_ex((ip_addr, server_port))
        sock.close()
        return False if result != 0 else True
    except:
        sock.close()
        return False

#TODO: more sort method
def get_path_by_time(dir):
    ssrs = []
    with os.scandir(dir) as it:
        ssrs = [(entry.path, entry.stat().st_mtime) for entry in it \
            if entry.is_file() \
            if re.match('.*\.(ssr|json|vmess)$', entry.name) \
            if os.stat(entry.path).st_size > 0]
        ssrs.sort(key=lambda x: x[1], reverse=True)
        ssrs = [x[0] for x in ssrs]
    return ssrs

ssr_names_cache = {}
def get_ssrnames(ssrs):
    if len(ssrs) == 0:
        return []
    if len(ssrs) == 1:
        ssr_name = basename(ssrs[0])
        if ssr_name in ssr_names_cache:
            return ssr_names_cache[ssr_name]
    # tasks = [get_ssrname(x) for x in ssrs]
    # loop = asyncio.get_event_loop()
    # ssr_names = loop.run_until_complete(asyncio.gather(*tasks))
    executor = concurrent.futures.ThreadPoolExecutor()
    ssr_names = []
    for ssr_name in executor.map(get_ssrname, ssrs):
        ssr_names.append(ssr_name)
    ssr_names = reduce(operator.concat, ssr_names)
    return ssr_names

def get_ssrname(ssr):
    ssr_names = []
    filename = basename(ssr)
    if filename in ssr_names_cache:
        return ssr_names_cache[filename]
    if re.match(JSON_FILE_REGEX, ssr):
        ssr_names.append(filename)
    if re.match(SSR_FILE_REGEX, ssr) or re.match(VMESS_FILE_REGEX, ssr):
        with open(ssr) as f:
            lines = f.readlines()
            length = len(lines)
            if length == 0:
                return []
            if length == 1:
                ssr_names.append(filename)
                ssr_names_cache[filename] = ssr_names
                return ssr_names
            name_parts = filename.split('.')
            name_parts.insert(-1, '0')
            new_names = []
            for line in lines:
                if re.match(SSR_LINK_REGEX, line) or re.match(VMESS_LINK_REGEX, line):
                    name_parts[-2] = '_' + str(len(new_names) + 1) + '_'
                    new_names.append('.'.join(name_parts))
            ssr_names.extend(new_names)
            ssr_names_cache[filename] = new_names
    return ssr_names

def select_ssr_names():
    try:
        checkContext()
        stdscr = curses.initscr()
        height,width = stdscr.getmaxyx()
        screen = curses.newwin(height-1, width, 0, 0)
        display = MultiPanelDisplay(screen)
        # display.rebuild()
        # result = display.draw()
        app = Application(layout=display)
        result = app.run()
        return result
    except KeyboardInterrupt:
        exit(0)
    finally:
        screen.keypad(0)
        curses.echo()
        curses.nocbreak()
        curses.endwin()

def checkContext() :
    curses.setupterm()
    colorNum = curses.tigetnum("colors")
    if colorNum != 256:
        raise Exception('terminal not supports 256 color')

def build_cmd(ssr_name, ssr_dir, is_lite):
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
    if re.match(VMESS_FILE_REGEX, ssr_name):
        if is_lite:
            return build_cmd_lite(ssr_name, ssr_dir)
        else:
            return build_cmd_vmess(ssr_name, ssr_dir)
    return f'{cmd} --ssr-name=\'{ssr_name}\''

def build_cmd_vmess(vmess_name, ssr_dir):
    vmess_link = get_vmess_link(vmess_name, ssr_dir)
    if not vmess_link:
        return ''
    vmess_config = ssrlink.parseLink(vmess_link)
    with open(f'{V2RAY_DIR}config.json', 'w') as f:
        f.write(json.dumps(vmess_config, indent=4, ensure_ascii=False))
    return f'{V2RAY_DIR}v2ray --config={V2RAY_DIR}config.json -format=json' 

def build_cmd_lite(vmess_name, ssr_dir):
    vmess_link = get_vmess_link(vmess_name, ssr_dir)
    if not vmess_link:
        return ''
    return f'{V2RAY_DIR}lite --port 8088 --link {vmess_link}' 

def get_vmess_link(vmess_name, ssr_dir):
    match = re.match('.*\._(\d+)_\.vmess?$', vmess_name)
    if not match:
        return ''
    line_num = match.group(1)
    line_num = int(line_num)
    vmess_name = re.sub('_\d+_\.', '', vmess_name)
    vmess_path = f'{ssr_dir}{vmess_name}'
    with open(vmess_path) as f:
        lines = f.readlines()
        lines = [line for line in lines if re.match(VMESS_LINK_REGEX, line.rstrip())]
        return lines[line_num - 1]

def match_multiple_links_filename(filename):
    match = re.match('.*\._(\d+)_\.ssr?$', filename) or re.match('.*\._(\d+)_\.vmess?$', filename) 
    return match

# TODO: confirm handle all key fuzzy search
# TODO: count call_back delete event driven
# TODO: three panel git http_proxy power request add index sort options file info
# TODO: kill current process | multiple keys | sort by name Or time | pageup pagedown | event loop
if __name__ == '__main__':
    main()
