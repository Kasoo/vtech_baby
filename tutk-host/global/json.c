/* JSON wrappers (kit: global/70-wrappers.md) over a small built-in parser. A parsed root is owned by
 * the caller and released with TUTK_Json_Obj_Release; values fetched from it are borrowed. */
#include <stdlib.h>
#include <string.h>

#include "tk.h"

#define MAX_DEPTH 64

enum { J_NULL, J_BOOL, J_NUMBER, J_STRING, J_ARRAY, J_OBJECT };

struct json_object {
    int           type;
    int           refcount;
    int           boolean;
    double        number;
    char         *string;
    int           count;        /* members of an object or elements of an array */
    char        **keys;         /* object member names */
    json_object **values;       /* object member values or array elements */
};

typedef struct {
    const char *p;
    int         depth;
} Parser;

static void json_free(json_object *o) {
    if (o == NULL)
        return;
    for (int i = 0; i < o->count; i++) {
        if (o->keys)
            free(o->keys[i]);
        json_free(o->values[i]);
    }
    free(o->keys);
    free(o->values);
    free(o->string);
    free(o);
}

static void skip_ws(Parser *ps) {
    while (*ps->p == ' ' || *ps->p == '\t' || *ps->p == '\n' || *ps->p == '\r')
        ps->p++;
}

static int hex4(const char *s) {
    int v = 0;
    for (int i = 0; i < 4; i++) {
        char c = s[i];
        v <<= 4;
        if (c >= '0' && c <= '9') v |= c - '0';
        else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
        else return -1;
    }
    return v;
}

static int put_utf8(char *dst, unsigned long cp) {
    if (cp < 0x80) { dst[0] = (char)cp; return 1; }
    if (cp < 0x800) { dst[0] = (char)(0xc0 | cp >> 6); dst[1] = (char)(0x80 | (cp & 0x3f)); return 2; }
    if (cp < 0x10000) {
        dst[0] = (char)(0xe0 | cp >> 12); dst[1] = (char)(0x80 | (cp >> 6 & 0x3f));
        dst[2] = (char)(0x80 | (cp & 0x3f)); return 3;
    }
    dst[0] = (char)(0xf0 | cp >> 18); dst[1] = (char)(0x80 | (cp >> 12 & 0x3f));
    dst[2] = (char)(0x80 | (cp >> 6 & 0x3f)); dst[3] = (char)(0x80 | (cp & 0x3f));
    return 4;
}

/* Parse a string starting at the opening quote; returns a malloc'd C string or NULL. */
static char *parse_string(Parser *ps) {
    const char *s = ps->p + 1, *end = s;
    char *out, *d;

    while (*end && *end != '"') {
        if (*end == '\\' && end[1])
            end++;
        end++;
    }
    if (*end != '"')
        return NULL;
    out = d = malloc((size_t)(end - s) + 1);     /* escapes never lengthen the text */
    if (out == NULL)
        return NULL;
    while (s < end) {
        unsigned char c = (unsigned char)*s++;
        if (c < 0x20)
            goto bad;
        if (c != '\\') {
            *d++ = (char)c;
            continue;
        }
        switch (*s++) {
        case '"':  *d++ = '"'; break;
        case '\\': *d++ = '\\'; break;
        case '/':  *d++ = '/'; break;
        case 'b':  *d++ = '\b'; break;
        case 'f':  *d++ = '\f'; break;
        case 'n':  *d++ = '\n'; break;
        case 'r':  *d++ = '\r'; break;
        case 't':  *d++ = '\t'; break;
        case 'u': {
            long cp = end - s >= 4 ? hex4(s) : -1;
            if (cp < 0)
                goto bad;
            s += 4;
            if (cp >= 0xd800 && cp < 0xdc00) {
                long lo = end - s >= 6 && s[0] == '\\' && s[1] == 'u' ? hex4(s + 2) : -1;
                if (lo < 0xdc00 || lo >= 0xe000)
                    goto bad;
                cp = 0x10000 + ((cp - 0xd800) << 10) + (lo - 0xdc00);
                s += 6;
            } else if (cp >= 0xdc00 && cp < 0xe000) {
                goto bad;
            }
            d += put_utf8(d, (unsigned long)cp);
            break;
        }
        default:
            goto bad;
        }
    }
    *d = '\0';
    ps->p = end + 1;
    return out;
bad:
    free(out);
    return NULL;
}

static json_object *parse_value(Parser *ps);

static int append(json_object *o, char *key, json_object *v) {
    json_object **values = realloc(o->values, sizeof *values * (size_t)(o->count + 1));
    if (values == NULL)
        return -1;
    o->values = values;
    if (o->type == J_OBJECT) {
        char **keys = realloc(o->keys, sizeof *keys * (size_t)(o->count + 1));
        if (keys == NULL)
            return -1;
        o->keys = keys;
        o->keys[o->count] = key;
    }
    o->values[o->count++] = v;
    return 0;
}

