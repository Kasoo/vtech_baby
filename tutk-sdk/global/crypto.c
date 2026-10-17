/* Crypto wrappers over the host OpenSSL (kit: global/70-wrappers.md). */
#include <stdlib.h>
#include <string.h>

#include "ossl.h"
#include "tk.h"

struct TEcdhKeyPair {
    EC_KEY *key;
};

TK_EXPORT int TUTK3rdSHA256(const void *data, int len, uint8_t *out, unsigned int out_size) {
    tk_trace("SHA256(len %d)", len);
    if (out_size < 32)
        return -1;
    return SHA256(data, (size_t)len, out) ? 0 : -1;
}

TK_EXPORT int TUTK3rdSHA1HMAC(const void *key, int key_len, const void *data, int data_len,
                              uint8_t *out, unsigned int out_size) {
    unsigned int n = 0;

    tk_trace("SHA1HMAC(key_len %d, data_len %d, out_size %u)", key_len, data_len, out_size);
    if (out_size < 20)
        return -1;
    return HMAC(EVP_sha1(), key, key_len, data, (size_t)data_len, out, &n) ? 0 : -1;
}

static int b64_value(unsigned char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

/* Standard base64, `len` a multiple of 4, '=' padding only in the last quantum. */
TK_EXPORT int TUTK3rdBase64Decode(const void *b64, unsigned int len, void **out) {
    const unsigned char *in = b64;
    unsigned char *buf;
    int n = 0;

    if (b64 == NULL || out == NULL || len % 4 != 0)
        return TUTK_ER_INVALID_ARG;
    buf = malloc(len / 4 * 3 + 1);
    if (buf == NULL)
        return TUTK_ER_MEM_INSUFFICIENT;
    for (unsigned int i = 0; i < len; i += 4) {
        int v[4], pad = 0;
        bool last = i + 4 == len;

        for (int j = 0; j < 4; j++) {
            if (last && j >= 2 && in[i + j] == '=' && (j == 3 || in[i + 3] == '=')) {
                v[j] = 0;
                pad++;
                continue;
            }
            v[j] = b64_value(in[i + j]);
            if (v[j] < 0) {
                free(buf);
                return TUTK_ER_INVALID_ARG;
            }
        }
        buf[n++] = (unsigned char)(v[0] << 2 | v[1] >> 4);
        if (pad < 2)
            buf[n++] = (unsigned char)(v[1] << 4 | v[2] >> 2);
        if (pad < 1)
            buf[n++] = (unsigned char)(v[2] << 6 | v[3]);
    }
    *out = buf;
    return n;
}

int TUTK3rdXOR(const void *in, unsigned int in_len, const void *key, unsigned int key_len,
               void *out, unsigned int out_len) {
    const uint8_t *src = in, *k = key;
    uint8_t *dst = out;

    if (out_len < in_len || key_len == 0)
        return -1;
    for (unsigned int i = 0; i < in_len; i++)
        dst[i] = src[i] ^ k[i % key_len];
    return (int)in_len;
}

static RSA *rsa_load(const char *pem_key, int is_public) {
    BIO *bio = BIO_new_mem_buf(pem_key, -1);
    RSA *rsa;

    if (bio == NULL)
        return NULL;
    rsa = is_public ? PEM_read_bio_RSA_PUBKEY(bio, NULL, NULL, NULL)
                    : PEM_read_bio_RSAPrivateKey(bio, NULL, NULL, NULL);
    BIO_free(bio);
    return rsa;
}

int TUTK3rdRSABlockSize(const char *pem_key, int is_public) {
    RSA *rsa;
    int size;

    if (pem_key == NULL || (rsa = rsa_load(pem_key, is_public)) == NULL)
        return -1;
    size = RSA_size(rsa);
    RSA_free(rsa);
    return size;
}

/* Raw RSA, no padding, one modulus-sized block at a time. */
int TUTK3rdRSACrypt(const char *pem_key, int is_public, int encrypt, const void *in, int in_len,
                    void *out, unsigned int out_len) {
    const unsigned char *src = in;
    unsigned char *dst = out;
    RSA *rsa;
    int k, written = 0;

    if (pem_key == NULL || in == NULL || out == NULL || (rsa = rsa_load(pem_key, is_public)) == NULL)
        return -1;
    k = RSA_size(rsa);
    if (in_len <= 0 || in_len % k != 0 || out_len < (unsigned int)in_len) {
        RSA_free(rsa);
        return -1;
    }
    for (int off = 0; off < in_len; off += k) {
        int n;
        if (is_public)
            n = encrypt ? RSA_public_encrypt(k, src + off, dst + written, rsa, RSA_NO_PADDING)
                        : RSA_public_decrypt(k, src + off, dst + written, rsa, RSA_NO_PADDING);
        else
            n = encrypt ? RSA_private_encrypt(k, src + off, dst + written, rsa, RSA_NO_PADDING)
                        : RSA_private_decrypt(k, src + off, dst + written, rsa, RSA_NO_PADDING);
        if (n != k) {
            RSA_free(rsa);
            return -1;
        }
        written += n;
    }
    RSA_free(rsa);
    return written;
}

int TUTK3rdRSADecrypt(const char *pem_key, int is_public, const void *in, int in_len,
                      void *out, unsigned int out_len) {
    return TUTK3rdRSACrypt(pem_key, is_public, 0, in, in_len, out, out_len);
}

TK_EXPORT TEcdhKeyPair *TUTK3rdECDHCreateKeyPair(void) {
    TEcdhKeyPair *pair;
    EC_KEY *key = EC_KEY_new_by_curve_name(NID_X9_62_prime256v1);

    tk_trace("ECDHCreateKeyPair");
    if (key == NULL)
        return NULL;
    if (EC_KEY_generate_key(key) != 1 || (pair = malloc(sizeof *pair)) == NULL) {
        EC_KEY_free(key);
        return NULL;
    }
    pair->key = key;
    return pair;
}

TK_EXPORT void TUTK3rdECDHKeysFree(TEcdhKeyPair *keypair) {
    if (keypair == NULL)
        return;
    EC_KEY_free(keypair->key);
    free(keypair);
}
