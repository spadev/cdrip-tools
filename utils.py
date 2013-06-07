from __future__ import print_function
import os, sys, signal, re, time

from os.path import dirname
from subprocess import Popen, PIPE
from fnmatch import fnmatch
from shutil import rmtree
from argparse import ArgumentTypeError

class DependencyError(Exception):
    """raised when dependency not found"""
class KilledError(Exception):
    """raised when process killed"""
class NotFromCDError(Exception):
    """raised when encountering track that has number of samples
    not divisible by 588"""
class AccurateripError(Exception):
    """raised when there's a problem parsing accuraterip data"""
class SubprocessError(Exception):
    """raised when a subprocess has a nonzero return code"""
class NetworkError(Exception):
    """raised when problem connecting to accuraterip database"""

STATUSES = ['[+----]',
            '[-+---]',
            '[--+--]',
            '[---+-]',
            '[----+]',
            '[---+-]',
            '[--+--]',
            '[-+---]',
            ]
STATUS_INDEX = 0

def which(name, flags=os.X_OK, additional_paths=[]):
    """Search PATH for executable files with the given name.

    On newer versions of MS-Windows, the PATHEXT environment variable will be
    set to the list of file extensions for files considered executable. This
    will normally include things like ".EXE". This fuction will also find files
    with the given name ending with any of these extensions.

    On MS-Windows the only flag that has any meaning is os.F_OK. Any other
    flags will be ignored.

    @type name: C{str}
    @param name: The name for which to search.

    @type flags: C{int}
    @param flags: Arguments to L{os.access}.

    @rtype: C{list}
    @param: A list of the full paths to files found, in the
    order in which they were found.
    """
    result = []
    exts = filter(None, os.environ.get('PATHEXT', '').split(os.pathsep))
    path = os.environ.get('PATH', None)

    if path is None:
        return []
    paths = os.environ.get('PATH', '').split(os.pathsep) + additional_paths
    for p in paths:
        p = os.path.join(p, name)
        if os.access(p, flags):
            result.append(p)
        for e in exts:
            pext = p + e
            if os.access(pext, flags):
                result.append(pext)
    return result

def isfile(value):
    if not os.path.isfile(value):
        raise ArgumentTypeError('%s is not a file' % value)

    return value

def check_dependencies(BIN, REQUIRED):
    for dep in BIN:
        value = which(dep, additional_paths=[dirname(sys.argv[0])])
        altdep = BIN[dep]
        altvalue = which(altdep, additional_paths=[dirname(sys.argv[0])]) \
            if altdep else None
        if not value and not altvalue:
            if dep in REQUIRED:
                raise DependencyError("%s required\n" % dep)
        else:
            BIN[dep] = altvalue[0] if altvalue else value[0]

def add_common_arguments(parser, version):
    parser.add_argument("-v", "--verbose",
                        help="enable verbose output",
                        action='store_true',
                        default=False,
                        )
    parser.add_argument('--version', action='version', version='%%(prog)s %s' %
                        version)

def show_status(msg, *args):
    global STATUS_INDEX
    status = STATUSES[STATUS_INDEX%len(STATUSES)]
    msg = msg % args
    msg = '\r'+msg+' %s   ' % status
    sys.stderr.write(msg)
    sys.stderr.flush()
    time.sleep(0.25)
    STATUS_INDEX += 1

def finish_status(msg=''):
    sys.stderr.write('\n')

def get_num_samples(BIN, path):
    devnull = open(os.devnull, 'w')
    if fnmatch(path.lower(), '*.flac') and BIN['metaflac']:
        p = Popen([BIN['metaflac'], '--show-total-samples', path], stdout=PIPE)
        out, err = p.communicate()
        num_samples = int(out.strip())
    else:
        p = Popen([BIN['ffprobe'], '-show_streams', path], stdout=PIPE,
                  stderr=devnull)
        out, err = p.communicate()
        try:
            dur = float(re.search(b'duration=([0-9.]+)', out).group(1))
        except:
            dur = 0
        num_samples = int(round(dur*44100))

    devnull.close()

    return num_samples

def abort(*args):
    raise KilledError

def execute(main, processes, tempfiles=[], tempdirs=[]):
    try:
        SIGS = [getattr(signal, s, None) for s in
                "SIGINT SIGTERM SIGHUP".split()]
        for sig in filter(None, SIGS):
            signal.signal(sig, abort)
        exitcode = main()
    except KilledError:
        exitcode = 1
    except (DependencyError, AccurateripError, SubprocessError,
            NotFromCDError, NetworkError) as e:
        print(e, file=sys.stderr)
        sys.stderr.write('%s\n' % e)
        exitcode = 2
    finally:
        for t in tempfiles:
            try: os.unlink(t)
            except OSError: pass
        for d in tempdirs:
            try: rmtree(d)
            except OSError: pass
        for p in processes:
            try: p.kill()
            except OSError: pass
    sys.exit(exitcode)