static json_object *parse_container(Parser *ps, json_object *o, char close) {
    ps->p++;
    skip_ws(ps);
    if (*ps->p == close) {
        ps->p++;
        return o;
    }
    for (;;) {
        char *key = NULL;
        json_object *v;

        skip_ws(ps);
        if (o->type == J_OBJECT) {
            if (*ps->p != '"' || (key = parse_string(ps)) == NULL)
                goto bad;
            skip_ws(ps);
            if (*ps->p++ != ':') {
                free(key);
                goto bad;
            }
        }
        v = parse_value(ps);
        if (v == NULL || append(o, key, v) != 0) {
            free(key);
            json_free(v);
            goto bad;
        }
        skip_ws(ps);
        if (*ps->p == ',') {
            ps->p++;
            continue;
        }
        if (*ps->p++ == close)
            return o;
        goto bad;
    }
bad:
    json_free(o);
    return NULL;
}

static json_object *parse_value(Parser *ps) {
    json_object *o;

    skip_ws(ps);
    if (++ps->depth > MAX_DEPTH)
        return NULL;
    o = calloc(1, sizeof *o);
    if (o == NULL)
        return NULL;
    o->refcount = 1;
    switch (*ps->p) {
    case '{':
        o->type = J_OBJECT;
        o = parse_container(ps, o, '}');
        break;
    case '[':
        o->type = J_ARRAY;
        o = parse_container(ps, o, ']');
        break;
    case '"':
        o->type = J_STRING;
        if ((o->string = parse_string(ps)) == NULL) {
            json_free(o);
            o = NULL;
        }
        break;
    case 't':
    case 'f':
    case 'n': {
        static const struct { const char *word; int type, value; } words[] = {
            {"true", J_BOOL, 1}, {"false", J_BOOL, 0}, {"null", J_NULL, 0}};
        size_t i;
        for (i = 0; i < 3; i++) {
            size_t n = strlen(words[i].word);
            if (strncmp(ps->p, words[i].word, n) == 0) {
                o->type = words[i].type;
                o->boolean = words[i].value;
                ps->p += n;
                break;
            }
        }
        if (i == 3) {
            json_free(o);
            o = NULL;
        }
        break;
    }
    default: {
        char *end;
        if (*ps->p != '-' && (*ps->p < '0' || *ps->p > '9')) {
            json_free(o);
            o = NULL;
            break;
        }
        o->type = J_NUMBER;
        o->number = strtod(ps->p, &end);
        ps->p = end;
        break;
    }
    }
    ps->depth--;
    return o;
}

TK_EXPORT int TUTK_Json_Obj_Create_From_String(const char *text, json_object **out) {
    Parser ps = {text, 0};
    json_object *root;

    if (out == NULL)
        return TJSON_ER_INVALID_ARG;
    *out = NULL;
    if (text == NULL)
        return TJSON_ER_INVALID_ARG;
    root = parse_value(&ps);
    if (root == NULL)
        return TJSON_ER_INVALID_ARG;
    skip_ws(&ps);
    if (*ps.p != '\0') {
        json_free(root);
        return TJSON_ER_INVALID_ARG;
    }
    if (root->type != J_OBJECT && root->type != J_ARRAY) {
        json_free(root);
        return TJSON_ER_WRONG_TYPE;
    }
    *out = root;
    return 0;
}

TK_EXPORT int TUTK_Json_Obj_Release(json_object *obj) {
    if (obj == NULL || --obj->refcount > 0)
        return TJSON_ER_INVALID_ARG;
    json_free(obj);
    return 0;
}

/* The last member named `key` wins, as with duplicate keys in json-c. */
TK_EXPORT int TUTK_Json_Obj_Get_Sub_Obj(json_object *obj, const char *key, json_object **out) {
    if (obj == NULL || key == NULL || out == NULL)
        return TJSON_ER_INVALID_ARG;
    *out = NULL;
    if (obj->type != J_OBJECT)
        return TJSON_ER_WRONG_TYPE;
    for (int i = obj->count - 1; i >= 0; i--) {
        if (strcmp(obj->keys[i], key) == 0) {
            *out = obj->values[i];
            return 0;
        }
    }
    return TJSON_ER_NO_MEMBER;
}

TK_EXPORT int TUTK_Json_Obj_Get_Sub_Obj_String(json_object *obj, const char *key, const char **out) {
    json_object *v;
    int rc = TUTK_Json_Obj_Get_Sub_Obj(obj, key, &v);

    if (rc != 0)
        return rc;
    if (v->type == J_NULL || out == NULL)
        return TJSON_ER_INVALID_ARG;
    if (v->type != J_STRING)
        return TJSON_ER_WRONG_TYPE;
    *out = v->string;
    return 0;
}

TK_EXPORT int TUTK_Json_Obj_Get_Sub_Obj_Bool(json_object *obj, const char *key, int *out) {
    json_object *v;
    int rc = TUTK_Json_Obj_Get_Sub_Obj(obj, key, &v);

    if (rc != 0)
        return rc;
    if (v->type != J_BOOL)
        return TJSON_ER_WRONG_TYPE;
    if (out == NULL)
        return TJSON_ER_INVALID_ARG;
    *out = v->boolean;
    return 0;
}

int tk_json_obj_count(const json_object *obj) { return obj && obj->type == J_OBJECT ? obj->count : 0; }

const char *tk_json_obj_key(const json_object *obj, int i) { return obj->keys[i]; }

json_object *tk_json_obj_value(const json_object *obj, int i) { return obj->values[i]; }

const char *tk_json_string(const json_object *obj) {
    return obj && obj->type == J_STRING ? obj->string : NULL;
}
