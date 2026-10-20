/* Licence check and master-server names (kit: global/50-licence.md, global/60-master-names.md). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "tk.h"

#define REALM_MAX      128
#define STANDALONE_MAX 10
#define DOMAIN_SIZE    512
#define KEY_MASK_SIZE  16
#define CONTENT_KEY    32

/* ThroughTek's licence-signing public key (RSA-1024, e = 65537). */
static const char LICENCE_PUBLIC_KEY[] =
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDQXw9rYBtx25rSHy3kNotbSoys\n"
    "Cb+DqSieB7PhXgJENqOqa2Z+nlcRbgQ+lnptlVYelWPR5phmkW2OSWwoQByvXVkh\n"
    "Nuiz5CAMNV+6WCYF84DQNstarDLwt7L6YhS8fTHt5ODv1uwArchCJp67+cS+gRK4\n"
    "95wiu9ejOoqiDmQGYwIDAQAB\n"
    "-----END PUBLIC KEY-----\n";

/* Set once, before any reader runs, so nothing locks it. */
static struct {
    char default_region[REALM_MAX];
    bool valid;
    struct {
        char name[REALM_MAX + 1];
        char value[REALM_MAX + 1];
    } standalone[STANDALONE_MAX];
    int     nebula_domain_from_license;
    char    realm[REALM_MAX + 1];
    uint8_t key_mask[KEY_MASK_SIZE];
} licence = {.default_region = "us"};

static void copy_str(char *dst, size_t size, const char *src) {
    snprintf(dst, size, "%s", src);
}

int CheckLicenseKeyHash(const char *json, const uint8_t key[CONTENT_KEY]) {
    uint8_t digest[CONTENT_KEY];

    if (TUTK3rdSHA256(json, (int)strlen(json), digest, sizeof digest) != 0)
        return TUTK_ER_INVALID_LICENSE_KEY;
    digest[0] = 0x11;
    return memcmp(digest, key, CONTENT_KEY) == 0 ? 0 : TUTK_ER_INVALID_LICENSE_KEY;
}

/* Decode a licence key: version 1, one RSA block holding the content key, then the JSON document
 * XORed with that key. On success *json_out is a NUL-terminated copy, freed with tutk_platform_free. */
int CheckLicenseKeyIsValid(const char *key, const char *public_key_pem, char **json_out) {
    uint8_t *raw = NULL, *block = NULL;
    char *json;
    int n, k, body, rc = TUTK_ER_INVALID_LICENSE_KEY;
    uint32_t version;

    if (key == NULL || public_key_pem == NULL || json_out == NULL)
        return TUTK_ER_INVALID_ARG;
    n = TUTK3rdBase64Decode(key, (unsigned int)strlen(key), (void **)&raw);
    if (n == TUTK_ER_MEM_INSUFFICIENT)
        return n;
    if (n < 0)
        return TUTK_ER_INVALID_LICENSE_KEY;

    if (n < 4)
        goto out;
    memcpy(&version, raw, 4);
    k = TUTK3rdRSABlockSize(public_key_pem, 1);
    if (version != 1 || k < CONTENT_KEY || n < 4 + k + 1)
        goto out;
    block = tutk_platform_malloc(k);
    if (block == NULL) {
        rc = TUTK_ER_MEM_INSUFFICIENT;
        goto out;
    }
    if (TUTK3rdRSADecrypt(public_key_pem, 1, raw + 4, k, block, (unsigned int)k) != k)
        goto out;

    body = n - 4 - k;
    json = tutk_platform_calloc(body + 1, 1);
    if (json == NULL) {
        rc = TUTK_ER_MEM_INSUFFICIENT;
        goto out;
    }
    TUTK3rdXOR(raw + 4 + k, (unsigned int)body, block, CONTENT_KEY, json, (unsigned int)body);
    if (CheckLicenseKeyHash(json, block) != 0) {
        tutk_platform_free(json);
        goto out;
    }
    *json_out = json;
    rc = 0;
out:
    tutk_platform_free(block);
    tutk_platform_free(raw);
    return rc;
}

