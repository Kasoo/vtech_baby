/* The per-session task lists (kit: iotc/70-tasks.md). One mutex guards every session's list; tasks
 * are deleted and nodes freed outside it, so a task's own teardown cannot deadlock against it. */
#include <stdlib.h>

#include "iotc.h"

int SessionTaskAdd(TSessionTaskList *list, TTask *task, int type) {
    TSessionTaskNode *n = calloc(1, sizeof *n);

    if (n == NULL)
        return -1;
    n->handle = (uint64_t)(uintptr_t)task;
    n->type = type;
    pthread_mutex_lock(&iotc.task_lock);
    if (list->tail)
        list->tail->next = n;
    else
        list->head = n;
    list->tail = n;
    pthread_mutex_unlock(&iotc.task_lock);
    return 0;
}

/* Unlink n, whose predecessor is prev (NULL at the head). */
static void unlink_node(TSessionTaskList *list, TSessionTaskNode *prev, TSessionTaskNode *n) {
    if (prev)
        prev->next = n->next;
    else
        list->head = n->next;
    if (list->tail == n)
        list->tail = prev;
    n->next = NULL;
}

void SessionTaskCleanNodesWithType(TSessionTaskList *list, int type) {
    TSessionTaskNode *removed = NULL, *prev = NULL, *n;

    pthread_mutex_lock(&iotc.task_lock);
    n = list->head;
    while (n) {
        TSessionTaskNode *next = n->next;

        if (n->type == type) {
            unlink_node(list, prev, n);
            n->next = removed;
            removed = n;
        } else {
            prev = n;
        }
        n = next;
    }
    pthread_mutex_unlock(&iotc.task_lock);
    while (removed) {
        n = removed->next;
        tutk_TaskMng_Delete((TTask *)(uintptr_t)removed->handle);
        free(removed);
        removed = n;
    }
}

void SessionTaskDeleteNode(TSessionTaskList *list, uint64_t task_handle, char delete_task) {
    TSessionTaskNode *prev = NULL, *n;

    pthread_mutex_lock(&iotc.task_lock);
    for (n = list->head; n && n->handle != task_handle; n = n->next)
        prev = n;
    if (n)
        unlink_node(list, prev, n);
    pthread_mutex_unlock(&iotc.task_lock);
    if (n == NULL)
        return;
    if (delete_task)
        tutk_TaskMng_Delete((TTask *)(uintptr_t)n->handle);
    free(n);
}
