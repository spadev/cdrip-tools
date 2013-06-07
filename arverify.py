#!/usr/bin/python
from __future__ import print_function
import os, re, sys, struct

from os.path import basename, dirname, join
from subprocess import Popen, PIPE
from argparse import ArgumentParser
from io import BytesIO
from tempfile import TemporaryFile

import utils
from utils import SubprocessError, NotFromCDError,\
    AccurateripError, NetworkError

try:
    from urllib import urlopen
except ImportError:
    from urllib.request import urlopen

BIN = {'metaflac': None,
       'ffprobe' : 'avprobe',
       'sox'     : None,
       'ckcdda'  : None,
       }

PROGNAME = 'arverify'
VERSION = '0.2'
REQUIRED = ['ffprope', 'sox', 'ckcdda']
PROCS = []

MIN_OFFSET = -2939

class AccurateripEntry(object):
    """Represents one entry in Accuraterip database. One track
    may have several entries in the database

    TODO: See if there's a way to determine if crc is v1 or v2 beforehand
    """
    def __init__(self, crc, crc450, confidence):
        self.crc = crc
        self.crc450 = crc450
        self.confidence = confidence

class Track(object):
    """One track and its associated metadata/information"""
    def __init__(self, path):
        self.path = path
        self.num_samples = utils.get_num_samples(BIN, path)
        self.num_sectors = int(self.num_samples/588)
        if self.num_samples % 588 != 0:
            msg = "%s not from CD (%i samples)\n" % \
                (path, self.num_samples)
            raise NotFromCDError(msg)
        self.ar_entries = []

        # key is offset, value is list of confidence levels
        self.exact_matches = {}
        self.possible_matches = {}

    @property
    def num_submissions(self):
        return sum([e.confidence for e in self.ar_entries])

def process_arguments():
    parser = \
        ArgumentParser(description='Verify lossless files with accuraterip.',
                       prog=PROGNAME)
    parser.add_argument('paths', metavar='file', nargs='+',
                        type=utils.isfile,
                        help='lossless audio file')
    parser.add_argument("-a", "--additional-sectors",
                        dest="additional_sectors", type=int,
                        help="additional pregap sectors beyond standard 150",
                        default=0,
                        )
    parser.add_argument("-d", "--data-track-length", dest="data_track_len",
                        help="length of data track in sectors or mm:ss.ff",
                        default=0,
                        )
    utils.add_common_arguments(parser, VERSION)

    return parser.parse_args()

def scan_files(tracks):
    sox_args = ['sox']+[t.path for t in tracks]+['-t', 'raw', '-']
    entries_per_track = max([len(t.ar_entries) for t in tracks])
    ckcdda_args = [BIN['ckcdda'], entries_per_track]

    for track in tracks:
        ckcdda_args.append(str(track.num_sectors))
        crcs = [e.crc for e in track.ar_entries]
        crc450s = [e.crc450 for e in track.ar_entries]
        crcs += [0]*(entries_per_track-len(crcs))
        crc450s += [0]*(entries_per_track-len(crc450s))
        ckcdda_args += crcs
        ckcdda_args += crc450s

    ckcdda_args = map(str, ckcdda_args)

    tmp = TemporaryFile()
    PROCS.append(Popen(sox_args, stdout=PIPE))
    PROCS.append(Popen(ckcdda_args, stdin=PROCS[-1].stdout, stdout=tmp))

    p = PROCS[-1]
    while p.poll() is None:
        utils.show_status('Calculating checksums for %i files', len(tracks))
    utils.finish_status()

    out, err = p.communicate()
    tmp.seek(0)
    out = tmp.read().decode()
    for pr in PROCS:
        if pr.returncode:
            raise SubprocessError('sox had an error (returned %i)' %
                                  pr.returncode)

    lines = out.split('\n')
    num_lines = len(lines)

    results1 = []
    results2 = []
    results450 = []
    for i, line in enumerate(lines):
        if not re.match('^\d', line):
            continue

        index, data = line.split(': ')
        track_index, offset = [int(x) for x in index.split(',')]
        hashes = [int(x, 16) for x in data.split()]

        crc1, crc450 = hashes[:2]
        if len(hashes) > 2:
            crc2 = hashes[2]
        else:
            crc2 = None

        track = tracks[track_index]

        if offset == 0:
            track.crc1 = crc1
            track.crc2 = crc2
            track.crc450 = crc450

        for entry in track.ar_entries:
            if entry.crc in (crc1, crc2):
                if offset not in track.exact_matches:
                    track.exact_matches[offset] = []
                track.exact_matches[offset].append(entry.confidence)
            elif entry.crc450 == crc450 and offset != 0:
                if offset not in track.possible_matches:
                    track.possible_matches[offset] = []
                track.possible_matches[offset].append(entry.confidence)

