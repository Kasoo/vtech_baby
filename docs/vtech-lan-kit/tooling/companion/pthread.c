/*
 * companion/pthread.c -- pthread STATE-translation layer for the companion libtutkabi.so.
 *
 * The TUTK libraries are compiled against bionic (Android libc) and allocate bionic-laid-out
 * pthread objects, passing their addresses to the pthread_* functions. bionic and the host
 * libc (glibc and musl) differ in the internal object layout even though the x86_64 LP64
 * object *sizes* match (mutex 40 / cond 48 / rwlock 56 / once 4 / key 4 / pthread_t 8). This
 * layer keeps a side table (bionic-object-address -> a host-initialized object) and routes
 * every op to the mapped host object.
 *
 * The patched TUTK libraries import these under the renamed tokens `Pthread_*` (see
 * elfpatch.py --rename), so the companion EXPORTS `Pthread_*` and calls the host `pthread_*`
 * directly by name (unversioned): because the export name differs from the host name there is
 * no recursion and no `.symver` machinery, and one glibc-built companion binds correctly on
 * both glibc and musl.
 *
 * The host-object init is libc-agnostic: glibc and musl mutex/rwlock/attr are byte-identical.
 */
#include <pthread.h>
#include <stdlib.h>
#include <stdint.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))

/* ------------------------------------------------------------------ side table */
struct ent { const void *key; void *val; };
static struct ent *g_mtx, *g_rw, *g_attr, *g_mattr;
static size_t n_mtx, c_mtx, n_rw, c_rw, n_attr, c_attr, n_mattr, c_mattr;

/* One real host mutex guards all four maps. Statically host-initialized. */
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static void lock(void)   { pthread_mutex_lock(&g_lock); }
static void unlock(void) { pthread_mutex_unlock(&g_lock); }

static void *map_get(struct ent *a, size_t n, const void *key) {
    for (size_t i = 0; i < n; i++) if (a[i].key == key) return a[i].val;
    return NULL;
}
static void map_put(struct ent **a, size_t *n, size_t *c, const void *key, void *val) {
    for (size_t i = 0; i < *n; i++) if ((*a)[i].key == key) { (*a)[i].val = val; return; }
    if (*n == *c) { *c = *c ? *c * 2 : 8; *a = realloc(*a, *c * sizeof(**a)); }
    (*a)[*n].key = key; (*a)[*n].val = val; (*n)++;
}
static void *map_del(struct ent *a, size_t *n, const void *key) {
    for (size_t i = 0; i < *n; i++)
        if (a[i].key == key) { void *v = a[i].val; a[i] = a[--(*n)]; return v; }
    return NULL;
}

/* Create a host mutex of the given type (NORMAL/RECURSIVE/ERRORCHECK -- 0/1/2, which are
 * identical values on bionic and host). */
static pthread_mutex_t *make_host_mutex(int type) {
    pthread_mutex_t *gm = malloc(sizeof(pthread_mutex_t));
    if (type > 0) {
        pthread_mutexattr_t ga;
        pthread_mutexattr_init(&ga);
        pthread_mutexattr_settype(&ga, type);
        pthread_mutex_init(gm, &ga);
        pthread_mutexattr_destroy(&ga);
    } else {
        pthread_mutex_init(gm, NULL);
    }
    return gm;
}

/* Lazily map a bionic mutex address to a host mutex. A mutex reaching us without a prior
 * Pthread_mutex_init() was STATIC-initialized; bionic encodes the type in bits[15:14] of the
 * first int32 ((type & 3) << 14): NORMAL=0, RECURSIVE=0x4000, ERRORCHECK=0x8000. */
static pthread_mutex_t *get_mtx(const void *m) {
    lock();
    pthread_mutex_t *gm = map_get(g_mtx, n_mtx, m);
    if (!gm) {
        int btype = (int)((*(const uint32_t *)m >> 14) & 3);
        gm = make_host_mutex(btype);
        map_put(&g_mtx, &n_mtx, &c_mtx, m, gm);
    }
    unlock();
    return gm;
}
static pthread_rwlock_t *get_rw(const void *m) {
    lock();
    pthread_rwlock_t *gr = map_get(g_rw, n_rw, m);
    if (!gr) {
        gr = malloc(sizeof(pthread_rwlock_t));
        pthread_rwlock_init(gr, NULL);
        map_put(&g_rw, &n_rw, &c_rw, m, gr);
    }
    unlock();
    return gr;
}

/* ------------------------------------------------------------------ mutex */
EXPORT int Pthread_mutex_init(pthread_mutex_t *m, const pthread_mutexattr_t *a) {
    lock();
    pthread_mutex_t *old = map_del(g_mtx, &n_mtx, m);
    if (old) { pthread_mutex_destroy(old); free(old); }
    int stored = a ? (int)(intptr_t)map_get(g_mattr, n_mattr, a) : 0;
    int type = stored ? stored - 1 : PTHREAD_MUTEX_NORMAL;
    map_put(&g_mtx, &n_mtx, &c_mtx, m, make_host_mutex(type));
    unlock();
    return 0;
}
EXPORT int Pthread_mutex_lock(pthread_mutex_t *m)    { return pthread_mutex_lock(get_mtx(m)); }
EXPORT int Pthread_mutex_trylock(pthread_mutex_t *m) { return pthread_mutex_trylock(get_mtx(m)); }
EXPORT int Pthread_mutex_unlock(pthread_mutex_t *m)  { return pthread_mutex_unlock(get_mtx(m)); }
EXPORT int Pthread_mutex_destroy(pthread_mutex_t *m) {
    lock();
    pthread_mutex_t *gm = map_del(g_mtx, &n_mtx, m);
    unlock();
    int rc = 0;
    if (gm) { rc = pthread_mutex_destroy(gm); free(gm); }
    return rc;
}

