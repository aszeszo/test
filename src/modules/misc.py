#!/usr/bin/python
#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or http://www.opensolaris.org/os/licensing.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#

# Copyright (c) 2007, 2012, Oracle and/or its affiliates. All rights reserved.

"""
Misc utility functions used by the packaging system.
"""

import OpenSSL.crypto as osc
import cStringIO
import calendar
import collections
import datetime
import errno
import getopt
import hashlib
import itertools
import locale
import os
import platform
import re
import resource
import shutil
import simplejson as json
import socket
import struct
import sys
import threading
import time
import traceback
import urllib
import urlparse
import zlib

from stat import S_IFMT, S_IMODE, S_IRGRP, S_IROTH, S_IRUSR, S_IRWXU, \
    S_ISBLK, S_ISCHR, S_ISDIR, S_ISFIFO, S_ISLNK, S_ISREG, S_ISSOCK, \
    S_IWUSR, S_IXGRP, S_IXOTH

import pkg.client.api_errors as api_errors
import pkg.portable as portable

from pkg import VERSION
from pkg.client import global_settings
from pkg.client.debugvalues import DebugValues
from pkg.client.imagetypes import img_type_names, IMG_NONE
from pkg.pkggzip import PkgGzipFile

# Default path where the temporary directories will be created.
DEFAULT_TEMP_PATH = "/var/tmp"

# Minimum number of days to issue warning before a certificate expires
MIN_WARN_DAYS = datetime.timedelta(days=30)

# Constant string used across many modules as a property name.
SIGNATURE_POLICY = "signature-policy"

# Bug URI Constants (deprecated)
# Line too long; pylint: disable=C0301
BUG_URI_CLI = "https://defect.opensolaris.org/bz/enter_bug.cgi?product=pkg&component=cli"
BUG_URI_GUI = "https://defect.opensolaris.org/bz/enter_bug.cgi?product=pkg&component=gui"
# pylint: enable=C0301

# Traceback message.
def get_traceback_message():
        """This function returns the standard traceback message.  A function
        is necessary since the _() call must be done at runtime after locale
        setup."""

        return _("""\n
This is an internal error in pkg(5) version %(version)s.  Please log a
Service Request about this issue including the information above and this
message.""") % { "version": VERSION }

def get_release_notes_url():
        """Return a release note URL pointing to the correct release notes
           for this version"""

        # TBD: replace with a call to api.info() that can return a "release"
        # attribute of form YYYYMM against the SUNWsolnm package
        return "http://www.oracle.com/pls/topic/lookup?ctx=E23824&id=SERNS"

def time_to_timestamp(t):
        """convert seconds since epoch to %Y%m%dT%H%M%SZ format"""
        # XXX optimize?; pylint: disable=W0511
        return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))

def timestamp_to_time(ts):
        """convert %Y%m%dT%H%M%SZ format to seconds since epoch"""
        # XXX optimize?; pylint: disable=W0511
        return calendar.timegm(time.strptime(ts, "%Y%m%dT%H%M%SZ"))

def timestamp_to_datetime(ts):
        """convert %Y%m%dT%H%M%SZ format to a datetime object"""
        return datetime.datetime.strptime(ts,"%Y%m%dT%H%M%SZ")

def copyfile(src_path, dst_path):
        """copy a file, preserving attributes, ownership, etc. where possible"""
        fs = os.lstat(src_path)
        shutil.copy2(src_path, dst_path)
        try:
                portable.chown(dst_path, fs.st_uid, fs.st_gid)
        except OSError, e:
                if e.errno != errno.EPERM:
                        raise

def copytree(src, dst):
        """Rewrite of shutil.copytree() that can handle special files such as
        FIFOs, sockets, and device nodes.  It re-creates all symlinks rather
        than copying the data behind them, and supports neither the 'symlinks'
        nor the 'ignore' keyword arguments of the shutil version.
        """

        problem = None
        os.makedirs(dst, PKG_DIR_MODE)
        src_stat = os.stat(src)
        for name in sorted(os.listdir(src)):
                s_path = os.path.join(src, name)
                d_path = os.path.join(dst, name)
                s = os.lstat(s_path)
                if S_ISDIR(s.st_mode):
                        copytree(s_path, d_path)
                        os.chmod(d_path, S_IMODE(s.st_mode))
                        os.chown(d_path, s.st_uid, s.st_gid)
                        os.utime(d_path, (s.st_atime, s.st_mtime))
                elif S_ISREG(s.st_mode):
                        shutil.copyfile(s_path, d_path)
                        os.chmod(d_path, S_IMODE(s.st_mode))
                        os.chown(d_path, s.st_uid, s.st_gid)
                        os.utime(d_path, (s.st_atime, s.st_mtime))
                elif S_ISLNK(s.st_mode):
                        os.symlink(os.readlink(s_path), d_path)
                elif S_ISFIFO(s.st_mode):
                        os.mkfifo(d_path, S_IMODE(s.st_mode))
                        os.chown(d_path, s.st_uid, s.st_gid)
                        os.utime(d_path, (s.st_atime, s.st_mtime))
                elif S_ISSOCK(s.st_mode):
                        sock = socket.socket(socket.AF_UNIX)
                        # The s11 fcs version of python doesn't have os.mknod()
                        # but sock.bind has a path length limitation that we can
                        # hit when archiving the test suite.
                        # E1101 Module '%s' has no '%s' member
                        # pylint: disable=E1101
                        if hasattr(os, "mknod"):
                                os.mknod(d_path, s.st_mode, s.st_dev)
                        else:
                                try:
                                        sock.bind(d_path)
                                        sock.close()
                                except sock.error, _e:
                                        # Store original exception so that the
                                        # real cause of failure can be raised if
                                        # this fails.
                                        problem = sys.exc_info()
                                        continue
                        os.chown(d_path, s.st_uid, s.st_gid)
                        os.utime(d_path, (s.st_atime, s.st_mtime))
                elif S_ISCHR(s.st_mode) or S_ISBLK(s.st_mode):
                        # the s11 fcs version of python doesn't have os.mknod()
                        # E1101 Module '%s' has no '%s' member
                        # pylint: disable=E1101
                        if hasattr(os, "mknod"):
                                os.mknod(d_path, s.st_mode, s.st_dev)
                                os.chown(d_path, s.st_uid, s.st_gid)
                                os.utime(d_path, (s.st_atime, s.st_mtime))
                elif S_IFMT(s.st_mode) == 0xd000: # doors
                        pass
                elif S_IFMT(s.st_mode) == 0xe000: # event ports
                        pass
                else:
                        print "unknown file type:", oct(S_IFMT(s.st_mode))

        os.chmod(dst, S_IMODE(src_stat.st_mode))
        os.chown(dst, src_stat.st_uid, src_stat.st_gid)
        os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))
        if problem:
                raise problem[0], problem[1], problem[2]

def move(src, dst):
        """Rewrite of shutil.move() that uses our copy of copytree()."""

        # If dst is a directory, then we try to move src into it.
        if os.path.isdir(dst):
                dst = os.path.join(dst,
                    os.path.basename(src).rstrip(os.path.sep))

        try:
                os.rename(src, dst)
        except EnvironmentError, e:
                s = os.lstat(src)

                if e.errno == errno.EXDEV:
                        if S_ISDIR(s.st_mode):
                                copytree(src, dst)
                                shutil.rmtree(src)
                        else:
                                shutil.copyfile(src, dst)
                                os.chmod(dst, S_IMODE(s.st_mode))
                                os.chown(dst, s.st_uid, s.st_gid)
                                os.utime(dst, (s.st_atime, s.st_mtime))
                                os.unlink(src)
                elif e.errno == errno.EINVAL and S_ISDIR(s.st_mode):
                        raise shutil.Error, "Cannot move a directory '%s' " \
                            "into itself '%s'." % (src, dst)
                elif e.errno == errno.ENOTDIR and S_ISDIR(s.st_mode):
                        raise shutil.Error, "Destination path '%s' already " \
                            "exists" % dst
                else:
                        raise

def expanddirs(dirs):
        """given a set of directories, return expanded set that includes
        all components"""
        out = set()
        for d in dirs:
                p = d
                while p != "":
                        out.add(p)
                        p = os.path.dirname(p)
        return out

def url_affix_trailing_slash(u):
        """if 'u' donesn't have a trailing '/', append one."""

        if u[-1] != '/':
                u = u + '/'

        return u

_client_version = "pkg/%s (%s %s; %s %s; %%s; %%s)" % \
    (VERSION, portable.util.get_canonical_os_name(), platform.machine(),
    portable.util.get_os_release(), platform.version())

def user_agent_str(img, client_name):
        """Return a string that can use to identify the client."""

        if not img or img.type is None:
                imgtype = IMG_NONE
        else:
                imgtype = img.type

        useragent = _client_version % (img_type_names[imgtype], client_name)

        return useragent

# Valid hostname can be : HOSTNAME or IPv4 addr or IPV6 addr
_hostname_re = re.compile(r"""^(?:[a-zA-Z0-9\-]+[a-zA-Z0-9\-\.]*
                   |(?:\d{1,3}\.){3}\d{3}
                   |\[([a-fA-F0-9\.]*:){,7}[a-fA-F0-9\.]+\])$""", re.X)

_invalid_host_chars = re.compile(".*[^a-zA-Z0-9\-\.:\[\]]+")
_valid_proto = ["file", "http", "https"]

def valid_pub_prefix(prefix):
        """Verify that the publisher prefix only contains valid characters."""

        if not prefix:
                return False

        # This is a workaround for the the hostname_re being slow when
        # it comes to finding invalid characters in the prefix string.
        if _invalid_host_chars.match(prefix):
                # prefix bad chars
                return False

        if _hostname_re.match(prefix):
                return True

        return False

