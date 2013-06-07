#!/usr/bin/python
from __future__ import print_function

import os
import re
import sys
import struct
from argparse import ArgumentParser
from io import BytesIO
from tempfile import TemporaryFile
from os.path import basename, dirname, join
from subprocess import Popen, PIPE
try:
    from urllib import urlopen
except ImportError:
    from urllib.request import urlopen

import utils
from utils import SubprocessError, NotFromCDError,\
    AccurateripError, NetworkError

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
    _fmt = '%-20s: CRC: %08X, Confidence: %3i, CRC450: %08X'

    def __init__(self, crc, crc450, confidence):
        self.crc = crc
        self.crc450 = crc450
        self.confidence = confidence

    def __str__(self):
        return self._fmt % ('Database entry', self.crc, self.confidence,
                            self.crc450)

class Track(object):
    """One track and its associated metadata/information"""
    exact_match_msg = 'Accurately ripped'
    possible_match_msg = 'Possibly accurately ripped'
    not_present_msg = 'Not present in database'

    not_accurate_fmt = '***Definitely not accurately ripped (%s)***'
    with_offset_fmt = ' with offset %i'
    _fmt = '%-20s: %08X'
    total_fmt = 'total %i submissions'

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

    def __matches_summary(self, matches, msg, album_matches):
        summary = []
        for offset, confidence in iter(matches.items()):
            ns = self.num_submissions
            m = '%s%s (confidence %s%s)' % \
                (msg, self.with_offset_fmt % offset if offset else '',
                 '+'.join(str(x) for x in confidence),
                 '/%i' % ns if ns != confidence else '')
            summary.append(m)
            if offset not in album_matches:
                album_matches[offset] = []
            album_matches[offset].append( (confidence, ns) )

        return summary

    def calcsummary(self, verbose):
        pairs = [('Calculated CRCv1', self.crc1),
                 ('Calculated CRCv2', self.crc2),
                 ('Calculated CRC450', self.crc450)]
        if not verbose:
            pairs = pairs[:-1]
            for entry in track.ar_entries:
                lines.append(str(entry))
        return [self._fmt % (x, y) for x, y in pairs]

    def dbsummary(self):
        return [str(e) for e in self.ar_entries]

    def ripsummary(self, album_exact_matches, album_possible_matches,
                   album_not_present, album_not_accurate):
        good = self.__matches_summary(self.exact_matches,
                                      self.exact_match_msg,
                                      album_exact_matches)
        possible = self.__matches_summary(self.possible_matches,
                                          self.possible_match_msg,
                                          album_possible_matches)
        summary = good + possible

        ns = self.num_submissions
        if ns == 0:
            summary.append(self.not_present_msg)
            album_not_present.append(0)
        elif not good and not possible:
            msg = self.total_fmt % (ns, 's' if ns != 1 else '')
            summary.append(self.not_accurate_fmt % msg)
            album_not_accurate.append(ns)

        return summary

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

    for track in tracks:
        lines = [track.path]
        lines += track.calcsummary(verbose)
        if verbose:
            lines += track.dbsummary()
        lines.append('-'*len(lines[-1]))
        lines += track.ripsummary(good, maybe, np, bad)
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
            (n, total, Track.exact_match_msg, Track.with_offset_fmt % offset
             if offset else '', sum(c))
        print(m)
    for offset in sorted(maybe.keys()):
        entry = maybe[offset]
        n = len(entry)
        c, ns = max(entry, key=lambda x: sum(x[0]))
        m = (mfmt+' %s%s (confidence %i)') % \
            (n, total, Track.possible_match_msg, Track.with_offset_fmt % offset
             if offset else '', sum(c))
        print(m)
    if bad:
        print((mfmt+' %s') % (len(bad), total, Track.not_accurate_msg))
    if np:
        print((mfmt+' %s') % (len(np), total, Track.not_present_msg))

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
