/*
 * pi_fm_rds_sim.c — test double for pi_fm_rds (PiRadio test suite only).
 *
 * Accepts the same command line as pi_fm_rds and runs the *real* PiFmRds
 * baseband code (fm_mpx.c: audio input via libsndfile + FIR + stereo MPX,
 * rds.c: RDS encoder, control_pipe.c: PS/RT commands) at real-time speed,
 * but instead of driving the Raspberry Pi clock/DMA hardware it prints a
 * status line every second:
 *
 *   SIM t=<sec> peak=<mpx peak> audio=<0|1> ps="..." rt="..."
 *
 * Built and run on a normal computer, never installed on the Pi.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <time.h>
#include <math.h>
#include <locale.h>
#include <unistd.h>

#include "rds.h"
#include "fm_mpx.h"
#include "control_pipe.h"

#define DATA_SIZE 5000            /* same as pi_fm_rds.c */
#define MPX_RATE 228000.0

static volatile sig_atomic_t stop_flag = 0;
static void on_signal(int s) { (void)s; stop_flag = 1; }

static char cur_ps[16] = "";
static char cur_rt[80] = "";

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    char *audio_file = NULL, *control_pipe = NULL, *ps = NULL;
    char *rt = "PiFmRds: live FM-RDS transmission from the RaspberryPi";
    double freq = 107.9;
    unsigned pi = 0x1234;

    for (int i = 1; i < argc; i++) {
        char *a = argv[i], *p = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!p) { fprintf(stderr, "Missing value for %s\n", a); return 1; }
        if (!strcmp(a, "-audio") || !strcmp(a, "-wav")) audio_file = p;
        else if (!strcmp(a, "-freq")) {
            freq = atof(p);
            if (freq < 76 || freq > 108) {
                fprintf(stderr, "Incorrect frequency specification. Must be in megahertz, of the form 107.9, between 76 and 108.\n");
                return 1;
            }
        }
        else if (!strcmp(a, "-ps")) ps = p;
        else if (!strcmp(a, "-rt")) rt = p;
        else if (!strcmp(a, "-pi")) pi = (unsigned)strtol(p, NULL, 16);
        else if (!strcmp(a, "-ctl")) control_pipe = p;
        else if (!strcmp(a, "-ppm")) { /* accepted, unused */ }
        else { fprintf(stderr, "Unrecognised argument: %s.\n", a); return 1; }
        i++;
    }

    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    signal(SIGPIPE, SIG_IGN);
    setlocale(LC_ALL, "");

    static float data[DATA_SIZE];
    if (fm_mpx_open(audio_file, DATA_SIZE) < 0) return 1;

    set_rds_pi(pi);
    set_rds_rt(rt);
    snprintf(cur_rt, sizeof cur_rt, "%s", rt);
    if (ps) { set_rds_ps(ps); snprintf(cur_ps, sizeof cur_ps, "%.8s", ps); }
    printf("PI: %04X, PS: \"%s\".\n", pi, ps ? ps : "<Varying>");

    if (control_pipe) {
        if (open_control_pipe(control_pipe) == 0) printf("Reading control commands on %s.\n", control_pipe);
        else { printf("Failed to open control pipe: %s.\n", control_pipe); control_pipe = NULL; }
    }
    printf("Starting to transmit on %3.1f MHz.\n", freq);

    double t0 = now_s(), produced = 0, next_report = 1.0;
    float peak = 0;
    while (!stop_flag) {
        if (control_pipe) {
            /* Mirror what pi_fm_rds does (one command per loop) and track it. */
            int r = poll_control_pipe();
            (void)r;
        }
        if (fm_mpx_get_samples(data) < 0) {
            printf("Audio input ended, terminating.\n");
            break;
        }
        for (int i = 0; i < DATA_SIZE; i++) {
            float v = fabsf(data[i]);
            if (v > peak) peak = v;
        }
        produced += DATA_SIZE / MPX_RATE;

        double ahead = produced - (now_s() - t0);
        if (ahead > 0) usleep((useconds_t)(ahead * 1e6));

        if (produced >= next_report) {
            /* silence = stereo pilot + RDS only (peak ~2.1); music/tone go well above 3. */
            printf("SIM t=%.0f peak=%.2f audio=%d\n", produced, peak, peak > 3.0f);
            peak = 0;
            next_report += 1.0;
        }
    }
    fm_mpx_close();
    close_control_pipe();
    printf("Terminating: cleanly deactivated the DMA engine and killed the carrier.\n");
    return 0;
}
