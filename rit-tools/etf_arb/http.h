/* Tiny HTTP/1.1 client for talking to the local RIT Client REST API.
 * Keep-alive, Content-Length and chunked bodies, Windows (Winsock) + macOS/Linux. */
#ifndef RIT_HTTP_H
#define RIT_HTTP_H
#include <stddef.h>

typedef struct {
    char host[128];
    int port;
    long long sock;       /* SOCKET on Windows, int fd elsewhere; -1 = not connected */
    char *buf;            /* response buffer (grown as needed) */
    size_t cap;
} http_conn;

typedef struct {
    int status;           /* HTTP status, or -1 on network error */
    char *body;           /* points into conn->buf, NUL-terminated, valid until next request */
    size_t body_len;
    double retry_after;   /* seconds from Retry-After header, 0 if none */
} http_resp;

int  http_global_init(void);                       /* call once (WSAStartup on Windows) */
void http_conn_init(http_conn *c, const char *host, int port);
void http_conn_close(http_conn *c);
/* path includes the query string, e.g. "/v1/orders?ticker=RITC&..." */
http_resp http_request(http_conn *c, const char *method, const char *path, const char *api_key);

void sleep_ms(int ms);
double now_ms(void);

#endif