def valid_pub_url(url, proxy=False):
        """Verify that the publisher URL contains only valid characters.
        If 'proxy' is set to True, some checks are relaxed."""

        if not url:
                return False

        # First split the URL and check if the scheme is one we support
        o = urlparse.urlsplit(url)

        if not o[0] in _valid_proto:
                return False

        if o[0] == "file":
                path = urlparse.urlparse(url, "file", allow_fragments=0)[2]
                path = urllib.url2pathname(path)
                if not os.path.abspath(path):
                        return False
                # No further validation to be done.
                return True

        # Next verify that the network location is valid
        host = urllib.splitport(o[1])[0]

        if proxy:
                # We may have authentication details in the proxy URI, which
                # we must ignore when checking for hostname validity.
                host_parts = host.split("@")
                if len(host_parts) == 2:
                        host = host[1]

        if not host or _invalid_host_chars.match(host):
                return False

        if _hostname_re.match(host):
                return True

        return False

def gunzip_from_stream(gz, outfile):
        """Decompress a gzipped input stream into an output stream.

        The argument 'gz' is an input stream of a gzipped file and 'outfile'
        is is an output stream.  gunzip_from_stream() decompresses data from
        'gz' and writes it to 'outfile', and returns the hexadecimal SHA-1 sum
        of that data.
        """

        FHCRC = 2
        FEXTRA = 4
        FNAME = 8
        FCOMMENT = 16

        # Read the header
        magic = gz.read(2)
        if magic != "\037\213":
                raise zlib.error, "Not a gzipped file"
        method = ord(gz.read(1))
        if method != 8:
                raise zlib.error, "Unknown compression method"
        flag = ord(gz.read(1))
        gz.read(6) # Discard modtime, extraflag, os

        # Discard an extra field
        if flag & FEXTRA:
                xlen = ord(gz.read(1))
                xlen = xlen + 256 * ord(gz.read(1))
                gz.read(xlen)

        # Discard a null-terminated filename
        if flag & FNAME:
                while True:
                        s = gz.read(1)
                        if not s or s == "\000":
                                break

        # Discard a null-terminated comment
        if flag & FCOMMENT:
                while True:
                        s = gz.read(1)
                        if not s or s == "\000":
                                break

        # Discard a 16-bit CRC
        if flag & FHCRC:
                gz.read(2)

        shasum = hashlib.sha1()
        dcobj = zlib.decompressobj(-zlib.MAX_WBITS)

        while True:
                buf = gz.read(64 * 1024)
                if buf == "":
                        ubuf = dcobj.flush()
                        shasum.update(ubuf) # pylint: disable=E1101
                        outfile.write(ubuf)
                        break
                ubuf = dcobj.decompress(buf)
                shasum.update(ubuf) # pylint: disable=E1101
                outfile.write(ubuf)

        return shasum.hexdigest()

class PipeError(Exception):
        """ Pipe exception. """

        def __init__(self, args=None):
                Exception.__init__(self)
                self._args = args

def msg(*text):
        """ Emit a message. """

        try:
                print ' '.join([str(l) for l in text])
        except IOError, e:
                if e.errno == errno.EPIPE:
                        raise PipeError, e
                raise

def emsg(*text):
        """ Emit a message to sys.stderr. """

        try:
                print >> sys.stderr, ' '.join([str(l) for l in text])
        except IOError, e:
                if e.errno == errno.EPIPE:
                        raise PipeError, e
                raise

def setlocale(category, loc=None, printer=None):
        """Wraps locale.setlocale(), falling back to the C locale if the desired
        locale is broken or unavailable.  The 'printer' parameter should be a
        function which takes a string and displays it.  If 'None' (the default),
        setlocale() will print the message to stderr."""

        if printer is None:
                printer = emsg

        try:
                locale.setlocale(category, loc)
                # Because of Python bug 813449, getdefaultlocale may fail
                # with a ValueError even if setlocale succeeds. So we call
                # it here to prevent having this error raised if it is
                # called later by other non-pkg(5) code.
                locale.getdefaultlocale()
        except (locale.Error, ValueError):
                try:
                        dl = " '%s.%s'" % locale.getdefaultlocale()
                except ValueError:
                        dl = ""
                printer("Unable to set locale%s; locale package may be broken "
                    "or\nnot installed.  Reverting to C locale." % dl)
                locale.setlocale(category, "C")
def N_(message):
        """Return its argument; used to mark strings for localization when
        their use is delayed by the program."""
        return message

def bytes_to_str(nbytes, fmt="%(num).2f %(unit)s"):
        """Returns a human-formatted string representing the number of bytes
        in the largest unit possible.
        
        If provided, 'fmt' should be a string which can be formatted
        with a dictionary containing a float 'num' and strings 'unit' and
        'shortunit'.  The default format prints, for example, '3.23 MB' """

        units = [
            (_("B"), _("B"), 2**10),
            (_("kB"), _("k"), 2**20),
            (_("MB"), _("M"), 2**30),
            (_("GB"), _("G"), 2**40),
            (_("TB"), _("T"), 2**50),
            (_("PB"), _("P"), 2**60),
            (_("EB"), _("E"), 2**70)
        ]

        for uom, shortuom, limit in units:
                if uom != _("EB") and nbytes >= limit:
                        # Try the next largest unit of measure unless this is
                        # the largest or if the byte size is within the current
                        # unit of measure's range.
                        continue

                return fmt % {
                    "num": round(nbytes / float(limit / 2**10), 2),
                    "unit": uom,
                    "shortunit": shortuom
                }

def get_rel_path(request, uri, pub=None):
        """Calculate the depth of the current request path relative to our
        base uri. path_info always ends with a '/' -- so ignore it when
        calculating depth."""

        rpath = request.path_info
        if pub:
                rpath = rpath.replace("/%s/" % pub, "/")
        depth = rpath.count("/") - 1
        return ("../" * depth) + uri

def get_pkg_otw_size(action):
        """Takes a file action and returns the over-the-wire size of
        a package as an integer.  The OTW size is the compressed size,
        pkg.csize.  If that value isn't available, it returns pkg.size.
        If pkg.size isn't available, return zero."""

        size = action.attrs.get("pkg.csize", 0)
        if size == 0:
                size = action.attrs.get("pkg.size", 0)

        return int(size)

def get_data_digest(data, length=None, return_content=False):
        """Returns a tuple of (SHA-1 hexdigest, content).

        'data' should be a file-like object or a pathname to a file.

        'length' should be an integer value representing the size of
        the contents of data in bytes.

        'return_content' is a boolean value indicating whether the
        second tuple value should contain the content of 'data' or
        if the content should be discarded during processing."""

        bufsz = 128 * 1024
        closefobj = False
        if isinstance(data, basestring):
                f = file(data, "rb", bufsz)
                closefobj = True
        else:
                f = data

        if length is None:
                length = os.stat(data).st_size

        # Read the data in chunks and compute the SHA1 hash as it comes in.  A
        # large read on some platforms (e.g. Windows XP) may fail.
        content = cStringIO.StringIO()
        fhash = hashlib.sha1()
        while length > 0:
                data = f.read(min(bufsz, length))
                if return_content:
                        content.write(data)
                fhash.update(data) # pylint: disable=E1101

                l = len(data)
                if l == 0:
                        break
                length -= l
        content.reset()
        if closefobj:
                f.close()

        return fhash.hexdigest(), content.read()

def compute_compressed_attrs(fname, file_path, data, size, compress_dir,
    bufsz=64*1024):
        """Returns the size and hash of the compressed data.  If the file
        located at file_path doesn't exist or isn't gzipped, it creates a file
        in compress_dir named fname."""

        #
        # This check prevents compressing a file which is already compressed.
        # This takes CPU load off the depot on large imports of mostly-the-same
        # stuff.  And in general it saves disk bandwidth, and on ZFS in
        # particular it saves us space in differential snapshots.  We also need
        # to check that the destination is in the same compression format as
        # the source, as we must have properly formed files for chash/csize
        # properties to work right.
        #

        fileneeded = True
        if file_path:
                if PkgGzipFile.test_is_pkggzipfile(file_path):
                        fileneeded = False
                        opath = file_path

        if fileneeded:
                opath = os.path.join(compress_dir, fname)
                ofile = PkgGzipFile(opath, "wb")

                nbuf = size / bufsz

                for n in range(0, nbuf):
                        l = n * bufsz
                        h = (n + 1) * bufsz
                        ofile.write(data[l:h])

                m = nbuf * bufsz
                ofile.write(data[m:])
                ofile.close()

        data = None

        # Now that the file has been compressed, determine its
        # size.
        fs = os.stat(opath)
        csize = str(fs.st_size)

        # Compute the SHA hash of the compressed file.  In order for this to
        # work correctly, we have to use the PkgGzipFile class.  It omits
        # filename and timestamp information from the gzip header, allowing us
        # to generate deterministic hashes for different files with identical
        # content.
        cfile = open(opath, "rb")
        chash = hashlib.sha1()
        while True:
                cdata = cfile.read(bufsz)
                if cdata == "":
                        break
                chash.update(cdata) # pylint: disable=E1101
        cfile.close()
        return csize, chash