def get_disc_ids(tracks, additional_sectors=0, data_track_len=0,
                 verbose=False):
    # first get track offsets
    try:
        data_track_len = int(data_track_len)
    except ValueError:
        dt = re.split('[:.]', data_track_len)
        data_track_len = int(dt.pop())
        num_seconds = 0
        multiplier = 1
        while dt:
            num_seconds += multiplier*int(dt.pop())
            multiplier *= 60
        data_track_len += (num_seconds*44100)/588

    if verbose:
        if additional_sectors:
            print('Additional pregap sectors: %i' % additional_sectors)
        if data_track_len:
            print('Data track length: %i' % data_track_len)

    track_offsets = [additional_sectors]
    cur_sectors = additional_sectors
    for track in tracks:
        cur_sectors += track.num_sectors
        track_offsets.append(cur_sectors)

    # now get disc ids
    id1, id2, cddb = (0, 0, 0)
    for tracknumber, offset in enumerate(track_offsets, start=1):
        id1 += offset
        id2 += tracknumber * (offset if offset else 1)
    if data_track_len:
        id1 += data_track_len + 11400
        id2 += (data_track_len + 11400)*len(track_offsets)
        track_offsets[-1] += 11400
        track_offsets.append(data_track_len + track_offsets[-1])

    cddb = sum([sum(map(int, str(int(o/75) + 2))) for o in track_offsets[:-1]])
    cddb = ((cddb % 255) << 24) + \
        (int(track_offsets[-1]/75) - int(track_offsets[0]/75) << 8) + \
        len(track_offsets) - 1

    id1 &= 0xFFFFFFFF;
    id2 &= 0xFFFFFFFF;
    cddb &= 0xFFFFFFFF;

    return (cddb, id1, id2)

def get_ar_entries(cddb, id1, id2, tracks, verbose=False):
    url = ("http://www.accuraterip.com/accuraterip/"+
           "%.1x/%.1x/%.1x/dBAR-%.3d-%.8x-%.8x-%.8x.bin")
    url = url % (id1 & 0xF, id1>>4 & 0xF, id1>>8 & 0xF,
                 len(tracks), id1, id2, cddb)
    if verbose:
        print(url)

    try:
        data = urlopen(url).read()
    except IOError:
        raise NetworkError("Could not connect to accuraterip database")
    if b'html' in data and b'404' in data:
        data = b''

    return process_binary_ar_entries(BytesIO(bytes(data)), cddb, id1, id2, tracks)

def process_binary_ar_entries(fdata, cddb, id1, id2, tracks):
    if not fdata:
        return

    trackcount = len(tracks)

    while True:
        chunk_trackcount = fdata.read(1)
        chunk_id1 = fdata.read(4)
        chunk_id2 = fdata.read(4)
        chunk_cddb = fdata.read(4)
        if len(chunk_trackcount) + len(chunk_id1) + len(chunk_id2) + \
                len(chunk_cddb) != 13:
            break
        # unpack as unsigned char
        ar_trackcount = int(struct.unpack('B', chunk_trackcount)[0])

        # unpack as unsigned integers
        ar_id1 = int(struct.unpack('I', chunk_id1)[0])
        ar_id2 = int(struct.unpack('I', chunk_id2)[0])
        ar_cddb = int(struct.unpack('I', chunk_cddb)[0])
        if ar_trackcount != trackcount or \
                ar_id1 != id1 or ar_id2 != id2 or ar_cddb != cddb:
            raise AccurateripError("Track count or Disc IDs don't match")
        for track in tracks:
            chunk_confidence = fdata.read(1)
            chunk_crc = fdata.read(4)
            chunk_crc450 = fdata.read(4) # skip 4 bytes
            if len(chunk_crc) + len(chunk_confidence) + len(chunk_crc450) != 9:
                break
            confidence = int(struct.unpack('B', chunk_confidence)[0])
            crc = int(struct.unpack('I', chunk_crc)[0])
            crc450 = int(struct.unpack('I', chunk_crc450)[0])
            track.ar_entries.append(AccurateripEntry(crc, crc450, confidence))