/* Copy the optional members; a malformed one rejects the licence. */
static int load_optional(json_object *root) {
    json_object *member;
    int flag;

    if (TUTK_Json_Obj_Get_Sub_Obj_Bool(root, "nebulaDomainFromLicense", &flag) == 0)
        licence.nebula_domain_from_license = flag;

    if (TUTK_Json_Obj_Get_Sub_Obj(root, "keyMask", &member) == 0) {
        const char *text = tk_json_string(member);
        uint8_t *mask = NULL;
        int n;

        if (text == NULL)
            return TUTK_ER_INVALID_LICENSE_KEY;
        n = TUTK3rdBase64Decode(text, (unsigned int)strlen(text), (void **)&mask);
        if (n < 0)
            return TUTK_ER_INVALID_LICENSE_KEY;
        if (n == KEY_MASK_SIZE)
            memcpy(licence.key_mask, mask, KEY_MASK_SIZE);
        tutk_platform_free(mask);
        if (n != KEY_MASK_SIZE)
            return TUTK_ER_INVALID_LICENSE_KEY;
    }

    if (TUTK_Json_Obj_Get_Sub_Obj(root, "standalone", &member) == 0) {
        int count = tk_json_obj_count(member);

        if (count > STANDALONE_MAX)
            count = STANDALONE_MAX;
        for (int i = 0; i < count; i++) {
            const char *value = tk_json_string(tk_json_obj_value(member, i));
            if (value == NULL)
                return TUTK_ER_INVALID_LICENSE_KEY;
            copy_str(licence.standalone[i].name, sizeof licence.standalone[i].name,
                     tk_json_obj_key(member, i));
            copy_str(licence.standalone[i].value, sizeof licence.standalone[i].value, value);
        }
    }
    return 0;
}

static int SetLicenseKey(const char *key) {
    json_object *root = NULL;
    const char *realm;
    char *json = NULL;
    int rc;

    memset(&licence, 0, sizeof licence);
    copy_str(licence.default_region, sizeof licence.default_region, "us");

    rc = CheckLicenseKeyIsValid(key, LICENCE_PUBLIC_KEY, &json);
    if (rc != 0)
        return rc;
    rc = TUTK_ER_INVALID_LICENSE_KEY;
    if (TUTK_Json_Obj_Create_From_String(json, &root) != 0)
        goto out;
    if (TUTK_Json_Obj_Get_Sub_Obj_String(root, "realm", &realm) != 0)
        goto out;
    copy_str(licence.realm, sizeof licence.realm, realm);
    rc = load_optional(root);
    if (rc == 0)
        licence.valid = true;
out:
    if (root)
        TUTK_Json_Obj_Release(root);
    tutk_platform_free(json);
    return rc;
}

TK_EXPORT int TUTK_SDK_Set_License_Key(const char *key) {
    if (key == NULL)
        return TUTK_ER_INVALID_ARG;
    return SetLicenseKey(key);
}

TK_EXPORT int GetLicenseKeyMask(uint8_t *out, int size) {
    if (out == NULL || size < KEY_MASK_SIZE)
        return TUTK_ER_INVALID_ARG;
    if (!licence.valid)
        return TUTK_ER_NO_LICENSE_KEY;
    memcpy(out, licence.key_mask, KEY_MASK_SIZE);
    return 0;
}

/* ---- master-server names ---- */

static int SearchStandaloneDomainName(const char *realm, char **out) {
    for (int i = 0; i < STANDALONE_MAX && licence.standalone[i].name[0]; i++) {
        if (strcmp(licence.standalone[i].name, realm) == 0) {
            if ((*out = tutk_platform_calloc(DOMAIN_SIZE, 1)) == NULL)
                return TUTK_ER_MEM_INSUFFICIENT;
            copy_str(*out, DOMAIN_SIZE, licence.standalone[i].value);
            return 0;
        }
    }
    return TUTK_ER_INVALID_ARG;
}

/* IOTC only needs a stable, non-empty name, and Global never resolves names, so this is a
 * placeholder under the reserved .invalid domain rather than ThroughTek's real host name. */
static int CreateDomainName(unsigned int kind, char **out, const char *realm, const char *region) {
    if ((*out = tutk_platform_calloc(DOMAIN_SIZE, 1)) == NULL)
        return TUTK_ER_MEM_INSUFFICIENT;
    snprintf(*out, DOMAIN_SIZE, "server%u.%s.%s.lan-only.invalid", kind, region, realm);
    return 0;
}

TK_EXPORT int GetMasterDomainName(int kind, unsigned int region, const char *realm, char **out) {
    const char *region_name;

    if (region >= 2)
        return -1;
    if (out == NULL)
        return TUTK_ER_INVALID_ARG;
    if (!licence.valid)
        return TUTK_ER_NO_LICENSE_KEY;
    if (realm == NULL || realm[0] == '\0') {
        realm = licence.realm;
        region_name = licence.default_region;
    } else {
        region_name = region == 1 ? "cn" : licence.default_region;
    }
    tk_trace("GetMasterDomainName(kind %d, region %u)", kind, region);
    if (SearchStandaloneDomainName(realm, out) == 0)
        return 0;
    return CreateDomainName((unsigned int)kind, out, realm, region_name);
}