class ProcFS(object):
        """This class is used as an interface to procfs."""

        _ctype_formats = {
            # This dictionary maps basic c types into python format characters
            # that can be used with struct.unpack().  The format of this
            # dictionary is:
            #    <ctype>: (<repeat count>, <format char>)

            # basic c types (repeat count should always be 1)
            # char[] is used to encode character arrays
            "char":        (1,  "c"),
            "char[]":      (1,  "s"),
            "int":         (1,  "i"),
            "long":        (1,  "l"),
            "uintptr_t":   (1,  "I"),
            "ushort_t":    (1,  "H"),

            # other simple types (repeat count should always be 1)
            "ctid_t":      (1,  "i"), # ctid_t -> id_t -> int
            "dev_t":       (1,  "L"), # dev_t -> ulong_t
            "gid_t":       (1,  "I"), # gid_t -> uid_t -> uint_t
            "pid_t":       (1,  "i"), # pid_t -> int
            "poolid_t":    (1,  "i"), # poolid_t -> id_t -> int
            "projid_t":    (1,  "i"), # projid_t -> id_t -> int
            "size_t":      (1,  "L"), # size_t -> ulong_t
            "taskid_t":    (1,  "i"), # taskid_t -> id_t -> int
            "time_t":      (1,  "l"), # time_t -> long
            "uid_t":       (1,  "I"), # uid_t -> uint_t
            "zoneid_t":    (1,  "i"), # zoneid_t -> id_t -> int
            "id_t":        (1,  "i"), # id_t -> int

            # structures must be represented as character arrays
            "timestruc_t": (8,  "s"), # sizeof (timestruc_t) = 8
        }

        _timestruct_desc = [
            # this list describes a timestruc_t structure
            # the entry format is (<ctype>, <repeat count>, <name>)
            ("time_t", 1, "tv_sec"),
            ("long",   1, "tv_nsec"),
        ]

        _psinfo_desc = [
            # this list describes a psinfo_t structure
            # the entry format is: (<ctype>, <repeat count>, <name>)
            ("int",         1,  "pr_flag"),
            ("int",         1,  "pr_nlwp"),
            ("pid_t",       1,  "pr_pid"),
            ("pid_t",       1,  "pr_ppid"),
            ("pid_t",       1,  "pr_pgid"),
            ("pid_t",       1,  "pr_sid"),
            ("uid_t",       1,  "pr_uid"),
            ("uid_t",       1,  "pr_euid"),
            ("gid_t",       1,  "pr_gid"),
            ("gid_t",       1,  "pr_egid"),
            ("uintptr_t",   1,  "pr_addr"),
            ("size_t",      1,  "pr_size"),
            ("size_t",      1,  "pr_rssize"),
            ("size_t",      1,  "pr_pad1"),
            ("dev_t",       1,  "pr_ttydev"),
            ("ushort_t",    1,  "pr_pctcpu"),
            ("ushort_t",    1,  "pr_pctmem"),
            ("timestruc_t", 1,  "pr_start"),
            ("timestruc_t", 1,  "pr_time"),
            ("timestruc_t", 1,  "pr_ctime"),
            ("char[]",      16, "pr_fname"),
            ("char[]",      80, "pr_psargs"),
            ("int",         1,  "pr_wstat"),
            ("int",         1,  "pr_argc"),
            ("uintptr_t",   1,  "pr_argv"),
            ("uintptr_t",   1,  "pr_envp"),
            ("char",        1,  "pr_dmodel"),
            ("char[]",      3,  "pr_pad2"),
            ("taskid_t",    1,  "pr_taskid"),
            ("projid_t",    1,  "pr_projid"),
            ("int",         1,  "pr_nzomb"),
            ("poolid_t",    1,  "pr_poolid"),
            ("zoneid_t",    1,  "pr_zoneid"),
            ("id_t",        1,  "pr_contract"),
            ("int",         1,  "pr_filler"),
        ]

        _struct_descriptions = {
            # this list contains all the known structure description lists
            # the entry format is: <structure name>: \
            #    [ <description>, <format string>, <namedtuple> ]
            #
            # Note that <format string> and <namedtuple> should be assigned
            # None in this table, and then they will get pre-populated
            # automatically when this class is instantiated
            #
            "psinfo_t":    [_psinfo_desc, None, None],
            "timestruc_t": [_timestruct_desc, None, None],
        }

        # fill in <format string> and <namedtuple> in _struct_descriptions
        for struct_name, v in _struct_descriptions.iteritems():
                desc = v[0]

                # update _struct_descriptions with a format string
                v[1] = ""
                for ctype, count1, name in desc:
                        count2, fmt_char = _ctype_formats[ctype]
                        v[1] = v[1] + str(count1 * count2) + fmt_char

                # update _struct_descriptions with a named tuple
                v[2] = collections.namedtuple(struct_name,
                    [ i[2] for i in desc ])

        @staticmethod
        def _struct_unpack(data, name):
                """Unpack 'data' using struct.unpack().  'name' is the name of
                the data we're unpacking and is used to lookup a description
                of the data (which in turn is used to build a format string to
                decode the data)."""

                # lookup the description of the data to unpack
                desc, fmt, nt = ProcFS._struct_descriptions[name]

                # unpack the data into a list
                rv = list(struct.unpack(fmt, data))

                # check for any nested data that needs unpacking
                for index, v in enumerate(desc):
                        ctype = v[0]
                        if ctype not in ProcFS._struct_descriptions:
                                continue
                        rv[index] = ProcFS._struct_unpack(rv[index], ctype)

                # return the data in a named tuple
                return nt(*rv)

        @staticmethod
        def psinfo():
                """Read the psinfo file and return its contents."""

                # This works only on Solaris, in 32-bit mode.  It may not work
                # on older or newer versions than 5.11.  Ideally, we would use
                # libproc, or check sbrk(0), but this is expedient.  In most
                # cases (there's a small chance the file will decode, but
                # incorrectly), failure will raise an exception, and we'll
                # fail safe.
                psinfo_size = 232
                try:
                        psinfo_data = file("/proc/self/psinfo").read(
                            psinfo_size)
                # Catch "Exception"; pylint: disable=W0703
                except Exception:
                        return None

                # make sure we got the expected amount of data, otherwise
                # unpacking it will fail.
                if len(psinfo_data) != psinfo_size:
                        return None

                return ProcFS._struct_unpack(psinfo_data, "psinfo_t")


def __getvmusage():
        """Return the amount of virtual memory in bytes currently in use."""

        psinfo = ProcFS.psinfo()
        if psinfo is None:
                return None
        return psinfo.pr_size * 1024

def _prstart():
        """Return the process start time expressed as a floating point number
        in seconds since the epoch, in UTC."""
        psinfo = ProcFS.psinfo()
        if psinfo is None:
                return 0.0
        return psinfo.pr_start.tv_sec + (float(psinfo.pr_start.tv_nsec) / 1e9)

def out_of_memory():
        """Return an out of memory message, for use in a MemoryError handler."""

        # figure out how much memory we're using (note that we could run out
        # of memory while doing this, so check for that.
        vsz = None
        try:
                vmusage = __getvmusage()
                if vmusage is not None:
                        vsz = bytes_to_str(vmusage, fmt="%(num).0f%(unit)s")
        except (MemoryError, EnvironmentError), __e:
                if isinstance(__e, EnvironmentError) and \
                    __e.errno != errno.ENOMEM:
                        raise

        if vsz is not None:
                error = """\
There is not enough memory to complete the requested operation.  At least
%(vsz)s of virtual memory was in use by this command before it ran out of memory.
You must add more memory (swap or physical) or allow the system to access more
existing memory, or quit other programs that may be consuming memory, and try
the operation again."""
        else:
                error = """\
There is not enough memory to complete the requested operation.  You must
add more memory (swap or physical) or allow the system to access more existing
memory, or quit other programs that may be consuming memory, and try the
operation again."""

        return _(error) % locals()


# EmptyI for argument defaults
EmptyI = tuple()

# ImmutableDict for argument defaults
class ImmutableDict(dict):
        # Missing docstring; pylint: disable=C0111
        # Unused argument; pylint: disable=W0613

        def __init__(self, default=EmptyI):
                dict.__init__(self, default)

        def __setitem__(self, item, value):
                self.__oops()

        def __delitem__(self, item, value):
                self.__oops()

        def pop(self, item, default=None):
                self.__oops()

        def popitem(self):
                self.__oops()

        def setdefault(self, item, default=None):
                self.__oops()

        def update(self, d):
                self.__oops()

        def copy(self):
                return ImmutableDict()

        def clear(self):
                self.__oops()

        @staticmethod
        def __oops():
                raise TypeError, "Item assignment to ImmutableDict"

# A way to have a dictionary be a property

