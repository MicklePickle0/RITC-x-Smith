#include "http.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#ifdef _WIN32
  #define WIN32_LEAN_AND_MEAN
  #include <winsock2.h>
  #include <ws2tcpip.h>
  #include <windows.h>
  typedef SOCKET sock_t;
  #define CLOSESOCK closesocket
  #define BADSOCK INVALID_SOCKET
#else
  #include <sys/types.h>
  #include <sys/socket.h>
  #include <sys/time.h>
  #include <netdb.h>
  #include <netinet/in.h>
  #include <netinet/tcp.h>
  #include <unistd.h>
  #include <time.h>
  typedef int sock_t;
  #define CLOSESOCK close
  #define BADSOCK (-1)
#endif

int http_global_init(void) {
#ifdef _WIN32
    WSADATA w;
    return WSAStartup(MAKEWORD(2, 2), &w) == 0 ? 0 : -1;
#else
    return 0;
#endif
}

void sleep_ms(int ms) {
    if (ms <= 0) return;
#ifdef _WIN32
    Sleep((DWORD)ms);
#else
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    nanosleep(&ts, NULL);
#endif
}

double now_ms(void) {
#ifdef _WIN32
    LARGE_INTEGER f, c;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart * 1000.0 / (double)f.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
#endif
}

void http_conn_init(http_conn *c, const char *host, int port) {
    memset(c, 0, sizeof(*c));
    snprintf(c->host, sizeof(c->host), "%s", host);
    c->port = port;
    c->sock = -1;
    c->cap = 1 << 16;
    c->buf = (char *)malloc(c->cap);
}

void http_conn_close(http_conn *c) {
    if (c->sock != -1) { CLOSESOCK((sock_t)c->sock); c->sock = -1; }
}

static int do_connect(http_conn *c) {
    struct addrinfo hints, *res = NULL, *ai;
    char port[16];
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    snprintf(port, sizeof(port), "%d", c->port);
    if (getaddrinfo(c->host, port, &hints, &res) != 0) return -1;
    sock_t s = BADSOCK;
    for (ai = res; ai; ai = ai->ai_next) {
        s = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (s == BADSOCK) continue;
        if (connect(s, ai->ai_addr, (int)ai->ai_addrlen) == 0) break;
        CLOSESOCK(s);
        s = BADSOCK;
    }
    freeaddrinfo(res);
    if (s == BADSOCK) return -1;
    int one = 1;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof(one)); /* no Nagle delay */
#ifdef _WIN32
    DWORD tmo = 3000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tmo, sizeof(tmo));
#else
    struct timeval tv = { 3, 0 };
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
#endif
    c->sock = (long long)s;
    return 0;
}

static int send_all(sock_t s, const char *p, size_t n) {
    while (n > 0) {
        int k = send(s, p, (int)n, 0);
        if (k <= 0) return -1;
        p += k; n -= (size_t)k;
    }
    return 0;
}

static int ensure_cap(http_conn *c, size_t need) {
    if (need <= c->cap) return 0;
    size_t nc = c->cap * 2;
    while (nc < need) nc *= 2;
    char *nb = (char *)realloc(c->buf, nc);
    if (!nb) return -1;
    c->buf = nb; c->cap = nc;
    return 0;
}

/* case-insensitive search for a header value inside the header block */
static const char *find_header(const char *hdrs, const char *name) {
    size_t n = strlen(name);
    for (const char *p = hdrs; *p; p++) {
        if ((p == hdrs || p[-1] == '\n')) {
            size_t i = 0;
            while (i < n && p[i] && tolower((unsigned char)p[i]) == tolower((unsigned char)name[i])) i++;
            if (i == n && p[i] == ':') {
                p += n + 1;
                while (*p == ' ') p++;
                return p;
            }
        }
    }
    return NULL;
}

