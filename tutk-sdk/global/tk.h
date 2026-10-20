/* Internal declarations shared by Global's source files. */
#ifndef TK_H
#define TK_H

#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>

#include "tutk_shared.h"
#include "tk_common.h"

/* Exported functions that other files call. */
void *tutk_platform_malloc(int size);
void *tutk_platform_calloc(int count, int size);
void  tutk_platform_free(void *p);
int   tutk_platform_mutex_init(pthread_mutex_t *m);
int   tutk_platform_mutex_destroy(pthread_mutex_t *m);
uint64_t tutk_platform_CreateTask(int *err, TThreadStart start, void *arg, char detach, int stack_size);
void  tutk_platform_set_thread_name(const char *name);
int   tutk_Host_Get_Sockaddr(const char *host, int transport, uint16_t port_be, unsigned int mode,
                             struct sockaddr_in6 *out);
int   TUTK3rdSHA256(const void *data, int len, uint8_t *out, unsigned int out_size);
int   TUTK3rdBase64Decode(const void *b64, unsigned int len, void **out);

/* ---- crypto.c: internal ---- */
int TUTK3rdXOR(const void *in, unsigned int in_len, const void *key, unsigned int key_len,
               void *out, unsigned int out_len) TK_HIDDEN;
int TUTK3rdRSABlockSize(const char *pem_key, int is_public) TK_HIDDEN;
int TUTK3rdRSACrypt(const char *pem_key, int is_public, int encrypt, const void *in, int in_len,
                    void *out, unsigned int out_len) TK_HIDDEN;
int TUTK3rdRSADecrypt(const char *pem_key, int is_public, const void *in, int in_len,
                      void *out, unsigned int out_len) TK_HIDDEN;

/* ---- json.c ---- */
typedef struct json_object json_object;

int TUTK_Json_Obj_Create_From_String(const char *text, json_object **out);
int TUTK_Json_Obj_Release(json_object *obj);
int TUTK_Json_Obj_Get_Sub_Obj(json_object *obj, const char *key, json_object **out);
int TUTK_Json_Obj_Get_Sub_Obj_String(json_object *obj, const char *key, const char **out);
int TUTK_Json_Obj_Get_Sub_Obj_Bool(json_object *obj, const char *key, int *out);

/* Members of an object in document order, and a string value; for walking `standalone`. */
int         tk_json_obj_count(const json_object *obj) TK_HIDDEN;
const char *tk_json_obj_key(const json_object *obj, int i) TK_HIDDEN;
json_object *tk_json_obj_value(const json_object *obj, int i) TK_HIDDEN;
const char *tk_json_string(const json_object *obj) TK_HIDDEN;     /* NULL if not a string */

/* ---- lists.c ---- */
TList     *tlistNew(void);
int        tlistAppend(TList *list, void *data);
TListNode *tlistHead(TList *list);
int        tlistRemove(TList *list, void *data);
int        tlistDestroy(TList *list);

/* The locked FIFO queue used by the connection manager. */
typedef struct TutkList TutkList;

int   TutkListNew(TutkList **out) TK_HIDDEN;
int   TutkList_push(TutkList *q, void *item) TK_HIDDEN;
void *TutkList_pop(TutkList *q) TK_HIDDEN;

#endif
