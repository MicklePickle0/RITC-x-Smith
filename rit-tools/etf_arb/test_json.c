/* quick self-test for json.c:  make test_json */
#include "json.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

int main(void) {
    char s1[] = "{\"bids\":[{\"price\":24.98,\"quantity\":5000,\"quantity_filled\":1000}],"
                "\"asks\":[],\"msg\":\"a\\\"b\\u00e9\\n\",\"ok\":true,\"n\":null,\"e\":-1.5e2}";
    jdoc *d = json_parse(s1, strlen(s1));
    assert(d);
    jnode *r = json_root(d);
    jnode *bid0 = json_at(json_get(r, "bids"), 0);
    assert(fabs(json_num(bid0, "price", 0) - 24.98) < 1e-12);
    assert(json_num(bid0, "quantity", 0) == 5000);
    assert(json_get(r, "asks")->len == 0);
    assert(!strcmp(json_str(r, "msg", ""), "a\"b\xc3\xa9\n"));
    assert(json_bool(r, "ok", 0) == 1);
    assert(isnan(json_num(r, "n", NAN)));
    assert(json_num(r, "e", 0) == -150);
    json_free(d);

    char s2[] = "[{\"tender_id\":3,\"price\":null,\"is_fixed_bid\":false}, 1, \"x\"]";
    d = json_parse(s2, strlen(s2));
    assert(d && json_root(d)->len == 3);
    assert(json_bool(json_at(json_root(d), 0), "is_fixed_bid", 1) == 0);
    json_free(d);

    char bad[] = "{\"a\":[1,2}";
    assert(json_parse(bad, strlen(bad)) == NULL);
    puts("json tests passed");
    return 0;
}
