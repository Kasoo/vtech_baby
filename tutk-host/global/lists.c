/* The tlist container and the locked FIFO queue (kit: global/20-lists.md). */
#include <stdlib.h>

#include "tk.h"

/* ---- tlist: not thread-safe; callers lock around it ---- */

struct TList {
    TListNode *head;
    TListNode *tail;
    int        count;
};

TK_EXPORT TList *tlistNew(void) { return calloc(1, sizeof(TList)); }

TK_EXPORT int tlistAppend(TList *list, void *data) {
    TListNode *node;

    if (list == NULL)
        return -TOS_ER_INVALID_ARG;
    node = calloc(1, sizeof *node);
    if (node == NULL)
        return -TOS_ER_NO_MEMORY;
    node->data = data;
    node->prev = list->tail;
    if (list->tail)
        list->tail->next = node;
    else
        list->head = node;
    list->tail = node;
    list->count++;
    return 0;
}

TK_EXPORT TListNode *tlistHead(TList *list) { return list ? list->head : NULL; }

TK_EXPORT void *tlistAtIndex(TList *list, int index) {
    TListNode *node;

    if (list == NULL || index < 0)
        return NULL;
    for (node = list->head; node && index > 0; index--)
        node = node->next;
    return node ? node->data : NULL;
}

TK_EXPORT int tlistLength(TList *list) { return list ? list->count : 0; }

TK_EXPORT int tlistForeach(TList *list, void (*fn)(void *data, void *arg), void *arg) {
    if (list == NULL || fn == NULL)
        return -TOS_ER_INVALID_ARG;
    for (TListNode *node = list->head; node; node = node->next)
        fn(node->data, arg);
    return 0;
}

TK_EXPORT int tlistRemove(TList *list, void *data) {
    TListNode *node;

    if (list == NULL)
        return -TOS_ER_INVALID_ARG;
    for (node = list->head; node && node->data != data; node = node->next)
        ;
    if (node == NULL)
        return -TOS_ER_NOT_FOUND;
    if (node->prev)
        node->prev->next = node->next;
    else
        list->head = node->next;
    if (node->next)
        node->next->prev = node->prev;
    else
        list->tail = node->prev;
    list->count--;
    free(node);
    return 0;
}

TK_EXPORT int tlistDestroy(TList *list) {
    TListNode *node, *next;

    if (list == NULL)
        return -TOS_ER_INVALID_ARG;
    for (node = list->head; node; node = next) {
        next = node->next;
        free(node);
    }
    free(list);
    return 0;
}

/* ---- TutkList: a FIFO of non-NULL pointers under its own lock ---- */

typedef struct TutkListNode {
    void                *item;
    struct TutkListNode *next;
} TutkListNode;

struct TutkList {
    pthread_mutex_t lock;
    TutkListNode   *head;
    TutkListNode   *tail;
};

int TutkListNew(TutkList **out) {
    TutkList *q = calloc(1, sizeof *q);

    if (q == NULL)
        return TUTK_ER_MEM_INSUFFICIENT;
    tutk_platform_mutex_init(&q->lock);
    *out = q;
    return 0;
}

int TutkList_push(TutkList *q, void *item) {
    TutkListNode *node;

    if (item == NULL)
        return TUTK_ER_INVALID_ARG;
    node = calloc(1, sizeof *node);
    if (node == NULL)
        return TUTK_ER_MEM_INSUFFICIENT;
    node->item = item;
    pthread_mutex_lock(&q->lock);
    if (q->tail)
        q->tail->next = node;
    else
        q->head = node;
    q->tail = node;
    pthread_mutex_unlock(&q->lock);
    return 0;
}

void *TutkList_pop(TutkList *q) {
    TutkListNode *node;
    void *item = NULL;

    pthread_mutex_lock(&q->lock);
    node = q->head;
    if (node) {
        q->head = node->next;
        if (q->head == NULL)
            q->tail = NULL;
        item = node->item;
    }
    pthread_mutex_unlock(&q->lock);
    free(node);
    return item;
}
