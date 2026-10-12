/* The measuring parent of one timed invocation (perf.py, invoke_measured): rusage_spawn REPORT_FD COMMAND [ARGS...]
 *
 * On Linux a process's peak RSS starts at the peak of the mm it exec'd from (exec_mmap carries the old mm's hiwater
 * RSS into signal->maxrss), so a child spawned straight from the harness reports the harness's peak. This program is
 * exec'd fresh, so its own mm is tiny; it spawns COMMAND, which inherits stdin, stdout, stderr, cwd and the
 * environment, reaps it with wait4 and writes one line to REPORT_FD:
 *     W <wait status> <ru_maxrss> <nanoseconds from spawn to reap>
 * or, when COMMAND could not be started, E <errno>. The interval is taken here, around the spawn and the reap, so
 * starting this program costs the measurement nothing. */
#include <errno.h>
#include <fcntl.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: rusage_spawn REPORT_FD COMMAND [ARGS...]\n");
        return 2;
    }
    int report = atoi(argv[1]);
    fcntl(report, F_SETFD, FD_CLOEXEC);
    struct timespec started, ended;
    clock_gettime(CLOCK_MONOTONIC, &started);
    pid_t pid;
    int error = posix_spawnp(&pid, argv[2], NULL, NULL, &argv[2], environ);
    if (error != 0) {
        dprintf(report, "E %d\n", error);
        return 127;
    }
    close(0), close(1), close(2);  /* the pipes end when the command's copies do */
    int status;
    struct rusage usage;
    while (wait4(pid, &status, 0, &usage) < 0) {
        if (errno != EINTR) {
            dprintf(report, "E %d\n", errno);
            return 127;
        }
    }
    clock_gettime(CLOCK_MONOTONIC, &ended);
    long long ns = (long long)(ended.tv_sec - started.tv_sec) * 1000000000LL + (ended.tv_nsec - started.tv_nsec);
    dprintf(report, "W %d %ld %lld\n", status, (long)usage.ru_maxrss, ns);
    return 0;
}
