#!/usr/bin/python
from __future__ import print_function
import os, re, sys, struct

import utils

from os.path import isfile, basename, dirname, join
from subprocess import Popen, PIPE
from argparse import ArgumentParser
from io import BytesIO
from tempfile import TemporaryFile

try:
    from urllib import urlopen
except ImportError:
    from urllib.request import urlopen

BIN = {'metaflac': None,
       'ckcdda': None,
       'ffprobe': 'avprobe',
       'sox': None,
       }

PROGNAME = 'arverify'
VERSION = '0.1'
REQUIRED = ['ffprope', 'sox', 'ckcdda']
PROCS = []

MIN_OFFSET = -2939

def process_arguments():
    parser = \
        ArgumentParser(description='Verify lossless files with accuraterip.',
                       prog=PROGNAME)
    parser.add_argument('paths', metavar='file', nargs='+',
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

def scan_files(sources):
    total = len(sources)
    sox_args = ['sox']+[s['path'] for s in sources]+['-t', 'raw', '-']
    ckcdda_args = [BIN['ckcdda']] + [str(s['num_sectors']) for s in sources]

    tmp = TemporaryFile()
    PROCS.append(Popen(sox_args, stdout=PIPE))
    PROCS.append(Popen(ckcdda_args, stdin=PROCS[-1].stdout, stdout=tmp))

    p = PROCS[-1]
    while p.poll() is None:
        utils.show_status('Calculating checksums for %i files', total)

    out, err = p.communicate()
    tmp.seek(0)
    out = tmp.read().decode()
    if PROCS[-1].returncode != 0:
        raise utils.SoxError('sox had an error (returned %i)' %
                             PROCS[-1].returncode)

    for s in sources:
        s['results'] = {}
        s['cresults'] = {}

    lines = out.split('\n')
    num_lines = len(lines)
    print('\r'+' '*79, file=sys.stderr, end='')
    msg = '\rAnalyzing results [%3i%%]'
    last_percentage = 0

    results1 = []
    results2 = []
    results450 = []
    for i, line in enumerate(lines):
        if not re.match('^\d', line):
            continue
        percentage = (float(i)/num_lines)*100
        if percentage != last_percentage:
            print((msg % percentage), file=sys.stderr, end='')
            last_percentage = percentage

        index, data = line.split(': ')
        track_index, offset = [int(x) for x in index.split(',')]
        hashes = [int(x, 16) for x in data.split()]

        crc1, crc450 = hashes[:2]
        if len(hashes) > 2:
            crc2 = hashes[2]
        else:
            crc2 = None

        s = sources[track_index]

        if offset == 0:
            s['crc1'] = crc1
            s['crc2'] = crc2
            s['crc450'] = crc450

        for ar in s['ar']:
            if ar['crc'] in (crc1, crc2):
                if offset not in s['results']:
                    s['results'][offset] = []
                s['results'][offset].append(ar['confidence'])
            elif ar['crc450'] == crc450 and offset != 0:
                if offset not in s['cresults']:
                    s['cresults'][offset] = []
                s['cresults'][offset].append(ar['confidence'])
    print(msg % 100, file=sys.stderr, end='\n')

def get_disc_ids(sources, additional_sectors=0, data_track_len=0,
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
    for s in sources:
        s['num_samples'] = utils.get_num_samples(BIN, s['path'])
        s['num_sectors'] = int(s['num_samples'] / 588)
        if s['num_samples'] % 588 != 0:
            msg = "%s not from CD (%i samples)\n" % (s['path'],
                                                     s['num_samples'])
            raise utils.NotFromCDError(msg)
        cur_sectors += s['num_sectors']
        track_offsets.append(cur_sectors)

    # now get disc ids
    id1, id2, cddb = (0, 0, 0)
    for tracknumber, offset in enumerate(track_offsets, start=1):
        id1 += offset
        id2 += tracknumber * (offset if offset else 1)
    if data_track_len:
        id1 += data_track_len + 11400
        id2 += (data_track_len + 11400)*len(track_offsets)

    if data_track_len:
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

def get_ardata(cddb, id1, id2, sources, verbose=False):
    url = ("http://www.accuraterip.com/accuraterip/"+
           "%.1x/%.1x/%.1x/dBAR-%.3d-%.8x-%.8x-%.8x.bin")
    url = url % (id1 & 0xF, id1>>4 & 0xF, id1>>8 & 0xF,
                 len(sources), id1, id2, cddb)
    if verbose:
        print(url)

    data = urlopen(url).read()
    if b'html' in data and b'404' in data:
        data = b''

    return process_binary_ardata(BytesIO(bytes(data)), cddb, id1, id2, sources)

def process_binary_ardata(fdata, cddb, id1, id2, sources):
    trackcount = len(sources)

    for s in sources:
        s['ar'] = []
    if not fdata:
        return

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
            raise utils.AccurateRipError("Track count or Disc IDs don't match")
        for s in sources:
            chunk_confidence = fdata.read(1)
            chunk_crc = fdata.read(4)
            chunk_crc450 = fdata.read(4) # skip 4 bytes
            if len(chunk_crc) + len(chunk_confidence) + len(chunk_crc450) != 9:
                break
            confidence = int(struct.unpack('B', chunk_confidence)[0])
            crc = int(struct.unpack('I', chunk_crc)[0])
            crc450 = int(struct.unpack('I', chunk_crc450)[0])
            s['ar'].append(dict(crc=crc, confidence=confidence, crc450=crc450))

def print_summary(sources, verbose=False):
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

    def generate_messages(s, results, msg, l):
        msgs = []
        for offset, confidence in iter(results.items()):
            ns = s['num_submissions']
            m = '%s%s (confidence %s%s)' % \
                (msg, wofmt % offset if offset else '',
                 '+'.join(str(x) for x in confidence),
                 '/%i' % ns if ns != confidence else '')
            msgs.append(m)
            if offset not in l:
                l[offset] = []
            l[offset].append( (confidence, ns) )

        return msgs

    for s in sources:
        lines = []
        lines.append(s['path'])
        lines.append(fmt % (calc1msg, s['crc1']))
        lines.append(fmt % (calc2msg, s['crc2']))
        s['num_submissions'] = sum([e['confidence'] for e in s['ar']])

        if verbose:
            lines.append(fmt % (calc450msg, s['crc450']))
            for e in s['ar']:
                lines.append(dbentryfmt % ('Database entry', e['crc'],
                                           e['confidence'], e['crc450']))

        lines.append('-'*len(lines[-1]))

        g = generate_messages(s, s['results'], goodmsg, good)
        p = generate_messages(s, s['cresults'], maybemsg, maybe)
        lines += g
        lines += p

        if s['num_submissions'] == 0:
            lines.append(npmsg)
            np.append(0)
        elif not g and not p:
            ns = s['num_submissions']
            nsmsg = totalfmt % (ns, 's' if ns != 1 else '')
            lines.append(badfmt % nsmsg)
            bad.append(num_submissions)

        summary.append('  \n'.join(lines))

    print('\n\n'.join(summary))
    print('\n'+'='*80)

    total = len(sources)
    for offset in sorted(good.keys(), key=abs):
        entry = good[offset]
        n = len(entry)
        c, ns = max(entry, key=lambda x: sum(x[0]))
        m = '%2i/%2i %s%s (confidence %i)' % \
            (n, total, goodmsg, wofmt % offset if offset else '', sum(c))
        print(m)
    for offset in sorted(maybe.keys()):
        entry = maybe[offset]
        n = len(entry)
        c, ns = max(entry, key=lambda x: sum(x[0]))
        m = '%2i/%2i %s%s (confidence %i)' % \
            (n, total, maybemsg, wofmt % offset if offset else '', sum(c))
        print(m)
    if bad:
        print('%2i/%2i %s' % (len(bad), total, badmsg))
    if np:
        print('%2i/%2i %s' % (len(np), total, npmsg))

    return len(bad)

def main():
    utils.check_dependencies(BIN, REQUIRED)
    ns = process_arguments()
    sources = [dict(path=p) for p in ns.paths if isfile(p)]
    total = len(sources)
    if total == 0:
        raise utils.InvalidFilesError('Please provide valid input files')

    cddb, id1, id2 = get_disc_ids(sources, ns.additional_sectors,
                                  ns.data_track_len, ns.verbose)
    print('Disc ID: %08x-%08x-%08x' % (id1, id2, cddb))
    get_ardata(cddb, id1, id2, sources, ns.verbose)
    scan_files(sources)
    return print_summary(sources, ns.verbose)

if __name__ == '__main__':
    utils.execute(main, PROCS)
