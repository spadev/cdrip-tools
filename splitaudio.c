/* splitaudio.c */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sndfile.h>
#include <unistd.h>

#define MIN(a,b) ((a) < (b) ? a : b)
#define BUFSIZE 16*1024

int
main(int argc, char *argv[])
{
    int track_count = argc-2;
    if (track_count == 0)
        return EXIT_SUCCESS;

    SF_INFO in_info = {0};
    in_info.samplerate = 44100;
    in_info.channels = 2;
    in_info.format = SF_FORMAT_RAW | SF_FORMAT_PCM_16;
    SNDFILE *infile = sf_open_fd(STDIN_FILENO, SFM_READ, &in_info, 1);
    if (infile == NULL) {
        fprintf(stderr, "Error opening soundfile on stdin: %s\n",
                sf_strerror(infile));
        return EXIT_FAILURE;
    }

    short buf[BUFSIZE*2];
    int format = atoi(argv[1]);

    for (int i = 0; i < track_count; i++) {
        int track_length = atoi(argv[i+2]);

        SF_INFO out_info = {0};
        out_info.channels = 2;
        out_info.frames = track_length;
        out_info.samplerate = 44100;

        char filename[14];
        if (format == 1) {
            sprintf(filename, "fixed%03u.flac", i);
            out_info.format = SF_FORMAT_FLAC | SF_FORMAT_PCM_16;
        } else {
            sprintf(filename, "fixed%03u.wav", i);
            out_info.format = SF_FORMAT_WAV | SF_FORMAT_PCM_16;
        }

        SNDFILE *outfile = sf_open(filename, SFM_WRITE, &out_info);
        if (outfile == NULL) {
            sf_close(infile);
            fprintf(stderr, "Error opening soundfile %s: %s\n", filename,
                    sf_strerror(outfile));
            return EXIT_FAILURE;
        }
        int r;
        for (int j = 0; j < track_length; j+=BUFSIZE) {
            int num_to_read = MIN(BUFSIZE, track_length-j);
            r = sf_readf_short(infile, buf, num_to_read);
            if (r != num_to_read) {
                fprintf(stderr, "Unexpected EOF\n");
                return EXIT_FAILURE;
            }
            sf_writef_short(outfile, buf, r);
        }
        sf_close(outfile);
    }

    sf_close(infile);
    return EXIT_SUCCESS;
}