class DictProperty(object):
        # Missing docstring; pylint: disable=C0111

        class __InternalProxy(object):
                def __init__(self, obj, fget, fset, fdel, iteritems, keys,
                    values, iterator, fgetdefault, fsetdefault, update, pop):
                        self.__obj = obj
                        self.__fget = fget
                        self.__fset = fset
                        self.__fdel = fdel
                        self.__iteritems = iteritems
                        self.__keys = keys
                        self.__values = values
                        self.__iter = iterator
                        self.__fgetdefault = fgetdefault
                        self.__fsetdefault = fsetdefault
                        self.__update = update
                        self.__pop = pop

                def __getitem__(self, key):
                        if self.__fget is None:
                                raise AttributeError, "unreadable attribute"

                        return self.__fget(self.__obj, key)

                def __setitem__(self, key, value):
                        if self.__fset is None:
                                raise AttributeError, "can't set attribute"
                        self.__fset(self.__obj, key, value)

                def __delitem__(self, key):
                        if self.__fdel is None:
                                raise AttributeError, "can't delete attribute"
                        self.__fdel(self.__obj, key)

                def iteritems(self):
                        if self.__iteritems is None:
                                raise AttributeError, "can't iterate over items"
                        return self.__iteritems(self.__obj)

                def keys(self):
                        if self.__keys is None:
                                raise AttributeError, "can't iterate over keys"
                        return self.__keys(self.__obj)

                def values(self):
                        if self.__values is None:
                                raise AttributeError, "can't iterate over " \
                                    "values"
                        return self.__values(self.__obj)

                def get(self, key, default=None):
                        if self.__fgetdefault is None:
                                raise AttributeError, "can't use get"
                        return self.__fgetdefault(self.__obj, key, default)

                def setdefault(self, key, default=None):
                        if self.__fsetdefault is None:
                                raise AttributeError, "can't use setdefault"
                        return self.__fsetdefault(self.__obj, key, default)

                def update(self, d):
                        if self.__update is None:
                                raise AttributeError, "can't use update"
                        return self.__update(self.__obj, d)

                def pop(self, d, default):
                        if self.__pop is None:
                                raise AttributeError, "can't use pop"
                        return self.__pop(self.__obj, d, default)

                def __iter__(self):
                        if self.__iter is None:
                                raise AttributeError, "can't iterate"
                        return self.__iter(self.__obj)

        def __init__(self, fget=None, fset=None, fdel=None, iteritems=None,
            keys=None, values=None, iterator=None, doc=None, fgetdefault=None,
            fsetdefault=None, update=None, pop=None):
                self.__fget = fget
                self.__fset = fset
                self.__fdel = fdel
                self.__iteritems = iteritems
                self.__doc__ = doc
                self.__keys = keys
                self.__values = values
                self.__iter = iterator
                self.__fgetdefault = fgetdefault
                self.__fsetdefault = fsetdefault
                self.__update = update
                self.__pop = pop

        def __get__(self, obj, objtype=None):
                # Unused argument; pylint: disable=W0613

                if obj is None:
                        return self
                return self.__InternalProxy(obj, self.__fget, self.__fset,
                    self.__fdel, self.__iteritems, self.__keys, self.__values,
                    self.__iter, self.__fgetdefault, self.__fsetdefault,
                    self.__update, self.__pop)


def build_cert(path, uri=None, pub=None):
        """Take the file given in path, open it, and use it to create
        an X509 certificate object.

        'uri' is an optional value indicating the uri associated with or that
        requires the certificate for access.

        'pub' is an optional string value containing the name (prefix) of a
        related publisher."""

        try:
                cf = file(path, "rb")
                certdata = cf.read()
                cf.close()
        except EnvironmentError, e:
                if e.errno == errno.ENOENT:
                        raise api_errors.NoSuchCertificate(path, uri=uri,
                            publisher=pub)
                if e.errno == errno.EACCES:
                        raise api_errors.PermissionsException(e.filename)
                if e.errno == errno.EROFS:
                        raise api_errors.ReadOnlyFileSystemException(e.filename)
                raise

        try:
                return osc.load_certificate(osc.FILETYPE_PEM, certdata)
        except osc.Error, e:
                # OpenSSL.crypto.Error
                raise api_errors.InvalidCertificate(path, uri=uri,
                    publisher=pub)

def validate_ssl_cert(ssl_cert, prefix=None, uri=None):
        """Validates the indicated certificate and returns a pyOpenSSL object
        representing it if it is valid."""
        cert = build_cert(ssl_cert, uri=uri, pub=prefix)

        if cert.has_expired():
                raise api_errors.ExpiredCertificate(ssl_cert, uri=uri,
                    publisher=prefix)

        now = datetime.datetime.utcnow()
        nb = cert.get_notBefore()
        t = time.strptime(nb, "%Y%m%d%H%M%SZ")
        nbdt = datetime.datetime.utcfromtimestamp(
            calendar.timegm(t))

        # PyOpenSSL's has_expired() doesn't validate the notBefore
        # time on the certificate.  Don't ask me why.

        if nbdt > now:
                raise api_errors.NotYetValidCertificate(ssl_cert, uri=uri,
                    publisher=prefix)

        na = cert.get_notAfter()
        t = time.strptime(na, "%Y%m%d%H%M%SZ")
        nadt = datetime.datetime.utcfromtimestamp(
            calendar.timegm(t))

        diff = nadt - now

        if diff <= MIN_WARN_DAYS:
                raise api_errors.ExpiringCertificate(ssl_cert, uri=uri,
                    publisher=prefix, days=diff.days)

        return cert

# Used for the conversion of the signature value between hex and binary.
char_list = "0123456789abcdef"

def binary_to_hex(s):
        """Converts a string of bytes to a hexadecimal representation.
        """

        res = ""
        for p in s:
                p = ord(p)
                a = char_list[p % 16]
                p = p/16
                b = char_list[p % 16]
                res += b + a
        return res

def hex_to_binary(s):
        """Converts a string of hex digits to the binary representation.
        """

        res = ""
        for i in range(0, len(s), 2):
                res += chr(char_list.find(s[i]) * 16 +
                    char_list.find(s[i+1]))
        return res

def config_temp_root():
        """Examine the environment.  If the environment has set TMPDIR, TEMP,
        or TMP, return None.  This tells tempfile to use the environment
        settings when creating temporary files/directories.  Otherwise,
        return a path that the caller should pass to tempfile instead."""

        # In Python's tempfile module, the default temp directory
        # includes some paths that are suboptimal for holding large numbers
        # of files.  If the user hasn't set TMPDIR, TEMP, or TMP in the
        # environment, override the default directory for creating a tempfile.
        tmp_envs = [ "TMPDIR", "TEMP", "TMP" ]
        for ev in tmp_envs:
                env_val = os.getenv(ev)
                if env_val:
                        return None

        return DEFAULT_TEMP_PATH

def get_temp_root_path():
        """Return the directory path where the temporary directories or 
        files should be created. If the environment has set TMPDIR
        or TEMP or TMP then return the corresponding value else return the
        default value."""

        temp_env = [ "TMPDIR", "TEMP", "TMP" ]
        for env in temp_env:
                env_val = os.getenv(env)
                if env_val:
                        return env_val

        return DEFAULT_TEMP_PATH 

def parse_uri(uri, cwd=None):
        """Parse the repository location provided and attempt to transform it
        into a valid repository URI.

        'cwd' is the working directory to use to turn paths into an absolute
        path.  If not provided, the current working directory is used.
        """

        if uri.find("://") == -1 and not uri.startswith("file:/"):
                # Convert the file path to a URI.
                if not cwd:
                        uri = os.path.abspath(uri)
                elif not os.path.isabs(uri):
                        uri = os.path.normpath(os.path.join(cwd, uri))

                uri = urlparse.urlunparse(("file", "",
                    urllib.pathname2url(uri), "", "", ""))

        scheme, netloc, path, params, query, fragment = \
            urlparse.urlparse(uri, "file", allow_fragments=0)
        scheme = scheme.lower()

        if scheme == "file":
                # During urlunparsing below, ensure that the path starts with
                # only one '/' character, if any are present.
                if path.startswith("/"):
                        path = "/" + path.lstrip("/")

        # Rebuild the URI with the sanitized components.
        return urlparse.urlunparse((scheme, netloc, path, params,
            query, fragment))


def makedirs(pathname):
        """Create a directory at the specified location if it does not
        already exist (including any parent directories) re-raising any
        unexpected exceptions as ApiExceptions.
        """

        try:
                os.makedirs(pathname, PKG_DIR_MODE)
        except EnvironmentError, e:
                if e.filename == pathname and (e.errno == errno.EEXIST or
                    os.path.exists(e.filename)):
                        return
                elif e.errno == errno.EACCES:
                        raise api_errors.PermissionsException(
                            e.filename)
                elif e.errno == errno.EROFS:
                        raise api_errors.ReadOnlyFileSystemException(
                            e.filename)
                elif e.errno != errno.EEXIST or e.filename != pathname:
                        raise

class DummyLock(object):
        """This has the same external interface as threading.Lock,
        but performs no locking.  This is a placeholder object for situations
        where we want to be able to do locking, but don't always need a
        lock object present.  The object has a held value, that is used
        for _is_owned.  This is informational and doesn't actually
        provide mutual exclusion in any way whatsoever."""
        # Missing docstring; pylint: disable=C0111

        def __init__(self):
                self.held = False

        def acquire(self, blocking=1):
                # Unused argument; pylint: disable=W0613
                self.held = True
                return True

        def release(self):
                self.held = False
                return

        def _is_owned(self):
                return self.held

        @property
        def locked(self):
                return self.held


class Singleton(type):
        """Set __metaclass__ to Singleton to create a singleton.
        See http://en.wikipedia.org/wiki/Singleton_pattern """

        def __init__(mcs, name, bases, dictionary):
                super(Singleton, mcs).__init__(name, bases, dictionary)
                mcs.instance = None

        def __call__(mcs, *args, **kw):
                if mcs.instance is None:
                        mcs.instance = super(Singleton, mcs).__call__(*args,
                            **kw)

                return mcs.instance


EmptyDict = ImmutableDict()

# Setting the python file buffer size to 128k gives substantial performance
# gains on certain files.
PKG_FILE_BUFSIZ = 128 * 1024

PKG_FILE_MODE = S_IWUSR | S_IRUSR | S_IRGRP | S_IROTH
PKG_DIR_MODE = (S_IRWXU | S_IRGRP | S_IXGRP | S_IROTH | S_IXOTH)
PKG_RO_FILE_MODE = S_IRUSR | S_IRGRP | S_IROTH

def relpath(path, start="."):
        """Version of relpath to workaround python bug:
            http://bugs.python.org/issue5117
        """
        if path and start and start == "/" and path[0] == "/":
                return path.lstrip("/")
        return os.path.relpath(path, start=start)