static http_resp request_once(http_conn *c, const char *method, const char *path, const char *key) {
    http_resp r = { -1, NULL, 0, 0 };
    if (c->sock == -1 && do_connect(c) != 0) return r;
    sock_t s = (sock_t)c->sock;

    char req[2048];
    int n = snprintf(req, sizeof(req),
                     "%s %s HTTP/1.1\r\nHost: %s:%d\r\nX-API-Key: %s\r\nAccept: application/json\r\n"
                     "Connection: keep-alive\r\nContent-Length: 0\r\n\r\n",
                     method, path, c->host, c->port, key);
    if (n <= 0 || n >= (int)sizeof(req) || send_all(s, req, (size_t)n) != 0) { http_conn_close(c); return r; }

    size_t got = 0, hdr_end = 0;
    /* read until end of headers */
    for (;;) {
        if (ensure_cap(c, got + 4097) != 0) { http_conn_close(c); return r; }
        int k = recv(s, c->buf + got, 4096, 0);
        if (k <= 0) { http_conn_close(c); return r; }
        got += (size_t)k;
        c->buf[got] = '\0';
        char *e = strstr(c->buf, "\r\n\r\n");
        if (e) { hdr_end = (size_t)(e - c->buf) + 4; break; }
    }
    /* status + headers */
    int status = 0;
    if (sscanf(c->buf, "HTTP/%*s %d", &status) != 1) { http_conn_close(c); return r; }
    char saved = c->buf[hdr_end - 2];
    c->buf[hdr_end - 2] = '\0';                /* terminate header block temporarily */
    const char *cl = find_header(c->buf, "Content-Length");
    const char *te = find_header(c->buf, "Transfer-Encoding");
    const char *cn = find_header(c->buf, "Connection");
    const char *ra = find_header(c->buf, "Retry-After");
    long content_len = cl ? strtol(cl, NULL, 10) : -1;
    int chunked = te && !strncmp(te, "chunked", 7);
    int close_after = cn && (!strncmp(cn, "close", 5) || !strncmp(cn, "Close", 5));
    r.retry_after = ra ? atof(ra) : 0;
    c->buf[hdr_end - 2] = saved;

    if (content_len >= 0) {
        size_t total = hdr_end + (size_t)content_len;
        if (ensure_cap(c, total + 1) != 0) { http_conn_close(c); return r; }
        while (got < total) {
            int k = recv(s, c->buf + got, (int)(total - got), 0);
            if (k <= 0) { http_conn_close(c); return r; }
            got += (size_t)k;
        }
        c->buf[total] = '\0';
        r.body = c->buf + hdr_end;
        r.body_len = (size_t)content_len;
    } else if (chunked) {
        /* read until terminating chunk, then de-chunk in place */
        for (;;) {
            c->buf[got] = '\0';
            if (strstr(c->buf + hdr_end, "\r\n0\r\n\r\n") || !strncmp(c->buf + hdr_end, "0\r\n\r\n", 5)) break;
            if (ensure_cap(c, got + 4097) != 0) { http_conn_close(c); return r; }
            int k = recv(s, c->buf + got, 4096, 0);
            if (k <= 0) { http_conn_close(c); return r; }
            got += (size_t)k;
        }
        char *rd = c->buf + hdr_end, *wr = rd;
        for (;;) {
            long sz = strtol(rd, NULL, 16);
            char *nl = strstr(rd, "\r\n");
            if (!nl || sz <= 0) break;
            rd = nl + 2;
            memmove(wr, rd, (size_t)sz);
            wr += sz; rd += sz + 2;
        }
        *wr = '\0';
        r.body = c->buf + hdr_end;
        r.body_len = (size_t)(wr - r.body);
    } else {
        /* no length: read until the server closes */
        for (;;) {
            if (ensure_cap(c, got + 4097) != 0) break;
            int k = recv(s, c->buf + got, 4096, 0);
            if (k <= 0) break;
            got += (size_t)k;
        }
        c->buf[got] = '\0';
        r.body = c->buf + hdr_end;
        r.body_len = got - hdr_end;
        close_after = 1;
    }
    if (close_after) http_conn_close(c);
    r.status = status;
    return r;
}

http_resp http_request(http_conn *c, const char *method, const char *path, const char *api_key) {
    http_resp r = request_once(c, method, path, api_key);
    /* Stale keep-alive socket: reconnect and retry once, but ONLY for GET. Retrying a POST
     * could submit the same order twice; callers re-read positions instead. */
    if (r.status == -1 && !strcmp(method, "GET")) {
        http_conn_close(c);
        r = request_once(c, method, path, api_key);
    }
    return r;
}
