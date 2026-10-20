/* The task manager: periodic callbacks run on one thread (kit: global/30-tasks.md). Tasks are kept in
 * a list ordered by id, which a pass walks in order. */
#include <stdlib.h>
#include <sys/time.h>
#include <unistd.h>

#include "tk.h"

#define BASE_TICK_MS 100

struct TTask {
    int          id;
    uint32_t     first_run_ms;      /* 0 until the first run */
    uint32_t     last_run_ms;       /* 0 until the first run */
    uint32_t     lifetime_ms;       /* 0 means forever */
    uint32_t     interval_ms;
    uint16_t     tag;
    TTaskCb      cb;                /* NULL once the task is deleted */
    void        *arg;
    struct TTask *next;
};

static struct {
    int             init_count;
    volatile bool   running;
    uint64_t        thread;
    pthread_mutex_t lock;           /* recursive; callbacks run with it held */
    TTask          *tasks;          /* live and deleted-not-freed tasks, ascending id */
    int             pending_deletes;
    int             walk_depth;     /* > 0 while a pass walks the list; purges wait */
    uint32_t        tick_ms;
    bool            any_run;
} mng = {.tick_ms = BASE_TICK_MS};

static uint32_t now_ms(void) {
    struct timeval tv;

    gettimeofday(&tv, NULL);
    return (uint32_t)((uint64_t)tv.tv_sec * 1000 + (uint64_t)tv.tv_usec / 1000);
}

static void sleep_ms(uint32_t ms) { usleep((useconds_t)ms * 1000); }

static uint32_t gcd(uint32_t a, uint32_t b) {
    while (b) {
        uint32_t t = a % b;
        a = b;
        b = t;
    }
    return a;
}

/* The tick is the GCD of 100 and every live task's interval. */
static void recompute_tick(void) {
    uint32_t tick = BASE_TICK_MS;

    for (TTask *t = mng.tasks; t; t = t->next)
        if (t->cb)
            tick = gcd(tick, t->interval_ms);
    mng.tick_ms = tick ? tick : BASE_TICK_MS;
}

/* Free the deleted tasks, unless a pass is walking the list or nothing is pending. */
static void purge(void) {
    TTask **link = &mng.tasks;

    if (mng.walk_depth > 0 || mng.pending_deletes == 0)
        return;
    while (*link) {
        TTask *t = *link;
        if (t->cb == NULL) {
            *link = t->next;
            free(t);
        } else {
            link = &t->next;
        }
    }
    mng.pending_deletes = 0;
    recompute_tick();
}

static void mark_deleted(TTask *t) {
    t->cb = NULL;
    t->arg = NULL;
    mng.pending_deletes++;
}

/* One pass: run every live task whose interval has elapsed. Returns how many ran. */
static int run_pass(void) {
    uint32_t now = now_ms();
    int ran = 0;

    mng.walk_depth++;
    /* Tasks are only freed by a purge, which waits for walk_depth 0, so `next` stays valid even
     * if a callback deletes or creates tasks. */
    for (TTask *t = mng.tasks; t; t = t->next) {
        if (t->cb == NULL || now - t->last_run_ms < t->interval_ms)
            continue;
        t->last_run_ms = now;
        if (t->first_run_ms == 0)
            t->first_run_ms = now;
        if (t->lifetime_ms != 0 && now - t->first_run_ms > t->lifetime_ms - 1) {
            t->cb(t, (int)t->interval_ms, t->tag, t->arg, 1);
            if (t->cb)
                mark_deleted(t);
        } else {
            t->cb(t, (int)t->interval_ms, t->tag, t->arg, 0);
        }
        ran++;
    }
    mng.walk_depth--;
    return ran;
}

static void *task_thread(void *arg) {
    tutk_platform_set_thread_name("iotc_taskmgr");
    while (mng.running) {
        uint32_t start = now_ms(), elapsed;
        int ran;

        pthread_mutex_lock(&mng.lock);
        ran = run_pass();
        if (ran)
            mng.any_run = true;
        purge();
        pthread_mutex_unlock(&mng.lock);

        if (ran) {
            elapsed = now_ms() - start;
            sleep_ms(elapsed < 4 ? 5 - elapsed : 1);
        } else {
            sleep_ms(mng.any_run ? mng.tick_ms : 50);
        }
    }
    return NULL;
}

TK_EXPORT int tutk_TaskMng_Init(void) {
    int err;

    if (mng.init_count == 0) {
        tutk_platform_mutex_init(&mng.lock);
        mng.running = true;
        mng.thread = tutk_platform_CreateTask(&err, task_thread, NULL, 0, 0);
        if (err == -1) {
            mng.running = false;
            return -2;
        }
    }
    mng.init_count++;
    return 0;
}

TK_EXPORT TTask *tutk_TaskMng_Create(int interval_ms, int lifetime_ms, uint16_t tag, TTaskCb cb,
                                     void *arg) {
    TTask *t, **link;
    int id = 1;

    pthread_mutex_lock(&mng.lock);
    purge();
    /* The smallest positive id not in use: walk the ordered list until a gap. */
    for (link = &mng.tasks; *link && (*link)->id == id; link = &(*link)->next)
        id++;
    t = calloc(1, sizeof *t);
    if (t) {
        t->id = id;
        t->interval_ms = (uint32_t)interval_ms;
        t->lifetime_ms = (uint32_t)lifetime_ms;
        t->tag = tag;
        t->cb = cb;
        t->arg = arg;
        t->next = *link;
        *link = t;
        recompute_tick();
    }
    purge();
    pthread_mutex_unlock(&mng.lock);
    tk_trace("TaskMng_Create(%d ms, life %d, tag %u) -> %p id %d", interval_ms, lifetime_ms, tag,
             (void *)t, t ? t->id : 0);
    return t;
}

TK_EXPORT int tutk_TaskMng_Delete(TTask *task) {
    int live;

    pthread_mutex_lock(&mng.lock);
    live = task->cb != NULL;
    if (live)
        mark_deleted(task);
    purge();
    pthread_mutex_unlock(&mng.lock);
    tk_trace("TaskMng_Delete(%p) -> %d", (void *)task, live);
    return live;
}