def recursive_chown_dir(d, uid, gid):
        """Change the ownership of all files under directory d to uid:gid."""
        for dirpath, dirnames, filenames in os.walk(d):
                for name in dirnames:
                        path = os.path.join(dirpath, name)
                        portable.chown(path, uid, gid)
                for name in filenames:
                        path = os.path.join(dirpath, name)
                        portable.chown(path, uid, gid)
def opts_parse(op, api_inst, args, table, pargs_limit, usage_cb):
        """Generic table-based options parsing function.  Returns a tuple
        consisting of a dictionary of parsed options and the remaining
        unparsed options.

        'op' is the operation being performed.

        'api_inst' is an image api object that is passed to options handling
        callbacks (passed in via 'table').

        'args' is the arguments that should be parsed.

        'table' is a list of options and callbacks.Each entry is either a
        a tuple or a callback function.

        tuples in 'table' specify allowable options and have the following
        format:

                (<short opt>, <long opt>, <key>, <default value>)

        An example of a short opt is "f", which maps to a "-f" option.  An
        example of a long opt is "foo", which maps to a "--foo" option.  Key
        is the value of this option in the parsed option dictionary.  The
        default value not only represents the default value assigned to the
        option, but it also implicitly determines how the option is parsed.  If
        the default value is True or False, the option doesn't take any
        arguments, can only be specified once, and if specified it inverts the
        default value.  If the default value is 0, the option doesn't take any
        arguments, can be specified multiple times, and if specified its value
        will be the number of times it was seen.  If the default value is
        None, the option requires an argument, can only be specified once, and
        if specified its value will be its argument string.  If the default
        value is an empty list, the option requires an argument, may be
        specified multiple times, and if specified its value will be a list
        with all the specified argument values.

        callbacks in 'table' specify callback functions that are invoked after
        all options have been parsed.  Callback functions must have the
        following signature:
                callback(api_inst, opts, opts_new)

        The opts parameter is a dictionary containing all the raw, parsed
        options.  Callbacks should never update the contents of this
        dictionary.  The opts_new parameter is a dictionary which is initially
        a copy of the opts dictionary.  This is the dictionary that will be
        returned to the caller of opts_parse().  If a callback function wants
        to update the arguments dictionary that will be returned to the
        caller, they should make all their updates to the opts_new dictionary.

        'pargs_limit' specified how to handle extra arguments not parsed by
        getops.  A value of -1 indicates that we allow an unlimited number of
        extra arguments.  A value of 0 or greater indicates the number of
        allowed additional unparsed options.

        'usage_cb' is a function pointer that should display usage information
        and will be invoked if invalid arguments are detected."""


        assert type(table) == list

        # return dictionary
        rv = dict()

        # option string passed to getopt
        opts_s_str = ""
        # long options list passed to getopt
        opts_l_list = list()

        # dict to map options returned by getopt to keys
        opts_keys = dict()

        # sanity checking to make sure each option is unique
        opts_s_set = set()
        opts_l_set = set()
        opts_seen = dict()

        # callbacks to invoke after processing options
        callbacks = []

        # process each option entry
        for entry in table:
                # check for a callback
                if type(entry) != tuple:
                        callbacks.append(entry)
                        continue

                # decode the table entry
                # s: a short option, ex: -f
                # l: a long option, ex: --foo
                # k: the key value for the options dictionary
                # v: the default value
                (s, l, k, v) = entry

                # make sure an option was specified
                assert s or l
                # sanity check the default value
                assert (v == None) or (v == []) or \
                    (type(v) == bool) or (type(v) == int)
                # make sure each key is unique
                assert k not in rv
                # initialize the default return dictionary entry.
                rv[k] = v
                if l:
                        # make sure each option is unique
                        assert set([l]) not in opts_l_set
                        opts_l_set |= set([l])

                        if type(v) == bool:
                                v = not v
                                opts_l_list.append("%s" % l)
                        elif type(v) == int:
                                opts_l_list.append("%s" % l)
                        else:
                                opts_l_list.append("%s=" % l)
                        opts_keys["--%s" % l] = k
                if s:
                        # make sure each option is unique
                        assert set([s]) not in opts_s_set
                        opts_s_set |= set([s])

                        if type(v) == bool:
                                v = not v
                                opts_s_str += "%s" % s
                        elif type(v) == int:
                                opts_s_str += "%s" % s
                        else:
                                opts_s_str += "%s:" % s
                        opts_keys["-%s" % s] = k

        # parse options
        try:
                opts, pargs = getopt.getopt(args, opts_s_str, opts_l_list)
        except getopt.GetoptError, e:
                usage_cb(_("illegal option -- %s") % e.opt, cmd=op)

        if (pargs_limit >= 0) and (pargs_limit < len(pargs)):
                usage_cb(_("illegal argument -- %s") % pargs[pargs_limit],
                    cmd=op)

        # update options dictionary with the specified options
        for opt, arg in opts:
                k = opts_keys[opt]
                v = rv[k]

                # check for duplicate options
                if k in opts_seen and (type(v) != list and type(v) != int):
                        if opt == opts_seen[k]:
                                usage_cb(_("option '%s' repeated") % opt,
                                    cmd=op)
                        usage_cb(_("'%(optA)s' and '%(optB)s' have the same "
                            "meaning") % {"optA": opts_seen[k], "optB": opt},
                            cmd=op)
                opts_seen[k] = opt

                # update the return dict value
                if type(v) == bool:
                        rv[k] = not rv[k]
                elif type(v) == list:
                        rv[k].append(arg)
                elif type(v) == int:
                        rv[k] += 1
                else:
                        rv[k] = arg

        # invoke callbacks (cast to set() to eliminate dups)
        rv_updated = rv.copy()
        for cb in set(callbacks):
                cb(op, api_inst, rv, rv_updated)

        return (rv_updated, pargs)

def api_cmdpath():
        """Returns the path to the executable that is invoking the api client
        interfaces."""

        cmdpath = None

        if global_settings.client_args[0]:
                cmdpath = os.path.realpath(os.path.join(sys.path[0],
                    os.path.basename(global_settings.client_args[0])))

        if "PKG_CMDPATH" in os.environ:
                cmdpath = os.environ["PKG_CMDPATH"]

        # DebugValues is a singleton, hence no 'self' arg; pylint: disable=E1120
        if DebugValues.get_value("simulate_cmdpath"):
                cmdpath = DebugValues.get_value("simulate_cmdpath")

        return cmdpath

def api_pkgcmd():
        """When running a pkg(1) command from within a packaging module, try
        to use the same pkg(1) path as our current invocation.  If we're
        running pkg(1) from some other command (like the gui updater) then
        assume that pkg(1) is in the default path."""

        pkg_bin = "pkg"
        cmdpath = api_cmdpath()
        if cmdpath and os.path.basename(cmdpath) == "pkg":
                try:
                        # check if the currently running pkg command
                        # exists and is accessible.
                        os.stat(cmdpath)
                        pkg_bin = cmdpath
                except OSError:
                        pass

        pkg_cmd = [pkg_bin]

        # propagate debug options
        for k, v in DebugValues.iteritems():
                pkg_cmd.append("-D")
                pkg_cmd.append("%s=%s" % (k, v))

        return pkg_cmd

def liveroot():
        """Return path to the current live root image, i.e. the image
        that we are running from."""

        # DebugValues is a singleton, hence no 'self' arg; pylint: disable=E1120
        live_root = DebugValues.get_value("simulate_live_root")
        if not live_root and "PKG_LIVE_ROOT" in os.environ:
                live_root = os.environ["PKG_LIVE_ROOT"]
        if not live_root:
                live_root = "/"
        return live_root

def spaceavail(path):
        """Find out how much space is available at the specified path if
        it exists; return -1 if path doesn't exist"""
        try:
                res = os.statvfs(path)
                return res.f_frsize * res.f_bavail
        except OSError:
                return -1

def get_dir_size(path):
        """Return the size (in bytes) of a directory and all of its contents."""
        try:
                return sum(
                    os.path.getsize(os.path.join(d, fname))
                    for d, dnames, fnames in os.walk(path)
                    for fname in fnames
                )
        except EnvironmentError, e:
                # Access to protected member; pylint: disable=W0212
                raise api_errors._convert_error(e)