/* mutexattr: store the requested type keyed by the bionic attr address (as type+1). */
EXPORT int Pthread_mutexattr_init(pthread_mutexattr_t *a) {
    lock(); map_put(&g_mattr, &n_mattr, &c_mattr, a, (void *)(intptr_t)(PTHREAD_MUTEX_NORMAL + 1)); unlock();
    return 0;
}
EXPORT int Pthread_mutexattr_settype(pthread_mutexattr_t *a, int type) {
    lock(); map_put(&g_mattr, &n_mattr, &c_mattr, a, (void *)(intptr_t)(type + 1)); unlock();
    return 0;
}

/* ------------------------------------------------------------------ rwlock */
EXPORT int Pthread_rwlock_init(pthread_rwlock_t *m, const pthread_rwlockattr_t *a) {
    (void)a;
    lock();
    pthread_rwlock_t *old = map_del(g_rw, &n_rw, m);
    if (old) { pthread_rwlock_destroy(old); free(old); }
    pthread_rwlock_t *gr = malloc(sizeof(pthread_rwlock_t));
    pthread_rwlock_init(gr, NULL);
    map_put(&g_rw, &n_rw, &c_rw, m, gr);
    unlock();
    return 0;
}
EXPORT int Pthread_rwlock_rdlock(pthread_rwlock_t *m) { return pthread_rwlock_rdlock(get_rw(m)); }
EXPORT int Pthread_rwlock_wrlock(pthread_rwlock_t *m) { return pthread_rwlock_wrlock(get_rw(m)); }
EXPORT int Pthread_rwlock_unlock(pthread_rwlock_t *m) { return pthread_rwlock_unlock(get_rw(m)); }
EXPORT int Pthread_rwlock_destroy(pthread_rwlock_t *m) {
    lock();
    pthread_rwlock_t *gr = map_del(g_rw, &n_rw, m);
    unlock();
    int rc = 0;
    if (gr) { rc = pthread_rwlock_destroy(gr); free(gr); }
    return rc;
}

/* ------------------------------------------------------------------ thread attr */
EXPORT int Pthread_attr_init(pthread_attr_t *a) {
    pthread_attr_t *ga = malloc(sizeof(pthread_attr_t));
    int rc = pthread_attr_init(ga);
    lock(); map_put(&g_attr, &n_attr, &c_attr, a, ga); unlock();
    return rc;
}
/* The bionic caller sizes stacks against bionic's PTHREAD_STACK_MIN (16K), but the HOST minimum can be
 * far larger (glibc on aarch64: 128K -> EINVAL below that) and aarch64 signal frames grow with SVE/SME
 * state. Clamp UP to max(host minimum, STACK_FLOOR); a bigger stack costs nothing but (lazily-committed)
 * virtual address space. */
#define STACK_FLOOR (256 * 1024)
EXPORT int Pthread_attr_setstacksize(pthread_attr_t *a, size_t sz) {
    lock(); pthread_attr_t *ga = map_get(g_attr, n_attr, a); unlock();
    if (!ga) return 0;
    long hmin = sysconf(_SC_THREAD_STACK_MIN);
    size_t floor = (hmin > 0 && (size_t)hmin > STACK_FLOOR) ? (size_t)hmin : STACK_FLOOR;
    return pthread_attr_setstacksize(ga, sz < floor ? floor : sz);
}
EXPORT int Pthread_attr_destroy(pthread_attr_t *a) {
    lock(); pthread_attr_t *ga = map_del(g_attr, &n_attr, a); unlock();
    if (ga) { pthread_attr_destroy(ga); free(ga); }
    return 0;
}

/* ------------------------------------------------------------------ lifecycle */
EXPORT int Pthread_create(pthread_t *t, const pthread_attr_t *a,
                          void *(*fn)(void *), void *arg) {
    pthread_attr_t *ga = NULL;
    if (a) { lock(); ga = map_get(g_attr, n_attr, a); unlock(); }
    return pthread_create(t, ga, fn, arg);
}
EXPORT int Pthread_join(pthread_t t, void **retval) { return pthread_join(t, retval); }
EXPORT int Pthread_detach(pthread_t t)              { return pthread_detach(t); }
EXPORT pthread_t Pthread_self(void)                 { return pthread_self(); }
EXPORT int Pthread_equal(pthread_t a, pthread_t b)  { return pthread_equal(a, b); }
EXPORT __attribute__((noreturn)) void Pthread_exit(void *retval) { pthread_exit(retval); }

/* ------------------------------------------------------------------ once / key
 * Compatible widths + self-consistent (only the host touches them after) -> forward. */
EXPORT int Pthread_once(pthread_once_t *o, void (*fn)(void)) { return pthread_once(o, fn); }
EXPORT int Pthread_key_create(pthread_key_t *k, void (*d)(void *)) { return pthread_key_create(k, d); }
EXPORT int Pthread_key_delete(pthread_key_t k)               { return pthread_key_delete(k); }
EXPORT void *Pthread_getspecific(pthread_key_t k)            { return pthread_getspecific(k); }
EXPORT int Pthread_setspecific(pthread_key_t k, const void *v) { return pthread_setspecific(k, v); }