def print_summary(tracks, verbose=False):
    summary = []

    good = {}      # Matching main CRC (with or without offset)
    maybe = {}     # main CRC mismatch and CRC450 match
    bad = []       # main CRC mismatch and no CRC450 match
    np = []        # No accuraterip data at all

    goodmsg      = 'Accurately ripped'
    npmsg        = 'Not present in database'
    badmsg       = '***Definitely not accurately ripped***'
    maybemsg     = 'Possibly accurately ripped'
    calc1msg     = 'Calculated CRCv1'
    calc2msg     = 'Calculated CRCv2'
    calc450msg   = 'Calculated CRC450'
    badfmt       = '***Definitely not accurately ripped (%s)***'
    wofmt        = ' with offset %i'
    fmt          = '%-20s: %08X'
    dbentryfmt   = '%-20s: CRC: %08X, Confidence: %3i, CRC450: %08X'
    totalfmt     = 'total %i submission%s'

    def generate_messages(track, matches, msg, l):
        msgs = []
        for offset, confidence in iter(matches.items()):
            ns = track.num_submissions
            m = '%s%s (confidence %s%s)' % \
                (msg, wofmt % offset if offset else '',
                 '+'.join(str(x) for x in confidence),
                 '/%i' % ns if ns != confidence else '')
            msgs.append(m)
            if offset not in l:
                l[offset] = []
            l[offset].append( (confidence, ns) )

        return msgs

    for track in tracks:
        lines = []
        lines.append(track.path)
        lines.append(fmt % (calc1msg, track.crc1))
        lines.append(fmt % (calc2msg, track.crc2))

        if verbose:
            lines.append(fmt % (calc450msg, track.crc450))
            for entry in track.ar_entries:
                lines.append(dbentryfmt % ('Database entry', entry.crc,
                                           entry.confidence, entry.crc450))

        lines.append('-'*len(lines[-1]))

        g = generate_messages(track, track.exact_matches, goodmsg, good)
        p = generate_messages(track, track.possible_matches, maybemsg, maybe)
        lines += g
        lines += p

        ns = track.num_submissions
        if ns == 0:
            lines.append(npmsg)
            np.append(0)
        elif not g and not p:
            nsmsg = totalfmt % (ns, 's' if ns != 1 else '')
            lines.append(badfmt % nsmsg)
            bad.append(ns)

        summary.append('\n    '.join(lines))

    print('\n\n'.join(summary))
    print('\n'+'='*80)

    total = len(tracks)
    mfmt = '%i/%i' if total < 10 else '%2i/%2i'
    for offset in sorted(good.keys(), key=abs):
        entry = good[offset]
        n = len(entry)
        c, ns = max(entry, key=lambda x: sum(x[0]))
        m = (mfmt+' %s%s (confidence %i)') % \
            (n, total, goodmsg, wofmt % offset if offset else '', sum(c))
        print(m)
    for offset in sorted(maybe.keys()):
        entry = maybe[offset]
        n = len(entry)
        c, ns = max(entry, key=lambda x: sum(x[0]))
        m = (mfmt+' %s%s (confidence %i)') % \
            (n, total, maybemsg, wofmt % offset if offset else '', sum(c))
        print(m)
    if bad:
        print((mfmt+' %s') % (len(bad), total, badmsg))
    if np:
        print((mfmt+' %s') % (len(np), total, npmsg))

    return len(bad)

def main():
    utils.check_dependencies(BIN, REQUIRED)
    ns = process_arguments()
    tracks = [Track(path) for path in ns.paths]

    cddb, id1, id2 = get_disc_ids(tracks, ns.additional_sectors,
                                  ns.data_track_len, ns.verbose)
    print('Disc ID: %08x-%08x-%08x' % (id1, id2, cddb))
    get_ar_entries(cddb, id1, id2, tracks, ns.verbose)
    scan_files(tracks)
    return print_summary(tracks, ns.verbose)

if __name__ == '__main__':
    utils.execute(main, PROCS)