def get_listing(desired_field_order, field_data, field_values, out_format,
    def_fmt, omit_headers, escape_output=True):
        """Returns a string containing a listing defined by provided values
        in the specified output format.

        'desired_field_order' is the list of the fields to show in the order
        they should be output left to right.

        'field_data' is a dictionary of lists of the form:
          {
            field_name1: {
              [(output formats), field header, initial field value]
            },
            field_nameN: {
              [(output formats), field header, initial field value]
            }
          }

        'field_values' is a generator or list of dictionaries of the form:
          {
            field_name1: field_value,
            field_nameN: field_value
          }

        'out_format' is the format to use for output.  Currently 'default',
        'tsv', 'json', and 'json-formatted' are supported.  The first is
        intended for columnar, human-readable output, and the others for
        parsable output.

        'def_fmt' is the default Python formatting string to use for the
        'default' human-readable output.  It must match the fields defined
        in 'field_data'.

        'omit_headers' is a boolean specifying whether headers should be
        included in the listing.  (If applicable to the specified output
        format.)

        'escape_output' is an optional boolean indicating whether shell
        metacharacters or embedded control sequences should be escaped
        before display.  (If applicable to the specified output format.)
        """
        # Missing docstring; pylint: disable=C0111

        # Custom sort function for preserving field ordering
        def sort_fields(one, two):
                return desired_field_order.index(get_header(one)) - \
                    desired_field_order.index(get_header(two))

        # Functions for manipulating field_data records
        def filter_default(record):
                return "default" in record[0]

        def filter_tsv(record):
                return "tsv" in record[0]

        def get_header(record):
                return record[1]

        def get_value(record):
                return record[2]

        def quote_value(val):
                if out_format == "tsv":
                        # Expand tabs if tsv output requested.
                        val = val.replace("\t", " " * 8)
                nval = val
                # Escape bourne shell metacharacters.
                for c in ("\\", " ", "\t", "\n", "'", "`", ";", "&", "(", ")",
                    "|", "^", "<", ">"):
                        nval = nval.replace(c, "\\" + c)
                return nval

        def set_value(entry):
                val = entry[1]
                multi_value = False
                if isinstance(val, (list, tuple, set, frozenset)):
                        multi_value = True
                elif val == "":
                        entry[0][2] = '""'
                        return
                elif val is None:
                        entry[0][2] = ''
                        return
                else:
                        val = [val]

                nval = []
                for v in val:
                        if v == "":
                                # Indicate empty string value using "".
                                nval.append('""')
                        elif v is None:
                                # Indicate no value using empty string.
                                nval.append('')
                        elif escape_output:
                                # Otherwise, escape the value to be displayed.
                                nval.append(quote_value(str(v)))
                        else:
                                # Caller requested value not be escaped.
                                nval.append(str(v))

                val = " ".join(nval)
                nval = None
                if multi_value:
                        val = "(%s)" % val
                entry[0][2] = val

        if out_format == "default":
                # Create a formatting string for the default output
                # format.
                fmt = def_fmt
                filter_func = filter_default
        elif out_format == "tsv":
                # Create a formatting string for the tsv output
                # format.
                num_fields = sum(
                    1 for k in field_data
                    if filter_tsv(field_data[k])
                )
                fmt = "\t".join('%s' for x in xrange(num_fields))
                filter_func = filter_tsv
        elif out_format == "json" or out_format == "json-formatted":
                args = { "sort_keys": True }
                if out_format == "json-formatted":
                        args["indent"] = 2

                # 'json' formats always include any extra fields returned;
                # any explicitly named fields are only included if 'json'
                # is explicitly listed.
                def fmt_val(v):
                        if isinstance(v, basestring):
                                return v
                        if isinstance(v, (list, tuple, set, frozenset)):
                                return [fmt_val(e) for e in v]
                        if isinstance(v, dict):
                                for k, e in v.iteritems():
                                        v[k] = fmt_val(e)
                                return v
                        return str(v)

                output = json.dumps([
                    dict(
                        (k, fmt_val(entry[k]))
                        for k in entry
                        if k not in field_data or "json" in field_data[k][0]
                    )
                    for entry in field_values
                ], **args)

                if out_format == "json-formatted":
                        # Include a trailing newline for readability.
                        return output + "\n"
                return output

        # Extract the list of headers from the field_data dictionary.  Ensure
        # they are extracted in the desired order by using the custom sort
        # function.
        hdrs = map(get_header, sorted(filter(filter_func, field_data.values()),
            sort_fields))

        # Output a header if desired.
        output = ""
        if not omit_headers:
                output += fmt % tuple(hdrs)
                output += "\n"

        for entry in field_values:
                map(set_value, (
                    (field_data[f], v)
                    for f, v in entry.iteritems()
                    if f in field_data
                ))
                values = map(get_value, sorted(filter(filter_func,
                    field_data.values()), sort_fields))
                output += fmt % tuple(values)
                output += "\n"

        return output

def truncate_file(f, size=0):
        """Truncate the specified file."""
        try:
                f.truncate(size)
        except IOError:
                pass
        except OSError, e:
                # Access to protected member; pylint: disable=W0212
                raise api_errors._convert_error(e)

def flush_output():
        """flush stdout and stderr"""

        try:
                sys.stdout.flush()
        except IOError:
                pass
        except OSError, e:
                # Access to protected member; pylint: disable=W0212
                raise api_errors._convert_error(e)

        try:
                sys.stderr.flush()
        except IOError:
                pass
        except OSError, e:
                # Access to protected member; pylint: disable=W0212
                raise api_errors._convert_error(e)

# valid json types
json_types_immediates = (bool, float, int, long, str, type(None), unicode)
json_types_collections = (dict, list)
json_types = tuple(json_types_immediates + json_types_collections)
json_debug = False

def json_encode(name, data, desc, commonize=None, je_state=None):
        """A generic json encoder.

        'name' a descriptive name of the data we're encoding.  If encoding a
        class, this would normally be the class name.  'name' is used when
        displaying errors to identify the data that caused the errors.

        'data' data to encode.

        'desc' a description of the data to encode.

        'commonize' a list of objects that should be cached by reference.
        this is used when encoding objects which may contain multiple
        references to a single object.  In this case, each reference will be
        replaced with a unique id, and the object that was pointed to will
        only be encoded once.  This ensures that upon decoding we can restore
        the original object and all references to it."""

        # debugging
        if je_state is None and json_debug:
                print >> sys.stderr, "json_encode name: ", name
                print >> sys.stderr, "json_encode data: ", data

        # we don't encode None
        if data is None:
                return None

        # initialize parameters to default
        if commonize is None:
                commonize = frozenset()

        if je_state is None:
                # this is the first invocation of this function, so "data"
                # points to the top-level object that we want to encode.  this
                # means that if we're commonizing any objects we should
                # finalize the object cache when we're done encoding this
                # object.
                finish = True

                # initialize recursion state
                obj_id = [0]
                obj_cache = {}
                je_state = [obj_id, obj_cache, commonize]
        else:
                # we're being invoked recursively, do not finalize the object
                # cache (since that will be done by a previous invocation of
                # this function).
                finish = False

                # get recursion state
                obj_id, obj_cache, commonize_old = je_state

                # check if we're changing the set of objects to commonize
                if not commonize:
                        commonize = commonize_old
                else:
                        # update the set of objects to commonize
                        # make a copy so we don't update our callers state
                        commonize = frozenset(commonize_old | commonize)
                        je_state = [obj_id, obj_cache, commonize]

        # verify state
        assert type(name) == str
        assert type(obj_cache) == dict
        assert type(obj_id) == list and len(obj_id) == 1 and obj_id[0] >= 0
        assert type(commonize) == frozenset
        assert type(je_state) == list and len(je_state) == 3

        def je_return(name, data, finish, je_state):
                """if necessary, finalize the object cache and merge it into
                the state data.

                while encoding, the object cache is a dictionary which
                contains tuples consisting of an assigned unique object id
                (obj_id) and an encoded object.  these tuples are hashed by
                the python object id of the original un-encoded python object.
                so the hash contains:

                       { id(<obj>): ( <obj_id>, <obj_state> ) }

                when we finish the object cache we update it so that it
                contains just encoded objects hashed by their assigned object
                id (obj_id).  so the hash contains:

                       { str(<obj_id>): <obj_state> }

                then we merge the state data and object cache into a single
                dictionary and return that.
                """
                # Unused argument; pylint: disable=W0613

                if not finish:
                        return data

                # json.dump converts integer dictionary keys into strings, so
                # we'll convert the object id keys (which are integers) into
                # strings (that way we're encoder/decoder independent).
                obj_cache = je_state[1]
                obj_cache2 = {}
                for obj_id, obj_state in obj_cache.itervalues():
                        obj_cache2[str(obj_id)] = obj_state

                data = { "json_state": data, "json_objects": obj_cache2 }

                if DebugValues["plandesc_validate"]:
                        json_validate(name, data)

                # debugging
                if json_debug:
                        print >> sys.stderr, "json_encode finished name: ", name
                        print >> sys.stderr, "json_encode finished data: ", data

                return data

        # check if the description is a type object
        if isinstance(desc, type):
                desc_type = desc
        else:
                # get the expected data type from the description
                desc_type = type(desc)

        # get the data type
        data_type = getattr(data, "__metaclass__", type(data))

        # sanity check that the data type matches the description
        assert desc_type == data_type, \
            "unexpected %s for %s, expected: %s, value: %s" % \
                (data_type, name, desc_type, data)

        # we don't need to do anything for basic types
        if desc_type in json_types_immediates:
                return je_return(name, data, finish, je_state)

        # encode elements nested in a dictionary like object
        # return elements in a dictionary
        if desc_type in (dict, collections.defaultdict):
                # we always return a new dictionary
                rv = {}

                # check if we're not encoding nested elements
                if len(desc) == 0:
                        rv.update(data)
                        return je_return(name, rv, finish, je_state)

                # lookup the first descriptor to see if we have
                # generic type description.
                desc_k, desc_v = desc.items()[0]

                # if the key in the first type pair is a type then we
                # have a generic type description that applies to all
                # keys and values in the dictionary.
                # check if the description is a type object
                if isinstance(desc_k, type):
                        # there can only be one generic type desc
                        assert len(desc) == 1

                        # encode all key / value pairs
                        for k, v in data.iteritems():
                                # encode the key
                                name2 = "%s[%s].key()" % (name, desc_k)
                                k2 = json_encode(name2, k, desc_k,
                                    je_state=je_state)

                                # encode the value
                                name2 = "%s[%s].value()" % (name, desc_k)
                                v2 = json_encode(name2, v, desc_v,
                                    je_state=je_state)

                                # save the result
                                rv[k2] = v2
                        return je_return(name, rv, finish, je_state)

                # we have element specific value type descriptions.
                # encode the specific values.
                rv.update(data)
                for desc_k, desc_v in desc.iteritems():
                        # check for the specific key
                        if desc_k not in rv:
                                continue

                        # encode the value
                        name2 = "%s[%s].value()" % (name, desc_k)
                        rv[desc_k] = json_encode(name2, rv[desc_k], desc_v,
                            je_state=je_state)
                return je_return(name, rv, finish, je_state)

        # encode elements nested in a list like object
        # return elements in a list
        if desc_type in (tuple, list, set, frozenset):

                # we always return a new list
                rv = []

                # check for an empty list since we use izip_longest
                if len(data) == 0:
                        return je_return(name, rv, finish, je_state)

                # check if we're not encoding nested elements
                if len(desc) == 0:
                        rv.extend(data)
                        return je_return(name, rv, finish, je_state)

                # don't accidentally generate data via izip_longest
                assert len(data) >= len(desc), \
                    "%d >= %d" % (len(data), len(desc))

                i = 0
                for data2, desc2 in itertools.izip_longest(data, desc,
                    fillvalue=list(desc)[0]):
                        name2 = "%s[%i]" % (name, i)
                        i += 1
                        rv.append(json_encode(name2, data2, desc2,
                            je_state=je_state))
                return je_return(name, rv, finish, je_state)

        # if we're commonizing this object and it's already been encoded then
        # just return its encoded object id.
        if desc_type in commonize and id(data) in obj_cache:
                rv = obj_cache[id(data)][0]
                return je_return(name, rv, finish, je_state)

        # find an encoder for this class, which should be:
        #     <class>.getstate(obj, je_state)
        encoder = getattr(desc_type, "getstate", None)
        assert encoder is not None, "no json encoder for: %s" % desc_type

        # encode the data
        rv = encoder(data, je_state)
        assert rv is not None, "json encoder returned none for: %s" % desc_type

        # if we're commonizing this object, then assign it an object id and
        # save that object id and the encoded object into the object cache
        # (which is indexed by the python id for the object).
        if desc_type in commonize:
                obj_cache[id(data)] = (obj_id[0], rv)
                rv = obj_id[0]
                obj_id[0] += 1

        # return the encoded element
        return je_return(name, rv, finish, je_state)

