/* ckcdda.c */

/* ARCF: AccurateRip Checksum (Flawed) */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>

#define SAMPLES_PER_FRAME  588 /* = 44100 / 75 */
#define CHECK_RADIUS  (5*SAMPLES_PER_FRAME-1)
#define ARCFS_PER_TRACK  (2*CHECK_RADIUS+1)

#define ARCF_IDX(track,offset)  ((offset) + (track)*ARCFS_PER_TRACK)


static int
read_value(FILE *f, uint32_t *value)
{
    uint16_t sample[2];
    size_t rd = fread(sample, sizeof(uint16_t), 2, f);
    if (rd < 2) {
        if (feof(f)) return 0;
        return -1;
    }
    *value = (sample[1] << 16) | sample[0];
    return rd;
}

static void
update_arcf(uint32_t *restrict arcf, uint32_t *restrict sum,
            int track, int track_count,
            const int *restrict length,
            int ti, int tr, int last_tr,
            uint32_t value)
{
    /* Update base ARCF if we're not
       in the zone after the last track. */
    if (track < track_count) {
        /* Save first values of track in ARCF block.
           This is the value we'll need later when
           calculating the derived ARCFs. */
        if (tr < ARCFS_PER_TRACK-1) {
            arcf[ARCF_IDX(track, tr+1)] = value;
        }

        /* Update sum and base ARCF */
        sum[track] += value;
        arcf[ARCF_IDX(track, 0)] += value*(ti+1);
    }

    /* Calculate derived ARCFs for previous track
       (so skip if this is the first track). */
    if (track > 0 && tr < ARCFS_PER_TRACK-1) {
        /* Fetch saved value */
        uint32_t first = arcf[ARCF_IDX(track-1, tr+1)];

        /* Calculate ARCF for moved window */
        arcf[ARCF_IDX(track-1, tr+1)] =
            arcf[ARCF_IDX(track-1, tr)] - (length[track-1]-last_tr)*first -
            sum[track-1] + length[track-1]*value;

        /* Adjust sum to be sum for new window */
        sum[track-1] += value - first;
    }
}

static void
update_framecrc(uint32_t *restrict frame,
                uint32_t *restrict framesum,
                uint32_t *restrict framecrc,
                int ti, uint32_t value)
{
    /* (1) Calculate subtr */
    /* (2) Write value to frame */
    /* (3) Update frame CRC */
    /* (4) Update framesum */
    uint32_t subtr;
    if (ti < SAMPLES_PER_FRAME) {
        subtr = 0; // (1)
        frame[ti % SAMPLES_PER_FRAME] = value; // (2)
        *framecrc += value*(ti+1); // (3)
    } else {
        subtr = frame[ti % SAMPLES_PER_FRAME]; // (1)
        frame[ti % SAMPLES_PER_FRAME] = value; // (2)
        *framecrc += value * SAMPLES_PER_FRAME - *framesum; // (3)
    }
    *framesum += value - subtr; // (4)
}

static void *
alloc_memory(size_t nmemb, size_t size, void *to_free[], int n)
{
    void *ptr = calloc(nmemb, size);
    if (ptr == NULL) {
        fprintf(stderr, "Unable to allocate memory.\n");
        for (int i = 0; i < n; i++)
            free(to_free[i]);
        exit(EXIT_FAILURE);
    }
    return ptr;
}

static uint32_t *
alloc_uint32(size_t nmemb, void *to_free, int n)
{
    return (uint32_t *) alloc_memory(nmemb, sizeof(uint32_t), to_free, n);
}

static int *
alloc_int(size_t nmemb, void *to_free, int n)
{
    return (int *) alloc_memory(nmemb, sizeof(int), to_free, n);
}

