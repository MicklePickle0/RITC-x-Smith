/* Minimal, dependency-free JSON parser (DOM, read-only).
 * Parses a buffer IN PLACE (strings are decoded into the input buffer),
 * nodes come from a pool that is freed in one call. Good enough for RIT responses. */
#ifndef RIT_JSON_H
#define RIT_JSON_H
#include <stddef.h>

typedef enum { J_NULL, J_BOOL, J_NUM, J_STR, J_ARR, J_OBJ } jtype;

typedef struct jnode {
    jtype type;
    double num;            /* J_NUM, J_BOOL (0/1) */
    const char *str;       /* J_STR */
    const char *key;       /* set when this node is an object member */
    struct jnode *child;   /* first element / member */
    struct jnode *next;    /* next sibling */
    int len;               /* number of children */
} jnode;

typedef struct jdoc jdoc;

/* Parse `buf` (modified in place, must stay alive while the doc is used). NULL on error. */
jdoc  *json_parse(char *buf, size_t len);
jnode *json_root(jdoc *d);
void   json_free(jdoc *d);

jnode      *json_get(const jnode *obj, const char *key);       /* object member or NULL */
jnode      *json_at(const jnode *arr, int i);                  /* array element or NULL */
double      json_num(const jnode *obj, const char *key, double dflt);
const char *json_str(const jnode *obj, const char *key, const char *dflt);
int         json_bool(const jnode *obj, const char *key, int dflt);

#endif