def json_decode(name, data, desc, commonize=None, jd_state=None):
        """A generic json decoder.

        'name' a descriptive name of the data.  (used to identify unexpected
        data errors.)

        'desc' a programmatic description of data types.

        'data' data to decode."""

        # debugging
        if jd_state is None and json_debug:
                print >> sys.stderr, "json_decode name: ", name
                print >> sys.stderr, "json_decode data: ", data

        # we don't decode None
        if data is None:
                return (data)

        # initialize parameters to default
        if commonize is None:
                commonize = frozenset()

        if jd_state is None:
                # this is the first invocation of this function, so when we
                # return we're done decoding data.
                finish = True

                # first time here, initialize recursion state
                if not commonize:
                        # no common state
                        obj_cache = {}
                else:
                        # load commonized state
                        obj_cache = data["json_objects"]
                        data = data["json_state"]
                jd_state = [obj_cache, commonize]
        else:
                # we're being invoked recursively.
                finish = False

                obj_cache, commonize_old = jd_state

                # check if the first object using commonization
                if not commonize_old and commonize:
                        obj_cache = data["json_objects"]
                        data = data["json_state"]

                # merge in any new commonize requests
                je_state_changed = False

                # check if we're updating the set of objects to commonize
                if not commonize:
                        commonize = commonize_old
                else:
                        # update the set of objects to commonize
                        # make a copy so we don't update our callers state.
                        commonize = frozenset(commonize_old | commonize)
                        je_state_changed = True

                if je_state_changed:
                        jd_state = [obj_cache, commonize]

        # verify state
        assert type(name) == str, "type(name) == %s" % type(name)
        assert type(obj_cache) == dict
        assert type(commonize) == frozenset
        assert type(jd_state) == list and len(jd_state) == 2

        def jd_return(name, data, desc, finish, jd_state):
                """Check if we're done decoding data."""
                # Unused argument; pylint: disable=W0613

                # check if the description is a type object
                if isinstance(desc, type):
                        desc_type = desc
                else:
                        # get the expected data type from the description
                        desc_type = type(desc)

                # get the data type
                data_type = getattr(data, "__metaclass__", type(data))

                # sanity check that the data type matches the description
                assert desc_type == data_type, \
                    "unexpected %s for %s, expected: %s, value: %s" % \
                        (data_type, name, desc_type, rv)

                if not finish:
                        return data

                # debugging
                if json_debug:
                        print >> sys.stderr, "json_decode finished name: ", name
                        print >> sys.stderr, "json_decode finished data: ", data
                return data

        # check if the description is a type object
        if isinstance(desc, type):
                desc_type = desc
        else:
                # get the expected data type from the description
                desc_type = type(desc)

        # we don't need to do anything for basic types
        if desc_type in json_types_immediates:
                rv = None
                return jd_return(name, data, desc, finish, jd_state)

        # decode elements nested in a dictionary
        # return elements in the specified dictionary like object
        if isinstance(desc, dict):

                # allocate the return object.  we don't just use
                # type(desc) because that won't work for things like
                # collections.defaultdict types.
                rv = desc.copy()
                rv.clear()

                # check if we're not decoding nested elements
                if len(desc) == 0:
                        rv.update(data)
                        return jd_return(name, rv, desc, finish, jd_state)

                # lookup the first descriptor to see if we have
                # generic type description.
                desc_k, desc_v = desc.items()[0]

                # if the key in the descriptor is a type then we have
                # a generic type description that applies to all keys
                # and values in the dictionary.
                # check if the description is a type object
                if isinstance(desc_k, type):
                        # there can only be one generic type desc
                        assert len(desc) == 1

                        # decode all key / value pairs
                        for k, v in data.iteritems():
                                # decode the key
                                name2 = "%s[%s].key()" % (name, desc_k)
                                k2 = json_decode(name2, k, desc_k,
                                    jd_state=jd_state)

                                # decode the value
                                name2 = "%s[%s].value()" % (name, desc_k)
                                v2 = json_decode(name2, v, desc_v,
                                    jd_state=jd_state)

                                # save the result
                                rv[k2] = v2
                        return jd_return(name, rv, desc, finish, jd_state)

                # we have element specific value type descriptions.
                # copy all data and then decode the specific values
                rv.update(data)
                for desc_k, desc_v in desc.iteritems():
                        # check for the specific key
                        if desc_k not in rv:
                                continue

                        # decode the value
                        name2 = "%s[%s].value()" % (name, desc_k)
                        rv[desc_k] = json_decode(name2, rv[desc_k],
                            desc_v, jd_state=jd_state)
                return jd_return(name, rv, desc, finish, jd_state)

        # decode elements nested in a list
        # return elements in the specified list like object
        if isinstance(desc, (tuple, list, set, frozenset)):
                # get the return type
                rvtype = type(desc)

                # check for an empty list since we use izip_longest
                if len(data) == 0:
                        rv = rvtype([])
                        return jd_return(name, rv, desc, finish, jd_state)

                # check if we're not encoding nested elements
                if len(desc) == 0:
                        rv = rvtype(data)
                        return jd_return(name, rv, desc, finish, jd_state)

                # don't accidentally generate data via izip_longest
                assert len(data) >= len(desc), \
                    "%d >= %d" % (len(data), len(desc))

                rv = []
                i = 0
                for data2, desc2 in itertools.izip_longest(data, desc,
                    fillvalue=list(desc)[0]):
                        name2 = "%s[%i]" % (name, i)
                        i += 1
                        rv.append(json_decode(name2, data2, desc2,
                            jd_state=jd_state))
                rv = rvtype(rv)
                return jd_return(name, rv, desc, finish, jd_state)

        # find a decoder for this data, which should be:
        #     <class>.fromstate(state, jd_state)
        decoder = getattr(desc_type, "fromstate", None)
        assert decoder is not None, "no json decoder for: %s" % desc_type

        # if this object was commonized then get a reference to it from the
        # object cache.
        if desc_type in commonize:
                assert type(data) == int
                # json.dump converts integer dictionary keys into strings, so
                # obj_cache was indexed by integer strings.
                data = str(data)
                rv = obj_cache[data]

                # get the data type
                data_type = getattr(rv, "__metaclass__", type(rv))

                if data_type != desc_type:
                        # this commonized object hasn't been decoded yet
                        # decode it and update the cache with the decoded obj
                        rv = decoder(rv, jd_state)
                        obj_cache[data] = rv
        else:
                # decode the data
                rv = decoder(data, jd_state)

        return jd_return(name, rv, desc, finish, jd_state)

def json_validate(name, data):
        """Validate that a named piece of data can be represented in json and
        that the data can be passed directly to json.dump().  If the data
        can't be represented as json we'll trigger an assert.

        'name' is the name of the data to validate

        'data' is the data to validate

        'recurse' is an optional integer that controls recursion.  if it's a
        negative number (the default) we recursively check any nested lists or
        dictionaries.  if it's a positive integer than we only recurse to
        the specified depth."""

        assert isinstance(data, json_types), \
            "invalid json type \"%s\" for \"%s\", value: %s" % \
            (type(data), name, str(data))

        if type(data) == dict:
                for k in data:
                        # json.dump converts integer dictionary keys into
                        # strings, which is a bit unexpected.  so make sure we
                        # don't have any of those.
                        assert type(k) != int, \
                            "integer dictionary keys detected for: %s" % name

                        # validate the key and the value
                        new_name = "%s[%s].key()" % (name, k)
                        json_validate(new_name, k)
                        new_name = "%s[%s].value()" % (name, k)
                        json_validate(new_name, data[k])

        if type(data) == list:
                for i in range(len(data)):
                        new_name = "%s[%i]" % (name, i)
                        json_validate(new_name, data[i])