int
main(int argc, char *argv[])
{
    /* Reopen stdin as binary */
    if (freopen(NULL, "rb", stdin) == NULL) {
        perror("freopen");
        exit(EXIT_FAILURE);
    }

    if (argc < 2) {
        fprintf(stderr, "Need at least two arguments\n");
        exit(EXIT_FAILURE);
    }

    int num_pairs_per_track = atoi(argv[1]); /* number of (crc, crc450) pairs per
                                                track */
    int track_count = (argc-2) / (num_pairs_per_track*2 + 1);
    if ( (argc-2) % (num_pairs_per_track*2 + 1) ) {
        fprintf(stderr, "Invalid number of arguments\n");
        exit(EXIT_FAILURE);
    }

    printf("track count: %i\n", track_count);
    printf("entries per track: %i\n", num_pairs_per_track);

    void     *to_alloc[8] = {NULL};
    int      *length = alloc_int(track_count+1, to_alloc, 0);
    uint32_t *sum = alloc_uint32(track_count, to_alloc, 1);
    uint32_t *crc2 = alloc_uint32(track_count, to_alloc, 2);
    uint32_t *arcf = alloc_uint32(track_count*ARCFS_PER_TRACK, to_alloc, 3);
    uint32_t *arcf450 = alloc_uint32(track_count*ARCFS_PER_TRACK, to_alloc, 4);
    uint32_t *frame = alloc_uint32(SAMPLES_PER_FRAME, to_alloc, 5);
    uint32_t *dbcrc = alloc_uint32(track_count*num_pairs_per_track,
                                   to_alloc, 6);
    uint32_t *dbcrc450 = alloc_uint32(track_count*num_pairs_per_track,
                                      to_alloc, 7);

    /* args layout:
       ./ckcdda num_pairs_per_track length(0) crc(0,0) crc(0,1)...
       crc450(0,0) crc450(0,1)... length(1) crc(1,0) crc(1,1)...
       crc450(1,0) crc450(1,1)... length(2)... */
    int total_length = 0;
    for (int trackno = 0; trackno < track_count; trackno++) {
        int p = 2+trackno*(2*num_pairs_per_track+1);
        length[trackno] = atoi(argv[p])*SAMPLES_PER_FRAME;

        /* Read in dbcrc and dbcrc450 */
        for (int j = 0, k = num_pairs_per_track; j < num_pairs_per_track;
             j++, k++) {
            dbcrc[trackno*num_pairs_per_track + j] = atoi(argv[p+j+1]);
            dbcrc450[trackno*num_pairs_per_track + j] = atoi(argv[p+k+1]);
        }

        total_length += length[trackno];
    }
    printf("total_length: %i\n", total_length);

    length[track_count-1] -= CHECK_RADIUS+1;
    length[track_count] = 2*CHECK_RADIUS+1;

    for (int i = 0; i < track_count+1; i++)
        printf("len(%i): %i\n", i, length[i]);

    int track = 0;
    printf("At track %u (%u, %u)\n", track, track < track_count, track > 0);

    int ti = CHECK_RADIUS;
    int tr = 0;
    int di = 0;

    int ti2 = 0;
    int track2 = 0;
    uint32_t framesum = 0; /* sum of all audio vales in current frame */
    uint32_t framecrc = 0; /* v1 CRC of current frame */

    int last_tr = 0;
    while (di < total_length) {
        /* Read one stereo sample */
        uint32_t value;
        int r = read_value(stdin, &value);
        if (r == 0) {
            fprintf(stderr, "Unexpected EOF.\n");
            exit(EXIT_FAILURE);
        } else if (r < 0) {
            perror("read_value");
            exit(EXIT_FAILURE);
        }

        /* Update ARCF values */
        update_arcf(arcf, sum, track, track_count, length, ti, tr, last_tr,
                    value);

        if (di >= CHECK_RADIUS) {
            uint64_t calcvalue = (uint64_t) value * ((uint64_t) ti2+1);
            /* Update ARv2 CRC */
            crc2[track2] += (calcvalue & 0xFFFFFFFF);
            crc2[track2] += (calcvalue / 0x100000000);

            /* Update frame CRC */
            int offset = ti2 - (451*SAMPLES_PER_FRAME-1-CHECK_RADIUS);
            if (offset < ARCFS_PER_TRACK) {
                update_framecrc(frame, &framesum, &framecrc, ti2, value);
                if (offset >= 0)
                    arcf450[ARCF_IDX(track2, offset)] = framecrc;
            }
        }

        /* Increment counters */
        di += 1;
        ti += 1;
        tr += 1;
        ti2 += 1;

        /* Check whether end of current track has been reached. */
        if (ti == length[track]) {
            last_tr = tr;
            ti = 0;
            tr = 0;
            track += 1;
            printf("At %i track %i (%u, %u)\n", di, track, track < track_count,
                   track > 0);
        }
        if (ti2 == length[track2]) {
            ti2 = 0;
            framesum = 0;
            framecrc = 0;
            memset(frame, 0, SAMPLES_PER_FRAME * sizeof(uint32_t));
            track2 += 1;
        }
    }

    /* Print ARCFs for offset 0 and matching offsets */
    for (int trackno = 0; trackno < track_count; trackno++) {
        for (int o = 0; o < ARCFS_PER_TRACK; o++) {
            int offset = o-CHECK_RADIUS;
            uint32_t crc = arcf[ARCF_IDX(trackno, o)];
            uint32_t crc450 = arcf450[ARCF_IDX(trackno, o)];
            if (offset == 0) {
                printf("%03u,%i: %08X %08X %08X\n", trackno,
                       o-CHECK_RADIUS, crc, crc450, crc2[trackno]);
            } else {
                for (int j = 0; j < num_pairs_per_track; j++) {
                    if (crc == dbcrc[trackno*num_pairs_per_track+j] ||
                        crc450 == dbcrc450[trackno*num_pairs_per_track+j]) {
                        printf("%03u,%i: %08X %08X\n", trackno, o-CHECK_RADIUS,
                               crc, crc450);
                    }
                }
            }
        }
    }
    return EXIT_SUCCESS;
}
