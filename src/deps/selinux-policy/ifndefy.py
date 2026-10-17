#!/usr/bin/env python3
#
# Copyright (C) 2021 Red Hat, Inc.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library.  If not, see
# <http://www.gnu.org/licenses/>.

# About:
# When provided with an interface file, this script outputs a new interface
# file, where all original interfaces are enclosed in an ifndef statement
# dependent on their name. All indentation inside the interface file should
# be converted to tabs before running this script.
# 
# Known issues:
# - interface already enclosed in an ifndef statement is not ignored
# - "`"  and "'" inside comments are not ignored
# - \t is always used for indentation - may result in mixture of spaces and tabs

import os
import re
import sys

if len(sys.argv) < 1:
    print((f"Usage: {sys.argv[0]} <policy>.if > <output>.if"), file=sys.stderr)
    exit(os.EX_USAGE)

# ending index of interface
end = 0
with open(sys.argv[1]) as f:
    interfaces = f.read()
    file_len = len(interfaces)

    while end < file_len:
        start = interfaces.find("interface(`", end)
        if start < 0:
            #no more interfaces
            print(interfaces[end:], end ="")
            break
        name_end = interfaces.find("'", start+11)
        name = interfaces[start+11:name_end]

        # locate the end of the interface - i.e. ending apostrophe
        reg = re.compile(r"[`']")
        # skip the ' after interface name
        i = name_end+1
        # counter for the depth (` opens a new block, while ' closes it)
        graves = 0
        while i < file_len:
            match = reg.search(interfaces, i)
            if not match:
                print(f"Malformed interface: {name}", file=sys.stderr)
                exit(1)

            i = match.end()
            if match.group(0) == '`':
                graves += 1
            else:
                graves -= 1
                if graves == 0:
                    break

        # keep everything between the end of previous interface and start of a new one
        print(interfaces[end:start], end ="")
        # interface ends in "')" -- add 1 for the edning bracket
        end = i+1
        # print the whole interface enclosed in "ifndef"
        print(f"ifndef(`{name}',`")
        print('\n'.join([('\t' + x) if x else x for x in interfaces[start:end].split('\n')]))
        print("')", end ="")