def json_diff(name, d0, d1, alld0, alld1):
        """Compare two json encoded objects to make sure they are
        identical, assert() if they are not."""

        def dbg():
                """dump debug info for json_diff"""
                def d(s):
                        """dbg helper"""
                        return json.dumps(s, sort_keys=True, indent=4)
                return "\n--- d0\n" + d(d0) + "\n+++ d1\n" + d(d1) + \
                    "\n--- alld0\n" + d(alld0) + "\n+++ alld1\n" + d(alld1)

        assert type(d0) == type(d1), ("Json data types differ for \"%s\":\n"
                "type 1: %s\ntype 2: %s\n") % (name, type(d0), type(d1)) + dbg()

        if type(d0) == dict:
                assert set(d0) == set(d1), (
                   "Json dictionary keys differ for \"%s\":\n"
                   "dict 1 missing: %s\n"
                   "dict 2 missing: %s\n") % (name,
                   set(d1) - set(d0), set(d0) - set(d1)) + dbg()

                for k in d0:
                        new_name = "%s[%s]" % (name, k)
                        json_diff(new_name, d0[k], d1[k], alld0, alld1)

        if type(d0) == list:
                assert len(d0) == len(d1), (
                   "Json list lengths differ for \"%s\":\n"
                   "list 1 length: %s\n"
                   "list 2 length: %s\n") % (name,
                   len(d0), len(d1)) + dbg()

                for i in range(len(d0)):
                        new_name = "%s[%i]" % (name, i)
                        json_diff(new_name, d0[i], d1[i], alld0, alld1)

class Timer(object):
        """A class which can be used for measuring process times (user,
        system, and wait)."""

        __precision = 3
        __log_fmt = "utime: %7.3f; stime: %7.3f; wtime: %7.3f"

        def __init__(self, module):
                self.__module = module
                self.__timings = []

                # we initialize our time values to account for all time used
                # since the start of the process.  (user and system time are
                # obtained relative to process start time, but wall time is an
                # absolute time value so here we initialize out initial wall
                # time value to the time our process was started.)
                self.__utime = self.__stime = 0
                self.__wtime = _prstart()

        def __zero1(self, delta):
                """Return True if a number is zero (up to a certain level of
                precision.)"""
                return int(delta * (10 ** self.__precision)) == 0

        def __zero(self, udelta, sdelta, wdelta):
                """Return True if all the passed in values are zero."""
                return self.__zero1(udelta) and \
                    self.__zero1(sdelta) and \
                    self.__zero1(wdelta)

        def __str__(self):
                s = "\nTimings for %s: [\n" % self.__module
                utotal = stotal = wtotal = 0
                phases = [i[0] for i in self.__timings] + ["total"]
                phase_width = max([len(i) for i in phases]) + 1
                fmt = "  %%-%ss %s;\n" % (phase_width, Timer.__log_fmt)
                for phase, udelta, sdelta, wdelta in self.__timings:
                        if self.__zero(udelta, sdelta, wdelta):
                                continue
                        utotal += udelta
                        stotal += sdelta
                        wtotal += wdelta
                        s += fmt % (phase + ":", udelta, sdelta, wdelta)
                s += fmt % ("total:", utotal, stotal, wtotal)
                s += "]\n"
                return s

        def reset(self):
                """Update saved times to current process values."""
                self.__utime, self.__stime, self.__wtime = self.__get_time()

        @staticmethod
        def __get_time():
                """Get current user, system, and wait times for this
                process."""

                rusage = resource.getrusage(resource.RUSAGE_SELF)
                utime = rusage[0]
                stime = rusage[1]
                wtime = time.time()
                return (utime, stime, wtime)

        def record(self, phase, logger=None):
                """Record the difference between the previously saved process
                time values and the current values.  Then update the saved
                values to match the current values"""

                utime, stime, wtime = self.__get_time()

                udelta = utime - self.__utime
                sdelta = stime - self.__stime
                wdelta = wtime - self.__wtime

                self.__timings.append((phase, udelta, sdelta, wdelta))
                self.__utime, self.__stime, self.__wtime = utime, stime, wtime

                rv = "%s: %s: " % (self.__module, phase)
                rv += Timer.__log_fmt % (udelta, sdelta, wdelta)
                if logger:
                        logger.debug(rv)
                return rv


class AsyncCallException(Exception):
        """Exception class for AsyncCall() errors.

        Any exceptions caught by the async call thread get bundled into this
        Exception because otherwise we'll lose the stack trace associated with
        the original exception."""

        def __init__(self, e=None):
                Exception.__init__(self)
                self.e = e
                self.tb = None

        def __str__(self):
                if self.tb:
                        return str(self.tb) + str(self.e)
                return str(self.e)


class AsyncCall(object):
        """Class which can be used to call a function asynchronously.
        The call is performed via a dedicated thread."""

        def __init__(self):
                self.rv = None
                self.e = None

                # keep track of what's been done
                self.started = False

                # internal state
                self.__thread = None

                # pre-allocate an exception that we'll used in case everything
                # goes horribly wrong.
                self.__e = AsyncCallException(
                    Exception("AsyncCall Internal Error"))

        def __thread_cb(self, dummy, cb, *args, **kwargs):
                """Dedicated call thread.

                'dummy' is a dummy parameter that is not used.  this is done
                because the threading module (which invokes this function)
                inspects the first argument of "args" to check if it's
                iterable, and that may cause bizarre failures if cb is a
                dynamically bound class (like xmlrpclib._Method).

                We need to be careful here and catch all exceptions.  Since
                we're executing in our own thread, any exceptions we don't
                catch get dumped to the console."""
                # Catch "Exception"; pylint: disable=W0703

                try:
                        if DebugValues["async_thread_error"]:
                                raise Exception("async_thread_error")

                        rv = e = None
                        try:
                                rv = cb(*args, **kwargs)
                        except Exception, e:
                                self.e = self.__e
                                self.e.e = e
                                self.e.tb = traceback.format_exc()
                                return

                        self.rv = rv

                except Exception, e:
                        # if we raise an exception here, we're hosed
                        self.rv = None
                        self.e = self.__e
                        self.e.e = e
                        try:
                                if DebugValues["async_thread_error"]:
                                        raise Exception("async_thread_error")
                                self.e.tb = traceback.format_exc()
                        except Exception:
                                pass

        def start(self, cb, *args, **kwargs):
                """Start a call to an rpc server."""

                assert not self.started
                self.started = True
                # prepare the arguments for the thread
                if args:
                        args = (0, cb) + args
                else:
                        args = (0, cb)

                # initialize and return the thread
                self.__thread = threading.Thread(target=self.__thread_cb,
                    args=args, kwargs=kwargs)
                self.__thread.daemon = True
                self.__thread.start()

        def join(self):
                """Wait for an rpc call to finish."""
                assert self.started
                self.__thread.join()

        def is_done(self):
                """Check if an rpc call is done."""
                assert self.started
                return not self.__thread.is_alive()

        def result(self):
                """Finish a call to an rpc server."""
                assert self.started
                # wait for the async call thread to exit
                self.join()
                assert self.is_done()
                if self.e:
                        # if the calling thread hit an exception, re-raise it
                        # Raising NoneType; pylint: disable=E0702
                        raise self.e
                return self.rv


def get_runtime_proxy(proxy, uri):
        """Given a proxy string and a URI we want to access using it, determine
        whether any OS environment variables should override that value.

        The special value "-" is returned when a no_proxy environment variable
        was found which should apply to this URI, indicating that no proxy
        should be used at runtime."""

        runtime_proxy = proxy
        # There is no upper case version of http_proxy, according to curl(1)
        environ_http_proxy = os.environ.get("http_proxy")
        environ_https_proxy = os.environ.get("https_proxy")
        environ_https_proxy_upper = os.environ.get("HTTPS_PROXY")
        environ_all_proxy = os.environ.get("all_proxy")
        environ_all_proxy_upper = os.environ.get("ALL_PROXY")

        no_proxy = os.environ.get("no_proxy", [])
        no_proxy_upper = os.environ.get("NO_PROXY", [])

        if no_proxy:
                no_proxy = no_proxy.split(",")

        if no_proxy_upper:
                no_proxy_upper = no_proxy_upper.split(",")

        # Give precedence to protocol-specific proxies, and lowercase versions.
        if uri and uri.startswith("http") and environ_http_proxy:
                runtime_proxy = environ_http_proxy
        elif uri and uri.startswith("https") and environ_https_proxy:
                runtime_proxy = environ_https_proxy
        elif uri and uri.startswith("https") and environ_https_proxy_upper:
                runtime_proxy = environ_https_proxy_upper
        elif environ_all_proxy:
                runtime_proxy = environ_all_proxy
        elif environ_all_proxy_upper:
                runtime_proxy = environ_all_proxy_upper

        if no_proxy or no_proxy_upper:
                # SplitResult has a netloc member; pylint: disable=E1103
                netloc = urlparse.urlsplit(uri, allow_fragments=0).netloc
                host = netloc.split(":")[0]
                if host in no_proxy or no_proxy == ["*"]:
                        return "-"
                if host in no_proxy_upper or no_proxy_upper == ["*"]:
                        return "-"

        if not runtime_proxy:
                return

        return runtime_proxy

def decode(s):
        """convert non-ascii strings to unicode;
        replace non-convertable chars"""
        try:
                # this will fail if any 8 bit chars in string
                # this is a nop if string is ascii.
                s = s.encode("ascii")
        except ValueError:
                # this will encode 8 bit strings into unicode
                s = s.decode("utf-8", "replace")
        return s
